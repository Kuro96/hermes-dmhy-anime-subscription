"""Hermes plugin registration for DMHY anime subscription workflows."""

from __future__ import annotations

from .workflow import list_state, monitor_once, organize_once, retry_failed_item, run_once, scheduler_tick, validate_config

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
    _register_tool(ctx, "dmhy.run_once_dry_run", lambda config_path, **kwargs: run_once(config_path, dry_run=True, **kwargs))
    _register_tool(ctx, "dmhy.run_once_apply", lambda config_path, **kwargs: run_once(config_path, dry_run=False, **kwargs))
    _register_tool(ctx, "dmhy.monitor_once", monitor_once)
    _register_tool(ctx, "dmhy.organize_once", organize_once)
    _register_tool(ctx, "dmhy.list_state", list_state)
    _register_tool(ctx, "dmhy.list_failures", lambda config_path: {"failed": list_state(config_path).failed, "retryable": list_state(config_path).retryable})
    _register_tool(ctx, "dmhy.retry_failed_item", retry_failed_item)
    _register_hook(ctx, "dmhy.schedule_tick", scheduler_tick)
    from .cli import main

    _register_cli(ctx, "hermes-dmhy", main)


def _register_tool(ctx, name, handler) -> None:
    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        register_tool(name, handler)


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
