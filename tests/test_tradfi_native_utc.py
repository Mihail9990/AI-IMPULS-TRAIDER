"""Production UTC verifier with read-only MT5 doubles; no network or live terminal."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone
from tradfi.native_time import NativeUTCVerifier
from tradfi.time_safety import TimeSafety
from tradfi.time_profile import offset_at
from tradfi.basket import BasketController
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.storage import Store
from test_tradfi_native_interactive import NativeAPI
from test_tradfi_mt5 import record

class UTCAPI(NativeAPI):
    def __init__(self):
        super().__init__();self.now=datetime(2026,11,2,12,0,30,tzinfo=timezone.utc).timestamp();self.shift=0;self.unavailable=False;self.delay=0;self.reads=0
    def symbol_info_tick(self,symbol):
        self.reads+=1
        if self.unavailable:return None
        raw=self.now+self.shift
        return record(bid=4000.,ask=4000.1,time=int(raw),time_msc=int(raw*1000))
    def copy_rates_from_pos(self,symbol,timeframe,start,count):
        self.reads+=1;self.now+=self.delay
        if self.unavailable:return None
        current=int((self.now+self.shift)//60)*60
        return [dict(time=current-60*i,open=3999.,close=4000.,high=4001.,low=3998.) for i in reversed(range(start,start+count))]

class NativeUTCTest(unittest.TestCase):
    def setUp(self):
        self.api=UTCAPI();self.b=SimpleNamespace(api=self.api,symbol='XAUUSD.s',monotonic=lambda:self.api.now,max_age=5)
        self.safety=TimeSafety(clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.verifier=NativeUTCVerifier(self.b,self.safety);self.safety.semantics_verifier=self.verifier;self.seed()
    def seed(self):
        self.safety.anchor=(self.api.now,self.api.now);self.safety.result=dict(ok=True,checked_monotonic=self.api.now)
    def test_utc_fresh_and_display_utc_plus_2_are_independent(self):
        self.assertTrue(self.verifier());self.assertIsNone(self.safety.check());self.assertEqual(offset_at(self.api.now),7200)
    def test_broker_wall_plus_2_is_rejected_without_offset_guess(self):
        self.api.shift=7200;self.assertFalse(self.verifier());self.assertEqual(self.verifier.reason,'MT5_UTC_CONTRACT_MISMATCH')
    def test_stale_and_missing_data_wait(self):
        self.api.shift=-120;self.assertFalse(self.verifier());self.assertEqual(self.verifier.reason,'MT5_UTC_WAIT_FRESH_DATA')
        self.api.unavailable=True;self.assertFalse(self.verifier())
    def test_slow_read_and_expired_reference_rejected(self):
        self.api.delay=3;self.assertFalse(self.verifier());self.assertEqual(self.verifier.reason,'MT5_UTC_READ_TIMEOUT')
        self.api.delay=0;self.api.now+=61;self.assertFalse(self.verifier());self.assertEqual(self.verifier.reason,'MT5_UTC_WAIT_REFERENCE')
    def test_worker_never_calls_mt5(self):
        result=[];t=threading.Thread(target=lambda:result.append(self.verifier()));t.start();t.join()
        self.assertEqual(result,[False]);self.assertEqual(self.api.reads,0)
    def test_disagreement_does_not_accept_stale_m1(self):
        original=self.api.copy_rates_from_pos
        def wrong(*args):return [dict(r,time=r['time']-60) for r in original(*args)]
        self.api.copy_rates_from_pos=wrong;self.assertFalse(self.verifier())
    def test_legacy_production_broker_wait_then_verifies_utc_and_enters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.db');self.addCleanup(store.close)
            config=dict(terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',market_time_mode='fixed_utc_v1',expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,enable_trading=True,execution_contract='cycle_scoped_identity_v1',legacy_ea_removed=True,legacy_state_reconciled=True)
            self.api.info=self.api.info._replace(trade_stops_level=1)
            broker=MT5EvidenceBroker(config,store,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now);broker.connect();self.addCleanup(broker.close)
            self.assertIsInstance(broker.time_safety.semantics_verifier,NativeUTCVerifier)
            self.assertFalse(broker._timestamp_semantics_verified())
            safety=broker.time_safety;safety.anchor=(self.api.now,self.api.now);safety.result=dict(ok=True,checked_monotonic=self.api.now)
            snap=broker.snapshot();self.assertEqual(snap['quote']['utc_time'],self.api.now)
            self.assertEqual(snap['candle']['id'],int(self.api.now//60)*60-60)
            bot=BasketController(store,broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
            bot.command('/start');bot.tick();self.assertFalse(self.api.orders)
            self.api.now+=30;bot.tick();bot.tick();self.assertEqual(len(self.api.orders),1)
            self.assertEqual(bot.state['cycle']['deadline_utc'],self.api.now+86400)
            self.api.fill();bot.tick();bot.tick();protection=[(p.identifier,p.sl,p.tp) for p in self.api.positions]
            self.api.shift=7200;bot.tick();bot.tick()
            self.assertEqual([(p.identifier,p.sl,p.tp) for p in self.api.positions],protection)
            self.assertTrue(bot.state['time_block'])


    def test_packaged_diagnostic_forces_read_only_and_writes_no_database(self):
        import io,json
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from tools.check_mt5_utc import main
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'service.json'
            path.write_text(json.dumps(dict(terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',market_time_mode='fixed_utc_v1',expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,enable_trading=True)))
            def factory(config):
                self.assertFalse(config['enable_trading'])
                broker=MT5EvidenceBroker(config,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
                class Response:
                    status_code=200
                    def raise_for_status(inner):pass
                    def json(inner):return dict(retCode=0,result=dict(timeNano=str(int(self.api.now*1e9)),timeSecond=str(int(self.api.now))))
                broker.time_safety.get=lambda *a,**k:Response()
                return broker
            output=io.StringIO()
            with patch('tools.check_mt5_utc.MT5EvidenceBroker',side_effect=factory),patch('sys.argv',['check_mt5_utc','--config',str(path)]),redirect_stdout(output):
                self.assertEqual(main(),0)
            self.assertEqual(json.loads(output.getvalue())['status'],'UTC_VERIFIED')
            self.assertFalse(self.api.sends);self.assertEqual(list(Path(tmp).iterdir()),[path])
