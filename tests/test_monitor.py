from datetime import datetime, timezone

from hermes_dmhy_anime_subscription.config import RetryConfig
from hermes_dmhy_anime_subscription.models import DownloadJobStatus
from hermes_dmhy_anime_subscription.monitor import TorrentSnapshot, monitor_downloads, torrent_state_family
from hermes_dmhy_anime_subscription.state import SubscriptionState

NOW = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def test_torrent_state_family_covers_qbittorrent_states():
    assert torrent_state_family("queuedDL") is DownloadJobStatus.QUEUED
    assert torrent_state_family("downloading") is DownloadJobStatus.DOWNLOADING
    assert torrent_state_family("stalledDL") is DownloadJobStatus.STALLED
    assert torrent_state_family("uploading") is DownloadJobStatus.COMPLETED
    assert torrent_state_family("error") is DownloadJobStatus.ERROR
    assert torrent_state_family("missingFiles") is DownloadJobStatus.MISSING
    assert torrent_state_family("deleted") is DownloadJobStatus.DELETED
    assert torrent_state_family("checkingDL", progress=1.0) is DownloadJobStatus.COMPLETED


def test_downloading_and_queued_snapshots_update_state_and_emit_submit_progress_events(tmp_path):
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        _insert_job(state, "job-queued", "HASHQUEUED")
        _insert_job(state, "job-downloading", "HASHDOWN")

        result = monitor_downloads(
            state,
            (
                TorrentSnapshot(torrent_hash="HASHQUEUED", name="Queued", state="queuedDL"),
                TorrentSnapshot(torrent_hash="HASHDOWN", name="Downloading", state="downloading", progress=0.42),
            ),
            _retry(),
            now=NOW,
        )

        queued = state.get_job("job-queued")
        downloading = state.get_job("job-downloading")

    assert queued is not None
    assert downloading is not None
    assert queued["status"] == "queued"
    assert downloading["status"] == "downloading"
    assert [event.event_type for event in result.events] == ["download_submitted", "download_progress"]
    assert result.organizer_inputs == ()


def test_stalled_waits_with_retry_metadata_before_threshold(tmp_path):
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        _insert_job(state, "job-stalled", "HASHSTALLED")

        result = monitor_downloads(
            state,
            (TorrentSnapshot(torrent_hash="HASHSTALLED", name="Stalled", state="stalledDL"),),
            _retry(max_attempts=3, backoff_seconds=120),
            now=NOW,
        )
        job = state.get_job("job-stalled")

    assert job is not None
    assert job["status"] == "stalled"
    assert job["retry_count"] == 1
    assert job["metadata"]["next_retry_at"] == "2026-05-24T12:02:00+00:00"
    assert job["metadata"]["retry_exhausted"] is False
    assert result.failures == ()
    assert result.events[0].event_type == "download_retry_waiting"


def test_completed_torrent_creates_organizer_input_once(tmp_path):
    snapshot = TorrentSnapshot(
        torrent_hash="HASHDONE",
        name="Example Anime 01",
        state="uploading",
        progress=1.0,
        save_path="/downloads/anime",
        content_path="/downloads/anime/Example Anime 01.mkv",
    )

    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        _insert_job(state, "job-done", "HASHDONE")
        first = monitor_downloads(state, (snapshot,), _retry(), now=NOW)
        second = monitor_downloads(state, (snapshot,), _retry(), now=NOW)
        job = state.get_job("job-done")

    assert job is not None
    assert job["status"] == "completed"
    assert job["organizer_outcome"] == "planned"
    assert len(first.organizer_inputs) == 1
    assert first.organizer_inputs[0].job_id == "job-done"
    assert first.organizer_inputs[0].source_path == "/downloads/anime/Example Anime 01.mkv"
    assert [event.event_type for event in first.events] == ["download_completed"]
    assert second.organizer_inputs == ()
    assert second.events == ()


