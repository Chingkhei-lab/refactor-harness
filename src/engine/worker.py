"""Worker pieces. Phase 1, checkpoint 2: the fenced heartbeat thread."""

from __future__ import annotations

import threading

from . import db, jobs
from .config import Settings
from .jobs import Job, LeaseLost


class HeartbeatThread(threading.Thread):
    """Keeps one claimed job's lease alive until the handler finishes.

    Owns its own connection (never shared with the handler thread). Beats
    every lease/3 with a fenced update; if the update matches zero rows the
    lease is lost, so it sets `lease_lost` and exits. The handler thread must
    check that flag and must not write a result afterwards.
    """

    def __init__(self, settings: Settings, job: Job) -> None:
        if job.locked_by is None:
            raise ValueError("heartbeat requires a claimed (running) job")
        super().__init__(daemon=True, name=f"heartbeat-{job.id}")
        self._settings = settings
        self._job_id = job.id
        self._locked_by = job.locked_by
        self._attempts = job.attempts
        self._stop_event = threading.Event()
        self.lease_lost = False

    def stop(self) -> None:
        """Ask the thread to stop before its next beat."""
        self._stop_event.set()

    def run(self) -> None:
        conn = db.connect(self._settings)
        interval = self._settings.heartbeat_interval_seconds
        while not self._stop_event.wait(interval):
            try:
                jobs.heartbeat(
                    conn,
                    job_id=self._job_id,
                    locked_by=self._locked_by,
                    attempts=self._attempts,
                    lease_seconds=self._settings.lease_seconds,
                )
            except LeaseLost:
                self.lease_lost = True
                return
