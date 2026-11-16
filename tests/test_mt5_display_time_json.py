"""Real NumPy MT5-shaped rates remain display-only and JSON serializable."""
import json
import unittest
from unittest.mock import patch
try:
    import numpy as np
except ImportError:
    np=None
import test_tradfi_native_service as native_fixture
from tradfi.recovery_v3 import ControlledRecoveryController
from tradfi.web_control import NativePanel,WebTransport

@unittest.skipIf(np is None,'NumPy required for native rates scalar regression')
class DisplayTimeJSON(unittest.TestCase):
    def setUp(self):
        self.f=native_fixture.NativeServiceTest();self.f.setUp();self.addCleanup(self.f.doCleanups);self.f.open(False)
        f=self.f;original=f.broker.api.copy_rates_from_pos
        def rates(*args):
            rows=original(*args)
            return np.array([(r['time'],r['open'],max(r['open'],r['close'])+1,min(r['open'],r['close'])-1,r['close']) for r in rows],dtype=[('time','i8'),('open','f8'),('high','f8'),('low','f8'),('close','f8')])
        p=patch.object(f.broker.api,'copy_rates_from_pos',side_effect=rates);p.start();self.addCleanup(p.stop)
        p=patch.object(f.broker,'_timestamp_semantics_verified',return_value=False);p.start();self.addCleanup(p.stop)
        self.bot=ControlledRecoveryController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
    def test_snapshot_and_api_state_numpy_time_are_json_safe_without_verifying_time(self):
        f=self.f;raw=f.broker.api.copy_rates_from_pos(f.broker.symbol,1,1,1)[0]['time']
        self.assertIsInstance(raw,np.int64)
        snap=self.bot.snapshot()
        # This is the production HTTP encoder, deliberately without a fallback.
        json.dumps(snap,allow_nan=False)
        self.assertIs(type(snap['mt5_display_time']['candle_time']),int)
        self.assertEqual(snap['mt5_display_time']['candle_time'],raw.item())
        self.assertIsNone(snap['quote']['utc_time']);self.assertEqual(snap['candle']['id'],0)
        self.assertEqual(snap['mt5_timestamp_semantics'],'UNVERIFIED')
        panel=NativePanel(self.bot,clock=lambda:f.api.now,monotonic=lambda:f.api.now);self.addCleanup(panel.close);panel.step()
        transport=WebTransport(panel.mailbox,password='test-only-local-password',clock=lambda:f.api.now)
        headers={'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        code,_,extra=transport.request('POST','/api/login',headers,json.dumps({'password':'test-only-local-password'}).encode());self.assertEqual(code,200)
        headers['Cookie']=extra['Set-Cookie'].split(';')[0]
        code,payload,_=transport.request('GET','/api/state',headers);self.assertEqual(code,200)
        decoded=json.loads(json.dumps(payload,allow_nan=False))
        self.assertEqual(decoded['feed']['display_time']['candle_time'],raw.item())
        self.assertEqual(decoded['feed']['status'],'UNVERIFIED');self.assertIsNone(decoded['feed']['timestamp'])
        self.assertTrue(decoded['read_only']);self.assertFalse(f.api.sends);self.assertFalse(f.api.checks)
        self.assertFalse(self.bot.state['commands'])

    def test_cached_current_reason_does_not_replace_latch_or_probe(self):
        from types import SimpleNamespace
        f=self.f;self.bot.state['time_block']={'reason':'MT5_UTC_WAIT_REFERENCE'}
        class Diagnostic:
            reason='MT5_UTC_CONTRACT_MISMATCH'
            def __call__(self):raise AssertionError('No additional verification during rendering')
        panel=NativePanel(self.bot,clock=lambda:f.api.now,monotonic=lambda:f.api.now);self.addCleanup(panel.close)
        with patch.object(f.broker,'time_safety',SimpleNamespace(semantics_verifier=Diagnostic(),result={'ok':True})):
            status=panel.time_status()
            self.assertEqual({k:status[k] for k in ('latched_reason','current_reason','waiting_data')},dict(latched_reason='MT5_UTC_WAIT_REFERENCE',current_reason='MT5_UTC_CONTRACT_MISMATCH',waiting_data=False))
            self.assertFalse(status['display_clock']['verified_utc'])
        self.assertEqual(self.bot.state['time_block'],{'reason':'MT5_UTC_WAIT_REFERENCE'})
        self.assertFalse(f.api.sends);self.assertFalse(f.api.checks)
    def test_verified_fixture_retains_existing_timestamp_contract(self):
        f=self.f
        with patch.object(f.broker,'_timestamp_semantics_verified',return_value=True):
            snap=self.bot.snapshot()
        json.dumps(snap,allow_nan=False)
        self.assertEqual(snap['candle']['id'],f.api.rates[0]['time'])
        self.assertEqual(snap['quote']['utc_time'],f.api.now)
        self.assertIs(type(snap['mt5_display_time']['candle_time']),int)
        self.assertTrue(f.broker.read_only);self.assertFalse(f.api.sends)
