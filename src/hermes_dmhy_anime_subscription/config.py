"""Configuration loading and validation for the DMHY plugin."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import OrganizerMode, RuleEpisodeMode, SubscriptionRule

MIN_POLLING_INTERVAL_MINUTES = 10


class ConfigError(ValueError):
    """Raised when a configuration file is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class DmhyFeedConfig:
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class DmhyConfig:
    feeds: tuple[DmhyFeedConfig, ...]


@dataclass(frozen=True, slots=True)
class SubscriptionsConfig:
    rules: tuple[SubscriptionRule, ...]


@dataclass(frozen=True, slots=True)
class QbittorrentConfig:
    endpoint: str
    username_env: str | None = None
    password_env: str | None = None
    category: str | None = None
    tags: tuple[str, ...] = ()
    save_path: str | None = None


@dataclass(frozen=True, slots=True)
class PollingConfig:
    interval_minutes: int
    jitter_seconds: int = 0


@dataclass(frozen=True, slots=True)
class StateConfig:
    path: Path


@dataclass(frozen=True, slots=True)
class OrganizerConfig:
    mode: OrganizerMode
    library_root: Path
    staging_root: Path


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    enabled: bool = False
    url_env: str | None = None


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_attempts: int
    backoff_seconds: int


@dataclass(frozen=True, slots=True)
class PluginConfig:
    dmhy: DmhyConfig
    subscriptions: SubscriptionsConfig
    qbittorrent: QbittorrentConfig
    polling: PollingConfig
    state: StateConfig
    organizer: OrganizerConfig
    webhook: WebhookConfig
    retry: RetryConfig


def load_config(path: str | os.PathLike[str]) -> PluginConfig:
    """Load and validate a JSON configuration file."""

    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {config_path}: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Configuration root must be a JSON object")
    return parse_config(raw, base_dir=config_path.parent)


def parse_config(raw: dict[str, Any], base_dir: Path | None = None) -> PluginConfig:
    base = base_dir or Path.cwd()
    dmhy_raw = _required_mapping(raw, "dmhy")
    subscriptions_raw = _required_mapping(raw, "subscriptions")
    qb_raw = _required_mapping(raw, "qbittorrent")
    polling_raw = _required_mapping(raw, "polling")
    state_raw = _required_mapping(raw, "state")
    organizer_raw = _required_mapping(raw, "organizer")
    webhook_raw = _optional_mapping(raw, "webhook")
    retry_raw = _required_mapping(raw, "retry")

    return PluginConfig(
        dmhy=_parse_dmhy(dmhy_raw),
        subscriptions=_parse_subscriptions(subscriptions_raw),
        qbittorrent=_parse_qbittorrent(qb_raw),
        polling=_parse_polling(polling_raw),
        state=StateConfig(path=_path_value(state_raw, "path", base)),
        organizer=_parse_organizer(organizer_raw, base),
        webhook=_parse_webhook(webhook_raw),
        retry=_parse_retry(retry_raw),
    )


def _parse_dmhy(raw: dict[str, Any]) -> DmhyConfig:
    feeds_raw = raw.get("feeds")
    if not isinstance(feeds_raw, list) or not feeds_raw:
        raise ConfigError("dmhy.feeds must be a non-empty list")
    feeds: list[DmhyFeedConfig] = []
    for index, feed_raw in enumerate(feeds_raw):
        if not isinstance(feed_raw, dict):
            raise ConfigError(f"dmhy.feeds[{index}] must be an object")
        name = _string_value(feed_raw, "name", f"dmhy.feeds[{index}].name")
        url = _string_value(feed_raw, "url", f"dmhy.feeds[{index}].url")
        feeds.append(DmhyFeedConfig(name=name, url=url))
    return DmhyConfig(feeds=tuple(feeds))


def _parse_subscriptions(raw: dict[str, Any]) -> SubscriptionsConfig:
    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ConfigError("subscriptions.rules must be a non-empty list")
    rules: list[SubscriptionRule] = []
    for index, rule_raw in enumerate(rules_raw):
        if not isinstance(rule_raw, dict):
            raise ConfigError(f"subscriptions.rules[{index}] must be an object")
        name = _string_value(rule_raw, "name", f"subscriptions.rules[{index}].name")
        rules.append(
            SubscriptionRule(
                name=name,
                include_keywords=_string_tuple(rule_raw.get("include_keywords", ()), f"subscriptions.rules[{index}].include_keywords"),
                exclude_keywords=_string_tuple(rule_raw.get("exclude_keywords", ()), f"subscriptions.rules[{index}].exclude_keywords"),
                use_regex=_bool_value(rule_raw.get("use_regex", False), f"subscriptions.rules[{index}].use_regex"),
                team_names=_string_tuple(rule_raw.get("team_names", ()), f"subscriptions.rules[{index}].team_names"),
                resolutions=_string_tuple(rule_raw.get("resolutions", ()), f"subscriptions.rules[{index}].resolutions"),
                categories=_string_tuple(rule_raw.get("categories", ()), f"subscriptions.rules[{index}].categories"),
                episode_mode=RuleEpisodeMode(_string_value(rule_raw, "episode_mode", f"subscriptions.rules[{index}].episode_mode") if "episode_mode" in rule_raw else "episode"),
                allow_packs=_bool_value(rule_raw.get("allow_packs", False), f"subscriptions.rules[{index}].allow_packs"),
                feed_names=_string_tuple(rule_raw.get("feed_names", ()), f"subscriptions.rules[{index}].feed_names"),
                save_path=_optional_string(rule_raw.get("save_path"), f"subscriptions.rules[{index}].save_path"),
                category=_optional_string(rule_raw.get("category"), f"subscriptions.rules[{index}].category"),
                bangumi_subject_id=_optional_int_value(rule_raw.get("bangumi_subject_id"), f"subscriptions.rules[{index}].bangumi_subject_id"),
                priority=_int_value(rule_raw.get("priority", 0), f"subscriptions.rules[{index}].priority"),
                enabled=_bool_value(rule_raw.get("enabled", True), f"subscriptions.rules[{index}].enabled"),
            )
        )
    return SubscriptionsConfig(rules=tuple(rules))


