"""New raw installations need no manual offset; old state is not reinterpreted."""
import json
import tempfile
from pathlib import Path
import unittest
from tradfi.storage import Store
from tradfi.market_time_state import resolve_market_time_config, bind_market_time_state, MarketTimeStateError

class RawDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'db');self.addCleanup(self.store.close)
        self.cfg=dict(time_profile='bybit_live6_time_v1',expected_login=123,expected_server='Bybit-Live-6')
    def test_example_no_offset_and_raw(self):
        cfg=json.loads(Path('tradfi_config.example.json').read_text())
        self.assertEqual(cfg['market_time_mode'],'mt5_raw_v1')
        self.assertNotIn('market_time_offset_seconds',cfg)
        self.assertFalse(cfg['enable_trading'])
    def test_new_empty_state_defaults_raw(self):
        cfg=resolve_market_time_config(self.store,self.cfg)
        self.assertEqual(cfg['market_time_mode'],'mt5_raw_v1')
        binding=bind_market_time_state(self.store,cfg)
        self.assertEqual(binding['timestamp_semantics'],'BROKER_RAW')
    def test_new_broker_without_time_fields_keeps_mt5_timestamp(self):
        from tradfi.evidence_broker import MT5EvidenceBroker
        from test_raw_market_mode import config
        from test_tradfi_native_utc import UTCAPI
        cfg=config();cfg.pop('market_time_mode')
        api=UTCAPI();api.shift=7200
        api.order_check=api.order_send=lambda *a,**k:self.fail('No trading during check')
        broker=MT5EvidenceBroker(cfg,self.store,api=api,clock=lambda:1,monotonic=lambda:10)
        self.addCleanup(broker.close);broker.connect()
        snap=broker.snapshot()
        self.assertEqual(snap['quote']['broker_time'],api.now+7200)
        self.assertIsNone(snap['quote']['utc_time'])
        self.assertEqual(snap['market_time_mode'],'mt5_raw_v1')
    def test_existing_offset_recovered_not_subtracted_twice(self):
        bind_market_time_state(self.store,dict(self.cfg,market_time_offset_seconds=7200))
        cfg=resolve_market_time_config(self.store,self.cfg)
        self.assertEqual(cfg['market_time_mode'],'fixed_utc_v1')
        self.assertEqual(cfg['market_time_offset_seconds'],7200)
        bind_market_time_state(self.store,cfg)
    def test_existing_native_raw_recovered(self):
        bind_market_time_state(self.store,dict(self.cfg,market_time_mode='mt5_raw_v1'))
        cfg=resolve_market_time_config(self.store,self.cfg)
        self.assertEqual(cfg['market_time_mode'],'mt5_raw_v1')
        bind_market_time_state(self.store,cfg)
    def test_legacy_unbound_rows_stay_utc_and_untouched(self):
        self.store.db.execute("INSERT INTO audit(event,payload) VALUES('old','{}')")
        before=list(self.store.db.iterdump())
        cfg=resolve_market_time_config(self.store,self.cfg)
        self.assertEqual(cfg['market_time_mode'],'fixed_utc_v1')
        self.assertEqual(list(self.store.db.iterdump()),before)
    def test_explicit_change_rejects_even_when_offset_omitted(self):
        bind_market_time_state(self.store,dict(self.cfg,market_time_offset_seconds=7200))
        cfg=resolve_market_time_config(self.store,dict(self.cfg,market_time_mode='mt5_raw_v1'))
        with self.assertRaises((MarketTimeStateError,ValueError)):
            bind_market_time_state(self.store,cfg)
    def test_account_mismatch_not_overridden(self):
        bind_market_time_state(self.store,dict(self.cfg,market_time_mode='mt5_raw_v1'))
        cfg=resolve_market_time_config(self.store,dict(self.cfg,expected_login=124))
        with self.assertRaises(MarketTimeStateError):bind_market_time_state(self.store,cfg)

if __name__=='__main__':unittest.main()
