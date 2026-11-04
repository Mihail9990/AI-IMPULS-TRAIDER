import hashlib
import io
import json
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch
import unittest
import https_setup as h
from test_https_setup import Fake

class Early(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'data').mkdir();self.config=self.root/'data'/'service.json'
        self.config.write_text(json.dumps({'enable_trading':False,'web':{'password':'SECRET_FIXTURE_ONLY_'+('x'*32),'host':'127.0.0.1','port':8765}}))
        self.a=Fake();self.s=h.Setup(self.root,self.a);self.p=self.s.prepare('38.60.203.9')
        self.sha=patch.object(h,'ARCHIVE_SHA256',h.digest(self.a.fetch()));self.sha.start();self.addCleanup(self.sha.stop)
    def apply(self):return self.s.apply(self.p,True)
    def failure(self,stage,code=None):
        a=h.read(self.s.attempt_path);self.assertEqual(a['stage'],stage);self.assertEqual(a['status'],'FAILED')
        if code:self.assertEqual(a['failure']['code'],code)
        self.assertNotIn('SECRET_FIXTURE_ONLY',json.dumps(a));return a
    def test_begin_before_download(self):
        def fetch():
            self.assertEqual(h.read(self.s.attempt_path)['stage'],'download');self.assertFalse(self.s.home.exists())
            raise urllib.error.URLError('secret password token')
        self.a.fetch=fetch
        with self.assertRaises(h.AttemptError) as e:self.apply()
        self.failure('download','NETWORK');self.assertNotIn('secret password',str(e.exception));self.assertFalse(self.s.journal.exists())
    def test_begin_write_failure_truthful(self):
        with patch.object(h,'save',side_effect=PermissionError('secret')),self.assertRaisesRegex(h.AttemptError,'НЕ удалось сохранить'):self.apply()
        self.assertFalse(self.s.attempt_path.exists());self.assertFalse(self.a.calls)
    def test_admin_failure_is_recorded(self):
        self.a.admin=lambda:(_ for _ in ()).throw(h.HttpsError('need elevation'))
        with self.assertRaises(h.AttemptError):self.apply()
        self.failure('admin','ADMIN_REQUIRED');self.assertFalse(self.s.home.exists())
    def test_http_tls_timeout_classification(self):
        for exc,code in [(urllib.error.HTTPError('https://secret',403,'secret',None,None),'HTTP_ERROR'),
                         (urllib.error.URLError(ssl.SSLCertVerificationError('secret')),'TLS_VERIFY'),
                         (TimeoutError('secret'),'TIMEOUT')]:
            f=h.safe_failure(exc,'download');self.assertEqual(f['code'],code);self.assertNotIn('secret',json.dumps(f))
    def test_checksum_stage(self):
        with patch.object(h,'ARCHIVE_SHA256','bad'),self.assertRaises(h.AttemptError):self.apply()
        self.failure('checksum','CHECKSUM');self.assertFalse(self.s.home.exists())
    def test_zip_stage(self):
        data=b'not a zip';self.a.fetch=lambda:data
        with patch.object(h,'ARCHIVE_SHA256',h.digest(data)),self.assertRaises(h.AttemptError):self.apply()
        self.failure('unpack','GUARD_REJECTED')
    def test_zip_missing_exe(self):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:z.writestr('other',b'other')
        self.a.fetch=lambda:data.getvalue()
        with patch.object(h,'ARCHIVE_SHA256',h.digest(data.getvalue())),self.assertRaises(h.AttemptError):self.apply()
        self.failure('unpack','GUARD_REJECTED')
    def test_foreign_empty_directory_not_adopted(self):
        self.s.home.mkdir()
        with self.assertRaises(h.AttemptError):self.apply()
        self.assertFalse(self.failure('preflight')['home_owned'])
        with self.assertRaises(h.HttpsError):self.s.recover_preparation(True)
        self.assertTrue(self.s.home.exists())
    def test_directory_permission_failure(self):
        real=Path.mkdir
        def fail(path,*a,**kw):
            if path==self.s.home:raise PermissionError('private')
            return real(path,*a,**kw)
        with patch.object(Path,'mkdir',fail),self.assertRaises(h.AttemptError):self.apply()
        self.failure('directory','FILE_PERMISSION')
    def test_acl_failure_empty_owned_folder_recoverable(self):
        self.a.protect=lambda p:(_ for _ in ()).throw(h.CommandError('icacls.exe',5))
        with self.assertRaises(h.AttemptError):self.apply()
        self.failure('acl','COMMAND_FAILED');self.s.recover_preparation(True)
        self.assertFalse(self.s.home.exists());self.assertEqual(h.read(self.s.attempt_path)['status'],'RECOVERED')
    def write_failure(self,name,partial=False):
        real=Path.write_bytes
        def fail(path,data):
            if path.name==name:
                if partial:real(path,b'partial')
                raise PermissionError('never print this secret')
            return real(path,data)
        with patch.object(Path,'write_bytes',fail),self.assertRaises(h.AttemptError):self.apply()
    def test_backup_write_failure_safe_retry(self):
        self.write_failure('service.original.json');self.failure('backup','FILE_PERMISSION')
        self.s.recover_preparation(True);self.assertFalse(self.s.home.exists())
    def test_binary_write_failure_preserves_backup(self):
        self.write_failure('caddy.exe');a=self.failure('binary','FILE_PERMISSION')
        before=self.config.read_bytes();self.s.recover_preparation(True)
        self.assertEqual((self.root/'https-recovery'/a['id']/'backup'/'service.original.json').read_bytes(),before)
        self.assertEqual(self.config.read_bytes(),before)
    def test_partial_file_refuses_recovery(self):
        self.write_failure('caddy.exe',True)
        with self.assertRaisesRegex(h.HttpsError,'частично'):self.s.recover_preparation(True)
        self.assertTrue((self.s.home/'caddy.exe').exists())
    def test_caddyfile_write_failure(self):
        self.write_failure('Caddyfile');self.failure('caddyfile','FILE_PERMISSION');self.s.recover_preparation(True)
    def test_adapt_error_records_exit_code_and_recovery(self):
        self.a.run=lambda args:(_ for _ in ()).throw(h.CommandError('caddy.exe',1))
        with self.assertRaises(h.AttemptError):self.apply()
        self.assertEqual(self.failure('adapt','COMMAND_FAILED')['failure']['exit_code'],1)
        self.s.recover_preparation(True);self.assertFalse(self.s.home.exists())
    def test_repeat_requires_explicit_recovery_and_new_plan(self):
        data=self.a.fetch();self.a.fetch=lambda:(_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(h.AttemptError):self.apply()
        with self.assertRaises(h.HttpsError):self.apply()
        with self.assertRaises(h.HttpsError):self.s.prepare('38.60.203.9')
        with self.assertRaises(h.HttpsError):self.s.recover_preparation(False)
        self.s.recover_preparation(True);self.a.fetch=lambda:data
        self.p=self.s.prepare('38.60.203.9');self.apply();self.assertEqual(h.read(self.s.attempt_path)['status'],'SUCCEEDED')
    def test_create_unknown_never_retried(self):
        self.a.fail='create'
        with self.assertRaises(h.AttemptError):self.apply()
        self.assertTrue(self.failure('service_create')['mutation_started'])
        with self.assertRaises(h.HttpsError):self.s.recover_preparation(True)
        with self.assertRaises(h.HttpsError):self.apply()
        self.assertEqual(sum(x[0]=='create' for x in self.a.calls),1)
    def test_start_unknown_never_retried(self):
        self.a.fail='start'
        with self.assertRaises(h.AttemptError):self.apply()
        self.failure('service_start')
        with self.assertRaises(h.HttpsError):self.s.recover_preparation(True)
        self.assertEqual(len(self.a.services),1)
    def test_config_write_error_is_not_early_retry(self):
        self.write_failure('service.https.tmp');self.failure('config','FILE_PERMISSION')
        with self.assertRaises(h.HttpsError):self.s.recover_preparation(True)
    def test_no_report_no_directory_diagnostic_honest(self):
        result=self.s.local_report()
        self.assertFalse(result['transaction_exists']);self.assertFalse(result['attempt_report_exists'])
    def test_diagnostic_projection_rejects_secrets(self):
        h.save(self.s.attempt_path,{'stage':'secret','status':'secret','password':'secret','failure':{'code':'HTTP_ERROR','reason':'secret','command':'secret','http_status':403}})
        text=json.dumps(self.s.local_report());self.assertNotIn('secret',text);self.assertIn('403',text)
    def test_diagnostic_returns_early_stage_without_transaction(self):
        self.a.fetch=lambda:(_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(h.AttemptError):self.apply()
        with patch('urllib.request.build_opener',side_effect=OSError()):result=self.s.diagnose('38.60.203.9')
        self.assertEqual(result['stage'],'download');self.assertFalse(result['transaction_exists'])
    def test_recovery_rejects_foreign_file(self):
        self.write_failure('caddy.exe');(self.s.home/'foreign.txt').write_text('keep')
        with self.assertRaises(h.HttpsError):self.s.recover_preparation(True)
        self.assertEqual((self.s.home/'foreign.txt').read_text(),'keep')
    def test_recovery_rejects_new_service_or_config(self):
        self.a.fetch=lambda:(_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(h.AttemptError):self.apply()
        self.config.write_text('{}')
        with self.assertRaises(h.HttpsError):self.s.recover_preparation(True)
    def test_worker_nonblocking_doubleclick_close_state(self):
        started=threading.Event();release=threading.Event();job=h.BackgroundJob();calls=[]
        def work():calls.append(1);started.set();release.wait(2);return 'done'
        self.assertTrue(job.start(work));self.assertTrue(started.wait(1));self.assertTrue(job.busy)
        self.assertFalse(job.start(work));self.assertIsNone(job.poll());release.set()
        deadline=time.monotonic()+2;result=None
        while result is None and time.monotonic()<deadline:result=job.poll();time.sleep(.001)
        self.assertEqual(result,(True,'done'));self.assertFalse(job.busy);self.assertEqual(calls,[1])
    def test_not_confirmed_no_attempt(self):
        with self.assertRaises(h.HttpsError):self.s.apply(self.p,False)
        self.assertFalse(self.s.attempt_path.exists());self.assertFalse(self.a.calls)

if __name__=='__main__':unittest.main()

class FakeTkWindow:
    def __init__(self,*a,**kw):self.callbacks=[];self.protocols={};self.alive=True;self.value='';self.options=kw
    def title(self,*a):pass
    def geometry(self,*a):pass
    def pack(self,*a,**kw):pass
    def configure(self,**kw):self.options.update(kw)
    def delete(self,*a):self.value=''
    def insert(self,*args):self.value+=args[-1]
    def after(self,ms,fn):self.callbacks.append(fn)
    def protocol(self,name,fn):self.protocols[name]=fn
    def grab_set(self):pass
    def destroy(self):self.alive=False
    def set(self,value):self.value=value
    def get(self):return self.value

class GuiTests(unittest.TestCase):
    def test_cancel_repeat_and_close_while_running(self):
        import types
        from unittest.mock import Mock
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);buttons={};window=FakeTkWindow()
            def button(*args,**kw):
                obj=FakeTkWindow(**kw);buttons[kw['text']]=obj;return obj
            mb=SimpleNamespace(askyesno=Mock(return_value=False),showinfo=Mock(),showerror=Mock())
            ttk=SimpleNamespace(Label=FakeTkWindow,Entry=FakeTkWindow,Frame=FakeTkWindow,Button=button)
            tk=types.ModuleType('tkinter');tk.Toplevel=lambda:window;tk.StringVar=FakeTkWindow;tk.Text=FakeTkWindow;tk.messagebox=mb;tk.ttk=ttk;tk.filedialog=SimpleNamespace(askopenfilename=Mock(return_value=''))
            prepared={'old':None,'origin':'https://38.60.203.9','caddyfile':'safe'}
            setup=Mock();setup.home=root/'https';setup.attempt_path=root/'https-attempt.json';setup.prepare.return_value=prepared
            setup.local_report.return_value={};manager=SimpleNamespace(root=root,config=lambda:{})
            def pump_until(test):
                deadline=time.monotonic()+2
                while not test() and time.monotonic()<deadline:
                    if window.callbacks:window.callbacks.pop(0)()
                    time.sleep(.002)
                self.assertTrue(test())
            with patch.dict('sys.modules',{'tkinter':tk}),patch.object(h,'Setup',return_value=setup):
                h.wizard(manager)
                buttons['Подготовить план'].options['command']()
                pump_until(lambda:buttons['Применить'].options.get('state')=='normal')
                buttons['Выбрать официальный ZIP…'].options['command']();setup.select_local_zip.assert_not_called()
                buttons['Применить'].options['command']();setup.apply.assert_not_called()
                tk.filedialog.askopenfilename.return_value='/local/test.zip'
                buttons['Выбрать официальный ZIP…'].options['command']()
                pump_until(lambda:buttons['Применить'].options.get('state')=='normal')
                setup.select_local_zip.assert_called_once_with('/local/test.zip')
                buttons['Применить'].options['command']();setup.apply.assert_not_called()
                buttons['Подготовить план'].options['command']()
                pump_until(lambda:buttons['Применить'].options.get('state')=='normal')
                mb.askyesno.return_value=True;entered=threading.Event();release=threading.Event()
                def apply(*a):entered.set();release.wait(2);return 'completed'
                setup.apply.side_effect=apply
                buttons['Применить'].options['command']();self.assertTrue(entered.wait(1))
                buttons['Применить'].options['command']();self.assertEqual(setup.apply.call_count,1)
                window.protocols['WM_DELETE_WINDOW']();self.assertTrue(window.alive);self.assertTrue(mb.showinfo.called)
                release.set();pump_until(lambda:buttons['Применить'].options.get('state')=='normal')
                window.protocols['WM_DELETE_WINDOW']();self.assertFalse(window.alive)
