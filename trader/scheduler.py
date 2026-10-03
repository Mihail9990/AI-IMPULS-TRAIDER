"""Shared bounded priority scheduler for Capital REST calls."""
from __future__ import annotations

from dataclasses import dataclass, field
import itertools
from queue import PriorityQueue
import threading
import time
from typing import Any, Callable


@dataclass(order=True)
class _Work:
    priority: int
    sequence: int
    key: str = field(compare=False)
    call: Callable[[], Any] = field(compare=False)
    done: threading.Event = field(default_factory=threading.Event, compare=False)
    result: Any = field(default=None, compare=False)
    error: BaseException | None = field(default=None, compare=False)


class ApiRequestScheduler:
    """Serialize one API host, coalesce equal reads and bound queued work.

    Priority 0 is position/mutation reconciliation, 1 confirmations/activity, 2 ordinary reads,
    and 3 reporting. A small burst allowance plus FIFO sequence prevents a stream of websocket
    hints from creating an unbounded set of identical GETs. Mutations are never coalesced or
    retried here.
    """

    def __init__(self, *, requests_per_second: float = 10, max_pending: int = 256,
                 workers: int = 4,
                 clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float], None] = time.sleep):
        self.interval = 1 / requests_per_second
        self.clock, self.wait = clock, wait
        self.queue: PriorityQueue[_Work] = PriorityQueue(max_pending)
        self._sequence = itertools.count()
        self._lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._reads: dict[str, _Work] = {}
        self._last_send = 0.0
        for index in range(workers):
            threading.Thread(target=self._run, name=f"capital-api-scheduler-{index}",
                             daemon=True).start()

    def execute(self, key: str, priority: int, call: Callable[[], Any], *, coalesce: bool) -> Any:
        with self._lock:
            work = self._reads.get(key) if coalesce else None
            if work is None:
                work = _Work(priority, next(self._sequence), key, call)
                if coalesce:
                    self._reads[key] = work
                self.queue.put_nowait(work)
        work.done.wait()
        if work.error is not None:
            raise work.error
        return work.result

    def _run(self) -> None:
        while True:
            work = self.queue.get()
            with self._rate_lock:
                delay = self.interval - (self.clock() - self._last_send)
                if delay > 0:
                    self.wait(delay)
                self._last_send = self.clock()
            try:
                work.result = work.call()
            except BaseException as exc:
                work.error = exc
            finally:
                with self._lock:
                    if self._reads.get(work.key) is work:
                        self._reads.pop(work.key, None)
                work.done.set()
                self.queue.task_done()


_SCHEDULERS: dict[str, ApiRequestScheduler] = {}
_SCHEDULERS_LOCK = threading.Lock()


def scheduler_for(host: str) -> ApiRequestScheduler:
    with _SCHEDULERS_LOCK:
        return _SCHEDULERS.setdefault(host, ApiRequestScheduler())
