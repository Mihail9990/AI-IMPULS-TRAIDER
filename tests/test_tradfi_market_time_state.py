"""Fixed clock bindings must never reinterpret saved execution/paper state."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from tradfi.storage import Store
from tradfi.market_time_state import bind_market_time_state, MarketTimeStateError
from tradfi.paper import PaperSession
from tradfi.runtime import _run_mt5


class MarketTimeBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.db'
        self.config = dict(expected_server='Bybit-Live-6', expected_login=123,
                           symbol='XAUUSD.s', web={'enabled': False}, state_file=str(self.path))

    def seed(self, binding=None, paper=False):
        store = Store(self.path)
        if binding is not None:
            bind_market_time_state(store, dict(self.config, market_time_offset_seconds=binding))
        # Deliberately retained READY intent and nonzero debt. A startup rejection
        # must happen before a controller can reconcile or dispatch this intent.
        state = dict(schema=1, backend='paper_mt5_quotes_v1', phase='READY',
                     basket_carry='123.45', commands={'old': {'status': 'READY'}},
                     candle_id=1793620800)
        store.db.execute('INSERT INTO snapshot VALUES(1,?)', (json.dumps(state),))
        if paper:
            store.db.execute('CREATE TABLE paper_exchange(id INTEGER PRIMARY KEY, payload TEXT)')
            store.db.execute('INSERT INTO paper_exchange VALUES(1,?)', (json.dumps(dict(
                cursor=1793620830000, cursor_keys=['original'], deals=[{'time': 1793620800}],
                orders=[{'id':'saved-order'}], positions=[])),))
        before = list(store.db.iterdump())
        store.close()
        return before

    def dump(self):
        store = Store(self.path)
        try:
            return list(store.db.iterdump())
        finally:
            store.close()

    def test_fresh_nonzero_binds_and_same_profile_reopens(self):
        store = Store(self.path)
        try:
            config = dict(self.config, market_time_offset_seconds=7200)
            self.assertEqual(bind_market_time_state(store, config)['market_time_offset_seconds'], 7200)
            bind_market_time_state(store, config)
            for update in ({'market_time_offset_seconds': 0}, {'expected_login': 124},
                           {'expected_server': 'another-server'}):
                with self.assertRaises(MarketTimeStateError):
                    bind_market_time_state(store, dict(config, **update))
        finally:
            store.close()

    def test_legacy_nonzero_rejects_without_any_database_change(self):
        before = self.seed(paper=True)
        store = Store(self.path)
        try:
            with self.assertRaisesRegex(MarketTimeStateError, 'Legacy database'):
                bind_market_time_state(store, dict(self.config, market_time_offset_seconds=7200))
        finally:
            store.close()
        self.assertEqual(self.dump(), before)

    def test_legacy_default_zero_adopts_without_changing_snapshot(self):
        self.seed(paper=True)
        store = Store(self.path)
        try:
            old = store.load()
            history = store.db.execute('SELECT payload FROM paper_exchange').fetchone()
            self.assertEqual(bind_market_time_state(store, self.config)['market_time_offset_seconds'], 0)
            self.assertEqual(store.load(), old)
            self.assertEqual(store.db.execute('SELECT payload FROM paper_exchange').fetchone(), history)
        finally:
            store.close()

    def test_paper_rejects_before_exchange_or_controller_and_preserves_cursor(self):
        before = self.seed(binding=0, paper=True)
        reader = SimpleNamespace(_expected_account='123@Bybit-Live-6', market_time_offset_seconds=7200)
        with patch('tradfi.paper.PaperExchange') as exchange, patch('tradfi.paper.ControlledRecoveryController') as controller:
            exchange.backend_id = 'paper_mt5_quotes_v1'
            with self.assertRaises(MarketTimeStateError):
                PaperSession(self.path, reader, clock=lambda: 0, monotonic=lambda: 0)
            exchange.assert_not_called()
            controller.assert_not_called()
        self.assertEqual(self.dump(), before)

    def test_live_stale_ready_cannot_connect_or_dispatch_after_offset_change(self):
        before = self.seed(binding=0)
        config = dict(self.config, market_time_offset_seconds=7200,
                      time_profile='bybit_live6_time_v1', enable_trading=True,
                      execution_contract='cycle_scoped_identity_v1',
                      legacy_ea_removed=True, legacy_state_reconciled=True)
        with patch('tradfi.managed.runtime_guard', return_value=nullcontext()), \
             patch('tradfi.runtime.owner_lock', return_value=nullcontext()), \
             patch('tradfi.runtime.account_lock_path', return_value=self.path), \
             patch('tradfi.managed.bind_state') as managed_bind, \
             patch('tradfi.evidence_broker.MT5EvidenceBroker') as broker, \
             patch('tradfi.recovery_v3.ControlledRecoveryController') as controller:
            with self.assertRaises(MarketTimeStateError):
                _run_mt5(config, False, service=True, execute=True, stop_event=SimpleNamespace(is_set=lambda: True))
            managed_bind.assert_not_called()
            broker.assert_not_called()
            controller.assert_not_called()
        self.assertEqual(self.dump(), before)

    def test_direct_controller_cannot_bypass_store_binding(self):
        from tradfi.recovery_v3 import ControlledRecoveryController
        before=self.seed(binding=0)
        store=Store(self.path)
        broker=SimpleNamespace(config=dict(self.config,time_profile='bybit_live6_time_v1',market_time_offset_seconds=7200))
        try:
            with self.assertRaises(MarketTimeStateError):
                ControlledRecoveryController(store,broker)
        finally:store.close()
        self.assertEqual(self.dump(),before)

    def test_startup_reason_explains_preservation_without_identity_disclosure(self):
        from tradfi.startup_errors import reason, REASONS
        self.assertEqual(reason(MarketTimeStateError('private account')), 'MARKET_TIME_STATE')
        self.assertNotIn('private account', REASONS['MARKET_TIME_STATE'])
        self.assertIn('offset 0', REASONS['MARKET_TIME_STATE'])

    def test_audit_only_legacy_database_is_not_fresh(self):
        store = Store(self.path)
        try:
            store.db.execute("INSERT INTO audit(event,payload) VALUES('old','{}')")
            with self.assertRaises(MarketTimeStateError):
                bind_market_time_state(store, dict(self.config, market_time_offset_seconds=60))
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
