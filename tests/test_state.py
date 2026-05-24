from datetime import datetime, timezone

from hermes_dmhy_anime_subscription.models import DownloadJobStatus, FeedItem
from hermes_dmhy_anime_subscription.state import SubscriptionState


def test_seen_item_recording_is_idempotent(tmp_path):
    item = FeedItem(
        title="Example Release",
        link="https://example.invalid/release",
        info_hash="ABCDEF",
        guid="guid-1",
        published_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        normalized_title="example release",
    )

    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.record_seen_item(item) is True
        assert state.record_seen_item(item) is False
        assert state.has_seen_item(item)


def test_job_and_torrent_hash_recording_are_idempotent(tmp_path):
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.upsert_job(
            "job-1",
            dedupe_key="infohash:abcdef",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="ABCDEF",
            retry_count=1,
            metadata={"rule": "example"},
        ) is True
        assert state.upsert_job(
            "job-1",
            dedupe_key="infohash:abcdef",
            status=DownloadJobStatus.COMPLETED,
            torrent_hash="ABCDEF",
            retry_count=1,
            organizer_outcome="planned",
            metadata={"rule": "example"},
        ) is False
        assert state.job_count("job-1") == 1
        assert state.record_torrent_hash("ABCDEF", job_id="job-1") is False

        job = state.get_job("job-1")

    assert job is not None
    assert job["status"] == "completed"
    assert job["torrent_hash"] == "abcdef"
    assert job["organizer_outcome"] == "planned"
    assert job["metadata"] == {"rule": "example"}


def test_failure_and_organizer_outcome_slots_are_available(tmp_path):
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.record_failure("job-1", "download", "temporary failure", attempts=2)
        state.record_organizer_outcome("job-1", "dry-run", "/tmp/source", "/tmp/dest")
