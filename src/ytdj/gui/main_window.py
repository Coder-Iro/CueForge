"""PySide6 desktop interface."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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

from ytdj.download import CookieBrowser, DownloadConfig, DownloadProgress, YTDLPDownloader
from ytdj.metadata import AcoustIDConfig, AcoustIDProvider, CoverArtProvider, MetadataResolver
from ytdj.metadata.fingerprint import FingerprintError, FingerprintUnavailable
from ytdj.metadata.matching import text_similarity
from ytdj.metadata.normalize import merge_metadata
from ytdj.models import DownloadJob, DownloadStatus, MetadataCandidate, ReviewState, TagWriteResult, TrackMetadata
from ytdj.runtime import find_executable, format_diagnostics
from ytdj.sources import SourcePlatform, detect_source_platform, trust_policy_for
from ytdj.tags import RekordboxTagWriter, safe_track_filename

DownloaderFactory = Callable[[DownloadConfig, Any], YTDLPDownloader]
ResolverFactory = Callable[[], MetadataResolver]
AcoustIDProviderFactory = Callable[[AcoustIDConfig], Any]
CoverArtProviderFactory = Callable[[], Any]
TagWriterFactory = Callable[[], Any]


class JobWorker(QThread):
    progress_changed = Signal(str, float, str)
    metadata_ready = Signal(str, object, object, object)
    job_done = Signal(str, str)
    job_failed = Signal(str, str)
    log_message = Signal(str, str)

    def __init__(
        self,
        job: DownloadJob,
        *,
        cookie_browser: CookieBrowser | None,
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

    def run(self) -> None:
        try:
            downloader = self._new_downloader(self.job.output_dir)
            metadata = self.approved_metadata
            downloaded_path = self.job.downloaded_path
            if metadata is None:
                metadata, state, candidates, platform = self._resolve_metadata(downloader)
                if state != ReviewState.AUTO_APPROVED or self.verify_auto_approved_metadata:
                    metadata, state, candidates, downloaded_path = self._try_audio_recognition(
                        metadata=metadata,
                        state=state,
                        candidates=candidates,
                        platform=platform,
                    )
                self.metadata_ready.emit(self.job.id, metadata, state, candidates)
                if self.analyze_only or state != ReviewState.AUTO_APPROVED:
                    return

            if downloaded_path and not downloaded_path.exists():
                self.log_message.emit(self.job.id, f"prepared download missing, downloading again: {downloaded_path}")
                downloaded_path = None

            if downloaded_path is None:
                result = downloader.download_audio(self.job.url)
                downloaded_path = result.path
            self.progress_changed.emit(self.job.id, 100.0, DownloadStatus.TAGGING.value)
            final_path = _move_to_final(downloaded_path, self.job.output_dir, metadata)
            tag_result: TagWriteResult = self._tag_writer_factory().write(final_path, metadata)
            if tag_result.written_fields:
                self.log_message.emit(self.job.id, f"tags written: {', '.join(tag_result.written_fields)}")
            if tag_result.skipped_fields:
                self.log_message.emit(self.job.id, f"tags skipped: {', '.join(tag_result.skipped_fields)}")
            for warning in tag_result.warnings:
                self.log_message.emit(self.job.id, warning)
            self.job_done.emit(self.job.id, str(final_path))
        except Exception as exc:
            self.job_failed.emit(self.job.id, str(exc))

    def _new_downloader(self, output_dir: Path) -> YTDLPDownloader:
        return self._downloader_factory(
            DownloadConfig(
                output_dir=output_dir,
                cookie_browser=self.cookie_browser,
                ffmpeg_location=self.ffmpeg_location,
            ),
            self._on_progress,
        )

    def _new_resolver(self) -> MetadataResolver:
        if self._resolver_factory:
            return self._resolver_factory()
        return MetadataResolver(cover_art_provider_factory=self._cover_art_provider_factory)

    def _resolve_metadata(self, downloader: YTDLPDownloader) -> tuple[TrackMetadata, ReviewState, list[MetadataCandidate], SourcePlatform]:
        self.progress_changed.emit(self.job.id, 0.0, DownloadStatus.METADATA.value)
        info = downloader.fetch_info(self.job.url)
        resolution = self._new_resolver().resolve(
            url=self.job.url,
            info=info,
            ytmusic_auth_path=self.ytmusic_auth_path,
            log=lambda message: self.log_message.emit(self.job.id, message),
        )
        self.log_message.emit(
            self.job.id,
            f"source: {resolution.platform.display_name}; {trust_policy_for(resolution.platform).note}",
        )
        if resolution.candidates:
            best = resolution.candidates[0]
            matched = ", ".join(best.matched_fields) or "no matched fields"
            self.log_message.emit(self.job.id, f"best metadata candidate: {best.provider} {best.score:.2f} ({matched})")
        self.log_message.emit(self.job.id, f"selected metadata: {resolution.metadata.artist} - {resolution.metadata.title}")
        if resolution.metadata.cover_url:
            cover_source = resolution.metadata.cover_source or _cover_source_from_url(resolution.metadata.cover_url)
            self.log_message.emit(self.job.id, f"cover source: {cover_source}")
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
            self.log_message.emit(self.job.id, f"audio recognition skipped: {reason}")
            return metadata, state, candidates, None

        if state == ReviewState.AUTO_APPROVED:
            self.log_message.emit(self.job.id, "verifying auto-approved metadata with AcoustID")
        else:
            self.log_message.emit(self.job.id, "metadata low confidence; downloading temporary audio for AcoustID lookup")
        result = self._new_downloader(_temp_output_dir(self.job)).download_audio(self.job.url)
        self.job.downloaded_path = result.path

        try:
            fingerprint_candidates = self._acoustid_provider_factory(self.acoustid_config).lookup(result.path)
        except FingerprintUnavailable as exc:
            self.log_message.emit(self.job.id, f"audio recognition skipped: {exc}")
            return metadata, state, candidates, result.path
        except FingerprintError as exc:
            self.log_message.emit(self.job.id, f"audio recognition failed: {exc}")
            return metadata, state, candidates, result.path

        if not fingerprint_candidates:
            self.log_message.emit(self.job.id, "audio recognition found no AcoustID match")
            return metadata, state, candidates, result.path

        merged_metadata, merged_state, merged_candidates = _merge_audio_recognition_candidates(
            metadata=metadata,
            state=state,
            candidates=candidates,
            fingerprint_candidates=fingerprint_candidates,
        )
        merged_metadata = self._new_resolver().enrich_cover_art(
            merged_metadata,
            platform=platform,
            fallback_cover_url=metadata.cover_url,
            log=lambda message: self.log_message.emit(self.job.id, message),
        )
        best = fingerprint_candidates[0]
        self.log_message.emit(self.job.id, f"AcoustID best match: {best.metadata.artist} - {best.metadata.title} ({best.score:.2f})")
        return merged_metadata, merged_state, merged_candidates, result.path

    def _on_progress(self, progress: DownloadProgress) -> None:
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


class UrlInput(QPlainTextEdit):
    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        self.setPlainText(value)


class MainWindow(QMainWindow):
    COLUMNS = ("Status", "Progress", "Source", "URL", "Title", "Artist", "Output")
    REVIEW_QUEUE_COLUMNS = ("Title", "Artist", "Confidence", "URL")
    CANDIDATE_COLUMNS = ("Provider", "Score", "Confidence", "Matched", "Title", "Artist", "Album", "Date", "ISRC", "Cover")

    def __init__(self, *, settings: QSettings | None = None) -> None:
        super().__init__()
        self.setWindowTitle("YT-DJ")
        self.resize(1120, 720)
        self._settings = settings or QSettings("YT-DJ", "YT-DJ")
        self.jobs: dict[str, DownloadJob] = {}
        self.row_job_ids: list[str] = []
        self.worker: JobWorker | None = None
        self.worker_mode = ""
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
        self.retry_failed_button: QPushButton | None = None
        self.approve_button: QPushButton | None = None
        self.reopen_review_button: QPushButton | None = None
        self.review_scroll_area: QScrollArea | None = None
        self.review_splitter: QSplitter | None = None
        self._loading_review = False
        self._loading_review_queue = False
        self._cover_preview_workers: list[CoverPreviewWorker] = []

        self.url_input = UrlInput()
        self.url_input.setPlaceholderText("Paste one or more YouTube / YouTube Music / SoundCloud URLs")
        self.url_input.setFixedHeight(76)
        self.output_dir_input = QLineEdit(str(Path.cwd() / "downloads"))
        self.cookie_combo = QComboBox()
        self.cookie_combo.addItem("No browser cookies", None)
        self.cookie_combo.addItem("Chrome", CookieBrowser.CHROME)
        self.cookie_combo.addItem("Edge", CookieBrowser.EDGE)
        self.cookie_combo.addItem("Firefox", CookieBrowser.FIREFOX)
        self.auth_path_input = QLineEdit()
        self.ffmpeg_path_input = QLineEdit()
        self.audio_recognition_checkbox = QCheckBox("Use AcoustID when metadata confidence is low")
        self.audio_recognition_checkbox.setChecked(True)
        self.verify_auto_approved_checkbox = QCheckBox("Verify YouTube auto-approved metadata with AcoustID")
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
        self.candidate_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.candidate_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.candidate_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.candidate_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.candidate_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.candidate_table.setMinimumHeight(88)
        self.candidate_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.candidate_table.itemSelectionChanged.connect(self._apply_selected_candidate)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)

        self.queue_status_label = QLabel("Add URLs, then process the queue.")
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
            "label": QLineEdit(),
            "isrc": QLineEdit(),
            "cover_url": QLineEdit(),
        }
        self.review_state_label = QLabel("No track selected")
        self.review_hint_label = QLabel("Tracks that need metadata review will appear here.")
        self.review_hint_label.setWordWrap(True)
        self.candidate_label = QLabel("")
        self.confidence_detail_label = QLabel("")
        self.confidence_detail_label.setWordWrap(True)
        self.cover_source_label = QLabel("Cover source: none")
        self.cover_preview_label = QLabel("No cover")
        self.cover_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_preview_label.setFixedSize(180, 180)
        self.cover_preview_label.setStyleSheet("border: 1px solid #b8b8b8;")
        self.review_fields["cover_url"].editingFinished.connect(self._cover_url_edited)

        self._load_settings()
        self._build_ui()

    def _build_ui(self) -> None:
        toolbar = QToolBar("Actions")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        add_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "Add URL", self)
        add_action.triggered.connect(self._add_url)
        self.start_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "Analyze Metadata", self)
        self.start_action.triggered.connect(self._analyze_next)
        self.download_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), "Download Approved", self)
        self.download_action.triggered.connect(self._download_next_approved)
        self.retry_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "Retry Failed", self)
        self.retry_action.triggered.connect(self._retry_failed)
        remove_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon), "Remove", self)
        remove_action.triggered.connect(self._remove_selected)
        toolbar.addAction(add_action)
        toolbar.addAction(self.start_action)
        toolbar.addAction(self.download_action)
        toolbar.addAction(self.retry_action)
        toolbar.addAction(remove_action)

        self.tabs = QTabWidget()
        self.queue_tab_index = self.tabs.addTab(self._queue_tab(), "Queue")
        self.review_tab_index = self.tabs.addTab(self._review_tab(), "Review")
        self.tabs.addTab(self._settings_tab(), "Settings")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)
        self._refresh_actions()

    def _queue_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        url_row = QGridLayout()
        url_row.addWidget(QLabel("URLs"), 0, 0)
        url_row.addWidget(self.url_input, 0, 1)
        self.add_url_button = QPushButton("Add")
        self.add_url_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        self.add_url_button.clicked.connect(self._add_url)
        url_row.addWidget(self.add_url_button, 0, 2)
        url_row.setColumnStretch(1, 1)
        layout.addLayout(url_row)

        action_row = QGridLayout()
        action_row.setColumnStretch(0, 1)
        self.start_queue_button = QPushButton("Analyze Metadata")
        self.start_queue_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_queue_button.clicked.connect(self._analyze_next)
        action_row.addWidget(self.start_queue_button, 0, 1)
        self.download_approved_button = QPushButton("Download Approved")
        self.download_approved_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.download_approved_button.clicked.connect(self._download_next_approved)
        action_row.addWidget(self.download_approved_button, 0, 2)
        self.review_selected_button = QPushButton("Move Selected to Review")
        self.review_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.review_selected_button.clicked.connect(self._move_selected_to_review_queue)
        action_row.addWidget(self.review_selected_button, 1, 1)
        self.retry_failed_button = QPushButton("Retry Failed")
        self.retry_failed_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.retry_failed_button.clicked.connect(self._retry_failed)
        action_row.addWidget(self.retry_failed_button, 1, 2)
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

        review_queue_group = QGroupBox("Review Queue")
        review_queue_group.setMinimumHeight(96)
        review_queue_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        review_queue_layout = QVBoxLayout(review_queue_group)
        review_queue_layout.addWidget(self.review_queue_table)

        provider_group = QGroupBox("Metadata Providers")
        provider_group.setMinimumHeight(112)
        provider_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        provider_layout = QVBoxLayout(provider_group)
        provider_layout.addWidget(self.review_state_label)
        provider_layout.addWidget(self.review_hint_label)
        provider_layout.addWidget(self.candidate_label)
        provider_layout.addWidget(self.confidence_detail_label)
        provider_layout.addWidget(self.candidate_table)

        tag_editor_group = QGroupBox("Tag Editor")
        tag_editor_group.setMinimumHeight(140)
        tag_editor_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tag_editor_layout = QVBoxLayout(tag_editor_group)

        form = QFormLayout()
        labels = {
            "title": "Title",
            "artist": "Artist",
            "album": "Album",
            "album_artist": "Album Artist",
            "genre": "Genre",
            "release_date": "Date",
            "label": "Label",
            "isrc": "ISRC",
            "cover_url": "Cover URL",
        }
        for key, label in labels.items():
            form.addRow(label, self.review_fields[key])
        tag_editor_layout.addLayout(form)

        cover_row = QWidget()
        cover_layout = QGridLayout(cover_row)
        cover_layout.setContentsMargins(0, 0, 0, 0)
        cover_layout.addWidget(self.cover_preview_label, 0, 0, 2, 1)
        cover_layout.addWidget(self.cover_source_label, 0, 1)
        cover_layout.setColumnStretch(1, 1)
        tag_editor_layout.addWidget(cover_row)

        review_action_row = QHBoxLayout()
        review_action_row.addStretch(1)
        self.reopen_review_button = QPushButton("Move to Review Queue")
        self.reopen_review_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.reopen_review_button.clicked.connect(self._move_active_to_review_queue)
        review_action_row.addWidget(self.reopen_review_button)
        self.approve_button = QPushButton("Approve Metadata")
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
        paths_group = QGroupBox("Paths and authentication")
        form = QFormLayout(paths_group)

        output_row = QWidget()
        output_layout = QGridLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_dir_input, 0, 0)
        output_button = QPushButton("Browse")
        output_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        output_button.clicked.connect(self._browse_output_dir)
        output_layout.addWidget(output_button, 0, 1)
        form.addRow("Output folder", output_row)

        form.addRow("Browser cookies", self.cookie_combo)
        form.addRow("YTMusic auth JSON", self._path_row(self.auth_path_input, self._browse_auth_file))
        form.addRow("ffmpeg path", self._path_row(self.ffmpeg_path_input, self._browse_ffmpeg))

        recognition_group = QGroupBox("Audio recognition")
        recognition_form = QFormLayout(recognition_group)
        recognition_form.addRow(self.audio_recognition_checkbox)
        recognition_form.addRow(self.verify_auto_approved_checkbox)
        recognition_form.addRow("AcoustID client key", self.acoustid_key_input)
        recognition_form.addRow("fpcalc path", self._path_row(self.fpcalc_path_input, self._browse_fpcalc))
        diagnostics_button = QPushButton("Copy Diagnostics")
        diagnostics_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        diagnostics_button.clicked.connect(self._copy_diagnostics)

        layout.addWidget(paths_group)
        layout.addWidget(recognition_group)
        layout.addWidget(diagnostics_button)
        layout.addStretch()
        return root

    def _path_row(self, line_edit: QLineEdit, callback: Any) -> QWidget:
        row = QWidget()
        layout = QGridLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit, 0, 0)
        button = QPushButton("Browse")
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
        acoustid = "configured" if self.acoustid_key_input.text().strip() else "missing"
        cookies = _cookie_browser_value(self.cookie_combo.currentData()) or "none"
        ytmusic_auth = "configured" if self.auth_path_input.text().strip() else "missing"
        return (
            f"Settings: ffmpeg {ffmpeg.source if ffmpeg.available else 'missing'}; "
            f"fpcalc {fpcalc.source if fpcalc.available else 'missing'}; "
            f"AcoustID {acoustid}; browser cookies {cookies}; YTMusic auth {ytmusic_auth}."
        )

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
            self._append_log(job.id, "queued")
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
            self._append_log(job.id, "queued for retry")
            retried += 1
        self._refresh_actions()
        if retried:
            self._analyze_next()

    def _run_worker(
        self,
        job: DownloadJob,
        approved_metadata: TrackMetadata | None = None,
        *,
        analyze_only: bool = False,
    ) -> None:
        self.save_settings()
        job.status = DownloadStatus.DOWNLOADING if approved_metadata else DownloadStatus.METADATA
        self._update_row(job)
        self.worker_mode = "analysis" if analyze_only else "download"
        self.worker = JobWorker(
            job,
            cookie_browser=self.cookie_combo.currentData(),
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
            self._append_log(job_id, "metadata auto-approved; ready to download")
        else:
            self._append_log(job_id, f"metadata requires review: {review_state.value}")
        self._refresh_actions()

    def _on_job_done(self, job_id: str, final_path: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.DONE
        job.progress = 100.0
        job.final_path = Path(final_path)
        self._update_row(job)
        self._append_log(job_id, f"done: {final_path}")
        self._refresh_actions()

    def _on_job_failed(self, job_id: str, error: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.FAILED
        job.error = error
        self._update_row(job)
        self._append_log(job_id, f"failed: {error}")
        self._refresh_actions()

    def _worker_finished(self) -> None:
        mode = self.worker_mode
        self.worker = None
        self.worker_mode = ""
        self._refresh_actions()
        if mode == "analysis":
            self._analyze_next()
        elif mode == "download":
            self._download_next_approved()

    def _approve_selected(self) -> None:
        job = self._active_review_job()
        if not job:
            self.log.appendPlainText("[review] approve skipped: no track loaded for review")
            QMessageBox.warning(self, "No track loaded", "Load a track in the Review tab before approving.")
            return
        metadata = self._metadata_from_review_fields(job.selected_metadata)
        job.selected_metadata = metadata
        job.status = DownloadStatus.APPROVED
        self._update_row(job)
        self._append_log(job.id, "metadata approved; ready to download")
        self._load_next_review_or_current(job)
        self._refresh_actions()

    def _move_selected_to_review_queue(self) -> None:
        job = self._selected_job()
        if not job:
            QMessageBox.warning(self, "No track selected", "Select an approved track in the Queue tab first.")
            return
        self._move_job_to_review_queue(job)

    def _move_active_to_review_queue(self) -> None:
        job = self._active_review_job()
        if not job:
            QMessageBox.warning(self, "No track loaded", "Load an approved track before moving it to review.")
            return
        self._move_job_to_review_queue(job)

    def _move_job_to_review_queue(self, job: DownloadJob) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Cannot move", "Wait for the current job to finish before changing review state.")
            return
        if job.status != DownloadStatus.APPROVED:
            QMessageBox.warning(self, "Not approved", "Only approved tracks can be moved back to the review queue.")
            return
        job.status = DownloadStatus.REVIEW_REQUIRED
        self._update_row(job)
        self._append_log(job.id, "approved metadata moved back to review queue")
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
            QMessageBox.warning(self, "Cannot remove", "Running jobs cannot be removed.")
            return
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
        self.review_state_label.setText(f"{job.status.value}: {platform.display_name}: {job.url}")
        if job.status == DownloadStatus.REVIEW_REQUIRED:
            self.review_hint_label.setText("Edit tags if needed. Approval marks this track ready for Download Approved.")
        elif job.status == DownloadStatus.APPROVED:
            self.review_hint_label.setText("Approved. Use Download Approved, or move it back to the review queue for more edits.")
        elif job.status == DownloadStatus.DONE:
            self.review_hint_label.setText("This track is already downloaded and tagged.")
        elif job.status == DownloadStatus.FAILED:
            self.review_hint_label.setText(job.error or "This track failed. Edit tags and retry if needed.")
        else:
            self.review_hint_label.setText("Metadata preview for the selected queue item.")
        if job.candidates:
            best = job.candidates[0]
            self._set_candidate_summary(best, reference=metadata)
        else:
            self.candidate_label.setText("No external candidate")
            self.confidence_detail_label.setText("No metadata provider candidate is available; edit the fields manually before approval.")
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
        self.review_state_label.setText("No track selected")
        self.review_hint_label.setText("Tracks that need metadata review will appear here.")
        self.candidate_label.setText("")
        self.confidence_detail_label.setText("")
        self.candidate_table.setRowCount(0)
        self._set_review_fields(TrackMetadata())
        self.cover_preview_label.setPixmap(QPixmap())
        self.cover_preview_label.setText("No cover")
        self.cover_source_label.setText("Cover source: none")

    def _set_candidate_summary(self, candidate: MetadataCandidate, *, reference: TrackMetadata | None = None) -> None:
        trust_note = ""
        if candidate.provider == "soundcloud" and candidate.raw.get("trusted_native"):
            trust_note = " - SoundCloud metadata trusted"
        matched = ", ".join(candidate.matched_fields) or "no matched fields"
        bucket = _confidence_bucket(candidate)
        self.candidate_label.setText(f"Best candidate: {candidate.provider} {candidate.score:.2f} - {bucket} ({matched}){trust_note}")
        self.confidence_detail_label.setText(_confidence_explanation(candidate, reference=reference))

    def _populate_candidate_table(self, job: DownloadJob) -> None:
        self.candidate_table.setRowCount(len(job.candidates))
        for row, candidate in enumerate(job.candidates):
            values = (
                candidate.provider,
                f"{candidate.score:.3f}",
                _confidence_bucket(candidate),
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

    def _apply_selected_candidate(self) -> None:
        if self._loading_review:
            return
        job = self._active_review_job()
        row = self.candidate_table.currentRow()
        if not job or row < 0:
            return
        item = self.candidate_table.item(row, 0)
        if not item:
            return
        candidate_index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(candidate_index, int) or candidate_index >= len(job.candidates):
            return
        candidate = job.candidates[candidate_index]
        metadata = candidate.metadata.with_defaults_from(job.selected_metadata).normalized()
        job.selected_metadata = metadata
        self._set_candidate_summary(candidate, reference=metadata)
        self._set_review_fields(metadata)
        self._update_row(job)
        self._refresh_cover_preview(job, metadata)

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
        self.cover_source_label.setText(f"Cover source: {source or 'none'}")
        if not cover_url:
            self.cover_preview_label.setPixmap(QPixmap())
            self.cover_preview_label.setText("No cover")
            return

        self.cover_preview_label.setPixmap(QPixmap())
        self.cover_preview_label.setText("Loading cover...")
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
            self.cover_preview_label.setText("Cover unavailable")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.cover_preview_label.setPixmap(QPixmap())
            self.cover_preview_label.setText("Cover unavailable")
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
        return TrackMetadata(
            title=self.review_fields["title"].text().strip(),
            artist=self.review_fields["artist"].text().strip(),
            album=self.review_fields["album"].text().strip(),
            album_artist=self.review_fields["album_artist"].text().strip(),
            genre=self.review_fields["genre"].text().strip(),
            release_date=self.review_fields["release_date"].text().strip(),
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
            job.status.value,
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
        if self.retry_failed_button:
            self.retry_failed_button.setEnabled(can_retry)
        if self.approve_button:
            self.approve_button.setEnabled(can_approve)
        if self.reopen_review_button:
            self.reopen_review_button.setEnabled(can_move_active_to_review)
        if self.tabs:
            self.tabs.setTabText(self.review_tab_index, f"Review ({review_count})" if review_count else "Review")

        if running and review_count:
            text = f"Processing continues. {review_count} track(s) need metadata review."
        elif running:
            text = "Processing the current track."
        elif approved_count and pending_count:
            text = f"{approved_count} approved track(s) ready to download; {pending_count} track(s) still need analysis."
        elif approved_count:
            text = f"{approved_count} approved track(s) ready to download."
        elif review_count and pending_count:
            text = f"{review_count} track(s) need review; {pending_count} still ready to process."
        elif review_count:
            text = f"{review_count} track(s) need metadata review."
        elif pending_count:
            text = f"{pending_count} track(s) ready. Analyze metadata first."
        elif failed_count:
            text = f"{failed_count} failed track(s). Retry Failed will re-analyze them."
        elif self.jobs:
            text = "No pending tracks."
        else:
            text = "Add URLs, then process the queue."
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
                _confidence_bucket(best) if best else "manual",
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
        folder = QFileDialog.getExistingDirectory(self, "Output folder", self.output_dir_input.text())
        if folder:
            self.output_dir_input.setText(folder)

    def _browse_auth_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "YTMusic auth JSON", "", "JSON files (*.json);;All files (*)")
        if path:
            self.auth_path_input.setText(path)

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "ffmpeg executable", "", "Executables (*.exe);;All files (*)")
        if path:
            self.ffmpeg_path_input.setText(path)

    def _browse_fpcalc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "fpcalc executable", "", "Executables (*.exe);;All files (*)")
        if path:
            self.fpcalc_path_input.setText(path)

    def _copy_diagnostics(self) -> None:
        QApplication.clipboard().setText(format_diagnostics())
        self._append_log("system", "diagnostics copied to clipboard")


def run_app() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


def _optional_path(value: str) -> Path | None:
    stripped = value.strip()
    return Path(stripped) if stripped else None


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
        return "metadata already auto-approved"
    if platform not in {SourcePlatform.YOUTUBE, SourcePlatform.YOUTUBE_MUSIC}:
        if platform == SourcePlatform.SOUNDCLOUD:
            return "SoundCloud native metadata is trusted"
        return "source is not eligible"
    if not enabled:
        return "disabled"
    if not config.client_key.strip():
        return "AcoustID client key is not configured"
    if not _has_fpcalc(config):
        return "fpcalc executable was not found"
    return ""


def _has_fpcalc(config: AcoustIDConfig) -> bool:
    return find_executable("fpcalc", explicit_path=config.fpcalc_path).available


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
    return job.output_dir / ".ytdj-temp" / job.id


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
        return "SoundCloud native"
    if "ytimg.com" in lowered or "youtube" in lowered:
        return "YouTube fallback"
    return "manual" if url else ""


def _confidence_bucket(candidate: MetadataCandidate | None) -> str:
    if not candidate:
        return "manual"
    if candidate.score >= 0.85:
        return "auto"
    if candidate.score >= 0.65:
        return "review"
    return "manual"


def _confidence_explanation(candidate: MetadataCandidate, *, reference: TrackMetadata | None = None) -> str:
    threshold = "auto approve" if candidate.score >= 0.85 else "needs review" if candidate.score >= 0.65 else "manual entry"
    matched = ", ".join(candidate.matched_fields) or "none"
    parts = [f"{candidate.provider} score {candidate.score:.2f}: {threshold}. Matched fields: {matched}."]
    missing = [
        field
        for field in ("title", "artist", "album", "release_date", "isrc", "cover_url")
        if not getattr(candidate.metadata, field)
    ]
    if missing:
        parts.append(f"Missing candidate fields: {', '.join(missing)}.")
    if reference:
        conflicts = _metadata_conflict_fields(reference, candidate.metadata)
        if conflicts:
            parts.append(f"Conflicts with current fields: {', '.join(conflicts)}.")
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
