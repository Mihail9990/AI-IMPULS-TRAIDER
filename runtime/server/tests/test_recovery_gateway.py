import unittest
from copy import deepcopy
from types import SimpleNamespace as N
from recovery.mt5_execution_gateway import GuardedMT5Gateway, map_native_request
from test_recovery_mt5_transport import REQUEST, FakeMT5
from test_recovery_close_api import APICloseFake

class GatewayTests(unittest.TestCase):
    def test_default_lock_precedes_any_terminal_call(self):
        api=N()
        g=GuardedMT5Gateway(api,account=1,server='demo')
        for fn in (g.order_check,g.order_send):
            with self.assertRaises(RuntimeError):fn(REQUEST)
        with self.assertRaises(RuntimeError):GuardedMT5Gateway.for_test_double(api,account=1,server='demo')
    def test_native_mapping_and_one_use_send(self):
        api=FakeMT5();g=GuardedMT5Gateway.for_test_double(api,account=1,server='demo')
        g.order_check(REQUEST);g.order_send(REQUEST)
        self.assertEqual(api.sent[0]['type'],api.ORDER_TYPE_BUY_STOP)
        self.assertEqual(api.sent[0]['sl'],3999.3)
        self.assertEqual(REQUEST['price'],'4000')
        with self.assertRaises(ValueError):g.order_send(REQUEST)
        self.assertEqual(len(api.sent),1)
    def test_mutation_account_switch_and_rejected_check_prevent_send(self):
        for mode in ('mutation','account','rejected'):
            api=FakeMT5();g=GuardedMT5Gateway.for_test_double(api,account=1,server='demo')
            if mode=='rejected':api.order_check=lambda r:N(retcode=1)
            g.order_check(REQUEST);r=deepcopy(REQUEST)
            if mode=='mutation':r['volume']='.02'
            if mode=='account':api.login=2
            with self.assertRaises(ValueError):g.order_send(r)
            self.assertEqual(api.sent,[])
    def test_market_execution_close_omits_price(self):
        api=APICloseFake()
        r={'action':'TRADE_ACTION_DEAL','position':70,'symbol':'XAUUSD.s','magic':123,
           'type':'ORDER_TYPE_SELL','volume':'.01','price':'4000','type_filling':'ORDER_FILLING_IOC'}
        self.assertNotIn('price',map_native_request(api,r,market_execution=True))
        self.assertEqual(map_native_request(api,r)['position'],70)
        del r['position']
        with self.assertRaises(ValueError):map_native_request(api,r)
    def test_remove_protect_and_invalid_numbers(self):
        api=FakeMT5()
        self.assertEqual(map_native_request(api,{'action':'TRADE_ACTION_REMOVE','order':51}),{'action':8,'order':51})
        r=map_native_request(api,{'action':'TRADE_ACTION_SLTP','position':70,'sl':'3999.3','tp':'0'})
        self.assertEqual(r['action'],6)
        for value in ('NaN','Infinity',True,'-1'):
            bad=dict(REQUEST,volume=value)
            with self.assertRaises(ValueError):map_native_request(api,bad)
    def test_timeout_consumes_capability(self):
        api=FakeMT5();g=GuardedMT5Gateway.for_test_double(api,account=1,server='demo')
        def timeout(r):raise TimeoutError('unknown')
        api.order_send=timeout;g.order_check(REQUEST)
        with self.assertRaises(TimeoutError):g.order_send(REQUEST)
        with self.assertRaises(ValueError):g.order_send(REQUEST)
