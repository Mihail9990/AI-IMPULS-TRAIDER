import tempfile
import unittest
from types import SimpleNamespace as N
from recovery.engine import Engine
from recovery.controller import Controller
from recovery.pipeline import MockPipeline
from recovery.service_loop import MockService
from test_recovery_close_api import APICloseFake

class LifecycleFake(APICloseFake):
    def fill_at(self,ticket,deal_id):
        o=next(o for o in self.orders if o.ticket==ticket);self.orders.remove(o)
        o.state=self.ORDER_STATE_FILLED;self.history.append(o)
        self.positions=[N(ticket=70+deal_id,identifier=700+deal_id,magic=o.magic,symbol=o.symbol,type=0,
            price_open=o.price_open,volume=o.volume_current,sl=o.sl,tp=0)]
        self.ask=o.price_open+.2
        self.close_deals.append(N(ticket=deal_id,order=ticket,position_id=700+deal_id,entry=0,type=0,
            reason=3,symbol=o.symbol,magic=o.magic,volume=o.volume_current,price=o.price_open,
            profit=0,commission=0,swap=0,fee=0,time_msc=deal_id))
    def support_close(self,deal_id):
        p=self.positions.pop()
        self.close_deals.append(N(ticket=deal_id,order=999,position_id=p.identifier,entry=1,type=1,
            reason=self.DEAL_REASON_SL,symbol=p.symbol,magic=0,volume=p.volume,price=p.sl,
            profit=0,commission=0,swap=0,fee=0,time_msc=deal_id))

