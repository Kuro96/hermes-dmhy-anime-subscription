"""Webhook notification delivery for workflow events."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener

from .config import WebhookConfig
from .models import FailureRecord, NotificationEvent

DEFAULT_TIMEOUT_SECONDS = 30.0
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class WebhookHttpRequest:
    url: str
    data: bytes
    headers: dict[str, str]
    timeout: float
    method: str = "POST"


@dataclass(frozen=True, slots=True)
class WebhookHttpResponse:
    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class WebhookTransport(Protocol):
    def send(self, request: WebhookHttpRequest) -> WebhookHttpResponse:
        """Send a webhook HTTP request."""


@dataclass(frozen=True, slots=True)
class WebhookDeliveryPlan:
    url: str
    redacted_url: str
    payload: dict[str, Any]
    dry_run: bool = False

    def body(self) -> bytes:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class WebhookError:
    kind: str
    message: str
    retryable: bool
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class WebhookDispatchResult:
    success: bool
    status: str
    message: str
    plan: WebhookDeliveryPlan
    retryable: bool = False
    disabled: bool = False
    error: WebhookError | None = None
    failure: FailureRecord | None = None
    http_status: int | None = None


class UrllibWebhookTransport:
    """Stdlib transport for webhook delivery."""

    def __init__(self) -> None:
        self._opener = build_opener()

    def send(self, request: WebhookHttpRequest) -> WebhookHttpResponse:
        urllib_request = Request(
            request.url,
            data=request.data,
            headers=request.headers,
            method=request.method,
        )
        try:
            with self._opener.open(urllib_request, timeout=request.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return WebhookHttpResponse(status=response.status, body=body, headers=dict(response.headers.items()))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return WebhookHttpResponse(status=exc.code, body=body, headers=dict(exc.headers.items()))


class WebhookNotifier:
    def __init__(
        self,
        config: WebhookConfig,
        *,
        transport: WebhookTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibWebhookTransport()
        self.timeout = timeout
        self.webhook_url = os.environ.get(config.url_env) if config.enabled and config.url_env else None

    def notify(
        self,
        event: NotificationEvent,
        *,
        rule_id: str | None = None,
        rule_name: str | None = None,
        release_title: str | None = None,
        guid: str | None = None,
        infohash: str | None = None,
        qbittorrent_job_id: str | None = None,
        qbittorrent_hash: str | None = None,
        status: str | None = None,
        failure_reason: str | None = None,
        dry_run: bool = False,
    ) -> WebhookDispatchResult:
        if not self.config.enabled:
            plan = WebhookDeliveryPlan(url="", redacted_url="<disabled>", payload=build_webhook_payload(event, dry_run=dry_run), dry_run=dry_run)
            return WebhookDispatchResult(
                success=True,
                status="disabled",
                message="Webhook notifications are disabled",
                plan=plan,
                disabled=True,
            )

        if not self.webhook_url:
            plan = WebhookDeliveryPlan(url="", redacted_url="<redacted>", payload=build_webhook_payload(event, dry_run=dry_run), dry_run=dry_run)
            message = f"Webhook URL environment variable {self.config.url_env} is not set"
            error = WebhookError(kind="configuration", message=message, retryable=False)
            failure = _failure_record(event, message, recoverable=False)
            return WebhookDispatchResult(
                success=False,
                status="failed",
                message=message,
                plan=plan,
                error=error,
                failure=failure,
            )

        payload = build_webhook_payload(
            event,
            rule_id=rule_id,
            rule_name=rule_name,
            release_title=release_title,
            guid=guid,
            infohash=infohash,
            qbittorrent_job_id=qbittorrent_job_id,
            qbittorrent_hash=qbittorrent_hash,
            status=status,
            failure_reason=failure_reason,
            dry_run=dry_run,
        )
        plan = WebhookDeliveryPlan(url=self.webhook_url, redacted_url=_redact_url(self.webhook_url), payload=payload, dry_run=dry_run)
        try:
            response = self._post_json(plan)
        except _RetryableTransportError as exc:
            return _failure(plan, "transport", str(exc), retryable=True, event=event)

        if 200 <= response.status < 300:
            return WebhookDispatchResult(
                success=True,
                status="sent",
                message=f"Webhook delivered to {plan.redacted_url}",
                plan=plan,
                http_status=response.status,
            )

        retryable = response.status in RETRYABLE_STATUS_CODES
        message = _response_message(response, f"Webhook delivery failed for {plan.redacted_url}")
        return _failure(plan, "api", message, retryable=retryable, http_status=response.status, event=event)

    def _post_json(self, plan: WebhookDeliveryPlan) -> WebhookHttpResponse:
        request = WebhookHttpRequest(
            url=plan.url,
            data=plan.body(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=self.timeout,
        )
        try:
            return self.transport.send(request)
        except (TimeoutError, socket.timeout, URLError, OSError) as exc:
            raise _RetryableTransportError(f"Webhook request failed: {exc.__class__.__name__}") from exc


def build_webhook_payload(
    event: NotificationEvent,
    *,
    rule_id: str | None = None,
    rule_name: str | None = None,
    release_title: str | None = None,
    guid: str | None = None,
    infohash: str | None = None,
    qbittorrent_job_id: str | None = None,
    qbittorrent_hash: str | None = None,
    status: str | None = None,
    failure_reason: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    metadata = dict(event.metadata)
    resolved_rule_id = rule_id or _text(metadata.get("rule_id"))
    resolved_rule_name = rule_name or _text(metadata.get("rule_name"))
    resolved_release_title = release_title or _text(metadata.get("release_title")) or event.title
    resolved_guid = guid or _text(metadata.get("guid"))
    resolved_infohash = infohash or _text(metadata.get("infohash")) or _text(metadata.get("torrent_hash"))
    resolved_job_id = qbittorrent_job_id or event.job_id or _text(metadata.get("qbittorrent_job_id"))
    resolved_hash = qbittorrent_hash or _text(metadata.get("qbittorrent_hash")) or _text(metadata.get("torrent_hash"))
    resolved_status = status or _text(metadata.get("status")) or event.severity
    resolved_failure_reason = failure_reason or _text(metadata.get("failure_reason"))
    if resolved_failure_reason is None and event.severity in {"warning", "error"}:
        resolved_failure_reason = event.message
    return {
        "event_type": event.event_type,
        "subscription": {
            "rule_id": resolved_rule_id,
            "rule_name": resolved_rule_name,
        },
        "release": {
            "title": resolved_release_title,
            "guid": resolved_guid,
            "infohash": resolved_infohash,
        },
        "qbittorrent": {
            "job_id": resolved_job_id,
            "hash": resolved_hash,
        },
        "status": resolved_status,
        "failure_reason": resolved_failure_reason,
        "timestamp": event.created_at.isoformat(),
        "dry_run": dry_run,
        "severity": event.severity,
        "title": event.title,
        "message": event.message,
    }


def _failure(
    plan: WebhookDeliveryPlan,
    kind: str,
    message: str,
    *,
    retryable: bool,
    event: NotificationEvent,
    http_status: int | None = None,
) -> WebhookDispatchResult:
    failure = _failure_record(event, message, recoverable=retryable)
    return WebhookDispatchResult(
        success=False,
        status="failed",
        message=message,
        plan=plan,
        retryable=retryable,
        error=WebhookError(kind=kind, message=message, retryable=retryable, http_status=http_status),
        failure=failure,
        http_status=http_status,
    )


def _failure_record(event: NotificationEvent, message: str, *, recoverable: bool) -> FailureRecord:
    return FailureRecord(
        subject_id=event.job_id or event.event_type,
        stage="webhook",
        message=message,
        attempts=1,
        last_failed_at=event.created_at,
        recoverable=recoverable,
    )


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme and not parsed.netloc:
        return "<redacted>"
    return urlunsplit((parsed.scheme, parsed.netloc, "<redacted>", "", ""))


def _response_message(response: WebhookHttpResponse, default: str) -> str:
    body = response.body.strip()
    if body:
        return f"{default}: {body}"
    return default


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class _RetryableTransportError(RuntimeError):
    pass
