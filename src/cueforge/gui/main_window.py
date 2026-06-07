"""PySide6 desktop interface."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from cueforge.download import CookieBrowser, DownloadCanceled, DownloadConfig, DownloadProgress, YTDLPDownloader
from cueforge.metadata import AcoustIDConfig, AcoustIDProvider, CoverArtProvider, MetadataResolver
from cueforge.metadata.fingerprint import FingerprintError, FingerprintUnavailable
from cueforge.metadata.matching import text_similarity
from cueforge.metadata.normalize import merge_metadata
from cueforge.models import DownloadJob, DownloadStatus, MetadataCandidate, ReviewState, TagWriteResult, TrackMetadata
from cueforge.runtime import find_executable, format_diagnostics
from cueforge.sources import SourcePlatform, detect_source_platform
from cueforge.tags import RekordboxTagWriter, safe_track_filename

DownloaderFactory = Callable[[DownloadConfig, Any], YTDLPDownloader]
ResolverFactory = Callable[[], MetadataResolver]
AcoustIDProviderFactory = Callable[[AcoustIDConfig], Any]
CoverArtProviderFactory = Callable[[], Any]
TagWriterFactory = Callable[[], Any]
_ACTIVE_STATUSES = {DownloadStatus.DOWNLOADING, DownloadStatus.METADATA, DownloadStatus.TAGGING}
_ANALYZABLE_STATUSES = {
    DownloadStatus.PENDING,
    DownloadStatus.REVIEW_REQUIRED,
    DownloadStatus.APPROVED,
    DownloadStatus.FAILED,
    DownloadStatus.CANCELED,
}


class JobWorker(QThread):
    progress_changed = Signal(str, float, str)
    metadata_ready = Signal(str, object, object, object)
    job_done = Signal(str, str)
    job_failed = Signal(str, str)
    job_canceled = Signal(str)
    log_message = Signal(str, str)

    def __init__(
        self,
        job: DownloadJob,
        *,
        cookie_browser: CookieBrowser | None,
        unlock_browser_cookie_database: bool = False,
        ytmusic_auth_path: Path | None,
        ffmpeg_location: Path | None,
        acoustid_config: AcoustIDConfig | None = None,
        audio_recognition_enabled: bool = True,
        verify_auto_approved_metadata: bool = False,
        approved_metadata: TrackMetadata | None = None,
        analyze_only: bool = False,
        downloader_factory: DownloaderFactory | None = None,
        resolver_factory: ResolverFactory | None = None,
        acoustid_provider_factory: AcoustIDProviderFactory | None = None,
        cover_art_provider_factory: CoverArtProviderFactory | None = None,
        tag_writer_factory: TagWriterFactory | None = None,
    ) -> None:
        super().__init__()
        self.job = job
        self.cookie_browser = cookie_browser
        self.unlock_browser_cookie_database = unlock_browser_cookie_database
        self.ytmusic_auth_path = ytmusic_auth_path
        self.ffmpeg_location = ffmpeg_location
        self.acoustid_config = acoustid_config or AcoustIDConfig()
        self.audio_recognition_enabled = audio_recognition_enabled
        self.verify_auto_approved_metadata = verify_auto_approved_metadata
        self.approved_metadata = approved_metadata
        self.analyze_only = analyze_only
        self._downloader_factory = downloader_factory or _create_downloader
        self._resolver_factory = resolver_factory
        self._acoustid_provider_factory = acoustid_provider_factory or AcoustIDProvider
        self._cover_art_provider_factory = cover_art_provider_factory or CoverArtProvider
        self._tag_writer_factory = tag_writer_factory or RekordboxTagWriter
        self._current_download_path: Path | None = None
        self._cancel_requested = False

    def run(self) -> None:
        try:
            self._check_canceled()
            downloader = self._new_downloader(self.job.output_dir)
            metadata = self.approved_metadata
            downloaded_path = self.job.downloaded_path
            if metadata is None:
                self._check_canceled()
                metadata, state, candidates, platform = self._resolve_metadata(downloader)
                self._check_canceled()
                if state != ReviewState.AUTO_APPROVED or self.verify_auto_approved_metadata:
                    metadata, state, candidates, downloaded_path = self._try_audio_recognition(
                        metadata=metadata,
                        state=state,
                        candidates=candidates,
                        platform=platform,
                    )
                self._check_canceled()
                self.metadata_ready.emit(self.job.id, metadata, state, candidates)
                if self.analyze_only or state != ReviewState.AUTO_APPROVED:
                    return

            self._check_canceled()
            if downloaded_path and not downloaded_path.exists():
                self.log_message.emit(self.job.id, f"준비된 다운로드 파일이 없어 다시 다운로드함: {downloaded_path}")
                downloaded_path = None

            if downloaded_path is None:
                self._check_canceled()
                result = self._new_downloader(_temp_output_dir(self.job)).download_audio(self.job.url)
                downloaded_path = result.path
                self.job.downloaded_path = downloaded_path
            self._check_canceled()
            self.progress_changed.emit(self.job.id, 100.0, DownloadStatus.TAGGING.value)
            self._check_canceled()
            final_path = _move_to_final(downloaded_path, self.job.output_dir, metadata)
            self.job.downloaded_path = None
            tag_result: TagWriteResult = self._tag_writer_factory().write(final_path, metadata)
            if tag_result.written_fields:
                self.log_message.emit(self.job.id, f"기록된 태그: {', '.join(tag_result.written_fields)}")
            if tag_result.skipped_fields:
                self.log_message.emit(self.job.id, f"생략된 태그: {', '.join(tag_result.skipped_fields)}")
            for warning in tag_result.warnings:
                self.log_message.emit(self.job.id, warning)
            self.job_done.emit(self.job.id, str(final_path))
        except DownloadCanceled:
            _cleanup_temp_download(self.job, self._current_download_path)
            self.job_canceled.emit(self.job.id)
        except Exception as exc:
            self.job_failed.emit(self.job.id, str(exc))

    def cancel(self) -> None:
        self._cancel_requested = True
        self.requestInterruption()

    def _check_canceled(self) -> None:
        if self._cancel_requested or self.isInterruptionRequested():
            raise DownloadCanceled("사용자가 작업을 취소했습니다.")

    def _new_downloader(self, output_dir: Path) -> YTDLPDownloader:
        return self._downloader_factory(
            DownloadConfig(
                output_dir=output_dir,
                cookie_browser=self.cookie_browser,
                unlock_browser_cookie_database=self.unlock_browser_cookie_database,
                ffmpeg_location=self.ffmpeg_location,
            ),
            self._on_progress,
        )

    def _new_resolver(self) -> MetadataResolver:
        if self._resolver_factory:
            return self._resolver_factory()
        return MetadataResolver(
            cover_art_provider_factory=self._cover_art_provider_factory,
        )

    def _resolve_metadata(self, downloader: YTDLPDownloader) -> tuple[TrackMetadata, ReviewState, list[MetadataCandidate], SourcePlatform]:
        self.progress_changed.emit(self.job.id, 0.0, DownloadStatus.METADATA.value)
        started_at = time.monotonic()
        self.log_message.emit(self.job.id, "yt-dlp 정보 조회 시작")
        info = downloader.fetch_info(self.job.url)
        self.log_message.emit(self.job.id, f"yt-dlp 정보 조회 완료 ({_elapsed(started_at)}): {_info_summary(info)}")
        started_at = time.monotonic()
        self.log_message.emit(self.job.id, "메타데이터 공급자 조회 시작")
        resolution = self._new_resolver().resolve(
            url=self.job.url,
            info=info,
            ytmusic_auth_path=self.ytmusic_auth_path,
            ytmusic_cookie_browser=_cookie_browser_value(self.cookie_browser),
            unlock_browser_cookie_database=self.unlock_browser_cookie_database,
            log=lambda message: self.log_message.emit(self.job.id, message),
        )
        self.log_message.emit(self.job.id, f"메타데이터 공급자 조회 완료 ({_elapsed(started_at)})")
        self.log_message.emit(
            self.job.id,
            f"소스: {resolution.platform.display_name}; {_trust_note_ko(resolution.platform)}",
        )
        if resolution.candidates:
            best = resolution.candidates[0]
            matched = ", ".join(best.matched_fields) or "일치 항목 없음"
            self.log_message.emit(self.job.id, f"최상위 메타데이터 후보: {best.provider} {best.score:.2f} ({matched})")
        self.log_message.emit(self.job.id, f"선택된 메타데이터: {resolution.metadata.artist} - {resolution.metadata.title}")
        if resolution.metadata.bpm:
            self.log_message.emit(
                self.job.id,
                f"선택된 BPM: {resolution.metadata.bpm} ({resolution.metadata.bpm_source or 'metadata'})",
            )
        if resolution.metadata.cover_url:
            cover_source = resolution.metadata.cover_source or _cover_source_from_url(resolution.metadata.cover_url)
            self.log_message.emit(self.job.id, f"커버 출처: {cover_source}")
        return resolution.metadata, resolution.state, resolution.candidates, resolution.platform

    def _try_audio_recognition(
        self,
        *,
        metadata: TrackMetadata,
        state: ReviewState,
        candidates: list[MetadataCandidate],
        platform: SourcePlatform,
    ) -> tuple[TrackMetadata, ReviewState, list[MetadataCandidate], Path | None]:
        reason = _audio_recognition_skip_reason(
            platform=platform,
            state=state,
            enabled=self.audio_recognition_enabled,
            verify_auto_approved=self.verify_auto_approved_metadata,
            config=self.acoustid_config,
        )
        if reason:
            self.log_message.emit(self.job.id, f"오디오 인식 생략: {reason}")
            return metadata, state, candidates, None

        self._check_canceled()
        if state == ReviewState.AUTO_APPROVED:
            self.log_message.emit(self.job.id, "자동 승인 메타데이터를 AcoustID로 검증 중")
        else:
            self.log_message.emit(self.job.id, "메타데이터 신뢰도가 낮아 AcoustID 조회용 임시 오디오 다운로드 중")
        result = self._new_downloader(_temp_output_dir(self.job)).download_audio(self.job.url)
        self.job.downloaded_path = result.path
        self._check_canceled()

        try:
            fingerprint_candidates = self._acoustid_provider_factory(self.acoustid_config).lookup(result.path)
        except FingerprintUnavailable as exc:
            self.log_message.emit(self.job.id, f"오디오 인식 생략: {exc}")
            return metadata, state, candidates, result.path
        except FingerprintError as exc:
            self.log_message.emit(self.job.id, f"오디오 인식 실패: {exc}")
            return metadata, state, candidates, result.path

        if not fingerprint_candidates:
            self.log_message.emit(self.job.id, "AcoustID 일치 항목 없음")
            return metadata, state, candidates, result.path

        merged_metadata, merged_state, merged_candidates = _merge_audio_recognition_candidates(
            metadata=metadata,
            state=state,
            candidates=candidates,
            fingerprint_candidates=fingerprint_candidates,
        )
        resolver = self._new_resolver()
        merged_metadata = resolver.enrich_cover_art(
            merged_metadata,
            platform=platform,
            fallback_cover_url=metadata.cover_url,
            log=lambda message: self.log_message.emit(self.job.id, message),
        )
        merged_metadata, bpm_candidates = resolver.enrich_bpm(
            merged_metadata,
            info=result.info,
            platform=platform,
            log=lambda message: self.log_message.emit(self.job.id, message),
        )
        merged_candidates.extend(bpm_candidates)
        self._check_canceled()
        best = fingerprint_candidates[0]
        self.log_message.emit(self.job.id, f"AcoustID 최상위 일치: {best.metadata.artist} - {best.metadata.title} ({best.score:.2f})")
        return merged_metadata, merged_state, merged_candidates, result.path

    def _on_progress(self, progress: DownloadProgress) -> None:
        if progress.filename:
            self._current_download_path = progress.filename
        self._check_canceled()
        percent = progress.percent if progress.percent is not None else 0.0
        status = DownloadStatus.DOWNLOADING.value if progress.status == "downloading" else progress.status
        self.progress_changed.emit(self.job.id, percent, status)


class CoverPreviewWorker(QThread):
    cover_loaded = Signal(str, str, object, str)

    def __init__(self, job_id: str, url: str) -> None:
        super().__init__()
        self.job_id = job_id
        self.url = url

    def run(self) -> None:
        try:
            import requests

            response = requests.get(self.url, timeout=8)
            response.raise_for_status()
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if mime and not mime.startswith("image/"):
                self.cover_loaded.emit(self.job_id, self.url, b"", f"non-image cover response: {mime}")
                return
            self.cover_loaded.emit(self.job_id, self.url, response.content, "")
        except Exception as exc:
            self.cover_loaded.emit(self.job_id, self.url, b"", str(exc))


class OnboardingDialog(QDialog):
    def __init__(
        self,
        *,
        parent: QWidget,
        dependency_rows: list[tuple[str, str]],
        optional_rows: list[tuple[str, str]],
        on_done: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self._on_done = on_done
        self.setWindowTitle("초기 환경 점검")
        self.resize(560, 420)

        layout = QVBoxLayout(self)
        intro = QLabel("설치된 외부 도구 상태를 확인하고 선택 설정을 점검합니다. 건너뛰어도 앱은 계속 사용할 수 있습니다.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        dependency_group = QGroupBox("번들 의존성")
        dependency_layout = QFormLayout(dependency_group)
        for name, status in dependency_rows:
            label = QLabel(status)
            label.setWordWrap(True)
            dependency_layout.addRow(name, label)
        layout.addWidget(dependency_group)

        optional_group = QGroupBox("선택 설정")
        optional_layout = QFormLayout(optional_group)
        for name, status in optional_rows:
            label = QLabel(status)
            label.setWordWrap(True)
            optional_layout.addRow(name, label)
        layout.addWidget(optional_group)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        skip_button = QPushButton("건너뛰기")
        skip_button.clicked.connect(self._complete)
        action_row.addWidget(skip_button)
        done_button = QPushButton("확인")
        done_button.clicked.connect(self._complete)
        action_row.addWidget(done_button)
        layout.addLayout(action_row)

    def _complete(self) -> None:
        self._on_done()
        self.accept()


class UrlInput(QPlainTextEdit):
    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        self.setPlainText(value)


class MainWindow(QMainWindow):
    COLUMNS = ("상태", "진행률", "소스", "URL", "제목", "아티스트", "출력")
    REVIEW_QUEUE_COLUMNS = ("제목", "아티스트", "신뢰도", "URL")
    CANDIDATE_COLUMNS = ("제공자", "점수", "신뢰도", "배지", "BPM", "일치 항목", "제목", "아티스트", "앨범", "날짜", "ISRC", "커버")
    CANDIDATE_PREVIEW_COLUMNS = ("필드", "현재 값", "후보 적용 값")

    def __init__(self, *, settings: QSettings | None = None) -> None:
        super().__init__()
        self.setWindowTitle("CueForge")
        self.resize(1120, 720)
        self._settings = settings or QSettings("CueForge", "CueForge")
        self.jobs: dict[str, DownloadJob] = {}
        self.row_job_ids: list[str] = []
        self.worker: JobWorker | None = None
        self.worker_mode = ""
        self.cancel_requested = False
        self.active_review_job_id: str | None = None
        self.tabs: QTabWidget | None = None
        self.queue_tab_index = 0
        self.review_tab_index = 1
        self.start_action: QAction | None = None
        self.download_action: QAction | None = None
        self.retry_action: QAction | None = None
        self.add_url_button: QPushButton | None = None
        self.start_queue_button: QPushButton | None = None
        self.download_approved_button: QPushButton | None = None
        self.review_selected_button: QPushButton | None = None
        self.analyze_selected_button: QPushButton | None = None
        self.download_selected_button: QPushButton | None = None
        self.retry_selected_button: QPushButton | None = None
        self.retry_failed_button: QPushButton | None = None
        self.remove_selected_button: QPushButton | None = None
        self.cancel_current_button: QPushButton | None = None
        self.approve_button: QPushButton | None = None
        self.reopen_review_button: QPushButton | None = None
        self.open_onboarding_button: QPushButton | None = None
        self.onboarding_dialog: OnboardingDialog | None = None
        self.apply_candidate_button: QPushButton | None = None
        self.pending_candidate_index: int | None = None
        self.review_scroll_area: QScrollArea | None = None
        self.review_splitter: QSplitter | None = None
        self._loading_review = False
        self._loading_review_queue = False
        self._cover_preview_workers: list[CoverPreviewWorker] = []

        self.url_input = UrlInput()
        self.url_input.setPlaceholderText("YouTube / YouTube Music / SoundCloud URL을 하나 이상 붙여넣으세요")
        self.url_input.setFixedHeight(76)
        self.output_dir_input = QLineEdit(str(Path.cwd() / "downloads"))
        self.cookie_combo = QComboBox()
        self.cookie_combo.addItem("브라우저 쿠키 사용 안 함", None)
        self.cookie_combo.addItem("Chrome", CookieBrowser.CHROME)
        self.cookie_combo.addItem("Edge", CookieBrowser.EDGE)
        self.cookie_combo.addItem("Firefox", CookieBrowser.FIREFOX)
        self.cookie_unlock_checkbox = QCheckBox("Chrome/Edge 쿠키 DB가 잠겨 있으면 잠금 해제 보조 기능 사용")
        self.auth_path_input = QLineEdit()
        self.ffmpeg_path_input = QLineEdit()
        self.audio_recognition_checkbox = QCheckBox("메타데이터 신뢰도가 낮으면 AcoustID 사용")
        self.audio_recognition_checkbox.setChecked(True)
        self.verify_auto_approved_checkbox = QCheckBox("YouTube 자동 승인 메타데이터를 AcoustID로 검증")
        self.acoustid_key_input = QLineEdit()
        self.acoustid_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.fpcalc_path_input = QLineEdit()

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._load_selected_job)

        self.review_queue_table = QTableWidget(0, len(self.REVIEW_QUEUE_COLUMNS))
        self.review_queue_table.setHorizontalHeaderLabels(self.REVIEW_QUEUE_COLUMNS)
        self.review_queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.review_queue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.review_queue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.review_queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.review_queue_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.review_queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.review_queue_table.setMinimumHeight(72)
        self.review_queue_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.review_queue_table.itemSelectionChanged.connect(self._load_selected_review_queue_job)

        self.candidate_table = QTableWidget(0, len(self.CANDIDATE_COLUMNS))
        self.candidate_table.setHorizontalHeaderLabels(self.CANDIDATE_COLUMNS)
        self.candidate_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.candidate_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.candidate_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.candidate_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.candidate_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.candidate_table.setMinimumHeight(88)
        self.candidate_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.candidate_table.itemSelectionChanged.connect(self._preview_selected_candidate)
        self.candidate_preview_table = QTableWidget(0, len(self.CANDIDATE_PREVIEW_COLUMNS))
        self.candidate_preview_table.setHorizontalHeaderLabels(self.CANDIDATE_PREVIEW_COLUMNS)
        self.candidate_preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.candidate_preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.candidate_preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.candidate_preview_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.candidate_preview_table.setMinimumHeight(96)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)

        self.queue_status_label = QLabel("URL을 추가한 뒤 큐를 처리하세요.")
        self.queue_status_label.setWordWrap(True)
        self.dependency_status_label = QLabel("")
        self.dependency_status_label.setWordWrap(True)
        self.review_fields = {
            "title": QLineEdit(),
            "artist": QLineEdit(),
            "album": QLineEdit(),
            "album_artist": QLineEdit(),
            "genre": QLineEdit(),
            "release_date": QLineEdit(),
            "bpm": QLineEdit(),
            "label": QLineEdit(),
            "isrc": QLineEdit(),
            "cover_url": QLineEdit(),
        }
        self.review_state_label = QLabel("선택된 트랙 없음")
        self.review_hint_label = QLabel("메타데이터 검수가 필요한 트랙이 여기에 표시됩니다.")
        self.review_hint_label.setWordWrap(True)
        self.candidate_label = QLabel("")
        self.confidence_detail_label = QLabel("")
        self.confidence_detail_label.setWordWrap(True)
        self.cover_source_label = QLabel("커버 출처: 없음")
        self.cover_source_label.setWordWrap(True)
        self.cover_preview_label = QLabel("커버 없음")
        self.cover_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_preview_label.setFixedSize(180, 180)
        self.cover_preview_label.setStyleSheet("border: 1px solid #b8b8b8;")
        self.review_fields["cover_url"].editingFinished.connect(self._cover_url_edited)

        self._load_settings()
        self._build_ui()
        if not _settings_bool(self._settings.value("onboarding/completed", False), default=False):
            self._open_onboarding()

    def _build_ui(self) -> None:
        toolbar = QToolBar("작업")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        add_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "URL 추가", self)
        add_action.triggered.connect(self._add_url)
        self.start_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "메타데이터 분석", self)
        self.start_action.triggered.connect(self._analyze_next)
        self.download_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), "승인 항목 다운로드", self)
        self.download_action.triggered.connect(self._download_next_approved)
        self.retry_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "실패 재시도", self)
        self.retry_action.triggered.connect(self._retry_failed)
        remove_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon), "삭제", self)
        remove_action.triggered.connect(self._remove_selected)
        toolbar.addAction(add_action)
        toolbar.addAction(self.start_action)
        toolbar.addAction(self.download_action)
        toolbar.addAction(self.retry_action)
        toolbar.addAction(remove_action)

        self.tabs = QTabWidget()
        self.queue_tab_index = self.tabs.addTab(self._queue_tab(), "큐")
        self.review_tab_index = self.tabs.addTab(self._review_tab(), "검수")
        self.tabs.addTab(self._settings_tab(), "설정")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)
        self._refresh_actions()

    def _queue_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        url_row = QGridLayout()
        url_row.addWidget(QLabel("URL"), 0, 0)
        url_row.addWidget(self.url_input, 0, 1)
        self.add_url_button = QPushButton("추가")
        self.add_url_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        self.add_url_button.clicked.connect(self._add_url)
        url_row.addWidget(self.add_url_button, 0, 2)
        url_row.setColumnStretch(1, 1)
        layout.addLayout(url_row)

        action_row = QGridLayout()
        action_row.setColumnStretch(0, 1)
        self.start_queue_button = QPushButton("메타데이터 분석")
        self.start_queue_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_queue_button.clicked.connect(self._analyze_next)
        action_row.addWidget(self.start_queue_button, 0, 1)
        self.download_approved_button = QPushButton("승인 항목 다운로드")
        self.download_approved_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.download_approved_button.clicked.connect(self._download_next_approved)
        action_row.addWidget(self.download_approved_button, 0, 2)
        self.cancel_current_button = QPushButton("현재 작업 취소")
        self.cancel_current_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
        self.cancel_current_button.clicked.connect(self._cancel_current_job)
        action_row.addWidget(self.cancel_current_button, 0, 3)
        self.analyze_selected_button = QPushButton("선택 항목 분석")
        self.analyze_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.analyze_selected_button.clicked.connect(self._analyze_selected)
        action_row.addWidget(self.analyze_selected_button, 1, 1)
        self.download_selected_button = QPushButton("선택 항목 다운로드")
        self.download_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.download_selected_button.clicked.connect(self._download_selected_approved)
        action_row.addWidget(self.download_selected_button, 1, 2)
        self.retry_selected_button = QPushButton("선택 항목 재시도")
        self.retry_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.retry_selected_button.clicked.connect(self._retry_selected)
        action_row.addWidget(self.retry_selected_button, 1, 3)
        self.review_selected_button = QPushButton("선택 항목 검수로 이동")
        self.review_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.review_selected_button.clicked.connect(self._move_selected_to_review_queue)
        action_row.addWidget(self.review_selected_button, 2, 1)
        self.retry_failed_button = QPushButton("실패 재시도")
        self.retry_failed_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.retry_failed_button.clicked.connect(self._retry_failed)
        action_row.addWidget(self.retry_failed_button, 2, 2)
        self.remove_selected_button = QPushButton("선택 항목 삭제")
        self.remove_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.remove_selected_button.clicked.connect(self._remove_selected)
        action_row.addWidget(self.remove_selected_button, 2, 3)
        layout.addLayout(action_row)
        layout.addWidget(self.queue_status_label)
        layout.addWidget(self.dependency_status_label)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.log)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)
        return root

    def _review_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)

        self.review_scroll_area = QScrollArea()
        self.review_scroll_area.setWidgetResizable(True)
        self.review_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.review_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.review_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

        content = QWidget()
        content.setMinimumHeight(760)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        splitter.setStyleSheet(
            """
            QSplitter::handle:vertical {
                background: #c9ced6;
                border: 1px solid #aeb5c0;
                margin: 3px 0;
            }
            QSplitter::handle:vertical:hover {
                background: #8f9bad;
            }
            """
        )
        self.review_splitter = splitter

        review_queue_group = QGroupBox("검수 큐")
        review_queue_group.setMinimumHeight(96)
        review_queue_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        review_queue_layout = QVBoxLayout(review_queue_group)
        review_queue_layout.addWidget(self.review_queue_table)

        provider_group = QGroupBox("태그 제공자")
        provider_group.setMinimumHeight(112)
        provider_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        provider_layout = QVBoxLayout(provider_group)
        provider_layout.addWidget(self.review_state_label)
        provider_layout.addWidget(self.review_hint_label)
        provider_layout.addWidget(self.candidate_label)
        provider_layout.addWidget(self.confidence_detail_label)
        provider_layout.addWidget(self.candidate_table)
        provider_layout.addWidget(QLabel("후보 미리보기"))
        provider_layout.addWidget(self.candidate_preview_table)
        candidate_action_row = QHBoxLayout()
        candidate_action_row.addStretch(1)
        self.apply_candidate_button = QPushButton("후보 적용")
        self.apply_candidate_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.apply_candidate_button.clicked.connect(self._apply_pending_candidate)
        self.apply_candidate_button.setEnabled(False)
        candidate_action_row.addWidget(self.apply_candidate_button)
        provider_layout.addLayout(candidate_action_row)

        tag_editor_group = QGroupBox("태그 편집")
        tag_editor_group.setMinimumHeight(140)
        tag_editor_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tag_editor_layout = QVBoxLayout(tag_editor_group)

        form = QFormLayout()
        labels = {
            "title": "제목",
            "artist": "아티스트",
            "album": "앨범",
            "album_artist": "앨범 아티스트",
            "genre": "장르",
            "release_date": "날짜",
            "bpm": "BPM",
            "label": "레이블",
            "isrc": "ISRC",
            "cover_url": "커버 URL",
        }
        for key, label in labels.items():
            form.addRow(label, self.review_fields[key])

        cover_panel = QWidget()
        cover_layout = QVBoxLayout(cover_panel)
        cover_layout.setContentsMargins(0, 0, 0, 0)
        cover_layout.addWidget(self.cover_preview_label, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        cover_layout.addWidget(self.cover_source_label)
        cover_layout.addStretch(1)
        cover_panel.setFixedWidth(220)

        editor_body = QWidget()
        editor_body_layout = QGridLayout(editor_body)
        editor_body_layout.setContentsMargins(0, 0, 0, 0)
        editor_body_layout.addLayout(form, 0, 0)
        editor_body_layout.addWidget(cover_panel, 0, 1, Qt.AlignmentFlag.AlignTop)
        editor_body_layout.setColumnStretch(0, 1)
        editor_body_layout.setColumnStretch(1, 0)
        tag_editor_layout.addWidget(editor_body)

        review_action_row = QHBoxLayout()
        review_action_row.addStretch(1)
        self.reopen_review_button = QPushButton("검수 큐로 이동")
        self.reopen_review_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.reopen_review_button.clicked.connect(self._move_active_to_review_queue)
        review_action_row.addWidget(self.reopen_review_button)
        self.approve_button = QPushButton("메타데이터 승인")
        self.approve_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.approve_button.clicked.connect(self._approve_selected)
        review_action_row.addWidget(self.approve_button)
        tag_editor_layout.addLayout(review_action_row)

        splitter.addWidget(review_queue_group)
        splitter.addWidget(provider_group)
        splitter.addWidget(tag_editor_group)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 3)
        splitter.setSizes([210, 270, 280])
        content_layout.addWidget(splitter)
        self.review_scroll_area.setWidget(content)
        layout.addWidget(self.review_scroll_area)
        return root

    def _settings_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        paths_group = QGroupBox("경로 및 인증")
        form = QFormLayout(paths_group)

        output_row = QWidget()
        output_layout = QGridLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_dir_input, 0, 0)
        output_button = QPushButton("찾아보기")
        output_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        output_button.clicked.connect(self._browse_output_dir)
        output_layout.addWidget(output_button, 0, 1)
        form.addRow("출력 폴더", output_row)

        form.addRow("브라우저 쿠키", self.cookie_combo)
        form.addRow(self.cookie_unlock_checkbox)
        form.addRow("YTMusic 인증 JSON (고급)", self._path_row(self.auth_path_input, self._browse_auth_file))
        form.addRow("ffmpeg 경로", self._path_row(self.ffmpeg_path_input, self._browse_ffmpeg))

        recognition_group = QGroupBox("오디오 인식")
        recognition_form = QFormLayout(recognition_group)
        recognition_form.addRow(self.audio_recognition_checkbox)
        recognition_form.addRow(self.verify_auto_approved_checkbox)
        recognition_form.addRow("AcoustID 클라이언트 키", self.acoustid_key_input)
        recognition_form.addRow("fpcalc 경로", self._path_row(self.fpcalc_path_input, self._browse_fpcalc))

        self.open_onboarding_button = QPushButton("초기 설정 다시 열기")
        self.open_onboarding_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.open_onboarding_button.clicked.connect(self._open_onboarding)
        diagnostics_button = QPushButton("진단 정보 복사")
        diagnostics_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        diagnostics_button.clicked.connect(self._copy_diagnostics)

        layout.addWidget(paths_group)
        layout.addWidget(recognition_group)
        layout.addWidget(self.open_onboarding_button)
        layout.addWidget(diagnostics_button)
        layout.addStretch()
        return root

    def _path_row(self, line_edit: QLineEdit, callback: Any) -> QWidget:
        row = QWidget()
        layout = QGridLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit, 0, 0)
        button = QPushButton("찾아보기")
        button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        button.clicked.connect(callback)
        layout.addWidget(button, 0, 1)
        return row

    def _load_settings(self) -> None:
        self.output_dir_input.setText(str(self._settings.value("paths/output_dir", self.output_dir_input.text())))
        self.auth_path_input.setText(str(self._settings.value("paths/ytmusic_auth", "")))
        self.ffmpeg_path_input.setText(str(self._settings.value("paths/ffmpeg", "")))
        self.fpcalc_path_input.setText(str(self._settings.value("paths/fpcalc", "")))
        self.acoustid_key_input.setText(str(self._settings.value("acoustid/client_key", "")))
        self.audio_recognition_checkbox.setChecked(_settings_bool(self._settings.value("acoustid/enabled", True), default=True))
        self.verify_auto_approved_checkbox.setChecked(
            _settings_bool(self._settings.value("acoustid/verify_auto_approved", False), default=False)
        )
        self._set_cookie_browser(str(self._settings.value("auth/cookie_browser", "")))
        self.cookie_unlock_checkbox.setChecked(
            _settings_bool(self._settings.value("auth/unlock_browser_cookie_database", False), default=False)
        )

    def save_settings(self) -> None:
        self._settings.setValue("paths/output_dir", self.output_dir_input.text().strip())
        self._settings.setValue("paths/ytmusic_auth", self.auth_path_input.text().strip())
        self._settings.setValue("paths/ffmpeg", self.ffmpeg_path_input.text().strip())
        self._settings.setValue("paths/fpcalc", self.fpcalc_path_input.text().strip())
        self._settings.setValue("acoustid/client_key", self.acoustid_key_input.text().strip())
        self._settings.setValue("acoustid/enabled", self.audio_recognition_checkbox.isChecked())
        self._settings.setValue("acoustid/verify_auto_approved", self.verify_auto_approved_checkbox.isChecked())
        cookie_browser = self.cookie_combo.currentData()
        self._settings.setValue("auth/cookie_browser", _cookie_browser_value(cookie_browser))
        self._settings.setValue("auth/unlock_browser_cookie_database", self.cookie_unlock_checkbox.isChecked())
        self._settings.sync()
        if hasattr(self, "dependency_status_label"):
            self.dependency_status_label.setText(self._settings_status_text())

    def _set_cookie_browser(self, value: str) -> None:
        for index in range(self.cookie_combo.count()):
            item = self.cookie_combo.itemData(index)
            item_value = _cookie_browser_value(item)
            if item_value == value:
                self.cookie_combo.setCurrentIndex(index)
                return

    def _settings_status_text(self) -> str:
        ffmpeg = find_executable("ffmpeg", explicit_path=_optional_path(self.ffmpeg_path_input.text()))
        fpcalc = find_executable("fpcalc", explicit_path=_optional_path(self.fpcalc_path_input.text()))
        acoustid = "설정됨" if self.acoustid_key_input.text().strip() else "미설정"
        cookies = _cookie_browser_value(self.cookie_combo.currentData()) or "없음"
        cookie_unlock = "켜짐" if self.cookie_unlock_checkbox.isChecked() else "꺼짐"
        ytmusic_auth = "수동 JSON" if self.auth_path_input.text().strip() else ("브라우저 쿠키 자동" if cookies != "없음" else "미설정")
        return (
            f"설정: ffmpeg {ffmpeg.source if ffmpeg.available else '없음'}; "
            f"fpcalc {fpcalc.source if fpcalc.available else '없음'}; "
            f"AcoustID {acoustid}; 브라우저 쿠키 {cookies}; 쿠키 잠금 해제 {cookie_unlock}; "
            f"YTMusic 인증 {ytmusic_auth}."
        )

    def _open_onboarding(self) -> None:
        if self.onboarding_dialog and self.onboarding_dialog.isVisible():
            self.onboarding_dialog.raise_()
            self.onboarding_dialog.activateWindow()
            return
        dialog = OnboardingDialog(
            parent=self,
            dependency_rows=self._onboarding_dependency_rows(),
            optional_rows=self._onboarding_optional_rows(),
            on_done=self._complete_onboarding,
        )
        dialog.finished.connect(lambda _result: self._onboarding_finished(dialog))
        self.onboarding_dialog = dialog
        dialog.show()

    def _complete_onboarding(self) -> None:
        self._settings.setValue("onboarding/completed", True)
        self._settings.sync()

    def _onboarding_finished(self, dialog: OnboardingDialog) -> None:
        if self.onboarding_dialog is dialog:
            self.onboarding_dialog = None
        dialog.deleteLater()

    def _onboarding_dependency_rows(self) -> list[tuple[str, str]]:
        return [
            ("ffmpeg", _dependency_setup_status("ffmpeg", explicit_path=_optional_path(self.ffmpeg_path_input.text()))),
            ("Deno", _dependency_setup_status("deno")),
            ("fpcalc", _dependency_setup_status("fpcalc", explicit_path=_optional_path(self.fpcalc_path_input.text()))),
        ]

    def _onboarding_optional_rows(self) -> list[tuple[str, str]]:
        cookie = _cookie_browser_value(self.cookie_combo.currentData()) or "사용 안 함"
        cookie_unlock = "켜짐" if self.cookie_unlock_checkbox.isChecked() else "꺼짐"
        ytmusic_auth = "수동 JSON" if self.auth_path_input.text().strip() else ("브라우저 쿠키 자동" if cookie != "사용 안 함" else "미설정")
        return [
            ("브라우저 쿠키", cookie),
            ("YTMusic 인증", ytmusic_auth),
            ("YTMusic 인증 JSON", "고급 fallback 설정됨" if self.auth_path_input.text().strip() else "고급 fallback 미설정"),
            ("쿠키 잠금 해제", cookie_unlock),
            ("AcoustID 클라이언트 키", "설정됨" if self.acoustid_key_input.text().strip() else "미설정"),
        ]

    def closeEvent(self, event: Any) -> None:
        self.save_settings()
        super().closeEvent(event)

    def _add_url(self) -> None:
        urls = _extract_urls(self.url_input.text())
        if not urls:
            return
        output_dir = Path(self.output_dir_input.text().strip() or "downloads")
        last_row = -1
        for url in urls:
            job = DownloadJob(url=url, output_dir=output_dir)
            self.jobs[job.id] = job
            self.row_job_ids.append(job.id)
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col in range(len(self.COLUMNS)):
                self.table.setItem(row, col, QTableWidgetItem(""))
            self._update_row(job)
            self._append_log(job.id, "큐에 추가됨")
            last_row = row
        self.url_input.clear()
        if last_row >= 0:
            self.table.selectRow(last_row)
        self._refresh_actions()

    def _start_next(self) -> None:
        self._analyze_next()

    def _analyze_next(self) -> None:
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        for job_id in self.row_job_ids:
            job = self.jobs[job_id]
            if job.status == DownloadStatus.PENDING:
                self._run_worker(job, analyze_only=True)
                return
        self._refresh_actions()

    def _analyze_selected(self) -> None:
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        job = self._selected_job()
        if not job or job.status not in _ANALYZABLE_STATUSES:
            self._refresh_actions()
            return
        self._prepare_job_retry(job, message="선택 항목 분석 시작")
        self._run_worker(job, analyze_only=True, continue_queue=False)

    def _download_next_approved(self) -> None:
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        for job_id in self.row_job_ids:
            job = self.jobs[job_id]
            if job.status == DownloadStatus.APPROVED:
                self._run_worker(job, approved_metadata=job.selected_metadata)
                return
        self._refresh_actions()

    def _download_selected_approved(self) -> None:
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        job = self._selected_job()
        if not job or job.status != DownloadStatus.APPROVED:
            self._refresh_actions()
            return
        self._run_worker(job, approved_metadata=job.selected_metadata, continue_queue=False)

    def _retry_failed(self) -> None:
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        retried = 0
        for job in self.jobs.values():
            if job.status != DownloadStatus.FAILED:
                continue
            job.status = DownloadStatus.PENDING
            job.progress = 0.0
            job.error = ""
            self._update_row(job)
            self._append_log(job.id, "재시도를 위해 큐에 추가됨")
            retried += 1
        self._refresh_actions()
        if retried:
            self._analyze_next()

    def _retry_selected(self) -> None:
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        job = self._selected_job()
        if not job or job.status not in {DownloadStatus.FAILED, DownloadStatus.CANCELED}:
            self._refresh_actions()
            return
        self._prepare_job_retry(job, message="선택 항목 재시도 시작")
        self._run_worker(job, analyze_only=True, continue_queue=False)

    def _prepare_job_retry(self, job: DownloadJob, *, message: str) -> None:
        _cleanup_temp_download(job)
        job.status = DownloadStatus.PENDING
        job.progress = 0.0
        job.error = ""
        self._update_row(job)
        self._append_log(job.id, message)

    def _cancel_current_job(self) -> None:
        if not self.worker or not self.worker.isRunning():
            self._refresh_actions()
            return
        self.cancel_requested = True
        if hasattr(self.worker, "cancel"):
            self.worker.cancel()
        else:
            self.worker.requestInterruption()
        job_id = getattr(getattr(self.worker, "job", None), "id", "system")
        self._append_log(job_id, "현재 작업 취소 요청됨")
        self._refresh_actions()

    def _run_worker(
        self,
        job: DownloadJob,
        approved_metadata: TrackMetadata | None = None,
        *,
        analyze_only: bool = False,
        continue_queue: bool = True,
    ) -> None:
        self.save_settings()
        self.cancel_requested = False
        job.status = DownloadStatus.DOWNLOADING if approved_metadata else DownloadStatus.METADATA
        self._update_row(job)
        self.worker_mode = ("analysis" if analyze_only else "download") if continue_queue else "single"
        self.worker = JobWorker(
            job,
            cookie_browser=self.cookie_combo.currentData(),
            unlock_browser_cookie_database=self.cookie_unlock_checkbox.isChecked(),
            ytmusic_auth_path=_optional_path(self.auth_path_input.text()),
            ffmpeg_location=_optional_path(self.ffmpeg_path_input.text()),
            acoustid_config=AcoustIDConfig(
                client_key=self.acoustid_key_input.text().strip(),
                fpcalc_path=_optional_path(self.fpcalc_path_input.text()),
            ),
            audio_recognition_enabled=self.audio_recognition_checkbox.isChecked(),
            verify_auto_approved_metadata=self.verify_auto_approved_checkbox.isChecked(),
            approved_metadata=approved_metadata,
            analyze_only=analyze_only,
        )
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.metadata_ready.connect(self._on_metadata_ready)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.job_failed.connect(self._on_job_failed)
        self.worker.job_canceled.connect(self._on_job_canceled)
        self.worker.log_message.connect(self._append_log)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()
        self._refresh_actions()

    def _on_progress(self, job_id: str, percent: float, status: str) -> None:
        job = self.jobs[job_id]
        job.progress = percent
        if status in DownloadStatus._value2member_map_:
            job.status = DownloadStatus(status)
        self._update_row(job)

    def _on_metadata_ready(
        self,
        job_id: str,
        metadata: TrackMetadata,
        state: ReviewState | str,
        candidates: list[MetadataCandidate],
    ) -> None:
        review_state = _review_state_value(state)
        job = self.jobs[job_id]
        job.selected_metadata = metadata
        job.candidates = candidates
        if review_state == ReviewState.AUTO_APPROVED:
            job.status = DownloadStatus.APPROVED
        else:
            job.status = DownloadStatus.REVIEW_REQUIRED
            loaded_review = self._loaded_review_job()
            if not loaded_review or loaded_review.id == job.id or loaded_review.status != DownloadStatus.REVIEW_REQUIRED:
                self._load_job_for_review(job, select_row=False)
        self._update_row(job)
        if review_state == ReviewState.AUTO_APPROVED:
            self._append_log(job_id, "메타데이터 자동 승인됨; 다운로드 준비 완료")
        else:
            self._append_log(job_id, f"메타데이터 검수 필요: {_review_state_label(review_state)}")
        self._refresh_actions()

    def _on_job_done(self, job_id: str, final_path: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.DONE
        job.progress = 100.0
        job.final_path = Path(final_path)
        self._update_row(job)
        self._append_log(job_id, f"완료: {final_path}")
        self._refresh_actions()

    def _on_job_failed(self, job_id: str, error: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.FAILED
        job.error = error
        self._update_row(job)
        self._append_log(job_id, f"실패: {error}")
        self._refresh_actions()

    def _on_job_canceled(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.CANCELED
        job.progress = 0.0
        job.error = ""
        self.worker_mode = "canceled"
        self._update_row(job)
        self._append_log(job_id, "작업이 취소됨")
        self._refresh_actions()

    def _worker_finished(self) -> None:
        mode = self.worker_mode
        self.worker = None
        self.worker_mode = ""
        self.cancel_requested = False
        self._refresh_actions()
        if mode == "analysis":
            self._analyze_next()
        elif mode == "download":
            self._download_next_approved()

    def _approve_selected(self) -> None:
        job = self._active_review_job()
        if not job:
            self.log.appendPlainText("[검수] 승인 생략: 검수할 트랙이 로드되지 않음")
            QMessageBox.warning(self, "트랙 없음", "승인하기 전에 검수 탭에서 트랙을 로드하세요.")
            return
        metadata = self._metadata_from_review_fields(job.selected_metadata)
        job.selected_metadata = metadata
        job.status = DownloadStatus.APPROVED
        self._update_row(job)
        self._append_log(job.id, "메타데이터 승인됨; 다운로드 준비 완료")
        self._load_next_review_or_current(job)
        self._refresh_actions()

    def _move_selected_to_review_queue(self) -> None:
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "트랙 선택 없음", "먼저 큐 탭에서 승인된 트랙을 선택하세요.")
            return
        self._move_job_to_review_queue(job)

    def _move_active_to_review_queue(self) -> None:
        job = self._active_review_job()
        if not job:
            QMessageBox.warning(self, "트랙 없음", "검수 큐로 이동하기 전에 승인된 트랙을 로드하세요.")
            return
        self._move_job_to_review_queue(job)

    def _move_job_to_review_queue(self, job: DownloadJob) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "이동할 수 없음", "검수 상태를 바꾸기 전에 현재 작업이 끝날 때까지 기다리세요.")
            return
        if job.status != DownloadStatus.APPROVED:
            QMessageBox.warning(self, "승인되지 않음", "승인된 트랙만 검수 큐로 다시 이동할 수 있습니다.")
            return
        job.status = DownloadStatus.REVIEW_REQUIRED
        self._update_row(job)
        self._append_log(job.id, "승인된 메타데이터를 검수 큐로 다시 이동함")
        self._load_job_for_review(job)
        if self.tabs:
            self.tabs.setCurrentIndex(self.review_tab_index)
        self._refresh_actions()

    def _remove_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        job_id = self.row_job_ids[row]
        job = self.jobs[job_id]
        if job.status in {DownloadStatus.DOWNLOADING, DownloadStatus.METADATA, DownloadStatus.TAGGING}:
            QMessageBox.warning(self, "삭제할 수 없음", "실행 중인 작업은 삭제할 수 없습니다.")
            return
        _cleanup_temp_download(job)
        self.table.removeRow(row)
        self.row_job_ids.pop(row)
        del self.jobs[job_id]
        if self.active_review_job_id == job_id:
            self.active_review_job_id = None
            next_review = self._next_review_job()
            if next_review:
                self._load_job_for_review(next_review, select_row=False)
            else:
                self._clear_review_panel()
        self._refresh_actions()

    def _load_selected_job(self) -> None:
        job = self._selected_job()
        if job:
            self._load_job_for_review(job)
            self._refresh_actions()

    def _load_selected_review_queue_job(self) -> None:
        if self._loading_review_queue:
            return
        row = self.review_queue_table.currentRow()
        if row < 0:
            return
        item = self.review_queue_table.item(row, 0)
        if not item:
            return
        job_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(job_id, str) and job_id in self.jobs:
            self._load_job_for_review(self.jobs[job_id], select_row=False)

    def _load_job_for_review(self, job: DownloadJob, *, select_row: bool = True) -> None:
        self.active_review_job_id = job.id
        if select_row:
            self._select_job_row(job)
        metadata = job.selected_metadata
        platform = detect_source_platform(job.url)
        self._loading_review = True
        self.review_state_label.setText(f"{_download_status_label(job.status)}: {platform.display_name}: {job.url}")
        if job.status == DownloadStatus.REVIEW_REQUIRED:
            self.review_hint_label.setText("필요하면 태그를 수정하세요. 승인하면 이 트랙이 다운로드 준비 상태가 됩니다.")
        elif job.status == DownloadStatus.APPROVED:
            self.review_hint_label.setText("승인됨. 승인 항목 다운로드를 실행하거나, 추가 수정을 위해 검수 큐로 되돌릴 수 있습니다.")
        elif job.status == DownloadStatus.DONE:
            self.review_hint_label.setText("이미 다운로드 및 태깅이 완료된 트랙입니다.")
        elif job.status == DownloadStatus.FAILED:
            self.review_hint_label.setText(job.error or "이 트랙은 실패했습니다. 필요하면 태그를 수정한 뒤 재시도하세요.")
        else:
            self.review_hint_label.setText("선택한 큐 항목의 메타데이터 미리보기입니다.")
        if job.candidates:
            best = job.candidates[0]
            self._set_candidate_summary(best, reference=metadata)
        else:
            self.candidate_label.setText("외부 후보 없음")
            self.confidence_detail_label.setText("사용 가능한 메타데이터 제공자 후보가 없습니다. 승인 전에 필드를 직접 수정하세요.")
        self.pending_candidate_index = None
        self._clear_candidate_preview()
        self._populate_candidate_table(job)
        self._set_review_fields(metadata)
        self._loading_review = False
        self._refresh_cover_preview(job, metadata)
        self._refresh_actions()

    def _load_next_review_or_current(self, current: DownloadJob) -> None:
        next_review = self._next_review_job(exclude_id=current.id)
        if next_review:
            self._load_job_for_review(next_review, select_row=False)
        else:
            self._load_job_for_review(current, select_row=False)

    def _clear_review_panel(self) -> None:
        self.review_state_label.setText("선택된 트랙 없음")
        self.review_hint_label.setText("메타데이터 검수가 필요한 트랙이 여기에 표시됩니다.")
        self.candidate_label.setText("")
        self.confidence_detail_label.setText("")
        self.candidate_table.setRowCount(0)
        self.pending_candidate_index = None
        self._clear_candidate_preview()
        self._set_review_fields(TrackMetadata())
        self.cover_preview_label.setPixmap(QPixmap())
        self.cover_preview_label.setText("커버 없음")
        self.cover_source_label.setText("커버 출처: 없음")

    def _set_candidate_summary(self, candidate: MetadataCandidate, *, reference: TrackMetadata | None = None) -> None:
        trust_note = ""
        if candidate.provider == "soundcloud" and candidate.raw.get("trusted_native"):
            trust_note = " - SoundCloud 메타데이터 신뢰"
        matched = ", ".join(candidate.matched_fields) or "일치 항목 없음"
        bucket = _confidence_bucket(candidate)
        bpm_note = ""
        if candidate.metadata.bpm:
            bpm_note = f"; BPM {candidate.metadata.bpm} 출처 {candidate.metadata.bpm_source or candidate.provider}"
        self.candidate_label.setText(f"최상위 후보: {candidate.provider} {candidate.score:.2f} - {bucket} ({matched}){trust_note}{bpm_note}")
        self.confidence_detail_label.setText(_confidence_explanation(candidate, reference=reference))

    def _populate_candidate_table(self, job: DownloadJob) -> None:
        self.candidate_table.setRowCount(len(job.candidates))
        for row, candidate in enumerate(job.candidates):
            values = (
                candidate.provider,
                f"{candidate.score:.3f}",
                _confidence_bucket(candidate),
                _candidate_badges(candidate, job.selected_metadata),
                _bpm_display(candidate.metadata),
                ", ".join(candidate.matched_fields),
                candidate.metadata.title,
                candidate.metadata.artist,
                candidate.metadata.album,
                candidate.metadata.release_date,
                candidate.metadata.isrc,
                candidate.metadata.cover_source or _cover_source_from_url(candidate.metadata.cover_url),
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.candidate_table.setItem(row, col, item)
        self.candidate_table.resizeRowsToContents()

    def _set_review_fields(self, metadata: TrackMetadata) -> None:
        for key, field in self.review_fields.items():
            field.setText(str(getattr(metadata, key) or ""))

    def _preview_selected_candidate(self) -> None:
        if self._loading_review:
            return
        job = self._active_review_job()
        row = self.candidate_table.currentRow()
        if not job or row < 0:
            self.pending_candidate_index = None
            self._clear_candidate_preview()
            return
        item = self.candidate_table.item(row, 0)
        if not item:
            self.pending_candidate_index = None
            self._clear_candidate_preview()
            return
        candidate_index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(candidate_index, int) or candidate_index >= len(job.candidates):
            self.pending_candidate_index = None
            self._clear_candidate_preview()
            return
        candidate = job.candidates[candidate_index]
        self.pending_candidate_index = candidate_index
        self._set_candidate_summary(candidate, reference=job.selected_metadata)
        self._populate_candidate_preview(job.selected_metadata, candidate.metadata.with_defaults_from(job.selected_metadata).normalized())
        if self.apply_candidate_button:
            self.apply_candidate_button.setEnabled(True)

    def _apply_pending_candidate(self) -> None:
        job = self._active_review_job()
        candidate_index = self.pending_candidate_index
        if not job or candidate_index is None or candidate_index >= len(job.candidates):
            return
        candidate = job.candidates[candidate_index]
        metadata = candidate.metadata.with_defaults_from(job.selected_metadata).normalized()
        job.selected_metadata = metadata
        self._set_candidate_summary(candidate, reference=metadata)
        self._set_review_fields(metadata)
        self._populate_candidate_preview(metadata, metadata)
        self._update_row(job)
        self._refresh_cover_preview(job, metadata)

    def _populate_candidate_preview(self, current: TrackMetadata, applied: TrackMetadata) -> None:
        rows = _candidate_preview_rows(current, applied)
        self.candidate_preview_table.setRowCount(len(rows))
        conflict_fields = set(_metadata_conflict_fields(current, applied))
        changed_color = QColor("#fff4cc")
        conflict_color = QColor("#ffd6d6")
        for row, (field_key, label, current_value, applied_value) in enumerate(rows):
            values = (label, current_value, applied_value)
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if current_value != applied_value:
                    item.setBackground(conflict_color if field_key in conflict_fields else changed_color)
                self.candidate_preview_table.setItem(row, col, item)
        self.candidate_preview_table.resizeRowsToContents()

    def _clear_candidate_preview(self) -> None:
        self.candidate_preview_table.setRowCount(0)
        if self.apply_candidate_button:
            self.apply_candidate_button.setEnabled(False)

    def _cover_url_edited(self) -> None:
        if self._loading_review:
            return
        job = self._active_review_job()
        if not job:
            return
        metadata = self._metadata_from_review_fields(job.selected_metadata)
        job.selected_metadata = metadata
        self._refresh_cover_preview(job, metadata)

    def _refresh_cover_preview(self, job: DownloadJob, metadata: TrackMetadata) -> None:
        cover_url = self.review_fields["cover_url"].text().strip()
        source = metadata.cover_source or _cover_source_from_url(cover_url)
        self.cover_source_label.setText(f"커버 출처: {source or '없음'}")
        if not cover_url:
            self.cover_preview_label.setPixmap(QPixmap())
            self.cover_preview_label.setText("커버 없음")
            return

        self.cover_preview_label.setPixmap(QPixmap())
        self.cover_preview_label.setText("커버 불러오는 중...")
        worker = CoverPreviewWorker(job.id, cover_url)
        worker.cover_loaded.connect(self._on_cover_preview_loaded)
        worker.finished.connect(lambda worker=worker: self._cover_preview_finished(worker))
        self._cover_preview_workers.append(worker)
        worker.start()

    def _on_cover_preview_loaded(self, job_id: str, url: str, data: bytes, error: str) -> None:
        job = self._active_review_job()
        if not job or job.id != job_id or self.review_fields["cover_url"].text().strip() != url:
            return
        if error:
            self.cover_preview_label.setPixmap(QPixmap())
            self.cover_preview_label.setText("커버 사용 불가")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.cover_preview_label.setPixmap(QPixmap())
            self.cover_preview_label.setText("커버 사용 불가")
            return
        scaled = pixmap.scaled(
            self.cover_preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.cover_preview_label.setText("")
        self.cover_preview_label.setPixmap(scaled)

    def _cover_preview_finished(self, worker: CoverPreviewWorker) -> None:
        if worker in self._cover_preview_workers:
            self._cover_preview_workers.remove(worker)
        worker.deleteLater()

    def _metadata_from_review_fields(self, base: TrackMetadata) -> TrackMetadata:
        cover_url = self.review_fields["cover_url"].text().strip()
        cover_source = "manual" if cover_url and cover_url != base.cover_url else base.cover_source
        bpm = _optional_bpm(self.review_fields["bpm"].text())
        bpm_source = base.bpm_source
        bpm_confidence = base.bpm_confidence
        if bpm != base.bpm:
            bpm_source = "manual" if bpm else ""
            bpm_confidence = 1.0 if bpm else None
        return TrackMetadata(
            title=self.review_fields["title"].text().strip(),
            artist=self.review_fields["artist"].text().strip(),
            album=self.review_fields["album"].text().strip(),
            album_artist=self.review_fields["album_artist"].text().strip(),
            genre=self.review_fields["genre"].text().strip(),
            release_date=self.review_fields["release_date"].text().strip(),
            bpm=bpm,
            bpm_source=bpm_source,
            bpm_confidence=bpm_confidence,
            label=self.review_fields["label"].text().strip(),
            isrc=self.review_fields["isrc"].text().strip(),
            cover_url=cover_url,
            cover_source=cover_source,
            source_url=base.source_url,
            musicbrainz_recording_id=base.musicbrainz_recording_id,
            musicbrainz_release_id=base.musicbrainz_release_id,
            comments=base.comments,
        ).normalized()

    def _update_row(self, job: DownloadJob) -> None:
        row = self.row_job_ids.index(job.id)
        values = (
            _download_status_label(job.status),
            f"{job.progress:.0f}%",
            detect_source_platform(job.url).display_name,
            job.url,
            job.selected_metadata.title,
            job.selected_metadata.artist,
            str(job.final_path or job.output_dir),
        )
        for col, value in enumerate(values):
            self.table.item(row, col).setText(value)

    def _selected_job(self) -> DownloadJob | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.row_job_ids):
            return None
        return self.jobs[self.row_job_ids[row]]

    def _active_review_job(self) -> DownloadJob | None:
        if self.active_review_job_id:
            return self.jobs.get(self.active_review_job_id)
        return self._selected_job()

    def _loaded_review_job(self) -> DownloadJob | None:
        if not self.active_review_job_id:
            return None
        return self.jobs.get(self.active_review_job_id)

    def _next_review_job(self, *, exclude_id: str = "") -> DownloadJob | None:
        for job_id in self.row_job_ids:
            if job_id == exclude_id:
                continue
            job = self.jobs[job_id]
            if job.status == DownloadStatus.REVIEW_REQUIRED:
                return job
        return None

    def _select_job_row(self, job: DownloadJob) -> None:
        try:
            row = self.row_job_ids.index(job.id)
        except ValueError:
            return
        if self.table.currentRow() == row:
            return
        self.table.blockSignals(True)
        self.table.selectRow(row)
        self.table.blockSignals(False)

    def _refresh_actions(self) -> None:
        running = bool(self.worker and self.worker.isRunning())
        pending_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.PENDING)
        approved_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.APPROVED)
        review_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.REVIEW_REQUIRED)
        failed_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.FAILED)
        selected_job = self._selected_job()
        active_review = self._active_review_job()
        can_analyze = pending_count > 0 and not running
        can_download = approved_count > 0 and not running
        can_retry = failed_count > 0 and not running
        can_approve = bool(active_review and active_review.status == DownloadStatus.REVIEW_REQUIRED)
        can_move_selected_to_review = bool(selected_job and selected_job.status == DownloadStatus.APPROVED and not running)
        can_move_active_to_review = bool(active_review and active_review.status == DownloadStatus.APPROVED and not running)
        can_analyze_selected = bool(selected_job and selected_job.status in _ANALYZABLE_STATUSES and not running)
        can_download_selected = bool(selected_job and selected_job.status == DownloadStatus.APPROVED and not running)
        can_retry_selected = bool(selected_job and selected_job.status in {DownloadStatus.FAILED, DownloadStatus.CANCELED} and not running)
        can_remove_selected = bool(selected_job and selected_job.status not in _ACTIVE_STATUSES)
        can_cancel_current = running and not self.cancel_requested
        self._refresh_review_queue()

        if self.start_action:
            self.start_action.setEnabled(can_analyze)
        if self.download_action:
            self.download_action.setEnabled(can_download)
        if self.retry_action:
            self.retry_action.setEnabled(can_retry)
        if self.start_queue_button:
            self.start_queue_button.setEnabled(can_analyze)
        if self.download_approved_button:
            self.download_approved_button.setEnabled(can_download)
        if self.review_selected_button:
            self.review_selected_button.setEnabled(can_move_selected_to_review)
        if self.analyze_selected_button:
            self.analyze_selected_button.setEnabled(can_analyze_selected)
        if self.download_selected_button:
            self.download_selected_button.setEnabled(can_download_selected)
        if self.retry_selected_button:
            self.retry_selected_button.setEnabled(can_retry_selected)
        if self.retry_failed_button:
            self.retry_failed_button.setEnabled(can_retry)
        if self.remove_selected_button:
            self.remove_selected_button.setEnabled(can_remove_selected)
        if self.cancel_current_button:
            self.cancel_current_button.setEnabled(can_cancel_current)
        if self.approve_button:
            self.approve_button.setEnabled(can_approve)
        if self.reopen_review_button:
            self.reopen_review_button.setEnabled(can_move_active_to_review)
        if self.tabs:
            self.tabs.setTabText(self.review_tab_index, f"검수 ({review_count})" if review_count else "검수")

        if running and review_count:
            text = f"처리는 계속 진행 중입니다. {review_count}개 트랙은 메타데이터 검수가 필요합니다."
        elif running:
            text = "현재 트랙을 처리 중입니다."
        elif approved_count and pending_count:
            text = f"승인된 {approved_count}개 트랙은 다운로드 준비 완료, {pending_count}개 트랙은 아직 분석이 필요합니다."
        elif approved_count:
            text = f"승인된 {approved_count}개 트랙이 다운로드 준비 완료 상태입니다."
        elif review_count and pending_count:
            text = f"{review_count}개 트랙은 검수가 필요하고, {pending_count}개 트랙은 처리 대기 중입니다."
        elif review_count:
            text = f"{review_count}개 트랙은 메타데이터 검수가 필요합니다."
        elif pending_count:
            text = f"{pending_count}개 트랙이 준비되었습니다. 먼저 메타데이터를 분석하세요."
        elif failed_count:
            text = f"{failed_count}개 트랙이 실패했습니다. 실패 재시도로 다시 분석할 수 있습니다."
        elif self.jobs:
            text = "대기 중인 트랙이 없습니다."
        else:
            text = "URL을 추가한 뒤 큐를 처리하세요."
        self.queue_status_label.setText(text)
        self.dependency_status_label.setText(self._settings_status_text())

    def _refresh_review_queue(self) -> None:
        review_jobs = [self.jobs[job_id] for job_id in self.row_job_ids if self.jobs[job_id].status == DownloadStatus.REVIEW_REQUIRED]
        self._loading_review_queue = True
        self.review_queue_table.setRowCount(len(review_jobs))
        selected_row = -1
        for row, job in enumerate(review_jobs):
            best = max(job.candidates, key=lambda candidate: candidate.score, default=None)
            values = (
                job.selected_metadata.title,
                job.selected_metadata.artist,
                _confidence_bucket(best) if best else "수동",
                job.url,
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setData(Qt.ItemDataRole.UserRole, job.id)
                self.review_queue_table.setItem(row, col, item)
            if job.id == self.active_review_job_id:
                selected_row = row
        if selected_row >= 0:
            self.review_queue_table.selectRow(selected_row)
        else:
            self.review_queue_table.clearSelection()
        self.review_queue_table.resizeRowsToContents()
        self._loading_review_queue = False

    def _on_tab_changed(self, index: int) -> None:
        if index != self.review_tab_index:
            return
        loaded_review = self._loaded_review_job()
        if loaded_review and loaded_review.status == DownloadStatus.REVIEW_REQUIRED:
            return
        next_review = self._next_review_job()
        if next_review:
            self._load_job_for_review(next_review, select_row=False)

    def _append_log(self, job_id: str, message: str) -> None:
        short_id = job_id[:8]
        self.log.appendPlainText(f"[{short_id}] {message}")

    def _browse_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "출력 폴더", self.output_dir_input.text())
        if folder:
            self.output_dir_input.setText(folder)

    def _browse_auth_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "YTMusic 인증 JSON", "", "JSON 파일 (*.json);;모든 파일 (*)")
        if path:
            self.auth_path_input.setText(path)

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "ffmpeg 실행 파일", "", "실행 파일 (*.exe);;모든 파일 (*)")
        if path:
            self.ffmpeg_path_input.setText(path)

    def _browse_fpcalc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "fpcalc 실행 파일", "", "실행 파일 (*.exe);;모든 파일 (*)")
        if path:
            self.fpcalc_path_input.setText(path)

    def _copy_diagnostics(self) -> None:
        QApplication.clipboard().setText(format_diagnostics())
        self._append_log("system", "진단 정보가 클립보드에 복사됨")


def run_app() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


def _optional_path(value: str) -> Path | None:
    stripped = value.strip()
    return Path(stripped) if stripped else None


def _elapsed(started_at: float) -> str:
    return f"{time.monotonic() - started_at:.1f}s"


def _info_summary(info: dict[str, Any]) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "unknown")
    title = str(info.get("title") or "").strip()
    video_id = str(info.get("id") or "").strip()
    parts = [extractor]
    if video_id:
        parts.append(video_id)
    if title:
        parts.append(title[:80])
    return " / ".join(parts)


def _create_downloader(config: DownloadConfig, progress_callback: Any) -> YTDLPDownloader:
    return YTDLPDownloader(config, progress_callback=progress_callback)


def _audio_recognition_skip_reason(
    *,
    platform: SourcePlatform,
    state: ReviewState,
    enabled: bool,
    verify_auto_approved: bool,
    config: AcoustIDConfig,
) -> str:
    if state == ReviewState.AUTO_APPROVED and not verify_auto_approved:
        return "메타데이터가 이미 자동 승인됨"
    if platform not in {SourcePlatform.YOUTUBE, SourcePlatform.YOUTUBE_MUSIC}:
        if platform == SourcePlatform.SOUNDCLOUD:
            return "SoundCloud 기본 메타데이터 신뢰"
        return "지원 대상 소스가 아님"
    if not enabled:
        return "비활성화됨"
    if not config.client_key.strip():
        return "AcoustID 클라이언트 키 미설정"
    if not _has_fpcalc(config):
        return "fpcalc 실행 파일을 찾을 수 없음"
    return ""


def _has_fpcalc(config: AcoustIDConfig) -> bool:
    return find_executable("fpcalc", explicit_path=config.fpcalc_path).available


def _dependency_setup_status(name: str, *, explicit_path: Path | None = None) -> str:
    status = find_executable(name, explicit_path=explicit_path)
    if status.available and status.source == "bundled":
        return f"정상 감지됨: {status.path}"
    if not status.available and getattr(sys, "frozen", False):
        return "설치가 불완전함: 번들된 실행 파일을 찾을 수 없습니다."
    if status.available:
        return f"개발/portable fallback 감지됨 ({status.source}): {status.path}"
    return "누락됨: 개발/portable 실행이라면 PATH 또는 고급 경로 설정을 확인하세요."


def _merge_audio_recognition_candidates(
    *,
    metadata: TrackMetadata,
    state: ReviewState,
    candidates: list[MetadataCandidate],
    fingerprint_candidates: list[MetadataCandidate],
) -> tuple[TrackMetadata, ReviewState, list[MetadataCandidate]]:
    combined = sorted([*candidates, *fingerprint_candidates], key=lambda candidate: candidate.score, reverse=True)
    best_fingerprint = max(fingerprint_candidates, key=lambda candidate: candidate.score, default=None)
    if (
        state == ReviewState.AUTO_APPROVED
        and best_fingerprint
        and best_fingerprint.score >= 0.85
        and _metadata_conflicts(metadata, best_fingerprint.metadata)
    ):
        merged = best_fingerprint.metadata.with_defaults_from(metadata).normalized()
        return merged, ReviewState.REVIEW_REQUIRED, combined

    mergeable = [candidate for candidate in combined if candidate.score >= 0.65]
    if not mergeable:
        return metadata, state, combined
    merged, merged_state = merge_metadata(youtube=metadata, candidates=mergeable, fallback=metadata)
    return merged, merged_state, combined


def _temp_output_dir(job: DownloadJob) -> Path:
    return job.output_dir / ".cueforge-temp" / job.id


def _cleanup_temp_download(job: DownloadJob, *extra_paths: Path | None) -> None:
    temp_dir = _temp_output_dir(job)
    paths = [job.downloaded_path, *extra_paths]
    for path in paths:
        if path:
            _unlink_if_job_temp_file(path, temp_dir)
    if temp_dir.exists():
        for child in temp_dir.iterdir():
            if child.is_file():
                _unlink_if_job_temp_file(child, temp_dir)
        try:
            temp_dir.rmdir()
        except OSError:
            pass
    job.downloaded_path = None


def _unlink_if_job_temp_file(path: Path, temp_dir: Path) -> None:
    try:
        resolved_path = path.resolve()
        resolved_temp_dir = temp_dir.resolve()
    except OSError:
        return
    if resolved_path != resolved_temp_dir and resolved_temp_dir not in resolved_path.parents:
        return
    if not resolved_path.is_file():
        return
    try:
        resolved_path.unlink()
    except OSError:
        pass


def _move_to_final(downloaded: Path, output_dir: Path, metadata: TrackMetadata) -> Path:
    target = _unique_path(output_dir / safe_track_filename(metadata))
    if downloaded.resolve() == target.resolve():
        return downloaded
    target.parent.mkdir(parents=True, exist_ok=True)
    downloaded.replace(target)
    return target


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find available filename for {path}")


def _cover_source_from_url(url: str) -> str:
    lowered = url.casefold()
    if "coverartarchive.org" in lowered:
        return "Cover Art Archive"
    if "sndcdn.com" in lowered or "soundcloud" in lowered:
        return "SoundCloud 기본 커버"
    if "ytimg.com" in lowered or "youtube" in lowered:
        return "YouTube 대체 썸네일"
    return "수동" if url else ""


def _download_status_label(status: DownloadStatus | str) -> str:
    if isinstance(status, str) and status in DownloadStatus._value2member_map_:
        status = DownloadStatus(status)
    return {
        DownloadStatus.PENDING: "대기",
        DownloadStatus.METADATA: "메타데이터",
        DownloadStatus.REVIEW_REQUIRED: "검수 필요",
        DownloadStatus.APPROVED: "승인됨",
        DownloadStatus.DOWNLOADING: "다운로드 중",
        DownloadStatus.TAGGING: "태깅 중",
        DownloadStatus.DONE: "완료",
        DownloadStatus.FAILED: "실패",
        DownloadStatus.CANCELED: "취소됨",
    }.get(status, str(status))


def _review_state_label(state: ReviewState | str) -> str:
    if isinstance(state, str) and state in ReviewState._value2member_map_:
        state = ReviewState(state)
    return {
        ReviewState.AUTO_APPROVED: "자동 승인",
        ReviewState.REVIEW_REQUIRED: "검수 필요",
        ReviewState.MANUAL_REQUIRED: "수동 입력 필요",
    }.get(state, str(state))


def _trust_note_ko(platform: SourcePlatform) -> str:
    if platform == SourcePlatform.SOUNDCLOUD:
        return "리믹스, 부트렉, 에딧, 매시업 작업을 위해 SoundCloud 기본 메타데이터를 신뢰합니다."
    if platform in {SourcePlatform.YOUTUBE, SourcePlatform.YOUTUBE_MUSIC}:
        return "YouTube 메타데이터는 보조값으로 보고 음악 메타데이터 제공자로 보강합니다."
    return "알 수 없는 소스는 태깅 전에 검수가 필요합니다."


def _bpm_display(metadata: TrackMetadata) -> str:
    if not metadata.bpm:
        return ""
    source = metadata.bpm_source or "메타데이터"
    if metadata.bpm_confidence is None:
        return f"{metadata.bpm} ({source})"
    return f"{metadata.bpm} ({source} {metadata.bpm_confidence:.2f})"


def _candidate_badges(candidate: MetadataCandidate, current: TrackMetadata) -> str:
    badges: list[str] = []
    if candidate.metadata.title and current.title and text_similarity(candidate.metadata.title, current.title) >= 0.9:
        badges.append("제목 일치")
    if candidate.metadata.artist and current.artist and text_similarity(candidate.metadata.artist, current.artist) < 0.65:
        badges.append("아티스트 충돌")
    if candidate.metadata.bpm:
        badges.append("BPM 있음")
    if candidate.metadata.cover_url:
        badges.append("커버 있음")
    if candidate.metadata.isrc:
        badges.append("ISRC 있음")
    return ", ".join(badges)


def _candidate_preview_rows(current: TrackMetadata, applied: TrackMetadata) -> list[tuple[str, str, str, str]]:
    labels = (
        ("title", "제목"),
        ("artist", "아티스트"),
        ("album", "앨범"),
        ("album_artist", "앨범 아티스트"),
        ("genre", "장르"),
        ("release_date", "날짜"),
        ("bpm", "BPM"),
        ("label", "레이블"),
        ("isrc", "ISRC"),
        ("cover_url", "커버 URL"),
    )
    return [
        (field, label, str(getattr(current, field) or ""), str(getattr(applied, field) or ""))
        for field, label in labels
    ]


def _optional_bpm(value: str) -> int | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        bpm = int(float(stripped) + 0.5)
    except ValueError:
        return None
    return bpm if bpm > 0 else None


def _confidence_bucket(candidate: MetadataCandidate | None) -> str:
    if not candidate:
        return "수동"
    if candidate.score >= 0.85:
        return "자동"
    if candidate.score >= 0.65:
        return "검수"
    return "수동"


def _confidence_explanation(candidate: MetadataCandidate, *, reference: TrackMetadata | None = None) -> str:
    threshold = "자동 승인" if candidate.score >= 0.85 else "검수 필요" if candidate.score >= 0.65 else "수동 입력"
    matched = ", ".join(candidate.matched_fields) or "없음"
    parts = [f"{candidate.provider} 점수 {candidate.score:.2f}: {threshold}. 일치 항목: {matched}."]
    missing = [
        field
        for field in ("title", "artist", "album", "release_date", "isrc", "cover_url")
        if not getattr(candidate.metadata, field)
    ]
    if missing:
        parts.append(f"후보에서 누락된 필드: {', '.join(missing)}.")
    if reference:
        conflicts = _metadata_conflict_fields(reference, candidate.metadata)
        if conflicts:
            parts.append(f"현재 필드와 충돌: {', '.join(conflicts)}.")
    if candidate.metadata.bpm:
        bpm_source = candidate.metadata.bpm_source or candidate.provider
        if candidate.metadata.bpm_confidence is None:
            parts.append(f"BPM 출처: {bpm_source}; BPM: {candidate.metadata.bpm}.")
        else:
            parts.append(
                f"BPM 출처: {bpm_source}; BPM 신뢰도: {candidate.metadata.bpm_confidence:.2f}; BPM: {candidate.metadata.bpm}."
            )
    return " ".join(parts)


def _metadata_conflict_fields(left: TrackMetadata, right: TrackMetadata) -> list[str]:
    conflicts: list[str] = []
    for field in ("title", "artist", "album"):
        left_value = getattr(left, field)
        right_value = getattr(right, field)
        if left_value and right_value and text_similarity(left_value, right_value) < 0.65:
            conflicts.append(field)
    if left.release_date and right.release_date and left.release_date[:4] != right.release_date[:4]:
        conflicts.append("release_year")
    return conflicts


_URL_PATTERN = re.compile(r"https?://[^\s<>()\"']+", flags=re.IGNORECASE)
_ADJACENT_URL_SEPARATOR_PATTERN = re.compile(r"(?<=[^\s,;])[,;](?=https?://)", flags=re.IGNORECASE)


def _extract_urls(value: str) -> list[str]:
    normalized = _ADJACENT_URL_SEPARATOR_PATTERN.sub(" ", value.strip())
    matches = _URL_PATTERN.findall(normalized)
    candidates = matches if matches else re.split(r"[\s,;]+", normalized)

    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = candidate.strip().strip("<>()[]{}\"'")
        url = url.rstrip(".,;")
        if not url or url in seen:
            continue
        urls.append(url)
        seen.add(url)
    return urls


def _metadata_conflicts(left: TrackMetadata, right: TrackMetadata) -> bool:
    if left.title and right.title and text_similarity(left.title, right.title) < 0.65:
        return True
    if left.artist and right.artist and text_similarity(left.artist, right.artist) < 0.65:
        return True
    return False


def _settings_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.casefold()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _cookie_browser_value(value: Any) -> str:
    if isinstance(value, CookieBrowser):
        return value.value
    return str(value or "")


def _review_state_value(value: ReviewState | str) -> ReviewState:
    if isinstance(value, ReviewState):
        return value
    if isinstance(value, str) and value in ReviewState._value2member_map_:
        return ReviewState(value)
    return ReviewState.REVIEW_REQUIRED
