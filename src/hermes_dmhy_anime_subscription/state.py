"""SQLite-backed durable state for DMHY subscription processing."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import DownloadJobStatus, FeedItem


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
        self._initialize_schema()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

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
        key = item_or_key.dedupe_key if isinstance(item_or_key, FeedItem) else item_or_key
        cursor = self._connection.execute("SELECT 1 FROM seen_items WHERE dedupe_key = ?", (key,))
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

    def record_pack_preference(self, rule_name: str, series_key: str, season: int, *, job_id: str, dedupe_key: str) -> bool:
        now = _now()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO pack_preferences
                    (rule_name, series_key, season, job_id, dedupe_key, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (rule_name, series_key, season, job_id, dedupe_key, now),
            )
        return cursor.rowcount == 1

    def list_pack_preferences(self) -> tuple[tuple[str, str, int], ...]:
        cursor = self._connection.execute("SELECT rule_name, series_key, season FROM pack_preferences")
        return tuple((str(row["rule_name"]), str(row["series_key"]), int(row["season"])) for row in cursor.fetchall())

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
        cursor = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        return _job_row(cursor.fetchone())

    def get_job_by_torrent_hash(self, torrent_hash: str) -> dict[str, Any] | None:
        cursor = self._connection.execute("SELECT * FROM jobs WHERE torrent_hash = ?", (torrent_hash.lower(),))
        return _job_row(cursor.fetchone())

    def get_failure(self, subject_id: str, stage: str) -> dict[str, Any] | None:
        cursor = self._connection.execute("SELECT * FROM failures WHERE subject_id = ? AND stage = ?", (subject_id, stage))
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def job_count(self, job_id: str) -> int:
        cursor = self._connection.execute("SELECT COUNT(*) FROM jobs WHERE job_id = ?", (job_id,))
        return int(cursor.fetchone()[0])

    def record_failure(self, subject_id: str, stage: str, message: str, attempts: int, recoverable: bool = True) -> None:
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

    def record_organizer_outcome(self, job_id: str, outcome: str, source_path: str | None = None, destination_path: str | None = None) -> None:
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


    def list_jobs(self, statuses: tuple[str, ...] | list[str] | None = None) -> tuple[dict[str, Any], ...]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            cursor = self._connection.execute(f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY updated_at DESC", tuple(statuses))
        else:
            cursor = self._connection.execute("SELECT * FROM jobs ORDER BY updated_at DESC")
        return tuple(job for row in cursor.fetchall() if (job := _job_row(row)) is not None)

    def list_failures(self, subject_id: str | None = None) -> tuple[dict[str, Any], ...]:
        if subject_id is None:
            cursor = self._connection.execute("SELECT * FROM failures ORDER BY last_failed_at DESC")
        else:
            cursor = self._connection.execute("SELECT * FROM failures WHERE subject_id = ? ORDER BY last_failed_at DESC", (subject_id,))
        return tuple(dict(row) for row in cursor.fetchall())

    def list_organizer_outcomes(self) -> tuple[dict[str, Any], ...]:
        cursor = self._connection.execute("SELECT * FROM organizer_outcomes ORDER BY recorded_at DESC")
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
                CREATE INDEX IF NOT EXISTS idx_seen_items_info_hash ON seen_items(info_hash);
                CREATE INDEX IF NOT EXISTS idx_seen_items_guid ON seen_items(guid);

                CREATE TABLE IF NOT EXISTS torrent_hashes (
                    torrent_hash TEXT PRIMARY KEY,
                    job_id TEXT,
                    first_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pack_preferences (
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
                CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key);
                CREATE INDEX IF NOT EXISTS idx_jobs_torrent_hash ON jobs(torrent_hash);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

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
                """
            )


def _job_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    metadata_json = data.pop("metadata_json")
    data["metadata"] = json.loads(metadata_json if isinstance(metadata_json, str) else "{}")
    return data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
