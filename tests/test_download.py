from pathlib import Path

from ytdj.download import CookieBrowser, DownloadConfig, DownloadProgress, YTDLPDownloader


class FakeYDL:
    calls: list[dict] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        self.calls.append(options)

    def __enter__(self) -> "FakeYDL":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict:
        assert url == "https://music.youtube.com/watch?v=abc"
        if download:
            for hook in self.options["progress_hooks"]:
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 50,
                        "total_bytes": 100,
                        "filename": "abc.webm",
                    }
                )
            return {
                "id": "abc",
                "title": "Track",
                "requested_downloads": [{"filepath": "D:/music/abc.mp3"}],
            }
        return {"id": "abc", "title": "Track"}

    def prepare_filename(self, info: dict) -> str:
        return f"D:/music/{info['id']}.webm"


def test_fetch_info_uses_ytdlp_without_download(tmp_path: Path) -> None:
    FakeYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, cookie_browser=CookieBrowser.CHROME),
        ydl_factory=FakeYDL,
    )

    info = downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert info["id"] == "abc"
    assert FakeYDL.calls[-1]["cookiesfrombrowser"] == ("chrome",)


def test_download_audio_configures_mp3_extraction(tmp_path: Path) -> None:
    progresses: list[DownloadProgress] = []
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path),
        ydl_factory=FakeYDL,
        progress_callback=progresses.append,
    )

    result = downloader.download_audio("https://music.youtube.com/watch?v=abc")

    options = FakeYDL.calls[-1]
    assert options["postprocessors"][0]["preferredcodec"] == "mp3"
    assert options["postprocessors"][0]["preferredquality"] == "320"
    assert result.path == Path("D:/music/abc.mp3")
    assert progresses[0].percent == 50.0

