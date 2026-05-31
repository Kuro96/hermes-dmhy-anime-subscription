import sqlite3
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
        assert (
            state.upsert_job(
                "job-1",
                dedupe_key="infohash:abcdef",
                status=DownloadJobStatus.SUBMITTED,
                torrent_hash="ABCDEF",
                retry_count=1,
                metadata={"rule": "example"},
            )
            is True
        )
        assert (
            state.upsert_job(
                "job-1",
                dedupe_key="infohash:abcdef",
                status=DownloadJobStatus.COMPLETED,
                torrent_hash="ABCDEF",
                retry_count=1,
                organizer_outcome="planned",
                metadata={"rule": "example"},
            )
            is False
        )
        assert state.job_count("job-1") == 1
        assert state.record_torrent_hash("ABCDEF", job_id="job-1") is False

        job = state.get_job("job-1")

    assert job is not None
    assert job["status"] == "completed"
    assert job["torrent_hash"] == "abcdef"
    assert job["organizer_outcome"] == "planned"
    assert job["metadata"] == {"rule": "example"}


def test_satisfied_season_pack_recording_is_idempotent(tmp_path):
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        assert (
            state.record_satisfied_season_pack(
                "Example anime",
                "example anime",
                1,
                job_id="job-pack",
                dedupe_key="infohash:pack",
            )
            is True
        )
        assert (
            state.record_satisfied_season_pack(
                "Example anime",
                "example anime",
                1,
                job_id="job-pack",
                dedupe_key="infohash:pack",
            )
            is False
        )
        assert state.list_satisfied_season_packs() == (
            ("Example anime", "example anime", 1),
        )


def test_copy_from_readonly_preserves_partial_old_schema_jobs(tmp_path):
    source_path = tmp_path / "old-state.sqlite3"
    with sqlite3.connect(source_path) as connection:
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
            ("job-old", "infohash:old", DownloadJobStatus.SUBMITTED.value),
        )

    with SubscriptionState(":memory:") as state:
        assert state.copy_from_readonly(source_path) is True
        jobs = state.list_jobs()

    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "job-old"
    assert jobs[0]["dedupe_key"] == "infohash:old"
    assert jobs[0]["status"] == DownloadJobStatus.SUBMITTED.value
    assert jobs[0]["retry_count"] == 0
    assert jobs[0]["metadata"] == {}
    assert jobs[0]["created_at"]
    assert jobs[0]["updated_at"]


