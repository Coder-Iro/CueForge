import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cueforge.gui.scheduler import JobScheduler
from cueforge.models import DownloadJob, DownloadStatus, SchedulerLimits


class FakeWorker:
    def __init__(self, job: DownloadJob, stage: str, started: list[tuple[str, str]]) -> None:
        self.job = job
        self.stage = stage
        self.started = started
        self.finished = _Signal()
        self.canceled = False

    def start(self) -> None:
        self.started.append((self.job.id, self.stage))

    def cancel(self) -> None:
        self.canceled = True

    def deleteLater(self) -> None:
        return None


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


def test_scheduler_respects_metadata_parallel_limit_and_pumps_next(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    started: list[tuple[str, str]] = []

    def factory(job: DownloadJob, stage: str, tag_semaphore: threading.Semaphore) -> FakeWorker:
        return FakeWorker(job, stage, started)

    scheduler = JobScheduler(worker_factory=factory, limits=SchedulerLimits(metadata=2, download=1, tagging=1))
    jobs = [DownloadJob(url=f"https://youtu.be/{index}", output_dir=tmp_path) for index in range(3)]

    scheduler.enqueue_analysis(jobs)

    assert scheduler.active_count("metadata") == 2
    assert scheduler.queued_count("metadata") == 1
    assert [stage for _job_id, stage in started] == ["metadata", "metadata"]

    scheduler._worker_finished(jobs[0].id)

    assert scheduler.active_count("metadata") == 2
    assert scheduler.queued_count("metadata") == 0
    assert started[-1] == (jobs[2].id, "metadata")
    app.processEvents()


def test_scheduler_moves_auto_approved_analysis_to_download_queue(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    started: list[tuple[str, str]] = []

    def factory(job: DownloadJob, stage: str, tag_semaphore: threading.Semaphore) -> FakeWorker:
        if stage == "metadata":
            job.status = DownloadStatus.APPROVED
        return FakeWorker(job, stage, started)

    scheduler = JobScheduler(worker_factory=factory, limits=SchedulerLimits(metadata=1, download=1, tagging=1))
    job = DownloadJob(url="https://youtu.be/abc", output_dir=tmp_path)

    scheduler.enqueue_analysis([job])
    scheduler._worker_finished(job.id)

    assert started == [(job.id, "metadata"), (job.id, "download")]
    app.processEvents()
