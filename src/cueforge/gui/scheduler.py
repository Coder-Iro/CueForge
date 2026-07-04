"""Qt job scheduler for bounded parallel CueForge work."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, QThread, Signal

from cueforge.models import DownloadJob, DownloadStatus, SchedulerLimits

WorkerFactory = Callable[[DownloadJob, str, threading.Semaphore], QThread]


class JobScheduler(QObject):
    job_started = Signal(str, str)
    idle = Signal()

    def __init__(
        self,
        *,
        worker_factory: WorkerFactory,
        limits: SchedulerLimits | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker_factory = worker_factory
        self._limits = (limits or SchedulerLimits()).normalized()
        self._queues: dict[str, deque[DownloadJob]] = {
            "metadata": deque(),
            "download": deque(),
        }
        self._active: dict[str, tuple[str, QThread, DownloadJob]] = {}
        self._tag_semaphore = threading.Semaphore(self._limits.tagging)

    @property
    def limits(self) -> SchedulerLimits:
        return self._limits

    @property
    def tag_semaphore(self) -> threading.Semaphore:
        return self._tag_semaphore

    def set_limits(self, limits: SchedulerLimits) -> None:
        normalized = limits.normalized()
        tagging_changed = normalized.tagging != self._limits.tagging
        self._limits = normalized
        if tagging_changed:
            self._tag_semaphore = threading.Semaphore(self._limits.tagging)
        self._pump()

    def enqueue_analysis(self, jobs: Iterable[DownloadJob]) -> None:
        self._enqueue("metadata", jobs)

    def enqueue_downloads(self, jobs: Iterable[DownloadJob], *, priority: bool = False) -> None:
        self._enqueue("download", jobs, priority=priority)

    def remove_queued_job(self, job_id: str, *, stage: str | None = None) -> bool:
        return self._remove_queued_job(job_id, stage=stage)

    def cancel_all(self) -> None:
        for queue in self._queues.values():
            queue.clear()
        for _stage, worker, _job in list(self._active.values()):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
            else:
                worker.requestInterruption()

    def is_running(self) -> bool:
        return bool(self._active or any(self._queues.values()))

    def active_count(self, stage: str | None = None) -> int:
        if stage is None:
            return len(self._active)
        return sum(1 for active_stage, _worker, _job in self._active.values() if active_stage == stage)

    def queued_count(self, stage: str | None = None) -> int:
        if stage is None:
            return sum(len(queue) for queue in self._queues.values())
        return len(self._queues.get(stage, ()))

    def _enqueue(self, stage: str, jobs: Iterable[DownloadJob], *, priority: bool = False) -> None:
        queued_ids = {job.id for queue in self._queues.values() for job in queue}
        active_ids = set(self._active)
        ready: list[DownloadJob] = []
        for job in jobs:
            if job.id in active_ids:
                continue
            if job.id in queued_ids:
                if not priority:
                    continue
                self._remove_queued_job(job.id)
                queued_ids.discard(job.id)
            ready.append(job)
            queued_ids.add(job.id)
        if priority:
            for job in reversed(ready):
                self._queues[stage].appendleft(job)
        else:
            self._queues[stage].extend(ready)
        self._pump()

    def _remove_queued_job(self, job_id: str, *, stage: str | None = None) -> bool:
        if stage is None:
            queues = list(self._queues.values())
        else:
            queue = self._queues.get(stage)
            if queue is None:
                return False
            queues = [queue]
        removed = False
        for queue in queues:
            if not any(job.id == job_id for job in queue):
                continue
            retained = deque(job for job in queue if job.id != job_id)
            removed = len(retained) != len(queue) or removed
            queue.clear()
            queue.extend(retained)
        return removed

    def _pump(self) -> None:
        self._pump_stage("metadata", self._limits.metadata)
        self._pump_stage("download", self._limits.download)
        if not self.is_running():
            self.idle.emit()

    def _pump_stage(self, stage: str, limit: int) -> None:
        queue = self._queues[stage]
        while queue and self.active_count(stage) < limit:
            job = queue.popleft()
            worker = self._worker_factory(job, stage, self._tag_semaphore)
            self._active[job.id] = (stage, worker, job)
            worker.finished.connect(lambda job_id=job.id: self._worker_finished(job_id))
            self.job_started.emit(job.id, stage)
            worker.start()

    def _worker_finished(self, job_id: str) -> None:
        item = self._active.pop(job_id, None)
        if item:
            stage, worker, job = item
            worker.deleteLater()
            if stage == "metadata" and job.status == DownloadStatus.APPROVED:
                self._queues["download"].append(job)
        self._pump()
