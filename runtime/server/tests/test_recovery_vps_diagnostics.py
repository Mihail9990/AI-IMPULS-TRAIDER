import unittest
from recovery.vps_diagnostics import diagnose
from test_recovery_close_api import APICloseFake

class DiagnosticFake(APICloseFake):
    def __init__(self):super().__init__();self.init=[];self.shutdowns=0;self.init_ok=True
    def initialize(self,*args,**kwargs):self.init.append((args,kwargs));return self.init_ok
    def shutdown(self):self.shutdowns+=1
    def order_send(self,*args):raise AssertionError('diagnostic cannot trade')
    def order_check(self,*args):raise AssertionError('diagnostic cannot check orders')
    def login(self,*args):raise AssertionError('diagnostic cannot login')
    def symbol_select(self,*args):raise AssertionError('diagnostic cannot select symbol')

class DiagnosticTests(unittest.TestCase):
    def call(self,a,**kw):
        args=dict(terminal_path='terminal64.exe',account=1,server='demo',symbol='XAUUSD.s')
        args.update(kw);return diagnose(a,**args)
    def test_one_shot_read_only_and_minimal_output(self):
        a=DiagnosticFake();r=self.call(a)
        self.assertFalse(r['safe_to_trade']);self.assertFalse(r['live_trading'])
        self.assertEqual(a.init,[(('terminal64.exe',),{'timeout':10000})])
        self.assertEqual(a.shutdowns,1)
        self.assertNotIn('account',r);self.assertNotIn('positions',r)
    def test_shutdown_on_init_failure_and_mismatch(self):
        a=DiagnosticFake();a.init_ok=False
        with self.assertRaises(RuntimeError):self.call(a)
        self.assertEqual(a.shutdowns,1)
        a=DiagnosticFake();a.login=2
        with self.assertRaises(ValueError):self.call(a)
        self.assertEqual(a.shutdowns,1)
    def test_validation_before_connection(self):
        for values in ({'account':True},{'history_days':True},{'symbol':''},{'terminal_path':''}):
            a=DiagnosticFake()
            with self.assertRaises(ValueError):self.call(a,**values)
            self.assertEqual(a.init,[])
    def test_account_switch_in_snapshot_rejected(self):
        a=DiagnosticFake();original=a.positions_get
        def switch(*args,**kwargs):a.login=2;return original(*args,**kwargs)
        a.positions_get=switch
        with self.assertRaises(ValueError):self.call(a)
        self.assertEqual(a.shutdowns,1)
    def test_missing_history_fails_not_ready(self):
        a=DiagnosticFake();a.history_deals_get=lambda *args:None
        with self.assertRaises(ValueError):self.call(a)
        self.assertEqual(a.shutdowns,1)
