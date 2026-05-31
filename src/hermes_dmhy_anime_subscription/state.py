"""SQLite-backed durable state for DMHY subscription processing."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import DownloadJobStatus, FeedItem


_STATE_TABLES = (
    "seen_items",
    "torrent_hashes",
    "satisfied_season_packs",
    "jobs",
    "failures",
    "organizer_outcomes",
    "archived_rules",
)


class SubscriptionState(AbstractContextManager["SubscriptionState"]):
    """Durable local state using Python's stdlib SQLite backend."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        database = str(path)
        if database != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            database = str(self.path)
        self._connection = sqlite3.connect(database)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self.initialize_schema()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def copy_from_readonly(self, source_path: str | Path) -> bool:
        source_database = Path(source_path)
        if not source_database.exists():
            return False
        uri = f"{source_database.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as source:
            source.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            copied_tables = tuple(table for table in _STATE_TABLES if table in tables)
            if not copied_tables:
                return False
            self.initialize_schema()
            with self._connection:
                for table in copied_tables:
                    self._copy_table_rows(source, table)
        return True

    def initialize_schema(self) -> None:
        self._initialize_schema()

    def _copy_table_rows(self, source: sqlite3.Connection, table: str) -> None:
        source_columns = _table_columns(source, table)
        source_column_set = set(source_columns)
        destination_columns = _table_columns(self._connection, table)
        if not source_column_set:
            return
        now = _now()
        columns = tuple(destination_columns)
        selected_rows_sql, parameters = _canonical_row_selection_sql(
            source, table, columns, source_column_set, now
        )
        quoted_table = _quote_identifier(table)
        quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        rows = source.execute(selected_rows_sql, parameters).fetchall()
        self._connection.executemany(
            f"INSERT OR IGNORE INTO {quoted_table} ({quoted_columns}) VALUES ({placeholders})",
            rows,
        )

    def record_seen_item(self, item: FeedItem) -> bool:
        """Record a feed item once; return True only for the first insertion."""

        now = _now()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO seen_items
                    (dedupe_key, info_hash, guid, normalized_title, published_at, title, link, source_feed, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.dedupe_key,
                    item.info_hash.lower() if item.info_hash else None,
                    item.guid,
                    item.normalized_title,
                    _datetime_value(item.published_at),
                    item.title,
                    item.link,
                    item.source_feed,
                    now,
                    now,
                ),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                self._connection.execute(
                    "UPDATE seen_items SET last_seen_at = ? WHERE dedupe_key = ?",
                    (now, item.dedupe_key),
                )
        return inserted

    def has_seen_item(self, item_or_key: FeedItem | str) -> bool:
        key = (
            item_or_key.dedupe_key if isinstance(item_or_key, FeedItem) else item_or_key
        )
        cursor = self._connection.execute(
            "SELECT 1 FROM seen_items WHERE dedupe_key = ?", (key,)
        )
        return cursor.fetchone() is not None

    def record_torrent_hash(self, torrent_hash: str, job_id: str | None = None) -> bool:
        """Record a submitted torrent hash once."""

        normalized_hash = torrent_hash.lower()
        now = _now()
        with self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO torrent_hashes (torrent_hash, job_id, first_seen_at) VALUES (?, ?, ?)",
                (normalized_hash, job_id, now),
            )
        return cursor.rowcount == 1

    def record_satisfied_season_pack(
        self,
        rule_name: str,
        series_key: str,
        season: int,
        *,
        job_id: str,
        dedupe_key: str,
    ) -> bool:
        now = _now()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO satisfied_season_packs
                    (rule_name, series_key, season, job_id, dedupe_key, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (rule_name, series_key, season, job_id, dedupe_key, now),
            )
        return cursor.rowcount == 1

    def list_satisfied_season_packs(self) -> tuple[tuple[str, str, int], ...]:
        cursor = self._connection.execute(
            "SELECT rule_name, series_key, season FROM satisfied_season_packs"
        )
        return tuple(
            (str(row["rule_name"]), str(row["series_key"]), int(row["season"]))
            for row in cursor.fetchall()
        )

    def upsert_job(
        self,
        job_id: str,
        *,
        dedupe_key: str,
        status: DownloadJobStatus | str = DownloadJobStatus.PENDING,
        torrent_hash: str | None = None,
        retry_count: int = 0,
        last_error: str | None = None,
        organizer_outcome: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Create or update a job idempotently; return True when a row was created."""

        status_value = status.value if isinstance(status, DownloadJobStatus) else status
        now = _now()
        payload = json.dumps(metadata or {}, sort_keys=True)
        normalized_hash = torrent_hash.lower() if torrent_hash else None
        existed = self.job_count(job_id) > 0
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO jobs
                    (job_id, dedupe_key, torrent_hash, status, retry_count, last_error, organizer_outcome, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    dedupe_key = excluded.dedupe_key,
                    torrent_hash = excluded.torrent_hash,
                    status = excluded.status,
                    retry_count = excluded.retry_count,
                    last_error = excluded.last_error,
                    organizer_outcome = excluded.organizer_outcome,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    dedupe_key,
                    normalized_hash,
                    status_value,
                    retry_count,
                    last_error,
                    organizer_outcome,
                    payload,
                    now,
                    now,
                ),
            )
            if normalized_hash:
                self._connection.execute(
                    "INSERT OR IGNORE INTO torrent_hashes (torrent_hash, job_id, first_seen_at) VALUES (?, ?, ?)",
                    (normalized_hash, job_id, now),
                )
        return not existed

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        cursor = self._connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        )
        return _job_row(cursor.fetchone())

    def get_job_by_torrent_hash(self, torrent_hash: str) -> dict[str, Any] | None:
        cursor = self._connection.execute(
            "SELECT * FROM jobs WHERE torrent_hash = ?", (torrent_hash.lower(),)
        )
        return _job_row(cursor.fetchone())

    def get_failure(self, subject_id: str, stage: str) -> dict[str, Any] | None:
        cursor = self._connection.execute(
            "SELECT * FROM failures WHERE subject_id = ? AND stage = ?",
            (subject_id, stage),
        )
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def job_count(self, job_id: str) -> int:
        cursor = self._connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_id = ?", (job_id,)
        )
        return int(cursor.fetchone()[0])

    def record_failure(
        self,
        subject_id: str,
        stage: str,
        message: str,
        attempts: int,
        recoverable: bool = True,
    ) -> None:
        now = _now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO failures (subject_id, stage, message, attempts, recoverable, last_failed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_id, stage) DO UPDATE SET
                    message = excluded.message,
                    attempts = excluded.attempts,
                    recoverable = excluded.recoverable,
                    last_failed_at = excluded.last_failed_at
                """,
                (subject_id, stage, message, attempts, int(recoverable), now),
            )

    def clear_failure(self, subject_id: str, stage: str) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM failures WHERE subject_id = ? AND stage = ?",
                (subject_id, stage),
            )

    def record_organizer_outcome(
        self,
        job_id: str,
        outcome: str,
        source_path: str | None = None,
        destination_path: str | None = None,
    ) -> None:
        now = _now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO organizer_outcomes (job_id, outcome, source_path, destination_path, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    outcome = excluded.outcome,
                    source_path = excluded.source_path,
                    destination_path = excluded.destination_path,
                    recorded_at = excluded.recorded_at
                """,
                (job_id, outcome, source_path, destination_path, now),
            )

    def archive_rule(
        self,
        rule_name: str,
        *,
        bangumi_subject_id: int | None = None,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        payload = json.dumps(metadata or {}, sort_keys=True)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO archived_rules (rule_name, bangumi_subject_id, reason, metadata_json, archived_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(rule_name) DO UPDATE SET
                    bangumi_subject_id = excluded.bangumi_subject_id,
                    reason = excluded.reason,
                    metadata_json = excluded.metadata_json,
                    archived_at = excluded.archived_at
                """,
                (rule_name, bangumi_subject_id, reason, payload, now),
            )

    def is_rule_archived(self, rule_name: str) -> bool:
        cursor = self._connection.execute(
            "SELECT 1 FROM archived_rules WHERE rule_name = ?", (rule_name,)
        )
        return cursor.fetchone() is not None

    def list_archived_rules(self) -> tuple[dict[str, Any], ...]:
        cursor = self._connection.execute(
            "SELECT * FROM archived_rules ORDER BY archived_at DESC"
        )
        return tuple(_archived_rule_row(row) for row in cursor.fetchall())

    def list_jobs(
        self, statuses: tuple[str, ...] | list[str] | None = None
    ) -> tuple[dict[str, Any], ...]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            cursor = self._connection.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY updated_at DESC",
                tuple(statuses),
            )
        else:
            cursor = self._connection.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC"
            )
        return tuple(
            job for row in cursor.fetchall() if (job := _job_row(row)) is not None
        )

    def list_failures(
        self, subject_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        if subject_id is None:
            cursor = self._connection.execute(
                "SELECT * FROM failures ORDER BY last_failed_at DESC"
            )
        else:
            cursor = self._connection.execute(
                "SELECT * FROM failures WHERE subject_id = ? ORDER BY last_failed_at DESC",
                (subject_id,),
            )
        return tuple(dict(row) for row in cursor.fetchall())

    def list_organizer_outcomes(self) -> tuple[dict[str, Any], ...]:
        cursor = self._connection.execute(
            "SELECT * FROM organizer_outcomes ORDER BY recorded_at DESC"
        )
        return tuple(dict(row) for row in cursor.fetchall())

    def _initialize_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_items (
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
                CREATE TABLE IF NOT EXISTS torrent_hashes (
                    torrent_hash TEXT PRIMARY KEY,
                    job_id TEXT,
                    first_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS satisfied_season_packs (
                    rule_name TEXT NOT NULL,
                    series_key TEXT NOT NULL,
                    season INTEGER NOT NULL,
                    job_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (rule_name, series_key, season)
                );

                CREATE TABLE IF NOT EXISTS jobs (
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
                CREATE TABLE IF NOT EXISTS failures (
                    subject_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    recoverable INTEGER NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    PRIMARY KEY (subject_id, stage)
                );

                CREATE TABLE IF NOT EXISTS organizer_outcomes (
                    job_id TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    source_path TEXT,
                    destination_path TEXT,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS archived_rules (
                    rule_name TEXT PRIMARY KEY,
                    bangumi_subject_id INTEGER,
                    reason TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    archived_at TEXT NOT NULL
                );
                """
            )
            self._migrate_known_tables()
            self._rebuild_known_tables_missing_constraints()
            self._connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_items_info_hash ON seen_items(info_hash);
                CREATE INDEX IF NOT EXISTS idx_seen_items_guid ON seen_items(guid);
                CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key);
                CREATE INDEX IF NOT EXISTS idx_jobs_torrent_hash ON jobs(torrent_hash);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                """
            )

    def _migrate_known_tables(self) -> None:
        now = _now()
        for table, definitions in _MIGRATION_COLUMN_DEFINITIONS.items():
            existing_columns = set(_table_columns(self._connection, table))
            for column, definition in definitions:
                quoted_table = _quote_identifier(table)
                quoted_column = _quote_identifier(column)
                if column not in existing_columns:
                    self._connection.execute(
                        f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} {definition}"
                    )
                    existing_columns.add(column)
                default = _migration_default_expression(table, column, now)
                if default is None:
                    continue
                if column in _REQUIRED_TABLE_KEYS[table]:
                    continue
                expression, values = default
                null_condition = f"{quoted_column} IS NULL"
                if column in _TIMESTAMP_COLUMNS:
                    null_condition = f"{null_condition} OR {quoted_column} = ''"
                self._connection.execute(
                    f"UPDATE {quoted_table} SET {quoted_column} = {expression} WHERE {null_condition}",
                    values,
                )

    def _rebuild_known_tables_missing_constraints(self) -> None:
        for table, key_columns in _REQUIRED_TABLE_KEYS.items():
            if (
                _has_required_key(self._connection, table, key_columns)
                and _has_canonical_column_constraints(self._connection, table)
                and not _has_invalid_identity_keys(self._connection, table, key_columns)
            ):
                continue
            _rebuild_table(self._connection, table)


def _job_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    metadata_json = data.pop("metadata_json")
    data["metadata"] = _json_object(metadata_json)
    return data


def _archived_rule_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    metadata_json = data.pop("metadata_json")
    data["metadata"] = _json_object(metadata_json)
    return data


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    cursor = connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    return tuple(str(row[1]) for row in cursor.fetchall())


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _has_required_key(
    connection: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> bool:
    table_info = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    primary_key_columns = tuple(
        str(row[1]) for row in sorted(table_info, key=lambda row: int(row[5])) if row[5]
    )
    if primary_key_columns == columns:
        return True
    for index in connection.execute(f"PRAGMA index_list({_quote_identifier(table)})"):
        if not index[2]:
            continue
        index_columns = tuple(
            str(row[2])
            for row in connection.execute(
                f"PRAGMA index_info({_quote_identifier(str(index[1]))})"
            )
        )
        if index_columns == columns:
            return True
    return False


def _has_canonical_column_constraints(
    connection: sqlite3.Connection, table: str
) -> bool:
    table_info = {
        str(row[1]): row
        for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    }
    for column, not_null, default in _CANONICAL_COLUMN_REQUIREMENTS[table]:
        row = table_info.get(column)
        if row is None:
            return False
        if bool(row[3]) != not_null:
            return False
        if _normalize_default(row[4]) != _normalize_default(default):
            return False
    return True


def _has_invalid_identity_keys(
    connection: sqlite3.Connection, table: str, key_columns: tuple[str, ...]
) -> bool:
    source_column_set = set(_table_columns(connection, table))
    condition = _valid_identity_key_condition(key_columns, source_column_set)
    if condition == "0":
        return True
    cursor = connection.execute(
        f"SELECT 1 FROM {_quote_identifier(table)} WHERE NOT ({condition}) LIMIT 1"
    )
    return cursor.fetchone() is not None


def _normalize_default(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    return normalized


def _rebuild_table(connection: sqlite3.Connection, table: str) -> None:
    now = _now()
    temporary_table = f"__hermes_migration_{table}"
    quoted_table = _quote_identifier(table)
    quoted_temporary_table = _quote_identifier(temporary_table)
    destination_columns = tuple(
        column for column, _definition in _MIGRATION_COLUMN_DEFINITIONS[table]
    )
    source_column_set = set(_table_columns(connection, table))
    columns = ", ".join(_quote_identifier(column) for column in destination_columns)
    selected_rows_sql, parameters = _canonical_row_selection_sql(
        connection, table, destination_columns, source_column_set, now
    )
    connection.execute(f"DROP TABLE IF EXISTS {quoted_temporary_table}")
    connection.execute(
        f"CREATE TABLE {quoted_temporary_table} ({_CANONICAL_TABLE_DEFINITIONS[table]})"
    )
    connection.execute(
        f"INSERT INTO {quoted_temporary_table} ({columns}) {selected_rows_sql}",
        parameters,
    )
    connection.execute(f"DROP TABLE {quoted_table}")
    connection.execute(f"ALTER TABLE {quoted_temporary_table} RENAME TO {quoted_table}")


def _canonical_row_selection_sql(
    connection: sqlite3.Connection,
    table: str,
    destination_columns: tuple[str, ...],
    source_column_set: set[str],
    now: str,
) -> tuple[str, tuple[object, ...]]:
    quoted_table = _quote_identifier(table)
    key_columns = _REQUIRED_TABLE_KEYS[table]
    inner_expressions: list[str] = []
    parameters: list[object] = []
    for column in destination_columns:
        quoted_column = _quote_identifier(column)
        if column in key_columns:
            if column in source_column_set:
                inner_expressions.append(f"{quoted_column} AS {quoted_column}")
            else:
                inner_expressions.append(f"NULL AS {quoted_column}")
            continue
        default = _migration_default_expression(table, column, now)
        if column in source_column_set:
            if default is None:
                inner_expressions.append(f"{quoted_column} AS {quoted_column}")
            else:
                expression, values = default
                value_expression = quoted_column
                if column in _TIMESTAMP_COLUMNS:
                    value_expression = f"NULLIF({quoted_column}, '')"
                inner_expressions.append(
                    f"COALESCE({value_expression}, {expression}) AS {quoted_column}"
                )
                parameters.extend(values)
            continue
        if default is None:
            inner_expressions.append(f"NULL AS {quoted_column}")
            continue
        expression, values = default
        inner_expressions.append(f"{expression} AS {quoted_column}")
        parameters.extend(values)

    include_rowid = _table_has_rowid(connection, table)
    if include_rowid:
        inner_expressions.append(f"rowid AS {_quote_identifier(_SOURCE_ROWID_COLUMN)}")

    selected_columns = ", ".join(
        _quote_identifier(column) for column in destination_columns
    )
    ranked_columns = selected_columns
    partition_by = ", ".join(_quote_identifier(column) for column in key_columns)
    order_by = _deduplication_order_by(table, destination_columns, include_rowid)
    where_clause = _valid_identity_key_condition(key_columns, source_column_set)
    inner_select = ", ".join(inner_expressions)
    sql = f"""
        SELECT {selected_columns}
        FROM (
            SELECT
                {ranked_columns},
                ROW_NUMBER() OVER (
                    PARTITION BY {partition_by}
                    ORDER BY {order_by}
                ) AS {_quote_identifier(_ROW_RANK_COLUMN)}
            FROM (
                SELECT {inner_select}
                FROM {quoted_table}
                WHERE {where_clause}
            ) AS {_quote_identifier(_SOURCE_ROWS_ALIAS)}
        ) AS {_quote_identifier(_RANKED_ROWS_ALIAS)}
        WHERE {_quote_identifier(_ROW_RANK_COLUMN)} = 1
    """
    return sql, tuple(parameters)


def _valid_identity_key_condition(
    key_columns: tuple[str, ...], source_column_set: set[str]
) -> str:
    conditions: list[str] = []
    for column in key_columns:
        if column not in source_column_set:
            return "0"
        quoted_column = _quote_identifier(column)
        conditions.append(f"{quoted_column} IS NOT NULL")
        conditions.append(f"CAST({quoted_column} AS TEXT) != ''")
    return " AND ".join(conditions)


def _deduplication_order_by(
    table: str, destination_columns: tuple[str, ...], include_rowid: bool
) -> str:
    terms = [
        f"NULLIF({_quote_identifier(column)}, '') DESC"
        for column in _DEDUPLICATION_TIMESTAMP_COLUMNS[table]
        if column in destination_columns
    ]
    if include_rowid:
        terms.append(f"{_quote_identifier(_SOURCE_ROWID_COLUMN)} DESC")
    if not terms:
        terms.append("1")
    return ", ".join(terms)


def _table_has_rowid(connection: sqlite3.Connection, table: str) -> bool:
    try:
        connection.execute(
            f"SELECT rowid FROM {_quote_identifier(table)} LIMIT 1"
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    return True


_MIGRATION_COLUMN_DEFINITIONS = {
    "seen_items": (
        ("dedupe_key", "TEXT DEFAULT ''"),
        ("info_hash", "TEXT"),
        ("guid", "TEXT"),
        ("normalized_title", "TEXT"),
        ("published_at", "TEXT"),
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("link", "TEXT NOT NULL DEFAULT ''"),
        ("source_feed", "TEXT"),
        ("first_seen_at", "TEXT NOT NULL DEFAULT ''"),
        ("last_seen_at", "TEXT NOT NULL DEFAULT ''"),
    ),
    "torrent_hashes": (
        ("torrent_hash", "TEXT DEFAULT ''"),
        ("job_id", "TEXT"),
        ("first_seen_at", "TEXT NOT NULL DEFAULT ''"),
    ),
    "satisfied_season_packs": (
        ("rule_name", "TEXT NOT NULL DEFAULT ''"),
        ("series_key", "TEXT NOT NULL DEFAULT ''"),
        ("season", "INTEGER NOT NULL DEFAULT 0"),
        ("job_id", "TEXT NOT NULL DEFAULT ''"),
        ("dedupe_key", "TEXT NOT NULL DEFAULT ''"),
        ("recorded_at", "TEXT NOT NULL DEFAULT ''"),
    ),
    "jobs": (
        ("job_id", "TEXT DEFAULT ''"),
        ("dedupe_key", "TEXT NOT NULL DEFAULT ''"),
        ("torrent_hash", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "TEXT"),
        ("organizer_outcome", "TEXT"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("created_at", "TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    ),
    "failures": (
        ("subject_id", "TEXT NOT NULL DEFAULT ''"),
        ("stage", "TEXT NOT NULL DEFAULT ''"),
        ("message", "TEXT NOT NULL DEFAULT ''"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("recoverable", "INTEGER NOT NULL DEFAULT 1"),
        ("last_failed_at", "TEXT NOT NULL DEFAULT ''"),
    ),
    "organizer_outcomes": (
        ("job_id", "TEXT DEFAULT ''"),
        ("outcome", "TEXT NOT NULL DEFAULT ''"),
        ("source_path", "TEXT"),
        ("destination_path", "TEXT"),
        ("recorded_at", "TEXT NOT NULL DEFAULT ''"),
    ),
    "archived_rules": (
        ("rule_name", "TEXT DEFAULT ''"),
        ("bangumi_subject_id", "INTEGER"),
        ("reason", "TEXT NOT NULL DEFAULT ''"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("archived_at", "TEXT NOT NULL DEFAULT ''"),
    ),
}

_CANONICAL_TABLE_DEFINITIONS = {
    "seen_items": """
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
    """,
    "torrent_hashes": """
        torrent_hash TEXT PRIMARY KEY,
        job_id TEXT,
        first_seen_at TEXT NOT NULL
    """,
    "satisfied_season_packs": """
        rule_name TEXT NOT NULL,
        series_key TEXT NOT NULL,
        season INTEGER NOT NULL,
        job_id TEXT NOT NULL,
        dedupe_key TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (rule_name, series_key, season)
    """,
    "jobs": """
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
    """,
    "failures": """
        subject_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        message TEXT NOT NULL,
        attempts INTEGER NOT NULL,
        recoverable INTEGER NOT NULL,
        last_failed_at TEXT NOT NULL,
        PRIMARY KEY (subject_id, stage)
    """,
    "organizer_outcomes": """
        job_id TEXT PRIMARY KEY,
        outcome TEXT NOT NULL,
        source_path TEXT,
        destination_path TEXT,
        recorded_at TEXT NOT NULL
    """,
    "archived_rules": """
        rule_name TEXT PRIMARY KEY,
        bangumi_subject_id INTEGER,
        reason TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        archived_at TEXT NOT NULL
    """,
}

_REQUIRED_TABLE_KEYS = {
    "seen_items": ("dedupe_key",),
    "torrent_hashes": ("torrent_hash",),
    "satisfied_season_packs": ("rule_name", "series_key", "season"),
    "jobs": ("job_id",),
    "failures": ("subject_id", "stage"),
    "organizer_outcomes": ("job_id",),
    "archived_rules": ("rule_name",),
}

_CANONICAL_COLUMN_REQUIREMENTS = {
    "seen_items": (
        ("dedupe_key", False, None),
        ("info_hash", False, None),
        ("guid", False, None),
        ("normalized_title", False, None),
        ("published_at", False, None),
        ("title", True, None),
        ("link", True, None),
        ("source_feed", False, None),
        ("first_seen_at", True, None),
        ("last_seen_at", True, None),
    ),
    "torrent_hashes": (
        ("torrent_hash", False, None),
        ("job_id", False, None),
        ("first_seen_at", True, None),
    ),
    "satisfied_season_packs": (
        ("rule_name", True, None),
        ("series_key", True, None),
        ("season", True, None),
        ("job_id", True, None),
        ("dedupe_key", True, None),
        ("recorded_at", True, None),
    ),
    "jobs": (
        ("job_id", False, None),
        ("dedupe_key", True, None),
        ("torrent_hash", False, None),
        ("status", True, None),
        ("retry_count", True, "0"),
        ("last_error", False, None),
        ("organizer_outcome", False, None),
        ("metadata_json", True, "'{}'"),
        ("created_at", True, None),
        ("updated_at", True, None),
    ),
    "failures": (
        ("subject_id", True, None),
        ("stage", True, None),
        ("message", True, None),
        ("attempts", True, None),
        ("recoverable", True, None),
        ("last_failed_at", True, None),
    ),
    "organizer_outcomes": (
        ("job_id", False, None),
        ("outcome", True, None),
        ("source_path", False, None),
        ("destination_path", False, None),
        ("recorded_at", True, None),
    ),
    "archived_rules": (
        ("rule_name", False, None),
        ("bangumi_subject_id", False, None),
        ("reason", True, None),
        ("metadata_json", True, "'{}'"),
        ("archived_at", True, None),
    ),
}

_DEDUPLICATION_TIMESTAMP_COLUMNS = {
    "seen_items": ("last_seen_at", "first_seen_at"),
    "torrent_hashes": ("first_seen_at",),
    "satisfied_season_packs": ("recorded_at",),
    "jobs": ("updated_at", "created_at"),
    "failures": ("last_failed_at",),
    "organizer_outcomes": ("recorded_at",),
    "archived_rules": ("archived_at",),
}

_SOURCE_ROWID_COLUMN = "__hermes_source_rowid"
_ROW_RANK_COLUMN = "__hermes_rank"
_SOURCE_ROWS_ALIAS = "__hermes_source_rows"
_RANKED_ROWS_ALIAS = "__hermes_ranked_rows"

_TIMESTAMP_COLUMNS = {
    "first_seen_at",
    "last_seen_at",
    "recorded_at",
    "created_at",
    "updated_at",
    "last_failed_at",
    "archived_at",
}


def _migration_default_expression(
    table: str, column: str, now: str
) -> tuple[str, tuple[object, ...]] | None:
    if table == "seen_items":
        if column in {"title", "link", "dedupe_key"}:
            return ("''", ())
        if column in {"first_seen_at", "last_seen_at"}:
            return ("?", (now,))
    if table == "torrent_hashes":
        if column == "torrent_hash":
            return ("''", ())
        if column == "first_seen_at":
            return ("?", (now,))
    if table == "satisfied_season_packs":
        if column in {"rule_name", "series_key", "job_id", "dedupe_key"}:
            return ("''", ())
        if column == "season":
            return ("0", ())
        if column == "recorded_at":
            return ("?", (now,))
    if table == "jobs":
        if column in {"job_id", "dedupe_key"}:
            return ("''", ())
        if column == "status":
            return ("'pending'", ())
        if column == "retry_count":
            return ("0", ())
        if column == "metadata_json":
            return ("'{}'", ())
        if column in {"created_at", "updated_at"}:
            return ("?", (now,))
    if table == "failures":
        if column in {"subject_id", "stage", "message"}:
            return ("''", ())
        if column == "attempts":
            return ("0", ())
        if column == "recoverable":
            return ("1", ())
        if column == "last_failed_at":
            return ("?", (now,))
    if table == "organizer_outcomes":
        if column in {"job_id", "outcome"}:
            return ("''", ())
        if column == "recorded_at":
            return ("?", (now,))
    if table == "archived_rules":
        if column in {"rule_name", "reason"}:
            return ("''", ())
        if column == "metadata_json":
            return ("'{}'", ())
        if column == "archived_at":
            return ("?", (now,))
    return None


def _copy_default_expression(
    table: str, column: str, now: str
) -> tuple[str, tuple[object, ...]] | None:
    return _migration_default_expression(table, column, now)


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
