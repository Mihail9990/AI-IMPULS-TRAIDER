"""M1 rollover safety with offline production MT5/time/controller paths."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from tradfi.native_time import NativeUTCVerifier, M1_TRANSITION_WAIT
from tradfi.time_safety import TimeSafety
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.recovery_v3 import ControlledRecoveryController
from tradfi.control import ActionGateway
from tradfi.storage import Store
from test_tradfi_native_utc import UTCAPI
from test_tradfi_mt5 import record


class RolloverAPI(UTCAPI):
    def __init__(self):
        super().__init__()
        self.boundary=int(self.now//60)*60+60
        self.now=self.boundary-.2
        self.tick_stamp=None
        self.bar_current=None
        self.bar_override=None
        self.tick_invalid=False

    def symbol_info_tick(self, symbol):
        self.reads+=1
        if self.unavailable:return None
        stamp=self.now if self.tick_stamp is None else self.tick_stamp
        return record(bid=4000.,ask=4000.1,time=int(stamp),time_msc=float('nan') if self.tick_invalid else int(stamp*1000))

    def copy_rates_from_pos(self,symbol,timeframe,start,count):
        self.reads+=1;self.now+=self.delay
        if self.unavailable:return None
        if self.bar_override is not None and count==2:return [dict(time=t) for t in self.bar_override]
        current=int(self.now//60)*60 if self.bar_current is None else self.bar_current
        return [dict(time=current-60*i,open=3999.,close=4000.,high=4001.,low=3998.) for i in reversed(range(start,start+count))]

    def lag(self,seconds=.2,*,tick=True,bars=True):
        self.now=self.boundary+seconds
        self.tick_stamp=self.boundary-.1 if tick else None
        self.bar_current=self.boundary-60 if bars else None

    def fresh(self,seconds=.5):
        self.now=self.boundary+seconds;self.tick_stamp=None;self.bar_current=None


class TransitionVerifierTests(unittest.TestCase):
    def setUp(self):
        self.api=RolloverAPI()
        self.b=SimpleNamespace(api=self.api,symbol='XAUUSD.s',monotonic=lambda:self.api.now,max_age=5)
        self.safety=TimeSafety(clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.safety.anchor=(self.api.now,self.api.now)
        self.safety.result=dict(ok=True,checked_monotonic=self.api.now)
        self.verifier=NativeUTCVerifier(self.b,self.safety)
        self.safety.semantics_verifier=self.verifier
        self.assertTrue(self.verifier())

    def test_three_recognized_handover_shapes_fail_closed_without_latch_reason(self):
        for tick,bars in ((True,True),(False,True),(True,False)):
            with self.subTest(tick=tick,bars=bars):
                self.api.lag(tick=tick,bars=bars)
                self.assertFalse(self.verifier())
                self.assertEqual(self.verifier.reason,M1_TRANSITION_WAIT)
                self.assertEqual(self.safety.check(),M1_TRANSITION_WAIT)

    def test_repeated_reads_do_not_extend_boundary_deadline(self):
        for seconds in (.2,.5,1.,2.9,2.999):
            self.api.lag(seconds)
            self.assertFalse(self.verifier())
            self.assertEqual(self.verifier.reason,M1_TRANSITION_WAIT)
        self.api.lag(3.)
        self.assertFalse(self.verifier())
        self.assertEqual(self.verifier.reason,'MT5_UTC_WAIT_FRESH_DATA')
        self.assertTrue(self.verifier.transition_expired)

    def test_first_fresh_sample_at_or_after_deadline_still_requires_latch(self):
        self.api.lag();self.assertFalse(self.verifier())
        self.api.fresh(3.)
        self.assertFalse(self.verifier());self.assertTrue(self.verifier.transition_expired)
        self.assertFalse(self.verifier())
        self.verifier.acknowledge_transition_timeout()
        self.assertTrue(self.verifier())

    def test_fresh_at_2999ms_recovers(self):
        self.api.lag();self.assertFalse(self.verifier())
        self.api.fresh(2.999)
        self.assertTrue(self.verifier());self.assertIsNone(self.safety.check())

    def test_startup_in_previous_minute_is_not_a_proven_transition(self):
        self.api.lag()
        verifier=NativeUTCVerifier(self.b,self.safety)
        self.assertFalse(verifier());self.assertEqual(verifier.reason,'MT5_UTC_WAIT_FRESH_DATA')

    def test_regression_after_current_minute_verified_is_immediate_fault(self):
        self.api.fresh();self.assertTrue(self.verifier())
        self.api.lag(.8)
        self.assertFalse(self.verifier());self.assertEqual(self.verifier.reason,'MT5_UTC_WAIT_FRESH_DATA')

    def test_nontransition_faults_are_never_deferred(self):
        for fault in ('missing','malformed','gap','duplicate','two_minutes','future','timeout','reference'):
            with self.subTest(fault=fault):
                self.setUp();self.api.lag()
                self.assertFalse(self.verifier());self.assertEqual(self.verifier.reason,M1_TRANSITION_WAIT)
                if fault=='missing':self.api.unavailable=True
                if fault=='malformed':self.api.tick_invalid=True
                if fault=='gap':self.api.bar_override=[self.api.boundary-180,self.api.boundary-60]
                if fault=='duplicate':self.api.bar_override=[self.api.boundary-60]*2
                if fault=='two_minutes':self.api.bar_current=self.api.boundary-120
                if fault=='future':self.api.tick_stamp=self.api.now+2.001
                if fault=='timeout':self.api.delay=2.01
                if fault=='reference':self.safety.result['ok']=False
                self.assertFalse(self.verifier());self.assertNotEqual(self.verifier.reason,M1_TRANSITION_WAIT)

    def test_worker_does_not_touch_owner_observation_or_deadline(self):
        self.api.lag();self.verifier()
        before=(deepcopy(self.verifier.observation),self.verifier._transition,self.verifier.reason,self.api.reads)
        result=[]
        worker=threading.Thread(target=lambda:result.append(self.verifier()))
        worker.start();worker.join()
        self.assertEqual(result,[False])
        self.assertEqual(before,(self.verifier.observation,self.verifier._transition,self.verifier.reason,self.api.reads))

    def test_reference_refresh_cannot_extend_monotonic_deadline(self):
        self.api.lag(.2);self.verifier()
        self.api.now=self.api.boundary+3.1
        # An allowed small reference correction moves projected UTC backwards.
        self.safety.anchor=(self.api.boundary+2.7,self.api.now)
        self.safety.result['checked_monotonic']=self.api.now
        self.assertFalse(self.verifier());self.assertTrue(self.verifier.transition_expired)


class TransitionNativeControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.api=RolloverAPI()
        self.api.info=self.api.info._replace(trade_stops_level=1)
        self.store=Store(Path(self.tmp.name)/'state.db');self.addCleanup(self.store.close)
        cfg=dict(terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,enable_trading=True,execution_contract='cycle_scoped_identity_v1',legacy_ea_removed=True,legacy_state_reconciled=True)
        self.broker=MT5EvidenceBroker(cfg,self.store,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.broker.connect();self.addCleanup(self.broker.close)
        safety=self.broker.time_safety
        safety.anchor=(self.api.now,self.api.now);safety.result=dict(ok=True,checked_monotonic=self.api.now)
        self.bot=ControlledRecoveryController(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.bot.command('/start')

    def test_auto_waits_without_pause_then_uses_fresh_close_once(self):
        self.api.lag()
        for _ in range(3):self.bot.tick()
        self.assertNotIn('time_block',self.bot.state)
        self.assertEqual(self.bot.state['phase'],'WAIT_CANDLE')
        self.assertFalse(self.api.sends)
        self.api.fresh(.5)
        self.bot.tick();self.bot.tick()
        self.assertNotIn('time_block',self.bot.state)
        self.assertEqual(len(self.api.orders),1)
        sends=deepcopy(self.api.sends)
        self.bot.tick();self.assertEqual(self.api.sends,sends)

    def test_timeout_latches_even_if_first_fresh_read_is_late(self):
        self.api.lag();self.bot.tick()
        self.api.fresh(3.)
        self.bot.tick();self.bot.tick()
        self.assertEqual(self.bot.state['time_block']['reason'],'MT5_UTC_WAIT_FRESH_DATA')
        self.assertFalse(self.api.sends)
        self.assertEqual(self.store.load()['time_block'],self.bot.state['time_block'])

    def test_old_latch_is_never_released_by_transition_recovery(self):
        self.bot.state['time_block']={'reason':'OLD_FAULT'};self.bot.save('test_old_latch')
        self.api.lag();self.bot.tick();self.api.fresh();self.bot.tick()
        self.assertEqual(self.bot.state['time_block'],{'reason':'OLD_FAULT'})
        self.assertFalse(self.api.sends)

    def test_pending_ready_intent_is_not_sent_or_cancelled_during_wait(self):
        cid=self.bot._intent('STOP',side='BUY',trigger='4001',price='4001',size='.01',sl='3998',tp='4001.25')
        self.bot.save('test_ready')
        self.api.lag();self.bot.dispatch()
        self.assertEqual(self.bot.state['commands'][cid]['status'],'READY')
        self.assertFalse(self.api.sends)
        self.api.now=self.api.boundary+3.;self.bot.dispatch()
        self.assertEqual(self.bot.state['commands'][cid]['status'],'CANCELLED')
        self.assertFalse(self.api.sends)

    def test_guard_race_during_intent_preparation_is_not_a_false_manual_pause(self):
        self.api.lag()
        cid=self.bot._intent('STOP',side='BUY',trigger='4001',price='4001',size='.01',sl='3998',tp='4001.25')
        self.bot.save('test_intent_wait');self.bot.dispatch()
        self.assertEqual(self.bot.state['commands'][cid]['status'],'READY')
        self.assertNotIn('time_block',self.bot.state)
        self.assertFalse(self.api.sends)

    def test_other_error_during_grace_latches_immediately(self):
        self.api.lag();self.bot.tick()
        self.api.tick_invalid=True;self.bot.tick()
        self.assertEqual(self.bot.state['time_block']['reason'],'MT5_UTC_INVALID_TIMESTAMP')
        self.assertFalse(self.api.sends)

    def test_fill_is_reconciled_and_existing_protection_survives_wait(self):
        self.api.fresh();self.bot.tick();self.api.fill();self.bot.tick()
        protections=deepcopy(self.api.positions)
        self.api.boundary+=60
        self.api.now=self.api.boundary-.2
        s=self.broker.time_safety;s.anchor=(self.api.now,self.api.now);s.result=dict(ok=True,checked_monotonic=self.api.now)
        self.bot.tick()
        self.api.lag();sends=deepcopy(self.api.sends)
        self.bot.tick();self.bot.tick()
        self.assertNotIn('time_block',self.bot.state)
        self.assertEqual(self.api.positions,protections)
        self.assertEqual(self.api.sends,sends)
        self.assertEqual(len(self.bot.state['cycle']['tickets']),1)

    def test_gateway_accepts_start_during_wait_and_deduplicates(self):
        self.bot.command('/stop')
        gateway=ActionGateway(self.bot,execute=self.bot.action)
        self.api.lag()
        request=dict(id='start-during-transition',revision=self.bot.state.get('control_revision',0),action=dict(type='command',text='/start'))
        result=gateway.apply(request)
        self.assertEqual(result['status'],'WAITING_DATA')
        self.assertEqual(gateway.apply(request)['status'],'WAITING_DATA')
        self.assertFalse(self.api.sends)
        self.api.fresh();gateway.flush_waiting()
        self.assertEqual(self.bot.state['control_receipts'][request['id']]['status'],'APPLIED')
        gateway.flush_waiting();self.assertFalse(self.api.sends)


class TransitionV3CommandTests(unittest.TestCase):
    def setUp(self):
        from test_recovery_v3 import V3
        self.fixture=V3();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.fixture.loss();self.fixture.profile(2)
        self.f=self.fixture.f
        self.safety=SimpleNamespace(reason=None)
        self.safety.check=lambda:self.safety.reason
        self.safety.poll_refresh=lambda:None
        self.safety.trusted_utc=lambda:self.f.b.data['now']
        self.f.b.time_safety=self.safety
        self.gateway=ActionGateway(self.f.bot,execute=self.f.bot.action)

    def action(self,kind='v3_entry',**kwargs):
        return dict(type=kind,cycle_id=self.f.bot.state['cycle']['id'],**kwargs)

    def submit(self,action,name='deferred-v3-request'):
        return self.gateway.apply(dict(id=name,revision=self.f.bot.state.get('control_revision',0),action=action))

    def test_manual_market_is_accepted_then_created_once_after_fresh_tick(self):
        self.safety.reason=M1_TRANSITION_WAIT
        result=self.submit(self.action(order_type='MARKET',timing='now',side='SELL'))
        self.assertEqual(result['status'],'WAITING_DATA')
        self.f.tick(3);self.assertFalse(self.f.b.data['positions'])
        self.assertNotIn('time_block',self.f.bot.state)
        self.safety.reason=None;self.gateway.flush_waiting();self.f.tick(4)
        self.assertEqual(len(self.f.b.data['positions']),2)
        self.assertFalse(self.f.bot.state['cycle'].get('manual'))
        commands=deepcopy(self.f.b.commands)
        self.gateway.flush_waiting();self.f.tick(3)
        self.assertEqual(self.f.b.commands,commands)

    def test_existing_scheduled_candle_survives_transition(self):
        self.submit(self.action(order_type='STOP',timing='current',side='BUY'))
        target=self.f.bot.state['cycle']['v3_request']['bar_id']
        self.f.b.data.update(now=target+60.2,candle=dict(id=0,open='4001',close='4000'))
        self.safety.reason=M1_TRANSITION_WAIT;self.f.tick(3)
        self.assertEqual(self.f.bot.state['cycle']['v3_request']['bar_id'],target)
        self.assertFalse(self.f.b.data['orders'])
        self.safety.reason=None
        self.f.b.data.update(now=target+60.5,candle=dict(id=target,open='4001',close='4000'))
        self.f.b.quote('3999.7','3999.8');self.f.tick(3)
        self.assertEqual(len(self.f.b.data['orders']),2)
        self.assertNotIn('v3_request',self.f.bot.state['cycle'])
        self.assertEqual({o['side'] for o in self.f.b.data['orders']},{'SELL'})

    def test_queued_current_and_next_preserve_acceptance_minute(self):
        self.safety.reason=M1_TRANSITION_WAIT
        self.f.b.data['now']=180.2
        self.submit(self.action(order_type='STOP',timing='next',side='BUY'))
        self.f.b.data['now']=181.;self.safety.reason=None
        self.gateway.flush_waiting()
        self.assertEqual(self.f.bot.state['cycle']['v3_request']['bar_id'],240)

    def test_stop_during_wait_prevents_later_entry(self):
        self.safety.reason=M1_TRANSITION_WAIT
        self.submit(self.action(order_type='MARKET',timing='now',side='SELL'))
        self.submit(dict(type='command',text='/stop'),name='stop-waiting-entry')
        self.safety.reason=None;self.gateway.flush_waiting();self.f.tick(4)
        self.assertFalse(self.f.b.data['positions']);self.assertFalse(self.f.b.data['orders'])
        self.assertEqual(self.f.bot.state['control_receipts']['deferred-v3-request']['status'],'REJECTED')


class TransitionBrokerControllerTests(TransitionNativeControllerTests):
    # Inherited production-native setup and safety tests also cover the final
    # broker gate. Extra cases force the boundary inside order_check itself.
    def prepare_race(self):
        self.api.now=self.api.boundary-.1
        snap=self.bot.snapshot()
        self.bot._new(snap)
        self.bot._arm(snap,'initial',{'BUY':'4001'})
        original=self.api.order_check
        def crossing(request):
            result=original(request);self.api.lag(.2)
            return result
        from unittest.mock import patch
        with patch.object(self.api,'order_check',side_effect=crossing):self.bot.dispatch()
        command=next(c for c in self.bot.state['commands'].values() if c['kind']=='STOP')
        self.assertEqual(command['status'],'READY')
        self.assertTrue(self.broker.entry_deferred(command['id']))
        self.assertFalse(self.api.sends)
        return command

    def test_mid_check_wait_has_no_false_manual_pause_and_sends_once(self):
        command=self.prepare_race()
        self.bot.tick()
        self.assertNotIn('time_block',self.bot.state)
        self.assertFalse(self.bot.state['cycle'].get('manual'))
        self.assertEqual(command['status'],'READY')
        self.api.fresh(.5);self.bot.dispatch();self.bot.tick();self.bot.tick()
        self.assertEqual(len(self.api.sends),1)
        self.assertEqual(len(self.api.orders),1)
        self.assertFalse(self.bot.state['cycle'].get('manual'))
        self.bot.dispatch();self.assertEqual(len(self.api.sends),1)

    def test_mid_check_wait_times_out_without_send(self):
        command=self.prepare_race()
        self.api.now=self.api.boundary+3.;self.bot.tick()
        self.assertEqual(self.bot.state['time_block']['reason'],'MT5_UTC_WAIT_FRESH_DATA')
        self.assertEqual(command['status'],'CANCELLED')
        self.assertFalse(self.api.sends)

    def test_restart_cancels_proven_deferred_row_without_replay(self):
        command=self.prepare_race()
        self.api.fresh(.5)
        restarted=ControlledRecoveryController(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.assertEqual(restarted.state['commands'][command['id']]['status'],'CANCELLED')
        restarted.tick();restarted.dispatch()
        self.assertFalse(self.api.sends)

    def test_explicit_cancel_is_not_resurrected_when_stop_flag_clears(self):
        command=self.prepare_race()
        self.bot.command('/stop')
        self.assertEqual(command['status'],'CANCELLED')
        self.bot.state['stop']=False;self.bot.save('test_stop_flag_cleared')
        self.api.fresh(.5)
        self.bot.tick();self.bot.tick();self.bot.dispatch()
        self.assertEqual(command['status'],'CANCELLED')
        self.assertFalse(self.api.sends)
