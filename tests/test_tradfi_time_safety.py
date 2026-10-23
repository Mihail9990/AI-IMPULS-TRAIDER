"""Public-time failures and durable entry-only guards, entirely offline."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from tradfi.time_safety import TimeSafety, URL
from tradfi.engine import Controller
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.storage import Store, DurabilityError
from time_fixtures import verified_time
import test_tradfi_evidence_controller as fixtures


class ExternalTimeTest(unittest.TestCase):
    def setUp(self):
        self.wall, self.mono = 1770000000., 10.
        self.calls = []
        self.delay, self.offset, self.jump = .2, 0., 0.
        self.payload = None
        self.semantics = True
        self.safety = TimeSafety(clock=lambda:self.wall, monotonic=lambda:self.mono,
            get=self.get, semantics_verifier=lambda:self.semantics)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        remote = self.wall+self.delay/2+self.offset
        self.wall += self.delay+self.jump
        self.mono += self.delay
        payload = self.payload or {'retCode': 0, 'result': {'timeNano': str(int(remote*1e9)), 'timeSecond': str(int(remote))}}
        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return payload
        return Response()

    def test_three_https_samples_include_rtt_uncertainty_without_credentials(self):
        self.assertIsNone(self.safety.refresh())
        self.assertEqual(len(self.calls), 3)
        for url, kwargs in self.calls:
            self.assertEqual(url, URL)
            self.assertEqual(kwargs, {'timeout': (2., 2.), 'allow_redirects': False})
        for sample in self.safety.result['samples']:
            self.assertAlmostEqual(sample['rtt_seconds'], .2)
            self.assertAlmostEqual(sample['uncertainty_seconds'], .100001)
        self.assertIsNone(self.safety.check())

    def test_semantics_not_verified_by_external_utc(self):
        self.semantics = False
        self.assertEqual(self.safety.refresh(), 'MT5_TIMESTAMP_SEMANTICS_UNVERIFIED')
        self.assertTrue(self.safety.result['ok'])

    def test_no_samples_is_fail_closed(self):
        self.assertEqual(self.safety.check(), 'EXTERNAL_UTC_UNVERIFIED')

    def test_stale_measurement(self):
        self.safety.refresh(); self.wall += 61; self.mono += 61
        self.assertEqual(self.safety.check(), 'EXTERNAL_UTC_STALE')

    def test_forward_and_backward_clock_jumps(self):
        for jump in (-20., 20.):
            with self.subTest(jump=jump):
                self.safety.refresh(); self.wall += jump
                self.assertEqual(self.safety.check(), 'LOCAL_CLOCK_JUMP')

    def test_monotonic_rollback(self):
        self.safety.refresh(); self.mono -= 1
        self.assertEqual(self.safety.check(), 'LOCAL_CLOCK_JUMP')

    def test_jump_during_measurement(self):
        self.jump = 10.
        self.assertEqual(self.safety.refresh(), 'LOCAL_CLOCK_JUMP')
        self.assertFalse(self.safety.result['ok'])

    def test_jump_between_samples(self):
        original = self.safety.get
        def get(*args, **kwargs):
            if self.calls: self.wall += 10
            return original(*args, **kwargs)
        self.safety.get = get
        self.assertEqual(self.safety.refresh(), 'LOCAL_CLOCK_JUMP')

    def test_timeout_and_errors_do_not_leak_details(self):
        for exc in (TimeoutError('private'), OSError('private')):
            with patch.object(self.safety, 'get', side_effect=exc):
                self.assertEqual(self.safety.refresh(), 'EXTERNAL_UTC_UNAVAILABLE')
                self.assertNotIn('private', str(self.safety.result))

    def test_failed_refresh_invalidates_previous_success(self):
        self.safety.refresh()
        with patch.object(self.safety, 'get', side_effect=TimeoutError): self.safety.refresh()
        self.assertEqual(self.safety.check(), 'EXTERNAL_UTC_UNAVAILABLE')

    def test_excessive_rtt(self):
        self.delay = 3
        self.assertEqual(self.safety.refresh(), 'EXTERNAL_UTC_RTT')

    def test_offset_budget_includes_uncertainty(self):
        self.offset = 1.95
        self.assertEqual(self.safety.refresh(), 'EXTERNAL_UTC_OFFSET_OR_UNCERTAINTY')

    def test_malformed_response(self):
        for payload in ({'retCode': 0, 'result': {}}, {'retCode': 1}, {'retCode': True},
                        {'retCode': 0, 'result': {'timeNano': 'nan', 'timeSecond': '1'}},
                        {'retCode': 0, 'result': {'timeNano': '1770000000000000000', 'timeSecond': '1'}}):
            with self.subTest(payload=payload):
                self.payload = payload
                self.assertIsNotNone(self.safety.refresh())
                self.assertFalse(self.safety.result['ok'])

    def test_inconsistent_offsets(self):
        original = self.safety.get
        def get(*args, **kwargs):
            self.offset = .5 if self.calls else -.5
            return original(*args, **kwargs)
        self.safety.get = get
        self.assertEqual(self.safety.refresh(), 'EXTERNAL_UTC_INCONSISTENT')

    def test_nonfinite_clocks_and_verifier_failure(self):
        self.wall = float('nan'); self.assertIsNotNone(self.safety.refresh())
        self.wall = 1770000000.
        with patch.object(self.safety, 'semantics_verifier', side_effect=RuntimeError):
            self.assertEqual(self.safety.check(), 'TIME_CHECK_FAILED')

    def test_redirect_not_accepted(self):
        response = self.get(URL); response.status_code = 302
        with patch.object(self.safety, 'get', return_value=response):
            self.assertEqual(self.safety.refresh(), 'EXTERNAL_UTC_HTTP_STATUS')


class DurableTimeTest(unittest.TestCase):
    setUp = fixtures.EvidenceControllerTest.setUp
    tearDown = fixtures.EvidenceControllerTest.tearDown
    start_pair = fixtures.EvidenceControllerTest.start_pair
    position = fixtures.EvidenceControllerTest.position
    ledger = fixtures.EvidenceControllerTest.ledger

    def restart(self):
        if hasattr(self, 'store'): self.broker.close(); self.store.close()
        if not hasattr(self, 'semantics'): self.semantics = True
        self.store = Store(self.path)
        safety = verified_time(clock=lambda:self.api.now, monotonic=lambda:self.api.now)
        safety.semantics_verifier = lambda:self.semantics
        self.broker = MT5EvidenceBroker(self.config, self.store, api=self.api,
            clock=lambda:self.api.now, monotonic=lambda:self.api.now, time_safety=safety)
        self.broker.connect()
        self.bot = Controller(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)

    def finish_time(self):
        worker = self.broker.time_safety._worker
        if worker:
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.bot.poll_time()

    def advance(self, seconds):
        self.api.now += seconds
        self.api.tick = self.api.tick._replace(time_msc=self.api.now*1000)

    def test_semantics_latch_survives_restart_and_start(self):
        self.semantics = False; self.bot.tick()
        self.assertEqual(self.store.load()['time_block']['reason'], 'MT5_TIMESTAMP_SEMANTICS_UNVERIFIED')
        self.semantics = True; self.restart()
        with self.assertRaisesRegex(ValueError, 'TIME_BLOCK'): self.bot.command('/start')
        self.assertFalse(self.api.sends)
        self.bot.command('/resume_time',17); self.finish_time()
        self.assertNotIn('time_block',self.store.load())
        self.assertEqual(self.bot.state['telegram_offset'],18)
        self.bot.command('/start')

    def test_periodic_success_never_resumes_and_failed_resume_keeps_latch(self):
        self.semantics = False; self.bot.tick()
        self.bot.command('/resume_time'); self.finish_time()
        self.assertIn('MT5_TIMESTAMP', self.bot.state['notifications'][-1]['text'])
        self.semantics = True; self.bot.refresh_time(); self.finish_time()
        self.assertIn('time_block',self.store.load())
        with patch.object(self.broker.time_safety,'get',side_effect=TimeoutError):
            self.bot.command('/resume_time'); self.finish_time()
            self.assertIn('EXTERNAL_UTC', self.bot.state['notifications'][-1]['text'])
        self.assertIn('time_block',self.store.load())

    def test_stale_check_blocks_final_send(self):
        self.bot.command('/start'); self.api.rates[0]['time'] += 60
        with patch.object(self.bot,'dispatch'): self.bot.tick()
        original = self.api.order_check
        def slow(request):
            value = original(request); self.advance(61); return value
        with patch.object(self.api,'order_check',side_effect=slow): self.bot.dispatch()
        self.assertFalse(self.api.sends)
        self.assertEqual(self.store.load()['time_block']['reason'],'EXTERNAL_UTC_STALE')

    def test_block_keeps_oco_and_position_protection(self):
        self.start_pair(); self.semantics=False; self.api.fill()
        self.bot.tick(); self.bot.tick()
        self.assertEqual(self.api.orders,[])
        self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertIn('time_block',self.store.load())
        p=self.api.positions[0]; count=len(self.api.sends)
        self.bot._intent('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        self.bot.save('test_protection'); self.bot.dispatch()
        self.assertEqual(len(self.api.sends),count+1)
        self.assertEqual(self.api.sends[-1]['action'],self.api.TRADE_ACTION_SLTP)
        self.assertEqual(self.api.positions[0].sl,p.sl)
        self.assertEqual(self.api.positions[0].tp,p.tp)

    def test_block_prevents_reentry_but_books_closure(self):
        self.position(); self.semantics=False
        self.api.close_position(reason=4); count=len(self.api.sends)
        self.bot.tick(); self.bot.tick()
        self.assertEqual(len(self.api.sends),count)
        self.assertEqual(len(self.ledger()),1)
        self.assertEqual(self.bot.state['phase'],'NEXT_PAIR')

    def test_resume_does_not_clear_manual_or_stop(self):
        self.position(); self.bot._enter_manual(self.bot.state['cycle'],'MANUAL',self.api.now)
        self.bot.command('/stop'); self.semantics=False; self.bot.tick()
        self.semantics=True; self.bot.command('/resume_time'); self.finish_time()
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertTrue(self.bot.state['stop'])
        with self.assertRaises(ValueError): self.bot.command('/start')

    def test_failed_latch_write_never_sends(self):
        self.start_pair(); self.semantics=False; count=len(self.api.sends)
        def crash(event):
            if event=='time_block_latched': raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError): self.bot.dispatch()
        self.assertEqual(len(self.api.sends),count)
        self.assertNotIn('time_block',self.store.load())
        self.restart(); self.assertIn('time_block',self.store.load())

    def test_partial_tp_preserves_start_date_deadline_restart(self):
        self.position(); c=deepcopy(self.bot.state['cycle'])
        self.advance(30); self.api.close_position(profit=.5); self.bot.tick()
        self.assertEqual(self.bot.state['cycle']['branch'],'PARTIAL_TP'); self.restart()
        for name in ('branch_started_at_utc','branch_started_date_utc','branch_deadline_utc','started_at_utc'):
            self.assertEqual(self.bot.state['cycle'][name],c[name])

    def test_sl_starts_new_branch_without_changing_cycle_start(self):
        self.position(); c=deepcopy(self.bot.state['cycle'])
        self.advance(30); self.api.close_position(reason=4); self.bot.tick()
        current=self.bot.state['cycle']
        self.assertEqual(current['started_at_utc'],c['started_at_utc'])
        self.assertEqual(current['branch_started_at_utc'],self.api.now)
        self.assertEqual(current['branch_deadline_utc'],self.api.now+86400)
        self.restart(); self.assertEqual(self.bot.state['cycle']['branch_deadline_utc'],self.api.now+86400)

    def test_expiry_cancels_pending_even_with_time_block(self):
        self.start_pair(); self.advance(86400); self.bot.tick(); self.bot.tick()
        self.assertIn('time_block',self.store.load()); self.assertEqual(self.api.orders,[])
        self.assertEqual(len(self.api.sends),4); self.assertEqual(self.bot.state['phase'],'MANUAL')

    def test_unknown_cancel_not_replayed_after_restart(self):
        self.start_pair(); self.api.unknown=True; self.advance(86400)
        self.bot.tick(); count=len(self.api.sends); self.restart(); self.bot.tick(); self.bot.tick()
        self.assertEqual(len(self.api.sends),count); self.assertTrue(self.api.orders)

    def test_old_state_uses_original_deadline(self):
        self.start_pair(); c=self.bot.state['cycle']
        for key in ('branch_started_at_utc','branch_started_date_utc','branch_deadline_utc'): c.pop(key)
        self.bot.save('legacy_branch_time'); self.advance(86400); self.restart(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'MANUAL'); self.assertEqual(self.api.orders,[])

    def test_native_default_semantics_block_without_external_calls(self):
        from tradfi.time_profile import BYBIT_PROFILE
        from test_tradfi_native_service import NativeAPI
        self.broker.close(); self.store.close()
        self.api=NativeAPI(); self.store=Store(self.path.with_name('native.db'))
        config=dict(self.config,time_profile=BYBIT_PROFILE,execution_contract='cycle_scoped_identity_v1')
        self.broker=MT5EvidenceBroker(config,self.store,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.broker.connect()
        with patch('requests.get',side_effect=AssertionError('No network during guard')):
            self.bot=Controller(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
            self.assertEqual(self.store.load()['time_block']['reason'],'MT5_TIMESTAMP_SEMANTICS_UNVERIFIED')
            with self.assertRaisesRegex(ValueError,'TIME_BLOCK'): self.bot.command('/start')
            self.bot.tick()
            snap=self.bot.snapshot()
            self.assertIsNone(snap['quote']['utc_time'])
            self.assertEqual(snap['candle']['id'],0)
            self.assertEqual(snap['mt5_timestamp_semantics'],'UNVERIFIED')
            self.assertEqual(snap['mt5_display_time']['tick_time_msc'],self.api.symbol_info_tick('XAUUSD.s').time_msc)
        self.assertFalse(self.api.sends)

    def test_failed_resume_write_rolls_back_latch(self):
        self.semantics=False; self.bot.tick(); self.semantics=True
        def crash(event):
            if event=='time_resumed_explicitly': raise RuntimeError('power loss')
        self.store.fault=crash
        self.bot.command('/resume_time')
        with self.assertRaises(DurabilityError): self.finish_time()
        self.assertIn('time_block',self.store.load()); self.restart()
        with self.assertRaisesRegex(ValueError,'TIME_BLOCK'): self.bot.command('/start')

    def test_partial_tp_does_not_extend_expiry(self):
        self.position(); deadline=self.bot.state['cycle']['branch_deadline_utc']
        self.advance(30); self.api.close_position(profit=.5); self.bot.tick(); self.bot.tick()
        self.advance(deadline-self.api.now); self.bot.tick()
        self.assertEqual(self.bot.state['cycle']['manual_reason'],'CYCLE_24H_LIMIT')
        self.assertEqual(self.api.orders,[])

    def test_sl_branch_can_outlive_original_cycle_deadline(self):
        self.position(); original=self.bot.state['cycle']['deadline_utc']
        self.advance(30); self.api.close_position(reason=4); self.bot.tick()
        self.advance(original-self.api.now); self.restart(); self.bot.tick()
        self.assertFalse(self.bot.state['cycle'].get('manual',False))
        self.advance(30); self.bot.tick()
        self.assertEqual(self.bot.state['cycle']['manual_reason'],'CYCLE_24H_LIMIT')
