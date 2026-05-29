"""Workflow orchestration for DMHY subscription runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Callable, Iterable
from urllib.request import urlopen

from .bangumi import lookup_chinese_title
from .config import ConfigError, OrganizerConfig, PluginConfig, load_config
from .dmhy import parse_rss
from .models import DownloadJobStatus, FailureRecord, NotificationEvent, OrganizerMode, ReleaseCandidate, RuleEpisodeMode, SubscriptionRule
from .monitor import OrganizerInput, TorrentSnapshot, monitor_downloads
from .organizer import OrganizerResult, organize_media
from .qbittorrent import QbittorrentClient, QbittorrentSubmitResult, QbittorrentTorrent
from .rules import DedupeDecision, dedupe_items, match_rules
from .state import SubscriptionState
from .webhook import WebhookDeliveryPlan, WebhookDispatchResult, WebhookNotifier, build_webhook_payload

FeedFetcher = Callable[[str], str]
QbittorrentClientFactory = Callable[[PluginConfig], QbittorrentClient]
WebhookNotifierFactory = Callable[[PluginConfig], WebhookNotifier]
OrganizerRunner = Callable[[OrganizerInput, PluginConfig], OrganizerResult]
BangumiLookup = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class WorkflowDependencies:
    feed_fetcher: FeedFetcher | None = None
    qbittorrent_factory: QbittorrentClientFactory | None = None
    webhook_factory: WebhookNotifierFactory | None = None
    organizer_runner: OrganizerRunner | None = None
    bangumi_lookup: BangumiLookup | None = None


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
        return sum(1 for outcome in self.candidates if outcome.submit_result is not None and outcome.submit_result.dry_run)


@dataclass(frozen=True, slots=True)
class MonitorOnceResult:
    organizer_inputs: tuple[OrganizerInput, ...]
    events: tuple[NotificationEvent, ...]
    failures: tuple[FailureRecord, ...]
    organizer_results: tuple[OrganizerResult, ...] = ()
    webhook_results: tuple[WebhookDispatchResult, ...] = ()


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
            outcome.submit_result is not None and not outcome.submit_result.success for outcome in self.run_result.candidates
        )
        monitor_failures = bool(self.monitor_result and self.monitor_result.failures)
        webhook_failures = any(result.failure is not None for outcome in self.run_result.candidates for result in outcome.webhook_results)
        if self.monitor_result is not None:
            webhook_failures = webhook_failures or any(result.failure is not None for result in self.monitor_result.webhook_results)
        return not (submit_failures or monitor_failures or webhook_failures or self.qbit_failure is not None)

    def summary(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "run_once": {
                "parsed_items": self.run_result.parsed_items,
                "parse_errors": self.run_result.parse_errors,
                "candidates": len(self.run_result.candidates),
                "submitted_or_seen": [outcome.job_id for outcome in self.run_result.candidates],
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
                "failures": [asdict(failure) for failure in self.monitor_result.failures],
                "organizer_actions": [
                    {
                        "job_id": result.job_id,
                        "actions": [
                            {
                                "status": action.status,
                                "media_type": action.media_type,
                                "source": str(action.source_path),
                                "destination": str(action.destination_path) if action.destination_path else None,
                                "reason": action.reason,
                            }
                            for action in result.actions
                        ],
                    }
                    for result in self.monitor_result.organizer_results
                ],
            },
        }


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
    qbittorrent = deps.qbittorrent_factory(config) if deps.qbittorrent_factory else QbittorrentClient.from_config_env(config.qbittorrent)
    notifier = deps.webhook_factory(config) if deps.webhook_factory else WebhookNotifier(config.webhook)
    events: list[NotificationEvent] = []
    outcomes: list[CandidateOutcome] = []
    items = []
    parse_errors = 0

    for feed in config.dmhy.feeds:
        parsed = parse_rss(fetcher(feed.url), source_feed=feed.name)
        items.extend(parsed.items)
        parse_errors += len(parsed.errors)

    with SubscriptionState(_state_path(config, dry_run=dry_run)) as state:
        satisfied_seasons = set(state.list_satisfied_season_packs())
        matched: list[tuple[DedupeDecision, ReleaseCandidate, SubscriptionRule]] = []
        for decision in dedupe_items(tuple(items)):
            if not decision.accepted:
                continue
            if state.has_seen_item(decision.dedupe_key):
                continue
            candidate, rule = _first_candidate(decision.item, config.subscriptions.rules)
            if candidate is None:
                if not dry_run:
                    state.record_seen_item(decision.item)
                continue
            if rule is None:
                continue
            if _season_pack_satisfaction_key(candidate) in satisfied_seasons:
                continue
            matched.append((decision, candidate, rule))

        for decision, candidate, rule in _suppress_episodes_for_allowed_season_packs(matched):
            job_id = job_id_for_candidate(candidate)
            submit_result = qbittorrent.submit(candidate, rule=rule, dry_run=dry_run)
            status = _job_status(submit_result, dry_run=dry_run)
            if not dry_run:
                state.upsert_job(
                    job_id,
                    dedupe_key=decision.dedupe_key,
                    status=status,
                    torrent_hash=decision.item.info_hash,
                    retry_count=0,
                    last_error=submit_result.message if not submit_result.success else None,
                    metadata={
                        "title": candidate.title,
                        "rule_name": candidate.rule_name,
                        "dry_run": dry_run,
                        "submit_status": submit_result.status,
                        "qbittorrent_category": submit_result.plan.category,
                    },
                )
                if submit_result.success:
                    state.record_seen_item(decision.item)
                    if candidate.feed_item.is_season_pack and _rule_allows_pack(rule):
                        key = _season_pack_satisfaction_key(candidate)
                        state.record_satisfied_season_pack(*key, job_id=job_id, dedupe_key=decision.dedupe_key)
                        satisfied_seasons.add(key)
                else:
                    state.record_failure(job_id, "qbittorrent", submit_result.message, attempts=1, recoverable=submit_result.retryable)
            event = NotificationEvent(
                event_type="download_planned" if dry_run else "download_submitted",
                title=candidate.title,
                message=submit_result.message,
                job_id=job_id,
                severity="info" if submit_result.success else "error",
                metadata={
                    "rule_name": candidate.rule_name,
                    "release_title": candidate.title,
                    "guid": candidate.feed_item.guid,
                    "infohash": candidate.feed_item.info_hash,
                    "status": submit_result.status,
                },
            )
            events.append(event)
            webhook_result = _notify(notifier, event, dry_run=dry_run)
            if not dry_run and webhook_result.failure is not None:
                state.record_failure(webhook_result.failure.subject_id, webhook_result.failure.stage, webhook_result.failure.message, webhook_result.failure.attempts, webhook_result.failure.recoverable)
            outcomes.append(CandidateOutcome(candidate, decision, job_id, submit_result, (webhook_result,), status=status.value))

    return RunOnceResult(dry_run=dry_run, parsed_items=len(items), parse_errors=parse_errors, candidates=tuple(outcomes), events=tuple(events))


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
    deps = dependencies or WorkflowDependencies()
    notifier = deps.webhook_factory(config) if deps.webhook_factory else WebhookNotifier(config.webhook)
    bangumi_lookup = _bangumi_lookup(deps, dry_run=dry_run)
    organizer_runner = deps.organizer_runner or (lambda organizer_input, loaded_config: organize_media(organizer_input, loaded_config.organizer, bangumi_lookup=bangumi_lookup))
    with SubscriptionState(_state_path(config, dry_run=dry_run)) as state:
        expected = tuple(expected_job_ids) if expected_job_ids is not None else tuple(str(job["job_id"]) for job in state.list_jobs(statuses=_ACTIVE_STATUSES))
        result = monitor_downloads(state, snapshots, config.retry, expected_job_ids=expected)
        organizer_results: list[OrganizerResult] = []
        if organize:
            for organizer_input in result.organizer_inputs:
                organizer_result = organizer_runner(organizer_input, config)
                organizer_results.append(organizer_result)
                _record_organizer_actions(state, organizer_result, dry_run=dry_run)
        webhook_results = tuple(_notify(notifier, event, dry_run=dry_run) for event in (*result.events, *[event for item in organizer_results for event in item.events]))
        for webhook_result in webhook_results:
            if not dry_run and webhook_result.failure is not None:
                state.record_failure(webhook_result.failure.subject_id, webhook_result.failure.stage, webhook_result.failure.message, webhook_result.failure.attempts, webhook_result.failure.recoverable)
    return MonitorOnceResult(result.organizer_inputs, result.events, result.failures, tuple(organizer_results), webhook_results)



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
    pre_active_job_ids = _active_job_ids(config)
    run_result = run_once(config_path, dry_run=dry_run, dependencies=deps)
    if dry_run:
        return ProductionTickResult(dry_run=True, run_result=run_result)

    qbittorrent = deps.qbittorrent_factory(config) if deps.qbittorrent_factory else QbittorrentClient.from_config_env(config.qbittorrent)
    try:
        torrents = _list_monitor_torrents(qbittorrent, config)
    except RuntimeError as exc:
        return ProductionTickResult(
            dry_run=False,
            run_result=run_result,
            qbit_failure={"stage": "list_torrents", "message": str(exc), "retryable": True},
        )
    snapshots = snapshots_from_qbittorrent_torrents(config, torrents, job_ids=pre_active_job_ids)
    monitor_result = monitor_once(config_path, snapshots=snapshots, dry_run=False, organize=True, dependencies=deps, expected_job_ids=pre_active_job_ids)
    return ProductionTickResult(
        dry_run=False,
        run_result=run_result,
        torrent_count=len(torrents),
        snapshots=snapshots,
        monitor_result=monitor_result,
    )



def _active_job_ids(config: PluginConfig) -> tuple[str, ...]:
    with SubscriptionState(config.state.path) as state:
        return tuple(str(job["job_id"]) for job in state.list_jobs(statuses=_ACTIVE_STATUSES))


def _list_monitor_torrents(qbittorrent: QbittorrentClient, config: PluginConfig) -> tuple[QbittorrentTorrent, ...]:
    by_hash: dict[str, QbittorrentTorrent] = {}
    for torrent in qbittorrent.list_torrents(all_categories=True):
        if torrent.torrent_hash:
            by_hash[torrent.torrent_hash.lower()] = torrent
    return tuple(by_hash.values())


def _monitor_categories(config: PluginConfig, jobs: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    rule_categories = {rule.name: (rule.category or config.qbittorrent.category or "") for rule in config.subscriptions.rules}
    categories: set[str] = set()
    for job in jobs:
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        category = metadata.get("qbittorrent_category") if isinstance(metadata, dict) else None
        if not category and isinstance(metadata, dict):
            rule_name = metadata.get("rule_name")
            if isinstance(rule_name, str):
                category = rule_categories.get(rule_name)
        if category is not None:
            categories.add(str(category))
    if not categories:
        categories.add(config.qbittorrent.category or "")
    return tuple(sorted(categories))

def snapshots_from_qbittorrent_torrents(
    config: PluginConfig,
    torrents: Iterable[QbittorrentTorrent],
    *,
    job_ids: Iterable[str] | None = None,
) -> tuple[TorrentSnapshot, ...]:
    """Match active jobs to qBittorrent torrents and produce monitor snapshots."""

    by_hash = {torrent.torrent_hash.lower(): torrent for torrent in torrents if torrent.torrent_hash}
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
        torrent = by_hash.get(_infohash_to_qbittorrent_hash(stored_hash)) or by_hash.get(stored_hash)
        if torrent is None:
            continue
        completed_at = None
        if torrent.completion_on and torrent.completion_on > 0:
            completed_at = datetime.fromtimestamp(int(torrent.completion_on), tz=timezone.utc)
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
    ensure_apply_safe(config, dry_run=dry_run or config.organizer.mode is OrganizerMode.DRY_RUN)
    deps = dependencies or WorkflowDependencies()
    notifier = deps.webhook_factory(config) if deps.webhook_factory else WebhookNotifier(config.webhook)
    bangumi_lookup = _bangumi_lookup(deps, dry_run=dry_run)
    organizer_runner = deps.organizer_runner or (lambda item, loaded_config: organize_media(item, loaded_config.organizer, bangumi_lookup=bangumi_lookup))
    effective_config = _dry_run_organizer_config(config) if dry_run else config
    result = organizer_runner(organizer_input, effective_config)
    with SubscriptionState(_state_path(config, dry_run=dry_run)) as state:
        _record_organizer_actions(state, result, dry_run=dry_run)
    webhook_results = tuple(_notify(notifier, event, dry_run=dry_run) for event in result.events)
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
    notifier = deps.webhook_factory(config) if deps.webhook_factory else WebhookNotifier(config.webhook)
    bangumi_lookup = _bangumi_lookup(deps, dry_run=True)
    organizer_runner = deps.organizer_runner or (lambda organizer_input, loaded_config: organize_media(organizer_input, loaded_config.organizer, bangumi_lookup=bangumi_lookup))
    snapshots = _completed_snapshots_from_run_result(run_result, source_path)
    with SubscriptionState(":memory:") as state:
        for outcome in run_result.candidates:
            if outcome.submit_result is None or outcome.candidate.feed_item.info_hash is None:
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
                    "dry_run": True,
                    "submit_status": outcome.submit_result.status,
                },
            )
        expected = tuple(outcome.job_id for outcome in run_result.candidates if outcome.submit_result is not None)
        result = monitor_downloads(state, snapshots, config.retry, expected_job_ids=expected)
        organizer_results: list[OrganizerResult] = []
        for organizer_input in result.organizer_inputs:
            organizer_result = organizer_runner(organizer_input, _dry_run_organizer_config(config))
            organizer_results.append(organizer_result)
            _record_organizer_actions(state, organizer_result, dry_run=True)
        webhook_results = tuple(_notify(notifier, event, dry_run=True) for event in (*result.events, *[event for item in organizer_results for event in item.events]))
    return MonitorOnceResult(result.organizer_inputs, result.events, result.failures, tuple(organizer_results), webhook_results)


def list_state(config_path: str | os.PathLike[str]) -> StateSummary:
    config = load_config(config_path)
    with SubscriptionState(config.state.path) as state:
        jobs = state.list_jobs()
        failures = state.list_failures()
    processed_statuses = {DownloadJobStatus.SUBMITTED.value, DownloadJobStatus.QUEUED.value, DownloadJobStatus.DOWNLOADING.value, DownloadJobStatus.COMPLETED.value}
    pending_statuses = {DownloadJobStatus.PENDING.value, DownloadJobStatus.STALLED.value, DownloadJobStatus.ERROR.value, DownloadJobStatus.MISSING.value, DownloadJobStatus.DELETED.value}
    return StateSummary(
        processed=tuple(job for job in jobs if str(job["status"]) in processed_statuses),
        pending=tuple(job for job in jobs if str(job["status"]) in pending_statuses),
        failed=tuple(job for job in jobs if str(job["status"]) == DownloadJobStatus.FAILED.value),
        retryable=tuple(failure for failure in failures if bool(failure["recoverable"])),
    )


def retry_failed_item(config_path: str | os.PathLike[str], job_id: str) -> RetryResult:
    config = load_config(config_path)
    with SubscriptionState(config.state.path) as state:
        job = state.get_job(job_id)
        if job is None:
            return RetryResult(job_id, False, "Job not found")
        failures = state.list_failures(subject_id=job_id)
        if not failures and job["status"] != DownloadJobStatus.FAILED.value:
            return RetryResult(job_id, False, "Job is not failed")
        if failures and not any(bool(failure["recoverable"]) for failure in failures):
            return RetryResult(job_id, False, "Job failure is not retryable")
        state.upsert_job(
            job_id,
            dedupe_key=str(job["dedupe_key"]),
            status=DownloadJobStatus.PENDING,
            torrent_hash=job.get("torrent_hash"),
            retry_count=0,
            last_error=None,
            organizer_outcome=job.get("organizer_outcome"),
            metadata={**dict(job["metadata"]), "manual_retry_requested_at": datetime.now(timezone.utc).isoformat()},
        )
    return RetryResult(job_id, True, "Job reset to pending for retry")


def scheduler_tick(config_path: str | os.PathLike[str], *, dependencies: WorkflowDependencies | None = None) -> RunOnceResult:
    return run_once(config_path, dry_run=True, dependencies=dependencies)


def scheduling_guidance(config: PluginConfig) -> str:
    jitter = f" with up to {config.polling.jitter_seconds} seconds of jitter" if config.polling.jitter_seconds else ""
    return f"Call scheduler_tick once every {config.polling.interval_minutes} minutes{jitter}; do not install an in-process infinite loop."


def ensure_apply_safe(config: PluginConfig, *, dry_run: bool) -> None:
    if dry_run:
        return
    if not config.qbittorrent.username_env or not config.qbittorrent.password_env:
        raise ConfigError("apply mode requires qbittorrent username_env and password_env")
    if not os.environ.get(config.qbittorrent.username_env) or not os.environ.get(config.qbittorrent.password_env):
        raise ConfigError("apply mode requires qBittorrent credential environment variables to be set")
    if config.organizer.mode not in {OrganizerMode.APPLY, OrganizerMode.MOVE}:
        raise ConfigError("apply mode requires organizer.mode to be apply or move")
    if config.webhook.enabled and config.webhook.url_env and not os.environ.get(config.webhook.url_env):
        raise ConfigError("apply mode requires webhook URL environment variable to be set when webhook is enabled")


def fetch_url_text(url: str) -> str:
    with urlopen(url, timeout=30) as response:  # nosec: runtime CLI path, tests inject fixture fetchers
        return response.read().decode("utf-8", errors="replace")


def snapshots_from_json(path: str | os.PathLike[str]) -> tuple[TorrentSnapshot, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("snapshot JSON must be a list")
    return tuple(TorrentSnapshot(**item) for item in raw)


def job_id_for_candidate(candidate: ReleaseCandidate) -> str:
    key = candidate.feed_item.info_hash or candidate.feed_item.guid or candidate.feed_item.dedupe_key
    safe = "".join(char.casefold() if char.isalnum() else "-" for char in key).strip("-")
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

def _first_candidate(item, rules: tuple[SubscriptionRule, ...]) -> tuple[ReleaseCandidate | None, SubscriptionRule | None]:
    for result in match_rules(item, rules):
        if result.accepted and result.candidate is not None:
            return result.candidate, result.rule
    return None, None


def _suppress_episodes_for_allowed_season_packs(matches: list[tuple[DedupeDecision, ReleaseCandidate, SubscriptionRule]]) -> tuple[tuple[DedupeDecision, ReleaseCandidate, SubscriptionRule], ...]:
    pack_groups = {
        _season_pack_satisfaction_key(candidate)
        for _, candidate, rule in matches
        if candidate.feed_item.is_season_pack and _rule_allows_pack(rule)
    }
    return tuple(
        match
        for match in matches
        if match[1].feed_item.is_season_pack or _season_pack_satisfaction_key(match[1]) not in pack_groups
    )


def _rule_allows_pack(rule: SubscriptionRule) -> bool:
    return rule.allow_packs or rule.episode_mode in {RuleEpisodeMode.PACK, RuleEpisodeMode.BOTH}


def _season_pack_satisfaction_key(candidate: ReleaseCandidate) -> tuple[str, str, int]:
    return (candidate.rule_name, _series_key(candidate.title), _season_number(candidate.title))


def _series_key(title: str) -> str:
    value = re.sub(r"^\s*\[[^\]]+\]\s*", " ", title)
    value = re.sub(r"\[[^\]]*\]|\([^\)]*\)", " ", value)
    value = re.sub(r"\bS\d{1,2}\s*E\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bS\d{1,2}\b|\bSeason\s*\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)\s+Season\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"第\s*\d{1,2}\s*[季期]", " ", value)
    value = re.sub(r"(?:^|[\s_\-.])\d{1,3}(?:v\d+)?(?:[\s_\-.]|$)", " ", value)
    value = re.sub(r"\b(?:480|720|1080|2160)p\b|\b4k\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"季度全集|季度|全集|合集|season pack|batch|complete", " ", value, flags=re.IGNORECASE)
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
    organizer = OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=config.organizer.library_root, staging_root=config.organizer.staging_root)
    return replace(config, organizer=organizer)


def _bangumi_lookup(deps: WorkflowDependencies, *, dry_run: bool) -> BangumiLookup | None:
    if deps.bangumi_lookup is not None:
        return deps.bangumi_lookup
    if dry_run:
        return None
    return lookup_chinese_title


def _state_path(config: PluginConfig, *, dry_run: bool) -> str | Path:
    return ":memory:" if dry_run else config.state.path


def _completed_snapshots_from_run_result(run_result: RunOnceResult, source_path: str) -> tuple[TorrentSnapshot, ...]:
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


def _notify(notifier: WebhookNotifier, event: NotificationEvent, *, dry_run: bool) -> WebhookDispatchResult:
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


def _record_organizer_actions(state: SubscriptionState, result: OrganizerResult, *, dry_run: bool) -> None:
    for action in result.actions:
        if action.destination_path is None:
            continue
        outcome = "dry-run" if dry_run else action.status
        state.record_organizer_outcome(result.job_id, outcome, str(action.source_path), str(action.destination_path))


_ACTIVE_STATUSES = (
    DownloadJobStatus.SUBMITTED.value,
    DownloadJobStatus.QUEUED.value,
    DownloadJobStatus.DOWNLOADING.value,
    DownloadJobStatus.STALLED.value,
    DownloadJobStatus.ERROR.value,
    DownloadJobStatus.MISSING.value,
    DownloadJobStatus.DELETED.value,
)


_MEDIA_SUFFIXES = frozenset({".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"})
