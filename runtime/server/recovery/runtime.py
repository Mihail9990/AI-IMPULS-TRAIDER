"""Finite, simulation-only runtime contract for isolated range workers.

This is NOT the MT5 Windows runner. Existing MockPipeline methods operate on all
ranges and must not be passed as range workers: doing so defeats isolation.
A future production implementation needs scoped workers, durable reconciliation,
account-stable snapshots and a reviewed explicit startup path first.
"""
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class RangeFailure:
    range_id: str
    stage: str
    error_type: str
    message: str


@dataclass(frozen=True)
class CycleResult:
    completed: tuple
    failures: tuple


class IsolatedRuntime:
    """Run finite deterministic cycles; no terminal, network, daemon or live mode.

    Worker protocol: range_id, simulation_only=True, reconcile(), protect(quote),
    synchronize(). Each worker must own a distinct range. `record_failure` must
    durably quarantine the failed range; failure to record aborts the whole cycle.
    Global preflight exceptions abort before invoking any worker. Per-range
    failures skip subsequent stages for that range, not healthy ranges.
    """
    def __init__(self, workers, *, preflight, record_failure, simulation_only=False):
        if simulation_only is not True:
            raise RuntimeError('Live runtime is not implemented')
        self._workers = tuple(workers)
        ids = [w.range_id for w in self._workers]
        if not ids or len(ids) > 2 or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError('One or two named range workers required')
        if len(set(ids)) != len(ids):
            raise ValueError('Range workers must have distinct ownership')
        if any(getattr(w, 'simulation_only', False) is not True for w in self._workers):
            raise RuntimeError('Only explicit simulation workers supported')
        self._identities = tuple(ids)
        self._preflight = preflight
        self._record_failure = record_failure
        self._lock = Lock()
        self._closed = False
        self._quarantined = set()

    def _validate_workers(self):
        # Check all workers, not only the next one: one callback can mutate a peer.
        # Ownership/mode violations are global faults, never a local range error.
        if tuple(getattr(w, 'range_id', None) for w in self._workers) != self._identities:
            self._closed = True
            raise RuntimeError('Worker ownership changed; runtime closed')
        if any(getattr(w, 'simulation_only', False) is not True for w in self._workers):
            self._closed = True
            raise RuntimeError('Worker mode changed; runtime closed')

    def step(self, quote):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Concurrent or recursive cycle forbidden')
        try:
            if self._closed:
                raise RuntimeError('Runtime closed')
            self._validate_workers()
            try:
                self._preflight()
            finally:
                self._validate_workers()
            completed, failures = [], []
            for worker, rid in zip(self._workers, self._identities):
                self._validate_workers()
                if rid in self._quarantined:
                    continue
                for stage in ('reconcile', 'protect', 'synchronize'):
                    self._validate_workers()
                    try:
                        method = getattr(worker, stage)
                        method(quote) if stage == 'protect' else method()
                    except Exception as exc:
                        self._validate_workers()
                        failure = RangeFailure(rid, stage, type(exc).__name__, str(exc))
                        # Quarantine before persistence, even if the durable sink fails.
                        self._quarantined.add(rid)
                        try:
                            self._record_failure(failure)
                        except Exception:
                            self._closed = True
                            raise
                        self._validate_workers()
                        failures.append(failure)
                        break
                    self._validate_workers()
                else:
                    completed.append(rid)
            return CycleResult(tuple(completed), tuple(failures))
        finally:
            self._lock.release()

    def run_steps(self, quotes, *, max_steps):
        """Bound execution even for an infinite iterable. Never sleep or auto-retry."""
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0:
            raise ValueError('Nonnegative integer max_steps required')
        from itertools import islice
        return [self.step(quote) for quote in islice(quotes, max_steps)]

    def close(self):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Cannot close during a cycle')
        try:
            self._closed = True
        finally:
            self._lock.release()
