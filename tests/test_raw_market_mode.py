"""Raw MT5 contract: read-only API doubles and durable command-unit tests only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tradfi.control import ActionGateway
from tradfi.engine import Controller
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.market_time_state import bind_market_time_state, MarketTimeStateError
from tradfi.raw_market_time import RawMarketTime, closed_pair
from tradfi.storage import Store
from tradfi.time_profile import validate_market_time_mode, TimeProfileError
from test_m1_deferred_control import ControllerDouble
from test_tradfi_native_utc import UTCAPI


def config(**changes):
    value=dict(terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',
               backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',
               expected_server='Bybit-Live-6',expected_login=111111111,
               symbol='XAUUSD.s',magic=25101102,enable_trading=False,
               market_time_mode='mt5_raw_v1')
    value.update(changes)
    return value


class RawBrokerTests(unittest.TestCase):
    def setUp(self):
        self.api=UTCAPI();self.api.shift=7200
        self.mono=10.;self.wall=self.api.now-86400*30
        self.api.order_check=self.api.order_send=lambda *a,**k:self.fail('Trading forbidden in READ_ONLY tests')
        self.broker=MT5EvidenceBroker(config(),api=self.api,clock=lambda:self.wall,monotonic=lambda:self.mono)
        self.broker.connect();self.addCleanup(self.broker.close)

    def test_old_cached_quote_accepted_without_utc_claim_or_wall_comparison(self):
        first=self.broker.snapshot(require_fresh=True)
        self.wall+=86400*90;self.mono+=600
        second=self.broker.snapshot(require_fresh=True)
        self.assertEqual(first['quote'],second['quote'])
        self.assertIsNone(second['quote']['utc_time'])
        self.assertEqual(second['quote']['broker_time'],self.api.now+7200)
        self.assertEqual(second['mt5_timestamp_semantics'],'BROKER_RAW')
        self.assertEqual(second['candle']['id'],int((self.api.now+7200)//60)*60-60)
        self.assertIsNone(second['mt5_display_time']['server_display_utc_offset_seconds'])
        self.assertEqual(second['market_observation']['seconds_since_tick_change'],600)
        self.assertTrue(Controller._fresh_closed_candle(second))
        self.assertIsNone(self.broker.time_safety)
        json.dumps(second,allow_nan=False)
        with self.assertRaises(TimeProfileError):self.broker.market_time_to_utc(self.api.now)

    def test_repeated_reads_do_not_fake_new_tick(self):
        sequence=self.broker.raw_market.sequence
        for _ in range(5):self.broker.observe_raw_market()
        self.assertEqual(self.broker.raw_market.sequence,sequence)
        self.mono+=1;self.api.now+=1
        self.assertTrue(self.broker.observe_raw_market())
        self.assertEqual(self.broker.raw_market.sequence,sequence+1)

    def test_reconnect_accepts_same_cached_quote_but_does_not_call_it_fresh(self):
        sequence=self.broker.raw_market.sequence
        original=self.api.terminal_info
        self.api.terminal_info=lambda:original()._replace(connected=False)
        self.assertFalse(self.broker.observe_raw_market())
        self.api.terminal_info=original;self.mono+=10000
        self.assertTrue(self.broker.observe_raw_market())
        self.assertEqual(self.broker.raw_market.sequence,sequence+1)
        self.assertEqual(self.broker.raw_market.view()['seconds_since_tick_change'],10000)
        self.assertEqual(self.broker.raw_market.view()['freshness_policy'],'ACCEPT_LAST_OBSERVED')

    def test_restoring_bars_or_invalid_quote_is_not_a_tick_or_reconnection(self):
        sequence=self.broker.raw_market.sequence
        original=self.api.copy_rates_from_pos
        self.api.copy_rates_from_pos=lambda *a:None
        self.assertFalse(self.broker.observe_raw_market())
        self.api.copy_rates_from_pos=original
        self.assertTrue(self.broker.observe_raw_market())
        self.assertEqual(self.broker.raw_market.sequence,sequence)
        self.api.unavailable=True;self.assertFalse(self.broker.observe_raw_market())
        self.api.unavailable=False;self.assertTrue(self.broker.observe_raw_market())
        self.assertEqual(self.broker.raw_market.sequence,sequence)

    def test_real_observation_callback_does_not_release_queue_when_only_bars_recover(self):
        bot=ControllerDouble()
        bot.broker=SimpleNamespace(raw_market=self.broker.raw_market,time_safety=None)
        def guard():
            bot.waiting=not self.broker.observe_raw_market()
            return bot.waiting
        bot._time_guard=guard
        gateway=ActionGateway(bot,bot.execute)
        original=self.api.copy_rates_from_pos
        self.api.copy_rates_from_pos=lambda *a:None
        req=dict(id='raw_recovery_001',revision=0,action=dict(type='start',mode='auto'))
        self.assertEqual(gateway.apply(req)['status'],'WAITING_DATA')
        self.api.copy_rates_from_pos=original
        self.mono+=100;gateway.flush_waiting()
        self.assertFalse(bot.calls)
        self.assertEqual(self.broker.raw_market.view()['seconds_since_tick_change'],100)
        self.api.now+=1;gateway.flush_waiting()
        self.assertEqual(len(bot.calls),1)

    def test_m1_gap_not_filled_and_incoherent_pair_waits(self):
        original=self.api.copy_rates_from_pos
        def gap(*args):
            rows=original(*args);rows[0]['time']-=120;return rows
        self.api.copy_rates_from_pos=gap
        snap=self.broker.snapshot()
        self.assertEqual(snap['candle']['current_id']-snap['candle']['id'],180)
        self.assertTrue(Controller._fresh_closed_candle(snap))
        def wrong(*args):
            rows=original(*args);rows[-1]['time']-=60;return rows
        self.api.copy_rates_from_pos=wrong
        self.assertFalse(self.broker.observe_raw_market())

    def test_invalid_quote_and_time_units_still_rejected(self):
        original=self.api.symbol_info_tick
        for change in ({'bid':0.},{'bid':float('nan')},{'ask':1.},{'time_msc':1}):
            self.api.symbol_info_tick=lambda symbol:original(symbol)._replace(**change)
            self.assertFalse(self.broker.observe_raw_market(),change)
        self.api.symbol_info_tick=original
        self.assertTrue(self.broker.observe_raw_market())

    def test_monotonic_rollback_waits_but_elapsed_age_never_blocks(self):
        self.mono=9
        self.assertFalse(self.broker.observe_raw_market())
        self.mono=100000
        self.assertTrue(self.broker.observe_raw_market())

    def test_raw_readonly_preflight_never_labels_utc(self):
        result=self.broker.preflight()
        self.assertEqual(result['mode'],'READ_ONLY')
        self.assertEqual(result['market_data_time_status'],'BROKER_RAW')
        self.assertFalse(result['execution_ready'])
        self.assertFalse(result['adapter_trading_enabled'])
        self.assertIsNone(result['quote']['utc_time'])

    def test_inspect_entrypoint_and_launcher_preserve_raw_readonly_contract(self):
        from contextlib import redirect_stdout
        from unittest.mock import patch
        import io
        from tradfi.runtime import _run_mt5
        from tools.check_mt5 import summarize
        output=io.StringIO()
        with patch('tradfi.runtime.Store',side_effect=AssertionError('No state writes in inspect')),redirect_stdout(output):
            _run_mt5(config(enable_trading=True),True,evidence=True,api=self.api,
                     clock=lambda:self.wall,monotonic=lambda:self.mono)
        result=summarize(json.loads(output.getvalue()))
        self.assertEqual(result['status'],'WARN')
        self.assertEqual(result['market_data_time_status'],'BROKER_RAW')
        self.assertFalse(result['execution_ready'])
        self.assertNotIn('age_s',result['metrics'])

    def test_utc_diagnostic_explicitly_declines_raw_without_connecting(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from tools.check_mt5_utc import main
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'raw.json';path.write_text(json.dumps(config()))
            output=io.StringIO()
            with patch('sys.argv',['check_mt5_utc','--config',str(path)]),redirect_stdout(output), \
                 patch('tools.check_mt5_utc.MT5EvidenceBroker',side_effect=AssertionError('No UTC broker')):
                self.assertEqual(main(),2)
            self.assertEqual(json.loads(output.getvalue())['status'],'RAW_MODE_NO_UTC_CHECK')

    def test_readonly_controller_uses_raw_clock_without_time_block_or_send(self):
        from tradfi.recovery_v3 import ControlledRecoveryController
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.db')
            try:
                broker=MT5EvidenceBroker(config(),store=store,api=self.api,
                    clock=lambda:self.wall,monotonic=lambda:self.mono)
                broker.connect()
                try:
                    bot=ControlledRecoveryController(store,broker,clock=lambda:self.wall,monotonic=lambda:self.mono)
                    bot.tick()
                    self.assertFalse(bot.state.get('time_block'))
                    self.api.unavailable=True;bot.tick()
                    self.assertFalse(bot.state.get('time_block'))
                    self.api.unavailable=False;self.mono+=1000;bot.tick()
                    self.assertFalse(bot.state.get('time_block'))
                    self.assertTrue(broker.read_only)
                finally:broker.close()
            finally:store.close()


class RawControlTests(unittest.TestCase):
    def setUp(self):
        self.bot=ControllerDouble();self.bot.waiting=False
        self.raw=RawMarketTime(lambda:10.)
        self.tick=SimpleNamespace(time=1000,time_msc=1000000,bid=4000.,ask=4000.1)
        self.raw.observe(self.tick)
        self.bot.broker=SimpleNamespace(raw_market=self.raw,time_safety=None)
        self.gate=ActionGateway(self.bot,self.bot.execute)

    def request(self, suffix='0001', action=None):
        return dict(id='raw_'+suffix,revision=self.bot.state.get('control_revision',0),
                    action=action or dict(type='v3_entry',cycle_id='cycle-a',timing='current'))

    def test_always_accept_wait_next_observed_tick_once_and_preserve_raw_coordinate(self):
        req=self.request();result=self.gate.apply(req)
        self.assertEqual(result['status'],'WAITING_DATA')
        self.assertIsNone(result['deferred']['accepted_utc'])
        self.assertEqual(result['deferred']['accepted_market_time'],1000.)
        self.gate.flush_waiting();self.assertFalse(self.bot.calls)
        self.tick.time=900;self.tick.time_msc=900000 # delayed/regressed source data accepted
        self.raw.observe(self.tick)
        captured=[]
        self.gate.execute=lambda a:captured.append(self.bot._deferred_action_accepted_market_time)
        self.gate.flush_waiting();self.gate.flush_waiting()
        self.assertEqual(captured,[1000.])
        self.assertEqual(self.gate.apply(req)['status'],'APPLIED')

    def test_disconnected_command_waits_then_cached_reconnection_is_eligible(self):
        self.raw.invalidate(disconnected=True);self.bot.waiting=True
        req=self.request();self.assertEqual(self.gate.apply(req)['status'],'WAITING_DATA')
        self.gate.flush_waiting();self.assertFalse(self.bot.calls)
        self.raw.observe(self.tick);self.bot.waiting=False
        self.gate.flush_waiting();self.assertEqual(len(self.bot.calls),1)

    def test_stop_priority_restart_and_unknown_never_replay(self):
        req=self.request();self.gate.apply(req)
        self.gate.apply(self.request('0002',dict(type='command',text='/stop')))
        self.assertEqual(self.gate.apply(req)['status'],'REJECTED')
        req=self.request('0003');self.gate.apply(req)
        ActionGateway(self.bot,self.bot.execute)
        self.assertEqual(self.gate.apply(req)['status'],'REJECTED')
        req=self.request('0004');self.gate.apply(req)
        self.tick.bid+=.01;self.raw.observe(self.tick)
        def uncertain(a):raise TimeoutError()
        self.gate.execute=uncertain;self.gate.flush_waiting()
        self.assertEqual(self.gate.apply(req)['status'],'UNKNOWN')
        self.gate.execute=lambda a:self.fail('An uncertain action was replayed')
        self.gate.flush_waiting()

    def test_context_change_still_rejects_before_execution(self):
        req=self.request();self.gate.apply(req)
        self.bot.state['basket_settings']={'size':'2'}
        self.tick.bid+=.01;self.raw.observe(self.tick)
        self.gate.flush_waiting()
        self.assertEqual(self.gate.apply(req)['status'],'REJECTED')
        self.assertFalse(self.bot.calls)

    def test_invalid_optional_tick_identity_cannot_fake_a_new_observation(self):
        sequence=self.raw.sequence
        self.tick.last=float('nan')
        with self.assertRaises(ValueError):self.raw.observe(self.tick)
        self.assertEqual(self.raw.sequence,sequence)


class RawBindingTests(unittest.TestCase):
    def test_current_next_schedules_use_raw_acceptance_coordinate_not_vps(self):
        from tradfi.recovery_v3 import ControlledRecoveryController
        for timing,accepted,expected in (('current',None,1200),('next',None,1260),
                                         ('current',1000.,960),('next',1000.,1020)):
            cycle={}
            snap=dict(now=9999999999,market_time_mode='mt5_raw_v1',quote={'broker_time':1234.})
            bot=SimpleNamespace(_context=lambda a:(snap,cycle),
                _profile_request=lambda s,a:dict(stages=[{}]),_entry_ready=lambda s,c:True,
                state={'v3_settings':{'entry_distance':'1'}},save=lambda event:None,
                _market_now=Controller._market_now,_deferred_action_accepted_market_time=accepted,
                _deferred_action_accepted_utc=5000000000.)
            ControlledRecoveryController._request_entry(bot,dict(type='v3_entry',cycle_id='x',
                order_type='STOP',timing=timing,side='BUY'))
            self.assertEqual(cycle['v3_request']['bar_id'],expected)

    def test_mode_is_explicit_and_conflicting_offset_rejected(self):
        self.assertEqual(validate_market_time_mode({}),'fixed_utc_v1')
        for cfg in ({'market_time_mode':True},{'market_time_mode':'other'},
                    {'market_time_mode':'mt5_raw_v1','market_time_offset_seconds':7200}):
            with self.assertRaises(TimeProfileError):validate_market_time_mode(cfg)

    def test_existing_utc_or_legacy_database_not_reinterpreted(self):
        for bound in (False,True):
            with tempfile.TemporaryDirectory() as tmp:
                store=Store(Path(tmp)/'state.db')
                try:
                    if bound:bind_market_time_state(store,config(market_time_mode='fixed_utc_v1'))
                    store.db.execute("INSERT INTO audit(event,payload) VALUES('old','{}')")
                    before=list(store.db.iterdump())
                    with self.assertRaises(MarketTimeStateError):bind_market_time_state(store,config())
                    self.assertEqual(list(store.db.iterdump()),before)
                finally:store.close()

    def test_fresh_raw_binding_survives_reopen_but_not_switch_to_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.db')
            try:
                binding=bind_market_time_state(store,config())
                self.assertEqual(binding['timestamp_semantics'],'BROKER_RAW')
                self.assertEqual(binding,bind_market_time_state(store,config()))
                with self.assertRaises(MarketTimeStateError):
                    bind_market_time_state(store,config(market_time_mode='fixed_utc_v1'))
            finally:store.close()


if __name__=='__main__':unittest.main()
