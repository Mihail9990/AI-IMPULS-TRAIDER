import unittest
from types import SimpleNamespace as N
from recovery.mt5_transport import MT5Transport
from recovery.executor import Executor

class FakeMT5:
    _RECOVERY_TEST_DOUBLE=True
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2
    SYMBOL_TRADE_EXECUTION_REQUEST=0;SYMBOL_TRADE_EXECUTION_INSTANT=1
    SYMBOL_TRADE_EXECUTION_MARKET=2;SYMBOL_TRADE_EXECUTION_EXCHANGE=3
    SYMBOL_FILLING_FOK=1;SYMBOL_FILLING_IOC=2
    POSITION_TYPE_BUY=0;POSITION_TYPE_SELL=1
    ORDER_TYPE_BUY_STOP=4;ORDER_TYPE_SELL_STOP=5
    ORDER_STATE_CANCELED=2;ORDER_STATE_FILLED=4
    TRADE_ACTION_PENDING=5;TRADE_ACTION_SLTP=6;TRADE_ACTION_REMOVE=8
    ORDER_TIME_GTC=0;ORDER_FILLING_RETURN=2
    TRADE_RETCODE_DONE=10009;TRADE_RETCODE_PLACED=10008
    TRADE_RETCODE_DONE_PARTIAL=10010;TRADE_RETCODE_TIMEOUT=10012
    def __init__(self):
        self.login=1;self.sent=[];self.orders=[];self.history=[];self.positions=[]
        self.ask=3999.2;self.retcode=self.TRADE_RETCODE_PLACED;self.read_error=False
    def account_info(self):return N(login=self.login,server='demo',margin_mode=2,trade_allowed=True)
    def terminal_info(self):return N(connected=True,trade_allowed=True)
    def symbol_info(self,s):return N(trade_exemode=1,filling_mode=3,trade_tick_size=.01,point=.01,trade_stops_level=5,trade_freeze_level=5,volume_min=.01,volume_max=100,volume_step=.01)
    def symbol_info_tick(self,s):return N(bid=self.ask-.2,ask=self.ask)
    def order_check(self,r):return N(retcode=0)
    def order_send(self,r):
        self.sent.append(r)
        if r['action']==5:
            self.orders.append(N(ticket=50,magic=r['magic'],symbol=r['symbol'],type=r['type'],price_open=r['price'],sl=r['sl'],tp=r['tp'],volume_current=r['volume'],state=1))
        return N(retcode=self.retcode,order=50,deal=0)
    def orders_get(self,ticket=None):return None if self.read_error else tuple(o for o in self.orders if ticket is None or o.ticket==ticket)
    def positions_get(self,ticket=None):return tuple(p for p in self.positions if ticket is None or p.ticket==ticket)
    def history_orders_get(self,a,b):return tuple(self.history)

REQUEST={'action':'TRADE_ACTION_PENDING','symbol':'XAUUSD.s','magic':123,
         'type':'ORDER_TYPE_BUY_STOP','price':'4000','sl':'3999.3','tp':'0','volume':'.01',
         'type_time':'ORDER_TIME_GTC','type_filling':'ORDER_FILLING_RETURN'}
EXPECTED={'account':1,'server':'demo','symbol':'XAUUSD.s','magic':123}

class TransportTests(unittest.TestCase):
    def test_real_object_cannot_construct(self):
        with self.assertRaises(RuntimeError):MT5Transport(N())
    def test_executor_to_mt5_and_verified(self):
        api=FakeMT5();e=Executor(MT5Transport(api));e.prepare('1',REQUEST,EXPECTED)
        self.assertEqual(e.submit_once('1')['state'],'ACKNOWLEDGED')
        self.assertEqual(e.reconcile('1')['state'],'VERIFIED')
        self.assertEqual(api.sent[0]['action'],5);self.assertEqual(api.sent[0]['price'],4000.)
    def test_lock_rechecked_before_send(self):
        api=FakeMT5();t=MT5Transport(api);t.preflight(REQUEST,EXPECTED)
        api._RECOVERY_TEST_DOUBLE=False
        with self.assertRaises(RuntimeError):t.send(REQUEST)
        self.assertEqual(api.sent,[])
    def test_crossed_quote_blocks(self):
        api=FakeMT5();api.ask=4001;e=Executor(MT5Transport(api));e.prepare('1',REQUEST,EXPECTED)
        self.assertEqual(e.submit_once('1')['state'],'REJECTED');self.assertEqual(api.sent,[])
    def test_changed_account_after_preflight(self):
        api=FakeMT5();t=MT5Transport(api);t.preflight(REQUEST,EXPECTED);api.login=2
        with self.assertRaises(ValueError):t.send(REQUEST)
        self.assertEqual(api.sent,[])
    def test_partial_never_verified_as_full_volume(self):
        api=FakeMT5();api.retcode=api.TRADE_RETCODE_DONE_PARTIAL;e=Executor(MT5Transport(api))
        e.prepare('1',REQUEST,EXPECTED);self.assertEqual(e.submit_once('1')['state'],'UNKNOWN')
        # Even if order exists at smaller amount, it is not requested full volume.
        api.orders[0].volume_current=.005
        self.assertEqual(e.reconcile('1')['state'],'UNKNOWN')
        e.submit_once('1');self.assertEqual(len(api.sent),1)
    def test_incomplete_read_not_empty_account(self):
        api=FakeMT5();e=Executor(MT5Transport(api));e.prepare('1',REQUEST,EXPECTED);e.submit_once('1')
        api.read_error=True;self.assertEqual(e.reconcile('1')['state'],'UNKNOWN')
    def test_mismatched_preflight_request(self):
        api=FakeMT5();t=MT5Transport(api);t.preflight(REQUEST,EXPECTED)
        with self.assertRaises(ValueError):t.send(dict(REQUEST,volume='.02'))
        self.assertEqual(api.sent,[])
