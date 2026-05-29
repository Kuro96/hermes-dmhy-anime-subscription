"""Bangumi title lookup using only the Python standard library."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

API_URL = "https://api.bgm.tv/v0/search/subjects"
SUBJECT_API_URL = "https://api.bgm.tv/v0/subjects"
EPISODES_API_URL = "https://api.bgm.tv/v0/episodes"
DEFAULT_TIMEOUT_SECONDS = 5.0
USER_AGENT = "hermes-dmhy-anime-subscription/0.1 (+https://bangumi.tv)"


@dataclass(frozen=True, slots=True)
class BangumiSubjectEpisodes:
    subject_id: int
    eps: int
    main_episode_numbers: tuple[int, ...]


def lookup_chinese_title(title: str, *, opener: Callable[..., Any] = urlopen, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str | None:
    """Return the first Bangumi Chinese subject name for an anime title."""

    query = title.strip()
    if not query:
        return None
    payload = json.dumps(
        {
            "keyword": query,
            "sort": "match",
            "filter": {"type": [2]},
        }
    ).encode("utf-8")
    request = Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, TimeoutError, URLError):
        return None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    data = decoded.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        chinese_title = item.get("name_cn")
        if isinstance(chinese_title, str) and chinese_title.strip():
            return chinese_title.strip()
    return None


def fetch_subject_main_episodes(
    subject_id: int,
    *,
    opener: Callable[..., Any] = urlopen,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> BangumiSubjectEpisodes:
    eps = _fetch_subject_eps(subject_id, opener=opener, timeout=timeout)
    if eps <= 0:
        return BangumiSubjectEpisodes(subject_id=subject_id, eps=eps, main_episode_numbers=())
    numbers = _fetch_main_episode_numbers(subject_id, opener=opener, timeout=timeout)
    return BangumiSubjectEpisodes(subject_id=subject_id, eps=eps, main_episode_numbers=numbers)


def _fetch_subject_eps(subject_id: int, *, opener: Callable[..., Any], timeout: float) -> int:
    request = Request(
        f"{SUBJECT_API_URL}/{subject_id}",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    decoded = _read_json(request, opener=opener, timeout=timeout)
    if not isinstance(decoded, dict):
        return 0
    eps = decoded.get("eps")
    return eps if isinstance(eps, int) and not isinstance(eps, bool) and eps > 0 else 0


def _fetch_main_episode_numbers(subject_id: int, *, opener: Callable[..., Any], timeout: float) -> tuple[int, ...]:
    numbers: set[int] = set()
    offset = 0
    limit = 200
    while True:
        url = f"{EPISODES_API_URL}?subject_id={subject_id}&type=0&limit={limit}&offset={offset}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}, method="GET")
        decoded = _read_json(request, opener=opener, timeout=timeout)
        if not isinstance(decoded, dict):
            return tuple(sorted(numbers))
        data = decoded.get("data")
        if not isinstance(data, list):
            return tuple(sorted(numbers))
        for item in data:
            number = _episode_number(item)
            if number is not None:
                numbers.add(number)
        total = decoded.get("total")
        offset += limit
        if not isinstance(total, int) or offset >= total or not data:
            return tuple(sorted(numbers))


def _read_json(request: Request, *, opener: Callable[..., Any], timeout: float) -> Any:
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, TimeoutError, URLError):
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _episode_number(item: Any) -> int | None:
    if not isinstance(item, dict):
        return None
    for key in ("ep", "sort"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                parsed = float(value)
            except ValueError:
                continue
            if parsed.is_integer():
                return int(parsed)
    return None
