import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ytdj.download import DownloadCanceled, DownloadConfig, DownloadProgress, DownloadResult
from ytdj.gui.main_window import JobWorker
from ytdj.metadata import AcoustIDConfig
from ytdj.models import DownloadJob, MetadataCandidate, ReviewState, TagWriteResult, TrackMetadata
from ytdj.sources import SourcePlatform


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


class FakeAcoustIDProvider:
    def __init__(self, config: AcoustIDConfig) -> None:
        self.config = config

    def lookup(self, audio_path: Path) -> list[MetadataCandidate]:
        assert audio_path.name == "abc.mp3"
        return [
            MetadataCandidate(
                provider="acoustid",
                score=0.96,
                matched_fields=("fingerprint", "title", "artist"),
                metadata=TrackMetadata(title="Recognized", artist="Artist", album="Album"),
            )
        ]


class FakeAcoustIDReleaseProvider:
    def __init__(self, config: AcoustIDConfig) -> None:
        self.config = config

    def lookup(self, audio_path: Path) -> list[MetadataCandidate]:
        return [
            MetadataCandidate(
                provider="acoustid",
                score=0.96,
                matched_fields=("fingerprint", "title", "artist", "album"),
                metadata=TrackMetadata(
                    title="Recognized",
                    artist="Artist",
                    album="Album",
                    musicbrainz_release_id="rel-1",
                ),
            )
        ]


class FakeCoverArtProvider:
    calls: list[str] = []

    def lookup(self, release_id: str) -> str:
        self.calls.append(release_id)
        return "https://coverartarchive.org/release/rel-1/front-500.jpg"


class FakeTagWriter:
    writes: list[Path] = []

    def write(self, path: Path, metadata: TrackMetadata) -> TagWriteResult:
        self.writes.append(path)
        return TagWriteResult(path=path, written_fields=("title", "artist"))


def test_worker_downloads_temp_audio_for_low_confidence_youtube(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        cookie_browser=None,
        ytmusic_auth_path=None,
        ffmpeg_location=None,
        acoustid_config=AcoustIDConfig(client_key="client-key", fpcalc_path=Path(__file__)),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        acoustid_provider_factory=FakeAcoustIDProvider,
    )

    metadata, state, candidates, downloaded_path = worker._try_audio_recognition(
        metadata=TrackMetadata(title="Fallback", artist="Uploader"),
        state=ReviewState.REVIEW_REQUIRED,
        candidates=[],
        platform=SourcePlatform.YOUTUBE,
    )

    assert state == ReviewState.AUTO_APPROVED
    assert metadata.title == "Recognized"
    assert metadata.artist == "Artist"
    assert candidates[0].provider == "acoustid"
    assert downloaded_path == job.downloaded_path
    assert downloaded_path is not None
    assert downloaded_path.parent == tmp_path / ".ytdj-temp" / job.id
    assert FakeDownloader.downloads == [downloaded_path]


def test_worker_passes_cookie_unlock_setting_to_downloader(tmp_path: Path) -> None:
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        cookie_browser="chrome",
        unlock_browser_cookie_database=True,
        ytmusic_auth_path=None,
        ffmpeg_location=None,
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
    )

    downloader = worker._new_downloader(tmp_path)

    assert downloader.config.cookie_browser == "chrome"
    assert downloader.config.unlock_browser_cookie_database is True


def test_worker_skips_auto_approved_audio_recognition_by_default(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        cookie_browser=None,
        ytmusic_auth_path=None,
        ffmpeg_location=None,
        acoustid_config=AcoustIDConfig(client_key="client-key", fpcalc_path=Path(__file__)),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        acoustid_provider_factory=FakeAcoustIDReleaseProvider,
    )

    metadata, state, candidates, downloaded_path = worker._try_audio_recognition(
        metadata=TrackMetadata(title="Auto", artist="Uploader"),
        state=ReviewState.AUTO_APPROVED,
        candidates=[],
        platform=SourcePlatform.YOUTUBE,
    )

    assert metadata.title == "Auto"
    assert state == ReviewState.AUTO_APPROVED
    assert candidates == []
    assert downloaded_path is None
    assert FakeDownloader.downloads == []


