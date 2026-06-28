"""PySide6 desktop interface."""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
    QSpinBox,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from cueforge.download import DownloadCanceled, DownloadConfig, DownloadProgress, PlaylistExpansionResult, YTDLPDownloader
from cueforge.errors import action_hint, user_facing_error
from cueforge.gui.scheduler import JobScheduler
from cueforge.metadata import (
    AcoustIDConfig,
    AcoustIDProvider,
    CoverArtProvider,
    MetadataResolver,
    default_ytmusic_oauth_account_path,
    default_ytmusic_oauth_token_path,
    fetch_ytmusic_oauth_account,
    find_ytmusic_oauth_client_file,
    google_oauth_account_label,
    load_ytmusic_oauth_client,
    read_ytmusic_oauth_account,
    refresh_ytmusic_oauth_token_if_needed,
    run_ytmusic_oauth_desktop_flow,
    write_ytmusic_oauth_account,
    write_ytmusic_oauth_token,
)
from cueforge.metadata.fingerprint import FingerprintError, FingerprintUnavailable
from cueforge.metadata.matching import text_similarity
from cueforge.metadata.normalize import merge_metadata
from cueforge.models import ErrorCategory, DownloadJob, DownloadStatus, JobEvent, MetadataCandidate, ReviewState, SchedulerLimits, TagWriteResult, TrackMetadata
from cueforge.paths import default_output_dir, legacy_cwd_output_dir
from cueforge.runtime import app_root, find_executable, format_diagnostics
from cueforge.sources import SourcePlatform, detect_source_platform
from cueforge.store import JobStore
from cueforge.tags import MAX_COVER_BYTES, RekordboxTagWriter, safe_track_filename

DownloaderFactory = Callable[[DownloadConfig, Any], YTDLPDownloader]
PlaylistExpander = Callable[[str], PlaylistExpansionResult]
ResolverFactory = Callable[[], MetadataResolver]
AcoustIDProviderFactory = Callable[[AcoustIDConfig], Any]
CoverArtProviderFactory = Callable[[], Any]
TagWriterFactory = Callable[[], Any]
YOUTUBE_DATA_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
YOUTUBE_YTDLP_REQUEST_INTERVAL_SECONDS = 1.5
_ACTIVE_STATUSES = {DownloadStatus.DOWNLOADING, DownloadStatus.METADATA, DownloadStatus.TAGGING}
_TERMINAL_STATUSES = {DownloadStatus.DONE, DownloadStatus.FAILED, DownloadStatus.CANCELED}
_ANALYZABLE_STATUSES = {
    DownloadStatus.PENDING,
    DownloadStatus.REVIEW_REQUIRED,
    DownloadStatus.APPROVED,
    DownloadStatus.FAILED,
    DownloadStatus.CANCELED,
}
_NON_RETRYABLE_ERROR_CATEGORIES = {ErrorCategory.VIDEO_UNAVAILABLE.value}
_PIPELINE_STATUSES = (
    DownloadStatus.PENDING,
    DownloadStatus.METADATA,
    DownloadStatus.REVIEW_REQUIRED,
    DownloadStatus.APPROVED,
    DownloadStatus.DOWNLOADING,
    DownloadStatus.DONE,
    DownloadStatus.FAILED,
)


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
        cookie_file: Path | None = None,
        ytmusic_auth_path: Path | None = None,
        ytmusic_oauth_client_file: Path | None = None,
        ytmusic_oauth_token_file: Path | None = None,
        ffmpeg_location: Path | None = None,
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
        tag_semaphore: threading.Semaphore | None = None,
    ) -> None:
        super().__init__()
        self.job = job
        self.cookie_file = cookie_file
        self.ytmusic_auth_path = ytmusic_auth_path
        self.ytmusic_oauth_client_file = ytmusic_oauth_client_file
        self.ytmusic_oauth_token_file = ytmusic_oauth_token_file
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
        self._tag_semaphore = tag_semaphore
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
            self._acquire_tag_slot()
            try:
                tag_result: TagWriteResult = self._tag_writer_factory().write(downloaded_path, metadata)
            finally:
                self._release_tag_slot()
            self._check_canceled()
            final_path = _move_to_final(downloaded_path, self.job.output_dir, metadata)
            self.job.downloaded_path = None
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
                cookie_file=self.cookie_file,
                ffmpeg_location=self.ffmpeg_location,
                youtube_request_interval_seconds=YOUTUBE_YTDLP_REQUEST_INTERVAL_SECONDS,
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
            ytmusic_cookie_file=self.cookie_file,
            ytmusic_oauth_client_file=self.ytmusic_oauth_client_file,
            ytmusic_oauth_token_file=self.ytmusic_oauth_token_file,
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

    def _acquire_tag_slot(self) -> None:
        if not self._tag_semaphore:
            return
        while not self._tag_semaphore.acquire(timeout=0.2):
            self._check_canceled()

    def _release_tag_slot(self) -> None:
        if self._tag_semaphore:
            self._tag_semaphore.release()


class PlaylistExpansionWorker(QThread):
    playlist_ready = Signal(str, object)
    playlist_failed = Signal(str, str)
    log_message = Signal(str, str)

    def __init__(
        self,
        job: DownloadJob,
        expand_playlist: Callable[[str, Path, Callable[[str, str], None]], PlaylistExpansionResult],
    ) -> None:
        super().__init__()
        self.job = job
        self._expand_playlist = expand_playlist

    def run(self) -> None:
        try:
            result = self._expand_playlist(self.job.url, self.job.output_dir, self.log_message.emit)
        except Exception as exc:
            self.playlist_failed.emit(self.job.id, str(exc))
            return
        self.playlist_ready.emit(self.job.id, result)

    def cancel(self) -> None:
        self.requestInterruption()


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
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > MAX_COVER_BYTES:
                        self.cover_loaded.emit(self.job_id, self.url, b"", "cover image is too large")
                        return
                except ValueError:
                    pass
            if len(response.content) > MAX_COVER_BYTES:
                self.cover_loaded.emit(self.job_id, self.url, b"", "cover image is too large")
                return
            self.cover_loaded.emit(self.job_id, self.url, response.content, "")
        except Exception as exc:
            self.cover_loaded.emit(self.job_id, self.url, b"", str(exc))


