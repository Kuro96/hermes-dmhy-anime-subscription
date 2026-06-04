from pathlib import Path

import pytest
import json

from hermes_dmhy_anime_subscription.config import ConfigError, load_config
from hermes_dmhy_anime_subscription.models import OrganizerMode, RuleEpisodeMode


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def test_valid_example_config_loads_with_safe_defaults():
    config = load_config(FIXTURE_DIR / "valid.example.json")

    assert config.dmhy.feeds[0].name == "dmhy-main"
    assert config.subscriptions.rules[0].name == "example-show"
    assert config.subscriptions.rules[0].use_regex is False
    assert config.subscriptions.rules[0].team_names == ("ExampleSub",)
    assert config.subscriptions.rules[0].resolutions == ("1080p",)
    assert config.subscriptions.rules[0].categories == ("動畫",)
    assert config.subscriptions.rules[0].episode_mode is RuleEpisodeMode.EPISODE
    assert config.subscriptions.rules[0].bangumi_subject_id is None
    assert config.subscriptions.rules[0].allow_packs is False
    assert config.qbittorrent.endpoint == "http://127.0.0.1:8080"
    assert config.qbittorrent.category == "anime"
    assert config.qbittorrent.tags == ("dmhy", "subscription")
    assert config.qbittorrent.save_path == "var/qbittorrent-downloads"
    assert config.polling.interval_minutes == 15
    assert config.organizer.mode is OrganizerMode.DRY_RUN
    assert config.webhook.enabled is False
    assert config.webhook.url_env == "DMHY_WEBHOOK_URL"
    assert config.telegram.enabled is False
    assert config.telegram.bot_token_env is None
    assert config.telegram.chat_id is None
    assert config.telegram.message_thread_id is None
    assert config.telegram.parse_mode == "Markdown"
    assert config.telegram.timeout is None
    assert str(config.state.path).endswith("var/state/dmhy-subscription.sqlite3")


def test_telegram_enabled_settings_parse(tmp_path):
    raw = json.loads((FIXTURE_DIR / "valid.example.json").read_text(encoding="utf-8"))
    raw["telegram"] = {
        "enabled": True,
        "bot_token_env": "TELEGRAM_BOT_TOKEN",
        "chat_id": "-1001234567890",
        "message_thread_id": 42,
        "parse_mode": "HTML",
        "timeout": 12.5,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    config = load_config(path)

    assert config.telegram.enabled is True
    assert config.telegram.bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert config.telegram.chat_id == "-1001234567890"
    assert config.telegram.message_thread_id == 42
    assert config.telegram.parse_mode == "HTML"
    assert config.telegram.timeout == 12.5


@pytest.mark.parametrize("parse_mode", ["Markdown", "MarkdownV2", "HTML"])
def test_telegram_parse_mode_accepts_supported_modes(tmp_path, parse_mode):
    raw = json.loads((FIXTURE_DIR / "valid.example.json").read_text(encoding="utf-8"))
    raw["telegram"] = {
        "enabled": True,
        "bot_token_env": "TELEGRAM_BOT_TOKEN",
        "chat_id": "-1001234567890",
        "parse_mode": parse_mode,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    config = load_config(path)

    assert config.telegram.parse_mode == parse_mode


def test_telegram_parse_mode_rejects_unknown_mode(tmp_path):
    raw = json.loads((FIXTURE_DIR / "valid.example.json").read_text(encoding="utf-8"))
    raw["telegram"] = {
        "enabled": True,
        "bot_token_env": "TELEGRAM_BOT_TOKEN",
        "chat_id": "-1001234567890",
        "parse_mode": "PlainText",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="telegram.parse_mode"):
        load_config(path)


@pytest.mark.parametrize(
    ("telegram_config", "message"),
    [
        ({"enabled": True, "chat_id": "-1001234567890"}, "telegram.bot_token_env"),
        ({"enabled": True, "bot_token_env": "TELEGRAM_BOT_TOKEN"}, "telegram.chat_id"),
    ],
)
def test_telegram_enabled_requires_token_env_and_chat_id(tmp_path, telegram_config, message):
    raw = json.loads((FIXTURE_DIR / "valid.example.json").read_text(encoding="utf-8"))
    raw["telegram"] = telegram_config
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_telegram_bot_token_env_rejects_literal_bot_token(tmp_path):
    raw = json.loads((FIXTURE_DIR / "valid.example.json").read_text(encoding="utf-8"))
    raw["telegram"] = {
        "enabled": True,
        "bot_token_env": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "chat_id": "-1001234567890",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="literal Telegram bot token"):
        load_config(path)


def test_subscription_rule_accepts_optional_bangumi_subject_id(tmp_path):
    raw = json.loads((FIXTURE_DIR / "valid.example.json").read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["bangumi_subject_id"] = 12345
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    config = load_config(path)

    assert config.subscriptions.rules[0].bangumi_subject_id == 12345


@pytest.mark.parametrize(
    ("fixture_name", "message"),
    [
        ("invalid-missing-qbittorrent-endpoint.json", "qbittorrent.endpoint"),
        ("invalid-unsafe-polling.json", "at least 10"),
        ("invalid-missing-library-root-apply.json", "organizer.library_root"),
        ("invalid-hardcoded-webhook-url.json", "environment variable name"),
    ],
)
def test_invalid_config_fixtures_are_rejected(fixture_name, message):
    with pytest.raises(ConfigError, match=message):
        load_config(FIXTURE_DIR / fixture_name)
