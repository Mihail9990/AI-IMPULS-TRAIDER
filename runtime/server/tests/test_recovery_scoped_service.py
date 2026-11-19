import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.application import MockApplication
from test_recovery_service_loop import LifecycleFake

class ScopedServiceTests(unittest.TestCase):
    def make(self,fake=None,path=':memory:'):
        fake=fake or LifecycleFake()
        a=MockApplication(fake,token='t'*40,account=1,server='demo',symbol='XAUUSD.s',magic=123,database=path)
        self.addCleanup(a.close)
        return fake,a
    def configure(self,c):
        r=c.post('/api/recovery/ranges',headers={'Authorization':'Bearer '+'t'*40},json={
            'request_id':str(uuid4()),'ranges':[{'id':'1','price':4000,'side':'BUY','volume':.01},
                                            {'id':'2','price':4001,'side':'BUY','volume':.01}]})
        self.assertEqual(r.status_code,200,r.text)
    def test_failed_range_does_not_stop_other_protection(self):
        f,a=self.make()
        with TestClient(a.app) as c:
            self.configure(c)
            f.fill_at(52,100)
            original=a.pipeline.protect_quote
            calls=[]
            def protect(bid,ask,range_id=None):
                calls.append(range_id)
                if range_id=='1':raise ValueError('injected range error')
                return original(bid,ask,range_id=range_id)
            f.ask=4001.7
            with patch.object(a.pipeline,'protect_quote',protect):a.step('4001.5','4001.7')
            self.assertEqual(calls,['1','2'])
            self.assertIn('1',a.controller.data['blocked'])
            self.assertNotIn('2',a.controller.data['blocked'])
            self.assertEqual(a.controller.engine().data['positions'][0]['stop_stage'],2)
            self.assertEqual(f.positions[0].sl,4001)
    def test_quarantine_persists_restart_healthy_range_continues(self):
        with tempfile.TemporaryDirectory() as d:
            f,a=self.make(path=d+'/state.db')
            with TestClient(a.app) as c:
                self.configure(c)
                a.pipeline._blocked('1','known local failure')
            _,b=self.make(f,d+'/state.db')
            with TestClient(b.app):
                calls=[]
                original=b.pipeline.protect_quote
                def protect(bid,ask,range_id=None):
                    calls.append(range_id);return original(bid,ask,range_id=range_id)
                with patch.object(b.pipeline,'protect_quote',protect):b.step('3999','3999.2')
                self.assertEqual(calls,['2'])
                self.assertEqual(b.controller.data['blocked']['1'],'known local failure')
    def test_global_history_failure_stops_all_and_api(self):
        f,a=self.make()
        with TestClient(a.app) as c:
            self.configure(c);sent=len(f.sent)
            with patch.object(f,'history_deals_get',return_value=None):
                with self.assertRaises(ValueError):a.step('3999','3999.2')
            self.assertFalse(a.controller._startup_ready)
            self.assertEqual(len(f.sent),sent)
            with self.assertRaises(RuntimeError):a.step('3999','3999.2')
            a.service.reconcile_startup()
            a.step('3999','3999.2')
    def test_quarantine_save_failure_closes_owner(self):
        f,a=self.make()
        with TestClient(a.app) as c:
            self.configure(c)
            with patch.object(a.pipeline,'protect_quote',side_effect=ValueError('bad')):
                with patch.object(a.pipeline,'_blocked',side_effect=OSError('disk')):
                    with self.assertRaises(OSError):a.step('3999','3999.2')
            self.assertTrue(a.service.closed)
            self.assertFalse(a.controller._service_owned)
    def test_scoped_sync_does_not_retire_other_range_pending(self):
        f,a=self.make()
        with TestClient(a.app) as c:
            self.configure(c);sent=len(f.sent)
            a.pipeline.sync_pending(range_id='1')
            self.assertEqual(len(f.orders),2)
            self.assertEqual(len(f.sent),sent)

    def partial(self,f):
        from types import SimpleNamespace as N
        d=f.deal();d.volume=.004
        f.close_deals=[d]
        f.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,
                       price_open=4000,volume=.004,sl=3999.3,tp=0)]
        f.orders[0].volume_current=.006
        f.ask=4000.2

    def test_partial_remainder_cancelled_position_kept_in_application(self):
        f,a=self.make()
        with TestClient(a.app) as c:
            self.configure(c);self.partial(f)
            a.step('4000','4000.2')
            binding=a.controller.data['bindings']['51']
            self.assertTrue(binding['remainder_cancel_confirmed'])
            self.assertEqual(binding['phase'],'open')
            self.assertEqual(f.positions[0].volume,.004)
            self.assertFalse(any(o.ticket==51 for o in f.orders))
            self.assertTrue(any(o.ticket==52 for o in f.orders))
            self.assertEqual(a.controller.data['blocked'],{})
            reverse=next(o for o in a.controller.engine().data['orders'] if o['range_id']=='1')
            self.assertEqual(reverse['volume'],'0.01')

    def test_partial_cancel_timeout_keeps_pending_and_continues_other_range(self):
        f,a=self.make()
        with TestClient(a.app) as c:
            self.configure(c);self.partial(f);f.cancel_timeout=True
            visited=[];original=a.pipeline.protect_quote
            def protect(bid,ask,range_id=None):
                visited.append(range_id);return original(bid,ask,range_id=range_id)
            with patch.object(a.pipeline,'protect_quote',protect):a.step('4000','4000.2')
            self.assertTrue(any(o.ticket==51 for o in f.orders))
            self.assertEqual(a.controller.data['bindings']['51']['phase'],'partial_entry')
            self.assertIn('1',a.controller.data['blocked'])
            self.assertEqual(visited,['2'])

    def test_partial_cancel_repoll_observes_fill_exit_race(self):
        f,a=self.make()
        with TestClient(a.app) as c:
            self.configure(c);self.partial(f)
            original=f.order_send
            def send(request):
                result=original(request)
                if request['action']==f.TRADE_ACTION_REMOVE:
                    incoming=f.deal(ticket=101);incoming.volume=.002
                    outgoing=f.deal(ticket=102,entry=1);outgoing.volume=.002
                    f.close_deals.extend([incoming,outgoing])
                return result
            with patch.object(f,'order_send',send):a.step('4000','4000.2')
            self.assertIn('101',a.controller.data['deals'])
            self.assertIn('102',a.controller.data['deals'])
            self.assertIn('1',a.controller.data['blocked'])
            self.assertNotIn('partial_entry_accepted',a.controller.data['bindings']['51'])
            self.assertTrue(any(o.ticket==52 for o in f.orders))
