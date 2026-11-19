import tempfile
import unittest
from dataclasses import replace
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.application import MockApplication
from recovery.ea_stop_evidence import StopEvidenceUnavailable
from test_recovery_service_loop import LifecycleFake

class EvidenceLifecycleTests(unittest.TestCase):
    def make(self,fake=None,path=':memory:'):
        f=fake or LifecycleFake()
        a=MockApplication(f,token='t'*40,account=1,server='demo',symbol='XAUUSD.s',magic=123,
                          database=path,require_stop_evidence=True)
        self.addCleanup(a.close);return f,a
    def enter(self,c,f,a,support=False):
        r=c.post('/api/recovery/ranges',headers={'Authorization':'Bearer '+'t'*40},json={
            'request_id':str(uuid4()),'ranges':[{'id':'1','price':4000,'side':'BUY','volume':.01}]})
        self.assertEqual(r.status_code,200)
        f.fill_at(51,100);a.step('4000','4000.2')
        if support:
            f.ask=4000.7;a.step('4000.5','4000.7')
        f.support_close(101);f.ask=4000.2
    def payload(self,b,sl):
        return ('\t'.join(['RECOVERY_DEAL_SL_V1',b.request_id,str(b.account),b.server,
            str(b.deal),str(b.order),str(b.position),b.symbol,b.exit_side,str(b.time_msc),
            format(b.volume,'.16f'),format(sl,'.16f'),'END'])+'\r\n').encode()
    def test_full_and_support_only_with_bound_evidence(self):
        from decimal import Decimal
        for support,sl,count in [(False,Decimal('3999.3'),1),(True,Decimal('4000'),0)]:
            with self.subTest(support=support):
                f,a=self.make()
                with TestClient(a.app) as c:
                    self.enter(c,f,a,support)
                    b=a.service.prepare_stop_evidence_requests()[0]
                    a.pipeline.stop_evidence.supply(b.deal,self.payload(b,sl))
                    a.step('4000','4000.2')
                    self.assertEqual(a.controller.engine()._range('1')['full_sl_count'],count)
                    self.assertEqual(a.controller.data['blocked'],{})
                    sent=len(f.sent);a.step('4000','4000.2')
                    self.assertEqual(len(f.sent),sent)
    def test_missing_evidence_blocks_and_later_supply_does_not_unblock(self):
        from decimal import Decimal
        f,a=self.make()
        with TestClient(a.app) as c:
            self.enter(c,f,a)
            a.step('4000','4000.2')
            self.assertIn('1',a.controller.data['blocked'])
            b=a.service.prepare_stop_evidence_requests()[0]
            a.pipeline.stop_evidence.supply(b.deal,self.payload(b,Decimal('3999.3')))
            a.step('4000','4000.2')
            self.assertEqual(a.controller.engine()._range('1')['full_sl_count'],0)
            self.assertNotIn('101',a.controller.data['deals'])
    def test_mismatched_or_changed_payload_rejected(self):
        from decimal import Decimal
        f,a=self.make()
        with TestClient(a.app) as c:
            self.enter(c,f,a)
            b=a.service.prepare_stop_evidence_requests()[0]
            with self.assertRaises(StopEvidenceUnavailable):
                a.pipeline.stop_evidence.supply(b.deal,self.payload(replace(b,position=999),Decimal('3999.3')))
            a.pipeline.stop_evidence.supply(b.deal,self.payload(b,Decimal('3999.3000000000001000')))
            with self.assertRaises(StopEvidenceUnavailable):
                a.pipeline.stop_evidence.supply(b.deal,self.payload(b,Decimal('3999.3')))
            a.step('4000','4000.2')
            self.assertIn('1',a.controller.data['blocked'])
            self.assertEqual(a.controller.engine()._range('1')['full_sl_count'],0)
    def test_restart_preserves_evidence_does_not_bypass_unresolved_exit(self):
        from decimal import Decimal
        with tempfile.TemporaryDirectory() as d:
            f,a=self.make(path=d+'/state.db')
            with TestClient(a.app) as c:
                self.enter(c,f,a)
                binding=a.service.prepare_stop_evidence_requests()[0]
                payload=self.payload(binding,Decimal('3999.3'))
                a.pipeline.stop_evidence.supply(binding.deal,payload)
            _,b=self.make(f,d+'/state.db')
            with TestClient(b.app):
                self.assertEqual(b.controller.data['stop_evidence']['101']['payload'],payload.decode())
                self.assertFalse(b.controller._startup_ready)
                with self.assertRaises(RuntimeError):b.step('4000','4000.2')
                self.assertEqual(b.controller.engine()._range('1')['full_sl_count'],0)

    def test_restart_cannot_disable_strict_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            f,a=self.make(path=d+'/state.db')
            with TestClient(a.app):pass
            legacy=MockApplication(f,token='t'*40,account=1,server='demo',symbol='XAUUSD.s',
                                   magic=123,database=d+'/state.db')
            self.addCleanup(legacy.close)
            with self.assertRaisesRegex(ValueError,'cannot be disabled'):
                with TestClient(legacy.app):pass

    def test_nonowner_stale_instance_cannot_write_evidence(self):
        from decimal import Decimal
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path=d+'/state.db';f,a=self.make(path=path)
            with TestClient(a.app) as client:
                self.enter(client,f,a)
                binding=a.service.prepare_stop_evidence_requests()[0]
                payload=self.payload(binding,Decimal('3999.3'))
                _,stale=self.make(f,path)
                a.pipeline.stop_evidence.supply(binding.deal,payload)
                a.step('4000','4000.2')
                conn=sqlite3.connect(path)
                self.addCleanup(conn.close)
                before=conn.execute('SELECT data FROM recovery_controller WHERE id=1').fetchone()[0]
                b=stale.controller.data['bindings']['51'];deal=f.close_deals[-1]
                for operation in (lambda:stale.pipeline.stop_evidence.supply(binding.deal,payload),
                                  lambda:stale.pipeline.stop_evidence.request(deal,b),
                                  lambda:stale.pipeline.stop_evidence.resolve(deal,b),
                                  lambda:stale.pipeline.stop_evidence.bind_owner(object())):
                    with self.assertRaises(RuntimeError):operation()
                    self.assertEqual(conn.execute('SELECT data FROM recovery_controller WHERE id=1').fetchone()[0],before)
            with self.assertRaises(RuntimeError):
                a.pipeline.stop_evidence.supply(binding.deal,payload)

    def test_binary64_tail_maps_to_exact_confirmed_tick_level(self):
        from decimal import Decimal
        f,a=self.make()
        with TestClient(a.app) as c:
            self.enter(c,f,a)
            b=a.service.prepare_stop_evidence_requests()[0]
            a.pipeline.stop_evidence.supply(b.deal,self.payload(b,Decimal('3999.3000000000001819')))
            a.step('4000','4000.2')
            self.assertEqual(a.controller.engine()._range('1')['full_sl_count'],1)
            self.assertEqual(a.controller.data['blocked'],{})
