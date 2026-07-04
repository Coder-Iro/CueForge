import os
import re
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QItemSelectionModel, QMimeData, QPoint, QRect, QSettings, Qt, QUrl
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QHeaderView, QLabel, QWidget

from cueforge.download import PlaylistExpansionResult
from cueforge.gui.main_window import (
    OnboardingDependencyRow,
    OnboardingDialog,
    MainWindow,
    UrlInput,
    _cover_source_from_url,
    _dependency_setup_status,
    _extract_urls,
    _supported_urls,
)
from cueforge.models import DownloadJob, ErrorCategory, DownloadStatus, MetadataCandidate, TrackMetadata
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
        assert window.table.item(0, 1).text() == "YouTube Music"
        assert window.table.item(0, 2).text() == "https://music.youtube.com/watch?v=abc"
        assert window.table.horizontalHeaderItem(5).text() == "BPM"
        assert window.table.item(0, 5).text() == ""
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
        assert header.sectionResizeMode(2) == QHeaderView.ResizeMode.Interactive
        assert header.sectionResizeMode(3) == QHeaderView.ResizeMode.Stretch
        assert header.sectionResizeMode(4) == QHeaderView.ResizeMode.Stretch
        assert header.sectionResizeMode(5) == QHeaderView.ResizeMode.Interactive
        assert header.sectionSize(2) >= 320

        header.resizeSection(2, 80)
        app.processEvents()

        assert header.sectionSize(2) == 80
    finally:
        window.close()
        app.processEvents()


