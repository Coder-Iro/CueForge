import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QSettings, Qt
from PySide6.QtWidgets import QApplication

from ytdj.gui.main_window import MainWindow, _cover_source_from_url, _dependency_setup_status, _extract_urls
from ytdj.models import DownloadStatus, MetadataCandidate, TrackMetadata
from ytdj.runtime import DependencyStatus


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
        title_rect = QRect(title_field.mapTo(window, QPoint(0, 0)), title_field.size())
        cover_rect = QRect(window.cover_preview_label.mapTo(window, QPoint(0, 0)), window.cover_preview_label.size())

        assert cover_rect.left() > title_rect.right()
        assert cover_rect.top() <= title_rect.top() + 40
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
        assert window.cookie_unlock_checkbox.isChecked() is False
        window.acoustid_key_input.setText("client-key")
        window.getsongbpm_key_input.setText("bpm-key")
        window.fpcalc_path_input.setText("C:\\tools\\fpcalc.exe")

        assert window.acoustid_key_input.text() == "client-key"
        assert window.getsongbpm_key_input.text() == "bpm-key"
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
        window.cookie_unlock_checkbox.setChecked(True)
        window.verify_auto_approved_checkbox.setChecked(True)
        window.acoustid_key_input.setText("client-key")
        window.getsongbpm_key_input.setText("bpm-key")
        window.save_settings()
    finally:
        window.close()
        app.processEvents()

    restored = MainWindow(settings=settings)
    try:
        assert restored.output_dir_input.text() == "D:\\Music"
        assert restored.cookie_combo.currentData() == "chrome"
        assert restored.cookie_unlock_checkbox.isChecked() is True
        assert restored.verify_auto_approved_checkbox.isChecked() is True
        assert restored.acoustid_key_input.text() == "client-key"
        assert restored.getsongbpm_key_input.text() == "bpm-key"
    finally:
        restored.close()
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
    monkeypatch.setattr("ytdj.gui.main_window.find_executable", lambda name, explicit_path=None: DependencyStatus(name, None, "missing"))
    monkeypatch.setattr("sys.frozen", True, raising=False)

    assert "설치가 불완전함" in _dependency_setup_status("ffmpeg")


def test_onboarding_dependency_status_treats_path_tool_as_portable_fallback(monkeypatch, tmp_path) -> None:
    tool = tmp_path / "ffmpeg.exe"
    tool.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "ytdj.gui.main_window.find_executable",
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
    finally:
        window.close()
        app.processEvents()


def test_tag_editor_bpm_field_manual_value_wins(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    try:
        window.url_input.setText("https://youtu.be/abc")
        window._add_url()
        job = next(iter(window.jobs.values()))
        job.status = DownloadStatus.REVIEW_REQUIRED
        job.selected_metadata = TrackMetadata(
            title="Song",
            artist="Artist",
            bpm=128,
            bpm_source="GetSongBPM",
            bpm_confidence=0.91,
        )

        window._load_job_for_review(job)
        assert window.review_fields["bpm"].text() == "128"

        window.review_fields["bpm"].setText("220")
        metadata = window._metadata_from_review_fields(job.selected_metadata)

        assert metadata.bpm == 220
        assert metadata.bpm_source == "manual"
        assert metadata.bpm_confidence == 1.0
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
        job.candidates = [
            MetadataCandidate(
                provider="musicbrainz",
                score=0.70,
                matched_fields=("title",),
                metadata=TrackMetadata(title="Candidate A", artist="Artist A", bpm=140, bpm_source="GetSongBPM", bpm_confidence=0.70),
            )
        ]

        window._load_job_for_review(job)

        assert window.review_queue_table.rowCount() == 1
        assert window.review_queue_table.item(0, 0).text() == "Fallback"
        assert window.review_queue_table.item(0, 2).text() == "검수"
        assert "점수 0.70" in window.confidence_detail_label.text()
        assert "검수 필요" in window.confidence_detail_label.text()
        assert "아티스트 충돌" in window.candidate_table.item(0, 3).text()
        assert "BPM 있음" in window.candidate_table.item(0, 3).text()
        assert window.candidate_table.item(0, 4).text() == "140 (GetSongBPM 0.70)"
        assert "BPM 출처: GetSongBPM" in window.confidence_detail_label.text()
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


def test_copy_diagnostics_puts_report_on_clipboard(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings=_test_settings(tmp_path))
    monkeypatch.setattr("ytdj.gui.main_window.format_diagnostics", lambda: "diagnostics report")
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


def test_analyze_and_download_buttons_are_separate(tmp_path, monkeypatch) -> None:
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

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False):
            started.append((job, approved_metadata, analyze_only))

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        window._analyze_next()
        window._download_next_approved()

        assert started[0] == (pending, None, True)
        assert started[1][0] is approved
        assert started[1][1].title == "Ready"
        assert started[1][2] is False
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

        assert started[0] == (pending, None, True, False)
        assert started[1][0] is approved
        assert started[1][1].title == "Ready"
        assert started[1][2:] == (False, False)
        assert started[2] == (failed, None, True, False)
        assert failed.status == DownloadStatus.PENDING
        assert failed.error == ""
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


def test_approve_uses_loaded_review_job_without_queue_selection(tmp_path) -> None:
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

        window._approve_selected()

        assert job.status == DownloadStatus.APPROVED
        assert job.selected_metadata.title == "Review Title"
        assert job.selected_metadata.artist == "Review Artist"
        assert window.download_approved_button.isEnabled() is True
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

        def fake_run_worker(job, approved_metadata=None, *, analyze_only=False):
            started.append((job, approved_metadata, analyze_only))
            job.status = DownloadStatus.DOWNLOADING

        monkeypatch.setattr(window, "_run_worker", fake_run_worker)

        window._on_metadata_ready(first.id, TrackMetadata(title="Song", artist="Artist"), "review_required", [])
        window.worker_mode = "analysis"
        window._worker_finished()

        assert first.status == DownloadStatus.REVIEW_REQUIRED
        assert second.status == DownloadStatus.DOWNLOADING
        assert started == [(second, None, True)]
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
