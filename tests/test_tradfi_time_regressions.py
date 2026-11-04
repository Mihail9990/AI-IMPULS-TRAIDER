"""Permanent regressions for the five audited time-safety failures; no real IO."""
import threading
import time
import unittest
from copy import deepcopy
from unittest.mock import patch

import test_tradfi_time_safety as time_cases
from test_tradfi_native_service import NativeAPI
from tradfi.engine import Controller
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.time_profile import BYBIT_PROFILE
from tradfi.time_safety import TimeSafety
from tradfi.storage import Store, DurabilityError


class TimeRegressions(unittest.TestCase):
    setUp = time_cases.DurableTimeTest.setUp
    tearDown = time_cases.DurableTimeTest.tearDown
    restart = time_cases.DurableTimeTest.restart
    start_pair = time_cases.DurableTimeTest.start_pair
    position = time_cases.DurableTimeTest.position
    advance = time_cases.DurableTimeTest.advance
    finish_time = time_cases.DurableTimeTest.finish_time

    def test_time_block_cancels_both_pending_and_restart_never_reenters(self):
        self.start_pair(); self.semantics = False
        self.bot.tick(); self.bot.tick()
        self.assertFalse(self.api.orders)
        self.assertEqual(len(self.api.sends), 4)
        self.restart(); self.bot.tick()
        self.assertEqual(len(self.api.sends), 4)
        self.assertIn('time_block', self.store.load())

    def test_confirmed_cancelled_pair_resumes_only_after_fresh_explicit_request(self):
        self.start_pair(); self.semantics=False
        self.bot.tick(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'NEXT_PAIR')
        self.semantics=True; self.bot.refresh_time(); self.finish_time(); self.bot.tick()
        self.assertFalse(self.api.orders)
        self.bot.command('/resume_time'); self.finish_time(); self.bot.tick()
        self.assertEqual(len(self.api.orders),2)

    def test_block_discards_ready_entries_before_dispatch(self):
        self.bot.command('/start'); self.api.rates[0]['time'] += 60
        with patch.object(self.bot, 'dispatch'): self.bot.tick()
        self.semantics = False; self.bot.dispatch()
        self.assertFalse(self.api.sends)
        self.assertTrue(all(c.get('never_sent') for c in self.bot.state['commands'].values()))

    def test_backwards_clock_preserves_oco_and_modify(self):
        self.start_pair()
        mono = self.api.now
        self.bot.monotonic = self.broker.monotonic = self.broker.time_safety.monotonic = lambda: mono
        self.broker.time_safety.refresh()
        self.api.fill(); self.api.now -= 20
        self.bot.tick(); self.bot.tick()
        self.assertFalse(self.api.orders)
        self.assertEqual(self.bot.state['cycle']['manual_reason'], 'UTC_CLOCK_MOVED_BACKWARDS')
        self.assertTrue(self.bot.state['cycle']['position'])
        p = self.api.positions[0]
        cid = self.bot._intent('MODIFY', position_id=str(p.identifier), sl=str(p.sl), tp=str(p.tp), purpose='ANCHOR')
        self.bot.save('test_modify'); self.bot.dispatch(); self.bot.tick()
        self.assertEqual(self.bot.state['commands'][cid]['status'], 'DONE')
        count = len(self.api.sends); self.restart(); self.bot.tick()
        self.assertEqual(len(self.api.sends), count)

    def test_time_block_fill_during_cancel_is_adopted_and_protected(self):
        self.start_pair(); self.semantics = False
        original = self.api.order_check
        raced = []
        def race(req):
            if req['action'] == self.api.TRADE_ACTION_REMOVE and not raced:
                raced.append(True); self.api.fill()
            return original(req)
        with patch.object(self.api, 'order_check', side_effect=race):
            self.bot.tick()
        self.bot.tick(); self.bot.tick()
        self.assertFalse(self.api.orders)
        self.assertTrue(self.bot.state['cycle']['position'])
        self.assertGreater(self.api.positions[0].sl, 0)
        self.assertGreater(self.api.positions[0].tp, 0)
        self.assertIn('time_block', self.store.load())

    def test_foreign_pending_is_never_cancelled_on_block(self):
        self.start_pair()
        self.api.orders.append(self.api.orders[0]._replace(ticket=999, magic=1, comment='foreign'))
        self.semantics = False; count = len(self.api.sends); self.bot.tick()
        self.assertEqual(len(self.api.sends), count)
        self.assertEqual(self.bot.state['phase'], 'SAFE_PAUSE')

    def test_stale_expiry_cancel_is_known_unsent_then_recovers_after_restart(self):
        self.start_pair(); self.api.now += 86400  # deliberately leave quote stale
        self.bot.tick(); self.bot.tick()
        cancels = [c for c in self.bot.state['commands'].values() if c['kind'] == 'CANCEL']
        self.assertEqual(len(cancels), 2)
        self.assertTrue(all(c['status'] == 'REJECTED' and c['result']['never_sent'] for c in cancels))
        self.assertTrue(all(not self.broker.rows[c['id']]['send_started'] for c in cancels))
        self.restart()
        for _ in range(5): self.bot.tick()
        self.assertEqual(len(self.api.sends), 2)
        self.assertEqual(len([c for c in self.bot.state['commands'].values() if c['kind']=='CANCEL']), 2)
        self.advance(0); self.bot.tick(); self.bot.tick()
        self.assertFalse(self.api.orders)
        self.assertEqual(len(self.api.sends), 4)

    def test_cancel_timeout_after_send_remains_unknown_no_replay(self):
        self.start_pair(); self.semantics = False
        original = self.api.order_send
        def unknown(req):
            if req['action'] == self.api.TRADE_ACTION_REMOVE:
                self.api.sends.append(deepcopy(req)); raise TimeoutError('lost reply')
            return original(req)
        with patch.object(self.api, 'order_send', side_effect=unknown):
            self.bot.tick()
        count = len(self.api.sends); self.restart()
        for _ in range(5): self.bot.tick()
        self.assertEqual(len(self.api.sends), count)
        cancels = [r for r in self.broker.rows.values() if r['command']['kind']=='CANCEL']
        self.assertTrue(all(r['send_started'] and r['result']['status']=='UNKNOWN' for r in cancels))

    def test_not_sent_record_failure_never_retries_uncertain_journal(self):
        self.start_pair(); self.api.now += 86400
        original = self.broker._save_row
        def fail(cid, row, event):
            if event == 'not_sent': raise DurabilityError('power loss')
            return original(cid, row, event)
        with patch.object(self.broker, '_save_row', side_effect=fail):
            with self.assertRaises(DurabilityError): self.bot.tick()
        self.restart(); self.advance(0)
        self.bot.tick(); self.bot.tick()
        # A persisted intent without terminal non-send proof stays unresolved.
        unknown = [c for c in self.bot.state['commands'].values() if c['kind']=='CANCEL' and c['status']=='UNKNOWN']
        self.assertTrue(unknown)
        self.assertTrue(self.api.orders)

    def test_stale_observation_does_not_create_price_sensitive_modify(self):
        self.position()
        p=self.api.positions[0]; self.api.positions[0]=p._replace(tp=p.tp+.1)
        self.bot.state['cycle']['position']['reanchored']=False
        self.bot.save('test_anchor_drift'); self.api.now+=10
        count=len(self.api.sends); self.bot.tick(); self.bot.tick()
        self.assertEqual(len(self.api.sends),count)
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.advance(0); self.bot.tick(); self.bot.tick()
        self.assertEqual(self.api.positions[0].tp,p.tp)

    def test_ready_modify_known_unsent_recovers_on_fresh_quote(self):
        self.position(); p=self.api.positions[0]
        cid=self.bot._intent('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        self.bot.save('queued_protection'); self.api.now+=10
        self.bot.dispatch(); self.bot.tick()
        self.assertEqual(self.bot.state['commands'][cid]['status'],'REJECTED')
        self.assertTrue(self.bot.state['commands'][cid]['result']['never_sent'])
        self.advance(0); self.bot.tick(); self.bot.tick()
        replacement=self.bot.state['commands'][cid]['retry_replaced']
        self.assertEqual(self.bot.state['commands'][replacement]['status'],'DONE')
        self.assertEqual(self.bot.state['phase'],'POSITION')

    def native(self):
        self.broker.close(); self.store.close()
        self.api = NativeAPI(); self.path = self.path.with_name('native-regression.db')
        self.config.update(time_profile=BYBIT_PROFILE, execution_contract='cycle_scoped_identity_v1')
        self.restart()

    def test_unverified_raw_times_do_not_prevent_owned_observation(self):
        self.native(); self.bot.command('/start')
        self.api.now += 60; self.api.rates[0]['time'] += 60; self.bot.tick()
        self.api.fill(); self.semantics = False
        tick = self.api.symbol_info_tick('XAUUSD.s')
        for raw in (0, -1, 1e30, float('nan')):
            with self.subTest(raw=raw), patch.object(self.api, 'symbol_info_tick', return_value=tick._replace(time_msc=raw, time=0)):
                snap = self.broker.snapshot()
                self.assertTrue(snap['orders']); self.assertTrue(snap['positions'])
                self.assertTrue(snap['positions'][0]['owned'])
                self.assertIsNone(snap['quote']['utc_time']); self.assertEqual(snap['candle']['id'], 0)
                self.assertEqual(snap['mt5_timestamp_semantics'], 'UNVERIFIED')
        self.assertFalse(self.broker._timestamp_semantics_verified())

    def native_pair(self):
        self.native(); self.bot.command('/start')
        self.api.now += 60; self.api.rates[0]['time'] += 60; self.bot.tick()
        self.assertEqual(len(self.api.orders), 2)

    def test_native_unverified_semantics_cancel_own_pending_without_utc_claim(self):
        self.native_pair(); self.semantics = False
        self.restart(); self.bot.tick(); self.bot.tick()
        self.assertFalse(self.api.orders)
        self.assertEqual(len(self.api.sends), 4)
        self.assertIn('time_block', self.store.load())
        self.assertIsNone(self.broker.snapshot()['quote']['utc_time'])
        with self.assertRaises(ValueError): self.broker.snapshot(require_fresh=True)

    def test_native_backward_clock_oco_and_protection_use_trusted_reference(self):
        self.native_pair(); self.api.fill()
        actual = self.api.now
        self.bot.clock = self.broker.clock = self.broker.time_safety.clock = lambda: actual-120
        self.bot.tick(); self.bot.tick()
        self.assertFalse(self.api.orders)
        self.assertTrue(self.bot.state['cycle']['position'])
        p=self.api.positions[0]
        cid=self.bot._intent('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        self.bot.save('native_clock_protection'); self.bot.dispatch(); self.bot.tick()
        self.assertEqual(self.bot.state['commands'][cid]['status'], 'DONE')
        self.assertEqual(self.store.load()['time_block']['reason'], 'LOCAL_CLOCK_JUMP')

    def test_native_unverified_freeze_guard_is_not_bypassed(self):
        self.native_pair(); self.semantics = False
        original = self.api.symbol_info
        with patch.object(self.api, 'symbol_info', side_effect=lambda symbol: original(symbol)._replace(trade_freeze_level=10)):
            self.bot.tick()
        self.assertEqual(len(self.api.sends), 2)
        self.assertEqual(len(self.api.orders), 2)

    def test_failed_manual_adoption_does_not_crash_or_remove_broker_protection(self):
        self.start_pair(); self.api.fill(); self.advance(86400)
        before=deepcopy(self.api.positions)
        with patch.object(self.broker, 'profit', return_value=0):
            self.bot.tick(); self.bot.tick()
        self.assertEqual(self.api.positions,before)
        self.assertIsNone(self.bot.state['cycle']['position'])
        self.assertIn('reconciliation_error',self.bot.state['cycle'])
        self.assertFalse(self.api.orders)

    def test_late_sl_keeps_existing_observation_time_branch_semantics(self):
        self.position(); self.api.close_position(reason=4)
        self.advance(30); self.bot.tick()
        self.assertEqual(self.bot.state['cycle']['branch_started_at_utc'], self.api.now)
        self.assertEqual(self.bot.state['cycle']['branch_deadline_utc'], self.api.now+86400)

    def test_resume_request_durability_failure_cannot_release_latch(self):
        self.semantics=False; self.bot.tick(); self.semantics=True
        def crash(event):
            if event=='time_resume_requested': raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError): self.bot.command('/resume_time')
        self.store.fault=None; self.finish_time()
        self.assertIn('time_block', self.store.load())

    def test_async_resume_failure_and_restart_cannot_clear_latch(self):
        self.semantics = False; self.bot.tick(); self.semantics = True
        gate = threading.Event(); entered = threading.Event()
        original = self.broker.time_safety.get
        def stalled(*a, **kw):
            entered.set(); gate.wait(2); return original(*a, **kw)
        with patch.object(self.broker.time_safety, 'get', side_effect=stalled):
            self.bot.command('/resume_time'); self.assertTrue(entered.wait(1))
            self.assertIn('time_block', self.store.load())
            old = self.broker.time_safety
            self.restart(); gate.set(); old._worker.join(1)
        self.bot.tick(); self.assertIn('time_block', self.store.load())


class AsyncUTCRegressions(unittest.TestCase):
    def test_stalled_check_is_nonblocking_bounded_and_late_success_ignored(self):
        wall, mono = [1791800000.], [10.]
        gate, entered = threading.Event(), threading.Event()
        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {'retCode':0,'result':{'timeNano':str(int(wall[0]*1e9)),'timeSecond':str(int(wall[0]))}}
        def get(*a, **kw):
            entered.set(); gate.wait(2); return Response()
        safety = TimeSafety(clock=lambda:wall[0], monotonic=lambda:mono[0], get=get, semantics_verifier=lambda:True)
        started=time.monotonic(); token=safety.request_refresh()
        self.assertLess(time.monotonic()-started, .5); self.assertTrue(entered.wait(1))
        worker=safety._worker
        self.assertIsNone(safety.request_refresh()); self.assertIs(safety._worker,worker)
        mono[0]+=16; wall[0]+=16
        self.assertEqual(safety.poll_refresh(),token)
        self.assertEqual(safety.check(),'EXTERNAL_UTC_TIMEOUT')
        gate.set(); worker.join(1); self.assertFalse(worker.is_alive())
        self.assertIsNone(safety.poll_refresh())
        self.assertEqual(safety.check(),'EXTERNAL_UTC_TIMEOUT')
        safety.request_refresh(); safety._worker.join(1); safety.poll_refresh()
        self.assertIsNone(safety.check())
        mono[0]+=61; wall[0]+=61
        self.assertEqual(safety.check(),'EXTERNAL_UTC_STALE')
