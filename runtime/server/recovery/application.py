"""Owned HTTP + strategy assembly for injected MT5 test doubles only.

No terminal import, network quote input, thread, timer or live execution. ASGI
lifespan is mandatory. The local test driver supplies explicit bounded ticks.
"""
from contextlib import asynccontextmanager
from .controller import Controller
from .engine import Engine
from .pipeline import MockPipeline
from .scoped_service import ScopedMockService
from .integrated_api import create_mock_pipeline_app


class MockApplication:
    def __init__(self, fake_mt5, *, token, account, server, symbol, magic,
                 database=':memory:', market_filling=None, require_stop_evidence=False):
        self.controller = Controller(Engine(), database, test_mode=True)
        self.pipeline = None
        self.service = None
        self.closed = False
        self.started = False
        try:
            # Controller and outbox share a durable database, not independent
            # in-memory outboxes when a caller chooses persistent state.
            self.pipeline = MockPipeline(self.controller, fake_mt5, account,
                                         server, symbol, magic, database,
                                         require_stop_evidence=require_stop_evidence)
            self.app = create_mock_pipeline_app(token, self.pipeline,
                market_filling=market_filling, lifespan=self._lifespan)
        except BaseException:
            self.close()
            raise

    @asynccontextmanager
    async def _lifespan(self, app):
        with self.controller.lock:
            if self.closed or self.started:
                raise RuntimeError('Application lifespan is single-use')
            self.started = True
            try:
                self.service = ScopedMockService(self.pipeline)
            except BaseException:
                self.close()
                raise
        try:
            yield
        finally:
            self.close()

    def step(self, bid, ask, *, now=None):
        """Local-only driver. API and ticks share Controller's serialization lock."""
        with self.controller.lock:
            if self.closed or self.service is None:
                raise RuntimeError('Application is not running')
            return self.service.step(bid, ask, now=now)

    def run_steps(self, quotes, *, max_steps=1000):
        """Consume at most max_steps; never implicitly run an unbounded iterator."""
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 10000:
            raise ValueError('max_steps must be an integer in 1..10000')
        from itertools import islice
        return [self.step(bid, ask) for bid, ask in islice(quotes, max_steps)]

    def close(self):
        with self.controller.lock:
            if self.closed:
                return
            self.closed = True
            try:
                if self.service is not None:
                    self.service.close()
            finally:
                try:
                    if self.pipeline is not None:
                        self.pipeline.executor.db.close()
                finally:
                    self.controller.db.close()
