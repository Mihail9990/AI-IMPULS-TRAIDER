from recovery.service_loop import MockService
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.controller import Controller
from recovery.engine import Engine
from recovery.pipeline import MockPipeline
from recovery.integrated_api import create_mock_pipeline_app
from test_recovery_maintenance import CloseFake

class APICloseFake(CloseFake):
    DEAL_REASON_EXPERT=3
    def order_send(self,r):
        result=super().order_send(r)
        if r['action']==self.TRADE_ACTION_DEAL:
            d=self.close_deals[-1]
            d.type=1;d.reason=self.DEAL_REASON_EXPERT;d.magic=123;d.price=3999.5
            d.profit=-.50;d.commission=-.02;d.swap=0;d.fee=0;d.time_msc=200
        return result

class CloseAPITests(unittest.TestCase):
    def make(self,filling='ORDER_FILLING_IOC'):
        c=Controller(Engine(),test_mode=True);a=APICloseFake();p=MockPipeline(c,a,1,'demo','XAUUSD.s',123)
        owner=MockService(p);self.addCleanup(owner.close)
        client=TestClient(create_mock_pipeline_app('t'*40,p,market_filling=filling));h={'Authorization':'Bearer '+'t'*40}
        r=client.post('/api/recovery/ranges',headers=h,json={'request_id':str(uuid4()),'ranges':[
            {'id':'1','price':4000,'side':'BUY','volume':.01},{'id':'2','price':3950,'side':'SELL','volume':.01}]})
        self.assertEqual(r.status_code,200)
        a.fill();p.observe_mt5_deals([a.deal()]);p.sync_pending()
        return c,a,p,client,h
    def test_close_cancel_finalizes_only_selected_range_with_actual_pnl(self):
        c,a,p,client,h=self.make();key=str(uuid4())
        r=client.post('/api/recovery/ranges/1/close-cancel',headers=h,json={'request_id':key})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual([x['id'] for x in r.json()['ranges']],['2'])
        self.assertEqual(a.positions,[]);self.assertEqual(len(a.orders),1)
        self.assertEqual(a.orders[0].price_open,3950.)
        history=r.json()['loss_history'];self.assertEqual(history[-1]['loss'],'0.53')
        sent=len(a.sent)
        self.assertEqual(client.post('/api/recovery/ranges/1/close-cancel',headers=h,json={'request_id':key}).status_code,200)
        self.assertEqual(len(a.sent),sent)
    def test_partial_close_is_not_finalized(self):
        c,a,p,client,h=self.make();a.partial=True
        r=client.post('/api/recovery/ranges/1/close-cancel',headers=h,json={'request_id':str(uuid4())})
        self.assertEqual(r.status_code,409)
        self.assertIsNotNone(c.engine()._range('1'));self.assertEqual(len(c.engine().data['positions']),1)
        self.assertEqual(a.positions[0].volume,.005);self.assertIn('1',c.data['blocked'])
    def test_missing_filling_policy_sends_nothing(self):
        c,a,p,client,h=self.make(None);before=len(a.sent)
        r=client.post('/api/recovery/ranges/1/close-cancel',headers=h,json={'request_id':str(uuid4())})
        self.assertEqual(r.status_code,409);self.assertEqual(len(a.sent),before)
    def test_unrelated_expert_close_not_blanket_authorized(self):
        c,a,p,client,h=self.make();d=a.deal(101,1,reason=a.DEAL_REASON_EXPERT)
        with self.assertRaisesRegex(ValueError,'Unsupported close reason'):p.observe_mt5_deals([d])
        self.assertEqual(len(c.engine().data['positions']),1)
