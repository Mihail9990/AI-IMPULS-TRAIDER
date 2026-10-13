"""Freshness of process-local status, using offline native evidence and lifecycle fixtures."""
import json
import unittest
from unittest.mock import patch
import test_tradfi_native_service as native_fixtures
from test_tradfi_mt5 import record
from tradfi.observation import Observation
from tradfi.storage import DurabilityError


class ObservationClockTest(unittest.TestCase):
    def setUp(self):
        self.wall=1791800000.;self.mono=100.
        self.obs=Observation(lambda:self.wall,lambda:self.mono)
        self.snap=dict(positions=[],orders=[],obligations={},history_complete=False,
                       history_time_status='UNVERIFIED',quote={'utc_time':self.wall})
    def record(self):self.obs.record(self.snap,self.obs.begin())
    def test_initial_unavailable_then_fresh_then_stale(self):
        self.assertEqual(self.obs.view()['status'],'UNAVAILABLE')
        self.record();self.assertEqual(self.obs.view()['status'],'FRESH')
        self.wall+=6;self.mono+=6;v=self.obs.view()
        self.assertEqual(v['status'],'STALE');self.assertEqual(v['age_seconds'],6)
        self.assertIsNone(v['current']);self.assertEqual(v['last_good']['positions'],0)
    def test_failed_read_never_shows_old_zero_as_current(self):
        self.record();self.obs.fail();v=self.obs.view()
        self.assertEqual(v['status'],'UNAVAILABLE');self.assertIsNone(v['current'])
        self.assertEqual(v['last_good']['orders'],0)
    def test_forward_backward_and_monotonic_jumps(self):
        for wall,mono in [(30,0),(-30,0),(0,-1),(float('nan'),0)]:
            with self.subTest(wall=wall,mono=mono):
                self.setUp();self.record();self.wall+=wall;self.mono+=mono
                v=self.obs.view();self.assertEqual(v['status'],'UNAVAILABLE')
                self.assertIsNone(v['age_seconds']);self.assertEqual(v['reason'],'CLOCK_DISCONTINUITY')
    def test_jump_between_snapshots_not_relabelled_fresh(self):
        self.record();self.wall+=30;self.record()
        self.assertEqual(self.obs.view()['status'],'UNAVAILABLE')
        self.wall+=1;self.mono+=1;self.record()
        self.assertEqual(self.obs.view()['status'],'FRESH')
    def test_jump_during_read_and_slow_read(self):
        start=self.obs.begin();self.wall+=30
        self.obs.record(self.snap,start);self.assertEqual(self.obs.view()['status'],'UNAVAILABLE')
        self.setUp();start=self.obs.begin();self.wall+=6;self.mono+=6
        self.obs.record(self.snap,start);self.assertEqual(self.obs.view()['reason'],'SNAPSHOT_TOO_SLOW')
    def test_partial_invalid_observation_not_published(self):
        self.record();self.snap['positions']=None;self.record()
        self.assertEqual(self.obs.view()['status'],'UNAVAILABLE')
    def test_no_identifiers_or_raw_status_values(self):
        self.snap['account']='PRIVATE_ACCOUNT';self.snap['positions']=[{'id':'PRIVATE_POSITION'}]
        self.snap['obligations']={'PRIVATE_COMMAND':{'status':'SECRET_TOKEN_SENTINEL'}}
        self.record();text=json.dumps(self.obs.view())
        self.assertNotIn('PRIVATE',text);self.assertNotIn('SECRET',text)
        self.assertEqual(self.obs.view()['current']['obligations'],{'OTHER':1})
    def test_observation_freshness_does_not_claim_market_or_history_fresh(self):
        self.snap['quote']['utc_time']=None;self.record();v=self.obs.view()
        self.assertEqual(v['status'],'FRESH');self.assertEqual(v['quote_status'],'UNAVAILABLE')
        self.assertEqual(v['current']['history_time_status'],'UNVERIFIED')
        self.assertFalse(v['current']['history_complete'])