def _parse_qbittorrent(raw: dict[str, Any]) -> QbittorrentConfig:
    endpoint = _string_value(raw, "endpoint", "qbittorrent.endpoint")
    return QbittorrentConfig(
        endpoint=endpoint,
        username_env=_optional_env_name(raw.get("username_env"), "qbittorrent.username_env"),
        password_env=_optional_env_name(raw.get("password_env"), "qbittorrent.password_env"),
        category=_optional_string(raw.get("category"), "qbittorrent.category"),
        tags=_string_tuple(raw.get("tags", ()), "qbittorrent.tags"),
        save_path=_optional_string(raw.get("save_path"), "qbittorrent.save_path"),
    )


def _parse_polling(raw: dict[str, Any]) -> PollingConfig:
    interval = _int_value(raw.get("interval_minutes"), "polling.interval_minutes")
    if interval < MIN_POLLING_INTERVAL_MINUTES:
        raise ConfigError("polling.interval_minutes must be at least 10")
    jitter = _int_value(raw.get("jitter_seconds"), "polling.jitter_seconds")
    if jitter < 0:
        raise ConfigError("polling.jitter_seconds must be non-negative")
    return PollingConfig(interval_minutes=interval, jitter_seconds=jitter)


def _parse_organizer(raw: dict[str, Any], base: Path) -> OrganizerConfig:
    mode = OrganizerMode(_string_value(raw, "mode", "organizer.mode"))
    library_value = raw.get("library_root")
    if mode in {OrganizerMode.APPLY, OrganizerMode.MOVE} and not _has_text(library_value):
        raise ConfigError("organizer.library_root is required when organizer.mode is apply or move")
    if not _has_text(library_value):
        library_root = Path(tempfile.gettempdir()) / "hermes-dmhy-library-dry-run"
    else:
        library_root = _resolve_path(str(library_value), base)
    return OrganizerConfig(
        mode=mode,
        library_root=library_root,
        staging_root=_path_value(raw, "staging_root", base),
    )


def _parse_webhook(raw: dict[str, Any]) -> WebhookConfig:
    enabled = _bool_value(raw.get("enabled", False), "webhook.enabled")
    url_env = _optional_env_name(raw.get("url_env"), "webhook.url_env")
    if enabled and not url_env:
        raise ConfigError("webhook.url_env is required when webhook.enabled is true")
    return WebhookConfig(enabled=enabled, url_env=url_env)


def _parse_retry(raw: dict[str, Any]) -> RetryConfig:
    max_attempts = _int_value(raw.get("max_attempts"), "retry.max_attempts")
    backoff_seconds = _int_value(raw.get("backoff_seconds"), "retry.backoff_seconds")
    if max_attempts < 1:
        raise ConfigError("retry.max_attempts must be at least 1")
    if backoff_seconds < 0:
        raise ConfigError("retry.backoff_seconds must be non-negative")
    return RetryConfig(max_attempts=max_attempts, backoff_seconds=backoff_seconds)


def _required_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object")
    return value


def _optional_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object")
    return value


def _string_value(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not _has_text(value):
        raise ConfigError(f"{label} is required")
    return str(value)


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not _has_text(value):
        raise ConfigError(f"{label} must be a non-empty string when provided")
    return str(value)


def _optional_env_name(value: Any, label: str) -> str | None:
    env_name = _optional_string(value, label)
    if env_name is None:
        return None
    if "://" in env_name or "/" in env_name or "?" in env_name:
        raise ConfigError(f"{label} must be an environment variable name, not a URL or secret value")
    if not env_name.replace("_", "A").isalnum() or not (env_name[0].isalpha() or env_name[0] == "_"):
        raise ConfigError(f"{label} must be a valid environment variable name")
    return env_name


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ConfigError(f"{label} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not _has_text(item):
            raise ConfigError(f"{label}[{index}] must be a non-empty string")
        result.append(str(item))
    return tuple(result)


def _optional_int_value(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _int_value(value, label)


def _int_value(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer")
    return value


def _bool_value(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean")
    return value


def _path_value(raw: dict[str, Any], key: str, base: Path) -> Path:
    return _resolve_path(_string_value(raw, key, f"{key}"), base)


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
