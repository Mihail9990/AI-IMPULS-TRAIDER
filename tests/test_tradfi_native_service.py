"""Native module import and production time profile, tested entirely offline."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import tempfile
import threading
from types import ModuleType
import unittest
from unittest.mock import patch

from test_tradfi_evidence_controller import TradingAPI
from test_tradfi_mt5 import record
from time_fixtures import verified_time
from tradfi.engine import Controller
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.runtime import _run_mt5
from tradfi.storage import Store
from tradfi.time_profile import BYBIT_PROFILE, offset_at, TimeProfileError


class NativeAPI(TradingAPI):
    def __init__(self):
        super().__init__()
        self.now=int(self.now//60)*60+10
        self.rates=[dict(time=int(self.now//60)*60-60,close=4000.)]
    def symbol_info_tick(self,symbol):
        raw=self.now+offset_at(self.now)
        return record(bid=self.tick.bid,ask=self.tick.ask,time=int(raw),time_msc=int(raw*1000))
    def copy_rates_from_pos(self,*args):
        return [dict(r,time=r['time']+offset_at(r['time'])) for r in self.rates]
    def module(self):
        module=ModuleType('MetaTrader5')
        for name in dir(self):
            if not name.startswith('_'): setattr(module,name,getattr(self,name))
        return module


class NativeServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.api=NativeAPI()
        self.config=dict(backend='mt5_direct_v1',time_profile=BYBIT_PROFILE,
            expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,
            terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',enable_trading=False,
            state_file=str(self.root/'state.db'),log_file=str(self.root/'diagnostic.log'))
        time_patch=patch('tradfi.time_safety.TimeSafety', side_effect=verified_time)
        time_patch.start(); self.addCleanup(time_patch.stop)
        self.store=None; self.broker=None
        self.addCleanup(self.close)
    def close(self):
        if self.broker: self.broker.close(); self.broker=None
        if self.store: self.store.close(); self.store=None
    def open(self,execute=False):
        self.close()
        self.store=Store(self.config['state_file'])
        config=dict(self.config,enable_trading=execute)
        if execute: config.update(execution_contract='cycle_scoped_identity_v1',legacy_ea_removed=True,legacy_state_reconciled=True)
        # Exercise the official import boundary, not the TEST_PROFILE path.
        self.broker=MT5EvidenceBroker(config,self.store,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        with patch('tradfi.mt5_adapter.importlib.import_module',return_value=self.api.module()) as importer:
            self.broker.connect(); importer.assert_called_once_with('MetaTrader5')
        self.bot=Controller(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
    def pair(self):
        self.open(True); self.bot.command('/start')
        self.api.now+=60; self.api.rates[0]['time']+=60; self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'PAIR')
    def position(self):
        self.pair(); self.api.fill(); self.bot.tick(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'POSITION')
    def test_readonly_uses_real_profile_and_never_sends_or_creates_intents(self):
        self.open()
        self.bot.command('/settings'); self.bot.command('/status'); self.bot.tick()
        self.assertIn('READ_ONLY',self.bot.status())
        snap=self.bot.snapshot()
        self.assertEqual(snap['quote']['utc_time'],self.api.now)
        self.assertEqual(snap['candle']['id'],self.api.rates[0]['time'])
        self.assertFalse(snap['history_complete']); self.assertEqual(snap['history_time_status'],'UNVERIFIED')
        with self.assertRaisesRegex(ValueError,'READ_ONLY'): self.bot.command('/start')
        with self.assertRaisesRegex(ValueError,'READ_ONLY'): self.bot._intent('STOP')
        with self.assertRaises(ValueError): self.bot.command('/reset_manual')
        self.assertFalse(self.bot.state['commands']); self.assertFalse(self.api.sends); self.assertFalse(self.api.checks)
    def test_native_execution_full_cycle_atomic_ledger_and_restart(self):
        self.position(); deal=self.api.close_position(); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'COOLDOWN')
        self.assertEqual(self.bot.state['seen_deals'],[str(deal.ticket)])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM consumed_deals').fetchone()[0],1)
        previous=deepcopy(self.bot.state); self.open(True); self.bot.tick()
        self.assertEqual(previous,self.bot.state)
    def test_readonly_restart_does_not_resume_active_cycle_or_dispatch(self):
        self.position(); before=len(self.api.sends); c=deepcopy(self.bot.state['cycle'])
        self.open(False); self.bot.tick(); self.bot.dispatch()
        self.assertEqual(len(self.api.sends),before); self.assertEqual(self.bot.state['cycle']['recovery'],c['recovery'])
        self.assertFalse(self.broker.enable_trading)
    def test_native_reset_reads_real_contract_without_mutations(self):
        self.position(); self.bot._enter_manual(self.bot.state['cycle'],'USER_MANUAL',self.api.now)
        self.api.close_position(reason=0,profit=-7)
        before=len(self.api.sends); self.open(False); self.bot.command('/reset_manual',8)
        self.assertEqual(self.bot.state['phase'],'WAIT_START'); self.assertEqual(len(self.api.sends),before)
        self.assertEqual(self.bot.state['completed'][-1]['realized'],'-7')
        with self.assertRaisesRegex(ValueError,'READ_ONLY'): self.bot.command('/start')
    def test_unknown_missing_response_never_replayed_in_native_bridge(self):
        self.api.unknown=True; self.pair(); before=len(self.api.sends)
        self.bot.tick(); self.open(True); self.bot.tick()
        self.assertEqual(len(self.api.sends),before)
        self.assertTrue(any(c['status']=='UNKNOWN' for c in self.bot.state['commands'].values()))
    def test_activation_requires_config_contract_and_bound_account(self):
        for updates in ({'enable_trading':True},{'enable_trading':False,'execution_contract':'cycle_scoped_identity_v1'}):
            with self.subTest(updates=updates), patch('tradfi.runtime.Store') as store:
                with self.assertRaises(TimeProfileError):
                    _run_mt5(dict(self.config,**updates),False,service=True,execute=True)
                store.assert_not_called()
    def test_readonly_adapter_denies_direct_submit_before_check_or_send(self):
        self.open()
        with self.assertRaisesRegex(OSError,'disabled'): self.broker.submit({'id':'a'*24})
        self.assertFalse(self.api.sends); self.assertFalse(self.api.checks)
    def test_native_market_time_stale_closed_bar_is_not_used(self):
        self.open(); self.api.rates[0]['time']-=60
        with self.assertRaisesRegex(OSError,'Latest closed'): self.bot.snapshot()
        self.assertFalse(self.api.sends)
    def test_native_command_exception_never_enters_telegram_or_state(self):
        from tradfi.telegram import Telegram
        from test_tradfi_sendlog import HTTP
        self.position(); self.bot._enter_manual(self.bot.state['cycle'],'MANUAL',self.api.now)
        self.api.close_position(reason=0,profit=-7); self.open(False)
        secret='private-account-token-DO-NOT-EXPORT'
        tg=Telegram('fake','123',HTTP(),document_session=HTTP()); self.addCleanup(tg.close)
        tg.incoming.put(dict(update_id=100,message={'chat':{'id':123},'from':{'id':123},'text':'/reset_manual'}))
        with patch.object(self.broker.api,'history_orders_get',side_effect=OSError(secret)): tg.process(self.bot)
        self.assertNotIn(secret,json.dumps(self.store.load()))
        self.assertEqual(self.bot.state['phase'],'MANUAL')
    def test_native_tick_exception_never_enters_notification_or_state(self):
        self.open()
        secret='private-account-token-DO-NOT-EXPORT'
        with patch.object(self.broker.api,'orders_get',side_effect=ValueError(secret)): self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertNotIn(secret,json.dumps(self.store.load()))
    def test_native_profit_exception_on_reentry_never_enters_notification(self):
        self.position(); self.api.close_position(reason=4); self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'NEXT_PAIR')
        secret='private-profit-api-payload-DO-NOT-EXPORT'
        with patch.object(self.broker.api,'order_calc_profit',side_effect=ValueError(secret)): self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertNotIn(secret,json.dumps(self.store.load()))
    def test_native_terminal_permissions_are_never_enabled_by_bridge(self):
        self.api.terminal=self.api.terminal._replace(trade_allowed=False)
        self.pair()
        self.assertFalse(self.api.sends); self.assertFalse(self.api.checks)
        self.assertFalse(self.api.terminal.trade_allowed)
    def test_native_runtime_execute_wires_same_controller_after_all_optins(self):
        stopped=threading.Event()
        api=self.api
        class TelegramDouble:
            def __init__(self,*args,**kwargs): self.http=self
            def updates(self,*args): return []
            def start(self,*args): pass
            def close(self): pass
            def process(self,controller):
                controller.command('/start auto'); api.now+=60; api.rates[0]['time']+=60; stopped.set()
        config=dict(self.config,enable_trading=True,execution_contract='cycle_scoped_identity_v1',
                    legacy_ea_removed=True,legacy_state_reconciled=True)
        real_import=importlib.import_module
        with patch.dict('os.environ',{'TRADFI_TELEGRAM_TOKEN':'fake','TRADFI_TELEGRAM_CHAT_ID':'123','LOCALAPPDATA':str(self.root)}), \
             patch('tradfi.mt5_adapter.importlib.import_module',side_effect=lambda name,*a,**kw: self.api.module() if name=='MetaTrader5' else real_import(name,*a,**kw)):
            _run_mt5(config,False,service=True,execute=True,telegram_factory=TelegramDouble,
                stop_event=stopped,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.assertEqual(len(self.api.sends),2)  # in-memory API double only
    def test_runtime_stalled_utc_does_not_delay_telegram_or_tick(self):
        import time
        stopped, entered, release = threading.Event(), threading.Event(), threading.Event()
        instances=[]; ticks=[]
        def safety_factory(**kwargs):
            safety=verified_time(**kwargs)
            original=safety.get
            def blocked(*a, **kw):
                entered.set(); release.wait(2); return original(*a, **kw)
            safety.get=blocked; instances.append(safety)
            return safety
        class TelegramDouble:
            def __init__(self,*args,**kwargs): self.http=self
            def updates(self,*args): return []
            def start(self,*args): pass
            def close(self): pass
            def process(self,controller):
                ticks.append(controller); stopped.set()
        config=dict(self.config,enable_trading=True,execution_contract='cycle_scoped_identity_v1',
                    legacy_ea_removed=True,legacy_state_reconciled=True)
        real_import=importlib.import_module
        try:
            with patch.dict('os.environ',{'TRADFI_TELEGRAM_TOKEN':'fake','TRADFI_TELEGRAM_CHAT_ID':'123','LOCALAPPDATA':str(self.root)}), \
                 patch('tradfi.time_safety.TimeSafety',side_effect=safety_factory), \
                 patch('tradfi.mt5_adapter.importlib.import_module',side_effect=lambda name,*a,**kw: self.api.module() if name=='MetaTrader5' else real_import(name,*a,**kw)):
                start=time.monotonic()
                _run_mt5(config,False,service=True,execute=True,telegram_factory=TelegramDouble,
                    stop_event=stopped,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
                self.assertLess(time.monotonic()-start,1.)
                self.assertTrue(ticks); self.assertTrue(entered.wait(1))
        finally:
            release.set()
            for safety in instances:
                if safety._worker: safety._worker.join(1)
        self.assertIsNotNone(ticks[0].observation.last_good)  # runtime still completed controller.tick

    def test_missing_history_blocks_manual_reset_and_keeps_money(self):
        self.position(); self.bot._enter_manual(self.bot.state['cycle'],'MANUAL',self.api.now)
        self.api.close_position(reason=0,profit=-7); self.open(False)
        before=deepcopy(self.bot.state); self.api.position_none=True
        with self.assertRaises(OSError): self.bot.command('/reset_manual')
        self.assertEqual(self.bot.state['cycle']['recovery'],before['cycle']['recovery'])
        self.assertFalse(self.bot.state['seen_deals']); self.assertEqual(self.bot.state['phase'],'MANUAL')
    def test_known_chain_settles_without_global_discovery_or_utc_claim(self):
        self.position(); self.api.omit_discovery=True; deal=self.api.close_position()
        # An ambiguous historical calendar hour cannot change an ID/volume-proven sum.
        from datetime import datetime,timezone
        ambiguous=int(datetime(2026,11,1,4,30,tzinfo=timezone.utc).timestamp()*1000)
        # Existing immutable entry witnesses must remain unchanged.
        self.api.deals[-1]=deal._replace(time_msc=ambiguous)
        self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'COOLDOWN')
        self.assertFalse(self.broker.snapshot()['history_complete'])
        self.assertEqual(self.broker.snapshot()['history_time_status'],'UNVERIFIED')
    def test_conflicting_native_fact_is_not_booked(self):
        self.position(); self.api.deals[0]=self.api.deals[0]._replace(volume=100)
        self.bot.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertFalse(self.bot.state['seen_deals'])
    def test_foreign_native_position_blocks_start(self):
        self.position(); self.close()
        self.config['state_file']=str(self.root/'different.db'); self.open(True)
        with self.assertRaisesRegex(ValueError,'broker-flat'): self.bot.command('/start')
    def test_pseudonymized_fixture_is_targeted_readonly_and_not_a_journal(self):
        self.position(); self.api.close_position(); ticket=self.api.history[0].ticket
        from tradfi.mt5_adapter import MT5Broker
        from tradfi.native_fixture import capture_chain
        broker=MT5Broker(self.config,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        broker.connect(); self.addCleanup(broker.close)
        before=(len(self.api.sends),len(self.api.checks)); self.api.calls=[]
        result=capture_chain(broker,ticket)
        self.assertTrue(result['not_an_ownership_journal']); self.assertTrue(result['calendar_times_shifted'])
        self.assertEqual(result['history_time_status'],'UNVERIFIED')
        self.assertEqual(before,(len(self.api.sends),len(self.api.checks)))
        self.assertTrue(all(kwargs for _,_,kwargs in self.api.calls))
        text=json.dumps(result)
        self.assertNotIn(str(self.api.account.login),text)
        self.assertNotIn(self.config['terminal_path'],text)
        self.assertEqual(sum(d['volume'] for d in result['deals'] if d['entry']==0),.01)
        self.assertEqual({d['position_id'] for d in result['deals']},{5001})
    def test_runtime_readonly_wiring_telegram_and_native_import(self):
        stopped=threading.Event(); observed=[]
        class TelegramDouble:
            def __init__(self,*args,**kwargs): self.http=self; observed.append(kwargs)
            def updates(self,*args): return []
            def start(self,*args): pass
            def close(self): pass
            def process(self,controller):
                self.controller=controller
                controller.command('/status'); controller.command('/settings')
                with self_test.assertRaisesRegex(ValueError,'READ_ONLY'): controller.command('/start')
                observed.append(controller.status()); stopped.set()
        self_test=self
        # A stale enable_trading=true config cannot activate without --execute.
        real_import=importlib.import_module
        with patch.dict('os.environ',{'TRADFI_TELEGRAM_TOKEN':'fake','TRADFI_TELEGRAM_CHAT_ID':'123','LOCALAPPDATA':str(self.root)}), \
             patch('tradfi.mt5_adapter.importlib.import_module',side_effect=lambda name,*a,**kw: self.api.module() if name=='MetaTrader5' else real_import(name,*a,**kw)):
            _run_mt5(dict(self.config,enable_trading=True),False,service=True,
                telegram_factory=TelegramDouble,stop_event=stopped,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.assertIn('READ_ONLY',observed[1]); self.assertEqual(self.api.initialized,1)
        self.assertFalse(self.api.sends); self.assertFalse(self.api.checks)
        with StoreContext(self.config['state_file']) as store:
            self.assertEqual(store.load()['ledger_contract'],'deal_ledger_v1')


class StoreContext:
    def __init__(self,path): self.store=Store(path)
    def __enter__(self): return self.store
    def __exit__(self,*args): self.store.close()

if __name__=='__main__': unittest.main()
