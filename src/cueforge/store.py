"""SQLite persistence for CueForge jobs and lightweight history."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from platformdirs import user_data_path

from cueforge.models import (
    DownloadJob,
    DownloadStatus,
    JobEvent,
    MetadataCandidate,
    StoredCandidateSummary,
    TrackMetadata,
)

SCHEMA_VERSION = 1
_SECRET_PATTERNS = (
    re.compile(r"(__Secure-[A-Za-z0-9_]+|SAPISID|SID|LOGIN_INFO)=([^;\s]+)", re.IGNORECASE),
    re.compile(r"(client[_ -]?key|authorization|cookie)\s*[:=]\s*([^\s;]+)", re.IGNORECASE),
)


def default_job_store_path() -> Path:
    return user_data_path("CueForge") / "jobs.sqlite"


class JobStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_job_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT version FROM schema_info").fetchone()
        return int(row[0]) if row else 0

    def upsert_job(self, job: DownloadJob) -> None:
        job.updated_at = time.time()
        candidate_summaries = _candidate_summaries(job)
        job.candidate_summaries = candidate_summaries
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, url, platform, status, progress, output_dir, downloaded_path, final_path,
                    error, error_category, error_message, retry_count, selected_metadata,
                    candidate_summaries, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url,
                    platform=excluded.platform,
                    status=excluded.status,
                    progress=excluded.progress,
                    output_dir=excluded.output_dir,
                    downloaded_path=excluded.downloaded_path,
                    final_path=excluded.final_path,
                    error=excluded.error,
                    error_category=excluded.error_category,
                    error_message=excluded.error_message,
                    retry_count=excluded.retry_count,
                    selected_metadata=excluded.selected_metadata,
                    candidate_summaries=excluded.candidate_summaries,
                    updated_at=excluded.updated_at
                """,
                (
                    job.id,
                    job.url,
                    job.platform,
                    job.status.value,
                    job.progress,
                    str(job.output_dir),
                    str(job.downloaded_path) if job.downloaded_path else "",
                    str(job.final_path) if job.final_path else "",
                    job.error,
                    job.error_category,
                    job.error_message,
                    job.retry_count,
                    _metadata_json(job.selected_metadata),
                    _summaries_json(candidate_summaries),
                    job.created_at,
                    job.updated_at,
                ),
            )

    def delete_job(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def clear_history(self) -> int:
        terminal = tuple(status.value for status in (DownloadStatus.DONE, DownloadStatus.FAILED, DownloadStatus.CANCELED))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM jobs WHERE status IN ({','.join('?' for _ in terminal)})",
                terminal,
            ).fetchall()
            ids = [str(row[0]) for row in rows]
            if ids:
                conn.executemany("DELETE FROM job_events WHERE job_id = ?", [(job_id,) for job_id in ids])
                conn.executemany("DELETE FROM jobs WHERE id = ?", [(job_id,) for job_id in ids])
        return len(ids)

    def load_jobs(self) -> list[DownloadJob]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, url, platform, status, progress, output_dir, downloaded_path, final_path,
                       error, error_category, error_message, retry_count, selected_metadata,
                       candidate_summaries, created_at, updated_at
                FROM jobs
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def record_event(self, event: JobEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO job_events (job_id, event_type, category, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.job_id,
                    event.event_type,
                    event.category,
                    sanitize_event_text(event.message),
                    event.created_at,
                ),
            )

    def list_events(self, job_id: str, *, limit: int = 30) -> list[JobEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id, event_type, category, message, created_at
                FROM job_events
                WHERE job_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        return [
            JobEvent(
                job_id=str(row[0]),
                event_type=str(row[1]),
                category=str(row[2] or ""),
                message=str(row[3] or ""),
                created_at=float(row[4]),
            )
            for row in rows
        ]

    def recent_failure_summary(self, *, limit: int = 5) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT url, error_category, error_message
                FROM jobs
                WHERE status = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (DownloadStatus.FAILED.value, limit),
            ).fetchall()
        return [
            f"{row[1] or 'unknown'}: {row[0]} :: {sanitize_event_text(str(row[2] or ''))}"
            for row in rows
        ]

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL)")
            row = conn.execute("SELECT version FROM schema_info").fetchone()
            if not row:
                conn.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row[0]) != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported CueForge job store schema: {row[0]}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    output_dir TEXT NOT NULL,
                    downloaded_path TEXT NOT NULL DEFAULT '',
                    final_path TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    error_category TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    selected_metadata TEXT NOT NULL,
                    candidate_summaries TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id, created_at DESC)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)


def sanitize_event_text(message: str) -> str:
    sanitized = str(message or "")
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(lambda match: f"{match.group(1)}=<redacted>", sanitized)
    return sanitized[:1200]


def _candidate_summaries(job: DownloadJob) -> list[StoredCandidateSummary]:
    if job.candidates:
        return [StoredCandidateSummary.from_candidate(candidate) for candidate in job.candidates[:10]]
    return list(job.candidate_summaries[:10])


def _metadata_json(metadata: TrackMetadata) -> str:
    return json.dumps({field: getattr(metadata, field) for field in metadata.field_names()}, ensure_ascii=False)


def _summaries_json(summaries: list[StoredCandidateSummary]) -> str:
    return json.dumps(
        [
            {
                "provider": summary.provider,
                "score": summary.score,
                "title": summary.title,
                "artist": summary.artist,
                "album": summary.album,
                "matched_fields": list(summary.matched_fields),
            }
            for summary in summaries
        ],
        ensure_ascii=False,
    )


def _job_from_row(row: tuple[Any, ...]) -> DownloadJob:
    (
        job_id,
        url,
        platform,
        status,
        progress,
        output_dir,
        downloaded_path,
        final_path,
        error,
        error_category,
        error_message,
        retry_count,
        selected_metadata,
        candidate_summaries,
        created_at,
        updated_at,
    ) = row
    summaries = _load_summaries(str(candidate_summaries or "[]"))
    candidates = [
        MetadataCandidate(
            provider=summary.provider,
            score=summary.score,
            matched_fields=summary.matched_fields,
            metadata=TrackMetadata(title=summary.title, artist=summary.artist, album=summary.album),
        )
        for summary in summaries
    ]
    return DownloadJob(
        id=str(job_id),
        url=str(url),
        platform=str(platform or ""),
        status=DownloadStatus(str(status)),
        progress=float(progress),
        output_dir=Path(str(output_dir)),
        downloaded_path=Path(str(downloaded_path)) if downloaded_path else None,
        final_path=Path(str(final_path)) if final_path else None,
        error=str(error or ""),
        error_category=str(error_category or ""),
        error_message=str(error_message or ""),
        retry_count=int(retry_count or 0),
        selected_metadata=_load_metadata(str(selected_metadata or "{}")),
        candidate_summaries=summaries,
        candidates=candidates,
        created_at=float(created_at),
        updated_at=float(updated_at),
    )


def _load_metadata(payload: str) -> TrackMetadata:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    values = {field: data.get(field) for field in TrackMetadata.field_names()}
    return TrackMetadata(**values).normalized()


def _load_summaries(payload: str) -> list[StoredCandidateSummary]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = []
    if not isinstance(data, list):
        return []
    summaries: list[StoredCandidateSummary] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        summaries.append(
            StoredCandidateSummary(
                provider=str(item.get("provider") or ""),
                score=float(item.get("score") or 0.0),
                title=str(item.get("title") or ""),
                artist=str(item.get("artist") or ""),
                album=str(item.get("album") or ""),
                matched_fields=tuple(str(field) for field in item.get("matched_fields") or []),
            )
        )
    return summaries
