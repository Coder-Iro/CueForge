import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QItemSelectionModel, QPoint, QRect, QSettings, Qt
from PySide6.QtWidgets import QApplication, QHeaderView, QWidget

from cueforge.download import PlaylistExpansionResult
from cueforge.gui.main_window import OnboardingDialog, MainWindow, _cover_source_from_url, _dependency_setup_status, _extract_urls, _supported_urls
from cueforge.models import ErrorCategory, DownloadStatus, MetadataCandidate, TrackMetadata
from cueforge.runtime import DependencyStatus


def _test_settings(tmp_path) -> QSettings:
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


class _FakeYTMusicPlaylistClient:
    def __init__(self, *, track_count: int) -> None:
        self.track_count = track_count

    def get_playlist(self, playlist_id: str, limit: int | None = 100) -> dict:
        assert playlist_id == "PLBIG"
        assert limit is None
        return {"tracks": [{"videoId": f"ytmusic-{index}"} for index in range(self.track_count)]}


class _FakeYTMusicLikedClient:
    def __init__(self, *, track_count: int) -> None:
        self.track_count = track_count
        self.get_playlist_calls: list[str] = []

    def get_liked_songs(self, limit: int | None = 100) -> dict:
        assert limit is None
        return {"tracks": [{"videoId": f"liked-{index}"} for index in range(self.track_count)]}

    def get_playlist(self, playlist_id: str, limit: int | None = 100) -> dict:
        self.get_playlist_calls.append(playlist_id)
        return {"tracks": []}


