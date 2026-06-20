"""Privacy-minimized HTTP callback episode parser."""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import PureWindowsPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .config import OrganizerEpisodeParserConfig
from .organizer import EpisodeParserResult

SAFE_CONTEXT_KEYS = frozenset(
    {"rule_name", "bangumi_subject_id", "release_group", "quality", "category"}
)


class CallbackEpisodeParser:
    def __init__(
        self,
        config: OrganizerEpisodeParserConfig,
        *,
        title: str | None = None,
        source_path: str | os.PathLike[str] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.config = config
        self.title = title
        self.source_path = str(source_path) if source_path else None
        self.metadata = metadata or {}
        self._opener = build_opener()
        self._cache: dict[str, EpisodeParserResult | None] = {}

    def __call__(self, text: str) -> EpisodeParserResult | None:
        if text in self._cache:
            return self._cache[text]
        result = self._request_parse(text)
        self._cache[text] = result
        return result

    def _request_parse(self, text: str) -> EpisodeParserResult | None:
        if self.config.mode != "callback" or not self.config.callback_url_env:
            return None
        url = os.environ.get(self.config.callback_url_env)
        if not _valid_callback_url(url):
            return None
        request = Request(
            url,
            data=json.dumps(
                self._payload(text),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.config.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    return None
                raw_body = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, socket.timeout, OSError):
            return None
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return None
        if not isinstance(body, dict):
            return None
        return _result_from_response(body, min_confidence=self.config.min_confidence)

    def _payload(self, text: str) -> dict[str, object]:
        payload: dict[str, object] = {"task": "organizer_episode_parse", "text": text}
        if self.title:
            payload["title"] = self.title
        source_name = _source_basename(self.source_path)
        if source_name:
            payload["source_name"] = source_name
        safe_context = {
            key: value
            for key, value in self.metadata.items()
            if key in SAFE_CONTEXT_KEYS and _safe_context_value(value)
        }
        if safe_context:
            payload["safe_context"] = safe_context
        return payload


def _source_basename(source_path: str | None) -> str | None:
    if not source_path:
        return None
    name = PureWindowsPath(source_path).name
    return name or None


def _valid_callback_url(url: str | None) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def _safe_context_value(value: object) -> bool:
    if isinstance(value, bool | int | float):
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    return not _looks_sensitive_or_path_like(value)


def _looks_sensitive_or_path_like(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.lower()
    if "://" in lowered or "?" in stripped:
        return True
    if "/" in stripped or "\\" in stripped:
        return True
    if re.search(r"\b(?:token|secret|password|passwd|apikey|api_key|credential)\b", lowered):
        return True
    return False


def _result_from_response(
    body: dict[str, Any], *, min_confidence: float
) -> EpisodeParserResult | None:
    confidence = body.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return None
    confidence_value = float(confidence)
    if not 0 <= confidence_value <= 1 or confidence_value < min_confidence:
        return None

    raw_title = body.get("series_title")
    if raw_title is not None and (not isinstance(raw_title, str) or not raw_title.strip()):
        return None
    season = _positive_int_or_none(body.get("season"))
    episode = _positive_int_or_none(body.get("episode"))
    if ("season" in body and season is None) or ("episode" in body and episode is None):
        return None
    release_group = _optional_text(body.get("release_group"))
    quality = _optional_text(body.get("quality"))
    if ("release_group" in body and release_group is None) or (
        "quality" in body and quality is None
    ):
        return None
    if raw_title is None and season is None and episode is None and release_group is None and quality is None:
        return None
    return EpisodeParserResult(
        series_title=raw_title.strip() if isinstance(raw_title, str) else None,
        season=season,
        episode=episode,
        release_group=release_group,
        quality=quality,
    )


def _positive_int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
