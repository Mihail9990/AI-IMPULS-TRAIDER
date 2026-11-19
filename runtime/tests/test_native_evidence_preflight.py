import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from datetime import datetime, timezone
from decimal import Decimal

spec=importlib.util.spec_from_file_location('prerequisite',Path(__file__).parents[1]/'native_evidence_preflight.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Pending(Exception): pass
class TestPrerequisites(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.terminal=self.root/'terminal64.exe';self.terminal.touch()
        self.now=datetime.now(timezone.utc)
        self.account=NS(login=1,server='Broker',trade_mode=2)
        self.info=NS(path=str(self.root),data_path=str(self.root),connected=True)
        self.deal=NS(ticket=11,order=12,position_id=13,symbol='XAUUSD.s',type=1,entry=1,reason=4,time_msc=int(self.now.timestamp()*1000),volume=.01)
        self.history=[self.deal];self.recheck=None;self.acls=[];self.bindings=[];self.collect_hook=None
        owner=self
        class Bridge:
            def __init__(self,p): pass
            def publish(self,b): owner.bindings.append(b)
            def collect(self,b):
                if owner.collect_hook: return owner.collect_hook()
                return Decimal('4000')
        self.api=NS(ACCOUNT_TRADE_MODE_REAL=2,DEAL_ENTRY_OUT=1,DEAL_REASON_SL=4,DEAL_TYPE_BUY=0,DEAL_TYPE_SELL=1,
            account_info=lambda:self.account,terminal_info=lambda:self.info,
            history_deals_get=lambda *a,**kw: self.recheck if kw and self.recheck is not None else self.history)
        self.kw=dict(terminal=self.terminal,account=1,server='Broker',symbol='XAUUSD.s',magic=77,
            _bridge_type=Bridge,_binding_type=NS,_pending_error=Pending,_acl_check=self.acls.append,_now=self.now)
    def call(self): return m.verify_native_prerequisites(self.api,**self.kw)
    def test_success_fresh_nonce_no_lifecycle_or_trading(self):
        self.assertTrue(self.call()['evidence_verified']);self.call()
        self.assertNotEqual(self.bindings[0].request_id,self.bindings[1].request_id)
        self.assertEqual(len(self.acls),4)
    def test_demo_wrong_identity_paths_and_disconnect(self):
        for obj,field,bad in [(self.account,'trade_mode',0),(self.account,'login',2),(self.account,'server','Other'),(self.info,'path','/other'),(self.info,'connected',False)]:
            old=getattr(obj,field);setattr(obj,field,bad)
            with self.assertRaises(m.CheckError):self.call()
            setattr(obj,field,old)
        self.kw['expected_data_path']='/other'
        with self.assertRaises(m.CheckError):self.call()
        self.assertEqual(self.bindings,[])
    def test_none_empty_duplicate_history(self):
        for history in (None,[],[self.deal,self.deal]):
            self.history=history
            with self.assertRaises(m.CheckError):self.call()
        self.assertEqual(self.bindings,[])
    def test_acl_failure_before_publish(self):
        def fail(p):raise m.CheckError('ACL')
        self.kw['_acl_check']=fail
        with self.assertRaises(m.CheckError):self.call()
        self.assertEqual(self.bindings,[])
    def test_account_or_history_changed_after_response(self):
        def changed():self.account.login=2;return Decimal('4000')
        self.collect_hook=changed
        with self.assertRaises(m.CheckError):self.call()
        self.account.login=1;self.collect_hook=None;self.recheck=[]
        with self.assertRaises(m.CheckError):self.call()
    def test_timeout_no_retry_publish(self):
        ticks=iter([0,0,21]);self.kw['_clock']=lambda:next(ticks);self.kw['_sleep']=lambda _:None
        def pending():raise Pending()
        self.collect_hook=pending
        with self.assertRaises(m.CheckError):self.call()
        self.assertEqual(len(self.bindings),1)
    def test_final_acl_and_data_change_rejected(self):
        def acl(p):
            self.acls.append(p)
            if len(self.acls)==2:self.info.data_path='/changed'
        self.kw['_acl_check']=acl
        with self.assertRaises(m.CheckError):self.call()
    def test_bad_stop(self):
        for bad in (Decimal('NaN'),Decimal('0'),1.0):
            self.collect_hook=lambda:bad
            with self.assertRaises(m.CheckError):self.call()

if __name__=='__main__':unittest.main()