def test_copy_from_readonly_coalesces_null_copy_defaults_for_jobs(tmp_path):
    source_path = tmp_path / "old-state.sqlite3"
    with sqlite3.connect(source_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO jobs (job_id, dedupe_key, status, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            ("job-null", "infohash:null", DownloadJobStatus.SUBMITTED.value, None),
        )

    with SubscriptionState(":memory:") as state:
        assert state.copy_from_readonly(source_path) is True
        jobs = state.list_jobs()

    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "job-null"
    assert jobs[0]["metadata"] == {}
    assert jobs[0]["retry_count"] == 0
    assert jobs[0]["created_at"]
    assert jobs[0]["updated_at"]


def test_copy_from_readonly_coalesces_present_null_required_fields(tmp_path):
    source_path = tmp_path / "old-state.sqlite3"
    with sqlite3.connect(source_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                dedupe_key TEXT,
                status TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO jobs (job_id, dedupe_key, status) VALUES (?, ?, ?)",
            ("job-null-required", None, None),
        )
        connection.execute(
            """
            CREATE TABLE failures (
                subject_id TEXT,
                stage TEXT,
                message TEXT,
                attempts INTEGER,
                recoverable INTEGER
            )
            """
        )
        connection.execute(
            """
            INSERT INTO failures (subject_id, stage, message, attempts, recoverable)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("job-null-required", "qbittorrent", None, None, None),
        )
        connection.execute(
            """
            CREATE TABLE archived_rules (
                rule_name TEXT PRIMARY KEY,
                reason TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO archived_rules (rule_name, reason) VALUES (?, ?)",
            ("example-show", None),
        )

    with SubscriptionState(":memory:") as state:
        assert state.copy_from_readonly(source_path) is True
        jobs = state.list_jobs()
        failures = state.list_failures()
        archived_rules = state.list_archived_rules()

    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "job-null-required"
    assert jobs[0]["dedupe_key"] == ""
    assert jobs[0]["status"] == DownloadJobStatus.PENDING.value
    assert len(failures) == 1
    assert failures[0]["message"] == ""
    assert failures[0]["attempts"] == 0
    assert failures[0]["recoverable"] == 1
    assert failures[0]["last_failed_at"]
    assert len(archived_rules) == 1
    assert archived_rules[0]["reason"] == ""
    assert archived_rules[0]["archived_at"]


def test_direct_migration_and_readonly_copy_default_empty_timestamp_strings(tmp_path):
    direct_path = tmp_path / "direct-empty-timestamps.sqlite3"
    copy_source_path = tmp_path / "copy-empty-timestamps.sqlite3"
    _create_state_with_empty_required_timestamps(direct_path)
    _create_state_with_empty_required_timestamps(copy_source_path)

    with SubscriptionState(direct_path) as state:
        direct_values = _required_timestamp_values(state)
    with SubscriptionState(":memory:") as state:
        assert state.copy_from_readonly(copy_source_path) is True
        copy_values = _required_timestamp_values(state)

    assert direct_values.keys() == copy_values.keys()
    for values in (direct_values, copy_values):
        assert all(value != "" for value in values.values())
        assert all(value for value in values.values())


def test_direct_migration_and_readonly_copy_default_missing_satisfied_pack_ids_to_empty_string(
    tmp_path,
):
    direct_path = tmp_path / "direct-old-satisfied.sqlite3"
    copy_source_path = tmp_path / "copy-old-satisfied.sqlite3"
    _create_satisfied_pack_state_without_job_columns(direct_path)
    _create_satisfied_pack_state_without_job_columns(copy_source_path)

    with SubscriptionState(direct_path) as state:
        direct_values = _satisfied_pack_job_values(state)
    with SubscriptionState(":memory:") as state:
        assert state.copy_from_readonly(copy_source_path) is True
        copy_values = _satisfied_pack_job_values(state)

    assert direct_values == {"job_id": "", "dedupe_key": ""}
    assert copy_values == direct_values


def test_migrates_jobs_without_primary_key_before_upsert(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute("CREATE TABLE jobs (job_id TEXT)")
        connection.execute("INSERT INTO jobs (job_id) VALUES (?)", ("job-old",))

    with SubscriptionState(state_path) as state:
        assert (
            state.upsert_job(
                "job-old",
                dedupe_key="infohash:old",
                status=DownloadJobStatus.COMPLETED,
            )
            is False
        )
        job = state.get_job("job-old")

    assert job is not None
    assert job["dedupe_key"] == "infohash:old"
    assert job["status"] == DownloadJobStatus.COMPLETED.value


def test_direct_migration_rebuilds_jobs_with_primary_key_but_noncanonical_columns(
    tmp_path,
):
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                dedupe_key TEXT,
                status TEXT,
                retry_count INTEGER,
                metadata_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO jobs
                (job_id, dedupe_key, status, retry_count, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("job-noncanonical", None, None, None, None, "", ""),
        )

    with SubscriptionState(state_path) as state:
        job = state.get_job("job-noncanonical")
        jobs_schema = _table_info(state, "jobs")

    assert job is not None
    assert job["dedupe_key"] == ""
    assert job["status"] == DownloadJobStatus.PENDING.value
    assert job["retry_count"] == 0
    assert job["metadata"] == {}
    assert job["created_at"]
    assert job["updated_at"]
    assert jobs_schema["dedupe_key"]["notnull"] == 1
    assert jobs_schema["status"]["notnull"] == 1
    assert jobs_schema["retry_count"]["notnull"] == 1
    assert jobs_schema["retry_count"]["dflt_value"] == "0"
    assert jobs_schema["metadata_json"]["notnull"] == 1
    assert jobs_schema["metadata_json"]["dflt_value"] == "'{}'"
    assert jobs_schema["created_at"]["notnull"] == 1
    assert jobs_schema["created_at"]["dflt_value"] is None


def test_direct_migration_and_readonly_copy_drop_empty_identity_key_rows(tmp_path):
    direct_path = tmp_path / "direct-empty-keys.sqlite3"
    copy_source_path = tmp_path / "copy-empty-keys.sqlite3"
    _create_jobs_with_empty_identity_keys(direct_path)
    _create_jobs_with_empty_identity_keys(copy_source_path)

    with SubscriptionState(direct_path) as state:
        direct_jobs = _jobs_by_id(state)
    with SubscriptionState(":memory:") as state:
        assert state.copy_from_readonly(copy_source_path) is True
        copy_jobs = _jobs_by_id(state)

    assert set(direct_jobs) == {"job-valid"}
    assert copy_jobs.keys() == direct_jobs.keys()
    assert direct_jobs["job-valid"]["status"] == DownloadJobStatus.SUBMITTED.value
    assert copy_jobs["job-valid"]["status"] == DownloadJobStatus.SUBMITTED.value


def test_direct_migration_drops_empty_identity_key_rows_from_canonical_table(
    tmp_path,
):
    state_path = tmp_path / "canonical-empty-keys.sqlite3"
    _create_canonical_jobs_with_empty_identity_key(state_path)

    with SubscriptionState(state_path) as state:
        jobs = _jobs_by_id(state)

    assert set(jobs) == {"job-valid"}
    assert jobs["job-valid"]["status"] == DownloadJobStatus.SUBMITTED.value


def test_direct_migration_and_readonly_copy_choose_newest_duplicate_identity_row(
    tmp_path,
):
    direct_path = tmp_path / "direct-duplicate-keys.sqlite3"
    copy_source_path = tmp_path / "copy-duplicate-keys.sqlite3"
    _create_jobs_with_duplicate_identity_keys(direct_path)
    _create_jobs_with_duplicate_identity_keys(copy_source_path)

    with SubscriptionState(direct_path) as state:
        direct_jobs = _jobs_by_id(state)
    with SubscriptionState(":memory:") as state:
        assert state.copy_from_readonly(copy_source_path) is True
        copy_jobs = _jobs_by_id(state)

    assert direct_jobs.keys() == copy_jobs.keys()
    assert direct_jobs["job-dupe"]["status"] == DownloadJobStatus.COMPLETED.value
    assert copy_jobs["job-dupe"]["status"] == DownloadJobStatus.COMPLETED.value
    assert direct_jobs["job-rowid"]["status"] == DownloadJobStatus.FAILED.value
    assert copy_jobs["job-rowid"]["status"] == DownloadJobStatus.FAILED.value


def test_failure_and_organizer_outcome_slots_are_available(tmp_path):
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.record_failure("job-1", "download", "temporary failure", attempts=2)
        state.record_organizer_outcome("job-1", "dry-run", "/tmp/source", "/tmp/dest")


def test_clear_failure_removes_only_matching_subject_and_stage(tmp_path):
    with SubscriptionState(tmp_path / "state.sqlite3") as state:
        state.record_failure("job-1", "qbittorrent", "temporary failure", attempts=1)
        state.record_failure("job-1", "webhook", "webhook failure", attempts=1)
        state.record_failure("job-2", "qbittorrent", "other failure", attempts=1)

        state.clear_failure("job-1", "qbittorrent")

        assert state.get_failure("job-1", "qbittorrent") is None
        assert state.get_failure("job-1", "webhook") is not None
        assert state.get_failure("job-2", "qbittorrent") is not None


def test_archived_rules_are_durable_and_listable(tmp_path):
    state_path = tmp_path / "state.sqlite3"

    with SubscriptionState(state_path) as state:
        assert state.is_rule_archived("example-show") is False
        state.archive_rule(
            "example-show",
            bangumi_subject_id=12345,
            reason="bangumi_complete",
            metadata={"completed_episodes": [1, 2]},
        )

    with SubscriptionState(state_path) as state:
        assert state.is_rule_archived("example-show") is True
        archived = state.list_archived_rules()

    assert len(archived) == 1
    assert archived[0]["rule_name"] == "example-show"
    assert archived[0]["bangumi_subject_id"] == 12345
    assert archived[0]["reason"] == "bangumi_complete"
    assert archived[0]["metadata"] == {"completed_episodes": [1, 2]}


def _create_state_with_empty_required_timestamps(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE seen_items (
                dedupe_key TEXT PRIMARY KEY,
                info_hash TEXT,
                guid TEXT,
                normalized_title TEXT,
                published_at TEXT,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                source_feed TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            INSERT INTO seen_items
                (dedupe_key, title, link, first_seen_at, last_seen_at)
            VALUES ('seen-empty-time', 'Seen', 'https://example.invalid/seen', '', '');

            CREATE TABLE torrent_hashes (
                torrent_hash TEXT PRIMARY KEY,
                job_id TEXT,
                first_seen_at TEXT NOT NULL
            );
            INSERT INTO torrent_hashes (torrent_hash, job_id, first_seen_at)
            VALUES ('abcdef', 'job-empty-time', '');

            CREATE TABLE satisfied_season_packs (
                rule_name TEXT NOT NULL,
                series_key TEXT NOT NULL,
                season INTEGER NOT NULL,
                job_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (rule_name, series_key, season)
            );
            INSERT INTO satisfied_season_packs
                (rule_name, series_key, season, job_id, dedupe_key, recorded_at)
            VALUES ('example-show', 'example-show', 1, 'job-pack', 'dedupe-pack', '');

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
            );
            INSERT INTO jobs
                (job_id, dedupe_key, status, retry_count, metadata_json, created_at, updated_at)
            VALUES ('job-empty-time', 'dedupe-job', 'pending', 0, '{}', '', '');

            CREATE TABLE failures (
                subject_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                message TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                recoverable INTEGER NOT NULL,
                last_failed_at TEXT NOT NULL,
                PRIMARY KEY (subject_id, stage)
            );
            INSERT INTO failures
                (subject_id, stage, message, attempts, recoverable, last_failed_at)
            VALUES ('job-empty-time', 'download', 'failed', 1, 1, '');

            CREATE TABLE organizer_outcomes (
                job_id TEXT PRIMARY KEY,
                outcome TEXT NOT NULL,
                source_path TEXT,
                destination_path TEXT,
                recorded_at TEXT NOT NULL
            );
            INSERT INTO organizer_outcomes (job_id, outcome, recorded_at)
            VALUES ('job-empty-time', 'planned', '');

            CREATE TABLE archived_rules (
                rule_name TEXT PRIMARY KEY,
                bangumi_subject_id INTEGER,
                reason TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                archived_at TEXT NOT NULL
            );
            INSERT INTO archived_rules (rule_name, reason, metadata_json, archived_at)
            VALUES ('example-show', 'complete', '{}', '');
            """
        )


def _create_satisfied_pack_state_without_job_columns(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE satisfied_season_packs (
                rule_name TEXT NOT NULL,
                series_key TEXT NOT NULL,
                season INTEGER NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (rule_name, series_key, season)
            );
            INSERT INTO satisfied_season_packs
                (rule_name, series_key, season, recorded_at)
            VALUES ('example-show', 'example-show', 1, '');
            """
        )


def _create_jobs_with_empty_identity_keys(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT,
                dedupe_key TEXT,
                status TEXT,
                updated_at TEXT
            );
            INSERT INTO jobs (job_id, dedupe_key, status, updated_at)
            VALUES
                (NULL, 'dedupe-null', 'pending', '2026-01-03T00:00:00+00:00'),
                ('', 'dedupe-empty', 'completed', '2026-01-04T00:00:00+00:00'),
                ('job-valid', 'dedupe-valid', 'submitted', '2026-01-02T00:00:00+00:00');
            """
        )


def _create_jobs_with_duplicate_identity_keys(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT,
                dedupe_key TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            INSERT INTO jobs (job_id, dedupe_key, status, created_at, updated_at)
            VALUES
                ('job-dupe', 'dedupe-old', 'pending',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
                ('job-dupe', 'dedupe-new', 'completed',
                    '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00'),
                ('job-rowid', 'dedupe-rowid-old', 'pending',
                    '2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00'),
                ('job-rowid', 'dedupe-rowid-new', 'failed',
                    '2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00');
            """
        )


def _create_canonical_jobs_with_empty_identity_key(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
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
            );
            INSERT INTO jobs
                (job_id, dedupe_key, status, retry_count, metadata_json, created_at, updated_at)
            VALUES
                ('', 'dedupe-empty', 'completed', 0, '{}',
                    '2026-01-04T00:00:00+00:00', '2026-01-04T00:00:00+00:00'),
                ('job-valid', 'dedupe-valid', 'submitted', 0, '{}',
                    '2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00');
            """
        )


def _required_timestamp_values(state):
    checks = {
        "seen_items": ("first_seen_at", "last_seen_at"),
        "torrent_hashes": ("first_seen_at",),
        "satisfied_season_packs": ("recorded_at",),
        "jobs": ("created_at", "updated_at"),
        "failures": ("last_failed_at",),
        "organizer_outcomes": ("recorded_at",),
        "archived_rules": ("archived_at",),
    }
    values = {}
    for table, columns in checks.items():
        selected = ", ".join(columns)
        row = state._connection.execute(f"SELECT {selected} FROM {table}").fetchone()
        for column in columns:
            values[(table, column)] = row[column]
    return values


def _satisfied_pack_job_values(state):
    row = state._connection.execute(
        "SELECT job_id, dedupe_key FROM satisfied_season_packs"
    ).fetchone()
    return {"job_id": row["job_id"], "dedupe_key": row["dedupe_key"]}


def _jobs_by_id(state):
    return {str(job["job_id"]): job for job in state.list_jobs()}


def _table_info(state, table):
    return {
        str(row["name"]): dict(row)
        for row in state._connection.execute(f"PRAGMA table_info({table})")
    }
