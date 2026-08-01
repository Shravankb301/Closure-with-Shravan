from __future__ import annotations

import logging
import os
import signal
from threading import Event, Thread

from evidence_delta.database import Database
from evidence_delta.worker import RecomputeWorker

LOGGER = logging.getLogger("evidence_delta.worker")


class WorkerLoop:
    """Small process/thread runner around the durable PostgreSQL queue."""

    def __init__(self, worker: RecomputeWorker, poll_seconds: float = 0.25) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.worker = worker
        self.poll_seconds = poll_seconds
        self.stop_event = Event()
        self.thread: Thread | None = None

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError("Worker loop already started")
        self.thread = Thread(target=self.run, name="evidence-delta-worker", daemon=True)
        self.thread.start()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = self.worker.run_once()
            except Exception as error:
                # Exception messages can contain evidence. Operational logs get
                # the class only, matching persisted job-error policy.
                LOGGER.warning("worker_iteration_failed error_class=%s", type(error).__name__)
                self.stop_event.wait(self.poll_seconds)
                continue
            if not result.claimed:
                self.stop_event.wait(self.poll_seconds)

    def stop(self, timeout: float = 10) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    database = Database(os.environ["DATABASE_URL"])
    worker = RecomputeWorker(database)
    runtime = WorkerLoop(worker, poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "0.25")))

    def stop(_signal_number, _frame) -> None:
        runtime.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runtime.run()
