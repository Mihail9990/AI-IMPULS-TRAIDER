import copy
import tempfile
import unittest
from recovery.executor import Executor

REQUEST={'action':'TRADE_ACTION_PENDING','symbol':'XAUUSD.s','magic':123,
         'type':'ORDER_TYPE_BUY_STOP','price':'4000','sl':'3999.3','tp':'0','volume':'.01'}
EXPECTED={'account':1,'server':'demo','symbol':'XAUUSD.s','magic':123}

class Fake:
    def __init__(self):
        self.sent=0;self.fail=False;self.reject=False
        self.state={'account':1,'server':'demo','orders':[],'positions':[],'history_orders':[]}
    def preflight(self,r,x):
        if self.state['account']!=x['account']:raise ValueError('Wrong account')
    def send(self,r):
        self.sent+=1
        if self.reject:return {'status':'rejected'}
        if r['action']=='TRADE_ACTION_PENDING':self.state['orders']=[dict(r,ticket=50)]
        if self.fail:raise TimeoutError('lost response after effect')
        return {'status':'accepted','order':50}
    def snapshot(self,x):return copy.deepcopy(self.state)

class ExecutorTests(unittest.TestCase):
    def test_ack_is_not_success_until_verified(self):
        f=Fake();e=Executor(f);e.prepare('1',REQUEST,EXPECTED)
        self.assertEqual(e.submit_once('1')['state'],'ACKNOWLEDGED')
        self.assertEqual(e.reconcile('1')['state'],'VERIFIED')
        e.submit_once('1');self.assertEqual(f.sent,1)

    def test_timeout_after_effect_never_retries_or_guesses_ticket(self):
        f=Fake();f.fail=True
        with tempfile.TemporaryDirectory() as d:
            e=Executor(f,d+'/db');e.prepare('1',REQUEST,EXPECTED)
            self.assertEqual(e.submit_once('1')['state'],'UNKNOWN');e.db.close()
            e=Executor(f,d+'/db')
            self.assertEqual(e.submit_once('1')['state'],'UNKNOWN')
            self.assertEqual(e.reconcile('1')['state'],'UNKNOWN')
            self.assertEqual(f.sent,1);e.db.close()

    def test_changed_request_rejected(self):
        e=Executor(Fake());e.prepare('1',REQUEST,EXPECTED)
        with self.assertRaises(ValueError):e.prepare('1',dict(REQUEST,price='4001'),EXPECTED)

    def test_preflight_blocks_send(self):
        f=Fake();f.state['account']=2;e=Executor(f);e.prepare('1',REQUEST,EXPECTED)
        self.assertEqual(e.submit_once('1')['state'],'REJECTED');self.assertEqual(f.sent,0)

    def test_mismatched_level_not_verified(self):
        f=Fake();e=Executor(f);e.prepare('1',REQUEST,EXPECTED);e.submit_once('1')
        f.state['orders'][0]['sl']='3998'
        self.assertEqual(e.reconcile('1')['state'],'UNKNOWN')

    def test_fill_before_read(self):
        f=Fake();e=Executor(f);e.prepare('1',REQUEST,EXPECTED);e.submit_once('1')
        f.state['orders']=[];f.state['history_orders']=[{'ticket':50,'magic':123,'symbol':'XAUUSD.s','state':'filled','type':'ORDER_TYPE_BUY_STOP','volume_initial':'.01'}]
        self.assertEqual(e.reconcile('1')['evidence']['kind'],'order_filled_before_verification')

    def test_cancel_absence_not_enough(self):
        f=Fake();e=Executor(f);x=dict(EXPECTED,magic=123,symbol='XAUUSD.s')
        e.prepare('1',{'action':'TRADE_ACTION_REMOVE','order':50},x);e.submit_once('1')
        self.assertEqual(e.reconcile('1')['state'],'UNKNOWN')
        f.state['history_orders']=[{'ticket':50,'magic':123,'symbol':'XAUUSD.s','state':'cancelled'}]
        self.assertEqual(e.reconcile('1')['state'],'VERIFIED')

    def test_sl_change_not_verified_for_different_position(self):
        f=Fake();e=Executor(f);x=dict(EXPECTED,magic=123,symbol='XAUUSD.s',identifier=99)
        e.prepare('1',{'action':'TRADE_ACTION_SLTP','position':50,'sl':'4000','tp':'0'},x);e.submit_once('1')
        f.state['positions']=[{'ticket':50,'identifier':98,'magic':123,'symbol':'XAUUSD.s','sl':'4000','tp':'0'}]
        self.assertEqual(e.reconcile('1')['state'],'UNKNOWN')
        f.state['positions'][0]['identifier']=99
        self.assertEqual(e.reconcile('1')['state'],'VERIFIED')
    def test_two_executor_instances_atomic_claim_only_one_send(self):
        import threading
        class RacingFake(Fake):
            def __init__(self):
                super().__init__();self.barrier=threading.Barrier(2)
            def preflight(self,r,x):
                self.barrier.wait(timeout=3)
                super().preflight(r,x)
        f=RacingFake()
        with tempfile.TemporaryDirectory() as d:
            a=Executor(f,d+'/db');b=Executor(f,d+'/db')
            a.prepare('1',REQUEST,EXPECTED)
            errors=[]
            def invoke(e):
                try:e.submit_once('1')
                except Exception as exc:errors.append(exc)
            t1=threading.Thread(target=invoke,args=(a,));t2=threading.Thread(target=invoke,args=(b,))
            t1.start();t2.start();t1.join(5);t2.join(5)
            self.assertFalse(t1.is_alive());self.assertFalse(t2.is_alive())
            self.assertEqual(errors,[]);self.assertEqual(f.sent,1)
            self.assertEqual(a.read('1')['state'],'ACKNOWLEDGED')
            a.db.close();b.db.close()
    def test_refresh_does_not_reuse_stale_verified_evidence(self):
        f=Fake();e=Executor(f);e.prepare('1',REQUEST,EXPECTED);e.submit_once('1')
        self.assertEqual(e.reconcile('1')['state'],'VERIFIED')
        f.state['orders'][0]['sl']='3998'
        self.assertEqual(e.reconcile('1',refresh=True)['state'],'UNKNOWN')
        e.submit_once('1');self.assertEqual(f.sent,1)