def test_queue_table_shows_bpm_instead_of_output_path(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.selected_metadata = TrackMetadata(title="Song", artist="Artist", bpm=128)
        window._update_row(job)

        assert [window.table.horizontalHeaderItem(index).text() for index in range(window.table.columnCount())] == [
            "상태",
            "소스",
            "URL",
            "제목",
            "아티스트",
            "BPM",
        ]
        assert window.table.item(0, 5).text() == "128"
        assert str(job.output_dir) not in [window.table.item(0, index).text() for index in range(window.table.columnCount())]
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
        assert window.table.item(0, 2).text() == "https://youtu.be/8k8aIaoOA40?si=MDMx7eVZ1-Dr949J"
        assert window.table.item(1, 2).text() == "https://youtu.be/7B4yU08Bs5A?si=JTpGDd5Z71F-Bysg"
        assert window.table.item(2, 2).text() == "https://soundcloud.com/artist/track"
        assert window.table.item(3, 2).text() == "https://music.youtube.com/watch?v=abc"
        assert window.queue_status_label.text().startswith("4개 트랙")
    finally:
        window.close()
        app.processEvents()


def test_url_input_paste_appends_urls_on_separate_lines() -> None:
    widget = UrlInput()
    first = QMimeData()
    first.setText("https://youtu.be/first")
    second = QMimeData()
    second.setText("https://youtu.be/second")

    widget.insertFromMimeData(first)
    widget.insertFromMimeData(second)

    assert widget.toPlainText() == "https://youtu.be/first\nhttps://youtu.be/second\n"


def test_url_input_paste_splits_multiple_urls_onto_lines() -> None:
    widget = UrlInput()
    mime = QMimeData()
    mime.setText("youtu.be/first, music.youtube.com/watch?v=second.")

    widget.insertFromMimeData(mime)

    assert widget.toPlainText() == "https://youtu.be/first\nhttps://music.youtube.com/watch?v=second\n"


def test_url_input_accepts_dragged_url_mime_list_on_separate_lines() -> None:
    widget = UrlInput()
    mime = QMimeData()
    mime.setUrls([QUrl("https://youtu.be/first"), QUrl("https://soundcloud.com/artist/track")])

    widget.insertFromMimeData(mime)

    assert widget.toPlainText() == "https://youtu.be/first\nhttps://soundcloud.com/artist/track\n"


def test_main_window_auto_starts_processing_after_url_add_in_desktop_mode(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    started: list[bool] = []
    try:
        monkeypatch.setattr("cueforge.gui.main_window.QApplication.platformName", lambda: "windows")
        monkeypatch.setattr(window, "_start_pipeline", lambda: started.append(True))

        window.url_input.setText("https://youtu.be/abc")
        window._add_url()

        assert started == [True]
        assert "자동 처리 시작" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_url_input_enter_submits_without_inserting_newline(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")

        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        window.url_input.keyPressEvent(event)

        assert window.table.rowCount() == 1
        assert window.table.item(0, 2).text() == "https://youtu.be/abc"
        assert window.url_input.text() == ""
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
        assert window.table.item(0, 2).text() == "https://www.youtube.com/playlist?list=PL123"
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
        assert window.table.item(0, 2).text() == "https://www.youtube.com/playlist?list=PL123"
        assert window.table.item(1, 2).text() == "https://www.youtube.com/watch?v=abc"
        assert window.table.item(2, 2).text() == "https://www.youtube.com/watch?v=def"
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
        assert window.table.item(0, 2).text() == "https://music.youtube.com/playlist?list=LM"
        assert window.table.item(1, 2).text() == "https://music.youtube.com/watch?v=abc"
        assert window.table.item(2, 2).text() == "https://music.youtube.com/watch?v=def"
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
        assert "cookies.txt" not in playlist_job.error
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


def test_main_window_log_lines_include_timestamps(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        job, _row = window._insert_job("https://music.youtube.com/watch?v=abc", output_dir=tmp_path)

        first_line = window.log.toPlainText().splitlines()[0]
        assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] \[" + re.escape(job.id[:8]) + r"\] 큐에 추가됨$", first_line)
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
        assert window.start_queue_button.isHidden() is True
        assert window.download_approved_button is not None
        assert window.download_approved_button.isHidden() is True
        assert window.review_selected_button is not None
        assert window.analyze_selected_button is not None
        assert window.analyze_selected_button.isHidden() is True
        assert window.download_selected_button is not None
        assert window.download_selected_button.isHidden() is True
        assert window.retry_selected_button is not None
        assert window.retry_failed_button is not None
        assert window.remove_done_button is not None
        assert window.remove_selected_button is not None
        assert window.cancel_current_button is not None

        button_rects = [
            QRect(button.mapTo(window, QPoint(0, 0)), button.size())
            for button in (
                window.add_url_button,
                window.cancel_current_button,
                window.retry_selected_button,
                window.review_selected_button,
                window.retry_failed_button,
                window.remove_done_button,
                window.remove_selected_button,
            )
            if button.isVisible()
        ]

        for index, rect in enumerate(button_rects):
            for other in button_rects[index + 1 :]:
                assert not rect.intersects(other)
    finally:
        window.close()
        app.processEvents()


def test_review_dialog_gives_provider_and_editor_room(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        assert window.review_dialog is not None
        window.review_dialog.resize(900, 760)
        window.review_dialog.show()
        app.processEvents()

        assert window.review_scroll_area is not None
        assert window.review_scroll_area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        assert window.review_splitter is not None
        assert window.review_splitter.count() == 3
        assert window.review_splitter.handleWidth() >= 10
        assert window.review_splitter.widget(0).isHidden() is True

        window.review_splitter.setSizes([0, 240, 520])
        app.processEvents()

        assert window.review_splitter.widget(1).height() >= 220
        assert window.review_splitter.widget(2).height() >= 340
    finally:
        window.close()
        app.processEvents()


def test_review_candidate_table_and_action_button_do_not_overlap(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        assert window.review_dialog is not None
        window.review_dialog.resize(1669, 775)
        job, _row = window._insert_job("https://youtu.be/abc", output_dir=tmp_path)
        job.status = DownloadStatus.APPROVED
        job.selected_metadata = TrackMetadata(title="Fallback", artist="Uploader")
        job.candidates = [
            MetadataCandidate(
                provider="title_cover",
                score=0.78,
                matched_fields=("title", "cover", "artist"),
                metadata=TrackMetadata(
                    title="꽃에 망령",
                    artist="계화",
                    release_date="2026-06-15",
                    cover_source="platform thumbnail",
                ),
            )
        ]

        window._open_review_dialog(job)
        app.processEvents()

        assert window.apply_candidate_button is not None
        assert window.candidate_table.verticalHeader().isVisible() is False
        assert window.candidate_table.wordWrap() is False
        assert window.candidate_table.rowHeight(0) >= 32

        dialog = window.review_dialog
        table_rect = QRect(window.candidate_table.mapTo(dialog, QPoint(0, 0)), window.candidate_table.size())
        button_rect = QRect(window.apply_candidate_button.mapTo(dialog, QPoint(0, 0)), window.apply_candidate_button.size())
        provider_rect = QRect(window.review_splitter.widget(1).mapTo(dialog, QPoint(0, 0)), window.review_splitter.widget(1).size())
        tag_editor_rect = QRect(window.review_splitter.widget(2).mapTo(dialog, QPoint(0, 0)), window.review_splitter.widget(2).size())
        cover_rect = QRect(window.cover_preview_label.mapTo(dialog, QPoint(0, 0)), window.cover_preview_label.size())
        assert not table_rect.intersects(button_rect)
        assert not provider_rect.intersects(tag_editor_rect)
        assert not button_rect.intersects(cover_rect)
        assert window.review_scroll_area is not None
        assert window.review_scroll_area.widget().height() > window.review_scroll_area.viewport().height()

        required_table_height = (
            window.candidate_table.horizontalHeader().height()
            + window.candidate_table.rowHeight(0)
            + window.candidate_table.horizontalScrollBar().sizeHint().height()
            + 12
        )
        assert window.candidate_table.height() >= required_table_height
    finally:
        window.close()
        app.processEvents()


def test_tag_editor_places_cover_preview_beside_fields(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        assert window.review_dialog is not None
        window.review_dialog.resize(1000, 900)
        window.review_dialog.show()
        app.processEvents()

        assert window.review_splitter is not None
        window.review_splitter.setSizes([90, 110, 700])
        app.processEvents()

        title_field = window.review_fields["title"]
        artist_field = window.review_fields["artist"]
        album_field = window.review_fields["album"]
        cover_button = window.change_cover_url_button
        dialog = window.review_dialog
        title_rect = QRect(title_field.mapTo(dialog, QPoint(0, 0)), title_field.size())
        artist_rect = QRect(artist_field.mapTo(dialog, QPoint(0, 0)), artist_field.size())
        album_rect = QRect(album_field.mapTo(dialog, QPoint(0, 0)), album_field.size())
        cover_rect = QRect(window.cover_preview_label.mapTo(dialog, QPoint(0, 0)), window.cover_preview_label.size())
        cover_button_rect = QRect(cover_button.mapTo(dialog, QPoint(0, 0)), cover_button.size())

        assert window.tag_fields_panel is not None
        assert artist_rect.left() > title_rect.right()
        assert abs(artist_rect.top() - title_rect.top()) <= 4
        assert album_rect.top() > title_rect.bottom()
        assert window.review_fields["cover_url"].isHidden() is True
        assert cover_rect.left() > title_rect.right()
        assert cover_rect.top() <= title_rect.top() + 40
        assert cover_button is not None
        assert cover_button_rect.top() > cover_rect.bottom()
    finally:
        window.close()
        app.processEvents()


def test_cover_change_button_updates_hidden_cover_url(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        refreshed: list[TrackMetadata] = []
        monkeypatch.setattr(
            "cueforge.gui.main_window.QInputDialog.getText",
            lambda *args, **kwargs: ("https://example.com/new-cover.jpg", True),
        )
        monkeypatch.setattr(window, "_refresh_cover_preview", lambda job, metadata: refreshed.append(metadata))
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.selected_metadata = TrackMetadata(
            title="Song",
            artist="Artist",
            cover_url="https://example.com/old-cover.jpg",
            cover_path=str(tmp_path / "old-cover.jpg"),
        )
        window._load_job_for_review(job)

        window._change_cover_url()

        assert window.review_fields["cover_url"].text() == "https://example.com/new-cover.jpg"
        assert job.selected_metadata.cover_url == "https://example.com/new-cover.jpg"
        assert job.selected_metadata.cover_path == ""
        assert refreshed[-1].cover_url == "https://example.com/new-cover.jpg"
    finally:
        window.close()
        app.processEvents()


def test_review_dialog_hides_secondary_details_until_needed(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.APPROVED
        job.selected_metadata = TrackMetadata(title="Fallback", artist="Uploader")
        job.source_title = "Original Video Title"
        job.source_channel = "Original Channel"
        job.candidates = [
            MetadataCandidate(
                provider="ytmusic",
                score=0.70,
                matched_fields=("title",),
                metadata=TrackMetadata(title="Candidate A", artist="Artist A"),
            )
        ]

        assert window.review_dialog is not None
        window.review_dialog.resize(900, 760)
        window._open_review_dialog(job)
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
        assert window.table.item(0, 1).text() == "SoundCloud"
        assert window.table.item(0, 2).text() == "https://soundcloud.com/artist/track"
    finally:
        window.close()
        app.processEvents()


def test_main_window_has_oauth_metadata_settings(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "missing-openai-oauth-token.json",
    )
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.openai_model_input.setText("gpt-test")

        assert window.openai_model_input.isEditable() is False
        assert window.openai_model_input.isEnabled() is False
        assert window.openai_model_input.text() == ""
        assert "ChatGPT 미연결" in window.dependency_status_label.text()
        assert "웹검색" not in window.dependency_status_label.text()
        assert "웹검색" not in window.openai_oauth_status_label.text()
        assert "YTMusic 인증" in window.dependency_status_label.text()
    finally:
        window.close()
        app.processEvents()


def test_main_window_openai_model_combo_only_selects_catalog_models(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "missing-openai-oauth-token.json",
    )
    settings = _test_settings(tmp_path)
    settings.setValue("openai/model", "gpt-5.4-mini")
    window = MainWindow(settings=settings)
    try:
        assert window.openai_model_input.isEditable() is False
        assert window.openai_model_input.text() == ""

        window.openai_model_input.set_models(["gpt-5.5", "gpt-5.4-mini"])
        assert window.openai_model_input.text() == "gpt-5.4-mini"

        window.openai_model_input.setText("not-in-catalog")
        assert window.openai_model_input.text() == "gpt-5.4-mini"
    finally:
        window.close()
        app.processEvents()


def test_main_window_openai_model_combo_uses_default_catalog_model_when_saved_model_is_missing(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "missing-openai-oauth-token.json",
    )
    settings = _test_settings(tmp_path)
    settings.setValue("openai/model", "retired-model")
    window = MainWindow(settings=settings)
    try:
        window.openai_model_input.set_models(["gpt-5.5", "gpt-5.4-mini", "gpt-5.3"])

        assert window.openai_model_input.text() == "gpt-5.4-mini"
    finally:
        window.close()
        app.processEvents()


def test_main_window_uses_chatgpt_metadata_when_oauth_is_connected(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "openai-oauth-token.json",
    )
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._openai_oauth_connected = lambda: True
        window.openai_model_input.set_models(["gpt-5.5"])

        config = window._openai_metadata_config()

        assert config is not None
        assert config.resolved_model == "gpt-5.5"
    finally:
        window.close()
        app.processEvents()


def test_main_window_openai_status_bar_shows_model_and_compact_usage(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "openai-oauth-token.json",
    )
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._openai_oauth_connected = lambda: True
        window.openai_model_input.set_models(["gpt-5.5"])
        window._openai_quota_status_text = (
            "Codex 사용량 (dj@example.com pro)\n"
            "- 5시간 75% 남음, 1시간 후 재설정\n"
            "- 주간 60% 남음, 2일 후 재설정\n"
            "- 크레딧 553"
        )

        window._refresh_openai_status_bar()

        text = window.openai_status_bar_label.text()
        assert "gpt-5.5" in text
        assert "5시간 75% 남음" in text
        assert "주간 60% 남음" in text
        assert "크레딧 553" in text
        assert "\n" not in text
        assert "Codex 사용량" not in text
    finally:
        window.close()
        app.processEvents()


def test_main_window_openai_status_bar_uses_quota_from_chatgpt_candidate(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "openai-oauth-token.json",
    )
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._openai_oauth_connected = lambda: True
        window.openai_model_input.set_models(["gpt-5.5"])
        job, _row = window._insert_job("https://youtu.be/abc", output_dir=tmp_path)
        candidate = MetadataCandidate(
            provider="chatgpt",
            score=0.72,
            metadata=TrackMetadata(title="Song", artist="Artist"),
            raw={"quota_status": "요청 42/100 남음, 재설정 5m"},
        )

        window._on_metadata_ready(job.id, TrackMetadata(title="Song", artist="Artist"), DownloadStatus.REVIEW_REQUIRED.value, [candidate])

        assert "요청 42/100 남음" in window.openai_status_bar_label.text()
        assert window.openai_quota_status_label.text() == "요청 42/100 남음, 재설정 5m"
    finally:
        window.close()
        app.processEvents()


def test_main_window_refreshes_openai_models_and_quota_on_startup_when_connected(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "missing-openai-oauth-token.json",
    )
    window = MainWindow(settings=_test_settings(tmp_path))
    calls: list[str] = []
    try:
        monkeypatch.setattr("cueforge.gui.main_window.QApplication.platformName", lambda: "windows")
        window._openai_oauth_connected = lambda: True
        window._refresh_openai_models = lambda: calls.append("models")
        window._refresh_openai_quota = lambda *, log_result=True: calls.append(("quota", log_result))

        window._refresh_openai_account_data_on_startup()

        assert calls == ["models", ("quota", False)]
    finally:
        window.close()
        app.processEvents()


def test_main_window_can_update_openai_quota_without_log_noise(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "openai-oauth-token.json",
    )
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._openai_oauth_connected = lambda: True
        window.openai_model_input.set_models(["gpt-5.4-mini"])
        window._openai_quota_log_result = False

        window._on_openai_quota_ready("Codex 사용량 (pro)\n- 5시간 70% 남음")

        assert "5시간 70% 남음" in window.openai_status_bar_label.text()
        assert "5시간 70% 남음" in window.openai_quota_status_label.text()
        assert "Codex 사용량" not in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_main_window_hides_openai_disconnect_actions_when_disconnected(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._openai_oauth_token_file = lambda: tmp_path / "missing-openai-oauth-token.json"
        window._openai_oauth_connected = lambda: False
        window._refresh_openai_oauth_status()

        assert window.openai_oauth_connect_button is not None
        assert window.openai_oauth_disconnect_button is not None
        assert window.openai_models_refresh_button is not None
        assert window.openai_quota_refresh_button is not None
        assert window.openai_oauth_connect_button.isHidden() is False
        assert window.openai_oauth_connect_button.isEnabled() is True
        assert window.openai_oauth_disconnect_button.isHidden() is True
        assert window.openai_models_refresh_button.isHidden() is True
        assert window.openai_quota_refresh_button.isHidden() is True
    finally:
        window.close()
        app.processEvents()


def test_main_window_hides_openai_connect_button_when_connected(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._openai_oauth_connected = lambda: True
        window._openai_oauth_account_label = lambda: "dj@example.com"
        window._refresh_openai_oauth_status()

        assert window.openai_oauth_connect_button is not None
        assert window.openai_oauth_disconnect_button is not None
        assert window.openai_models_refresh_button is not None
        assert window.openai_quota_refresh_button is not None
        assert window.openai_oauth_connect_button.isHidden() is True
        assert window.openai_oauth_disconnect_button.isHidden() is False
        assert window.openai_oauth_disconnect_button.isEnabled() is True
        assert window.openai_models_refresh_button.isHidden() is False
        assert window.openai_models_refresh_button.isEnabled() is True
        assert window.openai_quota_refresh_button.isHidden() is False
        assert window.openai_quota_refresh_button.isEnabled() is True
    finally:
        window.close()
        app.processEvents()


def test_main_window_hides_google_disconnect_button_when_disconnected(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    client_file = tmp_path / "google_oauth_client.json"
    token_file = tmp_path / "ytmusic_oauth_token.json"
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._ytmusic_oauth_client_file = lambda: client_file
        window._ytmusic_oauth_token_file = lambda: token_file
        window._refresh_google_oauth_status()

        assert window.google_oauth_connect_button is not None
        assert window.google_oauth_disconnect_button is not None
        assert window.google_oauth_connect_button.isHidden() is False
        assert window.google_oauth_connect_button.isEnabled() is True
        assert window.google_oauth_disconnect_button.isHidden() is True
    finally:
        window.close()
        app.processEvents()


def test_main_window_hides_google_connect_button_when_connected(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    client_file = tmp_path / "google_oauth_client.json"
    token_file = tmp_path / "ytmusic_oauth_token.json"
    token_file.write_text("{}", encoding="utf-8")
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window._ytmusic_oauth_client_file = lambda: client_file
        window._ytmusic_oauth_token_file = lambda: token_file
        window._google_oauth_account_label = lambda: "dj@example.com"
        window._refresh_google_oauth_status()

        assert window.google_oauth_connect_button is not None
        assert window.google_oauth_disconnect_button is not None
        assert window.google_oauth_connect_button.isHidden() is True
        assert window.google_oauth_disconnect_button.isHidden() is False
        assert window.google_oauth_disconnect_button.isEnabled() is True
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


def test_main_window_prefills_ffmpeg_path_from_detected_dependency(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("onboarding/completed", True)
    detected = tmp_path / "ffmpeg.exe"

    def fake_find_executable(name: str, *, explicit_path=None, root=None) -> DependencyStatus:
        assert name == "ffmpeg" or explicit_path is None
        if name == "ffmpeg":
            return DependencyStatus(name=name, path=detected, source="PATH")
        return DependencyStatus(name=name, path=None, source="missing")

    monkeypatch.setattr("cueforge.gui.main_window.find_executable", fake_find_executable)

    window = MainWindow(settings=settings)
    try:
        assert window.ffmpeg_path_input.text() == str(detected)
    finally:
        window.close()
        app.processEvents()


def test_main_window_replaces_stale_saved_ffmpeg_path_with_detected_dependency(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("onboarding/completed", True)
    settings.setValue("paths/ffmpeg", str(tmp_path / "missing-ffmpeg.exe"))
    detected = tmp_path / "current-ffmpeg.exe"

    def fake_find_executable(name: str, *, explicit_path=None, root=None) -> DependencyStatus:
        if name == "ffmpeg":
            return DependencyStatus(name=name, path=detected, source="PATH")
        return DependencyStatus(name=name, path=None, source="missing")

    monkeypatch.setattr("cueforge.gui.main_window.find_executable", fake_find_executable)

    window = MainWindow(settings=settings)
    try:
        assert window.ffmpeg_path_input.text() == str(detected)
    finally:
        window.close()
        app.processEvents()


def test_main_window_prefers_bundled_ffmpeg_over_saved_external_path(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("onboarding/completed", True)
    saved = tmp_path / "external" / "ffmpeg.exe"
    bundled = tmp_path / "app" / "bin" / "ffmpeg" / "bin" / "ffmpeg.exe"
    settings.setValue("paths/ffmpeg", str(saved))

    def fake_find_executable(name: str, *, explicit_path=None, root=None) -> DependencyStatus:
        assert name == "ffmpeg"
        if explicit_path:
            return DependencyStatus(name=name, path=explicit_path, source="settings")
        return DependencyStatus(name=name, path=bundled, source="bundled")

    monkeypatch.setattr("cueforge.gui.main_window.find_executable", fake_find_executable)

    window = MainWindow(settings=settings)
    try:
        assert window.ffmpeg_path_input.text() == str(bundled)
    finally:
        window.close()
        app.processEvents()


def test_main_window_persists_beta_settings(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "missing-openai-oauth-token.json",
    )
    settings = _test_settings(tmp_path)
    settings.setValue("auth/cookie_browser", "chrome")
    settings.setValue("auth/unlock_browser_cookie_database", True)
    settings.setValue("auth/cookie_file", "D:\\cookies.txt")
    settings.setValue("paths/ytmusic_auth", "D:\\ytmusic-auth.json")
    settings.setValue("openai/api_key", "sk-legacy")
    settings.setValue("openai/web_search", False)
    settings.setValue("metadata/openai_enabled", False)
    window = MainWindow(settings=settings)
    try:
        window.output_dir_input.setText("D:\\Music")
        window.openai_model_input.setText("gpt-test")
        window.save_settings()
    finally:
        window.close()
        app.processEvents()

    restored = MainWindow(settings=settings)
    try:
        assert restored.output_dir_input.text() == "D:\\Music"
        assert settings.value("openai/api_key") is None
        assert settings.value("openai/web_search") is None
        assert settings.value("metadata/openai_enabled") is None
        assert settings.value("auth/cookie_browser") is None
        assert settings.value("auth/unlock_browser_cookie_database") is None
        assert settings.value("auth/cookie_file") is None
        assert settings.value("paths/ytmusic_auth") is None
    finally:
        restored.close()
        app.processEvents()


def test_settings_are_saved_when_leaving_settings_tab(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "missing-openai-oauth-token.json",
    )
    settings = _test_settings(tmp_path)
    window = MainWindow(settings=settings)
    try:
        window.tabs.setCurrentIndex(window.settings_tab_index)
        window.openai_model_input.setText("gpt-test")
        window.metadata_parallel_spin.setValue(4)

        window.tabs.setCurrentIndex(window.queue_tab_index)
        app.processEvents()

        assert settings.value("metadata/openai_enabled") is None
        assert settings.value("openai/model") is None
        assert settings.value("scheduler/metadata_parallel") == 4
        assert settings.value("auth/cookie_file") is None
    finally:
        window.close()
        app.processEvents()


def test_first_run_onboarding_requires_both_account_logins_to_complete(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_openai_codex_oauth_token_path",
        lambda: tmp_path / "missing-openai-oauth-token.json",
    )
    monkeypatch.setattr(
        "cueforge.gui.main_window.default_ytmusic_oauth_token_path",
        lambda: tmp_path / "missing-ytmusic-oauth-token.json",
    )
    window = MainWindow(settings=settings)
    try:
        assert window.onboarding_dialog is not None
        assert window.onboarding_dialog.isVisible()
        assert window.onboarding_dialog.done_button.isEnabled() is False
        assert window.onboarding_dialog.skip_button.isEnabled() is True
        assert "CLI 도구" in window.onboarding_dialog.prepare_status_label.text()

        window.onboarding_dialog._complete()
        app.processEvents()

        assert settings.value("onboarding/completed", False) is False
        assert window.onboarding_dialog is not None
    finally:
        window.close()
        app.processEvents()


def test_first_run_onboarding_can_complete_after_both_accounts_and_cli_tools_are_ready(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)

    def fake_find_executable(name: str, *, explicit_path=None, root=None) -> DependencyStatus:
        return DependencyStatus(name=name, path=tmp_path / f"{name}.exe", source="PATH")

    monkeypatch.setattr("cueforge.gui.main_window.find_executable", fake_find_executable)
    window = MainWindow(settings=settings)
    try:
        assert window.onboarding_dialog is not None
        window._openai_oauth_connected = lambda: True
        window._ytmusic_oauth_connected = lambda: True
        window._refresh_onboarding_account_actions()

        assert window.onboarding_dialog.done_button.isEnabled() is True
        assert window.onboarding_dialog.skip_button.isEnabled() is False

        window.onboarding_dialog._complete()
        app.processEvents()

        assert settings.value("onboarding/completed") is True
        assert window.onboarding_dialog is None
    finally:
        window.close()
        app.processEvents()


def test_first_run_onboarding_still_requires_cli_tools_after_both_accounts_are_connected(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)

    def fake_find_executable(name: str, *, explicit_path=None, root=None) -> DependencyStatus:
        if name == "ffmpeg":
            return DependencyStatus(name=name, path=tmp_path / "ffmpeg.exe", source="PATH")
        return DependencyStatus(name=name, path=None, source="missing")

    monkeypatch.setattr("cueforge.gui.main_window.find_executable", fake_find_executable)
    window = MainWindow(settings=settings)
    try:
        assert window.onboarding_dialog is not None
        window._openai_oauth_connected = lambda: True
        window._ytmusic_oauth_connected = lambda: True
        window._refresh_onboarding_account_actions()

        assert window.onboarding_dialog.done_button.isEnabled() is False
        assert window.onboarding_dialog.skip_button.isEnabled() is True
        assert "CLI 도구" in window.onboarding_dialog.prepare_status_label.text()
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
        log("Deno 준비 중")
        progress(45.0)
        calls.append("prepared")

    dialog = OnboardingDialog(
        parent=parent,
        dependency_rows=[("Deno", "첫 실행 준비 필요")],
        optional_rows=[],
        prepare_steps=[("Deno 런타임", prepare)],
        auto_prepare=False,
        on_done=lambda: completed.append(True),
    )
    try:
        dialog.show()
        assert dialog.skip_button.isHidden() is True
        assert dialog.skip_button.isEnabled() is False
        assert "필수 구성 요소 준비 필요" in dialog.prepare_status_label.text()
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


def test_onboarding_dependency_label_keeps_long_path_in_tooltip(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    long_path = r"C:\Users\iroun\AppData\Local\Programs\CueForge\bin\ffmpeg\ffmpeg-8.1.1-full_build-shared\bin\ffmpeg.exe"
    dialog = OnboardingDialog(
        parent=parent,
        dependency_rows=[OnboardingDependencyRow("ffmpeg", "정상 감지됨 (번들)", long_path)],
        optional_rows=[],
        prepare_steps=[],
        auto_prepare=False,
        on_done=lambda: None,
    )
    try:
        labels = [label for label in dialog.findChildren(QLabel) if label.text() == "정상 감지됨 (번들)"]
        assert labels
        assert long_path not in labels[0].text()
        assert labels[0].toolTip() == long_path
    finally:
        dialog.close()
        parent.close()
        app.processEvents()


def test_completed_onboarding_stays_closed_without_required_prepare_steps(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("onboarding/completed", True)
    window = MainWindow(settings=settings)
    try:
        assert window._should_open_startup_onboarding() is False
        assert window._onboarding_prepare_steps() == []
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


def test_onboarding_exposes_account_login_actions(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("onboarding/completed", True)
    window = MainWindow(settings=settings)
    calls: list[str] = []
    try:
        window._openai_oauth_connected = lambda: False
        window._ytmusic_oauth_client_file = lambda: tmp_path / "google_oauth_client.json"
        window._ytmusic_oauth_connected = lambda: False
        window._connect_openai_oauth = lambda: calls.append("openai")
        window._connect_google_oauth = lambda: calls.append("google")

        window._open_onboarding()
        app.processEvents()

        dialog = window.onboarding_dialog
        assert dialog is not None
        assert dialog.account_action_buttons["ChatGPT"].isEnabled() is True
        assert dialog.account_action_buttons["Google"].isEnabled() is True
        assert dialog.done_button.isEnabled() is False
        assert dialog.skip_button.isEnabled() is True
        assert dialog.account_status_labels["ChatGPT"].text() == "미연결"
        assert dialog.account_status_labels["Google"].text() == "연결 가능"

        dialog.account_action_buttons["ChatGPT"].click()
        dialog.account_action_buttons["Google"].click()

        assert calls == ["openai", "google"]
    finally:
        window.close()
        app.processEvents()


def test_onboarding_account_actions_refresh_after_login(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    settings.setValue("onboarding/completed", True)

    def fake_find_executable(name: str, *, explicit_path=None, root=None) -> DependencyStatus:
        return DependencyStatus(name=name, path=tmp_path / f"{name}.exe", source="PATH")

    monkeypatch.setattr("cueforge.gui.main_window.find_executable", fake_find_executable)
    window = MainWindow(settings=settings)
    try:
        window._openai_oauth_connected = lambda: False
        window._ytmusic_oauth_client_file = lambda: tmp_path / "google_oauth_client.json"
        window._ytmusic_oauth_connected = lambda: False
        window._connect_openai_oauth = lambda: None
        window._connect_google_oauth = lambda: None
        window._open_onboarding()
        app.processEvents()
        dialog = window.onboarding_dialog
        assert dialog is not None

        window._openai_oauth_connected = lambda: True
        window._openai_oauth_account_label = lambda: "dj@example.com"
        window._refresh_onboarding_account_actions()

        assert dialog.account_status_labels["ChatGPT"].text() == "연결됨: dj@example.com"
        assert dialog.account_action_buttons["ChatGPT"].text() == "연결됨"
        assert dialog.account_action_buttons["ChatGPT"].isEnabled() is False
        assert dialog.done_button.isEnabled() is False
        assert dialog.skip_button.isEnabled() is True

        window._ytmusic_oauth_connected = lambda: True
        window._google_oauth_account_label = lambda: "yt@example.com"
        window._refresh_onboarding_account_actions()

        assert dialog.account_status_labels["Google"].text() == "연결됨: yt@example.com"
        assert dialog.account_action_buttons["Google"].text() == "연결됨"
        assert dialog.account_action_buttons["Google"].isEnabled() is False
        assert dialog.done_button.isEnabled() is True
        assert dialog.skip_button.isEnabled() is False
    finally:
        window.close()
        app.processEvents()


def test_onboarding_dependency_status_marks_missing_bundled_tool_as_incomplete(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("cueforge.gui.main_window.find_executable", lambda name, explicit_path=None: DependencyStatus(name, None, "missing"))
    monkeypatch.setattr("sys.frozen", True, raising=False)

    assert "설치가 불완전함" in _dependency_setup_status("ffmpeg")


def test_onboarding_dependency_status_hides_detected_path(monkeypatch, tmp_path) -> None:
    tool = tmp_path / "bin" / "ffmpeg" / "ffmpeg-8.1.1-full_build-shared" / "bin" / "ffmpeg.exe"
    monkeypatch.setattr(
        "cueforge.gui.main_window.find_executable",
        lambda name, explicit_path=None: DependencyStatus(name, tool, "bundled"),
    )

    status = _dependency_setup_status("ffmpeg")

    assert status == "정상 감지됨 (번들)"
    assert str(tool) not in status


def test_onboarding_dependency_status_treats_path_tool_as_portable_fallback(monkeypatch, tmp_path) -> None:
    tool = tmp_path / "ffmpeg.exe"
    tool.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "cueforge.gui.main_window.find_executable",
        lambda name, explicit_path=None: DependencyStatus(name, tool, "PATH"),
    )
    monkeypatch.delattr("sys.frozen", raising=False)

    assert "개발/portable fallback" in _dependency_setup_status("ffmpeg")
    assert str(tool) not in _dependency_setup_status("ffmpeg")


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
                provider="ytmusic",
                score=0.70,
                matched_fields=("title",),
                metadata=TrackMetadata(title="Candidate A", artist="Artist A"),
            ),
            MetadataCandidate(
                provider="chatgpt",
                score=0.96,
                matched_fields=("llm", "title", "artist"),
                metadata=TrackMetadata(title="Candidate B", artist="Artist B", album="Album B"),
            ),
        ]
        window.table.selectRow(0)
        window._load_job_for_review(job)

        assert window.candidate_table.rowCount() == 2
        assert window.candidate_table.item(1, 0).text() == "chatgpt"

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


def test_review_dialog_shows_source_and_confidence_details(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.APPROVED
        job.selected_metadata = TrackMetadata(title="Fallback", artist="Uploader")
        job.source_title = "Original Video Title"
        job.source_channel = "Original Channel"
        job.candidates = [
            MetadataCandidate(
                provider="ytmusic",
                score=0.70,
                matched_fields=("title",),
                metadata=TrackMetadata(title="Candidate A", artist="Artist A"),
            )
        ]

        window._open_review_dialog(job)

        assert window.review_dialog is not None
        assert window.review_dialog.isVisible() is True
        assert window.review_queue_table.rowCount() == 0
        assert window.source_url_input.text() == "https://youtu.be/abc"
        assert window.source_url_input.isReadOnly() is True
        assert window.source_title_input.text() == "Original Video Title"
        assert window.source_title_input.isReadOnly() is True
        assert window.source_channel_input.text() == "Original Channel"
        assert window.source_channel_input.isReadOnly() is True
        assert "점수 0.70" in window.confidence_detail_label.text()
        assert "확인 권장" in window.confidence_detail_label.text()
        assert "아티스트 충돌" in window.candidate_table.item(0, 3).text()
        assert window.candidate_table.item(0, 4).text() == "title"
    finally:
        window.close()
        app.processEvents()


def test_cover_source_infers_known_artwork_hosts() -> None:
    assert _cover_source_from_url("https://i1.sndcdn.com/artworks-test.jpg") == "SoundCloud 기본 커버"
    assert _cover_source_from_url("https://i.ytimg.com/vi/abc/maxresdefault.jpg") == "YouTube 대체 썸네일"
    assert _cover_source_from_url("https://example.com/cover.jpg") == "수동"


def test_extract_urls_handles_pasted_text_and_de_duplicates() -> None:
    assert _extract_urls(
        "first <youtu.be/a>,https://youtu.be/b.\n"
        "youtu.be/a music.youtube.com/watch?v=c&si=d soundcloud.com/artist/track"
    ) == [
        "https://youtu.be/a",
        "https://youtu.be/b",
        "https://music.youtube.com/watch?v=c&si=d",
        "https://soundcloud.com/artist/track",
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
        assert window.table.item(0, 2).text() == "https://youtu.be/two"
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
        assert [window.table.item(row, 2).text() for row in range(window.table.rowCount())] == [
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

        assert job.status == DownloadStatus.APPROVED
        assert "최상위 메타데이터 후보로 다운로드 진행" in window.log.toPlainText()
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
        assert "메타데이터 자동 선택됨; 다운로드 진행" in window.log.toPlainText()
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


def test_scheduled_review_required_metadata_enqueues_download_by_default(tmp_path) -> None:
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
            TrackMetadata(title="Review Song", artist="Review Artist"),
            "review_required",
            [],
        )

        assert job.status == DownloadStatus.APPROVED
        assert queued == [([job], False)]
    finally:
        window.scheduler = None
        window.close()
        app.processEvents()


def test_approved_selected_track_does_not_open_tag_edit_dialog(tmp_path) -> None:
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

        assert window.review_selected_button.isEnabled() is False

        window._move_selected_to_review_queue()

        assert job.status == DownloadStatus.APPROVED
        assert window.table.item(0, 0).text() == "다운로드 대기"
        assert window.review_queue_table.rowCount() == 0
        assert window.review_dialog is not None
        assert window.review_dialog.isHidden() is True
        assert "완료된 뒤" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_queue_double_click_waits_until_track_is_done(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.APPROVED
        job.selected_metadata = TrackMetadata(title="Approved Song", artist="Approved Artist")
        window._update_row(job)

        window._open_queue_job_for_review(0, 0)

        assert job.status == DownloadStatus.APPROVED
        assert window.review_dialog is not None
        assert window.review_dialog.isHidden() is True
        assert "완료된 뒤" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_queue_double_click_opens_done_tag_edit_dialog(tmp_path) -> None:
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

        window._open_queue_job_for_review(0, 0)

        assert window.active_review_job_id == job.id
        assert window.review_dialog is not None
        assert window.review_dialog.isVisible() is True
        assert window.review_fields["title"].text() == "Approved Song"
        assert window.review_fields["artist"].text() == "Approved Artist"
    finally:
        window.close()
        app.processEvents()


def test_loaded_approved_track_cannot_open_tag_edit_dialog(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.APPROVED
        job.selected_metadata = TrackMetadata(title="Approved Song", artist="Approved Artist")
        window._load_job_for_review(job)

        window._move_active_to_review_queue()

        assert job.status == DownloadStatus.APPROVED
        assert window.review_dialog is not None
        assert window.review_dialog.isHidden() is True
        assert window.review_queue_table.rowCount() == 0
        assert "완료된 뒤" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_done_selected_track_opens_tag_edit_dialog(tmp_path) -> None:
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

        assert job.status == DownloadStatus.DONE
        assert job.final_path == final_path
        assert window.table.item(0, 0).text() == "완료"
        assert window.review_queue_table.rowCount() == 0
        assert window.active_review_job_id == job.id
        assert window.review_dialog is not None
        assert window.review_dialog.isVisible() is True
        assert window.approve_button.isEnabled() is True
        assert "기존 파일" in window.review_hint_label.text()
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


def test_retry_failed_can_enqueue_while_scheduler_is_running(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    queued: list[list[DownloadJob]] = []
    started: list[DownloadJob] = []

    class RunningScheduler:
        tag_semaphore = None

        def is_running(self) -> bool:
            return True

        def enqueue_analysis(self, jobs) -> None:
            queued.append(list(jobs))

        def enqueue_downloads(self, jobs, *, priority: bool = False) -> None:
            return None

        def set_limits(self, limits) -> None:
            return None

    try:
        for url in ("https://youtu.be/failed-a", "https://youtu.be/failed-b"):
            window.url_input.setText(url)
            window._add_url()
        first, second = [window.jobs[job_id] for job_id in window.row_job_ids]
        for job in (first, second):
            job.status = DownloadStatus.FAILED
            job.error = "old error"
            job.error_message = "old raw error"
            job.error_category = ErrorCategory.NETWORK_TIMEOUT.value
            window._update_row(job)

        monkeypatch.setattr(window, "_run_worker", lambda job, **_kwargs: started.append(job))
        window.scheduler = RunningScheduler()
        window.table.selectRow(0)
        window._refresh_actions()

        assert window.retry_failed_button is not None
        assert window.retry_selected_button is not None
        assert window.retry_failed_button.isEnabled() is True
        assert window.retry_selected_button.isEnabled() is True

        window._retry_failed()

        assert queued == [[first, second]]
        assert started == []
        assert first.status == DownloadStatus.PENDING
        assert second.status == DownloadStatus.PENDING
        assert first.retry_count == 1
        assert second.retry_count == 1
        assert first.error == ""
        assert second.error == ""
    finally:
        window.scheduler = None
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
        assert window.analyze_selected_button.isHidden() is True
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


def test_selected_running_job_refreshes_review_panel_when_metadata_auto_approves(tmp_path) -> None:
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

        assert job.status == DownloadStatus.APPROVED
        assert window.active_review_job_id == job.id
        assert window.review_fields["title"].text() == "Needs Review"
        assert window.review_fields["artist"].text() == "Detected Artist"
        assert window.candidate_table.rowCount() == 1
        assert window.approve_button.isEnabled() is False
    finally:
        window.close()
        app.processEvents()


def test_approve_ignores_loaded_track_before_done(tmp_path, monkeypatch) -> None:
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

        assert job.status == DownloadStatus.REVIEW_REQUIRED
        assert job.selected_metadata.title == "Review Title"
        assert job.selected_metadata.artist == "Review Artist"
        assert downloads == []
        assert "완료된 뒤" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_review_tab_is_not_part_of_main_navigation(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        assert window.review_tab_index == -1
        assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == ["작업", "상태", "이력", "설정"]
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

        assert first.status == DownloadStatus.APPROVED
        assert second.status == DownloadStatus.DOWNLOADING
        assert started == [(second, None, False, "process")]
        assert window.tabs.currentIndex() == window.queue_tab_index
        assert window.approve_button.isEnabled() is False
    finally:
        window.close()
        app.processEvents()


def test_saving_before_done_does_not_load_next_item(tmp_path) -> None:
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

        assert first.status == DownloadStatus.REVIEW_REQUIRED
        assert window.active_review_job_id == first.id
        assert window.review_fields["title"].text() == "First Review"
        assert window.review_fields["artist"].text() == "Artist A"
        assert window.review_dialog is not None
        assert window.review_dialog.isHidden() is True
        assert "완료된 뒤" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_tag_edit_dialog_can_delete_loaded_item(tmp_path) -> None:
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
        assert window.review_queue_table.rowCount() == 0
        assert window.active_review_job_id is None
        assert window.review_dialog is not None
        assert window.review_dialog.isHidden() is True
    finally:
        window.close()
        app.processEvents()


def test_approve_while_queue_running_ignores_track_before_done(tmp_path, monkeypatch) -> None:
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

        assert first.status == DownloadStatus.REVIEW_REQUIRED
        assert started == []

        window._worker_finished()

        assert started == []
        assert second.status == DownloadStatus.PENDING

        window._download_next_approved()

        assert started == []
        assert "완료된 뒤" in window.log.toPlainText()
    finally:
        window.close()
        app.processEvents()


def test_manual_review_approval_before_done_does_not_use_priority_download_queue(tmp_path) -> None:
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

        assert job.status == DownloadStatus.REVIEW_REQUIRED
        assert queued == []
        assert "완료된 뒤" in window.log.toPlainText()
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
        assert second.status == DownloadStatus.APPROVED
        assert window.review_tab_index == -1
    finally:
        window.close()
        app.processEvents()
