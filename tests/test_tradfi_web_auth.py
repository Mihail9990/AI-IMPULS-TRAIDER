import io,json,os,secrets,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tradfi.web_auth import resolve_password,validate_password,redact,WebAuthError
from tradfi.web_control import WebTransport
from tradfi.control import OwnerMailbox

class AuthTest(unittest.TestCase):
    def setUp(self):self.short=secrets.token_hex(3)[:5];self.long=secrets.token_urlsafe(32)
    def test_local_minimum_ipv4_ipv6(self):
        for host,authority in [('127.0.0.1','127.0.0.1:8765'),('::1','[::1]:8765')]:
            self.assertEqual(resolve_password({'host':host,'password':self.short},{}),self.short)
            with self.assertRaises(WebAuthError):resolve_password({'host':host,'password':self.short[:4]}, {})
            web=WebTransport(OwnerMailbox(),authority=authority,password=self.short)
            code,_,_=web.request('POST','/api/login',{'Host':authority,'X-Local-Control':'1','Content-Type':'application/json'},json.dumps({'password':self.short}).encode());self.assertEqual(code,200)
    def test_external_origin_proxy_and_nonloopback(self):
        for options in [{'public_origin':'https://panel.example.org'},{'host':'0.0.0.0'},{'host':'::'},{'host':'192.0.2.1'},{'reverse_proxy':True},{'external_access':True},{'proxy_config':{'enabled':True}}]:
            with self.assertRaises(WebAuthError):resolve_password(dict(options,password=self.short),{})
            self.assertEqual(resolve_password(dict(options,password=self.long),{}),self.long)
            with self.assertRaises(WebAuthError):resolve_password(dict(options,password=self.long[:31]),{})
            self.assertEqual(resolve_password(dict(options,password=self.long[:32]),{}),self.long[:32])
        with self.assertRaises(ValueError):WebTransport(OwnerMailbox(),authority='0.0.0.0:8765',password=self.long)
        with self.assertRaises(WebAuthError):WebTransport(OwnerMailbox(),password=self.short,public_origin='https://panel.example.org')
    def test_env_precedence_and_no_invalid_fallback(self):
        self.assertEqual(resolve_password({'password':self.short},{'TRADFI_WEB_PASSWORD':self.long}),self.long)
        self.assertEqual(resolve_password({'password':''},{'TRADFI_WEB_PASSWORD':self.long}),self.long)
        for env in ['',self.short[:4],'     ']:
            with self.assertRaises(WebAuthError):resolve_password({'password':self.long},{'TRADFI_WEB_PASSWORD':env})
        for value in [None,123,[],{},True]:
            with self.assertRaises(WebAuthError):resolve_password({'password':value},{'TRADFI_WEB_PASSWORD':self.long})
        for value in ['',self.short[:4],'     ','\ud800'*5]:
            with self.assertRaises(WebAuthError):resolve_password({'password':value},{})
        for origin in ['',False,'http://panel.example.org','https://panel.example.org/path']:
            with self.assertRaises(WebAuthError):resolve_password({'password':self.long,'public_origin':origin},{})
    def test_config_auth_not_returned_in_state_or_logs(self):
        from tradfi.context_diagnostics import project
        from tradfi.web_logs import DiagnosticLogs
        box=OwnerMailbox();box._view={'scope':'safe'};web=WebTransport(box,password=resolve_password({'password':self.short},{}))
        h={'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        _,data,extra=web.request('POST','/api/login',h,json.dumps({'password':self.short}).encode());h['Cookie']=extra['Set-Cookie'].split(';')[0]
        self.assertNotIn(self.short,json.dumps(data));self.assertNotIn(self.short,json.dumps(web.request('GET','/api/state',h)))
        payload={'web':{'password':self.short},'nested':[{'TRADFI_WEB_PASSWORD':self.short,'access_token':self.long}]}
        for data in (redact(payload),project(payload)):
            self.assertNotIn(self.short,json.dumps(data));self.assertNotIn(self.long,json.dumps(data))
    def test_legacy_sqlite_export_recursive_redaction(self):
        import sqlite3
        from tradfi.diagnostics import export
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'state.db';dest=Path(tmp)/'export.json'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE snapshot(id INTEGER,payload TEXT)');conn.execute('CREATE TABLE audit(seq INTEGER,event TEXT,payload TEXT)')
                conn.execute('INSERT INTO snapshot VALUES(1,?)',(json.dumps({'web':{'password':self.short}}),))
                conn.execute('INSERT INTO audit VALUES(1,?,?)',('test',json.dumps({'config':{'web':{'password':self.short}},'secret':self.long})))
            export(db,dest);text=dest.read_text();self.assertNotIn(self.short,text);self.assertNotIn(self.long,text)
    def test_external_rejection_before_store_or_mt5(self):
        from tradfi.runtime import _run_mt5
        config=dict(expected_login=123,expected_server='fixture',symbol='XAUUSD.s',web={'password':self.short,'public_origin':'https://panel.example.org'})
        with patch.dict(os.environ,{},clear=True),patch('tradfi.runtime.Store') as store,patch('tradfi.mt5_adapter.MT5Broker.connect') as connect:
            with self.assertRaises(WebAuthError):_run_mt5(config,False,service=True)
            store.assert_not_called();connect.assert_not_called()
    def test_native_readonly_runtime_uses_config_without_environment(self):
        from test_tradfi_native_web import RuntimeAPI
        from time_fixtures import verified_time
        from tradfi.runtime import _run_mt5
        from tradfi.web_control import NativePanel
        api=RuntimeAPI();observed=[]
        class Stop:
            done=False
            def is_set(self):return self.done
            def wait(self,*args):pass
        stop=Stop()
        class Panel(NativePanel):
            def start(self):observed.append(self)
            def step(self):super().step();stop.done=True
        with tempfile.TemporaryDirectory() as tmp:
            config=dict(backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',enable_trading=False,legacy_ea_removed=False,legacy_state_reconciled=False,state_file=str(Path(tmp)/'state.db'),log_file=str(Path(tmp)/'runtime.log'),web={'password':self.short})
            with patch.dict(os.environ,{'LOCALAPPDATA':tmp},clear=True),patch('tradfi.web_control.NativePanel',Panel),patch('tradfi.time_safety.TimeSafety',side_effect=verified_time):
                _run_mt5(config,False,service=True,api=api,stop_event=stop,clock=lambda:api.now,monotonic=lambda:api.now)
            self.assertEqual(observed[0].transport.password,self.short);self.assertTrue(observed[0].controller.broker.read_only);self.assertFalse(api.sends)
            self.assertNotIn(self.short,json.dumps(observed[0].mailbox.view()))
            self.assertNotIn(self.short,observed[0].logs.document('live'))
