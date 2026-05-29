import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_dmhy_anime_subscription import cli, register
from hermes_dmhy_anime_subscription import workflow
from hermes_dmhy_anime_subscription.config import load_config
from hermes_dmhy_anime_subscription.models import DownloadJobStatus, OrganizerMode
from hermes_dmhy_anime_subscription.monitor import OrganizerInput, TorrentSnapshot
from hermes_dmhy_anime_subscription.organizer import OrganizerAction, OrganizerResult
from hermes_dmhy_anime_subscription.qbittorrent import QbittorrentSubmitResult, QbittorrentTorrent, plan_qbittorrent_submission
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
        plan = plan_qbittorrent_submission(candidate, load_config(VALID_CONFIG).qbittorrent, rule=rule, dry_run=dry_run)
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
        plan = plan_qbittorrent_submission(candidate, self.config.qbittorrent, rule=rule, dry_run=dry_run)
        return QbittorrentSubmitResult(success=success, status=status, message=message, plan=plan, retryable=retryable, dry_run=dry_run)


def test_e2e_dry_run_plans_rss_rules_submit_organizer_and_webhook_without_external_mutation(tmp_path):
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
    assert action.destination_path.resolve(strict=False).is_relative_to((tmp_path / "library").resolve(strict=False))


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


def test_retryable_qbittorrent_submit_failure_remains_eligible_until_success(tmp_path, monkeypatch):
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


def test_run_once_apply_prefers_allowed_season_pack_without_recording_suppressed_episode(tmp_path, monkeypatch):
    config_path = _config(tmp_path, organizer_mode="move")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["subscriptions"]["rules"][0]["allow_packs"] = True
    raw["subscriptions"]["rules"][0]["categories"] = ["動畫", "季度全集"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "user")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pass")
    fake_qbit = FakeQbittorrentClient()

    dependencies = WorkflowDependencies(
        feed_fetcher=lambda _url: _episode_and_pack_rss(),
        qbittorrent_factory=lambda _config: fake_qbit,
    )
    result = run_once(config_path, dry_run=False, dependencies=dependencies)
    repeat = run_once(config_path, dry_run=False, dependencies=dependencies)

    assert result.parsed_items == 2
    assert len(result.candidates) == 1
    assert len(repeat.candidates) == 0
    assert result.candidates[0].candidate.feed_item.is_season_pack is True
    assert len(fake_qbit.submissions) == 1
    assert fake_qbit.submissions[0][0].title == "[ExampleSub] Example Anime 季度全集 [1080p]"
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert state.has_seen_item("infohash:2222222222222222222222222222222222222222")
        assert not state.has_seen_item("infohash:1111111111111111111111111111111111111111")
        assert [job["torrent_hash"] for job in state.list_jobs()] == ["2222222222222222222222222222222222222222"]


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
    assert fake_qbit.submissions[0][0].title == "[ExampleSub] Example Anime - 01 [1080p][CHS]"


def test_cli_commands_cover_validate_run_monitor_state_failures_and_retry(tmp_path, capsys):
    config_path = _config(tmp_path)
    completed_source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 01 [1080p][CHS].mkv"
    completed_source.parent.mkdir()
    completed_source.write_bytes(b"video")

    assert cli.main(["validate-config", "--config", str(config_path)]) == 0
    assert cli.main([
        "run-once",
        "--config",
        str(config_path),
        "--feed-file",
        str(FIXTURE_RSS),
        "--completed-source-path",
        str(completed_source),
    ]) == 0

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
        state.record_failure("job-retry", "download", "temporary", attempts=2, recoverable=True)

    snapshot_json = tmp_path / "snapshots.json"
    snapshot_json.write_text("[]", encoding="utf-8")
    assert cli.main(["monitor-once", "--config", str(config_path), "--snapshot-json", str(snapshot_json)]) == 0
    assert cli.main(["state", "--config", str(config_path)]) == 0
    assert cli.main(["failures", "--config", str(config_path)]) == 0
    assert cli.main(["retry-failed", "--config", str(config_path), "--job-id", "job-retry"]) == 0
    output = capsys.readouterr().out
    assert "valid config" in output
    assert "run once: dry_run=True" in output
    assert "planned qBittorrent submit:" in output
    assert "planned organizer:" in output
    assert "destination=" in output
    assert "planned webhook:" in output
    assert "event_type=download_planned" in output
    assert "event_type=download_completed" in output
    assert "retryable" in output
    assert "Job reset to pending" in output



def test_snapshots_match_base32_jobs_to_hex_qbittorrent_hash_and_strip_mkv_title(tmp_path):
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
                content_path=str(tmp_path / "downloads" / "[Nekomoe kissaten&LoliHouse] LIAR GAME - 07 [1080p].mkv"),
                completion_on=1,
            ),
        ),
    )

    assert len(snapshots) == 1
    assert snapshots[0].torrent_hash == base32_hash
    assert snapshots[0].metadata["qbittorrent_hash"] == "63b8b04640befcd202c9047a20e925e1573fa5e9"
    assert snapshots[0].name == "[Nekomoe kissaten&LoliHouse] LIAR GAME - 07 [1080p]"


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


def test_production_tick_apply_does_not_mark_new_submissions_missing_in_same_tick(tmp_path, monkeypatch):
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
                (OrganizerAction(Path(item.source_path), tmp_path / "library" / "planned.mkv", "applied", "video"),),
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
            metadata={"title": "[ExampleSub] Example Anime - 01 [1080p][CHS]", "qbittorrent_category": "anime"},
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
                (OrganizerAction(Path(item.source_path), tmp_path / "library" / "planned.mkv", "applied", "video"),),
            ),
        ),
    )

    assert len(result.snapshots) == 1
    assert result.snapshots[0].name == "[ExampleSub] Example Anime - 01 [1080p][CHS]"
    assert result.monitor_result is not None
    assert len(result.monitor_result.organizer_inputs) == 1
    assert result.summary()["monitor"]["organizer_inputs"] == 1