class GoogleOAuthWorker(QThread):
    connected = Signal(str)
    failed = Signal(str)
    log_message = Signal(str)

    def __init__(self, client_file: Path, token_file: Path, account_file: Path) -> None:
        super().__init__()
        self.client_file = client_file
        self.token_file = token_file
        self.account_file = account_file

    def run(self) -> None:
        try:
            client = load_ytmusic_oauth_client(self.client_file)
            self.log_message.emit("Google OAuth 연결 시작: 브라우저에서 계정 승인을 완료하세요.")
            token = run_ytmusic_oauth_desktop_flow(client)
            write_ytmusic_oauth_token(token, self.token_file)
            account_label = ""
            if self.account_file.exists():
                try:
                    self.account_file.unlink()
                except OSError:
                    pass
            try:
                account = fetch_ytmusic_oauth_account(token)
                write_ytmusic_oauth_account(account, self.account_file)
                account_label = google_oauth_account_label(account)
                if account_label:
                    self.log_message.emit(f"Google OAuth 계정 확인됨: {account_label}")
            except Exception as exc:
                self.log_message.emit(f"Google OAuth 계정 정보 확인 실패: {exc}")
            self.connected.emit(account_label)
        except Exception as exc:
            self.failed.emit(str(exc))


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
        skip_button.clicked.connect(self.reject)
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
    CANDIDATE_COLUMNS = ("제공자", "점수", "신뢰도", "배지", "일치 항목", "제목", "아티스트", "앨범", "날짜", "ISRC", "커버")
    CANDIDATE_PREVIEW_COLUMNS = ("필드", "현재 값", "후보 적용 값")

    def __init__(
        self,
        *,
        settings: QSettings | None = None,
        job_store: JobStore | None = None,
        playlist_expander: PlaylistExpander | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("CueForge")
        self.resize(1120, 720)
        self._settings = settings or QSettings("CueForge", "CueForge")
        self.job_store = job_store or JobStore(_job_store_path_for_settings(self._settings))
        self.jobs: dict[str, DownloadJob] = {}
        self.row_job_ids: list[str] = []
        self.worker: QThread | None = None
        self.google_oauth_worker: GoogleOAuthWorker | None = None
        self.scheduler: JobScheduler | None = None
        self.worker_mode = ""
        self._playlist_expanded_job_ids: list[str] = []
        self.cancel_requested = False
        self.active_review_job_id: str | None = None
        self.tabs: QTabWidget | None = None
        self.queue_tab_index = 0
        self.review_tab_index = 1
        self.pipeline_tab_index = 0
        self.history_tab_index = 0
        self.settings_tab_index = 0
        self._last_tab_index = 0
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
        self.remove_done_button: QPushButton | None = None
        self.remove_selected_button: QPushButton | None = None
        self.cancel_current_button: QPushButton | None = None
        self.approve_button: QPushButton | None = None
        self.reopen_review_button: QPushButton | None = None
        self.remove_review_button: QPushButton | None = None
        self.open_onboarding_button: QPushButton | None = None
        self.google_oauth_connect_button: QPushButton | None = None
        self.google_oauth_disconnect_button: QPushButton | None = None
        self.onboarding_dialog: OnboardingDialog | None = None
        self.apply_candidate_button: QPushButton | None = None
        self.pending_candidate_index: int | None = None
        self.review_scroll_area: QScrollArea | None = None
        self.review_splitter: QSplitter | None = None
        self._loading_review = False
        self._loading_review_queue = False
        self._cover_preview_workers: list[CoverPreviewWorker] = []
        self._loading_pipeline = False
        self._loading_history = False
        self._dependency_status_cache = ""
        self._dependency_status_cache_key: tuple[Any, ...] | None = None
        self._playlist_expander = playlist_expander

        self.url_input = UrlInput()
        self.url_input.setPlaceholderText("YouTube / YouTube Music / SoundCloud URL을 하나 이상 붙여넣으세요")
        self.url_input.setFixedHeight(76)
        self.output_dir_input = QLineEdit(str(default_output_dir()))
        self.cookie_file_input = QLineEdit()
        self.auth_path_input = QLineEdit()
        self.ffmpeg_path_input = QLineEdit()
        self.google_oauth_status_label = QLabel("")
        self.google_oauth_status_label.setWordWrap(True)
        self.audio_recognition_checkbox = QCheckBox("메타데이터 신뢰도가 낮으면 AcoustID 사용")
        self.audio_recognition_checkbox.setChecked(True)
        self.verify_auto_approved_checkbox = QCheckBox("YouTube 자동 승인 메타데이터를 AcoustID로 검증")
        self.acoustid_key_input = QLineEdit()
        self.acoustid_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.fpcalc_path_input = QLineEdit()
        self.metadata_parallel_spin = QSpinBox()
        self.metadata_parallel_spin.setRange(1, 8)
        self.metadata_parallel_spin.setValue(3)
        self.download_parallel_spin = QSpinBox()
        self.download_parallel_spin.setRange(1, 6)
        self.download_parallel_spin.setValue(2)
        self.tagging_parallel_spin = QSpinBox()
        self.tagging_parallel_spin.setRange(1, 3)
        self.tagging_parallel_spin.setValue(1)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
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
        self.candidate_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.candidate_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
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
        self.pipeline_tables: dict[DownloadStatus, QTableWidget] = {}
        self.pipeline_detail = QPlainTextEdit()
        self.pipeline_detail.setReadOnly(True)
        self.pipeline_start_button: QPushButton | None = None
        self.pipeline_download_button: QPushButton | None = None
        self.pipeline_retry_button: QPushButton | None = None
        self.history_table = QTableWidget(0, 5)
        self.clear_history_button: QPushButton | None = None

        self._load_settings()
        self._build_ui()
        self._connect_settings_status_updates()
        self._refresh_google_oauth_status()
        self.scheduler = JobScheduler(worker_factory=self._create_scheduled_worker, limits=self._scheduler_limits(), parent=self)
        self.scheduler.job_started.connect(self._on_scheduled_job_started)
        self.scheduler.idle.connect(self._on_scheduler_idle)
        self._load_jobs_from_store()
        if not _settings_bool(self._settings.value("onboarding/completed", False), default=False):
            self._open_onboarding()

    def _build_ui(self) -> None:
        toolbar = QToolBar("작업")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        add_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "URL 추가", self)
        add_action.triggered.connect(self._add_url)
        self.start_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "전체 처리 시작", self)
        self.start_action.triggered.connect(self._start_pipeline)
        self.download_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), "승인 항목 다운로드", self)
        self.download_action.triggered.connect(self._schedule_approved_downloads)
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
        self.queue_tab_index = self.tabs.addTab(self._queue_tab(), "작업")
        self.pipeline_tab_index = self.tabs.addTab(self._pipeline_tab(), "상태")
        self.review_tab_index = self.tabs.addTab(self._review_tab(), "검수")
        self.history_tab_index = self.tabs.addTab(self._history_tab(), "이력")
        self.settings_tab_index = self.tabs.addTab(self._settings_tab(), "설정")
        self._last_tab_index = self.tabs.currentIndex()
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

        primary_action_row = QHBoxLayout()
        self.start_queue_button = QPushButton("전체 처리 시작")
        self.start_queue_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_queue_button.clicked.connect(self._start_pipeline)
        primary_action_row.addWidget(self.start_queue_button)
        self.download_approved_button = QPushButton("승인 항목 다운로드")
        self.download_approved_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.download_approved_button.clicked.connect(self._schedule_approved_downloads)
        primary_action_row.addWidget(self.download_approved_button)
        self.retry_failed_button = QPushButton("실패 재시도")
        self.retry_failed_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.retry_failed_button.clicked.connect(self._retry_failed)
        primary_action_row.addWidget(self.retry_failed_button)
        self.remove_done_button = QPushButton("완료 항목 제거")
        self.remove_done_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.remove_done_button.clicked.connect(self._remove_done_jobs)
        primary_action_row.addWidget(self.remove_done_button)
        self.cancel_current_button = QPushButton("현재 작업 취소")
        self.cancel_current_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
        self.cancel_current_button.clicked.connect(self._cancel_current_job)
        primary_action_row.addWidget(self.cancel_current_button)
        primary_action_row.addStretch(1)
        layout.addLayout(primary_action_row)

        selection_group = QGroupBox("선택 항목")
        selection_layout = QHBoxLayout(selection_group)
        self.analyze_selected_button = QPushButton("처리")
        self.analyze_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.analyze_selected_button.clicked.connect(self._analyze_selected)
        selection_layout.addWidget(self.analyze_selected_button)
        self.download_selected_button = QPushButton("다운로드")
        self.download_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.download_selected_button.clicked.connect(self._download_selected_approved)
        selection_layout.addWidget(self.download_selected_button)
        self.review_selected_button = QPushButton("검수로 이동")
        self.review_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.review_selected_button.clicked.connect(self._move_selected_to_review_queue)
        selection_layout.addWidget(self.review_selected_button)
        self.retry_selected_button = QPushButton("재시도")
        self.retry_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.retry_selected_button.clicked.connect(self._retry_selected)
        selection_layout.addWidget(self.retry_selected_button)
        self.remove_selected_button = QPushButton("큐에서 삭제")
        self.remove_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.remove_selected_button.clicked.connect(self._remove_selected)
        selection_layout.addWidget(self.remove_selected_button)
        selection_layout.addStretch(1)
        layout.addWidget(selection_group)
        layout.addWidget(self.queue_status_label)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.log)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 140])
        layout.addWidget(splitter)
        return root

    def _pipeline_board_widget(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        board = QWidget()
        board_layout = QGridLayout(board)
        board_layout.setContentsMargins(0, 0, 0, 0)
        board_layout.setHorizontalSpacing(8)
        board_layout.setVerticalSpacing(8)
        for index, status in enumerate(_PIPELINE_STATUSES):
            group = QGroupBox(_pipeline_status_label(status))
            group_layout = QVBoxLayout(group)
            table = QTableWidget(0, 2)
            table.setHorizontalHeaderLabels(("트랙", "URL"))
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.itemSelectionChanged.connect(self._load_selected_pipeline_job)
            self.pipeline_tables[status] = table
            group_layout.addWidget(table)
            board_layout.addWidget(group, index // 4, index % 4)
        splitter.addWidget(board)

        detail_group = QGroupBox("선택 작업")
        detail_layout = QVBoxLayout(detail_group)
        self.pipeline_detail.setMinimumWidth(260)
        detail_layout.addWidget(self.pipeline_detail)
        splitter.addWidget(detail_group)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        return splitter

    def _pipeline_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        action_row = QHBoxLayout()
        self.pipeline_start_button = QPushButton("전체 처리 시작")
        self.pipeline_start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.pipeline_start_button.clicked.connect(self._start_pipeline)
        action_row.addWidget(self.pipeline_start_button)
        self.pipeline_download_button = QPushButton("승인 항목 다운로드")
        self.pipeline_download_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.pipeline_download_button.clicked.connect(self._schedule_approved_downloads)
        action_row.addWidget(self.pipeline_download_button)
        self.pipeline_retry_button = QPushButton("실패 재시도")
        self.pipeline_retry_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.pipeline_retry_button.clicked.connect(self._retry_failed)
        action_row.addWidget(self.pipeline_retry_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        layout.addWidget(self._pipeline_board_widget())
        return root

    def _history_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.clear_history_button = QPushButton("완료/실패 이력 삭제")
        self.clear_history_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.clear_history_button.clicked.connect(self._clear_history)
        action_row.addWidget(self.clear_history_button)
        layout.addLayout(action_row)
        self.history_table.setHorizontalHeaderLabels(("상태", "제목", "아티스트", "오류", "출력"))
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.itemSelectionChanged.connect(self._load_selected_history_job)
        layout.addWidget(self.history_table)
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
        self.remove_review_button = QPushButton("검수 항목 삭제")
        self.remove_review_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.remove_review_button.clicked.connect(self._remove_active_review_job)
        review_action_row.addWidget(self.remove_review_button)
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

        form.addRow("쿠키 파일", self._path_row(self.cookie_file_input, self._browse_cookie_file))
        form.addRow("YTMusic 인증 JSON (고급)", self._path_row(self.auth_path_input, self._browse_auth_file))
        form.addRow("ffmpeg 경로", self._path_row(self.ffmpeg_path_input, self._browse_ffmpeg))

        google_group = QGroupBox("Google 계정")
        google_layout = QVBoxLayout(google_group)
        google_layout.addWidget(self.google_oauth_status_label)
        google_action_row = QHBoxLayout()
        self.google_oauth_connect_button = QPushButton("Google 계정 연결")
        self.google_oauth_connect_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.google_oauth_connect_button.clicked.connect(self._connect_google_oauth)
        google_action_row.addWidget(self.google_oauth_connect_button)
        self.google_oauth_disconnect_button = QPushButton("연결 해제")
        self.google_oauth_disconnect_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
        self.google_oauth_disconnect_button.clicked.connect(self._disconnect_google_oauth)
        google_action_row.addWidget(self.google_oauth_disconnect_button)
        google_action_row.addStretch(1)
        google_layout.addLayout(google_action_row)

        recognition_group = QGroupBox("오디오 인식")
        recognition_form = QFormLayout(recognition_group)
        recognition_form.addRow(self.audio_recognition_checkbox)
        recognition_form.addRow(self.verify_auto_approved_checkbox)
        recognition_form.addRow("AcoustID 클라이언트 키", self.acoustid_key_input)
        recognition_form.addRow("fpcalc 경로", self._path_row(self.fpcalc_path_input, self._browse_fpcalc))

        scheduler_group = QGroupBox("병렬 처리")
        scheduler_form = QFormLayout(scheduler_group)
        scheduler_form.addRow("메타데이터 동시 작업", self.metadata_parallel_spin)
        scheduler_form.addRow("다운로드 동시 작업", self.download_parallel_spin)
        scheduler_form.addRow("태깅 동시 작업", self.tagging_parallel_spin)

        self.open_onboarding_button = QPushButton("초기 설정 다시 열기")
        self.open_onboarding_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.open_onboarding_button.clicked.connect(self._open_onboarding)
        diagnostics_button = QPushButton("진단 정보 복사")
        diagnostics_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        diagnostics_button.clicked.connect(self._copy_diagnostics)

        layout.addWidget(paths_group)
        layout.addWidget(google_group)
        layout.addWidget(recognition_group)
        layout.addWidget(scheduler_group)
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
        self.output_dir_input.setText(str(self._saved_output_dir_or_default()))
        self.auth_path_input.setText(str(self._settings.value("paths/ytmusic_auth", "")))
        self.cookie_file_input.setText(str(self._settings.value("auth/cookie_file", "")))
        self.ffmpeg_path_input.setText(str(self._settings.value("paths/ffmpeg", "")))
        self.fpcalc_path_input.setText(str(self._settings.value("paths/fpcalc", "")))
        self.acoustid_key_input.setText(str(self._settings.value("acoustid/client_key", "")))
        self.audio_recognition_checkbox.setChecked(_settings_bool(self._settings.value("acoustid/enabled", True), default=True))
        self.verify_auto_approved_checkbox.setChecked(
            _settings_bool(self._settings.value("acoustid/verify_auto_approved", False), default=False)
        )
        self.metadata_parallel_spin.setValue(_settings_int(self._settings.value("scheduler/metadata_parallel", 3), default=3))
        self.download_parallel_spin.setValue(_settings_int(self._settings.value("scheduler/download_parallel", 2), default=2))
        self.tagging_parallel_spin.setValue(_settings_int(self._settings.value("scheduler/tagging_parallel", 1), default=1))

    def _saved_output_dir_or_default(self) -> Path:
        fallback = default_output_dir()
        saved = str(self._settings.value("paths/output_dir", "") or "").strip()
        if not saved:
            return fallback
        saved_path = Path(saved)
        if _same_path(saved_path, legacy_cwd_output_dir()):
            self._settings.setValue("paths/output_dir", str(fallback))
            self._settings.sync()
            return fallback
        return saved_path

    def save_settings(self) -> None:
        self._settings.setValue("paths/output_dir", self.output_dir_input.text().strip())
        self._settings.setValue("paths/ytmusic_auth", self.auth_path_input.text().strip())
        self._settings.setValue("auth/cookie_file", self.cookie_file_input.text().strip())
        self._settings.setValue("paths/ffmpeg", self.ffmpeg_path_input.text().strip())
        self._settings.setValue("paths/fpcalc", self.fpcalc_path_input.text().strip())
        self._settings.setValue("acoustid/client_key", self.acoustid_key_input.text().strip())
        self._settings.setValue("acoustid/enabled", self.audio_recognition_checkbox.isChecked())
        self._settings.setValue("acoustid/verify_auto_approved", self.verify_auto_approved_checkbox.isChecked())
        self._settings.remove("auth/cookie_browser")
        self._settings.remove("auth/unlock_browser_cookie_database")
        self._settings.setValue("scheduler/metadata_parallel", self.metadata_parallel_spin.value())
        self._settings.setValue("scheduler/download_parallel", self.download_parallel_spin.value())
        self._settings.setValue("scheduler/tagging_parallel", self.tagging_parallel_spin.value())
        self._settings.sync()
        self._dependency_status_cache_key = None
        if self.scheduler:
            self.scheduler.set_limits(self._scheduler_limits())
        if hasattr(self, "dependency_status_label"):
            self.dependency_status_label.setText(self._settings_status_text())

    def _settings_status_text(self) -> str:
        cache_key = (
            self.ffmpeg_path_input.text().strip(),
            self.fpcalc_path_input.text().strip(),
            self.acoustid_key_input.text().strip(),
            self.cookie_file_input.text().strip(),
            str(self._ytmusic_oauth_client_file() or ""),
            str(self._ytmusic_oauth_token_file().exists()),
            self._google_oauth_account_label(),
            str(self.metadata_parallel_spin.value()),
            str(self.download_parallel_spin.value()),
            str(self.tagging_parallel_spin.value()),
        )
        if self._dependency_status_cache_key == cache_key and self._dependency_status_cache:
            return self._dependency_status_cache
        ffmpeg = find_executable("ffmpeg", explicit_path=_optional_path(self.ffmpeg_path_input.text()))
        fpcalc = find_executable("fpcalc", explicit_path=_optional_path(self.fpcalc_path_input.text()))
        acoustid = "설정됨" if self.acoustid_key_input.text().strip() else "미설정"
        cookie_file = "설정됨" if self.cookie_file_input.text().strip() else "미설정"
        google_oauth = (
            f"연결됨: {self._google_oauth_account_label()}"
            if self._ytmusic_oauth_connected() and self._google_oauth_account_label()
            else "연결됨"
            if self._ytmusic_oauth_connected()
            else "클라이언트 있음"
            if self._ytmusic_oauth_client_file()
            else "미설정"
        )
        ytmusic_auth = (
            "Google OAuth"
            if self._ytmusic_oauth_connected()
            else "수동 JSON"
            if self.auth_path_input.text().strip()
            else "쿠키 파일"
            if self.cookie_file_input.text().strip()
            else "미설정"
        )
        text = (
            f"설정: ffmpeg {ffmpeg.source if ffmpeg.available else '없음'}; "
            f"fpcalc {fpcalc.source if fpcalc.available else '없음'}; "
            f"AcoustID {acoustid}; 쿠키 파일 {cookie_file}; "
            f"Google OAuth {google_oauth}; YTMusic 인증 {ytmusic_auth}; "
            f"병렬 {self.metadata_parallel_spin.value()}/{self.download_parallel_spin.value()}/{self.tagging_parallel_spin.value()}."
        )
        self._dependency_status_cache_key = cache_key
        self._dependency_status_cache = text
        return text

    def _connect_settings_status_updates(self) -> None:
        for line_edit in (
            self.auth_path_input,
            self.cookie_file_input,
            self.ffmpeg_path_input,
            self.fpcalc_path_input,
            self.acoustid_key_input,
        ):
            line_edit.textChanged.connect(self._refresh_settings_status_label)
        self.metadata_parallel_spin.valueChanged.connect(self._refresh_settings_status_label)
        self.download_parallel_spin.valueChanged.connect(self._refresh_settings_status_label)
        self.tagging_parallel_spin.valueChanged.connect(self._refresh_settings_status_label)

    def _refresh_settings_status_label(self, *args: Any) -> None:
        self._dependency_status_cache_key = None
        if self.scheduler:
            self.scheduler.set_limits(self._scheduler_limits())
        if hasattr(self, "dependency_status_label"):
            self.dependency_status_label.setText(self._settings_status_text())
        self._refresh_google_oauth_status()

    def _refresh_google_oauth_status(self) -> None:
        client_file = self._ytmusic_oauth_client_file()
        token_file = self._ytmusic_oauth_token_file()
        is_connecting = bool(self.google_oauth_worker and self.google_oauth_worker.isRunning())
        if is_connecting:
            text = "Google OAuth 연결 중입니다. 브라우저에서 계정 승인을 완료하세요."
        elif not client_file:
            text = "배포 설정에 Google OAuth 클라이언트가 없습니다. config/google_oauth_client.json을 포함하면 계정 연결을 사용할 수 있습니다."
        elif token_file.exists():
            account_label = self._google_oauth_account_label()
            if account_label:
                text = f"Google 계정 연결됨: {account_label}. 비공개 YouTube Music 플레이리스트 조회에 OAuth를 우선 사용합니다."
            else:
                text = "Google 계정 연결됨. 계정 정보를 표시하려면 Google 계정을 다시 연결하세요."
        else:
            text = "Google OAuth 클라이언트 준비됨. 계정 연결 후 비공개 플레이리스트 조회에 OAuth를 사용합니다."
        self.google_oauth_status_label.setText(text)
        if self.google_oauth_connect_button:
            self.google_oauth_connect_button.setEnabled(client_file is not None and not is_connecting)
        if self.google_oauth_disconnect_button:
            self.google_oauth_disconnect_button.setEnabled(token_file.exists() and not is_connecting)

    def _ytmusic_oauth_client_file(self) -> Path | None:
        return find_ytmusic_oauth_client_file(app_root())

    def _ytmusic_oauth_token_file(self) -> Path:
        return default_ytmusic_oauth_token_path()

    def _ytmusic_oauth_account_file(self) -> Path:
        return default_ytmusic_oauth_account_path()

    def _google_oauth_account_label(self) -> str:
        return google_oauth_account_label(read_ytmusic_oauth_account(self._ytmusic_oauth_account_file()))

    def _ytmusic_oauth_connected(self) -> bool:
        return bool(self._ytmusic_oauth_client_file() and self._ytmusic_oauth_token_file().exists())

    def _connect_google_oauth(self) -> None:
        if self.google_oauth_worker and self.google_oauth_worker.isRunning():
            return
        client_file = self._ytmusic_oauth_client_file()
        if not client_file:
            QMessageBox.warning(self, "OAuth 설정 없음", "배포 폴더의 config/google_oauth_client.json 파일을 찾을 수 없습니다.")
            return
        worker = GoogleOAuthWorker(client_file, self._ytmusic_oauth_token_file(), self._ytmusic_oauth_account_file())
        worker.log_message.connect(lambda message: self._append_log("system", message))
        worker.connected.connect(self._on_google_oauth_connected)
        worker.failed.connect(self._on_google_oauth_failed)
        worker.finished.connect(self._google_oauth_worker_finished)
        self.google_oauth_worker = worker
        self._refresh_google_oauth_status()
        worker.start()

    def _on_google_oauth_connected(self, account_label: str) -> None:
        suffix = f": {account_label}" if account_label else ""
        self._append_log("system", f"Google 계정 연결됨{suffix}")
        self._dependency_status_cache_key = None
        self._refresh_settings_status_label()

    def _on_google_oauth_failed(self, message: str) -> None:
        self._append_log("system", f"Google OAuth 연결 실패: {message}")
        QMessageBox.warning(self, "OAuth 연결 실패", message)

    def _google_oauth_worker_finished(self) -> None:
        worker = self.google_oauth_worker
        self.google_oauth_worker = None
        if worker:
            worker.deleteLater()
        self._refresh_google_oauth_status()

    def _disconnect_google_oauth(self) -> None:
        failed: list[str] = []
        for path in (self._ytmusic_oauth_token_file(), self._ytmusic_oauth_account_file()):
            if not path.exists():
                continue
            try:
                path.unlink()
            except Exception as exc:
                failed.append(str(exc))
        if failed:
            QMessageBox.warning(self, "연결 해제 실패", "\n".join(failed))
            return
        self._append_log("system", "Google 계정 연결 해제됨")
        self._dependency_status_cache_key = None
        self._refresh_settings_status_label()

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
        cookie_file = "설정됨" if self.cookie_file_input.text().strip() else "미설정"
        google_oauth = "연결됨" if self._ytmusic_oauth_connected() else "클라이언트 준비" if self._ytmusic_oauth_client_file() else "미설정"
        ytmusic_auth = (
            "Google OAuth"
            if self._ytmusic_oauth_connected()
            else "수동 JSON"
            if self.auth_path_input.text().strip()
            else "쿠키 파일"
            if self.cookie_file_input.text().strip()
            else "미설정"
        )
        return [
            ("Google OAuth", google_oauth),
            ("쿠키 파일", cookie_file),
            ("YTMusic 인증", ytmusic_auth),
            ("YTMusic 인증 JSON", "고급 fallback 설정됨" if self.auth_path_input.text().strip() else "고급 fallback 미설정"),
            ("AcoustID 클라이언트 키", "설정됨" if self.acoustid_key_input.text().strip() else "미설정"),
        ]

    def closeEvent(self, event: Any) -> None:
        if self._work_running():
            if QApplication.platformName() != "offscreen":
                result = QMessageBox.question(
                    self,
                    "작업 실행 중",
                    "진행 중인 작업이 있습니다. 작업을 취소하고 종료할까요?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if result != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
            self._cancel_all_work()
            self._wait_for_workers(3000)
        self._stop_cover_preview_workers()
        self.save_settings()
        super().closeEvent(event)

    def _add_url(self) -> None:
        urls = _extract_urls(self.url_input.text())
        if not urls:
            return
        supported, unsupported = _supported_urls(urls)
        if unsupported:
            message = "\n".join(unsupported[:5])
            if QApplication.platformName() == "offscreen":
                self._append_log("system", f"지원하지 않는 URL: {message}")
            else:
                QMessageBox.warning(self, "지원하지 않는 URL", message)
        duplicates = [url for url in supported if self._has_existing_url(url)]
        if duplicates and not self._should_add_duplicates(duplicates):
            supported = [url for url in supported if url not in set(duplicates)]
        if not supported:
            self.url_input.clear()
            return
        output_dir = Path(self.output_dir_input.text().strip()) if self.output_dir_input.text().strip() else default_output_dir()
        last_row = -1
        for url in supported:
            _job, row = self._insert_job(url, output_dir=output_dir)
            last_row = row
        self.url_input.clear()
        if last_row >= 0:
            self.table.selectRow(last_row)
        self._refresh_actions()

    def _insert_job(self, url: str, *, output_dir: Path, row: int | None = None, message: str = "큐에 추가됨") -> tuple[DownloadJob, int]:
        job = DownloadJob(url=url, output_dir=output_dir)
        job.platform = detect_source_platform(url).value
        insert_row = self.table.rowCount() if row is None else max(0, min(row, self.table.rowCount()))
        self.jobs[job.id] = job
        self.row_job_ids.insert(insert_row, job.id)
        self.table.insertRow(insert_row)
        for col in range(len(self.COLUMNS)):
            self.table.setItem(insert_row, col, QTableWidgetItem(""))
        self._update_row(job)
        self._append_log(job.id, message)
        self._store_job(job)
        return job, insert_row

    def _expand_playlist(
        self,
        url: str,
        *,
        output_dir: Path,
        cookie_file: Path | None = None,
        auth_path: Path | None = None,
        use_ui_auth: bool = True,
        log: Callable[[str, str], None] | None = None,
    ) -> PlaylistExpansionResult:
        emit_log = log or self._append_log
        if self._playlist_expander:
            return self._playlist_expander(url)
        playlist_id = _youtube_playlist_id(url)
        if playlist_id and self._ytmusic_oauth_connected():
            return self._expand_playlist_with_youtube_data_api(playlist_id)
        try:
            result = self._expand_playlist_with_ytdlp(
                url,
                output_dir=output_dir,
                cookie_file=cookie_file,
                use_ui_auth=use_ui_auth,
            )
        except Exception as exc:
            if _should_try_ytmusicapi_playlist_expansion(url):
                try:
                    return self._expand_playlist_with_ytmusicapi(
                        url,
                        auth_path=auth_path,
                        cookie_file=cookie_file,
                        use_ui_auth=use_ui_auth,
                    )
                except Exception as fallback_exc:
                    raise RuntimeError(f"{exc}\nYouTube Music 전체 펼치기 실패: {fallback_exc}") from fallback_exc
            raise
        if _should_retry_with_ytmusicapi_playlist_expansion(url, result):
            try:
                fallback = self._expand_playlist_with_ytmusicapi(
                    url,
                    auth_path=auth_path,
                    cookie_file=cookie_file,
                    use_ui_auth=use_ui_auth,
                )
            except Exception as exc:
                emit_log("system", f"YouTube Music 전체 펼치기 보강 실패: {exc}")
                if _is_liked_music_playlist(url) and not result.urls:
                    raise RuntimeError(f"YouTube Music 전체 펼치기 실패: {exc}") from exc
            else:
                if len(fallback.urls) > len(result.urls):
                    emit_log(
                        "system",
                        f"YouTube Music 전체 펼치기 보강: yt-dlp {len(result.urls)}개 -> 전체 {len(fallback.urls)}개",
                    )
                    return fallback
        return result

    def _expand_playlist_with_ytdlp(
        self,
        url: str,
        *,
        output_dir: Path,
        cookie_file: Path | None = None,
        use_ui_auth: bool = True,
    ) -> PlaylistExpansionResult:
        resolved_cookie_file = cookie_file
        if use_ui_auth and resolved_cookie_file is None:
            resolved_cookie_file = _optional_path(self.cookie_file_input.text())
        downloader = YTDLPDownloader(
            DownloadConfig(
                output_dir=output_dir,
                cookie_file=resolved_cookie_file,
                youtube_request_interval_seconds=YOUTUBE_YTDLP_REQUEST_INTERVAL_SECONDS,
            )
        )
        return downloader.expand_playlist(url)

    def _expand_playlist_with_ytmusicapi(
        self,
        url: str,
        *,
        auth_path: Path | None = None,
        cookie_file: Path | None = None,
        use_ui_auth: bool = True,
    ) -> PlaylistExpansionResult:
        playlist_id = _youtube_playlist_id(url)
        if not playlist_id:
            raise ValueError("YouTube playlist ID를 찾을 수 없습니다.")
        if self._ytmusic_oauth_connected():
            return self._expand_playlist_with_youtube_data_api(playlist_id)
        client = self._create_ytmusic_client(auth_path=auth_path, cookie_file=cookie_file, use_ui_auth=use_ui_auth)
        if playlist_id == "LM" and hasattr(client, "get_liked_songs"):
            playlist = client.get_liked_songs(limit=None)
        else:
            playlist = client.get_playlist(playlist_id, limit=None)
        tracks = playlist.get("tracks") if isinstance(playlist, dict) else None
        if not isinstance(tracks, list):
            raise ValueError("YouTube Music playlist tracks를 읽을 수 없습니다.")
        urls: list[str] = []
        skipped_count = 0
        for track in tracks:
            video_id = str(track.get("videoId") or "").strip() if isinstance(track, dict) else ""
            if video_id:
                urls.append(_ytmusic_track_url(video_id))
            else:
                skipped_count += 1
        return PlaylistExpansionResult(urls=urls, skipped_count=skipped_count, expected_count=len(tracks))

    def _expand_playlist_with_youtube_data_api(
        self,
        playlist_id: str,
        session: Any | None = None,
    ) -> PlaylistExpansionResult:
        access_token = self._youtube_data_api_access_token()
        http = session or requests.Session()
        urls: list[str] = []
        skipped_count = 0
        expected_count: int | None = None
        page_token = ""
        data_api_playlist_id = _youtube_data_api_playlist_id(playlist_id)
        while True:
            params = {
                "part": "contentDetails",
                "playlistId": data_api_playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            response = http.get(
                YOUTUBE_DATA_PLAYLIST_ITEMS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                timeout=30,
            )
            try:
                payload = response.json()
            except Exception as exc:
                raise ValueError(f"YouTube Data API 응답을 읽을 수 없습니다: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("YouTube Data API 응답 형식이 올바르지 않습니다.")
            if response.status_code >= 400 or payload.get("error"):
                error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                message = error.get("message") if isinstance(error, dict) else payload.get("error")
                raise ValueError(f"YouTube Data API 플레이리스트 조회 실패: {message or response.text}")
            page_info = payload.get("pageInfo")
            if expected_count is None and isinstance(page_info, dict):
                try:
                    expected_count = int(page_info.get("totalResults") or 0)
                except (TypeError, ValueError):
                    expected_count = None
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError("YouTube Data API 플레이리스트 items를 읽을 수 없습니다.")
            for item in items:
                content_details = item.get("contentDetails") if isinstance(item, dict) else {}
                video_id = str(content_details.get("videoId") or "").strip() if isinstance(content_details, dict) else ""
                if video_id:
                    urls.append(_ytmusic_track_url(video_id))
                else:
                    skipped_count += 1
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                break
        return PlaylistExpansionResult(
            urls=urls,
            skipped_count=skipped_count,
            expected_count=expected_count or len(urls) + skipped_count,
        )

    def _expand_liked_music_with_youtube_data_api(self, session: Any | None = None) -> PlaylistExpansionResult:
        return self._expand_playlist_with_youtube_data_api("LM", session)

    def _youtube_data_api_access_token(self) -> str:
        oauth_client_file = self._ytmusic_oauth_client_file()
        oauth_token_file = self._ytmusic_oauth_token_file()
        if not oauth_client_file or not oauth_token_file.exists():
            raise ValueError("Google OAuth 연결이 필요합니다.")
        client = load_ytmusic_oauth_client(oauth_client_file)
        token = refresh_ytmusic_oauth_token_if_needed(client, oauth_token_file)
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise ValueError("OAuth access_token이 비어 있습니다. Google 계정을 다시 연결하세요.")
        return access_token

    def _create_ytmusic_client(
        self,
        *,
        auth_path: Path | None = None,
        cookie_file: Path | None = None,
        use_ui_auth: bool = True,
    ) -> Any:
        from ytmusicapi import YTMusic

        if use_ui_auth and auth_path is None:
            auth_path = _optional_path(self.auth_path_input.text())
        if auth_path and auth_path.exists():
            return YTMusic(str(auth_path))

        if use_ui_auth and cookie_file is None:
            cookie_file = _optional_path(self.cookie_file_input.text())
        if cookie_file and cookie_file.exists():
            from cueforge.metadata.ytmusic_auth import YTMusicCookieAuthConfig, build_ytmusic_cookie_auth

            auth = build_ytmusic_cookie_auth(YTMusicCookieAuthConfig(cookie_file=cookie_file))
            return YTMusic(auth)

        return YTMusic()

    def _start_next(self) -> None:
        self._process_next()

    def _analyze_next(self) -> None:
        self._process_next()

    def _process_next(self) -> None:
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        index = 0
        while index < len(self.row_job_ids):
            job_id = self.row_job_ids[index]
            job = self.jobs[job_id]
            if job.status == DownloadStatus.PENDING:
                if _is_playlist_url(job.url):
                    if self._start_playlist_expansion(job, mode="playlist_process"):
                        return
                self._run_worker(job, analyze_only=False, worker_mode="process")
                return
            index += 1
        for job_id in self.row_job_ids:
            job = self.jobs[job_id]
            if job.status == DownloadStatus.APPROVED:
                self._run_worker(job, approved_metadata=job.selected_metadata, worker_mode="process")
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
        if job.status == DownloadStatus.FAILED and not _can_retry_job(job):
            self._append_log(job.id, "재시도 대상이 아닌 실패 항목이라 분석을 다시 시작하지 않음")
            self._refresh_actions()
            return
        if _is_playlist_url(job.url):
            if self._start_playlist_expansion(job, mode="playlist_single"):
                return
            self._run_worker(job, analyze_only=False, continue_queue=False)
            return
        self._prepare_job_retry(job, message="선택 항목 분석 시작")
        self._run_worker(job, analyze_only=False, continue_queue=False)

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
        skipped = 0
        for job in list(self.jobs.values()):
            if job.status != DownloadStatus.FAILED:
                continue
            if not _can_retry_job(job):
                skipped += 1
                continue
            if _is_playlist_url(job.url):
                job.status = DownloadStatus.PENDING
                job.progress = 0.0
                job.error = ""
                job.error_message = ""
                job.error_category = ""
                job.retry_count += 1
                self._update_row(job)
                self._append_log(job.id, "재시도를 위해 큐에 추가됨")
                retried += 1
                continue
            job.status = DownloadStatus.PENDING
            job.progress = 0.0
            job.error = ""
            job.error_message = ""
            job.error_category = ""
            job.retry_count += 1
            self._update_row(job)
            self._append_log(job.id, "재시도를 위해 큐에 추가됨")
            retried += 1
        self._refresh_actions()
        if skipped:
            self._append_log("system", f"재시도 대상이 아닌 실패 항목 {skipped}개는 건너뜀")
        if retried:
            self._process_next()

    def _retry_selected(self) -> None:
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        job = self._selected_job()
        if not job or not _can_retry_job(job):
            self._refresh_actions()
            return
        if _is_playlist_url(job.url):
            job.status = DownloadStatus.PENDING
            job.progress = 0.0
            job.error = ""
            job.error_message = ""
            job.error_category = ""
            job.retry_count += 1
            self._update_row(job)
            if self._start_playlist_expansion(job, mode="playlist_single"):
                return
            self._run_worker(job, analyze_only=False, continue_queue=False)
            return
        self._prepare_job_retry(job, message="선택 항목 재시도 시작")
        self._run_worker(job, analyze_only=False, continue_queue=False)

    def _prepare_job_retry(self, job: DownloadJob, *, message: str) -> None:
        _cleanup_temp_download(job)
        fallback_url = _single_video_url_from_playlist_url(job.url)
        if fallback_url:
            self._append_log(job.id, f"플레이리스트 매개변수 제거: {fallback_url}")
            job.url = fallback_url
            job.platform = detect_source_platform(job.url).value
        job.status = DownloadStatus.PENDING
        job.progress = 0.0
        job.error = ""
        job.error_message = ""
        job.error_category = ""
        job.retry_count += 1
        self._update_row(job)
        self._append_log(job.id, message)

    def _prepare_playlist_job_for_analysis(self, job: DownloadJob) -> list[DownloadJob]:
        if not _is_playlist_url(job.url):
            return []
        fallback_url = _single_video_url_from_playlist_url(job.url)
        if fallback_url:
            self._prepare_job_retry(job, message="플레이리스트 매개변수를 제거하고 분석 시작")
            return [job]

        self._append_log(job.id, "플레이리스트 분석 준비: 개별 항목으로 펼치는 중")
        try:
            result = self._expand_playlist(job.url, output_dir=job.output_dir)
        except Exception as exc:
            message = _playlist_expansion_failure_message(
                job.url,
                exc,
                auth_diagnostic=self._liked_music_auth_diagnostic() if _is_liked_music_playlist(job.url) else "",
            )
            category, text = user_facing_error(message)
            job.status = DownloadStatus.FAILED
            job.error_category = category.value
            job.error_message = message
            job.error = text
            self._update_row(job)
            self._append_log(job.id, f"플레이리스트 펼치기 실패: {message}")
            return []

        return self._apply_playlist_expansion(job, result)

    def _start_playlist_expansion(self, job: DownloadJob, *, mode: str) -> bool:
        if not _is_playlist_url(job.url):
            return False
        fallback_url = _single_video_url_from_playlist_url(job.url)
        if fallback_url:
            self._prepare_job_retry(job, message="플레이리스트 매개변수를 제거하고 분석 시작")
            return False
        if self.worker and self.worker.isRunning():
            return True

        auth_path = _optional_path(self.auth_path_input.text())
        cookie_file = _optional_path(self.cookie_file_input.text())

        def expand(url: str, output_dir: Path, log: Callable[[str, str], None]) -> PlaylistExpansionResult:
            return self._expand_playlist(
                url,
                output_dir=output_dir,
                auth_path=auth_path,
                cookie_file=cookie_file,
                use_ui_auth=False,
                log=log,
            )

        self.save_settings()
        self.cancel_requested = False
        self._playlist_expanded_job_ids = []
        job.status = DownloadStatus.METADATA
        job.progress = 0.0
        job.error = ""
        job.error_message = ""
        job.error_category = ""
        self._update_row(job)
        self._append_log(job.id, "플레이리스트 분석 준비: 개별 항목으로 펼치는 중")
        self.worker_mode = mode
        self.worker = PlaylistExpansionWorker(job, expand)
        self.worker.playlist_ready.connect(self._on_playlist_expanded)
        self.worker.playlist_failed.connect(self._on_playlist_expansion_failed)
        self.worker.log_message.connect(self._append_log)
        self.worker.finished.connect(self._playlist_worker_finished)
        self.worker.start()
        self._refresh_actions()
        return True

    def _on_playlist_expanded(self, job_id: str, result: PlaylistExpansionResult) -> None:
        job = self.jobs.get(job_id)
        if not job:
            self._playlist_expanded_job_ids = []
            return
        inserted = self._apply_playlist_expansion(job, result)
        self._playlist_expanded_job_ids = [item.id for item in inserted]

    def _on_playlist_expansion_failed(self, job_id: str, error: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        message = _playlist_expansion_failure_message(
            job.url,
            error,
            auth_diagnostic=self._liked_music_auth_diagnostic() if _is_liked_music_playlist(job.url) else "",
        )
        category, text = user_facing_error(message)
        job.status = DownloadStatus.FAILED
        job.progress = 0.0
        job.error_category = category.value
        job.error_message = message
        job.error = text
        self._update_row(job)
        self._append_log(job.id, f"플레이리스트 펼치기 실패: {message}")

    def _playlist_worker_finished(self) -> None:
        mode = self.worker_mode
        inserted_ids = list(getattr(self, "_playlist_expanded_job_ids", []))
        self.worker = None
        self.worker_mode = ""
        self.cancel_requested = False
        self._playlist_expanded_job_ids = []
        self._refresh_actions()
        if mode == "playlist_process":
            self._process_next()
        elif mode == "playlist_analysis":
            self._analyze_next()
        elif mode == "playlist_scheduler":
            self._schedule_pending_analysis()
        elif mode == "playlist_single" and inserted_ids:
            first_job = self.jobs.get(inserted_ids[0])
            if first_job:
                self._select_job_row(first_job)
                self._run_worker(first_job, analyze_only=False, continue_queue=False)

    def _apply_playlist_expansion(self, job: DownloadJob, result: PlaylistExpansionResult) -> list[DownloadJob]:
        urls = [url for url in _dedupe_preserving_order(result.urls) if not self._has_existing_url(url)]
        if _is_incomplete_playlist_expansion(result):
            message = _playlist_incomplete_message(job.url, result)
            category, text = user_facing_error(message)
            job.status = DownloadStatus.FAILED
            job.error_category = category.value
            job.error_message = message
            job.error = text
            self._update_row(job)
            self._append_log(job.id, message)
            return []

        job.status = DownloadStatus.DONE
        job.progress = 100.0
        job.error = ""
        job.error_message = ""
        job.error_category = ""
        self._update_row(job)
        row = self.row_job_ids.index(job.id) + 1
        if not urls:
            message = "플레이리스트에서 새로 추가할 수 있는 항목이 없습니다."
            self._append_log(job.id, message)
            return []

        inserted: list[DownloadJob] = []
        for offset, url in enumerate(urls):
            new_job, _row = self._insert_job(
                url,
                output_dir=job.output_dir,
                row=row + offset,
                message="플레이리스트에서 큐에 추가됨",
            )
            inserted.append(new_job)
        skipped = f", {result.skipped_count}개 건너뜀" if result.skipped_count else ""
        self._append_log("system", f"플레이리스트 분석 준비 완료: {len(inserted)}개 추가{skipped}")
        return inserted

    def _prepare_playlist_job_retry(self, job: DownloadJob) -> list[DownloadJob]:
        return self._prepare_playlist_job_for_analysis(job)

    def _liked_music_auth_diagnostic(self) -> str:
        if self._ytmusic_oauth_connected():
            return "Google OAuth 연결은 되어 있지만 YouTube Data API가 좋아요 표시한 음악 목록 접근을 거부했습니다. 연결 해제 후 다시 연결하거나 계정 권한을 확인하세요."
        if self._ytmusic_oauth_client_file():
            return "Google OAuth 클라이언트는 포함됐지만 아직 Google 계정 연결이 완료되지 않았습니다."

        cookie_file = _optional_path(self.cookie_file_input.text())
        if not cookie_file:
            return "현재 설정에 Google OAuth 연결 또는 쿠키 파일이 없습니다."
        if not cookie_file.exists():
            return "설정된 쿠키 파일을 찾을 수 없습니다."
        try:
            from cueforge.metadata.ytmusic_auth import YTMusicCookieAuthConfig, YTMusicCookieAuthError, build_ytmusic_cookie_auth

            build_ytmusic_cookie_auth(YTMusicCookieAuthConfig(cookie_file=cookie_file))
        except YTMusicCookieAuthError as exc:
            return f"쿠키 파일은 설정됐지만 music.youtube.com 인증 쿠키를 만들 수 없습니다: {exc}"
        except Exception as exc:
            return f"쿠키 파일을 확인할 수 없습니다: {exc}"
        return "쿠키 파일에 music.youtube.com 인증 쿠키는 있지만, 세션 만료 또는 계정 권한 문제일 수 있습니다."

    def _cancel_current_job(self) -> None:
        if self.scheduler and self.scheduler.is_running():
            self.cancel_requested = True
            self.scheduler.cancel_all()
            self._append_log("system", "실행 중인 작업 취소 요청됨")
            self._refresh_actions()
            return
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
        worker_mode: str | None = None,
    ) -> None:
        self.save_settings()
        self.cancel_requested = False
        job.status = DownloadStatus.DOWNLOADING if approved_metadata else DownloadStatus.METADATA
        job.platform = detect_source_platform(job.url).value
        self._update_row(job)
        self.worker_mode = worker_mode or (("analysis" if analyze_only else "download") if continue_queue else "single")
        self.worker = JobWorker(
            job,
            cookie_file=_optional_path(self.cookie_file_input.text()),
            ytmusic_auth_path=_optional_path(self.auth_path_input.text()),
            ytmusic_oauth_client_file=self._ytmusic_oauth_client_file(),
            ytmusic_oauth_token_file=self._ytmusic_oauth_token_file() if self._ytmusic_oauth_token_file().exists() else None,
            ffmpeg_location=_optional_path(self.ffmpeg_path_input.text()),
            acoustid_config=AcoustIDConfig(
                client_key=self.acoustid_key_input.text().strip(),
                fpcalc_path=_optional_path(self.fpcalc_path_input.text()),
            ),
            audio_recognition_enabled=self.audio_recognition_checkbox.isChecked(),
            verify_auto_approved_metadata=self.verify_auto_approved_checkbox.isChecked(),
            approved_metadata=approved_metadata,
            analyze_only=analyze_only,
            tag_semaphore=self.scheduler.tag_semaphore if self.scheduler else None,
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
        job.candidate_summaries = []
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
            if self.scheduler and self.scheduler.is_running():
                self.scheduler.enqueue_downloads([job])
        else:
            self._append_log(job_id, f"메타데이터 검수 필요: {_review_state_label(review_state)}")
        self._refresh_actions()

    def _on_job_done(self, job_id: str, final_path: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.DONE
        job.progress = 100.0
        job.final_path = Path(final_path)
        job.error = ""
        job.error_message = ""
        job.error_category = ""
        self._update_row(job)
        self._append_log(job_id, f"완료: {final_path}")
        self._refresh_actions()

    def _on_job_failed(self, job_id: str, error: str) -> None:
        job = self.jobs[job_id]
        category, friendly = user_facing_error(error)
        job.status = DownloadStatus.FAILED
        job.error = friendly
        job.error_message = str(error)
        job.error_category = category.value
        self._update_row(job)
        self._append_log(job_id, f"실패: {friendly}")
        if category == ErrorCategory.RATE_LIMITED:
            self._stop_after_rate_limit()
        self._refresh_actions()

    def _stop_after_rate_limit(self) -> None:
        self.cancel_requested = True
        self.worker_mode = "canceled"
        if self.scheduler and self.scheduler.is_running():
            self.scheduler.cancel_all()
        self._append_log(
            "system",
            "YouTube rate-limit이 감지되어 남은 작업 시작을 중지했습니다. 한 시간 정도 쉰 뒤 실패 항목만 재시도하세요.",
        )

    def _on_job_canceled(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.CANCELED
        job.progress = 0.0
        job.error = ""
        job.error_message = ""
        job.error_category = ""
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
        elif mode == "process":
            self._process_next()

    def _approve_selected(self) -> None:
        job = self._active_review_job()
        if not job:
            self.log.appendPlainText("[검수] 승인 생략: 검수할 트랙이 로드되지 않음")
            QMessageBox.warning(self, "트랙 없음", "승인하기 전에 검수 탭에서 트랙을 로드하세요.")
            return
        metadata = self._metadata_from_review_fields(job.selected_metadata)
        if not metadata.is_minimum_viable():
            QMessageBox.warning(self, "필수 태그 누락", "제목과 아티스트를 입력한 뒤 승인하세요.")
            return
        job.selected_metadata = metadata
        job.status = DownloadStatus.APPROVED
        self._update_row(job)
        self._append_log(job.id, "메타데이터 승인됨; 다운로드 시작")
        self._load_next_review_or_current(job)
        self._download_approved_job(job)
        self._refresh_actions()

    def _download_approved_job(self, job: DownloadJob) -> None:
        if job.status != DownloadStatus.APPROVED:
            return
        if self.scheduler and self.scheduler.is_running():
            self.scheduler.enqueue_downloads([job])
            return
        if self.worker and self.worker.isRunning():
            self._append_log(job.id, "현재 작업 뒤 다운로드 예정")
            return
        self._run_worker(job, approved_metadata=job.selected_metadata, continue_queue=False)

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

    def _remove_active_review_job(self) -> None:
        job = self._loaded_review_job()
        if not job:
            QMessageBox.warning(self, "트랙 없음", "삭제할 검수 항목이 없습니다.")
            return
        if self._work_running() or job.status in _ACTIVE_STATUSES:
            QMessageBox.warning(self, "삭제할 수 없음", "실행 중인 작업이 끝난 뒤 삭제하세요.")
            return
        self._append_log(job.id, "검수 화면에서 큐에서 삭제됨")
        self._remove_job(job)
        self._refresh_actions()

    def _remove_selected(self) -> None:
        jobs = self._selected_jobs()
        if not jobs:
            return
        removable = [job for job in jobs if job.status not in _ACTIVE_STATUSES]
        active_count = len(jobs) - len(removable)
        if not removable:
            QMessageBox.warning(self, "삭제할 수 없음", "실행 중인 작업은 삭제할 수 없습니다.")
            return
        self._remove_jobs(removable)
        if active_count:
            self._append_log("system", f"실행 중인 작업 {active_count}개는 삭제하지 않음")
        self._refresh_actions()

    def _remove_done_jobs(self) -> None:
        done_jobs = [self.jobs[job_id] for job_id in self.row_job_ids if self.jobs[job_id].status == DownloadStatus.DONE]
        if not done_jobs:
            self._refresh_actions()
            return
        removed_count = len(done_jobs)
        self._remove_jobs(done_jobs)
        self._append_log("system", f"완료 항목 {removed_count}개를 큐에서 제거함")
        self._refresh_actions()

    def _remove_job(self, job: DownloadJob) -> None:
        self._remove_jobs([job])

    def _remove_jobs(self, jobs: list[DownloadJob]) -> None:
        removable = [job for job in jobs if job.id in self.jobs]
        if not removable:
            return
        row_by_job_id = {job_id: row for row, job_id in enumerate(self.row_job_ids)}
        rows_and_jobs = sorted(
            ((row_by_job_id[job.id], job) for job in removable),
            key=lambda item: item[0],
            reverse=True,
        )
        removed_ids = [job.id for _row, job in rows_and_jobs]
        removed_id_set = set(removed_ids)
        active_review_removed = bool(self.active_review_job_id and self.active_review_job_id in removed_id_set)
        previous_signal_state = self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        try:
            for _row, job in rows_and_jobs:
                _cleanup_temp_download(job)
            for row, job in rows_and_jobs:
                self.table.removeRow(row)
                self.row_job_ids.pop(row)
                del self.jobs[job.id]
            self.table.clearSelection()
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.blockSignals(previous_signal_state)
        self.job_store.delete_jobs(removed_ids)
        if active_review_removed:
            self.active_review_job_id = None
            next_review = self._next_review_job()
            if next_review:
                self._load_job_for_review(next_review, select_row=False)
            else:
                self._clear_review_panel()
        self._refresh_pipeline_board()
        self._refresh_history()

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
        self.candidate_label.setText(f"최상위 후보: {candidate.provider} {candidate.score:.2f} - {bucket} ({matched}){trust_note}")
        self.confidence_detail_label.setText(_confidence_explanation(candidate, reference=reference))

    def _populate_candidate_table(self, job: DownloadJob) -> None:
        self.candidate_table.setRowCount(len(job.candidates))
        for row, candidate in enumerate(job.candidates):
            values = (
                candidate.provider,
                f"{candidate.score:.3f}",
                _confidence_bucket(candidate),
                _candidate_badges(candidate, job.selected_metadata),
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
                    item.setForeground(QColor("#202124"))
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
        return TrackMetadata(
            title=self.review_fields["title"].text().strip(),
            artist=self.review_fields["artist"].text().strip(),
            album=self.review_fields["album"].text().strip(),
            album_artist=self.review_fields["album_artist"].text().strip(),
            genre=self.review_fields["genre"].text().strip(),
            release_date=self.review_fields["release_date"].text().strip(),
            track_number=base.track_number,
            disc_number=base.disc_number,
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
        job.platform = job.platform or detect_source_platform(job.url).value
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
        self._store_job(job)
        self._refresh_pipeline_board()
        self._refresh_history()

    def _selected_job(self) -> DownloadJob | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.row_job_ids):
            return None
        return self.jobs[self.row_job_ids[row]]

    def _selected_jobs(self) -> list[DownloadJob]:
        selection = self.table.selectionModel()
        rows = sorted({index.row() for index in selection.selectedRows()}) if selection else []
        if not rows and 0 <= self.table.currentRow() < len(self.row_job_ids):
            rows = [self.table.currentRow()]
        return [self.jobs[self.row_job_ids[row]] for row in rows if 0 <= row < len(self.row_job_ids)]

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

    def _scheduler_limits(self) -> SchedulerLimits:
        return SchedulerLimits(
            metadata=self.metadata_parallel_spin.value(),
            download=self.download_parallel_spin.value(),
            tagging=self.tagging_parallel_spin.value(),
        ).normalized()

    def _start_pipeline(self) -> None:
        self.save_settings()
        self._schedule_pending_analysis()
        self._schedule_approved_downloads()

    def _schedule_pending_analysis(self) -> None:
        if not self.scheduler:
            return
        if self.worker and self.worker.isRunning():
            self._refresh_actions()
            return
        jobs: list[DownloadJob] = []
        for job_id in list(self.row_job_ids):
            job = self.jobs.get(job_id)
            if not job or job.status != DownloadStatus.PENDING:
                continue
            if _is_playlist_url(job.url):
                if jobs:
                    self.scheduler.enqueue_analysis(jobs)
                    jobs = []
                if self._start_playlist_expansion(job, mode="playlist_scheduler"):
                    return
            if job.status == DownloadStatus.PENDING:
                jobs.append(job)
        self.scheduler.enqueue_analysis(jobs)
        self._refresh_actions()

    def _schedule_approved_downloads(self) -> None:
        if not self.scheduler:
            return
        self.save_settings()
        jobs = [self.jobs[job_id] for job_id in self.row_job_ids if self.jobs[job_id].status == DownloadStatus.APPROVED]
        self.scheduler.enqueue_downloads(jobs)
        self._refresh_actions()

    def _create_scheduled_worker(
        self,
        job: DownloadJob,
        stage: str,
        tag_semaphore: threading.Semaphore,
    ) -> JobWorker:
        self.cancel_requested = False
        approved_metadata = job.selected_metadata if stage == "download" else None
        job.status = DownloadStatus.DOWNLOADING if approved_metadata else DownloadStatus.METADATA
        job.platform = detect_source_platform(job.url).value
        self._update_row(job)
        worker = JobWorker(
            job,
            cookie_file=_optional_path(self.cookie_file_input.text()),
            ytmusic_auth_path=_optional_path(self.auth_path_input.text()),
            ytmusic_oauth_client_file=self._ytmusic_oauth_client_file(),
            ytmusic_oauth_token_file=self._ytmusic_oauth_token_file() if self._ytmusic_oauth_token_file().exists() else None,
            ffmpeg_location=_optional_path(self.ffmpeg_path_input.text()),
            acoustid_config=AcoustIDConfig(
                client_key=self.acoustid_key_input.text().strip(),
                fpcalc_path=_optional_path(self.fpcalc_path_input.text()),
            ),
            audio_recognition_enabled=self.audio_recognition_checkbox.isChecked(),
            verify_auto_approved_metadata=self.verify_auto_approved_checkbox.isChecked(),
            approved_metadata=approved_metadata,
            analyze_only=stage == "metadata",
            tag_semaphore=tag_semaphore,
        )
        worker.progress_changed.connect(self._on_progress)
        worker.metadata_ready.connect(self._on_metadata_ready)
        worker.job_done.connect(self._on_job_done)
        worker.job_failed.connect(self._on_job_failed)
        worker.job_canceled.connect(self._on_job_canceled)
        worker.log_message.connect(self._append_log)
        return worker

    def _on_scheduled_job_started(self, job_id: str, stage: str) -> None:
        self._record_event(job_id, "started", f"{stage} 작업 시작")
        self._refresh_actions()

    def _on_scheduler_idle(self) -> None:
        self.cancel_requested = False
        self._refresh_actions()

    def _work_running(self) -> bool:
        legacy_running = bool(self.worker and self.worker.isRunning())
        scheduler_running = bool(self.scheduler and self.scheduler.is_running())
        return legacy_running or scheduler_running

    def _cancel_all_work(self) -> None:
        if self.scheduler and self.scheduler.is_running():
            self.scheduler.cancel_all()
        if self.worker and self.worker.isRunning():
            cancel = getattr(self.worker, "cancel", None)
            if callable(cancel):
                cancel()
            elif hasattr(self.worker, "requestInterruption"):
                self.worker.requestInterruption()
            else:
                self.worker = None
        self.cancel_requested = True

    def _wait_for_workers(self, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline and self._work_running():
            QApplication.processEvents()
            time.sleep(0.05)

    def _stop_cover_preview_workers(self) -> None:
        for worker in list(self._cover_preview_workers):
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(500)
            worker.deleteLater()
        self._cover_preview_workers.clear()

    def _store_job(self, job: DownloadJob) -> None:
        try:
            self.job_store.upsert_job(job)
        except Exception as exc:
            self.log.appendPlainText(f"[store] 저장 실패: {exc}")

    def _record_event(self, job_id: str, event_type: str, message: str, category: str = "") -> None:
        if job_id not in self.jobs:
            return
        try:
            self.job_store.record_event(JobEvent(job_id=job_id, event_type=event_type, category=category, message=message))
        except Exception as exc:
            self.log.appendPlainText(f"[store] 이벤트 저장 실패: {exc}")

    def _load_jobs_from_store(self) -> None:
        try:
            stored_jobs = self.job_store.load_jobs()
        except Exception as exc:
            self.log.appendPlainText(f"[store] 이력 로드 실패: {exc}")
            return
        for job in stored_jobs:
            if job.id in self.jobs:
                continue
            self.jobs[job.id] = job
            self.row_job_ids.append(job.id)
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col in range(len(self.COLUMNS)):
                self.table.setItem(row, col, QTableWidgetItem(""))
            self._update_row(job)
        self._refresh_actions()

    def _refresh_pipeline_board(self) -> None:
        if not self.pipeline_tables or self._loading_pipeline:
            return
        self._loading_pipeline = True
        try:
            for table in self.pipeline_tables.values():
                table.setRowCount(0)
            for job_id in self.row_job_ids:
                job = self.jobs[job_id]
                status = _pipeline_status(job.status)
                table = self.pipeline_tables.get(status)
                if not table:
                    continue
                row = table.rowCount()
                table.insertRow(row)
                track_label = job.selected_metadata.title or _download_status_label(job.status)
                if job.selected_metadata.artist:
                    track_label = f"{job.selected_metadata.artist} - {track_label}"
                values = (
                    track_label,
                    job.url,
                )
                for col, value in enumerate(values):
                    item = QTableWidgetItem(str(value or ""))
                    item.setData(Qt.ItemDataRole.UserRole, job.id)
                    table.setItem(row, col, item)
            for status, table in self.pipeline_tables.items():
                table.parentWidget().setTitle(f"{_pipeline_status_label(status)} ({table.rowCount()})")
        finally:
            self._loading_pipeline = False

    def _load_selected_pipeline_job(self) -> None:
        if self._loading_pipeline:
            return
        job = self._selected_pipeline_job()
        if not job:
            return
        self._select_job_row(job)
        self._load_job_for_review(job, select_row=False)
        self.pipeline_detail.setPlainText(self._job_detail_text(job))

    def _selected_pipeline_job(self) -> DownloadJob | None:
        for table in self.pipeline_tables.values():
            row = table.currentRow()
            if row < 0:
                continue
            item = table.item(row, 0)
            if not item:
                continue
            job_id = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(job_id, str):
                return self.jobs.get(job_id)
        return None

    def _refresh_history(self) -> None:
        if self._loading_history:
            return
        self._loading_history = True
        try:
            history_jobs = [
                self.jobs[job_id]
                for job_id in self.row_job_ids
                if self.jobs[job_id].status in _TERMINAL_STATUSES
            ]
            self.history_table.setRowCount(len(history_jobs))
            for row, job in enumerate(history_jobs):
                values = (
                    _download_status_label(job.status),
                    job.selected_metadata.title,
                    job.selected_metadata.artist,
                    job.error_category or job.error,
                    str(job.final_path or ""),
                )
                for col, value in enumerate(values):
                    item = QTableWidgetItem(str(value or ""))
                    item.setData(Qt.ItemDataRole.UserRole, job.id)
                    self.history_table.setItem(row, col, item)
        finally:
            self._loading_history = False

    def _load_selected_history_job(self) -> None:
        if self._loading_history:
            return
        row = self.history_table.currentRow()
        if row < 0:
            return
        item = self.history_table.item(row, 0)
        if not item:
            return
        job_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(job_id, str) and job_id in self.jobs:
            self._load_job_for_review(self.jobs[job_id], select_row=False)

    def _clear_history(self) -> None:
        terminal_ids = [job_id for job_id in self.row_job_ids if self.jobs[job_id].status in _TERMINAL_STATUSES]
        if not terminal_ids:
            return
        for job_id in terminal_ids:
            row = self.row_job_ids.index(job_id)
            self.table.removeRow(row)
            self.row_job_ids.pop(row)
            del self.jobs[job_id]
        self.job_store.clear_history()
        self._refresh_pipeline_board()
        self._refresh_history()
        self._refresh_actions()

    def _job_detail_text(self, job: DownloadJob) -> str:
        metadata = job.selected_metadata
        lines = [
            f"상태: {_download_status_label(job.status)}",
            f"소스: {detect_source_platform(job.url).display_name}",
            f"URL: {job.url}",
            f"제목: {metadata.title}",
            f"아티스트: {metadata.artist}",
            f"앨범: {metadata.album}",
            f"출력: {job.final_path or job.output_dir}",
            f"재시도: {job.retry_count}",
        ]
        if job.error_category or job.error:
            lines.append(f"오류: {job.error_category or 'unknown'}")
            lines.append(job.error or action_hint(job.error_category))
        events = self.job_store.list_events(job.id, limit=8)
        if events:
            lines.append("")
            lines.append("최근 이벤트")
            lines.extend(f"- {event.event_type}: {event.message}" for event in events)
        return "\n".join(lines)

    def _has_existing_url(self, url: str) -> bool:
        return any(job.url == url for job in self.jobs.values())

    def _should_add_duplicates(self, duplicates: list[str]) -> bool:
        if QApplication.platformName() == "offscreen":
            return False
        text = "이미 큐 또는 이력에 있는 URL입니다. 다시 추가할까요?\n" + "\n".join(duplicates[:5])
        result = QMessageBox.question(
            self,
            "중복 URL",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def _refresh_actions(self) -> None:
        running = self._work_running()
        pending_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.PENDING)
        approved_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.APPROVED)
        review_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.REVIEW_REQUIRED)
        failed_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.FAILED)
        retryable_failed_count = sum(
            1 for job in self.jobs.values() if _can_retry_job(job) and job.status == DownloadStatus.FAILED
        )
        done_count = sum(1 for job in self.jobs.values() if job.status == DownloadStatus.DONE)
        selected_job = self._selected_job()
        active_review = self._active_review_job()
        can_start_pipeline = (pending_count > 0 or approved_count > 0) and not running
        can_download = approved_count > 0 and not running
        can_retry = retryable_failed_count > 0 and not running
        can_approve = bool(active_review and active_review.status == DownloadStatus.REVIEW_REQUIRED)
        can_move_selected_to_review = bool(selected_job and selected_job.status == DownloadStatus.APPROVED and not running)
        can_move_active_to_review = bool(active_review and active_review.status == DownloadStatus.APPROVED and not running)
        can_analyze_selected = bool(
            selected_job
            and selected_job.status in _ANALYZABLE_STATUSES
            and (selected_job.status != DownloadStatus.FAILED or _can_retry_job(selected_job))
            and not running
        )
        can_download_selected = bool(selected_job and selected_job.status == DownloadStatus.APPROVED and not running)
        can_retry_selected = bool(selected_job and _can_retry_job(selected_job) and not running)
        selected_jobs = self._selected_jobs()
        can_remove_selected = any(job.status not in _ACTIVE_STATUSES for job in selected_jobs)
        loaded_review = self._loaded_review_job()
        can_remove_active_review = bool(loaded_review and loaded_review.status not in _ACTIVE_STATUSES and not running)
        can_cancel_current = running and not self.cancel_requested
        self._refresh_review_queue()

        if self.start_action:
            self.start_action.setEnabled(can_start_pipeline)
        if self.download_action:
            self.download_action.setEnabled(can_download)
        if self.retry_action:
            self.retry_action.setEnabled(can_retry)
        if self.start_queue_button:
            self.start_queue_button.setEnabled(can_start_pipeline)
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
        if self.remove_done_button:
            self.remove_done_button.setEnabled(done_count > 0)
        if self.remove_selected_button:
            self.remove_selected_button.setEnabled(can_remove_selected)
        if self.cancel_current_button:
            self.cancel_current_button.setEnabled(can_cancel_current)
        if self.approve_button:
            self.approve_button.setEnabled(can_approve)
        if self.reopen_review_button:
            self.reopen_review_button.setEnabled(can_move_active_to_review)
        if self.remove_review_button:
            self.remove_review_button.setEnabled(can_remove_active_review)
        if self.pipeline_start_button:
            self.pipeline_start_button.setEnabled((pending_count > 0 or approved_count > 0) and not running)
        if self.pipeline_download_button:
            self.pipeline_download_button.setEnabled(can_download)
        if self.pipeline_retry_button:
            self.pipeline_retry_button.setEnabled(can_retry)
        if self.clear_history_button:
            self.clear_history_button.setEnabled(any(job.status in _TERMINAL_STATUSES for job in self.jobs.values()))
        if self.tabs:
            self.tabs.setTabText(self.review_tab_index, f"검수 ({review_count})" if review_count else "검수")

        if running and review_count:
            text = f"처리는 계속 진행 중입니다. 검수가 필요한 {review_count}개 트랙은 검수 탭에서 확인하세요."
        elif running:
            text = "현재 트랙을 처리 중입니다."
        elif approved_count and pending_count:
            text = f"{pending_count}개는 분석 대기, {approved_count}개는 다운로드 준비 완료입니다. 전체 처리 시작으로 이어서 진행할 수 있습니다."
        elif approved_count:
            text = f"{approved_count}개 트랙이 다운로드 준비 완료 상태입니다. 전체 처리 시작 또는 승인 항목 다운로드로 마무리하세요."
        elif review_count and pending_count:
            text = f"{review_count}개는 검수가 필요하고 {pending_count}개는 분석 대기 중입니다. 검수와 처리를 병행할 수 있습니다."
        elif review_count:
            text = f"{review_count}개 트랙은 메타데이터 검수가 필요합니다."
        elif pending_count:
            text = f"{pending_count}개 트랙이 추가되었습니다. 전체 처리 시작으로 분석을 시작하세요."
        elif failed_count and retryable_failed_count:
            text = f"{failed_count}개 트랙이 실패했습니다. 재시도 가능한 {retryable_failed_count}개는 실패 재시도로 다시 분석할 수 있습니다."
        elif failed_count:
            text = f"{failed_count}개 트랙이 실패했습니다. 이 실패는 재시도 대상이 아닙니다."
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
        previous_index = self._last_tab_index
        self._last_tab_index = index
        if previous_index == self.settings_tab_index and index != self.settings_tab_index:
            self.save_settings()
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
        event_type = _event_type_for_log(message)
        if event_type:
            category = self.jobs[job_id].error_category if job_id in self.jobs else ""
            self._record_event(job_id, event_type, message, category)
            if job_id in self.jobs and self.pipeline_detail.toPlainText():
                active = self._active_review_job()
                if active and active.id == job_id:
                    self.pipeline_detail.setPlainText(self._job_detail_text(active))

    def _browse_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "출력 폴더", self.output_dir_input.text())
        if folder:
            self.output_dir_input.setText(folder)

    def _browse_auth_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "YTMusic 인증 JSON", "", "JSON 파일 (*.json);;모든 파일 (*)")
        if path:
            self.auth_path_input.setText(path)

    def _browse_cookie_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "쿠키 파일", "", "쿠키 파일 (*.txt *.cookies);;모든 파일 (*)")
        if path:
            self.cookie_file_input.setText(path)

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


def _can_retry_job(job: DownloadJob) -> bool:
    if job.status == DownloadStatus.CANCELED:
        return True
    if job.status != DownloadStatus.FAILED:
        return False
    return job.error_category not in _NON_RETRYABLE_ERROR_CATEGORIES


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


def _pipeline_status(status: DownloadStatus) -> DownloadStatus:
    if status == DownloadStatus.TAGGING:
        return DownloadStatus.DOWNLOADING
    if status == DownloadStatus.CANCELED:
        return DownloadStatus.FAILED
    return status if status in _PIPELINE_STATUSES else DownloadStatus.PENDING


def _pipeline_status_label(status: DownloadStatus) -> str:
    if status == DownloadStatus.METADATA:
        return "분석 중"
    if status == DownloadStatus.DOWNLOADING:
        return "다운로드/태깅 중"
    if status == DownloadStatus.FAILED:
        return "실패"
    return _download_status_label(status)


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


def _candidate_badges(candidate: MetadataCandidate, current: TrackMetadata) -> str:
    badges: list[str] = []
    if candidate.metadata.title and current.title and text_similarity(candidate.metadata.title, current.title) >= 0.9:
        badges.append("제목 일치")
    if candidate.metadata.artist and current.artist and text_similarity(candidate.metadata.artist, current.artist) < 0.65:
        badges.append("아티스트 충돌")
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
        ("label", "레이블"),
        ("isrc", "ISRC"),
        ("cover_url", "커버 URL"),
    )
    return [
        (field, label, str(getattr(current, field) or ""), str(getattr(applied, field) or ""))
        for field, label in labels
    ]


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


def _is_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    query = parse_qs(parsed.query)
    if "youtube.com" in host or "youtu.be" in host:
        return bool(query.get("list")) or parsed.path.rstrip("/").endswith("/playlist")
    if "soundcloud.com" in host:
        return "/sets/" in parsed.path
    return False


def _is_liked_music_playlist(url: str) -> bool:
    return _youtube_playlist_id(url) == "LM"


def _is_youtube_music_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.casefold() == "music.youtube.com" and bool(_youtube_playlist_id(url))


def _youtube_playlist_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if "youtube.com" not in host and "youtu.be" not in host:
        return ""
    return (parse_qs(parsed.query).get("list") or [""])[0].strip()


def _youtube_data_api_playlist_id(playlist_id: str) -> str:
    if playlist_id.startswith("VL") and len(playlist_id) > 2:
        return playlist_id[2:]
    return playlist_id


def _ytmusic_track_url(video_id: str) -> str:
    return f"https://music.youtube.com/watch?v={video_id}"


def _is_incomplete_playlist_expansion(result: PlaylistExpansionResult) -> bool:
    if result.expected_count is None:
        return False
    return len(result.urls) + result.skipped_count < result.expected_count


def _is_youtube_playlist_url(url: str) -> bool:
    return bool(_youtube_playlist_id(url))


def _should_retry_with_ytmusicapi_playlist_expansion(url: str, result: PlaylistExpansionResult) -> bool:
    if not _is_youtube_playlist_url(url):
        return False
    if _is_incomplete_playlist_expansion(result):
        return True
    if len(result.urls) == 100 and result.expected_count in (None, 100):
        return _is_youtube_music_playlist_url(url) or _is_liked_music_playlist(url)
    return _is_liked_music_playlist(url) and not result.urls


def _should_try_ytmusicapi_playlist_expansion(url: str) -> bool:
    return _is_youtube_music_playlist_url(url) or _is_liked_music_playlist(url)


def _playlist_incomplete_message(url: str, result: PlaylistExpansionResult) -> str:
    expected = result.expected_count if result.expected_count is not None else "알 수 없음"
    extracted = len(result.urls) + result.skipped_count
    if _is_youtube_music_playlist_url(url):
        return (
            "YouTube Music 플레이리스트 전체를 가져오지 못했습니다. "
            f"확인된 항목 {expected}개 중 {extracted}개만 읽었습니다. "
            "Google 계정 연결 또는 쿠키 파일 세션을 확인한 뒤 다시 시도하세요."
        )
    return (
        "yt-dlp가 플레이리스트 전체를 가져오지 못했습니다. "
        f"확인된 항목 {expected}개 중 {extracted}개만 읽었습니다. "
        "YouTube playlist pagination 제한/변경으로 보이며, yt-dlp 업데이트 후 다시 시도하세요."
    )


def _playlist_expansion_failure_message(url: str, error: object, *, auth_diagnostic: str = "") -> str:
    message = str(error or "").strip() or "알 수 없는 오류"
    if _is_liked_music_playlist(url):
        parts = [
            f"{message}\n"
            "YouTube Music 좋아요 표시한 음악(LM)은 계정 접근이 필요합니다. "
            "설정에서 Google 계정을 연결하거나 최신 Netscape 형식 cookies.txt를 지정한 뒤 playlist URL을 다시 추가하세요."
        ]
        if auth_diagnostic:
            parts.append(auth_diagnostic)
        return "\n".join(parts)
    return message


def _single_video_url_from_playlist_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    query = parse_qs(parsed.query)
    video_id = (query.get("v") or [""])[0].strip()
    if not video_id and "youtu.be" in host:
        video_id = parsed.path.strip("/").split("/")[0]
    if not video_id or ("youtube.com" not in host and "youtu.be" not in host):
        return ""
    fallback_host = "music.youtube.com" if host == "music.youtube.com" else "www.youtube.com"
    return urlunparse((parsed.scheme or "https", fallback_host, "/watch", "", urlencode({"v": video_id}), ""))


def _dedupe_preserving_order(urls: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        deduped.append(url)
        seen.add(url)
    return deduped


def _extract_urls(value: str) -> list[str]:
    normalized = _ADJACENT_URL_SEPARATOR_PATTERN.sub(" ", value.strip())
    matches = _URL_PATTERN.findall(normalized)
    candidates = matches if matches else re.split(r"[\s,;]+", normalized)

    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = candidate.strip().strip("<>()[]{}\"'")
        url = url.rstrip(".,;")
        parsed = urlparse(url)
        if not url or parsed.scheme not in {"http", "https"} or url in seen:
            continue
        urls.append(url)
        seen.add(url)
    return urls


def _supported_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    supported: list[str] = []
    unsupported: list[str] = []
    for url in urls:
        if detect_source_platform(url) in {SourcePlatform.YOUTUBE, SourcePlatform.YOUTUBE_MUSIC, SourcePlatform.SOUNDCLOUD}:
            supported.append(url)
        else:
            unsupported.append(url)
    return supported, unsupported


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


def _settings_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _same_path(left: Path, right: Path) -> bool:
    return str(left.expanduser()).casefold().rstrip("\\/") == str(right.expanduser()).casefold().rstrip("\\/")


def _review_state_value(value: ReviewState | str) -> ReviewState:
    if isinstance(value, ReviewState):
        return value
    if isinstance(value, str) and value in ReviewState._value2member_map_:
        return ReviewState(value)
    return ReviewState.REVIEW_REQUIRED


def _event_type_for_log(message: str) -> str:
    if message.startswith("큐에 추가"):
        return "queued"
    if "메타데이터 자동 승인" in message:
        return "approved_auto"
    if "메타데이터 승인" in message:
        return "approved"
    if "검수 필요" in message:
        return "review_required"
    if message.startswith("완료:"):
        return "done"
    if message.startswith("실패:"):
        return "failed"
    if "취소" in message:
        return "canceled"
    if "재시도" in message:
        return "retry"
    return ""


def _job_store_path_for_settings(settings: QSettings) -> Path | None:
    file_name = settings.fileName()
    if file_name:
        return Path(file_name).with_suffix(".jobs.sqlite")
    return None