def test_worker_can_verify_auto_approved_metadata_with_acoustid(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    FakeCoverArtProvider.calls.clear()
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        cookie_browser=None,
        ytmusic_auth_path=None,
        ffmpeg_location=None,
        acoustid_config=AcoustIDConfig(client_key="client-key", fpcalc_path=Path(__file__)),
        verify_auto_approved_metadata=True,
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        acoustid_provider_factory=FakeAcoustIDReleaseProvider,
        cover_art_provider_factory=FakeCoverArtProvider,
    )

    metadata, state, candidates, downloaded_path = worker._try_audio_recognition(
        metadata=TrackMetadata(title="Wrong Auto", artist="Wrong Artist", cover_url="https://img.youtube.com/yt-thumb.jpg"),
        state=ReviewState.AUTO_APPROVED,
        candidates=[],
        platform=SourcePlatform.YOUTUBE,
    )

    assert state == ReviewState.REVIEW_REQUIRED
    assert metadata.title == "Recognized"
    assert metadata.artist == "Artist"
    assert metadata.cover_url == "https://coverartarchive.org/release/rel-1/front-500.jpg"
    assert candidates[0].provider == "acoustid"
    assert downloaded_path == job.downloaded_path
    assert FakeDownloader.downloads == [downloaded_path]


def test_worker_refreshes_cover_art_after_audio_recognition(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    FakeCoverArtProvider.calls.clear()
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    worker = JobWorker(
        job,
        cookie_browser=None,
        ytmusic_auth_path=None,
        ffmpeg_location=None,
        acoustid_config=AcoustIDConfig(client_key="client-key", fpcalc_path=Path(__file__)),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        acoustid_provider_factory=FakeAcoustIDReleaseProvider,
        cover_art_provider_factory=FakeCoverArtProvider,
    )

    metadata, state, candidates, downloaded_path = worker._try_audio_recognition(
        metadata=TrackMetadata(title="Fallback", artist="Uploader", cover_url="https://img.youtube.com/yt-thumb.jpg"),
        state=ReviewState.REVIEW_REQUIRED,
        candidates=[],
        platform=SourcePlatform.YOUTUBE,
    )

    assert state == ReviewState.AUTO_APPROVED
    assert metadata.cover_url == "https://coverartarchive.org/release/rel-1/front-500.jpg"
    assert candidates[0].provider == "acoustid"
    assert downloaded_path == job.downloaded_path
    assert FakeCoverArtProvider.calls == ["rel-1"]


def test_worker_skips_audio_recognition_for_soundcloud(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    job = DownloadJob(url="https://soundcloud.com/a/b", output_dir=tmp_path)
    worker = JobWorker(
        job,
        cookie_browser=None,
        ytmusic_auth_path=None,
        ffmpeg_location=None,
        acoustid_config=AcoustIDConfig(client_key="client-key", fpcalc_path=Path(__file__)),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        acoustid_provider_factory=FakeAcoustIDProvider,
    )

    metadata, state, candidates, downloaded_path = worker._try_audio_recognition(
        metadata=TrackMetadata(title="Bootleg", artist="DJ"),
        state=ReviewState.REVIEW_REQUIRED,
        candidates=[],
        platform=SourcePlatform.SOUNDCLOUD,
    )

    assert metadata.title == "Bootleg"
    assert state == ReviewState.REVIEW_REQUIRED
    assert candidates == []
    assert downloaded_path is None
    assert FakeDownloader.downloads == []


def test_worker_reuses_prepared_download_after_review_approval(tmp_path: Path) -> None:
    FakeDownloader.downloads.clear()
    FakeTagWriter.writes.clear()
    prepared = tmp_path / ".ytdj-temp" / "job" / "abc.mp3"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"fake mp3")
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    job.downloaded_path = prepared
    done: list[str] = []
    worker = JobWorker(
        job,
        cookie_browser=None,
        ytmusic_auth_path=None,
        ffmpeg_location=None,
        approved_metadata=TrackMetadata(title="Title", artist="Artist"),
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
        tag_writer_factory=FakeTagWriter,
    )
    worker.job_done.connect(lambda job_id, final_path: done.append(final_path))

    worker.run()

    final_path = tmp_path / "Artist - Title.mp3"
    assert final_path.exists()
    assert not prepared.exists()
    assert FakeDownloader.downloads == []
    assert FakeTagWriter.writes == [final_path]
    assert done == [str(final_path)]


def test_worker_cancellation_cleans_prepared_temp_download(tmp_path: Path) -> None:
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)
    prepared = tmp_path / ".ytdj-temp" / job.id / "abc.mp3"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"fake mp3")
    job.downloaded_path = prepared
    canceled: list[str] = []
    worker = JobWorker(
        job,
        cookie_browser=None,
        ytmusic_auth_path=None,
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
        cookie_browser=None,
        ytmusic_auth_path=None,
        ffmpeg_location=None,
        downloader_factory=lambda config, progress_callback: FakeDownloader(config, progress_callback),
    )

    worker.cancel()

    with pytest.raises(DownloadCanceled):
        worker._on_progress(DownloadProgress(status="downloading", percent=12.0, filename=tmp_path / "abc.part"))
