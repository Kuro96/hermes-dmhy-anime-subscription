from datetime import datetime, timezone

from hermes_dmhy_anime_subscription.models import (
    DownloadJob,
    DownloadJobStatus,
    FailureRecord,
    FeedItem,
    LibraryMovePlan,
    NotificationEvent,
    OrganizerMode,
    ReleaseCandidate,
    RuleEpisodeMode,
    StateEntry,
    SubscriptionRule,
)


def test_feed_item_dedupe_prefers_infohash_then_guid_then_title_date():
    published = datetime(2026, 1, 2, tzinfo=timezone.utc)

    assert FeedItem(title="A", link="l", info_hash="ABC").dedupe_key == "infohash:abc"
    assert FeedItem(title="A", link="l", guid="g-1").dedupe_key == "guid:g-1"
    assert FeedItem(title=" Example ", link="l", published_at=published).dedupe_key == "title-date:example:2026-01-02T00:00:00+00:00"


def test_core_models_construct_without_runtime_dependencies():
    item = FeedItem(title="Example", link="https://example.invalid/torrent", guid="guid-1")
    candidate = ReleaseCandidate(feed_item=item, rule_name="example-rule", title="Example", quality="1080p")
    job = DownloadJob(job_id="job-1", candidate=candidate, status=DownloadJobStatus.PENDING)
    rule = SubscriptionRule(name="example-rule", include_keywords=("Example",), category="anime")
    move_plan = LibraryMovePlan(source_path="/tmp/a", destination_path="/tmp/b", mode=OrganizerMode.DRY_RUN)
    event = NotificationEvent(event_type="job", title="Created", message="Job created", job_id=job.job_id)
    failure = FailureRecord(subject_id=job.job_id, stage="download", message="failed", attempts=1)
    state_entry = StateEntry(key=item.dedupe_key, kind="seen_item", payload={"title": item.title})

    assert candidate.feed_item is item
    assert job.retry_count == 0
    assert rule.enabled is True
    assert rule.episode_mode is RuleEpisodeMode.EPISODE
    assert rule.allow_packs is False
    assert move_plan.should_apply is False
    assert event.metadata == {}
    assert failure.recoverable is True
    assert state_entry.payload["title"] == "Example"
