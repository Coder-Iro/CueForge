import sqlite3
from pathlib import Path

import pytest

from cueforge.models import DownloadJob, DownloadStatus, JobEvent, MetadataCandidate, TrackMetadata
from cueforge.store import JobStore


def test_job_store_persists_jobs_candidate_summaries_and_sanitized_events(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite")
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path / "downloads")
    job.status = DownloadStatus.REVIEW_REQUIRED
    job.platform = "youtube"
    job.selected_metadata = TrackMetadata(title="Song", artist="Artist")
    job.source_title = "Original Video Title"
    job.source_channel = "Original Channel"
    job.candidates = [
        MetadataCandidate(
            provider="musicbrainz",
            score=0.91,
            matched_fields=("title", "artist"),
            metadata=TrackMetadata(title="Song", artist="Artist", album="Album"),
            raw={"ignored": "__Secure-3PAPISID=secret"},
        )
    ]

    store.upsert_job(job)
    store.record_event(JobEvent(job_id=job.id, event_type="failed", message="Cookie: __Secure-3PAPISID=secret"))

    loaded = store.load_jobs()
    events = store.list_events(job.id)

    assert len(loaded) == 1
    assert loaded[0].selected_metadata.title == "Song"
    assert loaded[0].source_title == "Original Video Title"
    assert loaded[0].source_channel == "Original Channel"
    assert loaded[0].candidates[0].provider == "musicbrainz"
    assert loaded[0].candidates[0].metadata.album == "Album"
    assert "secret" not in events[0].message
    assert "<redacted>" in events[0].message


def test_job_store_keeps_active_queue_but_can_clear_terminal_history(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite")
    active = DownloadJob(url="https://youtu.be/active", output_dir=tmp_path)
    done = DownloadJob(url="https://youtu.be/done", output_dir=tmp_path, status=DownloadStatus.DONE)
    store.upsert_job(active)
    store.upsert_job(done)

    assert store.clear_history() == 1

    loaded = store.load_jobs()
    assert [job.url for job in loaded] == ["https://youtu.be/active"]


def test_job_store_deletes_jobs_in_one_call(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite")
    jobs = [DownloadJob(url=f"https://youtu.be/{index}", output_dir=tmp_path) for index in range(3)]
    for job in jobs:
        store.upsert_job(job)
        store.record_event(JobEvent(job_id=job.id, event_type="queued", message="queued"))

    store.delete_jobs([jobs[0].id, jobs[2].id])

    assert [job.url for job in store.load_jobs()] == [jobs[1].url]
    assert store.list_events(jobs[0].id) == []
    assert store.list_events(jobs[2].id) == []


def test_job_store_migrates_source_metadata_columns(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_info (version) VALUES (1)")
        conn.execute(
            """
            CREATE TABLE jobs (
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
            INSERT INTO jobs (
                id, url, platform, status, progress, output_dir, selected_metadata,
                candidate_summaries, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-1",
                "https://youtu.be/abc",
                "youtube",
                DownloadStatus.REVIEW_REQUIRED.value,
                0,
                str(tmp_path),
                "{}",
                "[]",
                1.0,
                1.0,
            ),
        )

    store = JobStore(path)
    loaded = store.load_jobs()

    assert store.schema_version == 2
    assert loaded[0].source_title == ""
    assert loaded[0].source_channel == ""


def test_job_store_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_info (version) VALUES (999)")

    with pytest.raises(RuntimeError, match="Unsupported"):
        JobStore(path)
