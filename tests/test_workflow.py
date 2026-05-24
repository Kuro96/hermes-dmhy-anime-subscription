import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_dmhy_anime_subscription import cli, register
from hermes_dmhy_anime_subscription.config import load_config
from hermes_dmhy_anime_subscription.models import DownloadJobStatus, OrganizerMode
from hermes_dmhy_anime_subscription.monitor import OrganizerInput
from hermes_dmhy_anime_subscription.qbittorrent import QbittorrentSubmitResult, plan_qbittorrent_submission
from hermes_dmhy_anime_subscription.state import SubscriptionState
from hermes_dmhy_anime_subscription.workflow import (
    WorkflowDependencies,
    ensure_apply_safe,
    list_state,
    monitor_once,
    organize_once,
    plan_completed_dry_run,
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
