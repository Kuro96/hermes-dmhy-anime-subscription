"""Command-line entrypoints for the DMHY subscription workflow."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .monitor import OrganizerInput, TorrentSnapshot
from .workflow import (
    WorkflowDependencies,
    audit_ingestion,
    list_state,
    monitor_once,
    organize_once,
    plan_completed_dry_run,
    production_tick,
    retry_failed_item,
    run_once,
    scheduler_tick,
    scheduling_guidance,
    snapshots_from_json,
    validate_config,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # CLI boundary: convert to deterministic non-zero output.
        print(f"error: {exc}")
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-dmhy", description="Hermes DMHY anime subscription workflow"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate-config")
    validate.add_argument("--config", required=True)
    validate.set_defaults(func=_validate_config)

    run = subcommands.add_parser("run-once")
    run.add_argument("--config", required=True)
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    run.add_argument("--feed-file")
    run.add_argument(
        "--completed-source-path",
        help="dry-run fixture path to treat planned downloads as completed and plan organizer actions",
    )
    run.set_defaults(func=_run_once)

    monitor = subcommands.add_parser("monitor-once")
    monitor.add_argument("--config", required=True)
    monitor.add_argument("--snapshot-json")
    monitor_mode = monitor.add_mutually_exclusive_group()
    monitor_mode.add_argument("--dry-run", action="store_true", default=True)
    monitor_mode.add_argument("--apply", action="store_true")
    monitor.set_defaults(func=_monitor_once)

    organize = subcommands.add_parser("organize-once")
    organize.add_argument("--config", required=True)
    organize.add_argument("--job-id", required=True)
    organize.add_argument("--torrent-hash", required=True)
    organize.add_argument("--title", required=True)
    organize.add_argument("--source-path", required=True)
    organize.set_defaults(func=_organize_once)

    state = subcommands.add_parser("state")
    state.add_argument("--config", required=True)
    state.set_defaults(func=_state)

    failures = subcommands.add_parser("failures")
    failures.add_argument("--config", required=True)
    failures.set_defaults(func=_failures)

    audit = subcommands.add_parser("audit-ingestion")
    audit.add_argument("--config", required=True)
    audit.set_defaults(func=_audit_ingestion)

    retry = subcommands.add_parser("retry-failed")
    retry.add_argument("--config", required=True)
    retry.add_argument("--job-id", required=True)
    retry.set_defaults(func=_retry_failed)

    schedule = subcommands.add_parser("schedule-tick")
    schedule.add_argument("--config", required=True)
    schedule.add_argument("--feed-file")
    schedule_mode = schedule.add_mutually_exclusive_group()
    schedule_mode.add_argument("--dry-run", action="store_true", default=True)
    schedule_mode.add_argument("--apply", action="store_true")
    schedule.set_defaults(func=_schedule_tick)
    return parser


def _validate_config(args: argparse.Namespace) -> int:
    config = validate_config(args.config)
    print(f"valid config: {args.config}")
    print(scheduling_guidance(config))
    return 0


def _run_once(args: argparse.Namespace) -> int:
    dry_run = not args.apply
    result = run_once(
        args.config,
        dry_run=dry_run,
        dependencies=_feed_file_dependencies(args.feed_file),
    )
    print(
        f"run once: dry_run={result.dry_run} parsed={result.parsed_items} candidates={len(result.candidates)} planned={result.planned_submissions}"
    )
    _print_run_once_details(result)
    if args.completed_source_path:
        if not dry_run:
            raise ValueError(
                "--completed-source-path is only supported for dry-run planning"
            )
        monitor_result = plan_completed_dry_run(
            args.config, result, args.completed_source_path
        )
        _print_monitor_details(monitor_result)
    return 0


def _print_run_once_details(result) -> None:
    for outcome in result.candidates:
        if outcome.submit_result is not None:
            plan = outcome.submit_result.plan
            print(
                "planned qBittorrent submit: "
                f"job_id={outcome.job_id} status={outcome.submit_result.status} title={plan.title} "
                f"source={plan.source} category={plan.category} save_path={plan.save_path}"
            )
        for webhook_result in outcome.webhook_results:
            print(
                "planned webhook: "
                f"job_id={outcome.job_id} status={webhook_result.status} dry_run={webhook_result.plan.dry_run} "
                f"event_type={webhook_result.plan.payload.get('event_type')}"
            )


def _print_monitor_details(result) -> None:
    for organizer_result in result.organizer_results:
        for action in organizer_result.actions:
            label = "planned organizer" if action.status == "planned" else "organizer"
            print(
                f"{label}: "
                f"job_id={organizer_result.job_id} status={action.status} media_type={action.media_type} "
                f"source={action.source_path} destination={action.destination_path}"
            )
    for webhook_result in result.webhook_results:
        label = "planned webhook" if webhook_result.plan.dry_run else "webhook"
        print(
            f"{label}: "
            f"status={webhook_result.status} dry_run={webhook_result.plan.dry_run} "
            f"event_type={webhook_result.plan.payload.get('event_type')}"
        )


def _completed_snapshots(result, source_path: str) -> tuple[TorrentSnapshot, ...]:
    snapshots: list[TorrentSnapshot] = []
    for outcome in result.candidates:
        info_hash = outcome.candidate.feed_item.info_hash
        if not info_hash:
            continue
        snapshots.append(
            TorrentSnapshot(
                torrent_hash=info_hash,
                name=outcome.candidate.title,
                state="uploading",
                progress=1.0,
                content_path=source_path,
            )
        )
    return tuple(snapshots)


def _monitor_once(args: argparse.Namespace) -> int:
    snapshots = snapshots_from_json(args.snapshot_json) if args.snapshot_json else ()
    dry_run = not args.apply
    result = monitor_once(args.config, snapshots, dry_run=dry_run)
    print(
        f"monitor once: dry_run={dry_run} updated_events={len(result.events)} organizer_inputs={len(result.organizer_inputs)} failures={len(result.failures)}"
    )
    _print_monitor_details(result)
    return 0


def _organize_once(args: argparse.Namespace) -> int:
    organizer_input = OrganizerInput(
        args.job_id,
        args.torrent_hash,
        args.title,
        args.source_path,
        datetime.now(timezone.utc),
    )
    result = organize_once(args.config, organizer_input)
    print(
        f"organize once: job_id={result.result.job_id} actions={len(result.result.actions)}"
    )
    return 0


def _state(args: argparse.Namespace) -> int:
    summary = list_state(args.config)
    print(_summary_json(summary))
    return 0


def _failures(args: argparse.Namespace) -> int:
    summary = list_state(args.config)
    print(
        json.dumps(
            {
                "failed": summary.failed,
                "retryable": summary.retryable,
                "all_failures": summary.all_failures,
            },
            sort_keys=True,
            default=str,
        )
    )
    return 0


def _audit_ingestion(args: argparse.Namespace) -> int:
    result = audit_ingestion(args.config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def _retry_failed(args: argparse.Namespace) -> int:
    result = retry_failed_item(args.config, args.job_id)
    print(result.message)
    return 0 if result.retried else 1


def _schedule_tick(args: argparse.Namespace) -> int:
    dry_run = not args.apply
    if dry_run:
        result = scheduler_tick(
            args.config, dependencies=_feed_file_dependencies(args.feed_file)
        )
        print(
            f"schedule tick: parsed={result.parsed_items} candidates={len(result.candidates)}"
        )
        return 0
    result = production_tick(
        args.config, dry_run=False, dependencies=_feed_file_dependencies(args.feed_file)
    )
    print(json.dumps(result.summary(), ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result.ok else 1


def _feed_file_dependencies(path: str | None) -> WorkflowDependencies | None:
    if path is None:
        return None
    text = Path(path).read_text(encoding="utf-8")
    return WorkflowDependencies(feed_fetcher=lambda _url: text)


def _summary_json(summary) -> str:
    return json.dumps(
        {
            "archived_rules": summary.archived_rules,
            "processed": summary.processed,
            "pending": summary.pending,
            "failed": summary.failed,
            "retryable": summary.retryable,
            "all_failures": summary.all_failures,
        },
        sort_keys=True,
        default=str,
    )


if __name__ == "__main__":
    raise SystemExit(main())