def test_production_tick_does_not_organize_qbittorrent_save_root_without_content_path(tmp_path, monkeypatch):
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
            organizer_runner=lambda item, config: organizer_calls.append(item)
            or OrganizerResult(item.job_id, config.organizer.mode, ()),
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


def test_production_tick_returns_failure_summary_when_qbittorrent_listing_fails(tmp_path, monkeypatch):
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
            organizer_runner=lambda item, config: organizer_calls.append(item)
            or OrganizerResult(item.job_id, config.organizer.mode, ()),
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


def test_monitor_once_production_injects_bangumi_lookup_into_default_organizer(tmp_path):
    config_path = _config(tmp_path)
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
        dependencies=WorkflowDependencies(bangumi_lookup=lambda title: calls.append(title) or "示例动画"),
    )

    assert calls == ["[ExampleSub] Example Anime - 01 [1080p][CHS]"]
    assert result.organizer_results[0].actions[0].destination_path == tmp_path / "library" / "示例动画" / "Example Anime - S01E01 - ExampleSub [1080p].mkv"


def test_plan_completed_dry_run_without_dependency_suppresses_default_bangumi_lookup(tmp_path, monkeypatch):
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
    monkeypatch.setattr(workflow, "lookup_chinese_title", lambda title: pytest.fail(f"unexpected Bangumi lookup for {title}"))

    result = plan_completed_dry_run(config_path, run_result, str(source))

    assert len(result.organizer_results) == 1
    assert result.organizer_results[0].actions[0].destination_path == tmp_path / "library" / "Example Anime" / "Season 01" / "Example Anime - S01E01 - ExampleSub [1080p].mkv"


def test_organize_once_dry_run_forces_planning_even_when_config_mode_moves(tmp_path):
    config_path = _config(tmp_path, organizer_mode="move")
    source = tmp_path / "downloads" / "[ExampleSub] Example Anime - 02 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")

    result = organize_once(
        config_path,
        OrganizerInput("job-organize", "HASH", "[ExampleSub] Example Anime - 02 [1080p]", str(source), datetime.now(timezone.utc)),
    )

    assert result.result.actions[0].status == "planned"
    assert source.exists()


def test_apply_mode_refuses_unsafe_config_until_credentials_and_move_are_explicit(tmp_path, monkeypatch):
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
        state.upsert_job("job-done", dedupe_key="infohash:done", status=DownloadJobStatus.COMPLETED)
        state.upsert_job("job-pending", dedupe_key="infohash:pending", status=DownloadJobStatus.PENDING)
        state.upsert_job("job-failed", dedupe_key="infohash:failed", status=DownloadJobStatus.FAILED)
        state.record_failure("job-failed", "download", "retry later", attempts=1, recoverable=True)

    summary = list_state(config_path)

    assert [job["job_id"] for job in summary.processed] == ["job-done"]
    assert [job["job_id"] for job in summary.pending] == ["job-pending"]
    assert [job["job_id"] for job in summary.failed] == ["job-failed"]
    assert [failure["subject_id"] for failure in summary.retryable] == ["job-failed"]
    assert retry_failed_item(config_path, "job-failed").retried is True


def test_register_tolerates_partial_hermes_contexts_and_exposes_tools():
    ctx = RecordingContext()

    register(ctx)

    assert "dmhy.validate_config" in ctx.tools
    assert "dmhy.run_once_dry_run" in ctx.tools
    assert "dmhy.schedule_tick" in ctx.hooks
    assert "hermes-dmhy" in ctx.commands

    partial = type("PartialContext", (), {"registered": {}, "register_tool": lambda self, name, handler: self.registered.setdefault(name, handler)})()
    register(partial)
    assert "dmhy.list_state" in partial.registered




def test_production_tick_lists_all_qbittorrent_torrents_to_avoid_stale_category_misses(tmp_path, monkeypatch):
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
            organizer_runner=lambda item, config: OrganizerResult(item.job_id, config.organizer.mode, ()),
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

    assert cli.main(["schedule-tick", "--config", str(config_path), "--feed-file", str(FIXTURE_RSS), "--apply"]) == 0

    assert calls == [(str(config_path), False, True)]
    output = json.loads(capsys.readouterr().out)
    assert output == {"ok": True, "dry_run": False, "monitor": {"organizer_inputs": 0}}



def test_cli_schedule_tick_apply_exits_nonzero_when_summary_not_ok(tmp_path, monkeypatch, capsys):
    config_path = _config(tmp_path)

    class FakeTickResult:
        ok = False

        def summary(self):
            return {"ok": False, "monitor": {"failures": [{"stage": "monitor"}]}}

    monkeypatch.setattr(cli, "production_tick", lambda *args, **kwargs: FakeTickResult())

    assert cli.main(["schedule-tick", "--config", str(config_path), "--apply"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_scheduler_tick_is_bounded_one_shot(tmp_path):
    config_path = _config(tmp_path)

    result = scheduler_tick(
        config_path,
        dependencies=WorkflowDependencies(feed_fetcher=lambda _url: FIXTURE_RSS.read_text(encoding="utf-8"), qbittorrent_factory=lambda _config: FakeQbittorrentClient()),
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
