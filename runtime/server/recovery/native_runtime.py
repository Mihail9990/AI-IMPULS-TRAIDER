"""Single-process scheduler for an explicitly opened native session.

No terminal initialization, credentials, reconnect, outbox replay, or account-mode
restriction lives here. Caller owns terminal shutdown after the runtime closes.
"""
from contextlib import asynccontextmanager
import math
import threading

from .native_api import create_native_app
from .native_coordinator import NativeCoordinator
from .native_executor_session import NativeExecutorSession
from .native_recovery_control import NativeRecoveryControl


class NativeScheduler:
    def __init__(self, coordinator, *, interval=0.25):
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 0.05:
            raise ValueError('Tick interval must be finite and >= 0.05 seconds')
        self.coordinator = coordinator
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._lifecycle = threading.Lock()
        self.failure = None

    def _run(self):
        # Event.wait provides interruptible stop and bounded scheduling, not a
        # catch-up queue. Coordinator serializes every transition with HTTP.
        while not self._stop.wait(self.interval):
            try:
                self.coordinator.tick()
            except Exception as exc:
                # Never retry unexpected failures automatically. Broker stops
                # remain in place; runtime has no emergency trade side effects.
                with self.coordinator.session._lock:
                    self.coordinator.session.ready = False
                    self.failure = type(exc).__name__
                    self.coordinator.last_tick = {'status': 'reconciliation_required',
                                                 'issues': ['Scheduler stopped: ' + self.failure]}
                self._stop.set()

    def start(self):
        with self._lifecycle:
            if self._thread is not None:
                raise RuntimeError('Scheduler is single-use')
            self._thread = threading.Thread(target=self._run, name='native-recovery', daemon=False)
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._stop.set()
                raise

    def stop(self):
        with self._lifecycle:
            self._stop.set()
            if self._thread is not None:
                # Do not close SQLite or terminal beneath a pending broker call.
                self._thread.join()


class NativeRuntime:
    """Explicit open -> ASGI lifespan -> close. Never open a second worker."""
    def __init__(self, api, binding, *, database, magic, token, interval=0.25):
        if not isinstance(token, str) or not token.isascii() or len(token) < 32:
            raise ValueError('ASCII API token of at least 32 characters required')
        self.session = NativeExecutorSession(api, binding, database=database)
        self.scheduler = None
        self.app = None
        self.closed = False
        self._opened = False
        self._config = (magic, token, interval)

    def open(self):
        if self.closed or self._opened:
            raise RuntimeError('Runtime is single-use')
        self._opened = True
        try:
            self.session.__enter__()
            magic, token, interval = self._config
            control = NativeRecoveryControl(self.session, magic=magic)
            control.reconcile()  # Only observe; never replay staged commands.
            self.coordinator = NativeCoordinator(control)
            self.scheduler = NativeScheduler(self.coordinator, interval=interval)

            @asynccontextmanager
            async def lifespan(app):
                try:
                    self.scheduler.start()
                    yield
                finally:
                    self.close()

            self.app = create_native_app(token, self.coordinator, lifespan=lifespan)
            return self.app
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        if self.scheduler is not None:
            self.scheduler.stop()
        self.session.close()
        self.closed = True
