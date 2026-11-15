"""Worker loop, handler registry, and the long-lived fenced heartbeat thread."""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import random
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from . import db, jobs, reconciler
from .config import Settings, load_settings
from .jobs import Job, LeaseLost

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], "HandlerContext"], dict[str, Any] | None]
AfterCompleteCallback = Callable[[psycopg.Connection[db.Row]], None]


@dataclass(frozen=True)
class HandlerContext:
    """What a handler sees: identity, attempt, and a cancellation signal.

    `after_complete(callback)` registers work the worker runs inside the same
    transaction as the job's `complete` — that is how a workflow step commits
    its output atomically with the fenced completion (ADR 002). Callbacks
    receive the worker's connection and must not commit or open their own
    transaction; any callback exception rolls the whole transaction back.
    """

    job_id: UUID
    attempt: int
    lease_lost: threading.Event
    _after_complete_callbacks: list[AfterCompleteCallback] = field(
        default_factory=list, repr=False
    )

    def after_complete(self, callback: AfterCompleteCallback) -> None:
        self._after_complete_callbacks.append(callback)

    @property
    def after_complete_callbacks(self) -> tuple[AfterCompleteCallback, ...]:
        return tuple(self._after_complete_callbacks)


def _make_worker_id() -> str:
    return f"{socket.gethostname()}#{os.getpid()}#{os.urandom(4).hex()}"


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _load_handlers(spec: str) -> dict[str, Handler]:
    """Load a HANDLERS dict from a dotted module or a .py file path."""
    if spec.endswith(".py"):
        module_spec = importlib.util.spec_from_file_location("engine_handlers", spec)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"cannot load handlers from {spec!r}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(spec)
    handlers = getattr(module, "HANDLERS", None)
    if not isinstance(handlers, dict):
        raise TypeError(f"{spec!r} must define a HANDLERS dict")
    return {str(key): value for key, value in handlers.items()}


class HeartbeatThread(threading.Thread):
    """One long-lived thread per worker; beats the current job's token.

    Owns a single connection for its whole life (a new connection per job is
    too slow at 10k jobs). Beats every lease/3 — beats land at L/3, 2L/3 and
    L, so two missed beats are tolerated.

    Transient database errors (OperationalError) are retried on the next
    beat after a reconnect: they are not lease loss. The thread tracks
    `last_successful_beat + lease` on the monotonic clock and only sets the
    lease-lost event when that deadline passes, because by then the reaper
    may legitimately have taken the job.
    """

    def __init__(self, settings: Settings, job: Job | None = None) -> None:
        super().__init__(daemon=True, name="engine-heartbeat")
        self._settings = settings
        self._lock = threading.Lock()
        self._token: tuple[UUID, str, int] | None = None
        self._deadline = 0.0
        self._lease_lost_event: threading.Event = threading.Event()
        self._stop_event = threading.Event()
        if job is not None:
            self.set_token(job)

    def set_token(
        self, job: Job, lease_lost_event: threading.Event | None = None
    ) -> None:
        if job.locked_by is None:
            raise ValueError("heartbeat requires a claimed (running) job")
        event = lease_lost_event if lease_lost_event is not None else threading.Event()
        with self._lock:
            self._token = (job.id, job.locked_by, job.attempts)
            self._lease_lost_event = event
            self._deadline = time.monotonic() + self._settings.lease_seconds

    def clear_token(self) -> None:
        """Stop beating the current job; waits out any in-flight beat.

        Taking the lock while the beat executes means that after this returns
        no heartbeat can hit the row the worker is about to complete or fail.
        """
        with self._lock:
            self._token = None
            self._deadline = 0.0

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost_event.is_set()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        conn = db.connect(self._settings, application_name="engine-heartbeat")
        interval = self._settings.heartbeat_interval_seconds
        lease = self._settings.lease_seconds
        while not self._stop_event.wait(interval):
            with self._lock:
                token = self._token
                if token is None:
                    continue
                if time.monotonic() >= self._deadline:
                    self._lease_lost_event.set()
                    self._token = None
                    self._deadline = 0.0
                    continue
                job_id, locked_by, attempts = token
                try:
                    jobs.heartbeat(
                        conn,
                        job_id=job_id,
                        locked_by=locked_by,
                        attempts=attempts,
                        lease_seconds=lease,
                    )
                except LeaseLost:
                    self._lease_lost_event.set()
                    self._token = None
                    self._deadline = 0.0
                except psycopg.OperationalError:
                    # transient database blip: not lease loss; reconnect and
                    # let the deadline decide if the lease is really gone.
                    try:
                        conn.close()
                    except psycopg.Error:
                        pass
                    conn = db.connect(
                        self._settings, application_name="engine-heartbeat"
                    )
                else:
                    self._deadline = time.monotonic() + lease


