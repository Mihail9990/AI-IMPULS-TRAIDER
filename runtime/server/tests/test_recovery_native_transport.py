import tempfile
import unittest
from types import SimpleNamespace as N
from recovery.native_transport import NativeMT5Transport
from recovery.native_preflight import TerminalBinding
from recovery.executor import Executor
from test_recovery_native_preflight import NativeFake
from test_recovery_mt5_transport import REQUEST,EXPECTED

class NativeSendFake(NativeFake):
    def __init__(self):super().__init__();self.retcode=self.TRADE_RETCODE_PLACED;self.lose=False;self.after_send_switch=False
    def history_deals_get(self,*args):return []
    def order_send(self,r):
        self.sent.append(r)
        if self.lose:raise TimeoutError('lost reply')
        if self.after_send_switch:self.login=2
        return N(retcode=self.retcode,order=51,deal=0)

class NativeTransportTests(unittest.TestCase):
    def make(self):
        api=NativeSendFake();active={'value':True}
        t=NativeMT5Transport(api,TerminalBinding(1,'demo','XAUUSD.s',frozenset({123})),
                             owner_guard=lambda:active['value'])
        return api,t,active
    def test_native_mapping_without_test_marker_and_one_use(self):
        a,t,_=self.make();self.assertFalse(a._RECOVERY_TEST_DOUBLE)
        t.preflight(REQUEST,EXPECTED);r=t.send(REQUEST)
        self.assertEqual(r['status'],'accepted');self.assertEqual(a.sent[0]['action'],5)
        with self.assertRaises(ValueError):t.send(REQUEST)
        self.assertEqual(len(a.sent),1)
    def test_owner_loss_consumes_preflight(self):
        a,t,active=self.make();t.preflight(REQUEST,EXPECTED);active['value']=False
        with self.assertRaises(RuntimeError):t.send(REQUEST)
        active['value']=True
        with self.assertRaises(ValueError):t.send(REQUEST)
        self.assertEqual(a.sent,[])
    def test_account_switch_or_policy_change_before_send(self):
        for change in (lambda a:setattr(a,'login',2),lambda a:setattr(a,'filling',1)):
            a,t,_=self.make();t.preflight(REQUEST,EXPECTED);change(a)
            with self.assertRaises(ValueError):t.send(REQUEST)
            self.assertEqual(a.sent,[])
    def test_changed_request_consumes_preflight(self):
        a,t,_=self.make();t.preflight(REQUEST,EXPECTED)
        with self.assertRaises(ValueError):t.send(dict(REQUEST,price='4001'))
        with self.assertRaises(ValueError):t.send(REQUEST)
        self.assertEqual(a.sent,[])
    def test_executor_lost_response_never_resubmits_even_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            a,t,_=self.make();a.lose=True;e=Executor(t,d+'/outbox.db')
            e.prepare('one',REQUEST,EXPECTED);self.assertEqual(e.submit_once('one')['state'],'UNKNOWN')
            e.db.close();second=Executor(t,d+'/outbox.db')
            self.assertEqual(second.submit_once('one')['state'],'UNKNOWN')
            self.assertEqual(len(a.sent),1);second.db.close()
    def test_partial_unknown_and_account_switch_after_send(self):
        for mode in ('partial','switch'):
            a,t,_=self.make()
            if mode=='partial':a.retcode=a.TRADE_RETCODE_DONE_PARTIAL
            else:a.after_send_switch=True
            e=Executor(t);self.addCleanup(e.db.close);e.prepare('one',REQUEST,EXPECTED)
            self.assertEqual(e.submit_once('one')['state'],'UNKNOWN')
            self.assertEqual(len(a.sent),1)
    def test_failed_check_does_not_allow_send(self):
        a,t,_=self.make();a.order_check=lambda request:N(retcode=123)
        with self.assertRaises(ValueError):t.preflight(REQUEST,EXPECTED)
        with self.assertRaises(ValueError):t.send(REQUEST)
        self.assertEqual(a.sent,[])

    def test_account_switch_in_final_metadata_lookup_prevents_send(self):
        a,t,_=self.make();t.preflight(REQUEST,EXPECTED)
        original=t.validator._execution_policy;calls=[]
        def policy(request):
            result=original(request);calls.append(request)
            if len(calls)==3:a.login=2  # check, validate, final fingerprint lookup
            return result
        t.validator._execution_policy=policy
        with self.assertRaises(ValueError):t.send(REQUEST)
        self.assertEqual(len(calls),3);self.assertEqual(a.sent,[])
        a.login=1
        with self.assertRaises(ValueError):t.send(REQUEST)

    def test_flat_cancel_guard_rechecks_after_final_metadata(self):
        a,t,_=self.make()
        a.orders=[N(ticket=50,magic=123,symbol='XAUUSD.s',type=4,price_open=4000,
                    sl=3999.3,tp=0,volume_current=.01,volume_initial=.01,state=1)]
        request={'action':'TRADE_ACTION_REMOVE','order':50}
        expected=dict(EXPECTED,require_no_owned_positions=True)
        t.preflight(request,expected)
        original=t.validator._execution_policy;calls=[]
        def policy(req):
            result=original(req);calls.append(1)
            if len(calls)==3:
                a.positions=[N(ticket=77,identifier=88,magic=123,symbol='XAUUSD.s',type=0,
                    volume=.01,price_open=4000,sl=3999.3,tp=0)]
            return result
        t.validator._execution_policy=policy
        with self.assertRaises(ValueError):t.send(request)
        self.assertEqual(a.sent,[])

    def test_flat_cancel_marker_cannot_be_false_or_skip_unavailable_positions(self):
        a,t,_=self.make()
        a.orders=[N(ticket=50,magic=123,symbol='XAUUSD.s',type=4,price_open=4000,
                    sl=3999.3,tp=0,volume_current=.01,volume_initial=.01,state=1)]
        request={'action':'TRADE_ACTION_REMOVE','order':50}
        with self.assertRaises(ValueError):t.preflight(request,dict(EXPECTED,require_no_owned_positions=False))
        a.positions_get=lambda **kwargs:None
        with self.assertRaises(ValueError):t.preflight(request,dict(EXPECTED,require_no_owned_positions=True))
        self.assertEqual(a.sent,[])