def test_completed_torrent_organizer_input_prefers_job_metadata_title(tmp_path):
    snapshot = TorrentSnapshot(
        torrent_hash="HASHDONE",
        name="[64bitsub][Super no Ura de Yani Suu Futari][03][1920x1080][AVC_AAC][CHT].mp4",
        state="uploading",
        progress=1.0,
        save_path="/downloads/anime",
        content_path="/downloads/anime/[64bitsub][Super no Ura de Yani Suu Futari][03][1920x1080][AVC_AAC][CHT].mp4",
    )
    release_title = "[喵萌奶茶屋&LoliHouse] 超市后门吸烟的两人 / Super no Ura de Yani Suu Futari - 03 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"

    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        _insert_job(state, "job-done", "HASHDONE", metadata={"title": release_title, "bangumi_subject_id": 571784})
        result = monitor_downloads(state, (snapshot,), _retry(), now=NOW)

    assert result.organizer_inputs[0].title == release_title
    assert result.organizer_inputs[0].metadata["content_path"] == snapshot.content_path


def test_completed_torrent_without_content_path_does_not_organize_save_path(tmp_path):
    snapshot = TorrentSnapshot(
        torrent_hash="HASHDONE",
        name="Example Anime 01",
        state="uploading",
        progress=1.0,
        save_path="/downloads/anime",
        content_path=None,
    )

    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        _insert_job(state, "job-done", "HASHDONE")
        result = monitor_downloads(state, (snapshot,), _retry(), now=NOW)
        job = state.get_job("job-done")

    assert job is not None
    assert job["status"] == "completed"
    assert job["organizer_outcome"] is None
    assert job["metadata"]["save_path"] == "/downloads/anime"
    assert job["metadata"]["content_path"] is None
    assert result.organizer_inputs == ()
    assert [event.event_type for event in result.events] == ["download_completed"]


def test_repeated_errored_state_exhausts_retries_and_records_failure_event(tmp_path):
    snapshot = TorrentSnapshot(torrent_hash="HASHERR", name="Broken", state="error", error="qBittorrent reported an error")

    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        _insert_job(state, "job-error", "HASHERR")
        first = monitor_downloads(state, (snapshot,), _retry(max_attempts=2), now=NOW)
        second = monitor_downloads(state, (snapshot,), _retry(max_attempts=2), now=NOW)
        third = monitor_downloads(state, (snapshot,), _retry(max_attempts=2), now=NOW)
        job = state.get_job("job-error")
        failure = state.get_failure("job-error", "download")

    assert job is not None
    assert failure is not None
    assert job["status"] == "failed"
    assert job["retry_count"] == 2
    assert first.events[0].event_type == "download_retry_waiting"
    assert [event.event_type for event in second.events] == ["download_failure"]
    assert second.failures[0].attempts == 2
    assert failure["attempts"] == 2
    assert failure["recoverable"] == 0
    assert third.events == ()
    assert third.failures == ()


def test_missing_expected_job_and_deleted_snapshot_use_retry_policy(tmp_path):
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        _insert_job(state, "job-missing", "HASHMISSING")
        _insert_job(state, "job-deleted", "HASHDELETED")

        missing = monitor_downloads(state, (), _retry(max_attempts=3), expected_job_ids=("job-missing",), now=NOW)
        deleted = monitor_downloads(
            state,
            (TorrentSnapshot(torrent_hash="HASHDELETED", name="Deleted", state="deleted"),),
            _retry(max_attempts=3),
            now=NOW,
        )
        missing_job = state.get_job("job-missing")
        deleted_job = state.get_job("job-deleted")

    assert missing_job is not None
    assert deleted_job is not None
    assert missing_job["status"] == "missing"
    assert missing_job["metadata"]["qbittorrent_state"] == "missing"
    assert deleted_job["status"] == "deleted"
    assert missing.events[0].event_type == "download_retry_waiting"
    assert deleted.events[0].event_type == "download_retry_waiting"


def _insert_job(state: SubscriptionState, job_id: str, torrent_hash: str, metadata=None) -> None:
    state.upsert_job(
        job_id,
        dedupe_key=f"infohash:{torrent_hash.lower()}",
        status=DownloadJobStatus.SUBMITTED,
        torrent_hash=torrent_hash,
        metadata=metadata or {"title": job_id},
    )


def _retry(max_attempts: int = 3, backoff_seconds: int = 60) -> RetryConfig:
    return RetryConfig(max_attempts=max_attempts, backoff_seconds=backoff_seconds)
