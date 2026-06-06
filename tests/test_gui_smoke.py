import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from ytdj.gui.main_window import MainWindow, _cover_source_from_url
from ytdj.models import DownloadStatus, MetadataCandidate, TrackMetadata


def _test_settings(tmp_path) -> QSettings:
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


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
        window.fpcalc_path_input.setText("C:\\tools\\fpcalc.exe")

        assert window.acoustid_key_input.text() == "client-key"
        assert window.fpcalc_path_input.text() == "C:\\tools\\fpcalc.exe"
    finally:
        window.close()
        app.processEvents()


def test_main_window_persists_beta_settings(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    settings = _test_settings(tmp_path)
    window = MainWindow(settings=settings)
    try:
        window.output_dir_input.setText("D:\\Music")
        window.cookie_combo.setCurrentIndex(1)
        window.verify_auto_approved_checkbox.setChecked(True)
        window.acoustid_key_input.setText("client-key")
        window.save_settings()
    finally:
        window.close()
        app.processEvents()

    restored = MainWindow(settings=settings)
    try:
        assert restored.output_dir_input.text() == "D:\\Music"
        assert restored.cookie_combo.currentData() == "chrome"
        assert restored.verify_auto_approved_checkbox.isChecked() is True
        assert restored.acoustid_key_input.text() == "client-key"
    finally:
        restored.close()
        app.processEvents()


def test_review_candidate_table_applies_selected_candidate(tmp_path) -> None:
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

        assert window.review_fields["title"].text() == "Candidate B"
        assert window.review_fields["artist"].text() == "Artist B"
        assert window.review_fields["album"].text() == "Album B"
        assert job.selected_metadata.title == "Candidate B"
    finally:
        window.close()
        app.processEvents()


def test_cover_source_infers_known_artwork_hosts() -> None:
    assert _cover_source_from_url("https://coverartarchive.org/release/rel/front-500.jpg") == "Cover Art Archive"
    assert _cover_source_from_url("https://i1.sndcdn.com/artworks-test.jpg") == "SoundCloud native"
    assert _cover_source_from_url("https://i.ytimg.com/vi/abc/maxresdefault.jpg") == "YouTube fallback"


def test_copy_diagnostics_puts_report_on_clipboard(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    monkeypatch.setattr("ytdj.gui.main_window.format_diagnostics", lambda: "diagnostics report")
    try:
        window._copy_diagnostics()

        assert QApplication.clipboard().text() == "diagnostics report"
        assert "diagnostics copied" in window.log.toPlainText()
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
        assert "metadata requires review: review_required" in window.log.toPlainText()
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

        started = []

        def fake_run_worker(run_job, approved_metadata=None):
            started.append((run_job, approved_metadata))

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)
        window._approve_selected()

        assert started
        assert started[0][0] is job
        assert started[0][1].title == "Review Title"
        assert started[0][1].artist == "Review Artist"
    finally:
        window.close()
        app.processEvents()


def test_review_required_pauses_queue_and_focuses_review_tab(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        for url in ("https://youtu.be/needs-review", "https://youtu.be/next"):
            window.url_input.setText(url)
            window._add_url()
        first, second = window.jobs[window.row_job_ids[0]], window.jobs[window.row_job_ids[1]]
        first.status = DownloadStatus.METADATA

        started = []
        monkeypatch.setattr(window, "_run_worker", lambda job, approved_metadata=None: started.append(job))

        window._on_metadata_ready(first.id, TrackMetadata(title="Song", artist="Artist"), "review_required", [])
        window._worker_finished()

        assert first.status == DownloadStatus.REVIEW_REQUIRED
        assert second.status == DownloadStatus.PENDING
        assert started == []
        assert window.tabs.currentIndex() == window.review_tab_index
        assert window.table.currentRow() == 0
        assert window.start_queue_button.isEnabled() is False
        assert window.approve_button.isEnabled() is True
        assert "Queue paused" in window.queue_status_label.text()
    finally:
        window.close()
        app.processEvents()


def test_queue_continues_after_review_approval_finishes(tmp_path, monkeypatch) -> None:
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

        def fake_run_worker(job, approved_metadata=None):
            started.append((job, approved_metadata))
            job.status = DownloadStatus.DOWNLOADING

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        window._approve_selected()
        first.status = DownloadStatus.DONE
        window._worker_finished()

        assert started[0][0] is first
        assert started[0][1].title == "Song"
        assert started[1][0] is second
        assert started[1][1] is None
    finally:
        window.close()
        app.processEvents()