class ServiceLoopTests(unittest.TestCase):
    def make(self,path=':memory:',api=None):
        e=Engine();e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}])
        c=Controller(e,path,test_mode=True);a=api or LifecycleFake();p=MockPipeline(c,a,1,'demo','XAUUSD.s',123)
        return c,a,p
    def test_reentry_retires_old_reverse_before_new_one(self):
        c,a,p=self.make()
        with MockService(p) as service:
            service.step('3999','3999.2')
            a.fill_at(51,100);service.step('4000','4000.2')
            old_reverse=a.orders[0].ticket;self.assertEqual(a.orders[0].price_open,3999.3)
            a.ask=4000.7;service.step('4000.5','4000.7')
            a.support_close(101);a.ask=4000.2;service.step('4000','4000.2')
            reentry=next(o for o in a.orders if o.type==a.ORDER_TYPE_BUY_STOP)
            self.assertEqual(reentry.price_open,4000.7)
            a.fill_at(reentry.ticket,102);service.step('4000.7','4000.9')
            self.assertEqual(len(a.orders),1);self.assertEqual(a.orders[0].price_open,4000.)
            self.assertTrue(any(o.ticket==old_reverse and o.state==a.ORDER_STATE_CANCELED for o in a.history))
            self.assertEqual(c.data['blocked'],{})
    def test_filled_before_verify_then_history_recovers(self):
        class Immediate(LifecycleFake):
            def __init__(self):super().__init__();self.once=True
            def order_send(self,r):
                result=super().order_send(r)
                if self.once and r['action']==self.TRADE_ACTION_PENDING:
                    self.once=False;self.fill_at(result.order,100)
                return result
        c,a,p=self.make(api=Immediate())
        with MockService(p) as service:
            service.step('3999','3999.2')
            self.assertEqual(c.data['bindings']['51']['phase'],'awaiting_fill_history')
            self.assertEqual(c.data['blocked'],{})
            service.step('4000','4000.2')
            self.assertEqual(len(c.engine().data['positions']),1)
            self.assertEqual(c.data['bindings']['51']['phase'],'open')
    def test_one_service_owner_per_controller_and_database(self):
        with tempfile.TemporaryDirectory() as d:
            c,a,p=self.make(d+'/c.db');first=MockService(p)
            with self.assertRaises(RuntimeError):MockService(p)
            c2,a2,p2=self.make(d+'/c.db')
            with self.assertRaises(RuntimeError):MockService(p2)
            first.close()
            with MockService(p2):pass
            c.db.close();c2.db.close()
    def test_closed_service_cannot_step(self):
        c,a,p=self.make();service=MockService(p);service.close()
        with self.assertRaises(RuntimeError):service.step('3999','3999.2')
    def test_retired_reverse_timeout_blocks_replacement(self):
        c,a,p=self.make()
        with MockService(p) as service:
            service.step('3999','3999.2');a.fill_at(51,100);service.step('4000','4000.2')
            old=a.orders[0].ticket
            a.ask=4000.7;service.step('4000.5','4000.7')
            a.support_close(101);a.ask=4000.2;service.step('4000','4000.2')
            reentry=next(o for o in a.orders if o.type==a.ORDER_TYPE_BUY_STOP)
            a.fill_at(reentry.ticket,102);a.cancel_timeout=True
            service.step('4000.7','4000.9')
            self.assertIn('1',c.data['blocked'])
            self.assertEqual([o.ticket for o in a.orders],[old])
            self.assertFalse(any(o.price_open==4000 for o in a.orders))

    def test_takeover_reloads_latest_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            c,a,p=self.make(d+'/c.db');first=MockService(p)
            c2,a2,p2=self.make(d+'/c.db',api=a)
            first.step('3999','3999.2')
            self.assertTrue(c.data['bindings'])
            self.assertFalse(c2.data['bindings'])
            first.close()
            with MockService(p2):
                self.assertEqual(c2.data,c.data)
                p2.sync_pending()
                self.assertEqual(len(a.orders),1)
            c.db.close();c2.db.close()

    def test_api_requires_current_ownership(self):
        from fastapi.testclient import TestClient
        from recovery.integrated_api import create_mock_pipeline_app
        from uuid import uuid4
        c,a,p=self.make()
        client=TestClient(create_mock_pipeline_app('t'*40,p))
        h={'Authorization':'Bearer '+'t'*40}
        payload={'request_id':str(uuid4())}
        self.assertEqual(client.get('/api/recovery/state',headers=h).status_code,409)
        self.assertEqual(client.post('/api/recovery/cancel',headers=h,json=payload).status_code,409)
        self.assertNotIn('api_requests',c.data)
        with MockService(p):
            self.assertEqual(client.get('/api/recovery/state',headers=h).status_code,200)
        self.assertEqual(client.post('/api/recovery/cancel',headers=h,json=payload).status_code,409)
        self.assertNotIn('api_requests',c.data)

    def test_startup_unknown_exposure_blocks_sends_and_commands(self):
        from recovery.coordinator import Coordinator
        c,a,p=self.make()
        a.positions=[N(ticket=90,identifier=900,magic=123,symbol='XAUUSD.s',type=0,
                       price_open=4000,volume=.01,sl=3999.3,tp=0)]
        with MockService(p) as service:
            self.assertFalse(service.startup_report['safe_to_resume'])
            with self.assertRaises(RuntimeError):service.step('3999','3999.2')
            with self.assertRaises(ValueError):Coordinator(p).command('cancel',{'kind':'cancel'})
            self.assertEqual(a.orders,[])
            self.assertNotIn('api_requests',c.data)

    def test_startup_prepared_intent_not_replayed(self):
        c,a,p=self.make()
        p.executor.prepare('orphan',{'action':'TRADE_ACTION_REMOVE','order':55},p.scope)
        with MockService(p) as service:
            self.assertFalse(service.startup_report['safe_to_resume'])
            self.assertEqual(service.startup_report['unresolved_intents'],['orphan'])
            self.assertEqual(p.executor.read('orphan')['state'],'PREPARED')
            self.assertEqual(a.orders,[])

    def test_old_owner_close_does_not_clear_new_owner(self):
        c,a,p=self.make()
        first=MockService(p);first.close()
        with MockService(p) as second:
            token=c._service_owner_token
            first.close()
            self.assertIs(c._service_owner_token,token)
            self.assertTrue(c._service_owned)
            second.step('3999','3999.2')

    def test_concurrent_initial_snapshots_are_not_overwritten(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            barrier=threading.Barrier(2);results=[];errors=[]
            def create(price):
                try:
                    e=Engine();e.replace_ranges([{'id':'1','price':price,'side':'BUY','volume':'.01'}])
                    barrier.wait()
                    c=Controller(e,d+'/c.db',test_mode=True)
                    results.append(c.data);c.db.close()
                except Exception as exc:errors.append(exc)
            threads=[threading.Thread(target=create,args=(price,)) for price in (4000,4001)]
            for t in threads:t.start()
            for t in threads:t.join()
            self.assertEqual(errors,[])
            self.assertEqual(len(results),2)
            self.assertEqual(results[0],results[1])

    def test_startup_pending_changes_fail_closed(self):
        for field,value in [('price_open',4100),('sl',0),('tp',4200),('volume_current',.02),
                            ('type',5),('magic',999),('symbol','OTHER'),('ticket',99)]:
            with self.subTest(field=field):
                c,a,p=self.make()
                with MockService(p) as first:first.step('3999','3999.2')
                setattr(a.orders[0],field,value)
                with MockService(p) as restarted:
                    self.assertFalse(restarted.startup_report['safe_to_resume'])
                    with self.assertRaises(RuntimeError):restarted.step('3999','3999.2')
                    self.assertEqual(getattr(a.orders[0],field),value)

    def test_startup_pending_without_logical_order_blocks(self):
        c,a,p=self.make()
        with MockService(p) as first:first.step('3999','3999.2')
        c.data['engine']['orders']=[];c._save()
        with MockService(p) as restarted:
            self.assertFalse(restarted.startup_report['safe_to_resume'])
            self.assertTrue(restarted.startup_report['snapshot_mismatches'])

    def test_startup_position_identity_and_phase_changes_block(self):
        for mutation in ('side','entry','phase','missing_logical','partial_unprotected'):
            with self.subTest(mutation=mutation):
                c,a,p=self.make()
                with MockService(p) as first:
                    first.step('3999','3999.2');a.fill_at(51,100)
                    first.step('4000','4000.2')
                b=c.data['bindings']['51']
                if mutation=='side':a.positions[0].type=1
                elif mutation=='entry':a.positions[0].price_open=4100
                elif mutation=='phase':b['phase']='closed'
                elif mutation=='missing_logical':c.data['engine']['positions']=[]
                else:
                    b['phase']='partial_entry'
                    c.data['engine']['positions']=[]
                c._save()
                with MockService(p) as restarted:
                    self.assertFalse(restarted.startup_report['safe_to_resume'])
                    self.assertTrue(restarted.startup_report['snapshot_mismatches'])
                    with self.assertRaises(RuntimeError):restarted.step('4000','4000.2')
