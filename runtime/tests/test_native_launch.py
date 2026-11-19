"""Mocked launch lifecycle only; never imports MT5 or runs any process."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from datetime import datetime,timezone
from unittest.mock import patch,MagicMock
import native_launch as n

class Child:
    def __init__(self):self.stdin=io.BytesIO();self.code=None;self.terminated=False
    def poll(self):return self.code
    def wait(self,timeout):self.code=0
    def terminate(self):self.terminated=True;self.code=0
    def kill(self):raise AssertionError('kill forbidden')

class LaunchTests(unittest.TestCase):
    def fixture(self,root):
        root=Path(root);(root/'terminal64.exe').write_bytes(b'MZ')
        (root/'tools/caddy').mkdir(parents=True);(root/'tools/caddy/caddy.exe').write_bytes(b'caddy')
        (root/'.venv-native/Scripts').mkdir(parents=True);(root/'.venv-native/Scripts/python.exe').write_bytes(b'python')
        return n.validate_scope(root/'terminal64.exe',123,'Bybit','XAUUSD.s',456)
    def test_confirm_before_process(self):
        x=n.NativeLaunchSession('/unused','/unused',popen=lambda *a,**k:self.fail('process'))
        with self.assertRaises(n.ConnectionError):x.start('8.8.8.8','x'*32,'/unused',terminal='none',account=1,broker_server='b',symbol='s',magic=1,expected_data_path='/unused')
    def test_owner_scope_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            scope=self.fixture(d);p=n.scoped_database(d,scope,resume_confirmed=False)
            self.assertEqual(p.name,'state.sqlite3')
            self.assertEqual(p,n.scoped_database(d,scope,resume_confirmed=False))  # orphaned marker after failed preflight
            self.assertFalse(p.exists())
            sidecar=p.with_name(p.name+'-wal');sidecar.touch()
            with self.assertRaises(n.ConnectionError):n.scoped_database(d,scope,resume_confirmed=False)
            sidecar.unlink();p.touch()
            with self.assertRaises(n.ConnectionError):n.scoped_database(d,scope,resume_confirmed=False)
            self.assertEqual(p,n.scoped_database(d,scope,resume_confirmed=True))
            with self.assertRaises(n.ConnectionError):n.scoped_database(d,dict(scope,account=555),resume_confirmed=True)
    def test_unknown_db_never_adopted(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)/'native-real';folder.mkdir();(folder/'state.sqlite3').write_bytes(b'old')
            with self.assertRaises(n.ConnectionError):n.scoped_database(d,{},resume_confirmed=True)
    def test_real_required(self):
        scope={'account':1,'server':'b'}
        for mode in (None,1,2):
            with self.assertRaises(n.ConnectionError):n.verify_real(N(ACCOUNT_TRADE_MODE_REAL=0,account_info=lambda:N(login=1,server='b',trade_mode=mode)),scope)
        n.verify_real(N(ACCOUNT_TRADE_MODE_REAL=0,account_info=lambda:N(login=1,server='b',trade_mode=0)),scope)
    def test_ready_context_fresh_real(self):
        scope=dict(account=1,server='b',symbol='s',magic=1)
        body={'instance':'nonce','widget':{'mode':'native','account_trade_mode':'real','snapshot':{'symbol':'s','context_id':n.context_id(scope),'status':'fresh','observed_at':datetime.now(timezone.utc).isoformat()}}}
        n.validate_ready(body,scope,'nonce')
        for field,value in [('status','unavailable'),('context_id','other'),('observed_at','2000-01-01T00:00:00+00:00')]:
            bad=json.loads(json.dumps(body));bad['widget']['snapshot'][field]=value
            with self.assertRaises(n.ConnectionError):n.validate_ready(bad,scope,'nonce')
    def test_whitelist(self):
        cfg=n.native_caddy_config('8.8.8.8','storage')
        self.assertNotIn('/_local',cfg);self.assertNotIn('/api/*',cfg)
        self.assertIn('method POST',cfg);self.assertIn('method GET',cfg)
        self.assertIn('respond "NOT_ALLOWED" 404',cfg);self.assertIn('admin off',cfg)
    def test_mock_start_order_secret_and_graceful_stop(self):
        with tempfile.TemporaryDirectory() as d:
            scope=self.fixture(d);calls=[];children=[];phases=[]
            def popen(args,**kwargs):
                calls.append((args,kwargs));c=Child();children.append(c);return c
            x=n.NativeLaunchSession(d,d,popen=popen,run=lambda *a,**k:N(returncode=0,stdout='{"admin":{"disabled":true}}'),readiness=lambda *a:phases.append(a[-1]),prerequisites=lambda s:None)
            with patch.object(n,'EXE_SHA256',n.hashlib.sha256(b'caddy').hexdigest()),patch.object(n.socket,'socket',MagicMock()):
                x.start('8.8.8.8','SECRET'*8,d,terminal=scope['terminal'],account=123,broker_server='Bybit',symbol='XAUUSD.s',magic=456,expected_data_path=d,owner_confirmed=True)
            self.assertEqual(phases,['local','https','arm']);self.assertEqual(len(children),2)
            self.assertNotIn('SECRET',str(calls));self.assertIn(b'SECRET',children[0].stdin.getvalue())
            x.stop();self.assertFalse(children[0].terminated);self.assertTrue(children[1].terminated)
            self.assertIn(b'"stop"',children[0].stdin.getvalue());self.assertFalse(x.children)
    def test_evidence_signature_compatibility(self):
        import inspect
        from native_evidence_preflight import verify_native_prerequisites
        inspect.signature(verify_native_prerequisites).bind(object(), terminal='terminal',account=1,server='server',symbol='symbol',magic=1,expected_data_path='data')
    def test_stop_timeout_retains_owner_and_never_kills(self):
        import subprocess
        child=Child()
        def timeout(timeout):raise subprocess.TimeoutExpired('native',timeout)
        child.wait=timeout
        x=n.NativeLaunchSession('/unused','/unused');x._native_child=child;x._nonce='nonce';x.children=[child]
        with self.assertRaises(n.ConnectionError):x.stop()
        self.assertEqual(x.children,[child]);self.assertFalse(child.terminated)
    def test_https_wrong_instance_never_receives_token(self):
        conn=MagicMock();response=MagicMock();response.status=401;response.read.return_value=b'{}';response.getheader.return_value='other';conn.getresponse.return_value=response
        x=n.NativeLaunchSession('/unused','/unused')
        with patch.object(n.http.client,'HTTPSConnection',return_value=conn):
            with self.assertRaises(n.ConnectionError):x._native_ready(Child(),'secret','nonce','8.8.8.8',{},'https')
        self.assertEqual(conn.request.call_count,1)
        self.assertEqual(conn.request.call_args.args,('GET','/api/recovery/state'))
        self.assertEqual(conn.request.call_args.kwargs,{})
    def test_arm_timeout_is_long_and_never_retried(self):
        conn=MagicMock();conn.request.side_effect=OSError('timeout')
        x=n.NativeLaunchSession('/unused','/unused')
        with patch.object(n.http.client,'HTTPConnection',return_value=conn) as ctor:
            with self.assertRaises(n.ConnectionError):x._native_ready(Child(),'secret','nonce','8.8.8.8',{},'arm')
        self.assertEqual(ctor.call_args.kwargs['timeout'],65)
        self.assertEqual(conn.request.call_count,1)
    def test_safe_reason_allowlist_never_echoes_exception(self):
        import contextlib
        cases={'No recent historical SL; evidence cannot be proven without it':'EA_NO_SL',
               'Mailbox and ancestor permissions not verified':'MAILBOX_ACL',
               'Invalid SL evidence':'EA_MISMATCH','EA evidence timeout':'EA_TIMEOUT'}
        for message,code in cases.items():self.assertEqual(n.failure_code(RuntimeError(message)),code)
        output=io.StringIO()
        with contextlib.redirect_stderr(output):n.emit_failure(RuntimeError('TOKEN-secret account=123456'))
        self.assertEqual(output.getvalue(),n.MARKER+'START_FAILED\n')
    def test_https_timeout_has_distinct_safe_code(self):
        x=n.NativeLaunchSession('/unused','/unused')
        with patch.object(n.time,'monotonic',side_effect=[0,100]):
            with self.assertRaises(n.LaunchFailure) as error:x._native_ready(Child(),'secret','nonce','8.8.8.8',{},'https')
        self.assertEqual(error.exception.code,'HTTPS_TIMEOUT')
        self.assertNotIn('secret',str(error.exception))
    def test_failed_start_preserves_only_safe_diagnostics_after_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            scope=self.fixture(d)
            def popen(args,**kwargs):
                kwargs['stdout'].write('TOKEN-secret account=123456\n'+n.MARKER+'EA_NO_SL\n'+n.MARKER+'unknown-secret\n');kwargs['stdout'].flush()
                return Child()
            def fail(*args):raise n.LaunchFailure('CHILD_EXIT')
            x=n.NativeLaunchSession(d,d,popen=popen,run=lambda *a,**k:N(returncode=0,stdout='{"admin":{"disabled":true}}'),readiness=fail,prerequisites=lambda s:None)
            with patch.object(n,'EXE_SHA256',n.hashlib.sha256(b'caddy').hexdigest()),patch.object(n.socket,'socket',MagicMock()):
                with self.assertRaises(n.ConnectionError) as error:
                    x.start('8.8.8.8','TOKEN-secret'*8,d,terminal=scope['terminal'],account=123,broker_server='Bybit',symbol='XAUUSD.s',magic=456,expected_data_path=d,owner_confirmed=True)
            self.assertIsNone(x.temp);self.assertFalse(x.children)
            self.assertIn('[EA_NO_SL]',str(error.exception));self.assertIn('[EA_NO_SL]',x.logs())
            for secret in ('TOKEN-secret','123456','unknown-secret',d):self.assertNotIn(secret,x.logs())
            self.assertLessEqual(len(x.logs()),4096)
    def test_default_prerequisite_fail_closed(self):
        with patch.object(n.Path,"is_file",return_value=False):
            with self.assertRaises(n.ConnectionError):n.required_preconditions({})

if __name__=='__main__':unittest.main()
