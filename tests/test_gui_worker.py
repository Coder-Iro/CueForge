import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cueforge.artwork import CachedCover
from cueforge.download import DownloadCanceled, DownloadConfig, DownloadProgress, DownloadResult
from cueforge.gui.main_window import JobWorker
from cueforge.metadata import MetadataResolution
from cueforge.models import DownloadJob, ReviewState, TagWriteResult, TrackMetadata
from cueforge.sources import SourcePlatform


class FakeDownloader:
    downloads: list[Path] = []

    def __init__(self, config: DownloadConfig, progress_callback: object) -> None:
        self.config = config
        self.progress_callback = progress_callback

    def fetch_info(self, url: str) -> dict:
        return {"extractor_key": "Youtube", "title": "Fallback", "uploader": "Uploader"}

    def download_audio(self, url: str) -> DownloadResult:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.output_dir / "abc.mp3"
        path.write_bytes(b"fake mp3")
        self.downloads.append(path)
        return DownloadResult(path=path, info={"id": "abc"})


class FakeCoverResolver:
    def resolve(self, **_kwargs) -> MetadataResolution:
        return MetadataResolution(
            metadata=TrackMetadata(title="Resolved", artist="Artist"),
            state=ReviewState.REVIEW_REQUIRED,
            candidates=[],
            platform=SourcePlatform.YOUTUBE,
        )

    def enrich_cover_art(
        self,
        metadata: TrackMetadata,
        *,
        platform: SourcePlatform,
        fallback_cover_url: str = "",
        log: object = None,
    ) -> TrackMetadata:
        return metadata


class FakeArtworkResolver:
    def resolve(self, **_kwargs) -> MetadataResolution:
        return MetadataResolution(
            metadata=TrackMetadata(title="Resolved", artist="Artist", cover_url="https://example.com/cover.jpg"),
            state=ReviewState.AUTO_APPROVED,
            candidates=[],
            platform=SourcePlatform.YOUTUBE,
        )


class SourceInfoDownloader(FakeDownloader):
    def fetch_info(self, url: str) -> dict:
        return {
            "extractor_key": "Youtube",
            "id": "abc",
            "title": "Original Video Title",
            "channel": "Original Channel",
        }


class FakeTagWriter:
    writes: list[Path] = []
    metadata: list[TrackMetadata] = []

    def write(self, path: Path, metadata: TrackMetadata) -> TagWriteResult:
        self.writes.append(path)
        self.metadata.append(metadata)
        return TagWriteResult(path=path, written_fields=("title", "artist"))


def test_worker_creates_downloader_without_cookie_file(tmp_path: Path) -> None:
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
    )

    downloader = worker._new_downloader(tmp_path)

    assert not hasattr(downloader.config, "cookie_file")


def test_worker_stores_original_source_title_and_channel(tmp_path: Path) -> None:
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        downloader_factory=lambda config, progress_callback: SourceInfoDownloader(config, progress_callback),
        resolver_factory=FakeCoverResolver,
    )

    metadata, state, candidates, platform = worker._resolve_metadata(worker._new_downloader(tmp_path))

    assert metadata.title == "Resolved"
    assert state == ReviewState.REVIEW_REQUIRED
    assert candidates == []
    assert platform == SourcePlatform.YOUTUBE
    assert job.source_id == "abc"
    assert job.source_title == "Original Video Title"
    assert job.source_channel == "Original Channel"


def test_worker_caches_cover_url_before_tagging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDownloader.downloads.clear()
    FakeTagWriter.writes.clear()
    FakeTagWriter.metadata.clear()
    cover_path = tmp_path / "cover-cache.jpg"
    cover_path.write_bytes(b"image")
    cache_calls: list[tuple[str, str]] = []

    def fake_cache_cover_url(url: str, *, cache_key: str = "", **_kwargs: object) -> CachedCover:
        cache_calls.append((url, cache_key))
        return CachedCover(path=cover_path, mime="image/jpeg", source_url=url)

    monkeypatch.setattr("cueforge.gui.main_window.cache_cover_url", fake_cache_cover_url)
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        resolver_factory=FakeArtworkResolver,
        tag_writer_factory=FakeTagWriter,
    )

    worker.run()

    assert cache_calls == [("https://example.com/cover.jpg", job.id)]
    assert FakeTagWriter.metadata[-1].cover_url == "https://example.com/cover.jpg"
    assert FakeTagWriter.metadata[-1].cover_path == str(cover_path)


