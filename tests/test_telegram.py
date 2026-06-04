from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from urllib.parse import parse_qs
import json

from hermes_dmhy_anime_subscription.config import TelegramConfig
from hermes_dmhy_anime_subscription.models import NotificationEvent
from hermes_dmhy_anime_subscription.telegram import (
    TelegramHttpRequest,
    TelegramHttpResponse,
    TelegramNotifier,
)


FAKE_TOKEN = "123456:TEST_TOKEN_VALUE_1234567890"


class MockTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def send(self, request: TelegramHttpRequest) -> TelegramHttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def test_send_message_payload_url_body_and_timeout(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    transport = MockTransport([TelegramHttpResponse(status=200, body='{"ok":true}')])
    notifier = TelegramNotifier(
        TelegramConfig(
            enabled=True,
            bot_token_env="TELEGRAM_BOT_TOKEN",
            chat_id="-1001234567890",
            message_thread_id=42,
            parse_mode="MarkdownV2",
            timeout=9.5,
        ),
        transport=transport,
    )

    result = notifier.notify(_event(title="Example_Anime [01]"))

    request = transport.requests[0]
    payload = parse_qs(request.data.decode("utf-8"), keep_blank_values=True)
    assert result.success is True
    assert result.plan.method == "sendMessage"
    assert result.plan.url == "https://api.telegram.org/bot<redacted>/sendMessage"
    assert FAKE_TOKEN not in result.plan.url
    assert FAKE_TOKEN not in json.dumps(asdict(result))
    assert request.url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert request.method == "POST"
    assert request.timeout == 9.5
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert payload == {
        "chat_id": ["-1001234567890"],
        "message_thread_id": ["42"],
        "parse_mode": ["MarkdownV2"],
        "text": ["Example\\_Anime \\[01\\]\nEpisode 1 organized"],
    }


def test_send_photo_payload_url_body_and_default_timeout(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    transport = MockTransport([TelegramHttpResponse(status=200, body='{"ok":true}')])
    notifier = TelegramNotifier(
        TelegramConfig(enabled=True, bot_token_env="TELEGRAM_BOT_TOKEN", chat_id="chat-1"),
        transport=transport,
    )

    result = notifier.notify(_event(), cover_url="https://img.example.invalid/cover.jpg")

    request = transport.requests[0]
    payload = parse_qs(request.data.decode("utf-8"), keep_blank_values=True)
    assert result.success is True
    assert result.plan.method == "sendPhoto"
    assert result.plan.url == "https://api.telegram.org/bot<redacted>/sendPhoto"
    assert FAKE_TOKEN not in result.plan.url
    assert request.url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendPhoto"
    assert request.timeout == 30.0
    assert payload == {
        "caption": ["Example Anime\nEpisode 1 organized"],
        "chat_id": ["chat-1"],
        "parse_mode": ["Markdown"],
        "photo": ["https://img.example.invalid/cover.jpg"],
    }


def test_html_parse_mode_escapes_dynamic_title_and_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    transport = MockTransport([TelegramHttpResponse(status=200, body='{"ok":true}')])
    notifier = TelegramNotifier(
        TelegramConfig(
            enabled=True,
            bot_token_env="TELEGRAM_BOT_TOKEN",
            chat_id="chat-1",
            parse_mode="HTML",
        ),
        transport=transport,
    )

    result = notifier.notify(
        NotificationEvent(
            event_type="organizer_completed",
            title="Example <Anime> & Friends",
            message="Organizer <completed> & copied",
            job_id="job-1",
        )
    )

    payload = parse_qs(transport.requests[0].data.decode("utf-8"), keep_blank_values=True)
    assert result.success is True
    assert payload["text"] == ["Example &lt;Anime&gt; &amp; Friends\nOrganizer &lt;completed&gt; &amp; copied"]


def test_telegram_error_messages_redact_full_bot_url_and_raw_bot_token_fragment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    full_url = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    body = f"failed at {full_url} after raw bot{FAKE_TOKEN} leaked"
    transport = MockTransport([TelegramHttpResponse(status=500, body=body)])
    notifier = TelegramNotifier(
        TelegramConfig(enabled=True, bot_token_env="TELEGRAM_BOT_TOKEN", chat_id="chat-1"),
        transport=transport,
    )

    result = notifier.notify(_event())

    assert result.success is False
    assert FAKE_TOKEN not in result.message
    assert full_url not in result.message
    assert "bot123456:" not in result.message
    assert "https://api.telegram.org/bot<redacted>/sendMessage" in result.message
    assert "bot<redacted>" in result.message
    assert result.error is not None
    assert FAKE_TOKEN not in result.error.message


def _event(*, title: str = "Example Anime") -> NotificationEvent:
    return NotificationEvent(
        event_type="organizer_completed",
        title=title,
        message="Organizer completed episode",
        job_id="job-1",
        metadata={"episode": 1},
        created_at=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
    )
