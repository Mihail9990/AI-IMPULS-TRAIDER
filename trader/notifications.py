"""Read-only background reconciliation for reports.

The worker owns a separate CapitalClient/session and never mutates CycleState or submits broker
orders.  Durable jobs live in CycleState; only the main Bot thread applies completed results.
"""
from __future__ import annotations

from collections import deque
import logging
import threading
import time
from typing import Callable

from .capital import CapitalClient
from .config import Settings
from .events import find_close_event


LOG = logging.getLogger(__name__)


class NotificationHistoryWorker:
    def __init__(
        self, cfg: Settings, *, client_factory: Callable[[Settings], CapitalClient] = CapitalClient,
        retry_seconds: float = 5.0,
    ) -> None:
        self.cfg = cfg
        self.client_factory = client_factory
        self.retry_seconds = retry_seconds
        self._jobs: deque[dict] = deque()
        self._results: deque[dict] = deque()
        self._keys: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="notification-history", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def submit(self, job: dict) -> None:
        key = str(job.get("key", ""))
        if not key:
            return
        with self._lock:
            if key in self._keys:
                return
            self._keys.add(key)
            self._jobs.append(dict(job))
        self._wake.set()

    def results(self) -> list[dict]:
        with self._lock:
            values = list(self._results)
            self._results.clear()
        return values

    def _run(self) -> None:
        client = self.client_factory(self.cfg)
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            with self._lock:
                job = self._jobs.popleft() if self._jobs else None
            if job is None:
                continue
            key = str(job["key"])
            try:
                result = self._resolve(client, job)
            except Exception as exc:
                LOG.warning("Notification history lookup delayed key=%s: %s", key, exc)
                result = None
            if result is None:
                if not self._stop.wait(self.retry_seconds):
                    with self._lock:
                        self._jobs.append(job)
                    self._wake.set()
                continue
            with self._lock:
                self._keys.discard(key)
                self._results.append({"key": key, "result": result})

    @staticmethod
    def _resolve(client: CapitalClient, job: dict) -> dict | None:
        target = job["waiting"]
        deal_id = str(target["deal_id"])
        activity = client.activity(deal_id)
        if not activity:
            activity = client.activity()
        for source in ("SL", "TP"):
            event = find_close_event(activity, deal_id, source)
            if event is not None and event.level is not None:
                return {"source": source, "fill": str(event.level)}
        return None


def split_report(text: str, limit: int = 3500) -> list[str]:
    """Return stable chunks so delivered part numbers survive process restarts."""
    if len(text) <= limit:
        return [text]
    chunks, remaining = [], text
    while remaining:
        cut = min(limit, len(remaining))
        if cut < len(remaining):
            newline = remaining.rfind("\n", 0, cut)
            if newline > limit // 2:
                cut = newline + 1
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    total = len(chunks)
    return [f"Часть {index}/{total}\n{chunk}" for index, chunk in enumerate(chunks, 1)]