def test_main_window_can_queue_url(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://music.youtube.com/watch?v=abc")
        window._add_url()

        assert window.table.rowCount() == 1
        job = next(iter(window.jobs.values()))
        assert job.status == DownloadStatus.PENDING
        assert window.table.item(0, 2).text() == "YouTube Music"
        assert window.table.item(0, 3).text() == "https://music.youtube.com/watch?v=abc"
    finally:
        window.close()
        app.processEvents()


def test_queue_url_column_can_be_resized_narrower(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.resize(900, 520)
        window.show()
        app.processEvents()

        header = window.table.horizontalHeader()
        assert header.sectionResizeMode(3) == QHeaderView.ResizeMode.Interactive
        assert header.sectionResizeMode(6) == QHeaderView.ResizeMode.Stretch
        assert header.sectionSize(3) >= 320

        header.resizeSection(3, 80)
        app.processEvents()

        assert header.sectionSize(3) == 80
    finally:
        window.close()
        app.processEvents()


def test_main_window_can_queue_multiple_pasted_urls(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText(
            "\n".join(
                [
                    "https://youtu.be/8k8aIaoOA40?si=MDMx7eVZ1-Dr949J",
                    "https://youtu.be/7B4yU08Bs5A?si=JTpGDd5Z71F-Bysg",
                    "https://soundcloud.com/artist/track, https://music.youtube.com/watch?v=abc.",
                ]
            )
        )
        window._add_url()

        assert window.table.rowCount() == 4
        assert [window.jobs[job_id].status for job_id in window.row_job_ids] == [DownloadStatus.PENDING] * 4
        assert window.table.item(0, 3).text() == "https://youtu.be/8k8aIaoOA40?si=MDMx7eVZ1-Dr949J"
        assert window.table.item(1, 3).text() == "https://youtu.be/7B4yU08Bs5A?si=JTpGDd5Z71F-Bysg"
        assert window.table.item(2, 3).text() == "https://soundcloud.com/artist/track"
        assert window.table.item(3, 3).text() == "https://music.youtube.com/watch?v=abc"
        assert window.queue_status_label.text().startswith("4개 트랙")
    finally:
        window.close()
        app.processEvents()


def test_main_window_queues_playlist_without_expanding(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    calls: list[str] = []

    def expand_playlist(url: str) -> PlaylistExpansionResult:
        calls.append(url)
        return PlaylistExpansionResult(
            urls=[
                "https://www.youtube.com/watch?v=abc",
                "https://www.youtube.com/watch?v=def",
            ],
            skipped_count=1,
        )

    window = MainWindow(settings=_test_settings(tmp_path), playlist_expander=expand_playlist)
    try:
        window.url_input.setText("https://www.youtube.com/playlist?list=PL123")
        window._add_url()

        assert calls == []
        assert window.table.rowCount() == 1
        assert window.table.item(0, 3).text() == "https://www.youtube.com/playlist?list=PL123"
        assert window.queue_status_label.text().startswith("1개 트랙")
    finally:
        window.close()
        app.processEvents()


def test_main_window_flattens_playlist_when_analysis_starts(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    calls: list[str] = []
    analyzed: list[str] = []

    def expand_playlist(url: str) -> PlaylistExpansionResult:
        calls.append(url)
        return PlaylistExpansionResult(
            urls=[
                "https://www.youtube.com/watch?v=abc",
                "https://www.youtube.com/watch?v=def",
            ],
            skipped_count=1,
        )

    window = MainWindow(settings=_test_settings(tmp_path), playlist_expander=expand_playlist)
    try:
        window.url_input.setText("https://www.youtube.com/playlist?list=PL123")
        window._add_url()

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False, continue_queue=True, worker_mode=None):
            analyzed.append(job.url)

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)
        window._analyze_next()
        window._wait_for_workers(1000)
        app.processEvents()

        assert calls == ["https://www.youtube.com/playlist?list=PL123"]
        assert analyzed == ["https://www.youtube.com/watch?v=abc"]
        assert window.table.rowCount() == 3
        assert window.table.item(0, 0).text() == "완료"
        assert window.table.item(0, 3).text() == "https://www.youtube.com/playlist?list=PL123"
        assert window.table.item(1, 3).text() == "https://www.youtube.com/watch?v=abc"
        assert window.table.item(2, 3).text() == "https://www.youtube.com/watch?v=def"
    finally:
        window.close()
        app.processEvents()


def test_main_window_keeps_playlist_job_and_inserts_items_after_it(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    def expand_playlist(url: str) -> PlaylistExpansionResult:
        assert url == "https://music.youtube.com/playlist?list=LM"
        return PlaylistExpansionResult(
            urls=[
                "https://music.youtube.com/watch?v=abc",
                "https://music.youtube.com/watch?v=def",
            ],
        )

    window = MainWindow(settings=_test_settings(tmp_path), playlist_expander=expand_playlist)
    try:
        playlist_job, _row = window._insert_job("https://music.youtube.com/playlist?list=LM", output_dir=tmp_path)
        playlist_job.status = DownloadStatus.FAILED
        window._update_row(playlist_job)

        replacement_jobs = window._prepare_playlist_job_for_analysis(playlist_job)

        assert playlist_job.id in window.jobs
        assert playlist_job.status == DownloadStatus.DONE
        assert [job.url for job in replacement_jobs] == [
            "https://music.youtube.com/watch?v=abc",
            "https://music.youtube.com/watch?v=def",
        ]
        assert window.table.rowCount() == 3
        assert window.table.item(0, 3).text() == "https://music.youtube.com/playlist?list=LM"
        assert window.table.item(1, 3).text() == "https://music.youtube.com/watch?v=abc"
        assert window.table.item(2, 3).text() == "https://music.youtube.com/watch?v=def"
    finally:
        window.close()
        app.processEvents()


def test_main_window_liked_music_playlist_failure_mentions_account_auth(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    def expand_playlist(url: str) -> PlaylistExpansionResult:
        raise RuntimeError("ERROR: [youtube:tab] LM: YouTube said: The playlist does not exist.")

    window = MainWindow(settings=_test_settings(tmp_path), playlist_expander=expand_playlist)
    try:
        playlist_job, _row = window._insert_job("https://music.youtube.com/playlist?list=LM", output_dir=tmp_path)
        playlist_job.status = DownloadStatus.FAILED
        window._update_row(playlist_job)

        replacement_jobs = window._prepare_playlist_job_for_analysis(playlist_job)

        assert replacement_jobs == []
        assert playlist_job.id in window.jobs
        assert "좋아요 표시한 음악" in playlist_job.error
        assert "Google 계정" in playlist_job.error
        assert "cookies.txt" in playlist_job.error
    finally:
        window.close()
        app.processEvents()


def test_main_window_liked_music_playlist_uses_ytdlp_expander(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    calls: list[str] = []

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        def expand_with_ytdlp(url: str, *, output_dir: object, **_kwargs: object) -> PlaylistExpansionResult:
            calls.append(url)
            return PlaylistExpansionResult(urls=[f"https://music.youtube.com/watch?v=video-{index}" for index in range(483)])

        window._expand_playlist_with_ytdlp = expand_with_ytdlp
        window._ytmusic_oauth_connected = lambda: False

        result = window._expand_playlist("https://music.youtube.com/playlist?list=LM", output_dir=tmp_path)

        assert calls == ["https://music.youtube.com/playlist?list=LM"]
        assert len(result.urls) == 483
        assert result.urls[0] == "https://music.youtube.com/watch?v=video-0"
        assert result.urls[-1] == "https://music.youtube.com/watch?v=video-482"
    finally:
        window.close()
        app.processEvents()


def test_main_window_oauth_skips_ytdlp_for_youtube_playlist(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._ytmusic_oauth_connected = lambda: True
        window._expand_playlist_with_ytdlp = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("yt-dlp should not be used when OAuth is connected")
        )
        window._expand_playlist_with_youtube_data_api = lambda playlist_id: PlaylistExpansionResult(
            urls=[f"https://music.youtube.com/watch?v={playlist_id}-track"]
        )

        result = window._expand_playlist("https://www.youtube.com/playlist?list=PLPRIVATE", output_dir=tmp_path)

        assert result.urls == ["https://music.youtube.com/watch?v=PLPRIVATE-track"]
    finally:
        window.close()
        app.processEvents()


def test_main_window_www_liked_music_playlist_falls_back_to_ytmusicapi_on_ytdlp_error(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    calls: list[str] = []

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        def expand_with_ytdlp(url: str, *, output_dir: object, **_kwargs: object) -> PlaylistExpansionResult:
            calls.append(url)
            raise RuntimeError("ERROR: [youtube:tab] LM: YouTube said: The playlist does not exist.")

        liked_client = _FakeYTMusicLikedClient(track_count=3)
        window._expand_playlist_with_ytdlp = expand_with_ytdlp
        window._create_ytmusic_client = lambda **_kwargs: liked_client
        window._ytmusic_oauth_connected = lambda: False

        result = window._expand_playlist("https://www.youtube.com/playlist?list=LM", output_dir=tmp_path)

        assert calls == ["https://www.youtube.com/playlist?list=LM"]
        assert result.urls == [
            "https://music.youtube.com/watch?v=liked-0",
            "https://music.youtube.com/watch?v=liked-1",
            "https://music.youtube.com/watch?v=liked-2",
        ]
        assert liked_client.get_playlist_calls == []
    finally:
        window.close()
        app.processEvents()


def test_main_window_www_liked_music_playlist_falls_back_when_ytdlp_caps_at_100(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._expand_playlist_with_ytdlp = lambda url, output_dir, **_kwargs: PlaylistExpansionResult(
            urls=[f"https://www.youtube.com/watch?v=track-{index}" for index in range(100)],
            expected_count=100,
        )
        window._create_ytmusic_client = lambda **_kwargs: _FakeYTMusicLikedClient(track_count=438)
        window._ytmusic_oauth_connected = lambda: False

        result = window._expand_playlist("https://www.youtube.com/playlist?list=LM", output_dir=tmp_path)

        assert len(result.urls) == 438
        assert result.urls[0] == "https://music.youtube.com/watch?v=liked-0"
        assert result.urls[-1] == "https://music.youtube.com/watch?v=liked-437"
    finally:
        window.close()
        app.processEvents()


def test_main_window_liked_music_oauth_uses_youtube_data_api(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._ytmusic_oauth_connected = lambda: True
        window._expand_playlist_with_youtube_data_api = lambda playlist_id: PlaylistExpansionResult(
            urls=[f"https://music.youtube.com/watch?v={playlist_id.lower()}"]
        )
        window._create_ytmusic_client = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("ytmusicapi should not be used"))

        result = window._expand_playlist_with_ytmusicapi("https://www.youtube.com/playlist?list=LM")

        assert result.urls == ["https://music.youtube.com/watch?v=lm"]
    finally:
        window.close()
        app.processEvents()


def test_main_window_private_playlist_oauth_uses_youtube_data_api(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._ytmusic_oauth_connected = lambda: True
        window._expand_playlist_with_youtube_data_api = lambda playlist_id: PlaylistExpansionResult(
            urls=[f"https://music.youtube.com/watch?v={playlist_id}-track"]
        )
        window._create_ytmusic_client = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("ytmusicapi should not be used"))

        result = window._expand_playlist_with_ytmusicapi("https://www.youtube.com/playlist?list=PLPRIVATE")

        assert result.urls == ["https://music.youtube.com/watch?v=PLPRIVATE-track"]
    finally:
        window.close()
        app.processEvents()


def test_main_window_liked_music_youtube_data_api_paginates(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    client_file = tmp_path / "google_oauth_client.json"
    client_file.write_text(
        '{"installed": {"client_id": "client.apps.googleusercontent.com", "client_secret": "secret"}}',
        encoding="utf-8",
    )
    token_file = tmp_path / "ytmusic_oauth_token.json"
    token_file.write_text(
        '{"access_token": "access", "refresh_token": "refresh", "expires_at": 9999999999, "scope": "https://www.googleapis.com/auth/youtube", "token_type": "Bearer"}',
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200
        text = ""

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class Session:
        def get(self, url: str, *, headers: dict[str, str], params: dict[str, object], timeout: int) -> Response:
            calls.append({"url": url, "headers": headers, "params": dict(params), "timeout": timeout})
            if len(calls) == 1:
                return Response(
                    {
                        "items": [
                            {"contentDetails": {"videoId": "a"}},
                            {"contentDetails": {"videoId": "b"}},
                        ],
                        "nextPageToken": "next",
                        "pageInfo": {"totalResults": 3},
                    }
                )
            return Response({"items": [{"contentDetails": {"videoId": "c"}}], "pageInfo": {"totalResults": 3}})

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._ytmusic_oauth_client_file = lambda: client_file
        window._ytmusic_oauth_token_file = lambda: token_file

        result = window._expand_liked_music_with_youtube_data_api(Session())

        assert result.urls == [
            "https://music.youtube.com/watch?v=a",
            "https://music.youtube.com/watch?v=b",
            "https://music.youtube.com/watch?v=c",
        ]
        assert result.expected_count == 3
        assert calls[0]["url"] == "https://www.googleapis.com/youtube/v3/playlistItems"
        assert calls[0]["headers"] == {"Authorization": "Bearer access"}
        assert calls[0]["params"] == {"part": "contentDetails", "playlistId": "LM", "maxResults": 50}
        assert calls[1]["params"] == {
            "part": "contentDetails",
            "playlistId": "LM",
            "maxResults": 50,
            "pageToken": "next",
        }
    finally:
        window.close()
        app.processEvents()


def test_main_window_playlist_youtube_data_api_paginates(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    client_file = tmp_path / "google_oauth_client.json"
    client_file.write_text(
        '{"installed": {"client_id": "client.apps.googleusercontent.com", "client_secret": "secret"}}',
        encoding="utf-8",
    )
    token_file = tmp_path / "ytmusic_oauth_token.json"
    token_file.write_text(
        '{"access_token": "access", "refresh_token": "refresh", "expires_at": 9999999999, "scope": "https://www.googleapis.com/auth/youtube", "token_type": "Bearer"}',
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200
        text = ""

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class Session:
        def get(self, url: str, *, headers: dict[str, str], params: dict[str, object], timeout: int) -> Response:
            calls.append({"url": url, "headers": headers, "params": dict(params), "timeout": timeout})
            if len(calls) == 1:
                return Response(
                    {
                        "items": [
                            {"contentDetails": {"videoId": "a"}},
                            {"contentDetails": {"videoId": "b"}},
                        ],
                        "nextPageToken": "next",
                        "pageInfo": {"totalResults": 3},
                    }
                )
            return Response({"items": [{"contentDetails": {"videoId": "c"}}], "pageInfo": {"totalResults": 3}})

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._ytmusic_oauth_client_file = lambda: client_file
        window._ytmusic_oauth_token_file = lambda: token_file

        result = window._expand_playlist_with_youtube_data_api("VLPLPRIVATE", Session())

        assert result.urls == [
            "https://music.youtube.com/watch?v=a",
            "https://music.youtube.com/watch?v=b",
            "https://music.youtube.com/watch?v=c",
        ]
        assert result.expected_count == 3
        assert calls[0]["url"] == "https://www.googleapis.com/youtube/v3/playlistItems"
        assert calls[0]["headers"] == {"Authorization": "Bearer access"}
        assert calls[0]["params"] == {
            "part": "contentDetails",
            "playlistId": "PLPRIVATE",
            "maxResults": 50,
        }
        assert calls[1]["params"] == {
            "part": "contentDetails",
            "playlistId": "PLPRIVATE",
            "maxResults": 50,
            "pageToken": "next",
        }
    finally:
        window.close()
        app.processEvents()


def test_main_window_liked_music_empty_ytdlp_result_fails_when_fallback_fails(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._expand_playlist_with_ytdlp = lambda url, output_dir, **_kwargs: PlaylistExpansionResult(urls=[])
        window._expand_playlist_with_ytmusicapi = lambda url, **_kwargs: (_ for _ in ()).throw(RuntimeError("oauth failed"))
        window._ytmusic_oauth_connected = lambda: False

        with pytest.raises(RuntimeError, match="oauth failed"):
            window._expand_playlist("https://www.youtube.com/playlist?list=LM", output_dir=tmp_path)
    finally:
        window.close()
        app.processEvents()


def test_main_window_youtube_music_playlist_falls_back_when_ytdlp_result_count_is_100(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._expand_playlist_with_ytdlp = lambda url, output_dir, **_kwargs: PlaylistExpansionResult(
            urls=[f"https://music.youtube.com/watch?v=track-{index}" for index in range(100)]
        )
        window._ytmusic_oauth_connected = lambda: False
        window._create_ytmusic_client = lambda **_kwargs: _FakeYTMusicPlaylistClient(track_count=483)

        result = window._expand_playlist("https://music.youtube.com/playlist?list=PLBIG", output_dir=tmp_path)

        assert len(result.urls) == 483
        assert result.urls[0] == "https://music.youtube.com/watch?v=ytmusic-0"
        assert result.urls[-1] == "https://music.youtube.com/watch?v=ytmusic-482"
    finally:
        window.close()
        app.processEvents()


def test_main_window_regular_youtube_playlist_can_use_ytmusicapi_fallback_when_ytdlp_is_incomplete(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._expand_playlist_with_ytdlp = lambda url, output_dir, **_kwargs: PlaylistExpansionResult(
            urls=[f"https://www.youtube.com/watch?v=track-{index}" for index in range(100)],
            expected_count=438,
        )
        window._ytmusic_oauth_connected = lambda: False
        window._create_ytmusic_client = lambda **_kwargs: _FakeYTMusicPlaylistClient(track_count=438)

        result = window._expand_playlist("https://www.youtube.com/playlist?list=PLBIG", output_dir=tmp_path)

        assert len(result.urls) == 438
        assert result.urls[0] == "https://music.youtube.com/watch?v=ytmusic-0"
        assert result.urls[-1] == "https://music.youtube.com/watch?v=ytmusic-437"
    finally:
        window.close()
        app.processEvents()


def test_main_window_keeps_incomplete_regular_youtube_playlist_as_failed(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    def expand_playlist(url: str) -> PlaylistExpansionResult:
        return PlaylistExpansionResult(
            urls=[f"https://www.youtube.com/watch?v=video-{index}" for index in range(100)],
            expected_count=303,
        )

    window = MainWindow(settings=_test_settings(tmp_path), playlist_expander=expand_playlist)
    try:
        playlist_job, _row = window._insert_job("https://www.youtube.com/playlist?list=PLBIG", output_dir=tmp_path)

        replacement_jobs = window._prepare_playlist_job_for_analysis(playlist_job)

        assert replacement_jobs == []
        assert playlist_job.id in window.jobs
        assert playlist_job.status == DownloadStatus.FAILED
        assert "303개 중 100개" in playlist_job.error
        assert window.table.rowCount() == 1
    finally:
        window.close()
        app.processEvents()


def test_main_window_does_not_analyze_original_playlist_after_expansion_failure(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    analyzed: list[str] = []

    def expand_playlist(url: str) -> PlaylistExpansionResult:
        return PlaylistExpansionResult(
            urls=[f"https://www.youtube.com/watch?v=video-{index}" for index in range(100)],
            expected_count=438,
        )

    window = MainWindow(settings=_test_settings(tmp_path), playlist_expander=expand_playlist)
    try:
        window._insert_job("https://www.youtube.com/playlist?list=PLBIG", output_dir=tmp_path)

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False, continue_queue=True):
            analyzed.append(job.url)

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)
        window._analyze_next()
        window._wait_for_workers(1000)
        app.processEvents()

        assert analyzed == []
        job = next(iter(window.jobs.values()))
        assert job.status == DownloadStatus.FAILED
        assert "438개 중 100개" in job.error
    finally:
        window.close()
        app.processEvents()


def test_main_window_stops_processing_after_youtube_rate_limit(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        job, _row = window._insert_job("https://music.youtube.com/watch?v=abc", output_dir=tmp_path)
        window.worker_mode = "process"

        window._on_job_failed(job.id, "The current session has been rate-limited by YouTube")

        assert job.status == DownloadStatus.FAILED
        assert job.error_category == ErrorCategory.RATE_LIMITED.value
        assert window.worker_mode == "canceled"
        assert window.cancel_requested is True
        assert "남은 작업 시작을 중지" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_queue_action_buttons_do_not_overlap(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.resize(520, 480)
        window.show()
        app.processEvents()

        assert window.add_url_button is not None
        assert window.start_queue_button is not None
        assert window.download_approved_button is not None
        assert window.review_selected_button is not None
        assert window.analyze_selected_button is not None
        assert window.download_selected_button is not None
        assert window.retry_selected_button is not None
        assert window.retry_failed_button is not None
        assert window.remove_done_button is not None
        assert window.remove_selected_button is not None
        assert window.cancel_current_button is not None

        button_rects = [
            QRect(button.mapTo(window, QPoint(0, 0)), button.size())
            for button in (
                window.add_url_button,
                window.start_queue_button,
                window.download_approved_button,
                window.cancel_current_button,
                window.analyze_selected_button,
                window.download_selected_button,
                window.retry_selected_button,
                window.review_selected_button,
                window.retry_failed_button,
                window.remove_done_button,
                window.remove_selected_button,
            )
        ]

        for index, rect in enumerate(button_rects):
            for other in button_rects[index + 1 :]:
                assert not rect.intersects(other)
    finally:
        window.close()
        app.processEvents()


def test_review_tab_gives_queue_and_provider_tables_room(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.resize(900, 760)
        window.tabs.setCurrentIndex(window.review_tab_index)
        window.show()
        app.processEvents()

        assert window.review_scroll_area is not None
        assert window.review_scroll_area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        assert window.review_splitter is not None
        assert window.review_splitter.count() == 3
        assert window.review_splitter.handleWidth() >= 10

        window.review_splitter.setSizes([100, 120, 520])
        app.processEvents()

        sizes = window.review_splitter.sizes()
        assert sizes[0] <= 130
        assert sizes[1] <= 160
        assert sizes[2] >= 400
    finally:
        window.close()
        app.processEvents()


def test_tag_editor_places_cover_preview_beside_fields(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.resize(1000, 900)
        window.tabs.setCurrentIndex(window.review_tab_index)
        window.show()
        app.processEvents()

        assert window.review_splitter is not None
        window.review_splitter.setSizes([90, 110, 700])
        app.processEvents()

        title_field = window.review_fields["title"]
        artist_field = window.review_fields["artist"]
        album_field = window.review_fields["album"]
        cover_url_field = window.review_fields["cover_url"]
        title_rect = QRect(title_field.mapTo(window, QPoint(0, 0)), title_field.size())
        artist_rect = QRect(artist_field.mapTo(window, QPoint(0, 0)), artist_field.size())
        album_rect = QRect(album_field.mapTo(window, QPoint(0, 0)), album_field.size())
        cover_url_rect = QRect(cover_url_field.mapTo(window, QPoint(0, 0)), cover_url_field.size())
        cover_rect = QRect(window.cover_preview_label.mapTo(window, QPoint(0, 0)), window.cover_preview_label.size())

        assert window.tag_fields_panel is not None
        assert artist_rect.left() > title_rect.right()
        assert abs(artist_rect.top() - title_rect.top()) <= 4
        assert album_rect.top() > title_rect.bottom()
        assert cover_url_rect.left() <= title_rect.left() + 4
        assert cover_url_rect.right() >= artist_rect.right() - 4
        assert cover_url_rect.top() > album_rect.bottom()
        assert cover_rect.left() > title_rect.right()
        assert cover_rect.top() <= title_rect.top() + 40
    finally:
        window.close()
        app.processEvents()


def test_review_tab_hides_secondary_details_until_needed(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.REVIEW_REQUIRED
        job.selected_metadata = TrackMetadata(title="Fallback", artist="Uploader")
        job.source_title = "Original Video Title"
        job.source_channel = "Original Channel"
        job.candidates = [
            MetadataCandidate(
                provider="musicbrainz",
                score=0.70,
                matched_fields=("title",),
                metadata=TrackMetadata(title="Candidate A", artist="Artist A"),
            )
        ]

        window.resize(900, 760)
        window.tabs.setCurrentIndex(window.review_tab_index)
        window.show()
        window._load_job_for_review(job)
        app.processEvents()

        assert window.source_details_group is not None
        assert window.source_fields_panel is not None
        assert window.candidate_preview_group is not None
        assert window.source_details_group.isChecked() is False
        assert window.source_fields_panel.isVisible() is False
        assert window.candidate_preview_group.isVisible() is False
        assert "https://youtu.be/abc" not in window.review_state_label.text()

        window.source_details_group.setChecked(True)
        window.candidate_table.selectRow(0)
        app.processEvents()

        assert window.source_fields_panel.isVisible() is True
        assert window.source_title_input.text() == "Original Video Title"
        assert window.candidate_preview_group.isVisible() is True
    finally:
        window.close()
        app.processEvents()


def test_main_window_marks_soundcloud_source(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://soundcloud.com/artist/track")
        window._add_url()

        assert window.table.rowCount() == 1
        assert window.table.item(0, 2).text() == "SoundCloud"
        assert window.table.item(0, 3).text() == "https://soundcloud.com/artist/track"
    finally:
        window.close()
        app.processEvents()


def test_main_window_has_audio_recognition_settings(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        assert window.audio_recognition_checkbox.isChecked() is True
        assert window.verify_auto_approved_checkbox.isChecked() is False
        window.acoustid_key_input.setText("client-key")
        window.cookie_file_input.setText("C:\\cookies.txt")
        window.fpcalc_path_input.setText("C:\\tools\\fpcalc.exe")

        assert window.acoustid_key_input.text() == "client-key"
        assert window.cookie_file_input.text() == "C:\\cookies.txt"
        assert window.fpcalc_path_input.text() == "C:\\tools\\fpcalc.exe"
        assert "AcoustID 설정됨" in window.dependency_status_label.text()
        assert "쿠키 파일 설정됨" in window.dependency_status_label.text()
    finally:
        window.close()
        app.processEvents()


def test_main_window_defaults_output_dir_to_user_downloads_cueforge(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    expected = tmp_path / "Downloads" / "CueForge"
    monkeypatch.setattr("cueforge.gui.main_window.default_output_dir", lambda: expected)
    monkeypatch.setattr("cueforge.gui.main_window.legacy_cwd_output_dir", lambda: tmp_path / "project" / "downloads")

    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        assert window.output_dir_input.text() == str(expected)
    finally:
        window.close()
        app.processEvents()


def test_main_window_migrates_legacy_cwd_download_default(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    legacy = tmp_path / "project" / "downloads"
    expected = tmp_path / "Downloads" / "CueForge"
    settings.setValue("paths/output_dir", str(legacy))
    monkeypatch.setattr("cueforge.gui.main_window.default_output_dir", lambda: expected)
    monkeypatch.setattr("cueforge.gui.main_window.legacy_cwd_output_dir", lambda: legacy)

    window = MainWindow(settings=settings)
    try:
        assert window.output_dir_input.text() == str(expected)
        assert settings.value("paths/output_dir") == str(expected)
    finally:
        window.close()
        app.processEvents()


def test_main_window_keeps_custom_saved_output_dir(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    custom = tmp_path / "Music"
    settings.setValue("paths/output_dir", str(custom))
    monkeypatch.setattr("cueforge.gui.main_window.default_output_dir", lambda: tmp_path / "Downloads" / "CueForge")
    monkeypatch.setattr("cueforge.gui.main_window.legacy_cwd_output_dir", lambda: tmp_path / "project" / "downloads")

    window = MainWindow(settings=settings)
    try:
        assert window.output_dir_input.text() == str(custom)
    finally:
        window.close()
        app.processEvents()


def test_main_window_persists_beta_settings(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("auth/cookie_browser", "chrome")
    settings.setValue("auth/unlock_browser_cookie_database", True)
    window = MainWindow(settings=settings)
    try:
        window.output_dir_input.setText("D:\\Music")
        window.cookie_file_input.setText("D:\\cookies.txt")
        window.verify_auto_approved_checkbox.setChecked(True)
        window.acoustid_key_input.setText("client-key")
        window.save_settings()
    finally:
        window.close()
        app.processEvents()

    restored = MainWindow(settings=settings)
    try:
        assert restored.output_dir_input.text() == "D:\\Music"
        assert restored.cookie_file_input.text() == "D:\\cookies.txt"
        assert restored.verify_auto_approved_checkbox.isChecked() is True
        assert restored.acoustid_key_input.text() == "client-key"
        assert settings.value("auth/cookie_browser") is None
        assert settings.value("auth/unlock_browser_cookie_database") is None
    finally:
        restored.close()
        app.processEvents()


def test_settings_are_saved_when_leaving_settings_tab(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    window = MainWindow(settings=settings)
    try:
        window.tabs.setCurrentIndex(window.settings_tab_index)
        window.acoustid_key_input.setText("client-key")
        window.cookie_file_input.setText("D:\\cookies.txt")
        window.metadata_parallel_spin.setValue(4)

        window.tabs.setCurrentIndex(window.queue_tab_index)
        app.processEvents()

        assert settings.value("acoustid/client_key") == "client-key"
        assert settings.value("auth/cookie_file") == "D:\\cookies.txt"
        assert settings.value("scheduler/metadata_parallel") == 4
    finally:
        window.close()
        app.processEvents()


def test_first_run_opens_onboarding_and_can_complete(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    window = MainWindow(settings=settings)
    try:
        assert window.onboarding_dialog is not None
        assert window.onboarding_dialog.isVisible()

        window.onboarding_dialog._complete()
        app.processEvents()

        assert settings.value("onboarding/completed") is True
        assert window.onboarding_dialog is None
    finally:
        window.close()
        app.processEvents()


def test_first_run_onboarding_skip_does_not_mark_completed(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    window = MainWindow(settings=settings)
    try:
        assert window.onboarding_dialog is not None

        window.onboarding_dialog.reject()
        app.processEvents()

        assert settings.value("onboarding/completed", False) is False
        assert window.onboarding_dialog is None
    finally:
        window.close()
        app.processEvents()


def test_onboarding_prepares_required_assets_before_completion(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    calls: list[str] = []
    completed: list[bool] = []

    def prepare(log, progress):
        log("MiniLM 다운로드 중")
        progress(45.0)
        calls.append("prepared")

    dialog = OnboardingDialog(
        parent=parent,
        dependency_rows=[("MiniLM", "첫 실행 준비 필요")],
        optional_rows=[],
        prepare_steps=[("MiniLM 후보 평가 모델", prepare)],
        auto_prepare=False,
        on_done=lambda: completed.append(True),
    )
    try:
        dialog.show()
        assert dialog.skip_button.isHidden() is True
        assert dialog.skip_button.isEnabled() is False
        assert "필수 모델 준비 필요" in dialog.prepare_status_label.text()
        assert dialog.prepare_progress_bar.isVisible() is True

        dialog._complete()
        assert dialog.skip_button.isEnabled() is False
        assert dialog.done_button.isEnabled() is False

        deadline = time.monotonic() + 2
        while not completed and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert calls == ["prepared"]
        assert completed == [True]
        assert dialog.isVisible() is False
        assert dialog.prepare_progress_bar.value() == 100
    finally:
        dialog.close()
        parent.close()
        app.processEvents()


def test_completed_onboarding_reopens_when_required_model_is_missing(monkeypatch, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("onboarding/completed", True)
    window = MainWindow(settings=settings)
    try:
        monkeypatch.setattr("cueforge.gui.main_window.QApplication.platformName", lambda: "windows")
        monkeypatch.setattr("cueforge.gui.main_window.semantic_model_cached", lambda: False)
        monkeypatch.setattr("cueforge.gui.main_window.gemma_e2b_cached", lambda: True)

        assert window._should_open_startup_onboarding() is True
        steps = window._onboarding_prepare_steps()
        assert [label for label, _step in steps] == ["MiniLM 후보 평가 모델"]
    finally:
        window.close()
        app.processEvents()


def test_settings_can_reopen_onboarding(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("onboarding/completed", True)
    window = MainWindow(settings=settings)
    try:
        assert window.onboarding_dialog is None
        assert window.open_onboarding_button is not None

        window.open_onboarding_button.click()
        app.processEvents()

        assert window.onboarding_dialog is not None
        assert window.onboarding_dialog.isVisible()
    finally:
        window.close()
        app.processEvents()


def test_onboarding_dependency_status_marks_missing_bundled_tool_as_incomplete(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("cueforge.gui.main_window.find_executable", lambda name, explicit_path=None: DependencyStatus(name, None, "missing"))
    monkeypatch.setattr("sys.frozen", True, raising=False)

    assert "설치가 불완전함" in _dependency_setup_status("ffmpeg")


def test_onboarding_dependency_status_treats_path_tool_as_portable_fallback(monkeypatch, tmp_path) -> None:
    tool = tmp_path / "ffmpeg.exe"
    tool.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "cueforge.gui.main_window.find_executable",
        lambda name, explicit_path=None: DependencyStatus(name, tool, "PATH"),
    )
    monkeypatch.delattr("sys.frozen", raising=False)

    assert "개발/portable fallback" in _dependency_setup_status("ffmpeg")


def test_review_candidate_table_previews_then_applies_selected_candidate(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.REVIEW_REQUIRED
        job.selected_metadata = TrackMetadata(title="Fallback", artist="Uploader")
        job.candidates = [
            MetadataCandidate(
                provider="musicbrainz",
                score=0.70,
                matched_fields=("title",),
                metadata=TrackMetadata(title="Candidate A", artist="Artist A"),
            ),
            MetadataCandidate(
                provider="acoustid",
                score=0.96,
                matched_fields=("fingerprint", "title", "artist"),
                metadata=TrackMetadata(title="Candidate B", artist="Artist B", album="Album B"),
            ),
        ]
        window.table.selectRow(0)
        window._load_job_for_review(job)

        assert window.candidate_table.rowCount() == 2
        assert window.candidate_table.item(1, 0).text() == "acoustid"

        window.candidate_table.selectRow(1)
        app.processEvents()

        assert window.review_fields["title"].text() == "Fallback"
        assert window.review_fields["artist"].text() == "Uploader"
        assert job.selected_metadata.title == "Fallback"
        assert window.candidate_preview_table.rowCount() > 0
        assert window.candidate_preview_table.item(0, 1).text() == "Fallback"
        assert window.candidate_preview_table.item(0, 2).text() == "Candidate B"
        assert window.apply_candidate_button.isEnabled() is True

        window.apply_candidate_button.click()
        app.processEvents()

        assert window.review_fields["title"].text() == "Candidate B"
        assert window.review_fields["artist"].text() == "Artist B"
        assert window.review_fields["album"].text() == "Album B"
        assert job.selected_metadata.title == "Candidate B"

        job.selected_metadata = TrackMetadata(title="Fallback", artist="Uploader")
        window._set_review_fields(job.selected_metadata)
        window.candidate_table.cellDoubleClicked.emit(0, 0)
        app.processEvents()

        assert window.review_fields["title"].text() == "Candidate A"
        assert window.review_fields["artist"].text() == "Artist A"
        assert job.selected_metadata.title == "Candidate A"
    finally:
        window.close()
        app.processEvents()


def test_review_queue_lists_waiting_items_and_confidence_details(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.REVIEW_REQUIRED
        job.selected_metadata = TrackMetadata(title="Fallback", artist="Uploader")
        job.source_title = "Original Video Title"
        job.source_channel = "Original Channel"
        job.candidates = [
            MetadataCandidate(
                provider="musicbrainz",
                score=0.70,
                matched_fields=("title",),
                metadata=TrackMetadata(title="Candidate A", artist="Artist A"),
            )
        ]

        window._load_job_for_review(job)

        assert window.review_queue_table.rowCount() == 1
        assert window.review_queue_table.item(0, 0).text() == "Fallback"
        assert window.review_queue_table.item(0, 2).text() == "검수"
        assert window.source_url_input.text() == "https://youtu.be/abc"
        assert window.source_url_input.isReadOnly() is True
        assert window.source_title_input.text() == "Original Video Title"
        assert window.source_title_input.isReadOnly() is True
        assert window.source_channel_input.text() == "Original Channel"
        assert window.source_channel_input.isReadOnly() is True
        assert "점수 0.70" in window.confidence_detail_label.text()
        assert "검수 필요" in window.confidence_detail_label.text()
        assert "아티스트 충돌" in window.candidate_table.item(0, 3).text()
        assert window.candidate_table.item(0, 4).text() == "title"
    finally:
        window.close()
        app.processEvents()


def test_cover_source_infers_known_artwork_hosts() -> None:
    assert _cover_source_from_url("https://coverartarchive.org/release/rel/front-500.jpg") == "Cover Art Archive"
    assert _cover_source_from_url("https://i1.sndcdn.com/artworks-test.jpg") == "SoundCloud 기본 커버"
    assert _cover_source_from_url("https://i.ytimg.com/vi/abc/maxresdefault.jpg") == "YouTube 대체 썸네일"


def test_extract_urls_handles_pasted_text_and_de_duplicates() -> None:
    assert _extract_urls(
        "first <https://youtu.be/a>,https://youtu.be/b.\n"
        "https://youtu.be/a https://music.youtube.com/watch?v=c&si=d"
    ) == [
        "https://youtu.be/a",
        "https://youtu.be/b",
        "https://music.youtube.com/watch?v=c&si=d",
    ]


def test_extract_urls_ignores_plain_text_and_supported_urls_split() -> None:
    urls = _extract_urls("hello https://example.com https://soundcloud.com/artist/track")

    supported, unsupported = _supported_urls(urls)

    assert urls == ["https://example.com", "https://soundcloud.com/artist/track"]
    assert supported == ["https://soundcloud.com/artist/track"]
    assert unsupported == ["https://example.com"]


def test_duplicate_url_is_skipped_in_offscreen_mode(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()

        assert window.table.rowCount() == 1
    finally:
        window.close()
        app.processEvents()


def test_remove_selected_deletes_multiple_selected_queue_rows(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/one", "https://youtu.be/two", "https://youtu.be/three"):
            window.url_input.setText(url)
            window._add_url()

        selection = window.table.selectionModel()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        selection.select(window.table.model().index(0, 0), flags)
        selection.select(window.table.model().index(2, 0), flags)
        app.processEvents()

        window._refresh_actions()
        assert window.remove_selected_button.isEnabled() is True

        window._remove_selected()

        assert window.table.rowCount() == 1
        assert window.table.item(0, 3).text() == "https://youtu.be/two"
        assert [job.url for job in window.jobs.values()] == ["https://youtu.be/two"]
    finally:
        window.close()
        app.processEvents()


def test_remove_selected_bulk_deletes_large_selection(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    delete_batches: list[list[str]] = []
    original_delete_jobs = window.job_store.delete_jobs
    monkeypatch.setattr(
        window.job_store,
        "delete_jobs",
        lambda job_ids: (delete_batches.append(list(job_ids)), original_delete_jobs(job_ids)),
    )
    try:
        for index in range(120):
            window.url_input.setText(f"https://youtu.be/{index}")
            window._add_url()

        window.table.selectAll()
        app.processEvents()
        window._remove_selected()

        assert window.table.rowCount() == 0
        assert window.jobs == {}
        assert len(delete_batches) == 1
        assert len(delete_batches[0]) == 120
    finally:
        window.close()
        app.processEvents()


def test_remove_done_jobs_deletes_only_completed_queue_rows(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        done_job, _row = window._insert_job("https://youtu.be/done", output_dir=tmp_path)
        failed_job, _row = window._insert_job("https://youtu.be/failed", output_dir=tmp_path)
        pending_job, _row = window._insert_job("https://youtu.be/pending", output_dir=tmp_path)

        done_job.status = DownloadStatus.DONE
        failed_job.status = DownloadStatus.FAILED
        window._update_row(done_job)
        window._update_row(failed_job)
        window._refresh_actions()

        assert window.remove_done_button is not None
        assert window.remove_done_button.isEnabled() is True

        window._remove_done_jobs()

        assert window.table.rowCount() == 2
        assert done_job.id not in window.jobs
        assert [job.id for job in window.jobs.values()] == [failed_job.id, pending_job.id]
        assert [window.table.item(row, 3).text() for row in range(window.table.rowCount())] == [
            "https://youtu.be/failed",
            "https://youtu.be/pending",
        ]
        assert window.remove_done_button.isEnabled() is False
    finally:
        window.close()
        app.processEvents()


def test_pipeline_board_and_history_tab_reflect_job_states(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))

        assert window.pipeline_tables[DownloadStatus.PENDING].rowCount() == 1

        job.status = DownloadStatus.DONE
        job.selected_metadata = TrackMetadata(title="Done Song", artist="Done Artist")
        job.final_path = tmp_path / "Done Artist - Done Song.mp3"
        window._update_row(job)

        assert window.pipeline_tables[DownloadStatus.DONE].rowCount() == 1
        assert window.history_table.rowCount() == 1
        assert window.history_table.item(0, 1).text() == "Done Song"

        window._clear_history()

        assert window.table.rowCount() == 0
        assert window.history_table.rowCount() == 0
    finally:
        window.close()
        app.processEvents()


def test_copy_diagnostics_puts_report_on_clipboard(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    monkeypatch.setattr("cueforge.gui.main_window.format_diagnostics", lambda: "diagnostics report")
    try:
        window._copy_diagnostics()

        assert QApplication.clipboard().text() == "diagnostics report"
        assert "진단 정보가 클립보드에 복사됨" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_metadata_ready_accepts_review_state_string(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))

        window._on_metadata_ready(
            job.id,
            TrackMetadata(title="Song", artist="Artist"),
            "review_required",
            [],
        )

        assert job.status == DownloadStatus.REVIEW_REQUIRED
        assert "메타데이터 검수 필요: 검수 필요" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_auto_approved_metadata_waits_for_download_approved(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))

        window._on_metadata_ready(
            job.id,
            TrackMetadata(title="Auto Song", artist="Auto Artist"),
            "auto_approved",
            [],
        )

        assert job.status == DownloadStatus.APPROVED
        assert window.download_approved_button.isEnabled() is True
        assert "다운로드 준비 완료" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_scheduled_auto_approved_metadata_enqueues_download_after_scheduler_idle(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    queued: list[tuple[list[DownloadJob], bool]] = []

    class IdleScheduler:
        def is_running(self) -> bool:
            return False

        def enqueue_downloads(self, jobs, *, priority: bool = False) -> None:
            queued.append((list(jobs), priority))

        def set_limits(self, limits) -> None:
            return None

    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        window.scheduler = IdleScheduler()
        window._scheduled_metadata_job_ids.add(job.id)

        window._on_metadata_ready(
            job.id,
            TrackMetadata(title="Auto Song", artist="Auto Artist"),
            "auto_approved",
            [],
        )

        assert job.status == DownloadStatus.APPROVED
        assert queued == [([job], False)]
    finally:
        window.scheduler = None
        window.close()
        app.processEvents()


def test_approved_selected_track_can_move_back_to_review_queue(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.APPROVED
        job.selected_metadata = TrackMetadata(title="Approved Song", artist="Approved Artist")
        window._update_row(job)
        window.table.selectRow(0)
        window._refresh_actions()

        assert window.review_selected_button.isEnabled() is True

        window._move_selected_to_review_queue()

        assert job.status == DownloadStatus.REVIEW_REQUIRED
        assert window.table.item(0, 0).text() == "검수 필요"
        assert window.review_queue_table.rowCount() == 1
        assert window.review_queue_table.item(0, 0).text() == "Approved Song"
        assert window.active_review_job_id == job.id
        assert window.tabs.currentIndex() == window.review_tab_index
        assert window.approve_button.isEnabled() is True
    finally:
        window.close()
        app.processEvents()


def test_loaded_approved_track_can_move_back_to_review_queue(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.APPROVED
        job.selected_metadata = TrackMetadata(title="Approved Song", artist="Approved Artist")
        window._load_job_for_review(job)

        assert window.reopen_review_button.isEnabled() is True

        window._move_active_to_review_queue()

        assert job.status == DownloadStatus.REVIEW_REQUIRED
        assert window.review_queue_table.rowCount() == 1
        assert window.review_fields["title"].text() == "Approved Song"
        assert window.approve_button.isEnabled() is True
    finally:
        window.close()
        app.processEvents()


def test_done_selected_track_can_move_back_to_review_queue(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        final_path = tmp_path / "Approved Artist - Approved Song [abc].mp3"
        final_path.write_bytes(b"fake mp3")
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.DONE
        job.progress = 100.0
        job.final_path = final_path
        job.selected_metadata = TrackMetadata(title="Approved Song", artist="Approved Artist")
        window._update_row(job)
        window.table.selectRow(0)
        window._refresh_actions()

        assert window.review_selected_button.isEnabled() is True

        window._move_selected_to_review_queue()

        assert job.status == DownloadStatus.REVIEW_REQUIRED
        assert job.final_path == final_path
        assert window.table.item(0, 0).text() == "검수 필요"
        assert window.review_queue_table.rowCount() == 1
        assert window.review_queue_table.item(0, 0).text() == "Approved Song"
        assert window.active_review_job_id == job.id
        assert window.tabs.currentIndex() == window.review_tab_index
        assert window.approve_button.isEnabled() is True
        assert "기존 완료 파일" in window.review_hint_label.text()
    finally:
        window.close()
        app.processEvents()


def test_approving_reopened_done_track_retags_existing_file_without_redownload(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    started = []
    try:
        final_path = tmp_path / "Old Artist - Old Song [abc].mp3"
        final_path.write_bytes(b"fake mp3")
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.DONE
        job.progress = 100.0
        job.final_path = final_path
        job.source_id = "abc"
        job.selected_metadata = TrackMetadata(title="Old Song", artist="Old Artist")
        window._load_job_for_review(job)
        window._move_active_to_review_queue()
        window.review_fields["title"].setText("New Song")
        window.review_fields["artist"].setText("New Artist")

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False, continue_queue=True, worker_mode=None):
            started.append((job, approved_metadata, analyze_only, continue_queue, worker_mode, job.downloaded_path))

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        window._approve_selected()

        assert job.status == DownloadStatus.APPROVED
        assert job.selected_metadata.title == "New Song"
        assert job.selected_metadata.artist == "New Artist"
        assert started == [(job, job.selected_metadata, False, False, None, final_path)]
        assert "완료 파일 태그 갱신 시작" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_queue_processing_auto_downloads_approved_tracks(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/pending", "https://youtu.be/approved"):
            window.url_input.setText(url)
            window._add_url()
        pending, approved = window.jobs[window.row_job_ids[0]], window.jobs[window.row_job_ids[1]]
        approved.status = DownloadStatus.APPROVED
        approved.selected_metadata = TrackMetadata(title="Ready", artist="Artist")

        started = []

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False, worker_mode=None):
            started.append((job, approved_metadata, analyze_only, worker_mode))

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        window._analyze_next()
        window._download_next_approved()

        assert started[0] == (pending, None, False, "process")
        assert started[1][0] is approved
        assert started[1][1].title == "Ready"
        assert started[1][2:] == (False, None)
    finally:
        window.close()
        app.processEvents()


def test_selected_queue_actions_start_selected_jobs(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/failed", "https://youtu.be/pending", "https://youtu.be/approved"):
            window.url_input.setText(url)
            window._add_url()
        failed, pending, approved = [window.jobs[job_id] for job_id in window.row_job_ids]
        failed.status = DownloadStatus.FAILED
        failed.error = "old error"
        approved.status = DownloadStatus.APPROVED
        approved.selected_metadata = TrackMetadata(title="Ready", artist="Artist")

        started = []

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False, continue_queue=True):
            started.append((job, approved_metadata, analyze_only, continue_queue))

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        window.table.selectRow(1)
        window._analyze_selected()
        window.table.selectRow(2)
        window._download_selected_approved()
        window.table.selectRow(0)
        window._retry_selected()

        assert started[0] == (pending, None, False, False)
        assert started[1][0] is approved
        assert started[1][1].title == "Ready"
        assert started[1][2:] == (False, False)
        assert started[2] == (failed, None, False, False)
        assert failed.status == DownloadStatus.PENDING
        assert failed.error == ""
    finally:
        window.close()
        app.processEvents()


def test_video_unavailable_failure_is_not_retryable(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/unavailable")
        window._add_url()
        job = next(iter(window.jobs.values()))
        started = []

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False, continue_queue=True, worker_mode=None):
            started.append((job, approved_metadata, analyze_only, continue_queue, worker_mode))

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        window._on_job_failed(job.id, "ERROR: Video unavailable. This video is not available")
        window.table.selectRow(0)
        window._refresh_actions()

        assert job.status == DownloadStatus.FAILED
        assert job.error_category == ErrorCategory.VIDEO_UNAVAILABLE.value
        assert window.retry_failed_button is not None
        assert window.retry_selected_button is not None
        assert window.analyze_selected_button is not None
        assert window.retry_failed_button.isEnabled() is False
        assert window.retry_selected_button.isEnabled() is False
        assert window.analyze_selected_button.isEnabled() is False

        window._retry_failed()
        window._retry_selected()
        window._analyze_selected()

        assert started == []
        assert job.status == DownloadStatus.FAILED
        assert job.retry_count == 0
    finally:
        window.close()
        app.processEvents()


def test_cancel_current_job_requests_worker_interruption(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/running")
        window._add_url()
        job = next(iter(window.jobs.values()))

        class RunningWorker:
            def __init__(self):
                self.job = job
                self.canceled = False

            def isRunning(self):
                return True

            def cancel(self):
                self.canceled = True

        worker = RunningWorker()
        window.worker = worker
        window.cancel_requested = False

        window._cancel_current_job()

        assert worker.canceled is True
        assert window.cancel_requested is True
        assert window.cancel_current_button is not None
        assert window.cancel_current_button.isEnabled() is False
        assert "현재 작업 취소 요청됨" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_job_canceled_updates_queue_status(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/cancel")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.DOWNLOADING
        window._update_row(job)

        window._on_job_canceled(job.id)

        assert job.status == DownloadStatus.CANCELED
        assert window.table.item(0, 0).text() == "취소됨"
        assert "작업이 취소됨" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_selected_running_job_loads_review_panel_when_metadata_needs_review(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.METADATA
        window.table.selectRow(0)

        window._on_metadata_ready(
            job.id,
            TrackMetadata(title="Needs Review", artist="Detected Artist"),
            "review_required",
            [
                MetadataCandidate(
                    provider="fallback",
                    score=0.70,
                    matched_fields=("title",),
                    metadata=TrackMetadata(title="Needs Review", artist="Detected Artist"),
                )
            ],
        )

        assert window.active_review_job_id == job.id
        assert window.review_fields["title"].text() == "Needs Review"
        assert window.review_fields["artist"].text() == "Detected Artist"
        assert window.candidate_table.rowCount() == 1
        assert window.approve_button.isEnabled() is True
    finally:
        window.close()
        app.processEvents()


def test_approve_uses_loaded_review_job_without_queue_selection(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.REVIEW_REQUIRED
        job.selected_metadata = TrackMetadata(title="Review Title", artist="Review Artist")
        window._load_job_for_review(job)
        assert window.active_review_job_id == job.id
        downloads = []
        monkeypatch.setattr(window, "_download_approved_job", downloads.append)

        window._approve_selected()

        assert job.status == DownloadStatus.APPROVED
        assert job.selected_metadata.title == "Review Title"
        assert job.selected_metadata.artist == "Review Artist"
        assert downloads == [job]
    finally:
        window.close()
        app.processEvents()


def test_review_tab_loads_waiting_review_when_no_review_form_is_active(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/first", "https://youtu.be/second"):
            window.url_input.setText(url)
            window._add_url()
        first, second = window.jobs[window.row_job_ids[0]], window.jobs[window.row_job_ids[1]]
        first.status = DownloadStatus.REVIEW_REQUIRED
        first.selected_metadata = TrackMetadata(title="First Review", artist="Artist A")
        second.status = DownloadStatus.PENDING
        window.active_review_job_id = None
        window.table.selectRow(1)

        window.tabs.setCurrentIndex(window.review_tab_index)
        app.processEvents()

        assert window.active_review_job_id == first.id
        assert window.review_fields["title"].text() == "First Review"
        assert window.review_fields["artist"].text() == "Artist A"
    finally:
        window.close()
        app.processEvents()


def test_review_required_does_not_block_pending_queue_items(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/needs-review", "https://youtu.be/next"):
            window.url_input.setText(url)
            window._add_url()
        first, second = window.jobs[window.row_job_ids[0]], window.jobs[window.row_job_ids[1]]
        first.status = DownloadStatus.METADATA

        started = []

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False, worker_mode=None):
            started.append((job, approved_metadata, analyze_only, worker_mode))
            job.status = DownloadStatus.DOWNLOADING

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        window._on_metadata_ready(first.id, TrackMetadata(title="Song", artist="Artist"), "review_required", [])
        window.worker_mode = "analysis"
        window._worker_finished()

        assert first.status == DownloadStatus.REVIEW_REQUIRED
        assert second.status == DownloadStatus.DOWNLOADING
        assert started == [(second, None, False, "process")]
        assert window.tabs.currentIndex() == window.queue_tab_index
        assert window.approve_button.isEnabled() is True
        assert window.tabs.tabText(window.review_tab_index) == "검수 (1)"
    finally:
        window.close()
        app.processEvents()


def test_approve_loads_next_waiting_review_item(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/first", "https://youtu.be/second"):
            window.url_input.setText(url)
            window._add_url()
        first, second = window.jobs[window.row_job_ids[0]], window.jobs[window.row_job_ids[1]]
        first.status = DownloadStatus.REVIEW_REQUIRED
        first.selected_metadata = TrackMetadata(title="First Review", artist="Artist A")
        second.status = DownloadStatus.REVIEW_REQUIRED
        second.selected_metadata = TrackMetadata(title="Second Review", artist="Artist B")
        window._load_job_for_review(first)

        class RunningWorker:
            def isRunning(self):
                return True

        window.worker = RunningWorker()
        window._approve_selected()

        assert first.status == DownloadStatus.APPROVED
        assert window.active_review_job_id == second.id
        assert window.review_fields["title"].text() == "Second Review"
        assert window.review_fields["artist"].text() == "Artist B"
    finally:
        window.close()
        app.processEvents()


def test_review_tab_can_delete_loaded_review_item(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/first", "https://youtu.be/second"):
            window.url_input.setText(url)
            window._add_url()
        first, second = window.jobs[window.row_job_ids[0]], window.jobs[window.row_job_ids[1]]
        first.status = DownloadStatus.REVIEW_REQUIRED
        first.selected_metadata = TrackMetadata(title="First Review", artist="Artist A")
        second.status = DownloadStatus.REVIEW_REQUIRED
        second.selected_metadata = TrackMetadata(title="Second Review", artist="Artist B")
        window._update_row(first)
        window._update_row(second)
        window._load_job_for_review(first)

        assert window.remove_review_button is not None
        assert window.remove_review_button.isEnabled() is True

        window._remove_active_review_job()

        assert first.id not in window.jobs
        assert window.table.rowCount() == 1
        assert window.review_queue_table.rowCount() == 1
        assert window.active_review_job_id == second.id
        assert window.review_fields["title"].text() == "Second Review"
        assert window.review_fields["artist"].text() == "Artist B"
        assert window.tabs.tabText(window.review_tab_index) == "검수 (1)"
    finally:
        window.close()
        app.processEvents()


def test_approve_while_queue_running_queues_reviewed_job_first(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/needs-review", "https://youtu.be/next"):
            window.url_input.setText(url)
            window._add_url()
        first, second = window.jobs[window.row_job_ids[0]], window.jobs[window.row_job_ids[1]]
        first.status = DownloadStatus.REVIEW_REQUIRED
        first.selected_metadata = TrackMetadata(title="Song", artist="Artist")
        window._load_job_for_review(first)

        started = []

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False):
            started.append((job, approved_metadata, analyze_only))
            job.status = DownloadStatus.DOWNLOADING

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        class RunningWorker:
            def isRunning(self):
                return True

        window.worker = RunningWorker()
        window._approve_selected()

        assert first.status == DownloadStatus.APPROVED
        assert started == []

        window._worker_finished()

        assert started == []
        assert second.status == DownloadStatus.PENDING

        window._download_next_approved()

        assert started[0][0] is first
        assert started[0][1].title == "Song"
        assert started[0][2] is False
    finally:
        window.close()
        app.processEvents()


def test_manual_review_approval_uses_priority_download_queue(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    queued: list[tuple[list[DownloadJob], bool]] = []

    class RunningScheduler:
        def is_running(self) -> bool:
            return True

        def enqueue_downloads(self, jobs, *, priority: bool = False) -> None:
            queued.append((list(jobs), priority))

        def set_limits(self, limits) -> None:
            return None

    try:
        window.url_input.setText("https://youtu.be/reviewed")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.REVIEW_REQUIRED
        job.selected_metadata = TrackMetadata(title="Reviewed Song", artist="Reviewed Artist")
        window._load_job_for_review(job)
        window.scheduler = RunningScheduler()

        window._approve_selected()

        assert job.status == DownloadStatus.APPROVED
        assert queued == [([job], True)]
    finally:
        window.scheduler = None
        window.close()
        app.processEvents()


def test_new_review_item_does_not_replace_active_review_form(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/first", "https://youtu.be/second"):
            window.url_input.setText(url)
            window._add_url()
        first, second = window.jobs[window.row_job_ids[0]], window.jobs[window.row_job_ids[1]]
        first.status = DownloadStatus.REVIEW_REQUIRED
        first.selected_metadata = TrackMetadata(title="First", artist="Artist A")
        second.status = DownloadStatus.METADATA
        window._load_job_for_review(first)

        window._on_metadata_ready(second.id, TrackMetadata(title="Second", artist="Artist B"), "review_required", [])

        assert window.active_review_job_id == first.id
        assert window.review_fields["title"].text() == "First"
        assert second.status == DownloadStatus.REVIEW_REQUIRED
        assert window.tabs.tabText(window.review_tab_index) == "검수 (2)"
    finally:
        window.close()
        app.processEvents()
