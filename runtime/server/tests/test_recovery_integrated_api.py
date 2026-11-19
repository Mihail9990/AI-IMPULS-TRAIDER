from recovery.service_loop import MockService
import unittest
from types import SimpleNamespace as N
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.controller import Controller
from recovery.engine import Engine
from recovery.pipeline import MockPipeline
from recovery.integrated_api import create_mock_pipeline_app
from test_recovery_pipeline import TradeFake

class CancelFake(TradeFake):
    def __init__(self):super().__init__();self.cancel_timeout=False;self.race=False
    def order_send(self,r):
        if r['action']!=self.TRADE_ACTION_REMOVE:return super().order_send(r)
        self.sent.append(r)
        if self.cancel_timeout:return N(retcode=self.TRADE_RETCODE_TIMEOUT,order=r['order'],deal=0)
        o=next(o for o in self.orders if o.ticket==r['order']);self.orders.remove(o)
        o.state=self.ORDER_STATE_CANCELED;self.history.append(o)
        if self.race:self.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,price_open=4000,volume=.01,sl=3999.3,tp=0)]
        return N(retcode=self.TRADE_RETCODE_DONE,order=r['order'],deal=0)

class IntegratedAPITests(unittest.TestCase):
    def setUp(self):
        self.c=Controller(Engine(),test_mode=True);self.a=CancelFake()
        self.p=MockPipeline(self.c,self.a,1,'demo','XAUUSD.s',123)
        self.owner=MockService(self.p);self.addCleanup(self.owner.close)
        self.client=TestClient(create_mock_pipeline_app('t'*40,self.p));self.h={'Authorization':'Bearer '+'t'*40}
    def configure(self,price=4000,request_id=None):
        return self.client.post('/api/recovery/ranges',headers=self.h,json={'request_id':request_id or str(uuid4()),'ranges':[{'id':'1','price':price,'side':'BUY','volume':.01}]})
    def test_create_then_verified_replace(self):
        self.assertEqual(self.configure().status_code,200);old=self.a.orders[0].ticket
        r=self.configure(4001);self.assertEqual(r.status_code,200)
        self.assertEqual(len(self.a.orders),1);self.assertNotEqual(self.a.orders[0].ticket,old)
        self.assertEqual(self.a.orders[0].price_open,4001.)
        self.assertEqual(self.c.data['bindings'][str(old)]['phase'],'cancelled')
        self.assertEqual(r.json()['execution_environment'],'test_double_only')
    def test_cancel_all_verified(self):
        self.configure();r=self.client.post('/api/recovery/cancel',headers=self.h,json={'request_id':str(uuid4())})
        self.assertEqual(r.status_code,200);self.assertEqual(self.a.orders,[]);self.assertEqual(r.json()['ranges'],[])
    def test_idempotent_request_no_new_broker_send(self):
        key=str(uuid4());r=self.configure(request_id=key);sent=len(self.a.sent)
        self.assertEqual(self.configure(request_id=key).json(),r.json());self.assertEqual(len(self.a.sent),sent)
        self.assertEqual(self.configure(4002,request_id=key).status_code,409)
    def test_cancel_timeout_retains_old_range_and_blocks_replay(self):
        self.configure();self.a.cancel_timeout=True;key=str(uuid4())
        r=self.configure(4001,request_id=key);self.assertEqual(r.status_code,409)
        self.assertEqual(self.c.engine()._range('1')['price'],'4000.0')
        sent=len(self.a.sent);self.configure(4001,request_id=key);self.assertEqual(len(self.a.sent),sent)
    def test_fill_race_does_not_commit_new_ranges(self):
        self.configure();self.a.race=True;r=self.configure(4001)
        self.assertEqual(r.status_code,409);self.assertEqual(self.c.engine()._range('1')['price'],'4000.0')
        self.assertTrue(self.a.positions)
    def test_broker_position_blocks_even_if_engine_empty(self):
        self.configure();self.a.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,price_open=4000,volume=.01,sl=3999.3,tp=0)]
        sent=len(self.a.sent);self.assertEqual(self.configure(4001).status_code,409);self.assertEqual(len(self.a.sent),sent)
    def test_auth(self):
        self.assertEqual(self.client.get('/api/recovery/state').status_code,401)
