"""qBittorrent Web API submission client."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from http.cookiejar import CookieJar
import os
import socket
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .config import QbittorrentConfig
from .models import ReleaseCandidate, SubscriptionRule


DEFAULT_TIMEOUT_SECONDS = 30.0
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
DUPLICATE_MARKERS = (
    "already in the transfer list",
    "already exists",
    "torrent already",
    "duplicate torrent",
    "duplicated torrent",
)


@dataclass(frozen=True, slots=True)
class QbittorrentHttpRequest:
    url: str
    data: bytes
    headers: dict[str, str]
    timeout: float
    method: str = "POST"


@dataclass(frozen=True, slots=True)
class QbittorrentHttpResponse:
    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class QbittorrentTransport(Protocol):
    def send(self, request: QbittorrentHttpRequest) -> QbittorrentHttpResponse:
        """Send an HTTP request to qBittorrent."""



@dataclass(frozen=True, slots=True)
class QbittorrentTorrent:
    """Read-only torrent state returned by qBittorrent /torrents/info."""

    torrent_hash: str
    name: str
    state: str
    progress: float
    save_path: str | None = None
    content_path: str | None = None
    completion_on: int | None = None
    raw: dict[str, object] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class QbittorrentSubmissionPlan:
    title: str
    rule_name: str
    source: str | None
    category: str | None
    tags: tuple[str, ...]
    save_path: str | None
    endpoint: str
    dry_run: bool = True

    def payload(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.source:
            values["urls"] = self.source
        if self.category:
            values["category"] = self.category
        if self.tags:
            values["tags"] = ",".join(self.tags)
        if self.save_path:
            values["savepath"] = self.save_path
        return values


@dataclass(frozen=True, slots=True)
class QbittorrentError:
    kind: str
    message: str
    retryable: bool
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class QbittorrentSubmitResult:
    success: bool
    status: str
    message: str
    plan: QbittorrentSubmissionPlan
    retryable: bool = False
    duplicate: bool = False
    dry_run: bool = False
    error: QbittorrentError | None = None
    http_status: int | None = None


class UrllibQbittorrentTransport:
    """Cookie-preserving urllib transport for qBittorrent Web API calls."""

    def __init__(self) -> None:
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def send(self, request: QbittorrentHttpRequest) -> QbittorrentHttpResponse:
        urllib_request = Request(
            request.url,
            data=request.data,
            headers=request.headers,
            method=request.method,
        )
        try:
            with self._opener.open(urllib_request, timeout=request.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return QbittorrentHttpResponse(status=response.status, body=body, headers=dict(response.headers.items()))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return QbittorrentHttpResponse(status=exc.code, body=body, headers=dict(exc.headers.items()))


class QbittorrentClient:
    def __init__(
        self,
        config: QbittorrentConfig,
        *,
        username: str | None = None,
        password: str | None = None,
        transport: QbittorrentTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self.username = username
        self.password = password
        self.transport = transport or UrllibQbittorrentTransport()
        self.timeout = timeout

    @classmethod
    def from_config_env(
        cls,
        config: QbittorrentConfig,
        *,
        transport: QbittorrentTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> "QbittorrentClient":
        username = os.environ.get(config.username_env) if config.username_env else None
        password = os.environ.get(config.password_env) if config.password_env else None
        return cls(config, username=username, password=password, transport=transport, timeout=timeout)

    def submit(
        self,
        candidate: ReleaseCandidate,
        *,
        rule: SubscriptionRule | None = None,
        dry_run: bool = False,
    ) -> QbittorrentSubmitResult:
        plan = plan_qbittorrent_submission(candidate, self.config, rule=rule, dry_run=dry_run)
        if dry_run:
            return QbittorrentSubmitResult(
                success=True,
                status="planned",
                message="Dry-run planned qBittorrent submission without HTTP mutation",
                plan=plan,
                dry_run=True,
            )
        if plan.source is None:
            return _failure(plan, "validation", "Candidate has no magnet URI or torrent URL", retryable=False)

        if self._auth_enabled:
            auth_error = self.login()
            if auth_error is not None:
                return _failure(
                    plan,
                    auth_error.kind,
                    auth_error.message,
                    retryable=auth_error.retryable,
                    http_status=auth_error.http_status,
                )

        try:
            response = self._post_form("/api/v2/torrents/add", plan.payload())
        except _RetryableTransportError as exc:
            return _failure(plan, "transport", str(exc), retryable=True)

        if _is_successful_add(response):
            return QbittorrentSubmitResult(
                success=True,
                status="submitted",
                message="Torrent submitted to qBittorrent",
                plan=plan,
                http_status=response.status,
            )
        if _is_duplicate_response(response):
            return QbittorrentSubmitResult(
                success=True,
                status="duplicate",
                message="Torrent was already present in qBittorrent",
                plan=plan,
                duplicate=True,
                http_status=response.status,
            )
        return _failure(
            plan,
            "api",
            _response_message(response, "qBittorrent rejected torrent submission"),
            retryable=response.status in RETRYABLE_STATUS_CODES,
            http_status=response.status,
        )


    def list_torrents(self, *, category: str | None = None, all_categories: bool = False) -> tuple[QbittorrentTorrent, ...]:
        """Return qBittorrent torrent states for monitoring without mutating torrents.

        By default this lists the configured qBittorrent category for backwards
        compatibility with the submission config. Pass ``all_categories=True``
        to omit the category query parameter and fetch every torrent visible to
        the qBittorrent API.
        """

        if self._auth_enabled:
            auth_error = self.login()
            if auth_error is not None:
                raise RuntimeError(auth_error.message)
        selected_category = None if all_categories else (self.config.category if category is None else category)
        values = {"category": selected_category} if selected_category is not None else {}
        try:
            response = self._get("/api/v2/torrents/info", values)
        except _RetryableTransportError as exc:
            raise RuntimeError(str(exc)) from exc
        if response.status != 200:
            raise RuntimeError(_response_message(response, "qBittorrent torrent listing failed"))
        try:
            raw_items = json.loads(response.body or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("qBittorrent torrent listing returned invalid JSON") from exc
        if not isinstance(raw_items, list):
            raise RuntimeError("qBittorrent torrent listing returned a non-list payload")
        return tuple(_torrent_from_payload(item) for item in raw_items if isinstance(item, dict))

    def login(self) -> QbittorrentError | None:
        if not self._auth_enabled:
            return None
        try:
            response = self._post_form(
                "/api/v2/auth/login",
                {"username": self.username or "", "password": self.password or ""},
            )
        except _RetryableTransportError as exc:
            return QbittorrentError(kind="transport", message=str(exc), retryable=True)

        body = response.body.strip().casefold()
        if response.status == 200 and body.startswith("ok"):
            return None
        retryable = response.status in RETRYABLE_STATUS_CODES
        return QbittorrentError(
            kind="auth",
            message=_response_message(response, "qBittorrent authentication failed"),
            retryable=retryable,
            http_status=response.status,
        )

    @property
    def _auth_enabled(self) -> bool:
        return self.username is not None or self.password is not None


    def _get(self, path: str, values: dict[str, str]) -> QbittorrentHttpResponse:
        query = urlencode(values)
        url = urljoin(_base_url(self.config.endpoint), path.lstrip("/"))
        if query:
            url = f"{url}?{query}"
        request = QbittorrentHttpRequest(
            url=url,
            data=b"",
            headers={"Referer": _base_url(self.config.endpoint).rstrip("/")},
            timeout=self.timeout,
            method="GET",
        )
        try:
            return self.transport.send(request)
        except (TimeoutError, socket.timeout, URLError, OSError) as exc:
            raise _RetryableTransportError(f"qBittorrent request failed: {exc.__class__.__name__}") from exc

    def _post_form(self, path: str, values: dict[str, str]) -> QbittorrentHttpResponse:
        body = urlencode(values).encode("utf-8")
        request = QbittorrentHttpRequest(
            url=urljoin(_base_url(self.config.endpoint), path.lstrip("/")),
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": _base_url(self.config.endpoint).rstrip("/"),
            },
            timeout=self.timeout,
        )
        try:
            return self.transport.send(request)
        except (TimeoutError, socket.timeout, URLError, OSError) as exc:
            raise _RetryableTransportError(f"qBittorrent request failed: {exc.__class__.__name__}") from exc


def plan_qbittorrent_submission(
    candidate: ReleaseCandidate,
    config: QbittorrentConfig,
    *,
    rule: SubscriptionRule | None = None,
    dry_run: bool = True,
) -> QbittorrentSubmissionPlan:
    item = candidate.feed_item
    return QbittorrentSubmissionPlan(
        title=candidate.title,
        rule_name=candidate.rule_name,
        source=item.magnet_uri or item.torrent_url,
        category=(rule.category if rule and rule.category else config.category),
        tags=config.tags,
        save_path=(rule.save_path if rule and rule.save_path else config.save_path),
        endpoint=config.endpoint,
        dry_run=dry_run,
    )


class _RetryableTransportError(RuntimeError):
    pass


def _base_url(endpoint: str) -> str:
    return endpoint.rstrip("/") + "/"


def _is_successful_add(response: QbittorrentHttpResponse) -> bool:
    return response.status == 200 and not _is_duplicate_response(response)


def _is_duplicate_response(response: QbittorrentHttpResponse) -> bool:
    text = response.body.strip().casefold()
    return any(marker in text for marker in DUPLICATE_MARKERS)


def _response_message(response: QbittorrentHttpResponse, default: str) -> str:
    body = response.body.strip()
    if body:
        return f"{default}: {body}"
    return default



def _torrent_from_payload(payload: dict[str, object]) -> QbittorrentTorrent:
    return QbittorrentTorrent(
        torrent_hash=str(payload.get("hash") or "").lower(),
        name=str(payload.get("name") or ""),
        state=str(payload.get("state") or "unknown"),
        progress=float(payload.get("progress") or 0.0),
        save_path=str(payload["save_path"]) if payload.get("save_path") is not None else None,
        content_path=str(payload["content_path"]) if payload.get("content_path") is not None else None,
        completion_on=int(payload["completion_on"]) if payload.get("completion_on") else None,
        raw=dict(payload),
    )

def _failure(
    plan: QbittorrentSubmissionPlan,
    kind: str,
    message: str,
    *,
    retryable: bool,
    http_status: int | None = None,
) -> QbittorrentSubmitResult:
    return QbittorrentSubmitResult(
        success=False,
        status="failed",
        message=message,
        plan=plan,
        retryable=retryable,
        error=QbittorrentError(kind=kind, message=message, retryable=retryable, http_status=http_status),
        http_status=http_status,
    )
