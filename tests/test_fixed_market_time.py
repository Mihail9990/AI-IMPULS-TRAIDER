"""Fixed profile verified against independent UTC; no MT5 installation or writes."""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
import numpy as np
from tradfi.time_profile import (validate_market_time_offset, market_timestamp_to_utc,
                                 market_tick_to_utc, TimeProfileError)
from tradfi.native_time import NativeUTCVerifier
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.time_safety import TimeSafety
from test_tradfi_native_utc import UTCAPI

class FixedMarketTimeTest(unittest.TestCase):
    def setup_market(self, offset=7200, raw_shift=7200):
        self.api=UTCAPI();self.api.shift=raw_shift
        self.api.info=self.api.info._replace(trade_stops_level=1)
        original=self.api.copy_rates_from_pos
        self.api.copy_rates_from_pos=lambda *a:np.array([(r['time'],r['open'],r['high'],r['low'],r['close']) for r in original(*a)],dtype=[('time','i8'),('open','f8'),('high','f8'),('low','f8'),('close','f8')])
        self.api.order_send=self.api.order_check=lambda *a: self.fail('Native mutation forbidden')
        self.safety=TimeSafety(clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.seed()
        b=SimpleNamespace(api=self.api,symbol='XAUUSD.s',monotonic=lambda:self.api.now,max_age=5,market_time_offset_seconds=offset)
        self.verifier=NativeUTCVerifier(b,self.safety);self.safety.semantics_verifier=self.verifier
    def seed(self):
        self.safety.anchor=(self.api.now,self.api.now);self.safety.result=dict(ok=True,checked_monotonic=self.api.now)
    def test_config_boundaries_and_invalid_values(self):
        self.assertEqual(validate_market_time_offset({}),0)
        for x in (-50400,50400,0,7200,-7200):self.assertEqual(validate_market_time_offset({'market_time_offset_seconds':x}),x)
        for x in (True,None,'7200',7200.,1,50460,-50460):
            with self.subTest(x=x),self.assertRaises(TimeProfileError):validate_market_time_offset({'market_time_offset_seconds':x})
    def test_numpy_conversion_and_raw_tick_consistency(self):
        self.assertEqual(market_timestamp_to_utc(np.int64(100000),7200),92800)
        for t,ms in ((100000,100001000),(100000,99999999)):
            with self.assertRaises(TimeProfileError):market_tick_to_utc(SimpleNamespace(time=t,time_msc=ms),7200)
        self.assertEqual(market_tick_to_utc(SimpleNamespace(time=100000,time_msc=100000999),7200),92800.999)
    def test_fixed_offsets_ignore_former_seasonal_boundaries(self):
        for offset in (-7200,7200):
            self.setup_market(offset,offset)
            for date in ('2026-03-08T01:59:59','2026-03-08T02:00:00','2026-11-01T01:59:59','2026-11-01T02:00:00'):
                self.api.now=datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp();self.seed()
                self.assertTrue(self.verifier(),(offset,date,self.verifier.reason))
                self.assertEqual(self.verifier.observation['normalized_tick_utc'],self.api.now)
                json.dumps(self.verifier.observation,allow_nan=False)
    def test_wrong_offset_stale_future_double_conversion_block(self):
        self.setup_market()
        for raw in (0,10800,7200-6,7200+3,-7200):
            self.api.shift=raw
            self.assertFalse(self.verifier(),raw)
        self.api.shift=7200;self.assertTrue(self.verifier())
        self.safety.anchor=None;self.assertFalse(self.verifier());self.assertIsNone(self.verifier.observation)
    def test_tick_candle_disagreement_blocks(self):
        self.setup_market();original=self.api.copy_rates_from_pos
        def wrong(*a):
            rows=original(*a);rows['time']+=3600;return rows
        self.api.copy_rates_from_pos=wrong
        self.assertFalse(self.verifier())
    def test_live_normalized_and_raw_display_time_preserved(self):
        self.setup_market()
        config=dict(terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,enable_trading=False,market_time_offset_seconds=7200)
        broker=MT5EvidenceBroker(config,api=self.api,time_safety=self.safety,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        broker.connect();self.addCleanup(broker.close)
        live=broker.snapshot(require_fresh=True)
        self.assertEqual(live['quote']['utc_time'],self.api.now)
        self.assertEqual(live['mt5_display_time']['candle_time'],live['candle']['id']+7200)
        json.dumps(live,allow_nan=False)
        self.assertFalse(self.api.sends);self.assertFalse(self.api.checks)

    def test_actual_diagnostic_nonzero_offset_and_wrong_profile(self):
        import io
        import tempfile
        from pathlib import Path
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from tools.check_mt5_utc import main
        self.setup_market()
        config=dict(terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,enable_trading=True)
        def factory(cfg):
            self.assertFalse(cfg['enable_trading'])
            broker=MT5EvidenceBroker(cfg,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
            class Response:
                status_code=200
                def raise_for_status(inner):pass
                def json(inner):return dict(retCode=0,result=dict(timeNano=str(int(self.api.now*1e9)),timeSecond=str(int(self.api.now))))
            broker.time_safety.get=lambda *a,**k:Response()
            return broker
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            for offset,code,status in ((7200,0,'UTC_VERIFIED'),(0,2,'WAIT_OR_REVIEW')):
                path.write_text(json.dumps(dict(config,market_time_offset_seconds=offset)))
                out=io.StringIO()
                with patch('tools.check_mt5_utc.MT5EvidenceBroker',side_effect=factory),patch('sys.argv',['check_mt5_utc','--config',str(path)]),redirect_stdout(out):
                    self.assertEqual(main(),code)
                result=json.loads(out.getvalue());obs=result['observation']
                self.assertEqual(result['status'],status);self.assertFalse(result['trading'])
                self.assertEqual(result['market_time_offset_seconds'],offset)
                self.assertEqual(obs['raw_tick_time_msc'],int((self.api.now+7200)*1000))
                self.assertEqual(obs['normalized_tick_utc'],self.api.now+7200-offset)
                self.assertEqual(obs['normalized_m1_utc'],[t-offset for t in obs['raw_m1_times']])
                self.assertEqual(obs['reference_utc'],self.api.now)
                self.assertEqual(list(Path(tmp).iterdir()),[path])
                self.assertFalse(self.api.sends);self.assertFalse(self.api.checks)



    def test_latest_supplied_tick_obeys_two_second_future_limit(self):
        from tradfi.mt5_adapter import QuoteFreshnessError
        self.setup_market()
        config=dict(terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,enable_trading=False,market_time_offset_seconds=7200)
        broker=MT5EvidenceBroker(config,api=self.api,time_safety=self.safety,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        broker.connect();self.addCleanup(broker.close)
        broker._require_fresh=True
        current=self.api.symbol_info_tick(broker.symbol)
        for seconds in (3,4):
            supplied=current._replace(time=current.time+seconds,time_msc=current.time_msc+seconds*1000)
            self.assertTrue(self.verifier())  # Separate reference read remains fresh.
            with self.assertRaises(QuoteFreshnessError):broker._check_quote_time(supplied,'latest_tick')
        supplied=current._replace(time=current.time+2,time_msc=current.time_msc+2000)
        self.assertEqual(broker._check_quote_time(supplied,'latest_tick')['utc_time'],self.api.now+2)
        broker._require_fresh=False
        supplied=current._replace(time=current.time+3,time_msc=current.time_msc+3000)
        self.assertIsNone(broker._check_quote_time(supplied,'observation_only'))
        self.assertFalse(self.api.sends);self.assertFalse(self.api.checks)
