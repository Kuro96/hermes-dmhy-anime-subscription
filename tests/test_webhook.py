from __future__ import annotations

import json
from datetime import datetime, timezone

from hermes_dmhy_anime_subscription.config import WebhookConfig
from hermes_dmhy_anime_subscription.models import NotificationEvent
from hermes_dmhy_anime_subscription.webhook import (
    WebhookDispatchResult,
    WebhookHttpRequest,
    WebhookHttpResponse,
    WebhookNotifier,
)


class MockTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def send(self, request: WebhookHttpRequest) -> WebhookHttpResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def test_disabled_webhook_makes_no_http_calls():
    transport = MockTransport([WebhookHttpResponse(status=200, body="ok")])
    notifier = WebhookNotifier(WebhookConfig(enabled=False, url_env="DMHY_WEBHOOK_URL"), transport=transport)

    result = notifier.notify(_event(), dry_run=True)

    assert result.disabled is True
    assert result.success is True
    assert result.status == "disabled"
    assert result.plan.redacted_url == "<disabled>"
    assert transport.requests == []


def test_webhook_delivery_payload_includes_required_fields(monkeypatch):
    monkeypatch.setenv("DMHY_WEBHOOK_URL", "https://hooks.example.invalid/webhook/path")
    transport = MockTransport([WebhookHttpResponse(status=200, body="ok")])
    notifier = WebhookNotifier(WebhookConfig(enabled=True, url_env="DMHY_WEBHOOK_URL"), transport=transport)

    result = notifier.notify(
        _event(),
        rule_id="rule-1",
        rule_name="Example Show",
        release_title="Example Show Episode 01",
        guid="guid-1",
        infohash="ABC123",
        qbittorrent_job_id="job-1",
        qbittorrent_hash="hash-1",
        status="submitted",
        failure_reason=None,
        dry_run=False,
    )

    payload = json.loads(transport.requests[0].data.decode("utf-8"))
    assert result.success is True
    assert result.status == "sent"
    assert transport.requests[0].headers["Content-Type"] == "application/json"
    assert payload == {
        "dry_run": False,
        "event_type": "download_completed",
        "failure_reason": None,
        "message": "Completed",
        "qbittorrent": {"hash": "hash-1", "job_id": "job-1"},
        "release": {"guid": "guid-1", "infohash": "ABC123", "title": "Example Show Episode 01"},
        "severity": "info",
        "status": "submitted",
        "subscription": {"rule_id": "rule-1", "rule_name": "Example Show"},
        "timestamp": "2026-05-24T12:00:00+00:00",
        "title": "Completed",
    }


def test_webhook_500_is_retryable_and_does_not_crash_pipeline(monkeypatch):
    monkeypatch.setenv("DMHY_WEBHOOK_URL", "https://hooks.example.invalid/webhook/path")
    transport = MockTransport([WebhookHttpResponse(status=500, body="server exploded")])
    notifier = WebhookNotifier(WebhookConfig(enabled=True, url_env="DMHY_WEBHOOK_URL"), transport=transport)

    result = notifier.notify(_event())

    assert isinstance(result, WebhookDispatchResult)
    assert result.success is False
    assert result.retryable is True
    error = result.error
    assert error is not None
    assert error.kind == "api"
    assert result.failure is not None
    assert result.failure.recoverable is True
    assert result.message.endswith("server exploded")
    assert len(transport.requests) == 1


def test_webhook_redacts_url_in_results_and_errors(monkeypatch):
    monkeypatch.setenv("DMHY_WEBHOOK_URL", "https://hooks.example.invalid/secret/token/value")
    transport = MockTransport([WebhookHttpResponse(status=500, body="fail")])
    notifier = WebhookNotifier(WebhookConfig(enabled=True, url_env="DMHY_WEBHOOK_URL"), transport=transport)

    result = notifier.notify(_event())
    error = result.error

    assert "secret/token/value" not in result.message
    assert error is not None
    assert "secret/token/value" not in error.message
    assert result.plan.redacted_url == "https://hooks.example.invalid/<redacted>"
    assert result.message.startswith("Webhook delivery failed for https://hooks.example.invalid/<redacted>")


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_type="download_completed",
        title="Completed",
        message="Completed",
        job_id="job-1",
        metadata={
            "rule_id": "rule-1",
            "rule_name": "Example Show",
            "release_title": "Example Show Episode 01",
            "guid": "guid-1",
            "infohash": "ABC123",
            "torrent_hash": "hash-1",
            "status": "completed",
            "failure_reason": None,
        },
        created_at=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
    )
