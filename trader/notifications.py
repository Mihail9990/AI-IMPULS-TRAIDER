"""Read-only background reconciliation for reports.

The worker owns a separate CapitalClient/session and never mutates CycleState or submits broker
orders.  Durable jobs live in CycleState; only the main Bot thread applies completed results.
"""
from __future__ import annotations

from collections import deque
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .capital import CapitalClient
from .config import Settings
from .events import find_close_event


LOG = logging.getLogger(__name__)
SEARCH_TIME_VERSION = 2


def migrate_notification_jobs(jobs: list[dict], *, now: float | None = None) -> bool:
    """Add durable search bounds to pre-range notification jobs in the main state owner.

    Legacy jobs did not contain time metadata.  A timestamp embedded in their immutable attempt
    snapshot is preferred.  Otherwise a frozen last-24-hours fallback is recorded explicitly as
    uncertain; migration time is never represented as the original attempt time.
    """
    current = time.time() if now is None else now
    changed = False

    def timestamp(value, *, broker_utc: bool = False) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            # Capital.com's dateUTC/createdDateUTC are UTC by contract even when their value has
            # no suffix.  A generic naive timestamp has no trustworthy origin: do not let the
            # phone's local timezone silently redefine the historical instant.
            if not broker_utc:
                return None
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    for job in jobs:
        has_bounds = (
            job.get("search_from_epoch") is not None
            and job.get("search_to_epoch") is not None
        )
        source = str(job.get("search_range_source", ""))
        version = int(job.get("search_time_version", 0) or 0)
        # Explicit ranges and frozen fallback ranges were not derived by parsing a broker time,
        # so they must not be shifted.  Version-1 attempt_snapshot ranges are reparsed from their
        # original immutable snapshot below.
        if has_bounds and (source != "attempt_snapshot" or version >= SEARCH_TIME_VERSION):
            continue
        candidates = [
            timestamp(job.get(name))
            for name in ("attempt_started_at", "attempt_epoch", "event_epoch")
        ]
        for snapshot_name in ("closed", "waiting"):
            snapshot = job.get(snapshot_name, {})
            if isinstance(snapshot, dict):
                candidates.extend((
                    timestamp(snapshot.get("dateUTC"), broker_utc=True),
                    timestamp(snapshot.get("createdDateUTC"), broker_utc=True),
                    timestamp(snapshot.get("timestamp")),
                    timestamp(snapshot.get("event_epoch")),
                ))
        known = [value for value in candidates if value is not None]
        if known:
            job["search_from_epoch"] = min(known) - 3600
            job["search_to_epoch"] = max(known) + 3600
            job["search_range_source"] = "attempt_snapshot"
            job["history_range_uncertain"] = False
            job["search_time_version"] = SEARCH_TIME_VERSION
        elif has_bounds and source == "attempt_snapshot":
            # An old derived range cannot be corrected safely without its source timestamp.
            # Preserve it for operator visibility but explicitly revoke the claim of certainty.
            job["search_range_source"] = "attempt_snapshot_unverified"
            job["history_range_uncertain"] = True
            job["search_time_version"] = SEARCH_TIME_VERSION
        else:
            job["search_from_epoch"] = current - 86400
            job["search_to_epoch"] = current
            job["search_range_source"] = "legacy_fallback_last_24h"
            job["history_range_uncertain"] = True
            job["range_migrated_at"] = current
            job["search_time_version"] = SEARCH_TIME_VERSION
        changed = True
    return changed


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
        # Capital.com caps /history/activity at one day.  A restored job can be older than that,
        # so query its saved attempt interval as consecutive, non-expanding UTC windows instead
        # of sending an invalid lastPeriod > 86400.
        if job.get("search_from_epoch") is None or job.get("search_to_epoch") is None:
            # Direct callers and legacy queues are safe even before the main owner persists the
            # migration.  The worker mutates only its private job copy, never CycleState.
            migrate_notification_jobs([job])
        started = float(job["search_from_epoch"])
        saved_end = float(job["search_to_epoch"])
        ended = max(started + 1, min(saved_end, time.time()))

        def formatted(value: float) -> str:
            return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        def read(specific: bool, window_start: float, window_end: float) -> list[dict]:
            try:
                return client.activity(
                    deal_id if specific else "",
                    from_date=formatted(window_start), to_date=formatted(window_end),
                )
            except TypeError:  # legacy read-only adapters
                # Compatibility is deliberately limited to a valid recent one-day query.  Old
                # jobs require the production client's explicit from/to support.
                try:
                    return client.activity(deal_id if specific else "", last_period=86400)
                except TypeError:
                    return client.activity(deal_id) if specific else client.activity()

        # Non-empty deal history may contain only the opening.  Fall back based on absence of a
        # matching confirmed close, never based merely on whether the response list is empty.
        def resolve(activity: list[dict]) -> dict | None:
            for source in ("SL", "TP"):
                event = find_close_event(activity, deal_id, source)
                if event is not None and event.level is not None:
                    return {"source": source, "fill": str(event.level)}
            return None

        window_start = started
        while window_start < ended:
            window_end = min(window_start + 86400, ended)
            # Parse the specific response before issuing a broader request.  Besides avoiding an
            # unnecessary API call, this preserves an already-proved close if global history is
            # temporarily unavailable.
            result = resolve(read(True, window_start, window_end))
            if result is not None:
                return result
            result = resolve(read(False, window_start, window_end))
            if result is not None:
                return result
            window_start = window_end
        return None


class TransactionHistoryWorker(NotificationHistoryWorker):
    """Background account-ledger snapshots; CycleState remains owned by the main thread."""

    @staticmethod
    def _resolve(client: CapitalClient, job: dict) -> dict | None:
        from .reporting import broker_attempt_pnl

        started = float(job["search_from_epoch"])
        ended = max(started + 1, time.time())

        def formatted(value: float) -> str:
            return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        transactions: list[dict] = []
        window_start = started
        while window_start < ended:
            window_end = min(window_start + 86400, ended)
            transactions.extend(client.transactions(
                from_date=formatted(window_start), to_date=formatted(window_end), all_types=True,
            ))
            window_start = window_end
        result = broker_attempt_pnl(transactions, set(job["deal_ids"]))
        result["observed_to_epoch"] = ended
        return result


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
