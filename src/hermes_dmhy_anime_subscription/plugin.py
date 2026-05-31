"""Hermes plugin registration for DMHY anime subscription workflows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from functools import wraps
from pathlib import Path

from .monitor import OrganizerInput, TorrentSnapshot
from .workflow import (
    list_state,
    monitor_once,
    organize_once,
    retry_failed_item,
    run_once,
    scheduler_tick,
    validate_config,
)

TOOL_NAMES = (
    "dmhy.validate_config",
    "dmhy.run_once_dry_run",
    "dmhy.run_once_apply",
    "dmhy.monitor_once",
    "dmhy.organize_once",
    "dmhy.list_state",
    "dmhy.list_failures",
    "dmhy.retry_failed_item",
)

PLUGIN_METADATA = {
    "name": "dmhy-anime-subscription",
    "version": "0.1.0",
    "description": "Hermes plugin for safe DMHY anime subscription workflow orchestration.",
    "provides_tools": list(TOOL_NAMES),
    "provides_hooks": ["dmhy.schedule_tick"],
}


def register(ctx):
    _register_tool(ctx, "dmhy.validate_config", validate_config)
    _register_tool(
        ctx,
        "dmhy.run_once_dry_run",
        lambda config_path, **kwargs: run_once(config_path, dry_run=True, **kwargs),
    )
    _register_tool(
        ctx,
        "dmhy.run_once_apply",
        lambda config_path, **kwargs: run_once(config_path, dry_run=False, **kwargs),
    )
    _register_tool(ctx, "dmhy.monitor_once", monitor_once)
    _register_tool(ctx, "dmhy.organize_once", organize_once)
    _register_tool(ctx, "dmhy.list_state", _list_state)
    _register_tool(ctx, "dmhy.list_failures", _list_failures)
    _register_tool(ctx, "dmhy.retry_failed_item", retry_failed_item)
    _register_hook(ctx, "dmhy.schedule_tick", scheduler_tick)
    from .cli import main

    _register_cli(ctx, "hermes-dmhy", main)


def _register_tool(ctx, name, handler) -> None:
    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        register_tool(name, _json_tool(name, handler))


def _json_tool(name, handler):
    @wraps(handler)
    def wrapper(*args, **kwargs):
        normalized_args, normalized_kwargs = _normalize_tool_inputs(name, args, kwargs)
        return _jsonable(handler(*normalized_args, **normalized_kwargs))

    return wrapper


def _normalize_tool_inputs(name, args, kwargs):
    if name == "dmhy.monitor_once":
        return _normalize_monitor_once_args(args, kwargs)
    if name == "dmhy.organize_once":
        return _normalize_organize_once_args(args, kwargs)
    return args, kwargs


def _normalize_monitor_once_args(args, kwargs):
    normalized_kwargs = dict(kwargs)
    normalized_args = list(args)
    if "snapshots" in normalized_kwargs:
        normalized_kwargs["snapshots"] = _torrent_snapshots(normalized_kwargs["snapshots"])
    elif len(normalized_args) > 1:
        normalized_args[1] = _torrent_snapshots(normalized_args[1])
    return tuple(normalized_args), normalized_kwargs


def _normalize_organize_once_args(args, kwargs):
    normalized_kwargs = dict(kwargs)
    normalized_args = list(args)
    if "organizer_input" in normalized_kwargs:
        normalized_kwargs["organizer_input"] = _organizer_input(normalized_kwargs["organizer_input"])
    elif len(normalized_args) > 1:
        normalized_args[1] = _organizer_input(normalized_args[1])
    return tuple(normalized_args), normalized_kwargs


def _torrent_snapshots(value):
    if value is None:
        return ()
    return tuple(_torrent_snapshot(item) for item in value)


def _torrent_snapshot(value):
    if isinstance(value, TorrentSnapshot):
        return value
    if isinstance(value, Mapping):
        payload = dict(value)
        payload["completed_at"] = _datetime_or_none(payload.get("completed_at"))
        return TorrentSnapshot(**payload)
    return value


def _organizer_input(value):
    if isinstance(value, OrganizerInput):
        return value
    if isinstance(value, Mapping):
        payload = dict(value)
        payload["completed_at"] = _datetime_or_none(payload.get("completed_at"))
        return OrganizerInput(**payload)
    return value


def _datetime_or_none(value):
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _jsonable(value):
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _list_failures(config_path):
    summary = list_state(config_path)
    return {
        "failed": summary.failed,
        "retryable": summary.retryable,
        "all_failures": summary.all_failures,
    }


def _list_state(config_path):
    summary = list_state(config_path)
    return {
        "processed": summary.processed,
        "pending": summary.pending,
        "failed": summary.failed,
        "retryable": summary.retryable,
        "all_failures": summary.all_failures,
        "archived_rules": summary.archived_rules,
    }


def _register_hook(ctx, name, handler) -> None:
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook(name, handler)


def _register_cli(ctx, name, handler) -> None:
    register_cli_command = getattr(ctx, "register_cli_command", None)
    if callable(register_cli_command):
        register_cli_command(name, handler)
        return
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command(name, handler)
