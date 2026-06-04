"""Telegram Bot API notification delivery for organizer completions."""

from __future__ import annotations

import html
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .config import TelegramConfig
from .models import FailureRecord, NotificationEvent

DEFAULT_TIMEOUT_SECONDS = 30.0
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
API_ROOT = "https://api.telegram.org"


@dataclass(frozen=True, slots=True)
class TelegramHttpRequest:
    url: str
    data: bytes
    headers: dict[str, str]
    timeout: float
    method: str = "POST"


@dataclass(frozen=True, slots=True)
class TelegramHttpResponse:
    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class TelegramTransport(Protocol):
    def send(self, request: TelegramHttpRequest) -> TelegramHttpResponse:
        """Send a Telegram HTTP request."""


@dataclass(frozen=True, slots=True)
class TelegramDeliveryPlan:
    url: str
    redacted_url: str
    method: str
    payload: dict[str, str | int]

    def body(self) -> bytes:
        return urlencode(self.payload).encode("utf-8")


@dataclass(frozen=True, slots=True)
class TelegramError:
    kind: str
    message: str
    retryable: bool
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramDispatchResult:
    success: bool
    status: str
    message: str
    plan: TelegramDeliveryPlan
    retryable: bool = False
    disabled: bool = False
    error: TelegramError | None = None
    failure: FailureRecord | None = None
    http_status: int | None = None


class UrllibTelegramTransport:
    def __init__(self) -> None:
        self._opener = build_opener()

    def send(self, request: TelegramHttpRequest) -> TelegramHttpResponse:
        urllib_request = Request(
            request.url,
            data=request.data,
            headers=request.headers,
            method=request.method,
        )
        try:
            with self._opener.open(urllib_request, timeout=request.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return TelegramHttpResponse(status=response.status, body=body, headers=dict(response.headers.items()))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return TelegramHttpResponse(status=exc.code, body=body, headers=dict(exc.headers.items()))


class TelegramNotifier:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        transport: TelegramTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibTelegramTransport()

    def notify(self, event: NotificationEvent, *, cover_url: str | None = None) -> TelegramDispatchResult:
        if not self.config.enabled:
            plan = TelegramDeliveryPlan(url="", redacted_url="<disabled>", method="disabled", payload={})
            return TelegramDispatchResult(True, "disabled", "Telegram notifications are disabled", plan, disabled=True)
        token = os.environ.get(self.config.bot_token_env or "")
        if not token:
            plan = TelegramDeliveryPlan(url="", redacted_url="<redacted>", method="configuration", payload={})
            message = f"Telegram bot token environment variable {self.config.bot_token_env} is not set"
            error = TelegramError("configuration", message, retryable=False)
            return TelegramDispatchResult(False, "failed", message, plan, error=error, failure=_failure_record(event, message, recoverable=False))
        plan = self._plan(event, cover_url=cover_url)
        try:
            response = self._post_form(plan, token)
        except _RetryableTransportError as exc:
            return _failure(plan, "transport", str(exc), retryable=True, event=event)
        if 200 <= response.status < 300:
            return TelegramDispatchResult(True, "sent", f"Telegram delivered via {plan.method}", plan, http_status=response.status)
        retryable = response.status in RETRYABLE_STATUS_CODES
        message = _response_message(response, f"Telegram delivery failed via {plan.method}", plan=plan)
        return _failure(plan, "api", message, retryable=retryable, http_status=response.status, event=event)

    def _plan(self, event: NotificationEvent, *, cover_url: str | None) -> TelegramDeliveryPlan:
        method = "sendPhoto" if cover_url else "sendMessage"
        payload: dict[str, str | int] = {
            "chat_id": self.config.chat_id or "",
            "parse_mode": self.config.parse_mode,
        }
        if self.config.message_thread_id is not None:
            payload["message_thread_id"] = self.config.message_thread_id
        text = _message_text(event, parse_mode=self.config.parse_mode)
        if cover_url:
            payload["photo"] = cover_url
            payload["caption"] = text
        else:
            payload["text"] = text
        url = f"{API_ROOT}/bot<redacted>/{method}"
        return TelegramDeliveryPlan(url=url, redacted_url=url, method=method, payload=payload)

    def _post_form(self, plan: TelegramDeliveryPlan, token: str) -> TelegramHttpResponse:
        request = TelegramHttpRequest(
            url=f"{API_ROOT}/bot{token}/{plan.method}",
            data=plan.body(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            timeout=self.config.timeout or DEFAULT_TIMEOUT_SECONDS,
        )
        try:
            return self.transport.send(request)
        except (TimeoutError, socket.timeout, URLError, OSError) as exc:
            raise _RetryableTransportError(f"Telegram request failed: {exc.__class__.__name__}") from exc


def _message_text(event: NotificationEvent, *, parse_mode: str | None = None) -> str:
    episode = event.metadata.get("episode")
    title = _escape_for_parse_mode(event.title.strip() or "Episode organized", parse_mode)
    if isinstance(episode, int):
        return f"{title}\nEpisode {episode} organized"
    message = _escape_for_parse_mode(event.message, parse_mode)
    return f"{title}\n{message}"


def _escape_for_parse_mode(value: str, parse_mode: str | None) -> str:
    if not parse_mode:
        return value
    normalized = parse_mode.casefold()
    if normalized == "markdownv2":
        return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}.!])", r"\\\1", value)
    if normalized == "markdown":
        return re.sub(r"([_\*\[`])", r"\\\1", value)
    if normalized == "html":
        return html.escape(value, quote=False)
    return value


def _failure(
    plan: TelegramDeliveryPlan,
    kind: str,
    message: str,
    *,
    retryable: bool,
    event: NotificationEvent,
    http_status: int | None = None,
) -> TelegramDispatchResult:
    return TelegramDispatchResult(
        success=False,
        status="failed",
        message=message,
        plan=plan,
        retryable=retryable,
        error=TelegramError(kind, message, retryable=retryable, http_status=http_status),
        failure=_failure_record(event, message, recoverable=retryable),
        http_status=http_status,
    )


def _failure_record(event: NotificationEvent, message: str, *, recoverable: bool) -> FailureRecord:
    return FailureRecord(
        subject_id=event.job_id or event.event_type,
        stage="telegram",
        message=message,
        attempts=1,
        last_failed_at=event.created_at,
        recoverable=recoverable,
    )


def _response_message(response: TelegramHttpResponse, default: str, *, plan: TelegramDeliveryPlan) -> str:
    body = response.body.strip()
    if body:
        return f"{default}: {_redact_text(body, plan=plan)}"
    return default


def _redact_text(value: str, *, plan: TelegramDeliveryPlan) -> str:
    redacted = value.replace(plan.url, plan.redacted_url)
    return re.sub(r"bot\d{6,}:[A-Za-z0-9_-]{20,}", "bot<redacted>", redacted)


class _RetryableTransportError(RuntimeError):
    pass
