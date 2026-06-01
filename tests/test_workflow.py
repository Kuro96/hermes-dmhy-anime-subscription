import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_dmhy_anime_subscription import cli, register
from hermes_dmhy_anime_subscription import workflow
from hermes_dmhy_anime_subscription.config import load_config
from hermes_dmhy_anime_subscription.models import DownloadJobStatus
from hermes_dmhy_anime_subscription.monitor import OrganizerInput, TorrentSnapshot
from hermes_dmhy_anime_subscription.organizer import OrganizerAction, OrganizerResult
from hermes_dmhy_anime_subscription.qbittorrent import (
    QbittorrentSubmitResult,
    QbittorrentTorrent,
    plan_qbittorrent_submission,
)
from hermes_dmhy_anime_subscription.state import SubscriptionState
from hermes_dmhy_anime_subscription.workflow import (
    WorkflowDependencies,
    ensure_apply_safe,
    list_state,
    monitor_once,
    organize_once,
    plan_completed_dry_run,
    production_tick,
    snapshots_from_qbittorrent_torrents,
    retry_failed_item,
    run_once,
    scheduler_tick,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_RSS = REPO_ROOT / "fixtures" / "dmhy" / "rss-anime.xml"
VALID_CONFIG = REPO_ROOT / "fixtures" / "config" / "valid.example.json"


class FakeQbittorrentClient:
    def __init__(self):
        self.submissions = []

    def submit(self, candidate, *, rule=None, dry_run=False):
        self.submissions.append((candidate, rule, dry_run))
        plan = plan_qbittorrent_submission(
            candidate, load_config(VALID_CONFIG).qbittorrent, rule=rule, dry_run=dry_run
        )
        return QbittorrentSubmitResult(
            success=True,
            status="planned" if dry_run else "submitted",
            message="fake planned submission" if dry_run else "fake submitted torrent",
            plan=plan,
            dry_run=dry_run,
        )


class FakeProductionQbittorrentClient(FakeQbittorrentClient):
    def __init__(self, torrents):
        super().__init__()
        self.torrents = tuple(torrents)
        self.list_calls = []

    def list_torrents(self, *, category=None, all_categories=False):
        self.list_calls.append((category, all_categories))
        return self.torrents


class FailingProductionQbittorrentClient(FakeQbittorrentClient):
    def __init__(self, message):
        super().__init__()
        self.message = message
        self.list_calls = []

    def list_torrents(self, *, category=None, all_categories=False):
        self.list_calls.append((category, all_categories))
        raise RuntimeError(self.message)


class SequenceQbittorrentClient:
    def __init__(self, config, statuses):
        self.config = config
        self.statuses = list(statuses)
        self.submissions = []

    def submit(self, candidate, *, rule=None, dry_run=False):
        self.submissions.append((candidate, rule, dry_run))
        success, status, message, retryable = self.statuses.pop(0)
        plan = plan_qbittorrent_submission(
            candidate, self.config.qbittorrent, rule=rule, dry_run=dry_run
        )
        return QbittorrentSubmitResult(
            success=success,
            status=status,
            message=message,
            plan=plan,
            retryable=retryable,
            dry_run=dry_run,
        )


def test_e2e_dry_run_plans_rss_rules_submit_organizer_and_webhook_without_external_mutation(
    tmp_path,
):
    config_path = _config(tmp_path)
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    fake_qbit = FakeQbittorrentClient()

    run_result = run_once(
        config_path,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert run_result.parsed_items == 1
    assert run_result.planned_submissions == 1
    assert fake_qbit.submissions[0][2] is True
    assert run_result.candidates[0].webhook_results[0].status == "planned"

    monitor_result = plan_completed_dry_run(
        config_path,
        run_result,
        str(source),
    )

    assert len(monitor_result.organizer_results) == 1
    action = monitor_result.organizer_results[0].actions[0]
    assert action.status == "planned"
    assert source.exists()
    assert action.destination_path is not None
    assert not action.destination_path.exists()
    assert action.destination_path.resolve(strict=False).is_relative_to(
        (tmp_path / "library").resolve(strict=False)
    )


def test_run_once_dry_run_is_repeatable_and_does_not_create_state(tmp_path):
    config_path = _config(tmp_path)
    state_path = tmp_path / "state.sqlite3"
    fake_qbit = FakeQbittorrentClient()
    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
        qbittorrent_factory=lambda _config: fake_qbit,
    )

    first = run_once(config_path, dependencies=dependencies)
    second = run_once(config_path, dependencies=dependencies)

    assert len(first.candidates) == 1
    assert len(second.candidates) == 1
    assert len(fake_qbit.submissions) == 2
    assert not state_path.exists()


def test_run_once_does_not_mark_unmatched_global_feed_item_seen_before_specialized_feed(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["dmhy"]["feeds"] = [
        {"name": "dmhy-main", "url": "https://example.invalid/main.xml"},
        {"name": "tongari-special", "url": "https://example.invalid/tongari.xml"},
    ]
    raw["subscriptions"]["rules"][0]["feed_names"] = ["tongari-special"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()

    result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="08",
                info_hash="8888888888888888888888888888888888888888",
                guid="tongari-08",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(result.candidates) == 1
    assert len(fake_qbit.submissions) == 1
    assert fake_qbit.submissions[0][0].feed_item.source_feed == "tongari-special"
    assert result.candidates[0].status == DownloadJobStatus.SUBMITTED.value


def test_run_once_dry_run_does_not_migrate_existing_old_schema_state(tmp_path):
    config_path = _config(tmp_path)
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE seen_items (
                dedupe_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        before = _sqlite_schema_objects(connection)

    result = run_once(
        config_path,
        dry_run=True,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: FakeQbittorrentClient(),
        ),
    )

    assert len(result.candidates) == 1
    with sqlite3.connect(state_path) as connection:
        after = _sqlite_schema_objects(connection)
    assert after == before == (("table", "seen_items"),)


def test_run_once_dry_run_supports_old_schema_with_jobs_without_migrating_state(
    tmp_path,
):
    config_path = _config(tmp_path)
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL,
                torrent_hash TEXT,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                organizer_outcome TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        before = _sqlite_schema_objects(connection)

    result = run_once(
        config_path,
        dry_run=True,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: FakeQbittorrentClient(),
        ),
    )

    assert len(result.candidates) == 1
    with sqlite3.connect(state_path) as connection:
        after = _sqlite_schema_objects(connection)
    assert after == before == (("table", "jobs"),)


def test_run_once_dry_run_reads_satisfied_pack_only_state_without_migrating_state(
    tmp_path,
):
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE satisfied_season_packs (
                rule_name TEXT NOT NULL,
                series_key TEXT NOT NULL,
                season INTEGER NOT NULL,
                job_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (rule_name, series_key, season)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO satisfied_season_packs (rule_name, series_key, season, job_id, dedupe_key, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "example-show",
                "example anime",
                1,
                "job-pack",
                "infohash:pack",
                "2026-05-31T00:00:00+00:00",
            ),
        )
        before_schema = _sqlite_schema_objects(connection)
        before_rows = tuple(
            connection.execute("SELECT * FROM satisfied_season_packs").fetchall()
        )
    fake_qbit = FakeQbittorrentClient()

    result = run_once(
        config_path,
        dry_run=True,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="02",
                info_hash="3333333333333333333333333333333333333333",
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 1
    assert result.candidates == ()
    assert fake_qbit.submissions == []
    with sqlite3.connect(state_path) as connection:
        after_schema = _sqlite_schema_objects(connection)
        after_rows = tuple(
            connection.execute("SELECT * FROM satisfied_season_packs").fetchall()
        )
    assert after_schema == before_schema == (("table", "satisfied_season_packs"),)
    assert after_rows == before_rows


def test_run_once_dry_run_suppresses_episode_from_partial_satisfied_pack_state(
    tmp_path,
):
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE satisfied_season_packs (
                rule_name TEXT NOT NULL,
                series_key TEXT NOT NULL,
                season INTEGER NOT NULL,
                PRIMARY KEY (rule_name, series_key, season)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO satisfied_season_packs (rule_name, series_key, season)
            VALUES (?, ?, ?)
            """,
            ("example-show", "example anime", 1),
        )
        before_schema = _sqlite_schema_objects(connection)
        before_rows = tuple(
            connection.execute("SELECT * FROM satisfied_season_packs").fetchall()
        )
    fake_qbit = FakeQbittorrentClient()

    result = run_once(
        config_path,
        dry_run=True,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="02",
                info_hash="3333333333333333333333333333333333333333",
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 1
    assert result.candidates == ()
    assert fake_qbit.submissions == []
    with sqlite3.connect(state_path) as connection:
        after_schema = _sqlite_schema_objects(connection)
        after_rows = tuple(
            connection.execute("SELECT * FROM satisfied_season_packs").fetchall()
        )
    assert after_schema == before_schema == (("table", "satisfied_season_packs"),)
    assert after_rows == before_rows


def test_production_tick_dry_run_does_not_create_or_migrate_configured_state(tmp_path):
    config_path = _config(tmp_path)
    state_path = tmp_path / "state.sqlite3"
    qbit = FakeProductionQbittorrentClient(())
    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
        qbittorrent_factory=lambda _config: qbit,
    )

    missing_result = production_tick(
        config_path, dry_run=True, dependencies=dependencies
    )

    assert len(missing_result.run_result.candidates) == 1
    assert not state_path.exists()
    assert qbit.list_calls == []

    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE seen_items (
                dedupe_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        before = _sqlite_schema_objects(connection)

    existing_result = production_tick(
        config_path, dry_run=True, dependencies=dependencies
    )

    assert len(existing_result.run_result.candidates) == 1
    assert qbit.list_calls == []
    with sqlite3.connect(state_path) as connection:
        after = _sqlite_schema_objects(connection)
    assert after == before == (("table", "seen_items"),)


def test_retryable_qbittorrent_submit_failure_remains_eligible_until_success(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    config = load_config(config_path)
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = SequenceQbittorrentClient(
        config,
        (
            (False, "failed", "temporary qBittorrent timeout", True),
            (True, "submitted", "fake submitted torrent", False),
        ),
    )
    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
        qbittorrent_factory=lambda _config: fake_qbit,
    )

    first = run_once(config_path, dry_run=False, dependencies=dependencies)

    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert not state.has_seen_item(first.candidates[0].dedupe_decision.dedupe_key)
        failed_job = state.get_job(first.candidates[0].job_id)
    assert failed_job is not None
    assert failed_job["status"] == DownloadJobStatus.ERROR.value

    assert first.candidates[0].status == DownloadJobStatus.ERROR.value
    second = run_once(config_path, dry_run=False, dependencies=dependencies)

    assert second.candidates[0].status == DownloadJobStatus.SUBMITTED.value
    assert len(fake_qbit.submissions) == 2
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.has_seen_item(first.candidates[0].dedupe_decision.dedupe_key)
        job = state.get_job(first.candidates[0].job_id)
    assert job is not None
    assert job["status"] == DownloadJobStatus.SUBMITTED.value


def test_retryable_season_pack_submit_failure_does_not_suppress_later_episode(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = SequenceQbittorrentClient(
        config,
        (
            (False, "failed", "temporary qBittorrent timeout", True),
            (True, "submitted", "fake submitted torrent", False),
        ),
    )

    pack_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash="2222222222222222222222222222222222222222",
                guid="season-pack-200002",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    later_episode = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="02",
                info_hash="3333333333333333333333333333333333333333",
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(pack_result.candidates) == 1
    assert pack_result.candidates[0].status == DownloadJobStatus.ERROR.value
    assert len(later_episode.candidates) == 1
    assert later_episode.candidates[0].candidate.feed_item.is_season_pack is False
    assert later_episode.candidates[0].status == DownloadJobStatus.SUBMITTED.value
    assert len(fake_qbit.submissions) == 2
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        pack_job = state.get_job(pack_result.candidates[0].job_id)
        episode_job = state.get_job(later_episode.candidates[0].job_id)
        assert pack_job is not None
        assert pack_job["status"] == DownloadJobStatus.ERROR.value
        assert pack_job["metadata"]["season_pack_satisfaction"] == {
            "rule_name": "example-show",
            "series_key": "example anime",
            "season": 1,
        }
        assert episode_job is not None
        assert episode_job["status"] == DownloadJobStatus.SUBMITTED.value
        assert state.has_seen_item(
            later_episode.candidates[0].dedupe_decision.dedupe_key
        )


def test_retryable_season_pack_submit_failure_is_cleared_after_same_pack_succeeds(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    pack_hash = "2222222222222222222222222222222222222222"
    fake_qbit = SequenceQbittorrentClient(
        config,
        (
            (False, "failed", "temporary qBittorrent timeout", True),
            (True, "submitted", "fake submitted torrent", False),
        ),
    )
    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _season_pack_rss(
            info_hash=pack_hash,
            guid="season-pack-200002",
        ),
        qbittorrent_factory=lambda _config: fake_qbit,
    )

    first = run_once(config_path, dry_run=False, dependencies=dependencies)
    second = run_once(config_path, dry_run=False, dependencies=dependencies)

    assert len(first.candidates) == 1
    assert first.candidates[0].status == DownloadJobStatus.ERROR.value
    assert len(second.candidates) == 1
    assert second.candidates[0].job_id == first.candidates[0].job_id
    assert second.candidates[0].status == DownloadJobStatus.SUBMITTED.value
    assert len(fake_qbit.submissions) == 2
    assert all(
        failure["subject_id"] != first.candidates[0].job_id
        for failure in list_state(config_path).retryable
    )
    retry_result = retry_failed_item(config_path, first.candidates[0].job_id)
    assert retry_result.retried is False
    assert retry_result.message == "Job is not failed"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job(first.candidates[0].job_id)
    assert job is not None
    assert job["status"] == DownloadJobStatus.SUBMITTED.value


def test_retryable_same_feed_season_pack_submit_failure_falls_back_to_episode(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = SequenceQbittorrentClient(
        config,
        (
            (False, "failed", "temporary qBittorrent timeout", True),
            (True, "submitted", "fake submitted torrent", False),
        ),
    )

    result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_and_pack_rss(),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 2
    assert len(result.candidates) == 2
    assert [
        outcome.candidate.feed_item.is_season_pack for outcome in result.candidates
    ] == [True, False]
    assert [outcome.status for outcome in result.candidates] == [
        DownloadJobStatus.ERROR.value,
        DownloadJobStatus.SUBMITTED.value,
    ]
    assert [submission[0].title for submission in fake_qbit.submissions] == [
        "[ExampleSub] Example Anime 季度全集 [1080p]",
        "[ExampleSub] Example Anime - 01 [1080p][CHS]",
    ]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        pack_job = state.get_job(result.candidates[0].job_id)
        episode_job = state.get_job(result.candidates[1].job_id)
        assert pack_job is not None
        assert pack_job["status"] == DownloadJobStatus.ERROR.value
        assert episode_job is not None
        assert episode_job["status"] == DownloadJobStatus.SUBMITTED.value
        assert not state.has_seen_item(result.candidates[0].dedupe_decision.dedupe_key)
        assert state.has_seen_item(result.candidates[1].dedupe_decision.dedupe_key)


def test_terminal_same_feed_season_pack_submit_failure_is_deduped_after_episode_fallback(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = SequenceQbittorrentClient(
        config,
        (
            (False, "failed", "qBittorrent rejected torrent", False),
            (True, "submitted", "fake submitted torrent", False),
            (True, "submitted", "unexpected terminal pack retry", False),
        ),
    )
    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _episode_and_pack_rss(),
        qbittorrent_factory=lambda _config: fake_qbit,
    )

    first = run_once(config_path, dry_run=False, dependencies=dependencies)
    second = run_once(config_path, dry_run=False, dependencies=dependencies)

    assert first.parsed_items == 2
    assert len(first.candidates) == 2
    assert [
        outcome.candidate.feed_item.is_season_pack for outcome in first.candidates
    ] == [True, False]
    assert [outcome.status for outcome in first.candidates] == [
        DownloadJobStatus.FAILED.value,
        DownloadJobStatus.SUBMITTED.value,
    ]
    assert second.parsed_items == 2
    assert second.candidates == ()
    assert [submission[0].title for submission in fake_qbit.submissions] == [
        "[ExampleSub] Example Anime 季度全集 [1080p]",
        "[ExampleSub] Example Anime - 01 [1080p][CHS]",
    ]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        pack_job = state.get_job(first.candidates[0].job_id)
        pack_failure = state.get_failure(first.candidates[0].job_id, "qbittorrent")
        episode_job = state.get_job(first.candidates[1].job_id)
        assert pack_job is not None
        assert pack_job["status"] == DownloadJobStatus.FAILED.value
        assert pack_job["last_error"] == "qBittorrent rejected torrent"
        assert pack_job["metadata"]["season_pack_satisfaction"] == {
            "rule_name": "example-show",
            "series_key": "example anime",
            "season": 1,
        }
        assert pack_failure is not None
        assert pack_failure["message"] == "qBittorrent rejected torrent"
        assert not bool(pack_failure["recoverable"])
        assert episode_job is not None
        assert episode_job["status"] == DownloadJobStatus.SUBMITTED.value
        assert state.has_seen_item(first.candidates[0].dedupe_decision.dedupe_key)
        assert state.has_seen_item(first.candidates[1].dedupe_decision.dedupe_key)
    assert all(
        failure["subject_id"] != first.candidates[0].job_id
        for failure in list_state(config_path).retryable
    )
    retry = retry_failed_item(config_path, first.candidates[0].job_id)
    assert retry.retried is False
    assert retry.message == "Job failure is not retryable"


def test_retryable_same_feed_pack_failure_flushes_fallback_before_unrelated_item(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    raw["subscriptions"]["rules"][0]["include_keywords"] = ["1080p"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = SequenceQbittorrentClient(
        config,
        (
            (False, "failed", "temporary qBittorrent timeout", True),
            (True, "submitted", "fake submitted torrent", False),
            (True, "submitted", "fake submitted torrent", False),
        ),
    )

    result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _same_feed_pack_failure_fallback_order_rss(),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert [outcome.candidate.title for outcome in result.candidates] == [
        "[Show A] 季度全集 [1080p]",
        "[Show A] [01][1080p]",
        "[Show B] [01][1080p]",
    ]
    assert [outcome.status for outcome in result.candidates] == [
        DownloadJobStatus.ERROR.value,
        DownloadJobStatus.SUBMITTED.value,
        DownloadJobStatus.SUBMITTED.value,
    ]
    assert [submission[0].title for submission in fake_qbit.submissions] == [
        "[Show A] 季度全集 [1080p]",
        "[Show A] [01][1080p]",
        "[Show B] [01][1080p]",
    ]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert not state.has_seen_item(
            "infohash:abababababababababababababababababababab"
        )
        assert state.has_seen_item("infohash:cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd")
        assert state.has_seen_item("infohash:efefefefefefefefefefefefefefefefefefefef")


def test_same_feed_pack_failure_marked_seen_by_later_same_group_pack_is_not_retryable(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(config_path)
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    first_pack_hash = "abababababababababababababababababababab"
    second_pack_hash = "bcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbc"
    episode_hash = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
    fake_qbit = SequenceQbittorrentClient(
        config,
        (
            (False, "failed", "temporary qBittorrent timeout", True),
            (True, "submitted", "fake submitted torrent", False),
            (True, "submitted", "unexpected retry", False),
        ),
    )
    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _same_feed_episode_and_two_same_group_packs_rss(
            episode_hash=episode_hash,
            first_pack_hash=first_pack_hash,
            second_pack_hash=second_pack_hash,
        ),
        qbittorrent_factory=lambda _config: fake_qbit,
    )

    result = run_once(config_path, dry_run=False, dependencies=dependencies)
    retry_result = run_once(config_path, dry_run=False, dependencies=dependencies)

    assert result.parsed_items == 3
    assert len(result.candidates) == 2
    assert [outcome.candidate.feed_item.info_hash for outcome in result.candidates] == [
        first_pack_hash,
        second_pack_hash,
    ]
    assert [outcome.status for outcome in result.candidates] == [
        DownloadJobStatus.ERROR.value,
        DownloadJobStatus.SUBMITTED.value,
    ]
    assert retry_result.parsed_items == 3
    assert retry_result.candidates == ()
    assert [
        submission[0].feed_item.info_hash for submission in fake_qbit.submissions
    ] == [first_pack_hash, second_pack_hash]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        first_pack_job = state.get_job(result.candidates[0].job_id)
        second_pack_job = state.get_job(result.candidates[1].job_id)
        assert first_pack_job is not None
        assert first_pack_job["status"] == DownloadJobStatus.ERROR.value
        assert second_pack_job is not None
        assert second_pack_job["status"] == DownloadJobStatus.SUBMITTED.value
        assert state.has_seen_item(f"infohash:{first_pack_hash}")
        assert state.has_seen_item(f"infohash:{second_pack_hash}")
        assert not state.has_seen_item(f"infohash:{episode_hash}")
    assert all(
        failure["subject_id"] != result.candidates[0].job_id
        for failure in list_state(config_path).retryable
    )
    retry = retry_failed_item(config_path, result.candidates[0].job_id)
    assert retry.retried is False
    assert retry.message == "Job dedupe key is already suppressed by a successful pack"


def test_new_later_same_group_replacement_pack_remains_eligible_across_runs(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    first_pack_hash = "abababababababababababababababababababab"
    second_pack_hash = "bcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbc"
    fake_qbit = FakeQbittorrentClient()

    first = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash=first_pack_hash,
                guid="season-pack-200051",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    second = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash=second_pack_hash,
                guid="season-pack-200052",
                title="[ExampleSub] Example Anime 季度全集 v2 [1080p]",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(first.candidates) == 1
    assert len(second.candidates) == 1
    assert first.candidates[0].candidate.feed_item.info_hash == first_pack_hash
    assert second.candidates[0].candidate.feed_item.info_hash == second_pack_hash
    assert [
        submission[0].feed_item.info_hash for submission in fake_qbit.submissions
    ] == [first_pack_hash, second_pack_hash]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.has_seen_item(f"infohash:{first_pack_hash}")
        assert state.has_seen_item(f"infohash:{second_pack_hash}")


def test_successful_same_feed_pack_suppresses_later_same_group_pack_across_runs(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    first_pack_hash = "abababababababababababababababababababab"
    second_pack_hash = "bcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbc"
    episode_hash = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
    fake_qbit = FakeQbittorrentClient()
    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _same_feed_episode_and_two_same_group_packs_rss(
            episode_hash=episode_hash,
            first_pack_hash=first_pack_hash,
            second_pack_hash=second_pack_hash,
        ),
        qbittorrent_factory=lambda _config: fake_qbit,
    )

    first = run_once(
        config_path,
        dry_run=False,
        dependencies=dependencies,
    )
    second = run_once(config_path, dry_run=False, dependencies=dependencies)

    assert first.parsed_items == 3
    assert len(first.candidates) == 1
    assert first.candidates[0].candidate.feed_item.info_hash == first_pack_hash
    assert second.parsed_items == 3
    assert second.candidates == ()
    assert [
        submission[0].feed_item.info_hash for submission in fake_qbit.submissions
    ] == [first_pack_hash]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.has_seen_item(f"infohash:{first_pack_hash}")
        assert state.has_seen_item(f"infohash:{second_pack_hash}")
        assert not state.has_seen_item(f"infohash:{episode_hash}")


def test_run_once_apply_active_pack_suppresses_later_episode_until_completion(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()

    pack_dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _episode_and_pack_rss(),
        qbittorrent_factory=lambda _config: fake_qbit,
    )
    pre_completion_episode_dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _episode_rss(
            episode="02",
            info_hash="3333333333333333333333333333333333333333",
            guid="episode-200003",
        ),
        qbittorrent_factory=lambda _config: fake_qbit,
    )
    post_completion_episode_dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _episode_rss(
            episode="03",
            info_hash="5555555555555555555555555555555555555555",
            guid="episode-200005",
        ),
        qbittorrent_factory=lambda _config: fake_qbit,
    )
    pack_v2_dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _season_pack_rss(
            info_hash="4444444444444444444444444444444444444444",
            guid="season-pack-200004",
        ),
        qbittorrent_factory=lambda _config: fake_qbit,
    )
    result = run_once(config_path, dry_run=False, dependencies=pack_dependencies)
    pre_completion_episode = run_once(
        config_path, dry_run=False, dependencies=pre_completion_episode_dependencies
    )
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.list_satisfied_season_packs() == ()
    monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="2222222222222222222222222222222222222222",
                name="[ExampleSub] Example Anime 季度全集 [1080p]",
                state="uploading",
                progress=1.0,
            ),
        ),
        dry_run=False,
        organize=False,
    )
    post_completion_episode = run_once(
        config_path, dry_run=False, dependencies=post_completion_episode_dependencies
    )
    later_pack = run_once(config_path, dry_run=False, dependencies=pack_v2_dependencies)

    assert result.parsed_items == 2
    assert len(result.candidates) == 1
    assert pre_completion_episode.parsed_items == 1
    assert len(pre_completion_episode.candidates) == 0
    assert post_completion_episode.parsed_items == 1
    assert len(post_completion_episode.candidates) == 0
    assert later_pack.parsed_items == 1
    assert len(later_pack.candidates) == 1
    assert result.candidates[0].candidate.feed_item.is_season_pack is True
    assert later_pack.candidates[0].candidate.feed_item.is_season_pack is True
    assert len(fake_qbit.submissions) == 2
    assert (
        fake_qbit.submissions[0][0].title
        == "[ExampleSub] Example Anime 季度全集 [1080p]"
    )
    assert (
        fake_qbit.submissions[1][0].title
        == "[ExampleSub] Example Anime 季度全集 [1080p]"
    )
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.has_seen_item("infohash:2222222222222222222222222222222222222222")
        assert not state.has_seen_item(
            "infohash:1111111111111111111111111111111111111111"
        )
        assert not state.has_seen_item(
            "infohash:3333333333333333333333333333333333333333"
        )
        assert not state.has_seen_item(
            "infohash:5555555555555555555555555555555555555555"
        )
        assert state.has_seen_item("infohash:4444444444444444444444444444444444444444")
        assert state.list_satisfied_season_packs() == (
            ("example-show", "example anime", 1),
        )
        assert {job["torrent_hash"] for job in state.list_jobs()} == {
            "2222222222222222222222222222222222222222",
            "4444444444444444444444444444444444444444",
        }


def test_run_once_dry_run_active_pack_suppresses_later_episode_without_mutating_state(
    tmp_path,
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    state_path = tmp_path / "state.sqlite3"
    fake_qbit = FakeQbittorrentClient()
    episode_hash = "3333333333333333333333333333333333333333"

    with SubscriptionState(state_path) as state:
        state.upsert_job(
            "job-active-season-pack",
            dedupe_key="infohash:2222222222222222222222222222222222222222",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="2222222222222222222222222222222222222222",
            metadata={
                "title": "[ExampleSub] Example Anime 季度全集 [1080p]",
                "rule_name": "example-show",
                "season_pack_satisfaction": {
                    "rule_name": "example-show",
                    "series_key": "example anime",
                    "season": 1,
                },
            },
        )
    with sqlite3.connect(state_path) as connection:
        before_schema = _sqlite_schema_objects(connection)

    result = run_once(
        config_path,
        dry_run=True,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="02",
                info_hash=episode_hash,
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 1
    assert result.candidates == ()
    assert fake_qbit.submissions == []
    with sqlite3.connect(state_path) as connection:
        after_schema = _sqlite_schema_objects(connection)
    assert after_schema == before_schema
    with SubscriptionState(state_path) as state:
        job = state.get_job("job-active-season-pack")
        assert job is not None
        assert job["status"] == DownloadJobStatus.SUBMITTED.value
        assert state.list_satisfied_season_packs() == ()
        assert not state.has_seen_item(f"infohash:{episode_hash}")


def test_run_once_pack_suppressed_match_dedupes_later_same_infohash_item(
    tmp_path,
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    first_rule = raw["subscriptions"]["rules"][0]
    first_rule["episode_mode"] = "both"
    second_rule = dict(first_rule)
    second_rule["name"] = "other-show"
    second_rule["include_keywords"] = ["Other Anime", "1080p"]
    second_rule["episode_mode"] = "episode"
    raw["subscriptions"]["rules"].append(second_rule)
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    duplicate_hash = "3333333333333333333333333333333333333333"

    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.record_satisfied_season_pack(
            "example-show",
            "example anime",
            1,
            job_id="job-completed-pack",
            dedupe_key="infohash:2222222222222222222222222222222222222222",
        )

    result = run_once(
        config_path,
        dry_run=True,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _same_infohash_pack_suppressed_then_other_rss(
                duplicate_hash
            ),
        ),
    )

    assert result.parsed_items == 2
    assert result.candidates == ()


def test_run_once_completed_numbered_sequel_pack_does_not_suppress_base_series_episode(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()
    pack_title = "[ExampleSub] Example Anime 2 季度全集 [1080p]"

    pack_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash="6666666666666666666666666666666666666666",
                guid="season-pack-200006",
                title=pack_title,
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="6666666666666666666666666666666666666666",
                name=pack_title,
                state="uploading",
                progress=1.0,
            ),
        ),
        dry_run=False,
        organize=False,
    )
    episode_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="01",
                info_hash="7777777777777777777777777777777777777777",
                guid="episode-200007",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(pack_result.candidates) == 1
    assert pack_result.candidates[0].candidate.feed_item.is_season_pack is True
    assert episode_result.parsed_items == 1
    assert len(episode_result.candidates) == 1
    assert episode_result.candidates[0].candidate.feed_item.is_season_pack is False
    assert len(fake_qbit.submissions) == 2
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.list_satisfied_season_packs() == (
            ("example-show", "example anime 2", 1),
        )
        assert state.has_seen_item("infohash:7777777777777777777777777777777777777777")


def test_run_once_completed_numeric_series_pack_suppresses_later_episode(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["include_keywords"] = ["86", "1080p"]
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()
    pack_title = "[Subs] 86 季度全集 [1080p]"

    pack_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash="8686868686868686868686868686868686868686",
                guid="season-pack-86",
                title=pack_title,
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="8686868686868686868686868686868686868686",
                name=pack_title,
                state="uploading",
                progress=1.0,
            ),
        ),
        dry_run=False,
        organize=False,
    )
    episode_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="01",
                info_hash="8787878787878787878787878787878787878787",
                guid="episode-86-01",
                title="[Subs] 86 - {episode} [1080p]",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(pack_result.candidates) == 1
    assert pack_result.candidates[0].candidate.feed_item.is_season_pack is True
    assert episode_result.parsed_items == 1
    assert episode_result.candidates == ()
    assert len(fake_qbit.submissions) == 1
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.list_satisfied_season_packs() == (("example-show", "86", 1),)
        assert state.has_seen_item("infohash:8686868686868686868686868686868686868686")
        assert not state.has_seen_item(
            "infohash:8787878787878787878787878787878787878787"
        )


def test_run_once_completed_bracketed_numeric_series_pack_suppresses_later_episode(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["include_keywords"] = ["86", "1080p"]
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()
    pack_title = "[86] 季度全集 [1080p]"

    pack_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash="8989898989898989898989898989898989898989",
                guid="season-pack-bracketed-86",
                title=pack_title,
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="8989898989898989898989898989898989898989",
                name=pack_title,
                state="uploading",
                progress=1.0,
            ),
        ),
        dry_run=False,
        organize=False,
    )
    episode_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="01",
                info_hash="9090909090909090909090909090909090909090",
                guid="episode-bracketed-86-01",
                title="[86] - {episode} [1080p]",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(pack_result.candidates) == 1
    assert pack_result.candidates[0].candidate.feed_item.is_season_pack is True
    assert episode_result.parsed_items == 1
    assert episode_result.candidates == ()
    assert len(fake_qbit.submissions) == 1
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.list_satisfied_season_packs() == (("example-show", "86", 1),)
        assert state.has_seen_item("infohash:8989898989898989898989898989898989898989")
        assert not state.has_seen_item(
            "infohash:9090909090909090909090909090909090909090"
        )


def test_run_once_numbered_sequel_range_suppresses_same_numbered_sequel_episode_in_feed_and_after_completion(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()
    sequel_episode_title = "[ExampleSub] Example Anime 2 - 01 - 02 [1080p][CHS]"
    sequel_pack_title = "[ExampleSub] Example Anime 2 季度全集 [1080p]"

    in_feed_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _numbered_sequel_episode_and_pack_rss(
                episode_title=sequel_episode_title
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="dddddddddddddddddddddddddddddddddddddddd",
                name=sequel_pack_title,
                state="uploading",
                progress=1.0,
            ),
        ),
        dry_run=False,
        organize=False,
    )
    after_completion_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                title=sequel_episode_title,
                episode="01",
                info_hash="7777777777777777777777777777777777777777",
                guid="episode-200007",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert in_feed_result.parsed_items == 2
    assert len(in_feed_result.candidates) == 1
    assert in_feed_result.candidates[0].candidate.feed_item.is_season_pack is True
    assert after_completion_result.parsed_items == 1
    assert len(after_completion_result.candidates) == 0
    assert len(fake_qbit.submissions) == 1
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.list_satisfied_season_packs() == (
            ("example-show", "example anime 2", 1),
        )
        assert state.has_seen_item("infohash:dddddddddddddddddddddddddddddddddddddddd")
        assert not state.has_seen_item(
            "infohash:7777777777777777777777777777777777777777"
        )


def test_run_once_base_range_suppresses_same_base_range_episode_in_feed_and_after_completion(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()

    pack_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _numbered_sequel_episode_and_pack_rss(
                episode_title="[ExampleSub] Example Anime - 01 - 02 [1080p][CHS]",
                pack_title="[ExampleSub] Example Anime 季度全集 [1080p]",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="dddddddddddddddddddddddddddddddddddddddd",
                name="[ExampleSub] Example Anime 季度全集 [1080p]",
                state="uploading",
                progress=1.0,
            ),
        ),
        dry_run=False,
        organize=False,
    )
    after_completion_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                title="[ExampleSub] Example Anime - 01 - 02 [1080p][CHS]",
                episode="01",
                info_hash="7777777777777777777777777777777777777777",
                guid="episode-200007",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert pack_result.parsed_items == 2
    assert len(pack_result.candidates) == 1
    assert pack_result.candidates[0].candidate.feed_item.is_season_pack is True
    assert after_completion_result.parsed_items == 1
    assert len(after_completion_result.candidates) == 0
    assert len(fake_qbit.submissions) == 1
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.list_satisfied_season_packs() == (
            ("example-show", "example anime", 1),
        )
        assert state.has_seen_item("infohash:dddddddddddddddddddddddddddddddddddddddd")
        assert not state.has_seen_item(
            "infohash:7777777777777777777777777777777777777777"
        )


def test_run_once_completed_pack_with_embedded_episode_range_suppresses_later_episode(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()
    pack_title = "[Subs] Example Anime 01-12合集 [1080p]"

    pack_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash="1212121212121212121212121212121212121212",
                guid="season-pack-range-01-12",
                title=pack_title,
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="1212121212121212121212121212121212121212",
                name=pack_title,
                state="uploading",
                progress=1.0,
            ),
        ),
        dry_run=False,
        organize=False,
    )
    episode_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="01",
                info_hash="1313131313131313131313131313131313131313",
                guid="episode-after-range-pack-01",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(pack_result.candidates) == 1
    assert pack_result.candidates[0].candidate.feed_item.is_season_pack is True
    assert episode_result.parsed_items == 1
    assert episode_result.candidates == ()
    assert len(fake_qbit.submissions) == 1
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.list_satisfied_season_packs() == (
            ("example-show", "example anime", 1),
        )
        assert state.has_seen_item("infohash:1212121212121212121212121212121212121212")
        assert not state.has_seen_item(
            "infohash:1313131313131313131313131313131313131313"
        )


def test_run_once_failed_pack_does_not_suppress_later_episode(tmp_path, monkeypatch):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    raw["retry"]["max_attempts"] = 2
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()

    pack_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash="2222222222222222222222222222222222222222",
                guid="season-pack-200002",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    first_monitor = monitor_once(
        config_path, snapshots=(), dry_run=False, organize=False
    )
    second_monitor = monitor_once(
        config_path, snapshots=(), dry_run=False, organize=False
    )
    later_episode = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="02",
                info_hash="3333333333333333333333333333333333333333",
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(pack_result.candidates) == 1
    assert [event.event_type for event in first_monitor.events] == [
        "download_retry_waiting"
    ]
    assert [event.event_type for event in second_monitor.events] == ["download_failure"]
    assert len(later_episode.candidates) == 1
    assert later_episode.candidates[0].candidate.feed_item.is_season_pack is False
    assert len(fake_qbit.submissions) == 2
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        pack_job = state.get_job(pack_result.candidates[0].job_id)
        assert pack_job is not None
        assert pack_job["status"] == DownloadJobStatus.FAILED.value
        assert state.list_satisfied_season_packs() == ()
        assert state.has_seen_item("infohash:3333333333333333333333333333333333333333")


def test_run_once_episode_only_rule_ignores_persisted_satisfied_pack(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    state_path = tmp_path / "state.sqlite3"
    with SubscriptionState(state_path) as state:
        state.record_satisfied_season_pack(
            "example-show",
            "example anime",
            1,
            job_id="dmhy-pack",
            dedupe_key="infohash:pack",
        )
    fake_qbit = FakeQbittorrentClient()

    result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="02",
                info_hash="3333333333333333333333333333333333333333",
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].candidate.feed_item.is_season_pack is False
    assert len(fake_qbit.submissions) == 1
    with SubscriptionState(state_path) as state:
        assert state.has_seen_item("infohash:3333333333333333333333333333333333333333")


def test_series_key_falls_back_to_bracketed_series_after_release_group():
    assert workflow._series_key("[Show A] [01][1080p]") == "show a"
    assert workflow._series_key("[Show B] 季度全集 [1080p]") == "show b"
    assert workflow._series_key("[ExampleSub] [Show A] [01][1080p]") == "show a"
    assert workflow._series_key("[ExampleSub] [Show B] 季度全集 [1080p]") == "show b"
    assert workflow._series_key("[ExampleSub] 我推的孩子 全集 [1080p]") == "我推的孩子"
    assert (
        workflow._series_key("[ExampleSub] 我推的孩子 第01話 [1080p]") == "我推的孩子"
    )
    assert (
        workflow._series_key("[ExampleSub] 我推的孩子 第01话 [1080p]") == "我推的孩子"
    )
    assert (
        workflow._series_key("[ExampleSub] 我推的孩子 第01集 [1080p]") == "我推的孩子"
    )
    assert (
        workflow._series_key("[ExampleSub] Example Anime - 01 - 02 [1080p][CHS]")
        == "example anime"
    )
    assert (
        workflow._series_key("[ExampleSub] Example Anime 2 - 01 - 02 [1080p][CHS]")
        == "example anime 2"
    )
    assert (
        workflow._series_key("[ExampleSub] Example Anime S02 - 01 - 02 [1080p][CHS]")
        == "example anime"
    )
    assert (
        workflow._series_key(
            "[ExampleSub] Example Anime Season 2 - 01 - 02 [1080p][CHS]"
        )
        == "example anime"
    )
    assert (
        workflow._series_key(
            "[Subs] Example Anime 01-12 合集 [1080p]", strip_bare_numbers=False
        )
        == "example anime"
    )
    assert (
        workflow._series_key(
            "[Subs] Example Anime 01-12合集 [1080p]", strip_bare_numbers=False
        )
        == "example anime"
    )
    assert (
        workflow._series_key(
            "[Subs] Example Anime 2 合集 [1080p]", strip_bare_numbers=False
        )
        == "example anime 2"
    )
    assert (
        workflow._series_key("[Subs] 86 季度全集 [1080p]", strip_bare_numbers=False)
        == "86"
    )
    assert workflow._series_key("[Subs] 86 - 01 [1080p]") == "86"
    assert (
        workflow._series_key("[86] 季度全集 [1080p]", strip_bare_numbers=False) == "86"
    )
    assert workflow._series_key("[86] - 01 [1080p]") == "86"
    assert workflow._series_key("[01][1080p]") == ""


@pytest.mark.parametrize("episode_marker", ["第01話", "第01话", "第01集"])
def test_run_once_satisfied_pack_suppresses_later_cjk_episode_marker(
    tmp_path, monkeypatch, episode_marker
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()

    pack_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _season_pack_rss(
                info_hash="2222222222222222222222222222222222222222",
                guid="season-pack-200002",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="2222222222222222222222222222222222222222",
                name="[ExampleSub] Example Anime 季度全集 [1080p]",
                state="uploading",
                progress=1.0,
            ),
        ),
        dry_run=False,
        organize=False,
    )
    episode_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode=episode_marker,
                info_hash="3333333333333333333333333333333333333333",
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(pack_result.candidates) == 1
    assert pack_result.candidates[0].candidate.feed_item.is_season_pack is True
    assert episode_result.parsed_items == 1
    assert episode_result.candidates == ()
    assert len(fake_qbit.submissions) == 1


def test_run_once_allowed_pack_does_not_suppress_different_bracketed_series_episode(
    tmp_path,
):
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    raw["subscriptions"]["rules"][0]["include_keywords"] = ["1080p"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    fake_qbit = FakeQbittorrentClient()

    result = run_once(
        config_path,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _different_bracketed_show_episode_and_pack_rss(),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 2
    assert len(result.candidates) == 2
    assert [submission[0].title for submission in fake_qbit.submissions] == [
        "[Show A] [01][1080p]",
        "[Show B] 季度全集 [1080p]",
    ]


def test_run_once_allowed_numbered_sequel_pack_does_not_suppress_base_series_episode(
    tmp_path,
):
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    fake_qbit = FakeQbittorrentClient()

    result = run_once(
        config_path,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _numbered_sequel_episode_and_pack_rss(),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 2
    assert len(result.candidates) == 2
    assert [submission[0].title for submission in fake_qbit.submissions] == [
        "[ExampleSub] Example Anime - 01 [1080p][CHS]",
        "[ExampleSub] Example Anime 2 季度全集 [1080p]",
    ]


def test_run_once_dry_run_allowed_pack_does_not_satisfy_later_episode(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["allow_packs"] = True
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    state_path = tmp_path / "state.sqlite3"
    fake_qbit = FakeQbittorrentClient()

    dry_result = run_once(
        config_path,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_and_pack_rss(),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    assert not state_path.exists()

    apply_result = run_once(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="02",
                info_hash="3333333333333333333333333333333333333333",
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert len(dry_result.candidates) == 1
    assert dry_result.candidates[0].candidate.feed_item.is_season_pack is True
    assert len(apply_result.candidates) == 1
    assert apply_result.candidates[0].candidate.feed_item.is_season_pack is False
    assert len(fake_qbit.submissions) == 2
    with SubscriptionState(state_path) as state:
        assert state.list_satisfied_season_packs() == ()
        assert state.has_seen_item("infohash:3333333333333333333333333333333333333333")


def test_run_once_dry_run_reads_satisfied_pack_suppression_without_mutating_state(
    tmp_path,
):
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["allow_packs"] = True
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    state_path = tmp_path / "state.sqlite3"
    fake_qbit = FakeQbittorrentClient()
    with SubscriptionState(state_path) as state:
        assert state.record_satisfied_season_pack(
            "example-show",
            "example anime",
            1,
            job_id="dmhy-season-pack",
            dedupe_key="infohash:2222222222222222222222222222222222222222",
        )
        before = state.list_satisfied_season_packs()

    result = run_once(
        config_path,
        dry_run=True,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="03",
                info_hash="5555555555555555555555555555555555555555",
                guid="episode-200005",
            ),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 1
    assert result.candidates == ()
    assert fake_qbit.submissions == []
    with SubscriptionState(state_path) as state:
        assert state.list_satisfied_season_packs() == before
        assert not state.has_seen_item(
            "infohash:5555555555555555555555555555555555555555"
        )


def test_run_once_episode_only_rule_keeps_episode_when_pack_is_present(tmp_path):
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    fake_qbit = FakeQbittorrentClient()

    result = run_once(
        config_path,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_and_pack_rss(),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.parsed_items == 2
    assert len(result.candidates) == 1
    assert result.candidates[0].candidate.feed_item.is_season_pack is False
    assert (
        fake_qbit.submissions[0][0].title
        == "[ExampleSub] Example Anime - 01 [1080p][CHS]"
    )


def test_cli_commands_cover_validate_run_monitor_state_failures_and_retry(
    tmp_path, capsys
):
    config_path = _config(tmp_path)
    completed_source = (
        tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    )
    completed_source.parent.mkdir()
    completed_source.write_bytes(b"video")

    assert cli.main(["validate-config", "--config", str(config_path)]) == 0
    assert (
        cli.main(
            [
                "run-once",
                "--config",
                str(config_path),
                "--feed-file",
                str(FIXTURE_RSS),
                "--completed-source-path",
                str(completed_source),
            ]
        )
        == 0
    )

    assert completed_source.exists()

    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-retry",
            dedupe_key="infohash:retry",
            status=DownloadJobStatus.FAILED,
            torrent_hash="RETRY",
            retry_count=2,
            last_error="temporary",
        )
        state.record_failure(
            "job-retry", "download", "temporary", attempts=2, recoverable=True
        )

    snapshot_json = tmp_path / "snapshots.json"
    snapshot_json.write_text("[]", encoding="utf-8")
    assert (
        cli.main(
            [
                "monitor-once",
                "--config",
                str(config_path),
                "--snapshot-json",
                str(snapshot_json),
            ]
        )
        == 0
    )
    assert cli.main(["state", "--config", str(config_path)]) == 0
    assert cli.main(["failures", "--config", str(config_path)]) == 0
    assert (
        cli.main(
            ["retry-failed", "--config", str(config_path), "--job-id", "job-retry"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "valid config" in output
    assert "run once: dry_run=True" in output
    assert "planned qBittorrent submit:" in output
    assert "planned organizer:" in output
    assert "destination=" in output
    assert "planned webhook:" in output
    assert "event_type=download_planned" in output
    assert "event_type=download_completed" in output
    assert '"archived_rules"' in output
    assert "retryable" in output
    assert "Job reset to pending" in output


def test_webhook_only_failures_are_visible_without_being_retryable(tmp_path, capsys):
    config_path = _config(tmp_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.record_failure(
            "webhook:download-completed",
            "webhook",
            "webhook-only failure",
            attempts=1,
            recoverable=True,
        )

    summary = list_state(config_path)

    assert summary.failed == ()
    assert summary.retryable == ()
    assert len(summary.all_failures) == 1
    assert summary.all_failures[0]["stage"] == "webhook"
    assert summary.all_failures[0]["message"] == "webhook-only failure"

    assert cli.main(["failures", "--config", str(config_path)]) == 0
    failure_output = json.loads(capsys.readouterr().out)
    assert failure_output["failed"] == []
    assert failure_output["retryable"] == []
    assert failure_output["all_failures"][0]["message"] == "webhook-only failure"

    ctx = RecordingContext()
    register(ctx)
    plugin_output = ctx.tools["dmhy.list_failures"](str(config_path))
    assert plugin_output["failed"] == []
    assert plugin_output["retryable"] == []
    assert plugin_output["all_failures"][0]["message"] == "webhook-only failure"


def test_state_summary_all_failures_keeps_constructor_default():
    summary = workflow.StateSummary((), (), (), ())

    assert summary.all_failures == ()
    assert summary.archived_rules == ()


def test_state_summary_fifth_positional_arg_is_archived_rules():
    archived_rules = ({"rule_name": "example-show"},)

    summary = workflow.StateSummary((), (), (), (), archived_rules)

    assert summary.archived_rules == archived_rules
    assert summary.all_failures == ()


def test_snapshots_match_base32_jobs_to_hex_qbittorrent_hash_and_strip_mkv_title(
    tmp_path,
):
    config_path = _config(tmp_path)
    config = load_config(config_path)
    base32_hash = "mo4larsax36neawjar5cb2jf4flt7jpj"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "dmhy-base32",
            dedupe_key=f"infohash:{base32_hash}",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash=base32_hash,
        )

    snapshots = snapshots_from_qbittorrent_torrents(
        config,
        (
            QbittorrentTorrent(
                torrent_hash="63b8b04640befcd202c9047a20e925e1573fa5e9",
                name="[Nekomoe kissaten&LoliHouse] LIAR GAME - 07 [1080p].mkv",
                state="uploading",
                progress=1.0,
                save_path=str(tmp_path / "downloads"),
                content_path=str(
                    tmp_path
                    / "downloads"
                    / "[Nekomoe kissaten&LoliHouse] LIAR GAME - 07 [1080p].mkv"
                ),
                completion_on=1,
            ),
        ),
    )

    assert len(snapshots) == 1
    assert snapshots[0].torrent_hash == base32_hash
    assert (
        snapshots[0].metadata["qbittorrent_hash"]
        == "63b8b04640befcd202c9047a20e925e1573fa5e9"
    )
    assert snapshots[0].name == "[Nekomoe kissaten&LoliHouse] LIAR GAME - 07 [1080p]"


def test_cli_monitor_once_dry_run_previews_configured_state_without_mutating_it(
    tmp_path, capsys
):
    config_path = _config(tmp_path)
    source = tmp_path / "downloads" / "Example Anime - 01.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-monitor-dry-run",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={"title": "Example Anime - 01"},
        )
    snapshot_json = tmp_path / "snapshots.json"
    snapshot_json.write_text(
        json.dumps(
            [
                {
                    "torrent_hash": "abcdef1234567890abcdef1234567890abcdef12",
                    "name": "Example Anime - 01",
                    "state": "uploading",
                    "progress": 1.0,
                    "content_path": str(source),
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "monitor-once",
                "--config",
                str(config_path),
                "--snapshot-json",
                str(snapshot_json),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert (
        "monitor once: dry_run=True updated_events=1 organizer_inputs=1 failures=0"
        in output
    )
    assert "planned organizer: job_id=job-monitor-dry-run status=planned" in output
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-monitor-dry-run")
    assert job is not None
    assert job["status"] == DownloadJobStatus.SUBMITTED.value
    assert job["organizer_outcome"] is None
    assert "organizer_input_created_at" not in job["metadata"]


def test_cli_monitor_once_apply_rejects_unsafe_organizer_config_without_mutation(
    tmp_path, capsys
):
    config_path = _config(tmp_path)
    source = tmp_path / "downloads" / "Example Anime - 01.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-monitor-apply",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={"title": "Example Anime - 01"},
        )
    snapshot_json = tmp_path / "snapshots.json"
    snapshot_json.write_text(
        json.dumps(
            [
                {
                    "torrent_hash": "abcdef1234567890abcdef1234567890abcdef12",
                    "name": "Example Anime - 01",
                    "state": "uploading",
                    "progress": 1.0,
                    "content_path": str(source),
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "monitor-once",
                "--config",
                str(config_path),
                "--snapshot-json",
                str(snapshot_json),
                "--apply",
            ]
        )
        == 2
    )

    assert "apply mode requires" in capsys.readouterr().out
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-monitor-apply")
    assert job is not None
    assert job["status"] == DownloadJobStatus.SUBMITTED.value
    assert job["organizer_outcome"] is None
    assert "organizer_input_created_at" not in job["metadata"]
    assert source.exists()


def test_cli_monitor_once_apply_prints_applied_organizer_label(
    tmp_path, monkeypatch, capsys
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    source = tmp_path / "downloads" / "Example Anime - 01.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-monitor-apply-label",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={"title": "Example Anime - 01"},
        )
    snapshot_json = tmp_path / "snapshots.json"
    snapshot_json.write_text(
        json.dumps(
            [
                {
                    "torrent_hash": "abcdef1234567890abcdef1234567890abcdef12",
                    "name": "Example Anime - 01",
                    "state": "uploading",
                    "progress": 1.0,
                    "content_path": str(source),
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "monitor-once",
                "--config",
                str(config_path),
                "--snapshot-json",
                str(snapshot_json),
                "--apply",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "organizer: job_id=job-monitor-apply-label status=applied" in output
    assert "planned organizer: job_id=job-monitor-apply-label" not in output
    destination = (
        tmp_path
        / "library"
        / "Example Anime"
        / "Season 01"
        / "Example Anime - S01E01 - Unknown [Unknown].mkv"
    )
    assert source.exists()
    assert destination.read_bytes() == b"video"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-monitor-apply-label")
    assert job is not None
    assert job["status"] == DownloadJobStatus.COMPLETED.value
    assert job["organizer_outcome"] == "applied"


def test_snapshots_preserve_qbittorrent_directory_content_path_with_dots(tmp_path):
    config_path = _config(tmp_path)
    config = load_config(config_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "dmhy-abcdef1234567890abcdef1234567890abcdef12",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
        )

    snapshots = snapshots_from_qbittorrent_torrents(
        config,
        (
            QbittorrentTorrent(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name="My.Show.S01.1080p",
                state="uploading",
                progress=1.0,
                save_path=str(tmp_path / "downloads"),
                content_path="My.Show.S01.1080p",
            ),
        ),
    )

    assert len(snapshots) == 1
    assert snapshots[0].name == "My.Show.S01.1080p"


def test_snapshots_ignore_unknown_qbittorrent_completion_timestamp(tmp_path):
    config_path = _config(tmp_path)
    config = load_config(config_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "dmhy-abcdef1234567890abcdef1234567890abcdef12",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
        )

    snapshots = snapshots_from_qbittorrent_torrents(
        config,
        (
            QbittorrentTorrent(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name="Example.mkv",
                state="uploading",
                progress=1.0,
                save_path=str(tmp_path / "downloads"),
                content_path=str(tmp_path / "downloads" / "Example.mkv"),
                completion_on=-1,
            ),
        ),
    )

    assert len(snapshots) == 1
    assert snapshots[0].completed_at is None


def test_snapshots_preserve_missing_qbittorrent_content_path(tmp_path):
    config_path = _config(tmp_path)
    config = load_config(config_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "dmhy-abcdef1234567890abcdef1234567890abcdef12",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
        )

    snapshots = snapshots_from_qbittorrent_torrents(
        config,
        (
            QbittorrentTorrent(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name="Example.mkv",
                state="uploading",
                progress=1.0,
                save_path=str(tmp_path / "downloads"),
                content_path=None,
            ),
        ),
    )

    assert len(snapshots) == 1
    assert snapshots[0].save_path == str(tmp_path / "downloads")
    assert snapshots[0].content_path is None


def test_production_tick_apply_does_not_mark_new_submissions_missing_in_same_tick(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    qbit = FakeProductionQbittorrentClient(
        (
            QbittorrentTorrent(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name=source.name,
                state="uploading",
                progress=1.0,
                save_path=str(source.parent),
                content_path=str(source),
                completion_on=1,
            ),
        )
    )

    result = production_tick(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: qbit,
            organizer_runner=lambda item, config: OrganizerResult(
                item.job_id,
                config.organizer.mode,
                (
                    OrganizerAction(
                        Path(item.source_path),
                        tmp_path / "library" / "planned.mkv",
                        "applied",
                        "video",
                    ),
                ),
            ),
        ),
    )

    assert result.dry_run is False
    assert result.torrent_count == 1
    assert qbit.list_calls == [(None, True)]
    assert len(result.snapshots) == 0
    assert result.monitor_result is not None
    assert len(result.monitor_result.organizer_inputs) == 0
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("dmhy-abcdef1234567890abcdef1234567890abcdef12")
    assert job is not None
    assert job["status"] == DownloadJobStatus.SUBMITTED.value
    assert job["retry_count"] == 0


def test_production_tick_apply_refreshes_completed_pack_state_before_rss_polling(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["episode_mode"] = "both"
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    pack_hash = "2222222222222222222222222222222222222222"
    episode_hash = "3333333333333333333333333333333333333333"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "dmhy-pack",
            dedupe_key=f"infohash:{pack_hash}",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash=pack_hash,
            metadata={
                "title": "[ExampleSub] Example Anime 季度全集 [1080p]",
                "rule_name": "example-show",
                "season_pack_satisfaction": {
                    "rule_name": "example-show",
                    "series_key": "example anime",
                    "season": 1,
                },
                "qbittorrent_category": "anime",
            },
        )
    qbit = FakeProductionQbittorrentClient(
        (
            QbittorrentTorrent(
                torrent_hash=pack_hash,
                name="[ExampleSub] Example Anime 季度全集 [1080p]",
                state="uploading",
                progress=1.0,
                save_path=str(tmp_path / "downloads"),
                content_path=None,
                completion_on=1,
            ),
        )
    )

    result = production_tick(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: _episode_rss(
                episode="02",
                info_hash=episode_hash,
                guid="episode-200003",
            ),
            qbittorrent_factory=lambda _config: qbit,
        ),
    )

    assert result.dry_run is False
    assert result.run_result.parsed_items == 1
    assert result.run_result.candidates == ()
    assert result.torrent_count == 1
    assert len(result.snapshots) == 1
    assert result.monitor_result is not None
    assert len(qbit.submissions) == 0
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.list_satisfied_season_packs() == (
            ("example-show", "example anime", 1),
        )
        assert not state.has_seen_item(f"infohash:{episode_hash}")


def test_production_tick_monitors_preexisting_active_jobs(tmp_path, monkeypatch):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "dmhy-abcdef1234567890abcdef1234567890abcdef12",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={
                "title": "[ExampleSub] Example Anime - 01 [1080p][CHS]",
                "qbittorrent_category": "anime",
            },
        )
    qbit = FakeProductionQbittorrentClient(
        (
            QbittorrentTorrent(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name=source.name,
                state="uploading",
                progress=1.0,
                save_path=str(source.parent),
                content_path=str(source),
                completion_on=1,
            ),
        )
    )

    result = production_tick(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: "<rss><channel></channel></rss>",
            qbittorrent_factory=lambda _config: qbit,
            organizer_runner=lambda item, config: OrganizerResult(
                item.job_id,
                config.organizer.mode,
                (
                    OrganizerAction(
                        Path(item.source_path),
                        tmp_path / "library" / "planned.mkv",
                        "applied",
                        "video",
                    ),
                ),
            ),
        ),
    )

    assert len(result.snapshots) == 1
    assert result.snapshots[0].name == "[ExampleSub] Example Anime - 01 [1080p][CHS]"
    assert result.monitor_result is not None
    assert len(result.monitor_result.organizer_inputs) == 1
    assert result.summary()["monitor"]["organizer_inputs"] == 1


def test_production_tick_does_not_organize_qbittorrent_save_root_without_content_path(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "dmhy-abcdef1234567890abcdef1234567890abcdef12",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={"title": "Example Anime", "qbittorrent_category": "anime"},
        )
    qbit = FakeProductionQbittorrentClient(
        (
            QbittorrentTorrent(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name="Example.mkv",
                state="uploading",
                progress=1.0,
                save_path=str(download_root),
                content_path=None,
                completion_on=1,
            ),
        )
    )
    organizer_calls = []

    result = production_tick(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: "<rss><channel></channel></rss>",
            qbittorrent_factory=lambda _config: qbit,
            organizer_runner=lambda item, config: (
                organizer_calls.append(item)
                or OrganizerResult(item.job_id, config.organizer.mode, ())
            ),
        ),
    )

    assert len(result.snapshots) == 1
    assert result.snapshots[0].content_path is None
    assert result.monitor_result is not None
    assert result.monitor_result.organizer_inputs == ()
    assert organizer_calls == []
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("dmhy-abcdef1234567890abcdef1234567890abcdef12")
    assert job is not None
    assert job["status"] == DownloadJobStatus.COMPLETED.value
    assert job["organizer_outcome"] is None
    assert job["metadata"]["save_path"] == str(download_root)
    assert job["metadata"]["content_path"] is None


def test_production_tick_returns_failure_summary_when_qbittorrent_listing_fails(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    qbit = FailingProductionQbittorrentClient("qBittorrent unavailable")
    organizer_calls = []

    result = production_tick(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: "<rss><channel></channel></rss>",
            qbittorrent_factory=lambda _config: qbit,
            organizer_runner=lambda item, config: (
                organizer_calls.append(item)
                or OrganizerResult(item.job_id, config.organizer.mode, ())
            ),
        ),
    )

    summary = result.summary()
    assert result.ok is False
    assert result.torrent_count == 0
    assert result.snapshots == ()
    assert result.monitor_result is None
    assert organizer_calls == []
    assert qbit.list_calls == [(None, True)]
    assert summary["qbit"]["failure"] == {
        "stage": "list_torrents",
        "message": "qBittorrent unavailable",
        "retryable": True,
    }


def test_monitor_once_production_injects_bangumi_lookup_into_default_organizer(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-monitor",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={"title": "[ExampleSub] Example Anime - 01 [1080p][CHS]"},
        )
    calls = []

    result = monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name="[ExampleSub] Example Anime - 01 [1080p][CHS]",
                state="uploading",
                progress=1.0,
                content_path=str(source),
                completed_at=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
            ),
        ),
        dry_run=False,
        dependencies=WorkflowDependencies(
            bangumi_lookup=lambda title: calls.append(title) or "示例动画"
        ),
    )

    assert calls == ["Example Anime"]
    assert (
        result.organizer_results[0].actions[0].destination_path
        == tmp_path
        / "library"
        / "示例动画"
        / "Example Anime - S01E01 - ExampleSub [1080p].mkv"
    )


def test_plan_completed_dry_run_without_dependency_suppresses_default_bangumi_lookup(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path)
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    fake_qbit = FakeQbittorrentClient()
    run_result = run_once(
        config_path,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )
    monkeypatch.setattr(
        workflow,
        "lookup_chinese_title",
        lambda title: pytest.fail(f"unexpected Bangumi lookup for {title}"),
    )

    result = plan_completed_dry_run(config_path, run_result, str(source))

    assert len(result.organizer_results) == 1
    assert (
        result.organizer_results[0].actions[0].destination_path
        == tmp_path
        / "library"
        / "Example Anime"
        / "Season 01"
        / "Example Anime - S01E01 - ExampleSub [1080p].mkv"
    )


def test_organize_once_dry_run_forces_planning_even_when_config_mode_moves(tmp_path):
    config_path = _config(tmp_path, organizer_mode="move")
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 02 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")

    result = organize_once(
        config_path,
        OrganizerInput(
            "job-organize",
            "HASH",
            "[ExampleSub] Example Anime - 02 [1080p]",
            str(source),
            datetime.now(timezone.utc),
        ),
    )

    assert result.result.actions[0].status == "planned"
    assert source.exists()


def test_apply_mode_refuses_unsafe_config_until_credentials_and_move_are_explicit(
    tmp_path, monkeypatch
):
    dry_config = load_config(_config(tmp_path))
    with pytest.raises(Exception, match="credential|organizer"):
        ensure_apply_safe(dry_config, dry_run=False)

    apply_config = load_config(_config(tmp_path, organizer_mode="move"))
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    ensure_apply_safe(apply_config, dry_run=False)


def test_state_lists_processed_pending_failed_and_retryable_records(tmp_path):
    config_path = _config(tmp_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-done", dedupe_key="infohash:done", status=DownloadJobStatus.COMPLETED
        )
        state.upsert_job(
            "job-pending",
            dedupe_key="infohash:pending",
            status=DownloadJobStatus.PENDING,
        )
        state.upsert_job(
            "job-failed", dedupe_key="infohash:failed", status=DownloadJobStatus.FAILED
        )
        state.record_failure(
            "job-failed", "qbittorrent", "retry later", attempts=1, recoverable=True
        )

    summary = list_state(config_path)

    assert [job["job_id"] for job in summary.processed] == ["job-done"]
    assert [job["job_id"] for job in summary.pending] == ["job-pending"]
    assert [job["job_id"] for job in summary.failed] == ["job-failed"]
    assert [failure["subject_id"] for failure in summary.retryable] == ["job-failed"]
    assert retry_failed_item(config_path, "job-failed").retried is True
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-failed")
    assert job is not None
    assert job["status"] == DownloadJobStatus.PENDING.value


def test_state_api_migrates_minimal_old_jobs_table_without_operational_error(
    tmp_path,
):
    config_path = _config(tmp_path)
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO jobs (job_id, dedupe_key, status) VALUES (?, ?, ?)",
            ("job-old", "infohash:old", DownloadJobStatus.FAILED.value),
        )

    summary = list_state(config_path)
    retry_result = retry_failed_item(config_path, "job-old")

    assert [job["job_id"] for job in summary.failed] == ["job-old"]
    assert retry_result.retried is False
    assert retry_result.message == "Job failure is not retryable"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-old")
    assert job is not None
    assert job["metadata"] == {}
    assert job["retry_count"] == 0


def test_retry_ignores_recoverable_webhook_failure_for_submitted_job(tmp_path):
    config_path = _config(tmp_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-submitted",
            dedupe_key="infohash:submitted",
            status=DownloadJobStatus.SUBMITTED,
        )
        state.record_failure(
            "job-submitted",
            "webhook",
            "webhook timed out",
            attempts=1,
            recoverable=True,
        )

    summary = list_state(config_path)

    assert [job["job_id"] for job in summary.processed] == ["job-submitted"]
    assert summary.retryable == ()
    retry_result = retry_failed_item(config_path, "job-submitted")
    assert retry_result.retried is False
    assert retry_result.message == "Job is not failed"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-submitted")
        failure = state.get_failure("job-submitted", "webhook")
    assert job is not None
    assert job["status"] == DownloadJobStatus.SUBMITTED.value
    assert failure is not None


@pytest.mark.parametrize(
    "status", [DownloadJobStatus.SUBMITTED, DownloadJobStatus.COMPLETED]
)
def test_retry_does_not_reset_processed_job_with_stale_recoverable_qbittorrent_failure(
    tmp_path, status
):
    config_path = _config(tmp_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-processed", dedupe_key="infohash:processed", status=status
        )
        state.record_failure(
            "job-processed", "qbittorrent", "old timeout", attempts=1, recoverable=True
        )

    retry_result = retry_failed_item(config_path, "job-processed")

    assert retry_result.retried is False
    assert retry_result.message == "Job is not failed"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-processed")
    assert job is not None
    assert job["status"] == status.value


def test_retry_terminal_download_failure_is_not_overridden_by_recoverable_webhook_failure(
    tmp_path,
):
    config_path = _config(tmp_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-terminal",
            dedupe_key="infohash:terminal",
            status=DownloadJobStatus.FAILED,
        )
        state.record_failure(
            "job-terminal",
            "download",
            "max attempts exceeded",
            attempts=3,
            recoverable=False,
        )
        state.record_failure(
            "job-terminal", "webhook", "webhook timed out", attempts=1, recoverable=True
        )

    summary = list_state(config_path)
    retry_result = retry_failed_item(config_path, "job-terminal")

    assert all(failure["subject_id"] != "job-terminal" for failure in summary.retryable)
    assert retry_result.retried is False
    assert retry_result.message == "Job failure is not retryable"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-terminal")
    assert job is not None
    assert job["status"] == DownloadJobStatus.FAILED.value


def test_list_state_includes_archived_subscription_rules(tmp_path):
    config_path = _config(tmp_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.archive_rule(
            "example-show", bangumi_subject_id=12345, reason="bangumi_complete"
        )

    summary = list_state(config_path)

    assert [rule["rule_name"] for rule in summary.archived_rules] == ["example-show"]


def test_cli_state_json_includes_archived_subscription_rules(tmp_path, capsys):
    config_path = _config(tmp_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.archive_rule(
            "example-show", bangumi_subject_id=12345, reason="bangumi_complete"
        )

    assert cli.main(["state", "--config", str(config_path)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert [rule["rule_name"] for rule in output["archived_rules"]] == ["example-show"]


def test_scheduler_tick_skips_archived_rules(tmp_path):
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["bangumi_subject_id"] = 12345
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    fake_qbit = FakeQbittorrentClient()
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.archive_rule(
            "example-show", bangumi_subject_id=12345, reason="bangumi_complete"
        )

    result = scheduler_tick(
        config_path,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.candidates == ()
    assert fake_qbit.submissions == []


def test_run_once_dry_run_reads_archived_rules_from_uri_safe_state_path(tmp_path):
    config_path = _config(tmp_path)
    state_path = tmp_path / "state#archive?.sqlite3"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["state"]["path"] = str(state_path)
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    fake_qbit = FakeQbittorrentClient()
    with SubscriptionState(state_path) as state:
        state.archive_rule(
            "example-show", bangumi_subject_id=12345, reason="bangumi_complete"
        )

    result = run_once(
        config_path,
        dry_run=True,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: fake_qbit,
        ),
    )

    assert result.candidates == ()
    assert fake_qbit.submissions == []


def test_monitor_once_dry_run_reads_jobs_from_uri_safe_state_path_without_sibling_db(
    tmp_path,
):
    config_path = _config(tmp_path)
    state_path = tmp_path / "state?round1#monitor.sqlite3"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["state"]["path"] = str(state_path)
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    with SubscriptionState(state_path) as state:
        state.upsert_job(
            "job-uri-safe",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={"title": "[ExampleSub] Example Anime - 01 [1080p][CHS]"},
        )

    result = monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name="[ExampleSub] Example Anime - 01 [1080p][CHS]",
                state="uploading",
                progress=1.0,
                content_path=str(source),
            ),
        ),
        dry_run=True,
    )

    assert [item.job_id for item in result.organizer_inputs] == ["job-uri-safe"]
    assert result.organizer_results[0].actions[0].status == "planned"
    assert source.exists()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("organizer_mode", ["move", "apply"])
def test_monitor_once_dry_run_forces_organizer_planning_and_leaves_state_unchanged(
    tmp_path, organizer_mode
):
    config_path = _config(tmp_path, organizer_mode=organizer_mode)
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-dry-run-monitor",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={"title": "[ExampleSub] Example Anime - 01 [1080p][CHS]"},
        )

    result = monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name="[ExampleSub] Example Anime - 01 [1080p][CHS]",
                state="uploading",
                progress=1.0,
                content_path=str(source),
            ),
        ),
        dry_run=True,
    )

    action = result.organizer_results[0].actions[0]
    assert action.status == "planned"
    assert source.read_bytes() == b"video"
    assert action.destination_path is not None
    assert not action.destination_path.exists()
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-dry-run-monitor")
        assert job is not None
        assert job["status"] == DownloadJobStatus.SUBMITTED.value
        assert job["organizer_outcome"] is None
        assert state.list_organizer_outcomes() == ()


def test_monitor_once_apply_without_organize_does_not_persist_planning_state_and_later_organizes(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    torrent_hash = "abcdef1234567890abcdef1234567890abcdef12"
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    snapshot = TorrentSnapshot(
        torrent_hash=torrent_hash,
        name="[ExampleSub] Example Anime - 01 [1080p][CHS]",
        state="uploading",
        progress=1.0,
        content_path=str(source),
    )
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-monitor-without-organize",
            dedupe_key=f"infohash:{torrent_hash}",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash=torrent_hash,
            metadata={"title": "[ExampleSub] Example Anime - 01 [1080p][CHS]"},
        )

    first = monitor_once(
        config_path,
        snapshots=(snapshot,),
        dry_run=False,
        organize=False,
    )

    assert first.organizer_inputs == ()
    assert first.organizer_results == ()
    assert [event.event_type for event in first.events] == ["download_completed"]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-monitor-without-organize")
        assert job is not None
        assert job["status"] == DownloadJobStatus.SUBMITTED.value
        assert job["organizer_outcome"] is None
        assert job["metadata"]["monitor_status"] == DownloadJobStatus.COMPLETED.value
        assert job["metadata"]["content_path"] == str(source)
        assert "organizer_input_created_at" not in job["metadata"]
        assert state.list_organizer_outcomes() == ()

    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    qbit = FakeProductionQbittorrentClient(
        (
            QbittorrentTorrent(
                torrent_hash=torrent_hash,
                name=source.name,
                state="uploading",
                progress=1.0,
                save_path=str(source.parent),
                content_path=str(source),
                completion_on=1,
            ),
        )
    )

    second = production_tick(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: "<rss><channel></channel></rss>",
            qbittorrent_factory=lambda _config: qbit,
        ),
    )

    destination = (
        tmp_path
        / "library"
        / "Example Anime"
        / "Season 01"
        / "Example Anime - S01E01 - ExampleSub [1080p].mkv"
    )
    assert qbit.list_calls == [(None, True)]
    assert second.monitor_result is not None
    assert [item.job_id for item in second.monitor_result.organizer_inputs] == [
        "job-monitor-without-organize"
    ]
    assert second.monitor_result.organizer_results[0].actions[0].status == "applied"
    assert source.exists()
    assert destination.read_bytes() == b"video"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-monitor-without-organize")
        assert job is not None
        assert job["organizer_outcome"] == "applied"
        assert "organizer_input_created_at" in job["metadata"]


def test_cli_monitor_once_dry_run_plans_without_mutation_and_apply_still_copies(
    tmp_path, monkeypatch, capsys
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = {
        "torrent_hash": "abcdef1234567890abcdef1234567890abcdef12",
        "name": "[ExampleSub] Example Anime - 01 [1080p][CHS]",
        "state": "uploading",
        "progress": 1.0,
        "content_path": str(source),
    }
    snapshot_path.write_text(json.dumps([snapshot]), encoding="utf-8")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-cli-monitor",
            dedupe_key="infohash:abcdef1234567890abcdef1234567890abcdef12",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
            metadata={"title": "[ExampleSub] Example Anime - 01 [1080p][CHS]"},
        )

    assert (
        cli.main(
            [
                "monitor-once",
                "--config",
                str(config_path),
                "--snapshot-json",
                str(snapshot_path),
                "--dry-run",
            ]
        )
        == 0
    )
    capsys.readouterr()

    destination = (
        tmp_path
        / "library"
        / "Example Anime"
        / "Season 01"
        / "Example Anime - S01E01 - ExampleSub [1080p].mkv"
    )
    assert source.read_bytes() == b"video"
    assert not destination.exists()
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-cli-monitor")
        assert job is not None
        assert job["status"] == DownloadJobStatus.SUBMITTED.value
        assert job["organizer_outcome"] is None

    assert (
        cli.main(
            [
                "monitor-once",
                "--config",
                str(config_path),
                "--snapshot-json",
                str(snapshot_path),
                "--apply",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert source.exists()
    assert destination.read_bytes() == b"video"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-cli-monitor")
        assert job is not None
        assert job["status"] == DownloadJobStatus.COMPLETED.value
        assert job["organizer_outcome"] == "applied"


def test_monitor_once_archives_rule_after_bangumi_main_episodes_are_completed_and_organized(
    tmp_path,
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["bangumi_subject_id"] = 12345
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        for episode in (1, 2):
            state.upsert_job(
                f"job-{episode}",
                dedupe_key=f"infohash:{episode}",
                status=DownloadJobStatus.COMPLETED,
                organizer_outcome="applied",
                metadata={"rule_name": "example-show", "episode": episode},
            )

    result = monitor_once(
        config_path,
        snapshots=(),
        dry_run=False,
        organize=False,
        dependencies=WorkflowDependencies(
            bangumi_subject_fetcher=lambda subject_id: workflow.BangumiSubjectEpisodes(
                subject_id, 2, (1, 2)
            )
        ),
    )

    assert [event.event_type for event in result.events] == ["subscription_archived"]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.is_rule_archived("example-show") is True


def test_monitor_once_archives_rule_after_one_job_organizes_multiple_bangumi_episodes(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["bangumi_subject_id"] = 12345
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    torrent_hash = "abcdef1234567890abcdef1234567890abcdef12"
    source = tmp_path / "downloads" / "Example Anime Complete Pack"
    episodes = tuple(range(1, 13))
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-pack",
            dedupe_key=f"infohash:{torrent_hash}",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash=torrent_hash,
            metadata={
                "rule_name": "example-show",
                "title": "[ExampleSub] Example Anime Complete Pack",
            },
        )

    def organize_pack(item, config):
        return OrganizerResult(
            item.job_id,
            config.organizer.mode,
            tuple(
                OrganizerAction(
                    Path(item.source_path) / f"Example Anime - {episode:02d}.mkv",
                    tmp_path / "library" / f"Example Anime - S01E{episode:02d}.mkv",
                    "applied",
                    "video",
                    episode=episode,
                )
                for episode in episodes
            ),
        )

    result = monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash=torrent_hash,
                name="[ExampleSub] Example Anime Complete Pack",
                state="uploading",
                progress=1.0,
                content_path=str(source),
            ),
        ),
        dry_run=False,
        dependencies=WorkflowDependencies(
            bangumi_subject_fetcher=lambda subject_id: workflow.BangumiSubjectEpisodes(
                subject_id, 12, episodes
            ),
            organizer_runner=organize_pack,
        ),
    )

    assert [event.event_type for event in result.events] == [
        "download_completed",
        "subscription_archived",
    ]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-pack")
        assert job is not None
        assert job["metadata"]["episodes"] == list(episodes)
        assert state.is_rule_archived("example-show") is True


def test_monitor_once_does_not_archive_rule_when_pack_has_non_applied_episode_action(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["bangumi_subject_id"] = 12345
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    torrent_hash = "abcdef1234567890abcdef1234567890abcdef12"
    source = tmp_path / "downloads" / "Example Anime Partial Pack"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-pack",
            dedupe_key=f"infohash:{torrent_hash}",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash=torrent_hash,
            metadata={
                "rule_name": "example-show",
                "title": "[ExampleSub] Example Anime Partial Pack",
                "episode": 1,
            },
        )

    def organize_pack(item, config):
        return OrganizerResult(
            item.job_id,
            config.organizer.mode,
            (
                OrganizerAction(
                    Path(item.source_path) / "Example Anime - 01.mkv",
                    tmp_path / "library" / "Example Anime - S01E01.mkv",
                    "conflict",
                    "video",
                    episode=1,
                ),
                OrganizerAction(
                    Path(item.source_path) / "Example Anime - 02.mkv",
                    tmp_path / "library" / "Example Anime - S01E02.mkv",
                    "applied",
                    "video",
                    episode=2,
                ),
            ),
        )

    result = monitor_once(
        config_path,
        snapshots=(
            TorrentSnapshot(
                torrent_hash=torrent_hash,
                name="[ExampleSub] Example Anime Partial Pack",
                state="uploading",
                progress=1.0,
                content_path=str(source),
            ),
        ),
        dry_run=False,
        dependencies=WorkflowDependencies(
            bangumi_subject_fetcher=lambda subject_id: workflow.BangumiSubjectEpisodes(
                subject_id, 2, (1, 2)
            ),
            organizer_runner=organize_pack,
        ),
    )

    assert [event.event_type for event in result.events] == ["download_completed"]
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        job = state.get_job("job-pack")
        assert job is not None
        assert job["metadata"]["episodes"] == [2]
        assert state.is_rule_archived("example-show") is False


@pytest.mark.parametrize("organizer_outcome", [None, "planned"])
def test_monitor_once_does_not_archive_completed_rule_without_applied_organizer_outcome(
    tmp_path, organizer_outcome
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["bangumi_subject_id"] = 12345
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        for episode in (1, 2):
            state.upsert_job(
                f"job-{episode}",
                dedupe_key=f"infohash:{episode}",
                status=DownloadJobStatus.COMPLETED,
                organizer_outcome=organizer_outcome,
                metadata={"rule_name": "example-show", "episode": episode},
            )

    result = monitor_once(
        config_path,
        snapshots=(),
        dry_run=False,
        organize=False,
        dependencies=WorkflowDependencies(
            bangumi_subject_fetcher=lambda subject_id: workflow.BangumiSubjectEpisodes(
                subject_id, 2, (1, 2)
            )
        ),
    )

    assert all(event.event_type != "subscription_archived" for event in result.events)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.is_rule_archived("example-show") is False


def test_monitor_once_does_not_archive_when_bangumi_episode_list_is_incomplete(
    tmp_path,
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["bangumi_subject_id"] = 12345
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-1",
            dedupe_key="infohash:1",
            status=DownloadJobStatus.COMPLETED,
            organizer_outcome="applied",
            metadata={"rule_name": "example-show", "episode": 1},
        )

    result = monitor_once(
        config_path,
        snapshots=(),
        dry_run=False,
        organize=False,
        dependencies=WorkflowDependencies(
            bangumi_subject_fetcher=lambda subject_id: workflow.BangumiSubjectEpisodes(
                subject_id, 2, (1,)
            )
        ),
    )

    assert all(event.event_type != "subscription_archived" for event in result.events)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.is_rule_archived("example-show") is False


def test_monitor_once_dry_run_does_not_persist_subscription_archival(tmp_path):
    config_path = _config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["bangumi_subject_id"] = 12345
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    result = monitor_once(
        config_path,
        snapshots=(),
        dry_run=True,
        organize=False,
        dependencies=WorkflowDependencies(
            bangumi_subject_fetcher=lambda subject_id: workflow.BangumiSubjectEpisodes(
                subject_id, 1, (1,)
            )
        ),
    )

    assert result.events == ()
    assert not (tmp_path / "state.sqlite3").exists()


def test_register_tolerates_partial_hermes_contexts_and_exposes_tools():
    ctx = RecordingContext()

    register(ctx)

    assert "dmhy.validate_config" in ctx.tools
    assert "dmhy.run_once_dry_run" in ctx.tools
    assert "dmhy.schedule_tick" in ctx.hooks
    assert "hermes-dmhy" in ctx.commands

    partial = type(
        "PartialContext",
        (),
        {
            "registered": {},
            "register_tool": lambda self, name, handler: self.registered.setdefault(
                name, handler
            ),
        },
    )()
    register(partial)
    assert "dmhy.list_state" in partial.registered


def test_plugin_list_state_returns_json_serializable_dict(tmp_path):
    config_path = _config(tmp_path)
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-done", dedupe_key="infohash:done", status=DownloadJobStatus.COMPLETED
        )
        state.record_failure(
            "job-done", "webhook", "webhook timed out", attempts=1, recoverable=True
        )
        state.archive_rule(
            "example-show", bangumi_subject_id=12345, reason="bangumi_complete"
        )
    ctx = RecordingContext()
    register(ctx)

    result = ctx.tools["dmhy.list_state"](str(config_path))

    assert result["processed"][0]["job_id"] == "job-done"
    assert result["all_failures"][0]["message"] == "webhook timed out"
    assert result["archived_rules"][0]["rule_name"] == "example-show"
    json.dumps(result)


def test_registered_plugin_monitor_once_accepts_json_snapshot_dicts(tmp_path):
    config_path = _config(tmp_path)
    torrent_hash = "abcdef1234567890abcdef1234567890abcdef12"
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.upsert_job(
            "job-plugin-monitor-json",
            dedupe_key=f"infohash:{torrent_hash}",
            status=DownloadJobStatus.SUBMITTED,
            torrent_hash=torrent_hash,
            metadata={"title": "[ExampleSub] Example Anime - 01 [1080p][CHS]"},
        )
    ctx = RecordingContext()
    register(ctx)

    result = ctx.tools["dmhy.monitor_once"](
        str(config_path),
        snapshots=[
            {
                "torrent_hash": torrent_hash,
                "name": "[ExampleSub] Example Anime - 01 [1080p][CHS]",
                "state": "uploading",
                "progress": 1.0,
                "content_path": str(source),
                "completed_at": "2026-01-02T00:00:00+00:00",
            }
        ],
        dry_run=True,
    )

    assert result["organizer_inputs"][0]["job_id"] == "job-plugin-monitor-json"
    assert result["organizer_inputs"][0]["completed_at"] == "2026-01-02T00:00:00+00:00"
    json.dumps(result)


def test_registered_plugin_organize_once_accepts_json_organizer_input_dict(tmp_path):
    config_path = _config(tmp_path)
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    ctx = RecordingContext()
    register(ctx)

    result = ctx.tools["dmhy.organize_once"](
        str(config_path),
        organizer_input={
            "job_id": "job-plugin-organize-json",
            "torrent_hash": "abcdef1234567890abcdef1234567890abcdef12",
            "title": "[ExampleSub] Example Anime - 01 [1080p][CHS]",
            "source_path": str(source),
            "completed_at": "2026-01-02T00:00:00+00:00",
            "metadata": {"qbittorrent_state": "uploading"},
        },
        dry_run=True,
    )

    assert result["result"]["job_id"] == "job-plugin-organize-json"
    json.dumps(result)


def test_registered_plugin_tool_results_are_json_serializable(tmp_path, monkeypatch):
    dry_root = tmp_path / "dry"
    retry_root = tmp_path / "retry"
    apply_root = tmp_path / "apply"
    dry_root.mkdir()
    retry_root.mkdir()
    apply_root.mkdir()
    dry_config_path = _config(dry_root)
    retry_config_path = _config(retry_root)
    apply_config_path = _config(apply_root, organizer_mode="move")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    with SubscriptionState(retry_root / "state.sqlite3") as state:
        state.upsert_job(
            "job-failed",
            dedupe_key="infohash:failed",
            status=DownloadJobStatus.ERROR,
            torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
        )
        state.record_failure(
            "job-failed", "download", "download stalled", attempts=1, recoverable=True
        )
    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
        qbittorrent_factory=lambda _config: FakeQbittorrentClient(),
    )
    organizer_input = OrganizerInput(
        "job-organize",
        "abcdef1234567890abcdef1234567890abcdef12",
        "[ExampleSub] Example Anime - 01 [1080p][CHS]",
        str(dry_root / "missing-download"),
        datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    ctx = RecordingContext()
    register(ctx)

    calls = (
        ("dmhy.validate_config", (str(dry_config_path),), {}),
        (
            "dmhy.run_once_dry_run",
            (str(dry_config_path),),
            {"dependencies": dependencies},
        ),
        (
            "dmhy.run_once_apply",
            (str(apply_config_path),),
            {"dependencies": dependencies},
        ),
        (
            "dmhy.monitor_once",
            (str(dry_config_path),),
            {"snapshots": (), "dry_run": True, "organize": False},
        ),
        (
            "dmhy.organize_once",
            (str(dry_config_path), organizer_input),
            {"dry_run": True},
        ),
        ("dmhy.list_state", (str(dry_config_path),), {}),
        ("dmhy.list_failures", (str(dry_config_path),), {}),
        ("dmhy.retry_failed_item", (str(retry_config_path), "job-failed"), {}),
    )

    for name, args, kwargs in calls:
        result = ctx.tools[name](*args, **kwargs)
        assert isinstance(result, dict | list | str | int | float | bool | type(None))
        json.dumps(result)


def test_production_tick_lists_all_qbittorrent_torrents_to_avoid_stale_category_misses(
    tmp_path, monkeypatch
):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["category"] = "rule-anime"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    qbit = FakeProductionQbittorrentClient(
        (
            QbittorrentTorrent(
                torrent_hash="abcdef1234567890abcdef1234567890abcdef12",
                name=source.name,
                state="uploading",
                progress=1.0,
                save_path=str(source.parent),
                content_path=str(source),
            ),
        )
    )

    production_tick(
        config_path,
        dry_run=False,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: qbit,
            organizer_runner=lambda item, config: OrganizerResult(
                item.job_id, config.organizer.mode, ()
            ),
        ),
    )

    assert qbit.list_calls == [(None, True)]


def test_cli_schedule_tick_apply_prints_json_summary(tmp_path, monkeypatch, capsys):
    config_path = _config(tmp_path)

    class FakeTickResult:
        ok = True

        def summary(self):
            return {"ok": True, "dry_run": False, "monitor": {"organizer_inputs": 0}}

    calls = []

    def fake_production_tick(config, *, dry_run, dependencies=None):
        calls.append((config, dry_run, dependencies is not None))
        return FakeTickResult()

    monkeypatch.setattr(cli, "production_tick", fake_production_tick)

    assert (
        cli.main(
            [
                "schedule-tick",
                "--config",
                str(config_path),
                "--feed-file",
                str(FIXTURE_RSS),
                "--apply",
            ]
        )
        == 0
    )

    assert calls == [(str(config_path), False, True)]
    output = json.loads(capsys.readouterr().out)
    assert output == {"ok": True, "dry_run": False, "monitor": {"organizer_inputs": 0}}


def test_cli_schedule_tick_apply_exits_nonzero_when_summary_not_ok(
    tmp_path, monkeypatch, capsys
):
    config_path = _config(tmp_path)

    class FakeTickResult:
        ok = False

        def summary(self):
            return {"ok": False, "monitor": {"failures": [{"stage": "monitor"}]}}

    monkeypatch.setattr(
        cli, "production_tick", lambda *args, **kwargs: FakeTickResult()
    )

    assert cli.main(["schedule-tick", "--config", str(config_path), "--apply"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_scheduler_tick_is_bounded_one_shot(tmp_path):
    config_path = _config(tmp_path)

    result = scheduler_tick(
        config_path,
        dependencies=WorkflowDependencies(
            feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"),
            qbittorrent_factory=lambda _config: FakeQbittorrentClient(),
        ),
    )

    assert result.dry_run is True
    assert result.parsed_items == 1


class RecordingContext:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.commands = {}

    def register_tool(self, name, handler):
        self.tools[name] = handler

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_cli_command(self, name, handler):
        self.commands[name] = handler


def _config(tmp_path, organizer_mode="dry-run"):
    raw = json.loads(VALID_CONFIG.read_text(encoding="utf-8"))
    raw["state"]["path"] = str(tmp_path / "state.sqlite3")
    raw["organizer"]["library_root"] = str(tmp_path / "library")
    raw["organizer"]["staging_root"] = str(tmp_path / "staging")
    raw["organizer"]["mode"] = organizer_mode
    raw["qbittorrent"]["save_path"] = str(tmp_path / "downloads")
    raw["subscriptions"]["rules"][0]["include_keywords"] = ["Example Anime", "1080p"]
    path = tmp_path / f"config-{organizer_mode}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _episode_and_pack_rss():
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Anime RSS</title>
    <item>
      <title>[ExampleSub] Example Anime - 01 [1080p][CHS]</title>
      <link>https://share.dmhy.org/topics/view/200001_example_anime_01.html</link>
      <pubDate>Sun, 24 May 2026 10:30:00 +0000</pubDate>
      <description>Example release description</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>episode-200001</guid>
      <enclosure url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111&amp;dn=Episode" type="application/x-bittorrent" />
    </item>
    <item>
      <title>[ExampleSub] Example Anime 季度全集 [1080p]</title>
      <link>https://share.dmhy.org/topics/view/200002_example_anime_batch.html?sort_id=31</link>
      <pubDate>Sun, 24 May 2026 11:00:00 +0000</pubDate>
      <description>Quarterly complete season pack</description>
      <author>ExampleSub</author>
      <category>季度全集</category>
      <guid>season-pack-200002</guid>
      <enclosure url="magnet:?xt=urn:btih:2222222222222222222222222222222222222222&amp;dn=SeasonPack" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
"""


def _different_bracketed_show_episode_and_pack_rss():
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Anime RSS</title>
    <item>
      <title>[Show A] [01][1080p]</title>
      <link>https://share.dmhy.org/topics/view/200011_show_a_01.html</link>
      <pubDate>Sun, 24 May 2026 10:30:00 +0000</pubDate>
      <description>Show A episode release</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>episode-200011</guid>
      <enclosure url="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&amp;dn=ShowA" type="application/x-bittorrent" />
    </item>
    <item>
      <title>[Show B] 季度全集 [1080p]</title>
      <link>https://share.dmhy.org/topics/view/200012_show_b_batch.html?sort_id=31</link>
      <pubDate>Sun, 24 May 2026 11:00:00 +0000</pubDate>
      <description>Show B complete season pack</description>
      <author>ExampleSub</author>
      <category>季度全集</category>
      <guid>season-pack-200012</guid>
      <enclosure url="magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&amp;dn=ShowBPack" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
"""


def _same_feed_pack_failure_fallback_order_rss():
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Anime RSS</title>
    <item>
      <title>[Show A] [01][1080p]</title>
      <link>https://share.dmhy.org/topics/view/200031_show_a_01.html</link>
      <pubDate>Sun, 24 May 2026 10:30:00 +0000</pubDate>
      <description>Show A episode release</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>episode-200031</guid>
      <enclosure url="magnet:?xt=urn:btih:cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd&amp;dn=ShowAEpisode" type="application/x-bittorrent" />
    </item>
    <item>
      <title>[Show A] 季度全集 [1080p]</title>
      <link>https://share.dmhy.org/topics/view/200032_show_a_batch.html?sort_id=31</link>
      <pubDate>Sun, 24 May 2026 11:00:00 +0000</pubDate>
      <description>Show A complete season pack</description>
      <author>ExampleSub</author>
      <category>季度全集</category>
      <guid>season-pack-200032</guid>
      <enclosure url="magnet:?xt=urn:btih:abababababababababababababababababababab&amp;dn=ShowAPack" type="application/x-bittorrent" />
    </item>
    <item>
      <title>[Show B] [01][1080p]</title>
      <link>https://share.dmhy.org/topics/view/200033_show_b_01.html</link>
      <pubDate>Sun, 24 May 2026 11:30:00 +0000</pubDate>
      <description>Show B episode release</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>episode-200033</guid>
      <enclosure url="magnet:?xt=urn:btih:efefefefefefefefefefefefefefefefefefefef&amp;dn=ShowBEpisode" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
"""


def _same_feed_episode_and_two_same_group_packs_rss(
    *, episode_hash, first_pack_hash, second_pack_hash
):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Anime RSS</title>
    <item>
      <title>[ExampleSub] Example Anime - 01 [1080p][CHS]</title>
      <link>https://share.dmhy.org/topics/view/200041_example_anime_01.html</link>
      <pubDate>Sun, 24 May 2026 10:30:00 +0000</pubDate>
      <description>Example Anime episode release</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>episode-200041</guid>
      <enclosure url="magnet:?xt=urn:btih:{episode_hash}&amp;dn=ExampleAnimeEpisode" type="application/x-bittorrent" />
    </item>
    <item>
      <title>[ExampleSub] Example Anime 季度全集 [1080p]</title>
      <link>https://share.dmhy.org/topics/view/200042_example_anime_batch.html?sort_id=31</link>
      <pubDate>Sun, 24 May 2026 11:00:00 +0000</pubDate>
      <description>Example Anime complete season pack</description>
      <author>ExampleSub</author>
      <category>季度全集</category>
      <guid>season-pack-200042</guid>
      <enclosure url="magnet:?xt=urn:btih:{first_pack_hash}&amp;dn=ExampleAnimePack" type="application/x-bittorrent" />
    </item>
    <item>
      <title>[ExampleSub] Example Anime 季度全集 [1080p]</title>
      <link>https://share.dmhy.org/topics/view/200043_example_anime_batch_v2.html?sort_id=31</link>
      <pubDate>Sun, 24 May 2026 11:10:00 +0000</pubDate>
      <description>Example Anime complete season pack mirror</description>
      <author>ExampleSub</author>
      <category>季度全集</category>
      <guid>season-pack-200043</guid>
      <enclosure url="magnet:?xt=urn:btih:{second_pack_hash}&amp;dn=ExampleAnimePackMirror" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
"""


def _numbered_sequel_episode_and_pack_rss(
    *,
    episode_title="[ExampleSub] Example Anime - 01 [1080p][CHS]",
    pack_title="[ExampleSub] Example Anime 2 季度全集 [1080p]",
):
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Anime RSS</title>
    <item>
      <title>{episode_title}</title>
      <link>https://share.dmhy.org/topics/view/200021_example_anime_01.html</link>
      <pubDate>Sun, 24 May 2026 10:30:00 +0000</pubDate>
      <description>Example release description</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>episode-200021</guid>
      <enclosure url="magnet:?xt=urn:btih:cccccccccccccccccccccccccccccccccccccccc&amp;dn=Episode" type="application/x-bittorrent" />
    </item>
    <item>
      <title>{pack_title}</title>
      <link>https://share.dmhy.org/topics/view/200022_example_anime_2_batch.html?sort_id=31</link>
      <pubDate>Sun, 24 May 2026 11:00:00 +0000</pubDate>
      <description>Quarterly complete sequel season pack</description>
      <author>ExampleSub</author>
      <category>季度全集</category>
      <guid>season-pack-200022</guid>
      <enclosure url="magnet:?xt=urn:btih:dddddddddddddddddddddddddddddddddddddddd&amp;dn=SeasonPack" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
""".format(episode_title=episode_title, pack_title=pack_title)


def _same_infohash_pack_suppressed_then_other_rss(info_hash):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Anime RSS</title>
    <item>
      <title>[ExampleSub] Example Anime - 02 [1080p][CHS]</title>
      <link>https://share.dmhy.org/topics/view/200051_example_anime_02.html</link>
      <pubDate>Mon, 25 May 2026 10:30:00 +0000</pubDate>
      <description>Example release description</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>episode-200051</guid>
      <enclosure url="magnet:?xt=urn:btih:{info_hash}&amp;dn=ExampleEpisode" type="application/x-bittorrent" />
    </item>
    <item>
      <title>[ExampleSub] Other Anime - 01 [1080p][CHS]</title>
      <link>https://share.dmhy.org/topics/view/200052_other_anime_01.html</link>
      <pubDate>Mon, 25 May 2026 10:35:00 +0000</pubDate>
      <description>Other release description</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>episode-200052</guid>
      <enclosure url="magnet:?xt=urn:btih:{info_hash}&amp;dn=OtherEpisode" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
"""


def _episode_rss(
    *,
    episode,
    info_hash,
    guid,
    title="[ExampleSub] Example Anime - {episode} [1080p][CHS]",
):
    resolved_title = title.format(episode=episode)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Anime RSS</title>
    <item>
      <title>{resolved_title}</title>
      <link>https://share.dmhy.org/topics/view/200003_example_anime_{episode}.html</link>
      <pubDate>Mon, 25 May 2026 10:30:00 +0000</pubDate>
      <description>Example release description</description>
      <author>ExampleSub</author>
      <category>動畫</category>
      <guid>{guid}</guid>
      <enclosure url="magnet:?xt=urn:btih:{info_hash}&amp;dn=Episode" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
"""


def _season_pack_rss(
    *, info_hash, guid, title="[ExampleSub] Example Anime 季度全集 [1080p]"
):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Anime RSS</title>
    <item>
      <title>{title}</title>
      <link>https://share.dmhy.org/topics/view/200004_example_anime_batch.html?sort_id=31</link>
      <pubDate>Mon, 25 May 2026 11:00:00 +0000</pubDate>
      <description>Quarterly complete season pack</description>
      <author>ExampleSub</author>
      <category>季度全集</category>
      <guid>{guid}</guid>
      <enclosure url="magnet:?xt=urn:btih:{info_hash}&amp;dn=SeasonPack" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
"""


def _sqlite_schema_objects(connection):
    cursor = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    )
    return tuple((str(row[0]), str(row[1])) for row in cursor.fetchall())
