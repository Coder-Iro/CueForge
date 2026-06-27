from pathlib import Path

from cueforge.download import DownloadConfig, DownloadProgress, YTDLPDownloader


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


class PlaylistYDL(FakeYDL):
    calls: list[dict] = []

    def extract_info(self, url: str, download: bool = False) -> dict:
        assert url == "https://www.youtube.com/playlist?list=PL123"
        return {
            "_type": "playlist",
            "entries": [
                {"id": "abc", "ie_key": "Youtube"},
                None,
                {"webpage_url": "https://www.youtube.com/watch?v=def"},
                {"title": "unavailable"},
            ],
        }


class IncompletePlaylistYDL(FakeYDL):
    calls: list[dict] = []

    def extract_info(self, url: str, download: bool = False) -> dict:
        assert url == "https://www.youtube.com/playlist?list=PL123"
        if self.options.get("extractor_args"):
            entries = [{"id": f"retry-{index}", "ie_key": "Youtube"} for index in range(4)]
        else:
            entries = [{"id": "first-page", "ie_key": "Youtube"}]
        return {"_type": "playlist", "playlist_count": 5, "entries": entries}


def test_fetch_info_uses_ytdlp_without_download(tmp_path: Path) -> None:
    FakeYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path),
        ydl_factory=FakeYDL,
    )

    info = downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert info["id"] == "abc"
    assert "cookiesfrombrowser" not in FakeYDL.calls[-1]
    assert FakeYDL.calls[-1]["remote_components"] == ["ejs:github"]
    assert FakeYDL.calls[-1]["noplaylist"] is True


def test_expand_playlist_flattens_entries_and_skips_unavailable_items(tmp_path: Path) -> None:
    PlaylistYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path),
        ydl_factory=PlaylistYDL,
    )

    result = downloader.expand_playlist("https://www.youtube.com/playlist?list=PL123")

    assert PlaylistYDL.calls[-1]["extract_flat"] == "in_playlist"
    assert PlaylistYDL.calls[-1]["ignoreerrors"] is True
    assert PlaylistYDL.calls[-1]["noplaylist"] is False
    assert PlaylistYDL.calls[-1]["playlist_items"] is None
    assert PlaylistYDL.calls[-1]["playlistend"] is None
    assert PlaylistYDL.calls[-1]["playliststart"] == 1
    assert result.urls == [
        "https://www.youtube.com/watch?v=abc",
        "https://www.youtube.com/watch?v=def",
    ]
    assert result.skipped_count == 2


def test_expand_playlist_retries_youtube_pagination_with_skip_webpage(tmp_path: Path) -> None:
    IncompletePlaylistYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path),
        ydl_factory=IncompletePlaylistYDL,
    )

    result = downloader.expand_playlist("https://www.youtube.com/playlist?list=PL123")

    assert len(IncompletePlaylistYDL.calls) == 2
    assert IncompletePlaylistYDL.calls[-1]["extractor_args"] == {"youtubetab": {"skip": ["webpage"]}}
    assert result.expected_count == 5
    assert result.urls == [
        "https://www.youtube.com/watch?v=retry-0",
        "https://www.youtube.com/watch?v=retry-1",
        "https://www.youtube.com/watch?v=retry-2",
        "https://www.youtube.com/watch?v=retry-3",
    ]


def test_fetch_info_uses_cookie_file(tmp_path: Path) -> None:
    FakeYDL.calls.clear()
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, cookie_file=cookie_file),
        ydl_factory=FakeYDL,
    )

    downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert FakeYDL.calls[-1]["cookiefile"] == str(cookie_file)
    assert "cookiesfrombrowser" not in FakeYDL.calls[-1]


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


def test_download_audio_uses_cookie_file(tmp_path: Path) -> None:
    FakeYDL.calls.clear()
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, cookie_file=cookie_file),
        ydl_factory=FakeYDL,
    )

    downloader.download_audio("https://music.youtube.com/watch?v=abc")

    assert FakeYDL.calls[-1]["cookiefile"] == str(cookie_file)


def test_download_can_disable_remote_js_components(tmp_path: Path) -> None:
    FakeYDL.calls.clear()
    downloader = YTDLPDownloader(
        DownloadConfig(output_dir=tmp_path, allow_remote_js_components=False),
        ydl_factory=FakeYDL,
    )

    downloader.fetch_info("https://music.youtube.com/watch?v=abc")

    assert "remote_components" not in FakeYDL.calls[-1]
