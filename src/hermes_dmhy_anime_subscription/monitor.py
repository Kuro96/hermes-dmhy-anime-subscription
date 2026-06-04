"""Download monitor for mocked qBittorrent torrent snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .config import RetryConfig
from .models import DownloadJobStatus, FailureRecord, NotificationEvent
from .state import SubscriptionState

QUEUED_STATES = frozenset({"queueddl", "queuedup", "queued", "metadl"})
DOWNLOADING_STATES = frozenset({"downloading", "forceddl", "allocating", "checkingdl", "checkingresume", "moving"})
STALLED_STATES = frozenset({"stalleddl", "stalled", "pauseddl"})
COMPLETED_STATES = frozenset({"uploading", "stalledup", "forcedup", "pausedup", "completed", "seeding"})
ERRORED_STATES = frozenset({"error", "missing", "missingfiles", "unknown", "timeout", "timedout"})
DELETED_STATES = frozenset({"deleted", "removed"})
RETRY_STAGE = "download"
ORGANIZER_OUTCOME_PLANNED = "planned"


@dataclass(frozen=True, slots=True)
class TorrentSnapshot:
    torrent_hash: str
    name: str
    state: str
    progress: float = 0.0
    save_path: str | None = None
    content_path: str | None = None
    completed_at: datetime | None = None
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrganizerInput:
    job_id: str
    torrent_hash: str
    title: str
    source_path: str
    completed_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MonitorResult:
    organizer_inputs: tuple[OrganizerInput, ...]
    events: tuple[NotificationEvent, ...]
    failures: tuple[FailureRecord, ...]
    updated_job_ids: tuple[str, ...]


def monitor_downloads(
    state: SubscriptionState,
    snapshots: Iterable[TorrentSnapshot],
    retry: RetryConfig,
    *,
    expected_job_ids: Iterable[str] = (),
    now: datetime | None = None,
    plan_organizer: bool = True,
) -> MonitorResult:
    """Update durable job state from supplied torrent snapshots."""

    observed_at = now or datetime.now(timezone.utc)
    organizer_inputs: list[OrganizerInput] = []
    events: list[NotificationEvent] = []
    failures: list[FailureRecord] = []
    updated: list[str] = []
    seen_job_ids: set[str] = set()

    for snapshot in snapshots:
        job = state.get_job_by_torrent_hash(snapshot.torrent_hash)
        if job is None:
            continue
        seen_job_ids.add(job["job_id"])
        outcome = _process_snapshot(
            state, job, snapshot, retry, observed_at, plan_organizer=plan_organizer
        )
        updated.append(job["job_id"])
        organizer_inputs.extend(outcome.organizer_inputs)
        events.extend(outcome.events)
        failures.extend(outcome.failures)

    for job_id in expected_job_ids:
        if job_id in seen_job_ids:
            continue
        job = state.get_job(job_id)
        if job is None or job["status"] in {DownloadJobStatus.COMPLETED.value, DownloadJobStatus.FAILED.value, DownloadJobStatus.SKIPPED.value}:
            continue
        snapshot = TorrentSnapshot(
            torrent_hash=job.get("torrent_hash") or job_id,
            name=str(job["metadata"].get("title") or job_id),
            state="missing",
            error="Torrent was not present in the supplied snapshot list",
        )
        outcome = _process_snapshot(
            state, job, snapshot, retry, observed_at, plan_organizer=plan_organizer
        )
        updated.append(job_id)
        events.extend(outcome.events)
        failures.extend(outcome.failures)

    return MonitorResult(
        organizer_inputs=tuple(organizer_inputs),
        events=tuple(events),
        failures=tuple(failures),
        updated_job_ids=tuple(updated),
    )


@dataclass(frozen=True, slots=True)
class _SnapshotOutcome:
    organizer_inputs: tuple[OrganizerInput, ...] = ()
    events: tuple[NotificationEvent, ...] = ()
    failures: tuple[FailureRecord, ...] = ()


def _process_snapshot(
    state: SubscriptionState,
    job: dict[str, Any],
    snapshot: TorrentSnapshot,
    retry: RetryConfig,
    observed_at: datetime,
    *,
    plan_organizer: bool,
) -> _SnapshotOutcome:
    family = torrent_state_family(snapshot.state, snapshot.progress)
    if family == DownloadJobStatus.COMPLETED:
        return _mark_completed(
            state, job, snapshot, observed_at, plan_organizer=plan_organizer
        )
    if family in {DownloadJobStatus.ERROR, DownloadJobStatus.MISSING, DownloadJobStatus.DELETED, DownloadJobStatus.STALLED}:
        return _record_retryable_state(state, job, snapshot, family, retry, observed_at)
    return _mark_active(state, job, snapshot, family, observed_at)


def torrent_state_family(qbittorrent_state: str, progress: float = 0.0) -> DownloadJobStatus:
    value = qbittorrent_state.strip().casefold()
    if progress >= 1.0 or value in COMPLETED_STATES:
        return DownloadJobStatus.COMPLETED
    if value in QUEUED_STATES:
        return DownloadJobStatus.QUEUED
    if value in DOWNLOADING_STATES:
        return DownloadJobStatus.DOWNLOADING
    if value in STALLED_STATES:
        return DownloadJobStatus.STALLED
    if value in DELETED_STATES:
        return DownloadJobStatus.DELETED
    if value in ERRORED_STATES:
        return DownloadJobStatus.MISSING if value in {"missing", "missingfiles"} else DownloadJobStatus.ERROR
    return DownloadJobStatus.ERROR


def _mark_active(
    state: SubscriptionState,
    job: dict[str, Any],
    snapshot: TorrentSnapshot,
    status: DownloadJobStatus,
    observed_at: datetime,
) -> _SnapshotOutcome:
    metadata = _snapshot_metadata(job, snapshot, observed_at, status)
    metadata.pop("next_retry_at", None)
    state.upsert_job(
        job["job_id"],
        dedupe_key=job["dedupe_key"],
        status=status,
        torrent_hash=snapshot.torrent_hash,
        retry_count=int(job["retry_count"]),
        last_error=None,
        organizer_outcome=job["organizer_outcome"],
        metadata=metadata,
    )
    event_type = "download_submitted" if status is DownloadJobStatus.QUEUED else "download_progress"
    return _SnapshotOutcome(events=(_event(event_type, job, snapshot, status.value, observed_at),))


def _mark_completed(
    state: SubscriptionState,
    job: dict[str, Any],
    snapshot: TorrentSnapshot,
    observed_at: datetime,
    *,
    plan_organizer: bool,
) -> _SnapshotOutcome:
    metadata = _snapshot_metadata(job, snapshot, observed_at, DownloadJobStatus.COMPLETED)
    metadata.pop("next_retry_at", None)
    already_planned = job["organizer_outcome"] == ORGANIZER_OUTCOME_PLANNED or bool(metadata.get("organizer_input_created_at"))
    if already_planned:
        state.upsert_job(
            job["job_id"],
            dedupe_key=job["dedupe_key"],
            status=DownloadJobStatus.COMPLETED,
            torrent_hash=snapshot.torrent_hash,
            retry_count=int(job["retry_count"]),
            last_error=None,
            organizer_outcome=job["organizer_outcome"],
            metadata=metadata,
        )
        return _SnapshotOutcome()

    completed_at = snapshot.completed_at or observed_at
    if not snapshot.content_path:
        state.upsert_job(
            job["job_id"],
            dedupe_key=job["dedupe_key"],
            status=DownloadJobStatus.COMPLETED,
            torrent_hash=snapshot.torrent_hash,
            retry_count=int(job["retry_count"]),
            last_error=None,
            organizer_outcome=job["organizer_outcome"],
            metadata=metadata,
        )
        events = (
            ()
            if job["status"] == DownloadJobStatus.COMPLETED.value
            else (_event("download_completed", job, snapshot, "completed", observed_at),)
        )
        return _SnapshotOutcome(events=events)

    if not plan_organizer:
        state.upsert_job(
            job["job_id"],
            dedupe_key=job["dedupe_key"],
            status=str(job["status"]),
            torrent_hash=snapshot.torrent_hash,
            retry_count=int(job["retry_count"]),
            last_error=None,
            organizer_outcome=job["organizer_outcome"],
            metadata=metadata,
        )
        events = (
            ()
            if job["status"] == DownloadJobStatus.COMPLETED.value
            else (_event("download_completed", job, snapshot, "completed", observed_at),)
        )
        return _SnapshotOutcome(events=events)

    source_path = snapshot.content_path
    metadata["organizer_input_created_at"] = observed_at.isoformat()
    state.upsert_job(
        job["job_id"],
        dedupe_key=job["dedupe_key"],
        status=DownloadJobStatus.COMPLETED,
        torrent_hash=snapshot.torrent_hash,
        retry_count=int(job["retry_count"]),
        last_error=None,
        organizer_outcome=ORGANIZER_OUTCOME_PLANNED,
        metadata=metadata,
    )
    state.record_organizer_outcome(job["job_id"], ORGANIZER_OUTCOME_PLANNED, source_path=source_path)
    organizer_input = OrganizerInput(
        job_id=job["job_id"],
        torrent_hash=snapshot.torrent_hash.lower(),
        title=snapshot.name,
        source_path=source_path,
        completed_at=completed_at,
        metadata={**metadata, "qbittorrent_state": snapshot.state, **dict(snapshot.metadata)},
    )
    return _SnapshotOutcome(
        organizer_inputs=(organizer_input,),
        events=(_event("download_completed", job, snapshot, "completed", observed_at),),
    )


def _record_retryable_state(
    state: SubscriptionState,
    job: dict[str, Any],
    snapshot: TorrentSnapshot,
    status: DownloadJobStatus,
    retry: RetryConfig,
    observed_at: datetime,
) -> _SnapshotOutcome:
    attempts = int(job["retry_count"]) + 1
    next_retry_at = observed_at + timedelta(seconds=retry.backoff_seconds)
    message = snapshot.error or f"Torrent entered {status.value} state from qBittorrent state {snapshot.state}"
    metadata = _snapshot_metadata(job, snapshot, observed_at, status)
    metadata["next_retry_at"] = next_retry_at.isoformat()
    metadata["retry_exhausted"] = attempts >= retry.max_attempts
    if attempts < retry.max_attempts:
        state.upsert_job(
            job["job_id"],
            dedupe_key=job["dedupe_key"],
            status=status,
            torrent_hash=snapshot.torrent_hash,
            retry_count=attempts,
            last_error=message,
            organizer_outcome=job["organizer_outcome"],
            metadata=metadata,
        )
        return _SnapshotOutcome(events=(_event("download_retry_waiting", job, snapshot, message, observed_at, severity="warning"),))

    if job["status"] == DownloadJobStatus.FAILED.value:
        return _SnapshotOutcome()
    state.upsert_job(
        job["job_id"],
        dedupe_key=job["dedupe_key"],
        status=DownloadJobStatus.FAILED,
        torrent_hash=snapshot.torrent_hash,
        retry_count=attempts,
        last_error=message,
        organizer_outcome=job["organizer_outcome"],
        metadata=metadata,
    )
    state.record_failure(job["job_id"], RETRY_STAGE, message, attempts=attempts, recoverable=False)
    failure = FailureRecord(subject_id=job["job_id"], stage=RETRY_STAGE, message=message, attempts=attempts, last_failed_at=observed_at, recoverable=False)
    return _SnapshotOutcome(
        events=(_event("download_failure", job, snapshot, message, observed_at, severity="error", attempts=attempts),),
        failures=(failure,),
    )


def _snapshot_metadata(job: dict[str, Any], snapshot: TorrentSnapshot, observed_at: datetime, status: DownloadJobStatus) -> dict[str, Any]:
    metadata = dict(job["metadata"])
    metadata.update(
        {
            "title": metadata.get("title") or snapshot.name,
            "qbittorrent_state": snapshot.state,
            "monitor_status": status.value,
            "progress": snapshot.progress,
            "last_monitored_at": observed_at.isoformat(),
            "save_path": snapshot.save_path,
            "content_path": snapshot.content_path,
            **dict(snapshot.metadata),
        }
    )
    return metadata


def _event(
    event_type: str,
    job: dict[str, Any],
    snapshot: TorrentSnapshot,
    message: str,
    observed_at: datetime,
    *,
    severity: str = "info",
    attempts: int | None = None,
) -> NotificationEvent:
    metadata: dict[str, Any] = {
        "torrent_hash": snapshot.torrent_hash.lower(),
        "qbittorrent_state": snapshot.state,
        "progress": snapshot.progress,
    }
    if attempts is not None:
        metadata["attempts"] = attempts
    return NotificationEvent(
        event_type=event_type,
        title=snapshot.name,
        message=message,
        job_id=job["job_id"],
        severity=severity,
        metadata=metadata,
        created_at=observed_at,
    )
