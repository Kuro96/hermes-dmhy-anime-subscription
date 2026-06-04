"""Workflow orchestration for DMHY subscription runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterable
from urllib.request import urlopen

from .bangumi import (
    BangumiSubjectEpisodes,
    fetch_subject_cover_url,
    fetch_subject_main_episodes,
    lookup_chinese_title,
)
from .config import ConfigError, OrganizerConfig, PluginConfig, load_config
from .dmhy import parse_rss
from .models import (
    DownloadJobStatus,
    FailureRecord,
    NotificationEvent,
    OrganizerMode,
    ReleaseCandidate,
    RuleEpisodeMode,
    SubscriptionRule,
)
from .monitor import OrganizerInput, TorrentSnapshot, monitor_downloads
from .organizer import OrganizerResult, organize_media
from .qbittorrent import QbittorrentClient, QbittorrentSubmitResult, QbittorrentTorrent
from .rules import DedupeDecision, match_rules
from .state import SubscriptionState
from .telegram import TelegramDispatchResult, TelegramNotifier, _valid_bot_token
from .webhook import (
    WebhookDeliveryPlan,
    WebhookDispatchResult,
    WebhookNotifier,
    build_webhook_payload,
)

FeedFetcher = Callable[[str], str]
QbittorrentClientFactory = Callable[[PluginConfig], QbittorrentClient]
WebhookNotifierFactory = Callable[[PluginConfig], WebhookNotifier]
TelegramNotifierFactory = Callable[[PluginConfig], TelegramNotifier]
OrganizerRunner = Callable[[OrganizerInput, PluginConfig], OrganizerResult]
BangumiLookup = Callable[[str], str | None]
BangumiSubjectFetcher = Callable[[int], BangumiSubjectEpisodes]
BangumiCoverFetcher = Callable[[int], str | None]


@dataclass(frozen=True, slots=True)
class WorkflowDependencies:
    feed_fetcher: FeedFetcher | None = None
    qbittorrent_factory: QbittorrentClientFactory | None = None
    webhook_factory: WebhookNotifierFactory | None = None
    telegram_factory: TelegramNotifierFactory | None = None
    organizer_runner: OrganizerRunner | None = None
    bangumi_lookup: BangumiLookup | None = None
    bangumi_subject_fetcher: BangumiSubjectFetcher | None = None
    bangumi_cover_fetcher: BangumiCoverFetcher | None = None


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    candidate: ReleaseCandidate
    dedupe_decision: DedupeDecision
    job_id: str
    submit_result: QbittorrentSubmitResult | None
    webhook_results: tuple[WebhookDispatchResult, ...] = ()
    status: str = "skipped"


@dataclass(frozen=True, slots=True)
class RunOnceResult:
    dry_run: bool
    parsed_items: int
    parse_errors: int
    candidates: tuple[CandidateOutcome, ...]
    events: tuple[NotificationEvent, ...]

    @property
    def planned_submissions(self) -> int:
        return sum(
            1
            for outcome in self.candidates
            if outcome.submit_result is not None and outcome.submit_result.dry_run
        )


@dataclass(frozen=True, slots=True)
class MonitorOnceResult:
    organizer_inputs: tuple[OrganizerInput, ...]
    events: tuple[NotificationEvent, ...]
    failures: tuple[FailureRecord, ...]
    organizer_results: tuple[OrganizerResult, ...] = ()
    webhook_results: tuple[WebhookDispatchResult, ...] = ()
    telegram_results: tuple[TelegramDispatchResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductionTickResult:
    dry_run: bool
    run_result: RunOnceResult
    torrent_count: int = 0
    snapshots: tuple[TorrentSnapshot, ...] = ()
    monitor_result: MonitorOnceResult | None = None
    qbit_failure: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        submit_failures = any(
            outcome.submit_result is not None and not outcome.submit_result.success
            for outcome in self.run_result.candidates
        )
        monitor_failures = bool(self.monitor_result and self.monitor_result.failures)
        webhook_failures = any(
            result.failure is not None
            for outcome in self.run_result.candidates
            for result in outcome.webhook_results
        )
        if self.monitor_result is not None:
            webhook_failures = webhook_failures or any(
                result.failure is not None
                for result in self.monitor_result.webhook_results
            )
            webhook_failures = webhook_failures or any(
                result.failure is not None
                for result in self.monitor_result.telegram_results
            )
        return not (
            submit_failures
            or monitor_failures
            or webhook_failures
            or self.qbit_failure is not None
        )

    def summary(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "run_once": {
                "parsed_items": self.run_result.parsed_items,
                "parse_errors": self.run_result.parse_errors,
                "candidates": len(self.run_result.candidates),
                "submitted_or_seen": [
                    outcome.job_id for outcome in self.run_result.candidates
                ],
            },
            "qbit": {
                "torrent_count": self.torrent_count,
                "snapshots_for_active_jobs": len(self.snapshots),
                "failure": self.qbit_failure,
            },
            "monitor": None
            if self.monitor_result is None
            else {
                "organizer_inputs": len(self.monitor_result.organizer_inputs),
                "events": len(self.monitor_result.events),
                "failures": [
                    asdict(failure) for failure in self.monitor_result.failures
                ],
                "organizer_actions": [
                    {
                        "job_id": result.job_id,
                        "actions": [
                            {
                                "status": action.status,
                                "media_type": action.media_type,
                                "source": str(action.source_path),
                                "destination": str(action.destination_path)
                                if action.destination_path
                                else None,
                                "reason": action.reason,
                            }
                            for action in result.actions
                        ],
                    }
                    for result in self.monitor_result.organizer_results
                ],
                "telegram_results": [
                    _telegram_result_summary(result)
                    for result in self.monitor_result.telegram_results
                ],
            },
        }


def _telegram_result_summary(result: TelegramDispatchResult) -> dict[str, object]:
    return {
        "success": result.success,
        "status": result.status,
        "message": result.message,
        "method": result.plan.method,
        "redacted_url": result.plan.redacted_url,
        "retryable": result.retryable,
        "http_status": result.http_status,
        "failure": _failure_summary(result.failure),
    }


def _failure_summary(failure: FailureRecord | None) -> dict[str, object] | None:
    if failure is None:
        return None
    summary = asdict(failure)
    summary["last_failed_at"] = failure.last_failed_at.isoformat()
    return summary


@dataclass(frozen=True, slots=True)
class OrganizeOnceResult:
    result: OrganizerResult
    webhook_results: tuple[WebhookDispatchResult, ...] = ()


@dataclass(frozen=True, slots=True)
class StateSummary:
    processed: tuple[dict[str, object], ...]
    pending: tuple[dict[str, object], ...]
    failed: tuple[dict[str, object], ...]
    retryable: tuple[dict[str, object], ...]
    archived_rules: tuple[dict[str, object], ...] = ()
    all_failures: tuple[dict[str, object], ...] = field(default=(), kw_only=True)


@dataclass(frozen=True, slots=True)
class RetryResult:
    job_id: str
    retried: bool
    message: str


def validate_config(config_path: str | os.PathLike[str]) -> PluginConfig:
    return load_config(config_path)


def run_once(
    config_path: str | os.PathLike[str],
    *,
    dry_run: bool = True,
    dependencies: WorkflowDependencies | None = None,
) -> RunOnceResult:
    config = load_config(config_path)
    ensure_apply_safe(config, dry_run=dry_run)
    deps = dependencies or WorkflowDependencies()
    fetcher = deps.feed_fetcher or fetch_url_text
    qbittorrent = (
        deps.qbittorrent_factory(config)
        if deps.qbittorrent_factory
        else QbittorrentClient.from_config_env(config.qbittorrent)
    )
    notifier = (
        deps.webhook_factory(config)
        if deps.webhook_factory
        else WebhookNotifier(config.webhook)
    )
    events: list[NotificationEvent] = []
    outcomes: list[CandidateOutcome] = []
    items = []
    parse_errors = 0

    for feed in config.dmhy.feeds:
        parsed = parse_rss(fetcher(feed.url), source_feed=feed.name)
        items.extend(parsed.items)
        parse_errors += len(parsed.errors)

    archived_rule_names = _archived_rule_names(config)
    with _run_once_state(config, dry_run=dry_run) as state:
        satisfied_seasons = set(
            state.list_satisfied_season_packs()
        ) | _active_satisfied_season_packs(
            state,
            config,
        )
        matched: list[tuple[DedupeDecision, ReleaseCandidate, SubscriptionRule]] = []
        matched_dedupe_keys: set[str] = set()
        for item in tuple(items):
            dedupe_key = item.dedupe_key
            if dedupe_key in matched_dedupe_keys:
                continue
            if state.has_seen_item(dedupe_key):
                continue
            candidate, rule = _first_candidate(
                item,
                config.subscriptions.rules,
                archived_rule_names=archived_rule_names,
            )
            if candidate is None or rule is None:
                continue
            if (
                not candidate.feed_item.is_season_pack
                and _rule_allows_pack(rule)
                and _season_pack_satisfaction_key(candidate) in satisfied_seasons
            ):
                continue
            decision = DedupeDecision(
                item=item,
                accepted=True,
                dedupe_key=dedupe_key,
                reason="first_matched",
            )
            matched.append((decision, candidate, rule))
            matched_dedupe_keys.add(dedupe_key)

        def submit_match(
            decision: DedupeDecision,
            candidate: ReleaseCandidate,
            rule: SubscriptionRule,
        ) -> QbittorrentSubmitResult:
            job_id = job_id_for_candidate(candidate)
            submit_result = qbittorrent.submit(candidate, rule=rule, dry_run=dry_run)
            status = _job_status(submit_result, dry_run=dry_run)
            if not dry_run:
                metadata = {
                    "title": candidate.title,
                    "rule_name": candidate.rule_name,
                    "bangumi_subject_id": rule.bangumi_subject_id,
                    "episode": _candidate_episode(candidate),
                    "dry_run": dry_run,
                    "submit_status": submit_result.status,
                    "qbittorrent_category": submit_result.plan.category,
                }
                metadata.update(_season_pack_satisfaction_metadata(candidate, rule))
                state.upsert_job(
                    job_id,
                    dedupe_key=decision.dedupe_key,
                    status=status,
                    torrent_hash=decision.item.info_hash,
                    retry_count=0,
                    last_error=submit_result.message
                    if not submit_result.success
                    else None,
                    metadata=metadata,
                )
                if submit_result.success:
                    state.clear_failure(job_id, "qbittorrent")
                    state.record_seen_item(decision.item)
                else:
                    state.record_failure(
                        job_id,
                        "qbittorrent",
                        submit_result.message,
                        attempts=1,
                        recoverable=submit_result.retryable,
                    )
                    if not submit_result.retryable:
                        state.record_seen_item(decision.item)
            event = NotificationEvent(
                event_type="download_planned" if dry_run else "download_submitted",
                title=candidate.title,
                message=submit_result.message,
                job_id=job_id,
                severity="info" if submit_result.success else "error",
                metadata={
                    "rule_name": candidate.rule_name,
                    "bangumi_subject_id": rule.bangumi_subject_id,
                    "release_title": candidate.title,
                    "guid": candidate.feed_item.guid,
                    "infohash": candidate.feed_item.info_hash,
                    "status": submit_result.status,
                },
            )
            events.append(event)
            webhook_result = _notify(notifier, event, dry_run=dry_run)
            if not dry_run and webhook_result.failure is not None:
                state.record_failure(
                    webhook_result.failure.subject_id,
                    webhook_result.failure.stage,
                    webhook_result.failure.message,
                    webhook_result.failure.attempts,
                    webhook_result.failure.recoverable,
                )
            outcomes.append(
                CandidateOutcome(
                    candidate,
                    decision,
                    job_id,
                    submit_result,
                    (webhook_result,),
                    status=status.value,
                )
            )
            return submit_result

        preferred_matches, deferred_episode_matches = (
            _defer_episodes_for_allowed_season_packs(matched)
        )
        accepted_pack_groups: set[tuple[str, str, int]] = set()
        flushed_pack_groups: set[tuple[str, str, int]] = set()
        pack_candidates_remaining: dict[tuple[str, str, int], int] = {}
        pack_matches_by_group: dict[tuple[str, str, int], list[DedupeDecision]] = {}
        for decision, candidate, rule in preferred_matches:
            if candidate.feed_item.is_season_pack and _rule_allows_pack(rule):
                key = _season_pack_satisfaction_key(candidate)
                pack_candidates_remaining[key] = (
                    pack_candidates_remaining.get(key, 0) + 1
                )
                pack_matches_by_group.setdefault(key, []).append(decision)

        def record_seen_pack_group(key: tuple[str, str, int]) -> None:
            if dry_run:
                return
            for pack_decision in pack_matches_by_group.get(key, ()):
                state.record_seen_item(pack_decision.item)

        def flush_deferred_episode_matches(key: tuple[str, str, int]) -> None:
            if key in accepted_pack_groups or key in flushed_pack_groups:
                return
            flushed_pack_groups.add(key)
            for decision, candidate, rule in deferred_episode_matches.get(key, ()):
                submit_match(decision, candidate, rule)

        for decision, candidate, rule in preferred_matches:
            if candidate.feed_item.is_season_pack and _rule_allows_pack(rule):
                key = _season_pack_satisfaction_key(candidate)
                if key in accepted_pack_groups:
                    if not dry_run:
                        state.record_seen_item(decision.item)
                    continue
            submit_result = submit_match(decision, candidate, rule)
            if candidate.feed_item.is_season_pack and _rule_allows_pack(rule):
                key = _season_pack_satisfaction_key(candidate)
                if submit_result.success:
                    accepted_pack_groups.add(key)
                    record_seen_pack_group(key)
                pack_candidates_remaining[key] -= 1
                if pack_candidates_remaining[key] == 0:
                    flush_deferred_episode_matches(key)
        for key, episode_matches in deferred_episode_matches.items():
            if key in accepted_pack_groups or key in flushed_pack_groups:
                continue
            for decision, candidate, rule in episode_matches:
                submit_match(decision, candidate, rule)

    return RunOnceResult(
        dry_run=dry_run,
        parsed_items=len(items),
        parse_errors=parse_errors,
        candidates=tuple(outcomes),
        events=tuple(events),
    )


def monitor_once(
    config_path: str | os.PathLike[str],
    snapshots: Iterable[TorrentSnapshot] = (),
    *,
    dry_run: bool = True,
    organize: bool = True,
    dependencies: WorkflowDependencies | None = None,
    expected_job_ids: Iterable[str] | None = None,
) -> MonitorOnceResult:
    config = load_config(config_path)
    if organize:
        ensure_apply_safe(config, dry_run=dry_run)
    elif not dry_run:
        _ensure_telegram_apply_safe(config)
    deps = dependencies or WorkflowDependencies()
    notifier = (
        deps.webhook_factory(config)
        if deps.webhook_factory
        else WebhookNotifier(config.webhook)
    )
    telegram_notifier = (
        deps.telegram_factory(config)
        if deps.telegram_factory
        else TelegramNotifier(config.telegram)
    )
    bangumi_lookup = _bangumi_lookup(deps, dry_run=dry_run)
    organizer_runner = deps.organizer_runner or (
        lambda organizer_input, loaded_config: organize_media(
            organizer_input, loaded_config.organizer, bangumi_lookup=bangumi_lookup
        )
    )
    with _monitor_state(config, dry_run=dry_run) as state:
        expected = (
            tuple(expected_job_ids)
            if expected_job_ids is not None
            else tuple(
                str(job["job_id"]) for job in state.list_jobs(statuses=_ACTIVE_STATUSES)
            )
        )
        result = monitor_downloads(
            state,
            snapshots,
            config.retry,
            expected_job_ids=expected,
            plan_organizer=organize,
        )
        organizer_results: list[OrganizerResult] = []
        organizer_inputs_by_job_id: dict[str, OrganizerInput] = {}
        effective_config = _dry_run_organizer_config(config) if dry_run else config
        if organize:
            for organizer_input in result.organizer_inputs:
                organizer_inputs_by_job_id[organizer_input.job_id] = organizer_input
                organizer_result = organizer_runner(organizer_input, effective_config)
                organizer_results.append(organizer_result)
                _record_organizer_actions(
                    state,
                    organizer_result,
                    dry_run=dry_run,
                    telegram_enabled=config.telegram.enabled,
                )
        telegram_results: tuple[TelegramDispatchResult, ...] = ()
        if not dry_run and config.telegram.enabled:
            telegram_results = _dispatch_pending_telegram_notifications(
                state,
                telegram_notifier,
                cover_fetcher=deps.bangumi_cover_fetcher or fetch_subject_cover_url,
            )
        if not dry_run:
            _record_completed_satisfied_season_packs(state, config)
        archive_events = (
            _archive_completed_rules(state, config, deps) if not dry_run else ()
        )
        all_events = (*result.events, *archive_events)
        webhook_results = tuple(
            _notify(notifier, event, dry_run=dry_run)
            for event in (
                *all_events,
                *[event for item in organizer_results for event in item.events],
            )
        )
        for webhook_result in webhook_results:
            if not dry_run and webhook_result.failure is not None:
                state.record_failure(
                    webhook_result.failure.subject_id,
                    webhook_result.failure.stage,
                    webhook_result.failure.message,
                    webhook_result.failure.attempts,
                    webhook_result.failure.recoverable,
                )
    return MonitorOnceResult(
        result.organizer_inputs,
        all_events,
        result.failures,
        tuple(organizer_results),
        webhook_results,
        telegram_results,
    )


def production_tick(
    config_path: str | os.PathLike[str],
    *,
    dry_run: bool = True,
    dependencies: WorkflowDependencies | None = None,
) -> ProductionTickResult:
    """Run one bounded scheduler tick, optionally applying qBittorrent monitor/organizer side effects."""

    config = load_config(config_path)
    ensure_apply_safe(config, dry_run=dry_run)
    deps = dependencies or WorkflowDependencies()
    if dry_run:
        run_result = run_once(config_path, dry_run=True, dependencies=deps)
        return ProductionTickResult(dry_run=True, run_result=run_result)

    pre_active_job_ids = _active_job_ids(config)
    qbittorrent = (
        deps.qbittorrent_factory(config)
        if deps.qbittorrent_factory
        else QbittorrentClient.from_config_env(config.qbittorrent)
    )
    try:
        torrents = _list_monitor_torrents(qbittorrent, config)
    except RuntimeError as exc:
        return ProductionTickResult(
            dry_run=False,
            run_result=RunOnceResult(
                dry_run=False, parsed_items=0, parse_errors=0, candidates=(), events=()
            ),
            qbit_failure={
                "stage": "list_torrents",
                "message": str(exc),
                "retryable": True,
            },
        )
    snapshots = snapshots_from_qbittorrent_torrents(
        config, torrents, job_ids=pre_active_job_ids
    )
    monitor_result = monitor_once(
        config_path,
        snapshots=snapshots,
        dry_run=False,
        organize=True,
        dependencies=deps,
        expected_job_ids=pre_active_job_ids,
    )
    run_result = run_once(config_path, dry_run=False, dependencies=deps)
    return ProductionTickResult(
        dry_run=False,
        run_result=run_result,
        torrent_count=len(torrents),
        snapshots=snapshots,
        monitor_result=monitor_result,
    )


def _active_job_ids(config: PluginConfig) -> tuple[str, ...]:
    with SubscriptionState(config.state.path) as state:
        return tuple(
            str(job["job_id"]) for job in state.list_jobs(statuses=_ACTIVE_STATUSES)
        )


def _list_monitor_torrents(
    qbittorrent: QbittorrentClient, config: PluginConfig
) -> tuple[QbittorrentTorrent, ...]:
    by_hash: dict[str, QbittorrentTorrent] = {}
    for torrent in qbittorrent.list_torrents(all_categories=True):
        if torrent.torrent_hash:
            by_hash[torrent.torrent_hash.lower()] = torrent
    return tuple(by_hash.values())


def snapshots_from_qbittorrent_torrents(
    config: PluginConfig,
    torrents: Iterable[QbittorrentTorrent],
    *,
    job_ids: Iterable[str] | None = None,
) -> tuple[TorrentSnapshot, ...]:
    """Match active jobs to qBittorrent torrents and produce monitor snapshots."""

    by_hash = {
        torrent.torrent_hash.lower(): torrent
        for torrent in torrents
        if torrent.torrent_hash
    }
    snapshots: list[TorrentSnapshot] = []
    selected_job_ids = set(job_ids) if job_ids is not None else None
    with SubscriptionState(config.state.path) as state:
        jobs = state.list_jobs(statuses=_ACTIVE_STATUSES)
    for job in jobs:
        if selected_job_ids is not None and str(job["job_id"]) not in selected_job_ids:
            continue
        stored_hash = str(job.get("torrent_hash") or "").lower()
        if not stored_hash:
            continue
        torrent = by_hash.get(
            _infohash_to_qbittorrent_hash(stored_hash)
        ) or by_hash.get(stored_hash)
        if torrent is None:
            continue
        completed_at = None
        if torrent.completion_on and torrent.completion_on > 0:
            completed_at = datetime.fromtimestamp(
                int(torrent.completion_on), tz=timezone.utc
            )
        snapshots.append(
            TorrentSnapshot(
                torrent_hash=stored_hash,
                name=_snapshot_title(torrent),
                state=torrent.state,
                progress=torrent.progress,
                save_path=torrent.save_path,
                content_path=torrent.content_path,
                completed_at=completed_at,
                metadata={"qbittorrent_hash": torrent.torrent_hash},
            )
        )
    return tuple(snapshots)


def organize_once(
    config_path: str | os.PathLike[str],
    organizer_input: OrganizerInput,
    *,
    dry_run: bool = True,
    dependencies: WorkflowDependencies | None = None,
) -> OrganizeOnceResult:
    config = load_config(config_path)
    ensure_apply_safe(
        config, dry_run=dry_run or config.organizer.mode is OrganizerMode.DRY_RUN
    )
    deps = dependencies or WorkflowDependencies()
    notifier = (
        deps.webhook_factory(config)
        if deps.webhook_factory
        else WebhookNotifier(config.webhook)
    )
    bangumi_lookup = _bangumi_lookup(deps, dry_run=dry_run)
    organizer_runner = deps.organizer_runner or (
        lambda item, loaded_config: organize_media(
            item, loaded_config.organizer, bangumi_lookup=bangumi_lookup
        )
    )
    effective_config = _dry_run_organizer_config(config) if dry_run else config
    result = organizer_runner(organizer_input, effective_config)
    with SubscriptionState(_state_path(config, dry_run=dry_run)) as state:
        _record_organizer_actions(
            state, result, dry_run=dry_run, telegram_enabled=False
        )
    webhook_results = tuple(
        _notify(notifier, event, dry_run=dry_run) for event in result.events
    )
    return OrganizeOnceResult(result, webhook_results)


def plan_completed_dry_run(
    config_path: str | os.PathLike[str],
    run_result: RunOnceResult,
    source_path: str,
    *,
    dependencies: WorkflowDependencies | None = None,
) -> MonitorOnceResult:
    config = load_config(config_path)
    deps = dependencies or WorkflowDependencies()
    notifier = (
        deps.webhook_factory(config)
        if deps.webhook_factory
        else WebhookNotifier(config.webhook)
    )
    bangumi_lookup = _bangumi_lookup(deps, dry_run=True)
    organizer_runner = deps.organizer_runner or (
        lambda organizer_input, loaded_config: organize_media(
            organizer_input, loaded_config.organizer, bangumi_lookup=bangumi_lookup
        )
    )
    snapshots = _completed_snapshots_from_run_result(run_result, source_path)
    with SubscriptionState(":memory:") as state:
        for outcome in run_result.candidates:
            if (
                outcome.submit_result is None
                or outcome.candidate.feed_item.info_hash is None
            ):
                continue
            state.upsert_job(
                outcome.job_id,
                dedupe_key=outcome.dedupe_decision.dedupe_key,
                status=DownloadJobStatus.PENDING,
                torrent_hash=outcome.candidate.feed_item.info_hash,
                retry_count=0,
                metadata={
                    "title": outcome.candidate.title,
                    "rule_name": outcome.candidate.rule_name,
                    "episode": _candidate_episode(outcome.candidate),
                    "dry_run": True,
                    "submit_status": outcome.submit_result.status,
                },
            )
        expected = tuple(
            outcome.job_id
            for outcome in run_result.candidates
            if outcome.submit_result is not None
        )
        result = monitor_downloads(
            state,
            snapshots,
            config.retry,
            expected_job_ids=expected,
        )
        organizer_results: list[OrganizerResult] = []
        for organizer_input in result.organizer_inputs:
            organizer_result = organizer_runner(
                organizer_input, _dry_run_organizer_config(config)
            )
            organizer_results.append(organizer_result)
            _record_organizer_actions(
                state, organizer_result, dry_run=True, telegram_enabled=False
            )
        webhook_results = tuple(
            _notify(notifier, event, dry_run=True)
            for event in (
                *result.events,
                *[event for item in organizer_results for event in item.events],
            )
        )
    return MonitorOnceResult(
        result.organizer_inputs,
        result.events,
        result.failures,
        tuple(organizer_results),
        webhook_results,
    )


def list_state(config_path: str | os.PathLike[str]) -> StateSummary:
    config = load_config(config_path)
    with SubscriptionState(config.state.path) as state:
        jobs = state.list_jobs()
        failures = state.list_failures()
        archived_rules = state.list_archived_rules()
        jobs_by_id = {str(job["job_id"]): job for job in jobs}
        retryable = tuple(
            failure
            for failure in failures
            if bool(failure["recoverable"])
            and str(failure["stage"]) in _DOWNLOAD_RETRY_STAGES
            and _is_manually_retryable(
                jobs_by_id.get(str(failure["subject_id"])), failures, state=state
            )
        )
    processed_statuses = {
        DownloadJobStatus.SUBMITTED.value,
        DownloadJobStatus.QUEUED.value,
        DownloadJobStatus.DOWNLOADING.value,
        DownloadJobStatus.COMPLETED.value,
    }
    pending_statuses = {
        DownloadJobStatus.PENDING.value,
        DownloadJobStatus.STALLED.value,
        DownloadJobStatus.ERROR.value,
        DownloadJobStatus.MISSING.value,
        DownloadJobStatus.DELETED.value,
    }
    return StateSummary(
        processed=tuple(
            job for job in jobs if str(job["status"]) in processed_statuses
        ),
        pending=tuple(job for job in jobs if str(job["status"]) in pending_statuses),
        failed=tuple(
            job for job in jobs if str(job["status"]) == DownloadJobStatus.FAILED.value
        ),
        retryable=retryable,
        all_failures=failures,
        archived_rules=archived_rules,
    )


def retry_failed_item(config_path: str | os.PathLike[str], job_id: str) -> RetryResult:
    config = load_config(config_path)
    with SubscriptionState(config.state.path) as state:
        job = state.get_job(job_id)
        if job is None:
            return RetryResult(job_id, False, "Job not found")
        failures = state.list_failures(subject_id=job_id)
        if str(job["status"]) not in _MANUAL_RETRY_STATUSES:
            return RetryResult(job_id, False, "Job is not failed")
        if _has_terminal_download_failure(failures):
            return RetryResult(job_id, False, "Job failure is not retryable")
        if _has_seen_qbittorrent_retry_suppression(job, failures, state=state):
            return RetryResult(
                job_id,
                False,
                "Job dedupe key is already suppressed by a successful pack",
            )
        if not _recoverable_download_failures(failures):
            return RetryResult(job_id, False, "Job failure is not retryable")
        state.upsert_job(
            job_id,
            dedupe_key=str(job["dedupe_key"]),
            status=DownloadJobStatus.PENDING,
            torrent_hash=job.get("torrent_hash"),
            retry_count=0,
            last_error=None,
            organizer_outcome=job.get("organizer_outcome"),
            metadata={
                **dict(job["metadata"]),
                "manual_retry_requested_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    return RetryResult(job_id, True, "Job reset to pending for retry")


def _is_manually_retryable(
    job: dict[str, object] | None,
    failures: tuple[dict[str, object], ...],
    *,
    state: SubscriptionState,
) -> bool:
    if job is None or str(job["status"]) not in _MANUAL_RETRY_STATUSES:
        return False
    subject_failures = tuple(
        failure
        for failure in failures
        if str(failure["subject_id"]) == str(job["job_id"])
    )
    if _has_terminal_download_failure(subject_failures):
        return False
    if _has_seen_qbittorrent_retry_suppression(job, subject_failures, state=state):
        return False
    return bool(_recoverable_download_failures(subject_failures))


def _recoverable_download_failures(
    failures: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        failure
        for failure in failures
        if bool(failure["recoverable"])
        and str(failure["stage"]) in _DOWNLOAD_RETRY_STAGES
    )


def _has_terminal_download_failure(failures: tuple[dict[str, object], ...]) -> bool:
    return any(
        not bool(failure["recoverable"])
        and str(failure["stage"]) in _DOWNLOAD_RETRY_STAGES
        for failure in failures
    )


def _has_seen_qbittorrent_retry_suppression(
    job: dict[str, object],
    failures: tuple[dict[str, object], ...],
    *,
    state: SubscriptionState,
) -> bool:
    return (
        str(job["status"]) == DownloadJobStatus.ERROR.value
        and state.has_seen_item(str(job["dedupe_key"]))
        and any(
            bool(failure["recoverable"]) and str(failure["stage"]) == "qbittorrent"
            for failure in failures
        )
    )


def _monitor_state(config: PluginConfig, *, dry_run: bool) -> SubscriptionState:
    if not dry_run:
        return SubscriptionState(config.state.path)
    state = SubscriptionState(":memory:")
    _copy_configured_state_if_available(Path(config.state.path), state)
    return state


def _run_once_state(config: PluginConfig, *, dry_run: bool) -> SubscriptionState:
    if not dry_run:
        return SubscriptionState(config.state.path)
    state = SubscriptionState(":memory:")
    _copy_configured_state_if_available(Path(config.state.path), state)
    return state


def _copy_configured_state_if_available(
    source_path: Path, destination: SubscriptionState
) -> None:
    if destination.copy_from_readonly(source_path):
        destination.initialize_schema()


def scheduler_tick(
    config_path: str | os.PathLike[str],
    *,
    dependencies: WorkflowDependencies | None = None,
) -> RunOnceResult:
    return run_once(config_path, dry_run=True, dependencies=dependencies)


def scheduling_guidance(config: PluginConfig) -> str:
    jitter = (
        f" with up to {config.polling.jitter_seconds} seconds of jitter"
        if config.polling.jitter_seconds
        else ""
    )
    return f"Call scheduler_tick once every {config.polling.interval_minutes} minutes{jitter}; do not install an in-process infinite loop."


def ensure_apply_safe(config: PluginConfig, *, dry_run: bool) -> None:
    if dry_run:
        return
    if not config.qbittorrent.username_env or not config.qbittorrent.password_env:
        raise ConfigError(
            "apply mode requires qbittorrent username_env and password_env"
        )
    if not os.environ.get(config.qbittorrent.username_env) or not os.environ.get(
        config.qbittorrent.password_env
    ):
        raise ConfigError(
            "apply mode requires qBittorrent credential environment variables to be set"
        )
    if config.organizer.mode not in {OrganizerMode.APPLY, OrganizerMode.MOVE}:
        raise ConfigError("apply mode requires organizer.mode to be apply or move")
    if (
        config.webhook.enabled
        and config.webhook.url_env
        and not os.environ.get(config.webhook.url_env)
    ):
        raise ConfigError(
            "apply mode requires webhook URL environment variable to be set when webhook is enabled"
        )
    _ensure_telegram_apply_safe(config)


def _ensure_telegram_apply_safe(config: PluginConfig) -> None:
    if not config.telegram.enabled:
        return
    token_env = config.telegram.bot_token_env
    token = os.environ.get(token_env or "")
    if not token:
        raise ConfigError(
            "apply mode requires Telegram bot token environment variable to be set when Telegram is enabled"
        )
    if not _valid_bot_token(token):
        raise ConfigError(
            "apply mode requires Telegram bot token environment variable to contain a valid Telegram bot token when Telegram is enabled"
        )


def fetch_url_text(url: str) -> str:
    with urlopen(url, timeout=30) as response:  # nosec: runtime CLI path, tests inject fixture fetchers
        return response.read().decode("utf-8", errors="replace")


def snapshots_from_json(path: str | os.PathLike[str]) -> tuple[TorrentSnapshot, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("snapshot JSON must be a list")
    return tuple(TorrentSnapshot(**item) for item in raw)


def job_id_for_candidate(candidate: ReleaseCandidate) -> str:
    key = (
        candidate.feed_item.info_hash
        or candidate.feed_item.guid
        or candidate.feed_item.dedupe_key
    )
    safe = "".join(char.casefold() if char.isalnum() else "-" for char in key).strip(
        "-"
    )
    return f"dmhy-{safe[:48]}"


def _infohash_to_qbittorrent_hash(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) == 40 and all(char in "0123456789abcdef" for char in normalized):
        return normalized
    try:
        padded = normalized + ("=" * ((8 - len(normalized) % 8) % 8))
        decoded = base64.b32decode(padded.upper())
    except Exception:
        return normalized
    if len(decoded) == 20:
        return decoded.hex()
    return normalized


def _snapshot_title(torrent: QbittorrentTorrent) -> str:
    source = torrent.content_path or torrent.name
    leaf = source.replace("\\", "/").rsplit("/", 1)[-1] if source else ""
    suffix = Path(leaf).suffix.casefold()
    title = leaf[: -len(suffix)] if suffix in _MEDIA_SUFFIXES else leaf
    return title or torrent.name


def _first_candidate(
    item,
    rules: tuple[SubscriptionRule, ...],
    *,
    archived_rule_names: frozenset[str] = frozenset(),
) -> tuple[ReleaseCandidate | None, SubscriptionRule | None]:
    active_rules = tuple(rule for rule in rules if rule.name not in archived_rule_names)
    for result in match_rules(item, active_rules):
        if result.accepted and result.candidate is not None:
            return result.candidate, result.rule
    return None, None


def _defer_episodes_for_allowed_season_packs(
    matches: list[tuple[DedupeDecision, ReleaseCandidate, SubscriptionRule]],
) -> tuple[
    tuple[tuple[DedupeDecision, ReleaseCandidate, SubscriptionRule], ...],
    dict[
        tuple[str, str, int],
        tuple[tuple[DedupeDecision, ReleaseCandidate, SubscriptionRule], ...],
    ],
]:
    pack_groups = {
        _season_pack_satisfaction_key(candidate)
        for _, candidate, rule in matches
        if candidate.feed_item.is_season_pack and _rule_allows_pack(rule)
    }
    preferred = []
    deferred: dict[
        tuple[str, str, int],
        list[tuple[DedupeDecision, ReleaseCandidate, SubscriptionRule]],
    ] = {}
    for match in matches:
        candidate = match[1]
        key = _season_pack_satisfaction_key(candidate)
        if not candidate.feed_item.is_season_pack and key in pack_groups:
            deferred.setdefault(key, []).append(match)
        else:
            preferred.append(match)
    return tuple(preferred), {key: tuple(value) for key, value in deferred.items()}


def _rule_allows_pack(rule: SubscriptionRule) -> bool:
    return rule.allow_packs or rule.episode_mode in {
        RuleEpisodeMode.PACK,
        RuleEpisodeMode.BOTH,
    }


def _season_pack_satisfaction_key(candidate: ReleaseCandidate) -> tuple[str, str, int]:
    return (
        candidate.rule_name,
        _series_key(
            candidate.title, strip_bare_numbers=not candidate.feed_item.is_season_pack
        ),
        _season_number(candidate.title),
    )


def _season_pack_satisfaction_metadata(
    candidate: ReleaseCandidate, rule: SubscriptionRule
) -> dict[str, object]:
    if not candidate.feed_item.is_season_pack or not _rule_allows_pack(rule):
        return {}
    rule_name, series_key, season = _season_pack_satisfaction_key(candidate)
    return {
        "season_pack_satisfaction": {
            "rule_name": rule_name,
            "series_key": series_key,
            "season": season,
        }
    }


def _series_key(title: str, *, strip_bare_numbers: bool = True) -> str:
    value = _strip_leading_release_group(title, strip_bare_numbers=strip_bare_numbers)
    value = re.sub(r"\[([^\]]*)\]", _series_key_bracket_replacement, value)
    value = re.sub(r"\([^\)]*\)", " ", value)
    key = _normalize_series_key(value, strip_bare_numbers=strip_bare_numbers)
    if key:
        return key
    match = re.match(r"^\s*\[(\d{1,3}(?:v\d+)?)\](.*)", title, flags=re.IGNORECASE)
    has_episode_delimiter = match and re.match(
        r"\s*[-–—]\s*\d{1,3}(?:v\d+)?\b", match.group(2), flags=re.IGNORECASE
    )
    if match and (not strip_bare_numbers or has_episode_delimiter):
        return _normalize_series_key(match.group(1), strip_bare_numbers=False)
    return key


def _strip_leading_release_group(title: str, *, strip_bare_numbers: bool = True) -> str:
    match = re.match(r"^\s*\[[^\]]+\]\s*", title)
    if not match:
        return title
    remainder = title[match.end() :]
    remainder = re.sub(r"\[([^\]]*)\]", _series_key_bracket_replacement, remainder)
    remainder = re.sub(r"\([^\)]*\)", " ", remainder)
    if _normalize_series_key(remainder, strip_bare_numbers=strip_bare_numbers):
        return title[match.end() :]
    return title


def _series_key_bracket_replacement(match: re.Match[str]) -> str:
    content = match.group(1).strip()
    if not content or _is_series_key_metadata_bracket(content):
        return " "
    return f" {content} "


def _is_series_key_metadata_bracket(content: str) -> bool:
    normalized = content.strip().casefold()
    return bool(
        re.fullmatch(r"\d{1,3}(?:v\d+)?", normalized)
        or re.fullmatch(r"(?:480|720|1080|2160)p", normalized)
        or re.fullmatch(r"4k", normalized)
        or re.fullmatch(
            r"[gb]b|hevc|x26[45]|avc|aac|flac|chs|cht|sc|tc|big5|gb", normalized
        )
    )


def _normalize_series_key(value: str, *, strip_bare_numbers: bool = True) -> str:
    value = re.sub(r"\bS\d{1,2}\s*E\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\bS\d{1,2}\b|\bSeason\s*\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)\s+Season\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"第\s*\d{1,2}\s*[季期]", " ", value)
    value = re.sub(r"第\s*\d{1,3}\s*[話话集]", " ", value)
    if strip_bare_numbers:
        value = re.sub(
            r"(?:^|[\s_\-.])\d{1,3}(?:v\d+)?(?:[\s_\-.]|$)(?!\s*[-–—]\s*\d{1,3}\b)",
            " ",
            value,
        )
        value = re.sub(
            r"(?:\s*[-–—]\s*\d{1,3}(?:v\d+)?)*(?:\s*[-–—]\s*)\s*$", " ", value
        )
    else:
        value = re.sub(
            r"(?:^|[\s_\.])\d{1,3}(?:v\d+)?\s*[-–—]\s*\d{1,3}(?:v\d+)?(?=$|[\s_\.]|\s*(?:季度全集|全集|合集|season pack|batch|complete))",
            " ",
            value,
            flags=re.IGNORECASE,
        )
    value = re.sub(
        r"\b(?:480|720|1080|2160)p\b|\b4k\b", " ", value, flags=re.IGNORECASE
    )
    value = re.sub(
        r"季度全集|季度|全集|合集|season pack|batch|complete",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[_\W]+", " ", value.casefold()).strip()


def _season_number(title: str) -> int:
    for pattern in (
        r"\bS(?P<season>\d{1,2})(?:\s*E\d{1,3})?\b",
        r"\bSeason\s*(?P<season>\d{1,2})\b",
        r"\b(?P<season>\d{1,2})(?:st|nd|rd|th)\s+Season\b",
        r"第\s*(?P<season>\d{1,2})\s*[季期]",
    ):
        match = re.search(pattern, title, flags=re.IGNORECASE)
        if match:
            return int(match.group("season"))
    return 1


def _job_status(result: QbittorrentSubmitResult, *, dry_run: bool) -> DownloadJobStatus:
    if result.success and dry_run:
        return DownloadJobStatus.PENDING
    if result.success:
        return DownloadJobStatus.SUBMITTED
    return DownloadJobStatus.FAILED if not result.retryable else DownloadJobStatus.ERROR


def _dry_run_organizer_config(config: PluginConfig) -> PluginConfig:
    if config.organizer.mode is OrganizerMode.DRY_RUN:
        return config
    organizer = OrganizerConfig(
        mode=OrganizerMode.DRY_RUN,
        library_root=config.organizer.library_root,
        staging_root=config.organizer.staging_root,
    )
    return replace(config, organizer=organizer)


def _bangumi_lookup(
    deps: WorkflowDependencies, *, dry_run: bool
) -> BangumiLookup | None:
    if deps.bangumi_lookup is not None:
        return deps.bangumi_lookup
    if dry_run:
        return None
    return lookup_chinese_title


def _state_path(config: PluginConfig, *, dry_run: bool) -> str | Path:
    return ":memory:" if dry_run else config.state.path


def _archived_rule_names(config: PluginConfig) -> frozenset[str]:
    if not config.state.path.exists():
        return frozenset()
    uri = f"{config.state.path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'archived_rules'"
        ).fetchone()
        if table is None:
            return frozenset()
        cursor = connection.execute("SELECT rule_name FROM archived_rules")
        return frozenset(str(row[0]) for row in cursor.fetchall())


def _completed_snapshots_from_run_result(
    run_result: RunOnceResult, source_path: str
) -> tuple[TorrentSnapshot, ...]:
    snapshots: list[TorrentSnapshot] = []
    for outcome in run_result.candidates:
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


def _archive_completed_rules(
    state: SubscriptionState,
    config: PluginConfig,
    deps: WorkflowDependencies,
) -> tuple[NotificationEvent, ...]:
    fetcher = deps.bangumi_subject_fetcher or fetch_subject_main_episodes
    events: list[NotificationEvent] = []
    for rule in config.subscriptions.rules:
        if rule.bangumi_subject_id is None or state.is_rule_archived(rule.name):
            continue
        subject = fetcher(rule.bangumi_subject_id)
        required = set(range(1, subject.eps + 1))
        fetched = set(subject.main_episode_numbers)
        if subject.eps <= 0 or not required.issubset(fetched):
            continue
        completed = _completed_rule_episodes(state, rule.name)
        if not required.issubset(completed):
            continue
        metadata = {
            "bangumi_subject_id": subject.subject_id,
            "eps": subject.eps,
            "main_episode_numbers": list(subject.main_episode_numbers),
            "completed_episodes": sorted(completed),
        }
        state.archive_rule(
            rule.name,
            bangumi_subject_id=rule.bangumi_subject_id,
            reason="bangumi_complete",
            metadata=metadata,
        )
        events.append(
            NotificationEvent(
                event_type="subscription_archived",
                title=rule.name,
                message=f"Subscription rule {rule.name} archived after Bangumi subject completion",
                severity="info",
                metadata={"rule_name": rule.name, "status": "archived", **metadata},
            )
        )
    return tuple(events)


def _completed_rule_episodes(state: SubscriptionState, rule_name: str) -> set[int]:
    completed: set[int] = set()
    for job in state.list_jobs(statuses=(DownloadJobStatus.COMPLETED.value,)):
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        if metadata.get("rule_name") != rule_name:
            continue
        organizer_outcome = job.get("organizer_outcome")
        if organizer_outcome != "applied":
            continue
        completed.update(_metadata_episodes(metadata))
    return completed


def _metadata_episodes(metadata: dict[str, object]) -> set[int]:
    episodes = _metadata_episode_list(metadata)
    episode = _integral_episode(metadata.get("episode"))
    if episode is not None:
        episodes.add(episode)
    return episodes


def _metadata_episode_list(metadata: dict[str, object]) -> set[int]:
    episodes: set[int] = set()
    value = metadata.get("episodes")
    if isinstance(value, list):
        for item in value:
            episode = _integral_episode(item)
            if episode is not None:
                episodes.add(episode)
    return episodes


def _integral_episode(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        if parsed.is_integer():
            return int(parsed)
    return None


def _candidate_episode(candidate: ReleaseCandidate) -> int | None:
    explicit = _integral_episode(candidate.episode)
    if explicit is not None:
        return explicit
    match = re.search(
        r"\bS\d{1,2}\s*E(?P<episode>\d{1,3})\b", candidate.title, flags=re.IGNORECASE
    )
    if match:
        return int(match.group("episode"))
    bracketed = re.search(
        r"(?:^|[\s_\-.\[\(])(?P<episode>\d{1,3})(?:v\d+)?(?:[\s_\-.\]\)]|$)",
        candidate.title,
    )
    return int(bracketed.group("episode")) if bracketed else None


def _notify(
    notifier: WebhookNotifier, event: NotificationEvent, *, dry_run: bool
) -> WebhookDispatchResult:
    if not dry_run:
        return notifier.notify(event, dry_run=False)
    plan = WebhookDeliveryPlan(
        url="",
        redacted_url="<dry-run>",
        payload=build_webhook_payload(event, dry_run=True),
        dry_run=True,
    )
    return WebhookDispatchResult(
        success=True,
        status="planned",
        message="Dry-run planned webhook delivery without HTTP mutation",
        plan=plan,
    )


def _dispatch_pending_telegram_notifications(
    state: SubscriptionState,
    notifier: TelegramNotifier,
    *,
    cover_fetcher: BangumiCoverFetcher,
) -> tuple[TelegramDispatchResult, ...]:
    results: list[TelegramDispatchResult] = []
    for job in state.list_jobs():
        notifications = _telegram_notifications(job.get("metadata"))
        changed = False
        for notification in notifications:
            if not _telegram_notification_should_dispatch(notification):
                continue
            event = _telegram_event_from_notification(notification)
            if event is None:
                continue
            result = _notify_telegram(notifier, event, cover_fetcher=cover_fetcher)
            results.append(result)
            changed = True
            if result.success:
                notification["status"] = "sent"
                notification.pop("last_failure", None)
                if not _has_failed_telegram_notification(notifications):
                    state.clear_failure(event.job_id or event.event_type, "telegram")
            else:
                notification["status"] = "failed"
                notification["retryable"] = result.retryable
                notification["last_failure"] = _telegram_result_summary(result)
                if result.failure is not None:
                    state.record_failure(
                        result.failure.subject_id,
                        result.failure.stage,
                        result.failure.message,
                        result.failure.attempts,
                        result.failure.recoverable,
                    )
        if changed:
            metadata = dict(job.get("metadata") if isinstance(job.get("metadata"), dict) else {})
            metadata[_TELEGRAM_NOTIFICATIONS_KEY] = notifications
            _save_job_metadata(state, job, metadata)
    return tuple(results)


def _telegram_notifications(metadata: object) -> list[dict[str, object]]:
    if not isinstance(metadata, dict):
        return []
    value = metadata.get(_TELEGRAM_NOTIFICATIONS_KEY)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _telegram_notification_should_dispatch(notification: dict[str, object]) -> bool:
    status = notification.get("status")
    return status == "pending" or (status == "failed" and notification.get("retryable") is True)


def _has_failed_telegram_notification(notifications: Iterable[dict[str, object]]) -> bool:
    return any(
        notification.get("status") == "failed"
        for notification in notifications
    )


def _telegram_event_from_notification(notification: dict[str, object]) -> NotificationEvent | None:
    event = notification.get("event")
    if not isinstance(event, dict):
        return None
    created_at = event.get("created_at")
    if isinstance(created_at, str):
        try:
            parsed_created_at = datetime.fromisoformat(created_at)
        except ValueError:
            parsed_created_at = datetime.now(timezone.utc)
    else:
        parsed_created_at = datetime.now(timezone.utc)
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return NotificationEvent(
        event_type=str(event.get("event_type") or "organizer_completed"),
        title=str(event.get("title") or "Episode organized"),
        message=str(event.get("message") or "Organizer completed episode"),
        job_id=str(event["job_id"]) if event.get("job_id") is not None else None,
        severity=str(event.get("severity") or "info"),
        metadata=dict(metadata),
        created_at=parsed_created_at,
    )


def _record_pending_telegram_notifications(
    metadata: dict[str, object], events: Iterable[NotificationEvent]
) -> dict[str, object]:
    notifications = _telegram_notifications(metadata)
    existing_keys = {str(item.get("key")) for item in notifications if item.get("key") is not None}
    for event in events:
        key = _telegram_notification_key(event)
        if key in existing_keys:
            continue
        notifications.append(
            {
                "key": key,
                "status": "pending",
                "retryable": True,
                "event": _telegram_event_record(event),
            }
        )
        existing_keys.add(key)
    if notifications:
        metadata[_TELEGRAM_NOTIFICATIONS_KEY] = notifications
    return metadata


def _telegram_notification_key(event: NotificationEvent) -> str:
    episode = event.metadata.get("episode")
    destination = event.metadata.get("destination_path")
    return f"{event.event_type}:{event.job_id}:{episode}:{destination}"


def _telegram_event_record(event: NotificationEvent) -> dict[str, object]:
    return {
        "event_type": event.event_type,
        "title": event.title,
        "message": event.message,
        "job_id": event.job_id,
        "severity": event.severity,
        "metadata": dict(event.metadata),
        "created_at": event.created_at.isoformat(),
    }


def _save_job_metadata(
    state: SubscriptionState, job: dict[str, object], metadata: dict[str, object]
) -> None:
    state.upsert_job(
        str(job["job_id"]),
        dedupe_key=str(job["dedupe_key"]),
        status=str(job["status"]),
        torrent_hash=job.get("torrent_hash") if isinstance(job.get("torrent_hash"), str) else None,
        retry_count=int(job["retry_count"]),
        last_error=job.get("last_error") if isinstance(job.get("last_error"), str) else None,
        organizer_outcome=job.get("organizer_outcome") if isinstance(job.get("organizer_outcome"), str) else None,
        metadata=metadata,
    )


def _telegram_events_for_organizer_result(
    result: OrganizerResult,
    organizer_input: OrganizerInput | None,
) -> tuple[NotificationEvent, ...]:
    if organizer_input is None:
        return ()
    return tuple(
        event
        for action in result.actions
        if (
            event := _telegram_event_for_organizer_action(
                result, action, title=organizer_input.title, base_metadata=organizer_input.metadata
            )
        )
        is not None
    )


def _telegram_event_for_organizer_action(
    result: OrganizerResult, action: object, *, title: str, base_metadata: dict[str, object]
) -> NotificationEvent | None:
    subject_id = _integral_episode(base_metadata.get("bangumi_subject_id"))
    if subject_id is None:
        return None
    if (
        getattr(action, "status", None) != "applied"
        or getattr(action, "media_type", None) != "video"
        or getattr(action, "episode", None) is None
    ):
        return None
    metadata = dict(base_metadata)
    metadata.update(
        {
            "source_path": str(getattr(action, "source_path")),
            "destination_path": str(getattr(action, "destination_path"))
            if getattr(action, "destination_path", None)
            else None,
            "media_type": getattr(action, "media_type"),
            "episode": getattr(action, "episode"),
            "season": getattr(action, "season", None),
            "status": getattr(action, "status"),
        }
    )
    return NotificationEvent(
        event_type="organizer_completed",
        title=title,
        message="Organizer completed episode",
        job_id=result.job_id,
        severity="info",
        metadata=metadata,
    )


def _notify_telegram(
    notifier: TelegramNotifier,
    event: NotificationEvent,
    *,
    cover_fetcher: BangumiCoverFetcher,
) -> TelegramDispatchResult:
    cover_url = None
    subject_id = _integral_episode(event.metadata.get("bangumi_subject_id"))
    if subject_id is not None:
        try:
            cover_url = cover_fetcher(subject_id)
        except Exception:
            cover_url = None
    return notifier.notify(event, cover_url=cover_url)


def _record_organizer_actions(
    state: SubscriptionState,
    result: OrganizerResult,
    *,
    dry_run: bool,
    telegram_enabled: bool,
) -> None:
    for action in result.actions:
        if action.destination_path is None:
            continue
        outcome = "dry-run" if dry_run else action.status
        job = state.get_job(result.job_id)
        if job is not None:
            metadata = dict(job["metadata"])
            applied = not dry_run and action.status == "applied"
            if applied and action.episode is not None:
                # During organizer action recording, the candidate-level legacy scalar
                # metadata["episode"] is not proof that that episode was organized.
                # Only episodes already recorded by applied actions may be carried
                # forward here; _completed_rule_episodes still reads the scalar for
                # older completed jobs that predate metadata["episodes"].
                episodes = _metadata_episode_list(metadata)
                episodes.add(action.episode)
                metadata["episode"] = action.episode
                metadata["episodes"] = sorted(episodes)
            if applied and action.season is not None:
                metadata["season"] = action.season
            if applied and telegram_enabled:
                event = _telegram_event_for_organizer_action(
                    result,
                    action,
                    title=str(metadata.get("title") or result.job_id),
                    base_metadata=metadata,
                )
                if event is not None:
                    metadata = _record_pending_telegram_notifications(metadata, (event,))
            state.upsert_job(
                result.job_id,
                dedupe_key=str(job["dedupe_key"]),
                status=str(job["status"]),
                torrent_hash=job.get("torrent_hash"),
                retry_count=int(job["retry_count"]),
                last_error=job.get("last_error"),
                organizer_outcome=outcome,
                metadata=metadata,
            )
        state.record_organizer_outcome(
            result.job_id,
            outcome,
            str(action.source_path),
            str(action.destination_path),
        )


def _record_completed_satisfied_season_packs(
    state: SubscriptionState, config: PluginConfig
) -> None:
    pack_rule_names = {
        rule.name for rule in config.subscriptions.rules if _rule_allows_pack(rule)
    }
    for job in state.list_jobs(statuses=(DownloadJobStatus.COMPLETED.value,)):
        satisfaction = _satisfied_season_pack_from_job(job, pack_rule_names)
        if satisfaction is None:
            continue
        rule_name, series_key, season = satisfaction
        state.record_satisfied_season_pack(
            rule_name,
            series_key,
            season,
            job_id=str(job["job_id"]),
            dedupe_key=str(job["dedupe_key"]),
        )


def _active_satisfied_season_packs(
    state: SubscriptionState,
    config: PluginConfig,
) -> set[tuple[str, str, int]]:
    pack_rule_names = {
        rule.name for rule in config.subscriptions.rules if _rule_allows_pack(rule)
    }
    return {
        satisfaction
        for job in state.list_jobs(statuses=_PACK_SUPPRESSION_STATUSES)
        if (satisfaction := _satisfied_season_pack_from_job(job, pack_rule_names))
        is not None
    }


def _satisfied_season_pack_from_job(
    job: dict[str, object],
    pack_rule_names: set[str],
) -> tuple[str, str, int] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    satisfaction = metadata.get("season_pack_satisfaction")
    if not isinstance(satisfaction, dict):
        return None
    rule_name = satisfaction.get("rule_name")
    series_key = satisfaction.get("series_key")
    season = _integral_episode(satisfaction.get("season"))
    if not isinstance(rule_name, str) or rule_name not in pack_rule_names:
        return None
    if not isinstance(series_key, str) or season is None:
        return None
    return rule_name, series_key, season


_TELEGRAM_NOTIFICATIONS_KEY = "telegram_notifications"


_ACTIVE_STATUSES = (
    DownloadJobStatus.SUBMITTED.value,
    DownloadJobStatus.QUEUED.value,
    DownloadJobStatus.DOWNLOADING.value,
    DownloadJobStatus.STALLED.value,
    DownloadJobStatus.ERROR.value,
    DownloadJobStatus.MISSING.value,
    DownloadJobStatus.DELETED.value,
)


_PACK_SUPPRESSION_STATUSES = (
    DownloadJobStatus.SUBMITTED.value,
    DownloadJobStatus.QUEUED.value,
    DownloadJobStatus.DOWNLOADING.value,
)


_MANUAL_RETRY_STATUSES = frozenset(
    {
        DownloadJobStatus.FAILED.value,
        DownloadJobStatus.ERROR.value,
        DownloadJobStatus.MISSING.value,
        DownloadJobStatus.DELETED.value,
        DownloadJobStatus.STALLED.value,
    }
)


_DOWNLOAD_RETRY_STAGES = frozenset({"download", "qbittorrent"})


_MEDIA_SUFFIXES = frozenset(
    {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
)
