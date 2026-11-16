"""Log regression and removed DEMO entrypoints. No native broker calls."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tradfi.control import OwnerMailbox
from tradfi.web_control import WebTransport
from tradfi.web_logs import DiagnosticLogs
from tradfi.context_diagnostics import project,VERSION
import test_tradfi_basket as basket_fixtures

class LogsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.fixture=basket_fixtures.BasketTest();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.logs=DiagnosticLogs(Path(self.tmp.name)/'runtime.log');self.addCleanup(self.logs.close)
        self.box=OwnerMailbox();self.box._view={'scope':'live-scope'}
        self.web=WebTransport(self.box,password='x'*32,logs=self.logs)
        self.headers={'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
    def login(self):
        _,_,cookie=self.web.request('POST','/api/login',self.headers,json.dumps({'password':'x'*32}).encode())
        self.headers['Cookie']=cookie['Set-Cookie'].split(';')[0]
    def capture(self):
        b=self.fixture.bot;b.command('/stop')
        self.logs.capture('live',self.fixture.store,b.state,1800000000,dict(quote=dict(bid=4000,ask=4001)))
    def test_auth_scope_utf8_download_actual_event(self):
        self.capture();self.assertEqual(self.web.request('GET','/api/logs?scope=live',self.headers)[0],401);self.login()
        code,data,_=self.web.request('GET','/api/logs?scope=live',self.headers)
        self.assertEqual(code,200);self.assertIn('basket_stop',data['text'])
        code,body,headers=self.web.request('GET','/api/logs/download?scope=live',self.headers)
        self.assertEqual(code,200);self.assertIn('Логи',body.decode());self.assertIn('attachment;',headers['Content-Disposition']);self.assertLessEqual(len(body),1048576)
        self.assertEqual(self.web.request('GET','/api/logs?scope=paper',self.headers)[0],400)
        self.headers['Origin']='https://evil.invalid';self.assertEqual(self.web.request('GET','/api/logs?scope=live',self.headers)[0],403)
    def test_reject_path_and_removed_demo_scope(self):
        self.login()
        for path in ['/api/logs?scope=../../secret','/api/logs?scope=live&scope=paper','/api/logs?scope=live&page=-1','/api/logs?scope=live&path=secret','/api/logs?scope=demo']:
            self.assertEqual(self.web.request('GET',path,self.headers)[0],400)
    def test_removed_demo_routes_never_enqueue(self):
        self.login();self.headers['X-Controller-Scope']='live-scope'
        for action in ['enable','disable','test','reconcile']:
            body=json.dumps(dict(id='obsolete-request',revision=0,action=dict(type=action,operation='buy'))).encode()
            self.assertEqual(self.web.request('POST','/api/demo/action',self.headers,body)[0],404)
        self.assertEqual(self.web.request('GET','/api/demo/state',self.headers)[0],404);self.assertTrue(self.box.requests.empty())
    def test_removed_launcher_fails_before_configuration_or_broker(self):
        from tradfi.runtime import main
        with patch('tradfi.runtime._run_mt5',side_effect=AssertionError('Must not launch')),patch('pathlib.Path.read_text',side_effect=AssertionError('Must not read config')),contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as e:main(['--mode','mt5','--demo-service','--execute','--config','old-demo.json'])
        self.assertEqual(e.exception.code,2);self.assertIn('unrecognized arguments: --demo-service',err.getvalue())
    def test_projection_redaction_large_and_missing_file(self):
        path=Path(self.logs.runtime_path)
        path.write_text('password=VERY_SECRET\n'+json.dumps(dict(version=1,utc='2026-11-02T00:00:00Z',level='INFO',event='service_started',password='VERY_SECRET',account=123456))+'\n')
        self.assertNotIn('VERY_SECRET',self.logs.document('runtime'));self.assertNotIn('123456',self.logs.document('runtime'))
        self.assertEqual(project(dict(password='VERY_SECRET',account=dict(login=123456,identifier='••56'),request=dict(price=4000,token='VERY_SECRET'))),{'account':{'identifier':'••56'},'request':{'price':'4000'}})
        record=json.dumps(dict(version=VERSION,scope='live',observed_utc='2026-11-02T00:00:00+00:00',context=dict(password='VERY_SECRET',account=dict(login=123456),quote=dict(bid=4000,ask=4001))))+'\n'
        Path(str(path)+'.live.diagnostic.jsonl').write_text(record*10000)
        body=self.logs.response('live',download=True);self.assertLessEqual(len(body),1048576);self.assertNotIn(b'VERY_SECRET',body);self.assertNotIn(b'123456',body)
        self.assertIn('files_read=0',DiagnosticLogs(Path(self.tmp.name)/'missing.log').document('runtime'))
    def test_preserve_previous_log_format(self):
        path=Path(str(self.logs.runtime_path)+'.live.diagnostic.jsonl')
        raw=json.dumps(dict(version='demo-diagnostics-v1',scope='live',context=dict(phase='INITIAL_POSITION')))+'\n';path.write_text(raw)
        self.assertIn('INITIAL_POSITION',self.logs.document('live'));self.assertEqual(path.read_text(),raw)
    def test_no_per_tick_log_and_pagination(self):
        state=self.fixture.bot.state;view=dict(quote=dict(bid=4000,ask=4001))
        self.logs.capture('live',self.fixture.store,state,1800000000,view);view['quote']['bid']=4001
        self.logs.capture('live',self.fixture.store,state,1800000001,view);self.assertEqual(len(self.logs.recent['live']),1)
        self.logs.documents['live']='Я'*40000
        page=self.logs.response('live',1);self.assertEqual(len(page['text']),16000);self.assertTrue(page['has_more'])

class TelegramRouteTest(unittest.TestCase):
    def setUp(self):
        from tradfi.telegram_logs import TelegramLogs
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.sent=[]
        self.outbox=TelegramLogs(Path(self.tmp.name)/'outbox','123456:'+'x'*30,'123',sender=lambda *a:self.sent.append(a) or 1)
        self.addCleanup(self.outbox.close)
        self.logs=DiagnosticLogs();self.logs.telegram=self.outbox
        self.box=OwnerMailbox();self.box._view={'scope':'live','phase':'SAFE_PAUSE','read_only':True}
        self.web=WebTransport(self.box,password='x'*32,logs=self.logs)
        self.headers={'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        self.request_id='11111111-1111-4111-8111-111111111111'
    def login(self):
        _,_,h=self.web.request('POST','/api/login',self.headers,json.dumps({'password':'x'*32}).encode())
        self.headers['Cookie']=h['Set-Cookie'].split(';')[0]
    def send(self,payload=None):
        return self.web.request('POST','/api/logs/telegram/send',self.headers,json.dumps(payload if payload is not None else {'request_id':self.request_id}).encode())
    def test_send_requires_auth_origin_and_exact_body(self):
        self.assertEqual(self.send()[0],401);self.login()
        for payload in ({'request_id':self.request_id,'chat_id':'999'}, {'request_id':12}, [],{}, {'request_id':'x'}):
            self.assertEqual(self.send(payload)[0],400)
        self.headers['Origin']='https://evil.invalid';self.assertEqual(self.send()[0],403)
        self.assertFalse(self.sent)
    def test_safe_pause_read_only_no_trading_and_query_receipt(self):
        self.login();self.outbox.append(json.dumps({'scope':'live','context':{'phase':'SAFE_PAUSE'}}))
        self.assertEqual(self.send()[1]['last_result'],'QUEUED');self.assertFalse(self.sent)
        self.assertTrue(self.box.requests.empty());self.assertEqual(self.box._view['phase'],'SAFE_PAUSE')
        self.outbox.tick()
        code,result,_=self.web.request('GET','/api/logs/telegram?request_id='+self.request_id,self.headers)
        self.assertEqual(code,200);self.assertEqual(result['last_result'],'SENT')
        self.send();self.outbox.tick();self.assertEqual(len(self.sent),1)
        self.assertEqual(self.web.request('GET','/api/logs/telegram?request_id=x',self.headers)[0],400)
    def test_disabled_reports_without_send(self):
        self.login();self.logs.telegram=None
        self.assertEqual(self.send()[1]['status'],'DISABLED');self.assertFalse(self.sent)
