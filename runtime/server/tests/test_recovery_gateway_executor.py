import tempfile
import unittest
from types import SimpleNamespace as N
from recovery.gateway_transport import GatewayTransport
from recovery.executor import Executor
from test_recovery_mt5_transport import FakeMT5,REQUEST,EXPECTED

class GatewayExecutorTests(unittest.TestCase):
    def make(self,api=None,path=':memory:'):
        api=api or FakeMT5()
        return api,Executor(GatewayTransport(api,account=1,server='demo'),path)
    def test_durable_send_verify_and_restart_no_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            api,e=self.make(path=d+'/outbox.db');e.prepare('a',REQUEST,EXPECTED)
            self.assertEqual(e.submit_once('a')['state'],'ACKNOWLEDGED')
            self.assertEqual(e.reconcile('a')['state'],'VERIFIED')
            api,e2=self.make(api,d+'/outbox.db')
            self.assertEqual(e2.submit_once('a')['state'],'VERIFIED')
            self.assertEqual(len(api.sent),1)
            e.db.close();e2.db.close()
    def test_check_failure_prevents_submission(self):
        for result in (None,N(retcode=1),N(retcode=10009)):
            api=FakeMT5();api.order_check=lambda r:result
            api,e=self.make(api);e.prepare('a',REQUEST,EXPECTED)
            self.assertEqual(e.submit_once('a')['state'],'REJECTED')
            self.assertEqual(api.sent,[])
    def test_send_timeout_remains_unknown_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            api=FakeMT5();calls=[]
            def send(r):calls.append(r);raise TimeoutError('lost reply')
            api.order_send=send
            api,e=self.make(api,d+'/outbox.db');e.prepare('a',REQUEST,EXPECTED)
            self.assertEqual(e.submit_once('a')['state'],'UNKNOWN')
            api,e2=self.make(api,d+'/outbox.db')
            self.assertEqual(e2.submit_once('a')['state'],'UNKNOWN')
            self.assertEqual(len(calls),1)
            e.db.close();e2.db.close()
    def test_changed_account_at_second_check_never_sends(self):
        api=FakeMT5();calls=[]
        def check(r):
            calls.append(r)
            if len(calls)==2:api.login=2
            return N(retcode=0)
        api.order_check=check
        api,e=self.make(api);e.prepare('a',REQUEST,EXPECTED)
        self.assertEqual(e.submit_once('a')['state'],'UNKNOWN')
        self.assertEqual(api.sent,[])
    def test_real_object_rejected(self):
        with self.assertRaises(RuntimeError):GatewayTransport(N(),account=1,server='demo')

    def close_case(self,mode,flags):
        from test_recovery_close_api import APICloseFake
        from recovery.mt5_plan import close_position
        api=APICloseFake();api.ask=4000.2
        original=api.symbol_info
        def info(name):
            s=original(name);s.trade_exemode=mode;s.filling_mode=flags;return s
        api.symbol_info=info
        api.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,
                         price_open=4000,volume=.01,sl=3999.3,tp=0)]
        transport=GatewayTransport(api,account=1,server='demo')
        s,q=transport._symbol('XAUUSD.s')
        request=close_position(transport._normalize_position(api.positions[0]),s,q,
                              expected_ticket=70,expected_identifier=700,expected_magic=123,
                              filling='ORDER_FILLING_IOC')
        expected=dict(EXPECTED,symbol='XAUUSD.s',magic=123,identifier=700)
        return api,transport,request,expected

    def test_market_execution_integrated_close_omits_price(self):
        api,t,r,x=self.close_case(2,2);e=Executor(t);e.prepare('close',r,x)
        self.assertEqual(e.submit_once('close')['state'],'ACKNOWLEDGED')
        self.assertNotIn('price',api.sent[-1])
        self.assertEqual(e.reconcile('close')['state'],'VERIFIED')

    def test_execution_filling_matrix(self):
        for mode,flags,allowed in [(0,0,True),(1,0,True),(2,0,False),(2,1,False),
                                   (2,2,True),(3,0,False),(3,2,True),(99,3,False)]:
            with self.subTest(mode=mode,flags=flags):
                api,t,r,x=self.close_case(mode,flags);e=Executor(t);e.prepare('close',r,x)
                result=e.submit_once('close')
                self.assertEqual(result['state'],'ACKNOWLEDGED' if allowed else 'REJECTED')
                self.assertEqual(len(api.sent),1 if allowed else 0)
                if allowed and mode!=2:self.assertIn('price',api.sent[-1])

    def test_pending_return_valid_even_market_execution(self):
        api=FakeMT5();original=api.symbol_info
        def info(name):
            s=original(name);s.trade_exemode=2;s.filling_mode=0;return s
        api.symbol_info=info
        api,e=self.make(api);e.prepare('pending',REQUEST,EXPECTED)
        self.assertEqual(e.submit_once('pending')['state'],'ACKNOWLEDGED')
        self.assertIn('price',api.sent[-1])

    def test_execution_mode_changes_after_check_no_send(self):
        api,t,r,x=self.close_case(2,2)
        original=api.order_check
        def check(native):
            result=original(native)
            old=api.symbol_info
            def changed(name):
                s=old(name);s.trade_exemode=3;return s
            api.symbol_info=changed
            return result
        api.order_check=check
        # Direct native boundary demonstrates revalidation on final check/send gap.
        t._order_check(r)
        with self.assertRaises(ValueError):t._order_send(r)
        self.assertEqual(api.sent,[])

    def test_pending_scope_mismatch_or_missing_rejected_before_journal_and_send(self):
        for field,value in [('symbol','OTHER'),('magic',999)]:
            with self.subTest(field=field):
                api,e=self.make();request=dict(REQUEST,**{field:value})
                with self.assertRaises(ValueError):e.prepare('wrong',request,EXPECTED)
                with self.assertRaises(ValueError):e.transport.preflight(request,EXPECTED)
                missing=dict(EXPECTED);missing.pop(field)
                with self.assertRaises(ValueError):e.prepare('missing',REQUEST,missing)
                with self.assertRaises(ValueError):e.transport.preflight(REQUEST,missing)
                self.assertEqual(api.sent,[])
                self.assertEqual(e.db.execute('SELECT count(*) FROM recovery_outbox').fetchone()[0],0)

    def test_all_action_scope_fields_must_match(self):
        requests=[{'action':'TRADE_ACTION_REMOVE','order':51},
                  {'action':'TRADE_ACTION_SLTP','position':70,'sl':'3999.3','tp':'0'},
                  {'action':'TRADE_ACTION_DEAL','position':70,'symbol':'XAUUSD.s','magic':123,
                   'type':'ORDER_TYPE_SELL','volume':'0.01','price':'4000.0','type_filling':'ORDER_FILLING_IOC'}]
        for base in requests:
            for field,value in [('symbol','OTHER'),('magic',999)]:
                with self.subTest(action=base['action'],field=field):
                    api,t,r,x=self.close_case(2,2);e=Executor(t)
                    bad=dict(base,**{field:value})
                    with self.assertRaises(ValueError):e.prepare('scope',bad,x)
                    with self.assertRaises(ValueError):t.preflight(bad,x)
                    self.assertEqual(api.sent,[])
                    self.assertEqual(e.db.execute('SELECT count(*) FROM recovery_outbox').fetchone()[0],0)
                    missing=dict(x);missing.pop(field)
                    good=dict(base,**{field:x[field]})
                    with self.assertRaises(ValueError):e.prepare('missing-scope',good,missing)
                    with self.assertRaises(ValueError):t.preflight(good,missing)

    def test_deal_requires_explicit_request_scope(self):
        for field in ('symbol','magic'):
            api,t,r,x=self.close_case(2,2);r.pop(field);e=Executor(t)
            with self.assertRaises(ValueError):e.prepare('missing',r,x)
            with self.assertRaises(ValueError):t.preflight(r,x)
            self.assertEqual(api.sent,[])