class NativeStatusTest(unittest.TestCase):
    setUp=native_fixtures.NativeServiceTest.setUp
    close=native_fixtures.NativeServiceTest.close
    open=native_fixtures.NativeServiceTest.open
    pair=native_fixtures.NativeServiceTest.pair
    position=native_fixtures.NativeServiceTest.position

    def view(self):return json.loads(self.bot.status().split('observation=',1)[1])
    def test_readonly_to_execution_uses_new_snapshot(self):
        self.open();self.bot.tick();old=self.view()['timestamp_utc']
        self.bot.command('/stop');self.open(True)
        self.assertEqual(self.view()['status'],'UNAVAILABLE')
        self.assertIsNone(self.view()['current'])
        self.api.now+=1;self.bot.tick();v=self.view()
        self.assertEqual(v['status'],'FRESH');self.assertNotEqual(v['timestamp_utc'],old)
        self.assertEqual(self.bot.state['phase'],'PAUSED');self.assertTrue(self.bot.state['stop'])
        self.assertFalse(self.api.sends);self.assertFalse(self.api.checks)
    def test_execution_idle_and_paused_heartbeat(self):
        self.open(True)
        for phase in ['IDLE','PAUSED']:
            self.bot.state['phase']=phase;self.bot.save('telegram_command')
            self.api.now+=1;self.bot.tick()
            self.assertEqual(self.view()['status'],'FRESH')
            self.assertEqual(self.view()['current']['positions'],0)
        self.assertFalse(self.api.sends)
    def test_new_foreign_positions_and_orders_observed_readonly(self):
        self.open();self.bot.tick()
        self.api.positions=[record(ticket=501,identifier=501,symbol='XAUUSD.s',magic=123,type=0,
            volume=.1,price_open=4000.,sl=3998.,tp=4001.)]
        self.api.orders=[record(ticket=502,position_id=0,symbol='XAUUSD.s',magic=123,comment='manual',
            type=4,state=1,volume_initial=.1,volume_current=.1,price_open=4002.,sl=4000.,tp=4003.,
            time_setup_msc=self.api.now*1000,time_done_msc=0)]
        self.bot.tick();v=self.view()
        self.assertEqual(v['status'],'FRESH')
        self.assertEqual((v['current']['positions'],v['current']['orders']),(1,1))
        self.assertFalse(self.api.sends);self.assertFalse(self.api.checks)
    def test_active_cycle_observation_and_post_send_invalidation(self):
        self.position();self.bot.tick();v=self.view()
        self.assertEqual(v['status'],'FRESH');self.assertEqual(v['current']['positions'],1)
        self.assertIn('cycle_position=',self.bot.status())
        self.assertEqual(self.bot.state['phase'],'POSITION')
        count=len(self.api.sends)
        with patch.object(self.broker,'snapshot',side_effect=AssertionError('status cannot read')):
            self.bot.command('/status');self.bot.status()
        self.assertEqual(len(self.api.sends),count)
    def test_pair_send_marks_old_flat_unavailable_until_readback(self):
        self.pair()
        self.assertEqual(self.view()['status'],'UNAVAILABLE')
        self.assertEqual(self.view()['reason'],'AWAITING_POST_MUTATION_READBACK')
        self.bot.tick();self.assertEqual(self.view()['current']['orders'],2)
    def test_disconnect_failure_and_recovery(self):
        self.open();self.bot.tick()
        self.api.terminal=self.api.terminal._replace(connected=False)
        self.bot.tick();v=self.view()
        self.assertEqual(v['status'],'UNAVAILABLE');self.assertIsNone(v['current'])
        self.api.terminal=self.api.terminal._replace(connected=True)
        self.bot.tick();self.assertEqual(self.view()['status'],'FRESH')
    def test_partial_changed_snapshot_not_current(self):
        self.open();self.bot.tick()
        with patch.object(self.broker.api,'positions_get',return_value=None):self.bot.tick()
        self.assertEqual(self.view()['status'],'UNAVAILABLE')
        self.assertIsNone(self.view()['current'])
    def test_orders_changing_mid_snapshot_do_not_publish_partial_counts(self):
        self.open();self.bot.tick();calls=[]
        def orders(**kwargs):
            calls.append(1)
            return [] if len(calls)==1 else [record(ticket=999)]
        with patch.object(self.broker.api,'orders_get',side_effect=orders):self.bot.tick()
        self.assertEqual(self.view()['status'],'UNAVAILABLE')
        self.assertIsNone(self.view()['current'])
    def test_execution_foreign_position_is_observed_and_guard_still_pauses(self):
        self.open(True);self.bot.tick()
        self.api.positions=[record(ticket=501,identifier=501,symbol='XAUUSD.s',magic=123,type=0,
            volume=.1,price_open=4000.,sl=3998.,tp=4001.)]
        self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertEqual(self.view()['current']['positions'],1)
        self.assertFalse(self.api.sends)
    def test_exception_payload_never_appears_in_status_or_notifications(self):
        self.open();self.bot.tick()
        with patch.object(self.broker,'snapshot',side_effect=OSError('SECRET_ACCOUNT_TOKEN')):
            self.bot.tick()
        self.bot.command('/status')
        self.assertNotIn('SECRET_ACCOUNT_TOKEN',self.bot.status())
        self.assertNotIn('SECRET_ACCOUNT_TOKEN',json.dumps(self.bot.state['notifications']))
    def test_invalid_complete_snapshot_does_not_replace_last_good(self):
        self.open();self.bot.tick();snap=self.bot.snapshot()
        snap['hedging']=False
        with patch.object(self.broker,'snapshot',return_value=snap),self.assertRaises(ValueError):self.bot.snapshot()
        self.assertEqual(self.view()['status'],'UNAVAILABLE')
    def test_restart_ignores_legacy_service_and_cycle_observation(self):
        self.position();self.bot.tick()
        self.bot.state['service_observation']={'utc':self.api.now,'positions':99}
        self.bot.state['cycle']['manual_observation']={'positions':['PRIVATE_OLD_CACHE']}
        self.bot.save('telegram_command');self.open(True)
        v=self.view();self.assertEqual(v['status'],'UNAVAILABLE');self.assertIsNone(v['current'])
        self.assertNotIn('PRIVATE_OLD_CACHE',self.bot.status())
        self.bot.tick();self.assertEqual(self.view()['current']['positions'],1)
    def test_status_uses_no_additional_api_calls_and_no_send(self):
        self.open();self.bot.tick();before=(len(self.api.calls),len(self.api.sends),len(self.api.checks))
        with patch.object(self.broker,'snapshot',side_effect=AssertionError('unexpected MT5 read')):
            self.bot.command('/status');self.bot.command('/status')
        self.assertEqual(before,(len(self.api.calls),len(self.api.sends),len(self.api.checks)))
    def test_ledger_failure_invalidates_display_and_still_propagates(self):
        self.open();self.bot.tick()
        with patch.object(self.store,'verify_deal_ledger',side_effect=DurabilityError('safe')):
            with self.assertRaises(DurabilityError):self.bot.snapshot()
        self.assertEqual(self.view()['status'],'UNAVAILABLE')

if __name__=='__main__':unittest.main()
