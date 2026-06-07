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


class CookieCopyFailingYDL(FakeYDL):
    calls: list[dict] = []

    def extract_info(self, url: str, download: bool = False) -> dict:
        if self.options.get("cookiesfrombrowser"):
            raise RuntimeError("ERROR: Could not copy Chrome cookie database")
        return super().extract_info(url, download=download)


def test_fetch_info_uses_ytdlp_without_download(tmp_path: Path) -> None:
    FakeYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, cookie_browser=CookieBrowser.CHROME),
        ydl_factory=FakeYDL,
    )

    info = downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert info["id"] == "abc"
    assert FakeYDL.calls[-1]["cookiesfrombrowser"] == ("chrome",)
    assert FakeYDL.calls[-1]["remote_components"] == ["ejs:github"]
    assert FakeYDL.calls[-1]["noplaylist"] is True


def test_fetch_info_accepts_cookie_browser_string_from_qt(tmp_path: Path) -> None:
    FakeYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, cookie_browser="chrome"),
        ydl_factory=FakeYDL,
    )

    downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert FakeYDL.calls[-1]["cookiesfrombrowser"] == ("chrome",)


def test_chromium_cookie_unlock_can_be_enabled_for_chrome(tmp_path: Path, monkeypatch) -> None:
    FakeYDL.calls.clear()
    unlock_calls: list[bool] = []
    monkeypatch.setattr("ytdj.download.set_chromium_cookie_unlock_enabled", unlock_calls.append)
    downloader = YTDLPDownloader(
        DownloadConfig(
            output_dir=tmp_path,
            cookie_browser=CookieBrowser.CHROME,
            unlock_browser_cookie_database=True,
        ),
        ydl_factory=FakeYDL,
    )

    downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert unlock_calls == [True]
    assert FakeYDL.calls[-1]["cookiesfrombrowser"] == ("chrome",)


def test_cookie_unlock_is_not_enabled_for_firefox(tmp_path: Path, monkeypatch) -> None:
    unlock_calls: list[bool] = []
    monkeypatch.setattr("ytdj.download.set_chromium_cookie_unlock_enabled", unlock_calls.append)
    downloader = YTDLPDownloader(
        DownloadConfig(
            output_dir=tmp_path,
            cookie_browser=CookieBrowser.FIREFOX,
            unlock_browser_cookie_database=True,
        ),
        ydl_factory=FakeYDL,
    )

    downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert unlock_calls == [False]


def test_fetch_info_retries_without_browser_cookies_when_cookie_copy_fails(tmp_path: Path) -> None:
    CookieCopyFailingYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, cookie_browser=CookieBrowser.CHROME),
        ydl_factory=CookieCopyFailingYDL,
    )

    info = downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert info["id"] == "abc"
    assert CookieCopyFailingYDL.calls[0]["cookiesfrombrowser"] == ("chrome",)
    assert "cookiesfrombrowser" not in CookieCopyFailingYDL.calls[1]


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


def test_download_audio_retries_without_browser_cookies_when_cookie_copy_fails(tmp_path: Path) -> None:
    CookieCopyFailingYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, cookie_browser=CookieBrowser.CHROME),
        ydl_factory=CookieCopyFailingYDL,
    )

    result = downloader.download_audio("https://music.youtube.com/watch?v=abc")

    assert result.path == Path("D:/music/abc.mp3")
    assert CookieCopyFailingYDL.calls[0]["cookiesfrombrowser"] == ("chrome",)
    assert "cookiesfrombrowser" not in CookieCopyFailingYDL.calls[1]


def test_download_can_disable_remote_js_components(tmp_path: Path) -> None:
    FakeYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, allow_remote_js_components=False),
        ydl_factory=FakeYDL,
    )

    downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert "remote_components" not in FakeYDL.calls[-1]
