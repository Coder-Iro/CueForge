"""PySide6 desktop interface."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
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
from ytdj.metadata import AcoustIDConfig, AcoustIDProvider, MetadataResolver
from ytdj.metadata.fingerprint import FingerprintError, FingerprintUnavailable
from ytdj.metadata.normalize import merge_metadata
from ytdj.models import DownloadJob, DownloadStatus, MetadataCandidate, ReviewState, TagWriteResult, TrackMetadata
from ytdj.sources import SourcePlatform, detect_source_platform, trust_policy_for
from ytdj.tags import RekordboxTagWriter, safe_track_filename

DownloaderFactory = Callable[[DownloadConfig, Any], YTDLPDownloader]
ResolverFactory = Callable[[], MetadataResolver]
AcoustIDProviderFactory = Callable[[AcoustIDConfig], Any]
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
        approved_metadata: TrackMetadata | None = None,
        downloader_factory: DownloaderFactory | None = None,
        resolver_factory: ResolverFactory | None = None,
        acoustid_provider_factory: AcoustIDProviderFactory | None = None,
        tag_writer_factory: TagWriterFactory | None = None,
    ) -> None:
        super().__init__()
        self.job = job
        self.cookie_browser = cookie_browser
        self.ytmusic_auth_path = ytmusic_auth_path
        self.ffmpeg_location = ffmpeg_location
        self.acoustid_config = acoustid_config or AcoustIDConfig()
        self.audio_recognition_enabled = audio_recognition_enabled
        self.approved_metadata = approved_metadata
        self._downloader_factory = downloader_factory or _create_downloader
        self._resolver_factory = resolver_factory or MetadataResolver
        self._acoustid_provider_factory = acoustid_provider_factory or AcoustIDProvider
        self._tag_writer_factory = tag_writer_factory or RekordboxTagWriter

    def run(self) -> None:
        try:
            downloader = self._new_downloader(self.job.output_dir)
            metadata = self.approved_metadata
            downloaded_path = self.job.downloaded_path
            if metadata is None:
                metadata, state, candidates, platform = self._resolve_metadata(downloader)
                if state != ReviewState.AUTO_APPROVED:
                    metadata, state, candidates, downloaded_path = self._try_audio_recognition(
                        metadata=metadata,
                        state=state,
                        candidates=candidates,
                        platform=platform,
                    )
                self.metadata_ready.emit(self.job.id, metadata, state, candidates)
                if state != ReviewState.AUTO_APPROVED:
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

    def _resolve_metadata(self, downloader: YTDLPDownloader) -> tuple[TrackMetadata, ReviewState, list[MetadataCandidate], SourcePlatform]:
        self.progress_changed.emit(self.job.id, 0.0, DownloadStatus.METADATA.value)
        info = downloader.fetch_info(self.job.url)
        resolution = self._resolver_factory().resolve(
            url=self.job.url,
            info=info,
            ytmusic_auth_path=self.ytmusic_auth_path,
            log=lambda message: self.log_message.emit(self.job.id, message),
        )
        self.log_message.emit(
            self.job.id,
            f"source: {resolution.platform.display_name}; {trust_policy_for(resolution.platform).note}",
        )
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
            config=self.acoustid_config,
        )
        if reason:
            self.log_message.emit(self.job.id, f"audio recognition skipped: {reason}")
            return metadata, state, candidates, None

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
        best = fingerprint_candidates[0]
        self.log_message.emit(self.job.id, f"AcoustID best match: {best.metadata.artist} - {best.metadata.title} ({best.score:.2f})")
        return merged_metadata, merged_state, merged_candidates, result.path

    def _on_progress(self, progress: DownloadProgress) -> None:
        percent = progress.percent if progress.percent is not None else 0.0
        status = DownloadStatus.DOWNLOADING.value if progress.status == "downloading" else progress.status
        self.progress_changed.emit(self.job.id, percent, status)


class MainWindow(QMainWindow):
    COLUMNS = ("Status", "Progress", "Source", "URL", "Title", "Artist", "Output")

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("YT-DJ")
        self.resize(1120, 720)
        self.jobs: dict[str, DownloadJob] = {}
        self.row_job_ids: list[str] = []
        self.worker: JobWorker | None = None

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("YouTube / YouTube Music / SoundCloud URL")
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

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)

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
        self.candidate_label = QLabel("")

        self._build_ui()

    def _build_ui(self) -> None:
        toolbar = QToolBar("Actions")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        add_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "Add", self)
        add_action.triggered.connect(self._add_url)
        start_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "Start Queue", self)
        start_action.triggered.connect(self._start_next)
        remove_action = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon), "Remove", self)
        remove_action.triggered.connect(self._remove_selected)
        toolbar.addAction(add_action)
        toolbar.addAction(start_action)
        toolbar.addAction(remove_action)

        tabs = QTabWidget()
        tabs.addTab(self._queue_tab(), "Queue")
        tabs.addTab(self._review_tab(), "Review")
        tabs.addTab(self._settings_tab(), "Settings")
        self.setCentralWidget(tabs)

    def _queue_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        url_row = QGridLayout()
        url_row.addWidget(QLabel("URL"), 0, 0)
        url_row.addWidget(self.url_input, 0, 1)
        add_button = QPushButton("Add")
        add_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        add_button.clicked.connect(self._add_url)
        url_row.addWidget(add_button, 0, 2)
        layout.addLayout(url_row)

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
        layout.addWidget(self.review_state_label)
        layout.addWidget(self.candidate_label)

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
        layout.addLayout(form)

        approve_button = QPushButton("Approve && Download")
        approve_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        approve_button.clicked.connect(self._approve_selected)
        layout.addWidget(approve_button)
        layout.addStretch()
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
        recognition_form.addRow("AcoustID client key", self.acoustid_key_input)
        recognition_form.addRow("fpcalc path", self._path_row(self.fpcalc_path_input, self._browse_fpcalc))

        layout.addWidget(paths_group)
        layout.addWidget(recognition_group)
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

    def _add_url(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            return
        output_dir = Path(self.output_dir_input.text().strip() or "downloads")
        job = DownloadJob(url=url, output_dir=output_dir)
        self.jobs[job.id] = job
        self.row_job_ids.append(job.id)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col in range(len(self.COLUMNS)):
            self.table.setItem(row, col, QTableWidgetItem(""))
        self._update_row(job)
        self.url_input.clear()
        self._append_log(job.id, "queued")

    def _start_next(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        for job_id in self.row_job_ids:
            job = self.jobs[job_id]
            if job.status == DownloadStatus.PENDING:
                self._run_worker(job)
                return

    def _run_worker(self, job: DownloadJob, approved_metadata: TrackMetadata | None = None) -> None:
        job.status = DownloadStatus.DOWNLOADING if approved_metadata else DownloadStatus.METADATA
        self._update_row(job)
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
            approved_metadata=approved_metadata,
        )
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.metadata_ready.connect(self._on_metadata_ready)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.job_failed.connect(self._on_job_failed)
        self.worker.log_message.connect(self._append_log)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

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
        state: ReviewState,
        candidates: list[MetadataCandidate],
    ) -> None:
        job = self.jobs[job_id]
        job.selected_metadata = metadata
        job.candidates = candidates
        job.status = DownloadStatus.DOWNLOADING if state == ReviewState.AUTO_APPROVED else DownloadStatus.REVIEW_REQUIRED
        self._update_row(job)
        self._load_job_for_review(job)
        if state == ReviewState.AUTO_APPROVED:
            self._append_log(job_id, "metadata auto-approved")
        else:
            self._append_log(job_id, f"metadata requires review: {state.value}")

    def _on_job_done(self, job_id: str, final_path: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.DONE
        job.progress = 100.0
        job.final_path = Path(final_path)
        self._update_row(job)
        self._append_log(job_id, f"done: {final_path}")

    def _on_job_failed(self, job_id: str, error: str) -> None:
        job = self.jobs[job_id]
        job.status = DownloadStatus.FAILED
        job.error = error
        self._update_row(job)
        self._append_log(job_id, f"failed: {error}")

    def _worker_finished(self) -> None:
        self.worker = None
        self._start_next()

    def _approve_selected(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Queue running", "Wait for the current job to finish before approving another track.")
            return
        job = self._selected_job()
        if not job:
            return
        metadata = self._metadata_from_review_fields(job.selected_metadata)
        job.selected_metadata = metadata
        self._run_worker(job, approved_metadata=metadata)

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

    def _load_selected_job(self) -> None:
        job = self._selected_job()
        if job:
            self._load_job_for_review(job)

    def _load_job_for_review(self, job: DownloadJob) -> None:
        metadata = job.selected_metadata
        platform = detect_source_platform(job.url)
        self.review_state_label.setText(f"{job.status.value}: {platform.display_name}: {job.url}")
        if job.candidates:
            best = job.candidates[0]
            trust_note = ""
            if best.provider == "soundcloud" and best.raw.get("trusted_native"):
                trust_note = " - SoundCloud metadata trusted"
            self.candidate_label.setText(
                f"Best candidate: {best.provider} {best.score:.2f} ({', '.join(best.matched_fields)}){trust_note}"
            )
        else:
            self.candidate_label.setText("No external candidate")
        for key, field in self.review_fields.items():
            field.setText(str(getattr(metadata, key) or ""))

    def _metadata_from_review_fields(self, base: TrackMetadata) -> TrackMetadata:
        return TrackMetadata(
            title=self.review_fields["title"].text().strip(),
            artist=self.review_fields["artist"].text().strip(),
            album=self.review_fields["album"].text().strip(),
            album_artist=self.review_fields["album_artist"].text().strip(),
            genre=self.review_fields["genre"].text().strip(),
            release_date=self.review_fields["release_date"].text().strip(),
            label=self.review_fields["label"].text().strip(),
            isrc=self.review_fields["isrc"].text().strip(),
            cover_url=self.review_fields["cover_url"].text().strip(),
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
    config: AcoustIDConfig,
) -> str:
    if state == ReviewState.AUTO_APPROVED:
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
    if config.fpcalc_path:
        return config.fpcalc_path.exists()
    return shutil.which("fpcalc") is not None


def _merge_audio_recognition_candidates(
    *,
    metadata: TrackMetadata,
    state: ReviewState,
    candidates: list[MetadataCandidate],
    fingerprint_candidates: list[MetadataCandidate],
) -> tuple[TrackMetadata, ReviewState, list[MetadataCandidate]]:
    combined = sorted([*candidates, *fingerprint_candidates], key=lambda candidate: candidate.score, reverse=True)
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