class Worker:
    """Claim loop: reap when due, claim, run handler, finish. Never crashes.

    Order per iteration: reap if due, claim with types=registry keys, idle
    wait on the stop event with jitter when nothing came back. SIGTERM sets
    the stop event only; the current job finishes before the worker exits 0.
    """

    def __init__(
        self,
        settings: Settings,
        handlers: dict[str, Handler],
        worker_id: str | None = None,
        application_name: str = "engine-worker",
    ) -> None:
        self._settings = settings
        self._handlers = handlers
        self._worker_id = worker_id or _make_worker_id()
        self._application_name = application_name
        self._stop_event = threading.Event()
        self._heartbeat = HeartbeatThread(settings)
        self._conn: psycopg.Connection[db.Row] | None = None
        self._last_reap = 0.0
        self._last_reconcile = 0.0

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def request_stop(self) -> None:
        self._stop_event.set()

    def _connection(self) -> psycopg.Connection[db.Row]:
        if self._conn is None or self._conn.closed:
            self._conn = db.connect(
                self._settings, application_name=self._application_name
            )
        return self._conn

    def _reconnect(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
        self._conn = None

    def _idle_seconds(self) -> float:
        return random.uniform(
            self._settings.idle_sleep_min_seconds,
            self._settings.idle_sleep_max_seconds,
        )

    def _reap_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_reap < self._settings.reaper_interval_seconds:
            return
        self._last_reap = now
        reaped = jobs.reaper(self._connection(), batch=self._settings.reaper_batch)
        if reaped:
            logger.info("reaped %d expired jobs", len(reaped))

    def _reconcile_if_due(self) -> None:
        """Repair loop next to the reaper: workflow rules on a cadence.

        reconciler.reconcile catches DeadlockDetected per rule (skip; the next
        cycle retries); OperationalError propagates to run()'s reconnect path.
        """
        now = time.monotonic()
        if now - self._last_reconcile < self._settings.reconciler_interval_seconds:
            return
        self._last_reconcile = now
        try:
            counts = reconciler.reconcile(
                self._connection(), settings=self._settings
            )
        except psycopg.errors.DeadlockDetected:
            # the rules catch their own deadlocks; this guards one raised
            # between rules. The next cycle retries.
            logger.warning("reconcile deadlocked; retrying next cycle")
            return
        if any(counts.values()):
            logger.info("reconciler actions: %s", counts)

    def _try_claim(self) -> Job | None:
        return jobs.claim(
            self._connection(),
            locked_by=self._worker_id,
            lease_seconds=self._settings.lease_seconds,
            types=sorted(self._handlers),
        )

    def _finish_complete(
        self,
        job: Job,
        result: dict[str, Any] | None,
        callbacks: tuple[AfterCompleteCallback, ...] = (),
    ) -> None:
        try:
            if callbacks:
                # The advance transaction: the fenced complete, then the
                # registered callbacks (a workflow step's output write and
                # next-job enqueue), committing atomically. LeaseLost (zero
                # fenced rows) or any callback exception rolls everything
                # back, so a stale attempt's output dies with its lease.
                with self._connection().transaction():
                    jobs.complete(
                        self._connection(),
                        job_id=job.id,
                        locked_by=job.locked_by or "",
                        attempts=job.attempts,
                        result=result,
                    )
                    for callback in callbacks:
                        callback(self._connection())
            else:
                jobs.complete(
                    self._connection(),
                    job_id=job.id,
                    locked_by=job.locked_by or "",
                    attempts=job.attempts,
                    result=result,
                )
        except LeaseLost:
            logger.warning(
                "job %s attempt %s: lease lost before complete; result dropped",
                job.id,
                job.attempts,
            )
        except psycopg.DataError as exc:
            # Postgres jsonb rejects values json.dumps accepts (NaN, Infinity,
            # \u0000 in strings, over-deep nesting). That must fail the JOB,
            # not crash the worker: the transaction rolled back, so the row
            # and token are untouched and fail() carries the same token.
            logger.warning(
                "job %s attempt %s: database rejected the result: %s",
                job.id,
                job.attempts,
                _truncate(repr(exc)),
            )
            self._finish_fail(job, _truncate(f"invalid result: {exc!r}"))
        except psycopg.OperationalError as exc:
            logger.warning(
                "job %s attempt %s: database error during complete: %s",
                job.id,
                job.attempts,
                _truncate(repr(exc)),
            )
            self._reconnect()
        except Exception as exc:  # a callback failed; the complete rolled back
            logger.warning(
                "job %s attempt %s: advance transaction failed: %s",
                job.id,
                job.attempts,
                _truncate(repr(exc)),
            )
            self._finish_fail(job, _truncate(f"workflow advance failed: {exc!r}"))

    def _finish_fail(
        self, job: Job, last_error: str, force_dead: bool = False
    ) -> None:
        try:
            jobs.fail(
                self._connection(),
                job_id=job.id,
                locked_by=job.locked_by or "",
                attempts=job.attempts,
                backoff_seconds=jobs.jittered_backoff_seconds(
                    job.attempts, self._settings
                ),
                last_error=last_error,
                force_dead=force_dead,
            )
        except LeaseLost:
            logger.warning(
                "job %s attempt %s: lease lost before fail; error dropped",
                job.id,
                job.attempts,
            )
        except psycopg.DataError as exc:
            # Same class of rejection, on the fail path: e.g. a last_error
            # containing a NUL. The job stays 'running' with an expired-lease
            # future; the reaper cycles it (claim increments attempts) until
            # it goes dead at max_attempts. Never crash the worker over it.
            logger.warning(
                "job %s attempt %s: database rejected the failure record: %s;"
                " leaving the job to the reaper",
                job.id,
                job.attempts,
                _truncate(repr(exc)),
            )
        except psycopg.OperationalError as exc:
            logger.warning(
                "job %s attempt %s: database error during fail: %s",
                job.id,
                job.attempts,
                _truncate(repr(exc)),
            )
            self._reconnect()

    def _run_job(self, job: Job) -> None:
        """Per-job section: claim happened; run handler, then complete/fail."""
        lease_lost_event = threading.Event()
        ctx = HandlerContext(
            job_id=job.id, attempt=job.attempts, lease_lost=lease_lost_event
        )
        handler = self._handlers.get(job.type)
        if handler is None:
            self._finish_fail(
                job, _truncate(f"no handler registered for type {job.type!r}")
            )
            return

        self._heartbeat.set_token(job, lease_lost_event)
        failure: str | None = None
        force_dead = False
        result: dict[str, Any] | None = None
        try:
            result = handler(job.payload, ctx)
        except jobs.NonRetryableError as exc:
            # the handler declares the job unsalvageable: dead this attempt
            failure = _truncate(f"non-retryable: {exc}")
            force_dead = True
        except Exception as exc:  # any exception is a failure
            failure = _truncate(repr(exc))
        finally:
            # Stop beating this job and wait out any in-flight beat BEFORE
            # complete/fail touch the row: a late heartbeat would hit the
            # cleared lease columns and raise a spurious LeaseLost.
            self._heartbeat.clear_token()

        if failure is not None:
            logger.warning(
                "job %s attempt %s failed: %s", job.id, job.attempts, failure
            )
            self._finish_fail(job, failure, force_dead=force_dead)
            return

        if lease_lost_event.is_set():
            logger.warning(
                "job %s attempt %s lost its lease; not writing a result",
                job.id,
                job.attempts,
            )
            return

        try:
            # allow_nan=False matches what jsonb accepts: NaN/Infinity are not
            # valid JSON and Postgres rejects them; without this, the rejection
            # would only surface later as a DataError from the database.
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            self._finish_fail(
                job, _truncate(f"non-serializable result: {exc!r}")
            )
            return

        self._finish_complete(job, result, ctx.after_complete_callbacks)

    def run(self) -> int:
        logger.info("worker %s starting", self._worker_id)
        self._heartbeat.start()
        try:
            while not self._stop_event.is_set():
                try:
                    self._reap_if_due()
                    self._reconcile_if_due()
                    job = self._try_claim()
                except psycopg.OperationalError as exc:
                    logger.warning(
                        "database unavailable: %s; reconnecting",
                        _truncate(repr(exc)),
                    )
                    self._reconnect()
                    self._stop_event.wait(self._idle_seconds())
                    continue
                if job is None:
                    self._stop_event.wait(self._idle_seconds())
                    continue
                self._run_job(job)
        finally:
            self._heartbeat.stop()
            self._heartbeat.join(timeout=5)
        logger.info("worker %s stopped", self._worker_id)
        return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings()
    spec = os.environ.get("ENGINE_HANDLERS_MODULE", "")
    if not spec:
        raise SystemExit("ENGINE_HANDLERS_MODULE is required (module or .py path)")
    handlers = _load_handlers(spec)
    workflows_spec = os.environ.get("ENGINE_WORKFLOWS_MODULE", "")
    if workflows_spec:
        from .workflows import load_workflows, make_workflow_handlers

        workflow_defs = load_workflows(workflows_spec)
        if "workflow_step" in handlers:
            raise SystemExit(
                "ENGINE_HANDLERS_MODULE must not define 'workflow_step'; it is "
                "provided by ENGINE_WORKFLOWS_MODULE"
            )
        handlers.update(make_workflow_handlers(workflow_defs, settings=settings))
    worker = Worker(settings, handlers)

    def _on_signal(signum: int, frame: Any) -> None:
        logger.info("signal %s received: finishing current job", signum)
        worker.request_stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    return worker.run()


if __name__ == "__main__":
    raise SystemExit(main())