def test_worker_reuses_prepared_download_after_review_approval(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    FakeTagWriter.writes.clear()
    FakeTagWriter.metadata.clear()
    prepared = tmp_path / ".cueforge-temp" / "job" / "abc.mp3"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"fake mp3")
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    job.downloaded_path = prepared
    done: list[str] = []
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        approved_metadata=TrackMetadata(title="Title", artist="Artist"),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        tag_writer_factory=FakeTagWriter,
    )
    worker.job_done.connect(lambda job_id, final_path: done.append(final_path))

    worker.run()

    final_path = tmp_path / "Artist - Title [abc].mp3"
    assert final_path.exists()
    assert not prepared.exists()
    assert FakeDownloader.downloads == []
    assert FakeTagWriter.writes == [prepared]
    assert done == [str(final_path)]


def test_worker_retags_existing_final_file_after_review_edit(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    FakeTagWriter.writes.clear()
    FakeTagWriter.metadata.clear()
    existing = tmp_path / "Old Artist - Old Title [abc].mp3"
    existing.write_bytes(b"fake mp3")
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    job.downloaded_path = existing
    job.final_path = existing
    job.source_id = "abc"
    done: list[str] = []
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        approved_metadata=TrackMetadata(title="New Title", artist="New Artist"),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        tag_writer_factory=FakeTagWriter,
    )
    worker.job_done.connect(lambda job_id, final_path: done.append(final_path))

    worker.run()

    final_path = tmp_path / "New Artist - New Title [abc].mp3"
    assert final_path.exists()
    assert not existing.exists()
    assert FakeDownloader.downloads == []
    assert FakeTagWriter.writes == [existing]
    assert done == [str(final_path)]


def test_worker_keeps_existing_final_filename_when_review_edit_does_not_change_name(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    FakeTagWriter.writes.clear()
    FakeTagWriter.metadata.clear()
    existing = tmp_path / "Artist - Title [abc].mp3"
    existing.write_bytes(b"fake mp3")
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    job.downloaded_path = existing
    job.final_path = existing
    job.source_id = "abc"
    done: list[str] = []
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        approved_metadata=TrackMetadata(title="Title", artist="Artist"),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        tag_writer_factory=FakeTagWriter,
    )
    worker.job_done.connect(lambda job_id, final_path: done.append(final_path))

    worker.run()

    assert existing.exists()
    assert not (tmp_path / "Artist - Title [abc] (2).mp3").exists()
    assert FakeDownloader.downloads == []
    assert FakeTagWriter.writes == [existing]
    assert done == [str(existing)]


def test_worker_leaves_no_final_file_when_tagging_fails(tmp_path: Path) -> None:
    prepared = tmp_path / ".cueforge-temp" / "job" / "abc.mp3"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"fake mp3")
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    job.downloaded_path = prepared

    class FailingTagWriter:
        def write(self, path: Path, metadata: TrackMetadata) -> TagWriteResult:
            raise RuntimeError("tag failed")

    failed: list[str] = []
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        approved_metadata=TrackMetadata(title="Title", artist="Artist"),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        tag_writer_factory=FailingTagWriter,
    )
    worker.job_failed.connect(lambda job_id, error: failed.append(error))

    worker.run()

    assert failed == ["tag failed"]
    assert prepared.exists()
    assert not (tmp_path / "Artist - Title [abc].mp3").exists()


def test_worker_cancellation_cleans_prepared_temp_download(tmp_path: Path) -> None:
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    prepared = tmp_path / ".cueforge-temp" / job.id / "abc.mp3"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"fake mp3")
    job.downloaded_path = prepared
    canceled: list[str] = []
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        approved_metadata=TrackMetadata(title="Title", artist="Artist"),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        tag_writer_factory=FakeTagWriter,
    )
    worker.job_canceled.connect(canceled.append)

    worker.cancel()
    worker.run()

    assert canceled == [job.id]
    assert job.downloaded_path is None
    assert not prepared.exists()


def test_worker_progress_hook_raises_when_cancel_requested(tmp_path: Path) -> None:
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        ffmpeg_location=None,
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
    )

    worker.cancel()

    with pytest.raises(DownloadCanceled):
        worker._on_progress(DownloadProgress(status="downloading", percent=12.0, filename=tmp_path / "abc.part"))
