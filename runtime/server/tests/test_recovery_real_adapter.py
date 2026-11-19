import unittest
from recovery.real_mt5_adapter import RealMT5ReadOnlyAdapter
from recovery.pipeline import MockPipeline
from recovery.controller import Controller
from recovery.engine import Engine
from test_recovery_close_api import APICloseFake

class ReadOnlyAdapterTests(unittest.TestCase):
    def make(self):
        api=APICloseFake()
        return api,RealMT5ReadOnlyAdapter(api,account=1,server='demo')
    def test_reads_without_any_send(self):
        api,adapter=self.make()
        result=adapter.snapshot({'account':1,'server':'demo'})
        self.assertEqual(result['mode'],'read_only')
        self.assertFalse(result['safe_to_trade'])
        self.assertEqual(api.sent,[])
    def test_no_write_or_mock_pipeline_path(self):
        api,adapter=self.make()
        for operation in (adapter.preflight,adapter.send,adapter._test_lock):
            with self.assertRaises(RuntimeError):operation()
        with self.assertRaises(RuntimeError):
            MockPipeline(Controller(Engine(),test_mode=True),adapter,1,'demo','XAUUSD.s',123)
        self.assertEqual(api.sent,[])
    def test_account_switch_during_snapshot_rejected(self):
        api,adapter=self.make()
        original=api.orders_get
        def switch():
            api.login=2
            return original()
        api.orders_get=switch
        with self.assertRaises(ValueError):adapter.snapshot({'account':1,'server':'demo'})
    def test_scope_and_missing_history_rejected(self):
        api,adapter=self.make()
        with self.assertRaises(ValueError):adapter.snapshot({'account':2,'server':'demo'})
        api.history_deals_get=lambda *a:None
        with self.assertRaises(ValueError):adapter.snapshot({'account':1,'server':'demo'})
        self.assertEqual(api.sent,[])
