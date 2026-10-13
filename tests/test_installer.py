"""Offline failure-atomic installer checks. No pip, network, terminal or GUI starts."""
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
import install_tradfi as tool
from tradfi import managed

A='a'*40; B='b'*40
SOURCE=Path(__file__).resolve().parents[1]

def archive(sha, extra=None):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        for name in ['managed_release.json','requirements-tradfi-mt5.txt','tradfi_main.py','tradfi/runtime.py','tradfi/managed.py','tradfi_config.example.json']:
            z.writestr('AI-IMPULS-TRAIDER-'+sha+'/'+name,(SOURCE/name).read_bytes())
        for name,data in (extra or []): z.writestr(name,data)
    return out.getvalue()

def crash_lock_child(path,conn):
    with tool.lock(path):
        conn.send('locked');conn.close();os._exit(0)

def lock_child(path,conn):
    try:
        with tool.lock(path): conn.send('acquired')
    except tool.SetupError: conn.send('blocked')
    finally: conn.close()

class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'install'; self.sha=A; self.urls=[]; self.messages=[]
        def fetch(url,limit=tool.MAX_DOWNLOAD):
            self.urls.append(url)
            return json.dumps({'ref':'refs/heads/work','object':{'type':'commit','sha':self.sha}}).encode() if '/git/ref/' in url else archive(self.sha)
        self.manager=tool.Manager(self.root,fetch=fetch,report=self.messages.append)
        self.prepare=patch.object(self.manager,'prepare_python'); self.prepare.start();self.addCleanup(self.prepare.stop)
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':self.tmp.name});self.env.start();self.addCleanup(self.env.stop)
    def install(self): self.manager.install()
    def state(self,**overrides):
        value=dict(schema=1,backend='mt5_direct_v1',ledger_contract='deal_ledger_v1',scope_contract='cycle_scope_v1',phase='PAUSED',cycle=None,commands={})
        value.update(overrides); p=tool.state_path(self.root,self.manager.config())
        with sqlite3.connect(p) as db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshot(id INTEGER PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS mt5_commands(id TEXT PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS consumed_deals(ticket TEXT PRIMARY KEY,payload TEXT)')
            db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?)',(json.dumps(value),))
        return p
    def test_first_install_pinned_download_and_safe_defaults(self):
        self.install(); self.assertEqual(self.manager.pointer(),{'current':A,'previous':None})
        self.assertIn('/git/ref/heads/work',self.urls[0]);self.assertIn('/'+A+'.zip',self.urls[1])
        self.assertFalse(self.manager.config()['enable_trading'])
        self.assertFalse(self.manager.config()['legacy_state_reconciled'])
        self.assertEqual(tool.read_json(self.root/'data'/'telegram.json')['TRADFI_TELEGRAM_TOKEN'],'')
    def test_update_preserves_data_and_rollback_only_code(self):
        self.install(); db=self.state(); creds=self.root/'data'/'telegram.json'
        creds.write_text('local-secret'); log=self.root/'data'/'tradfi.log';log.write_text('log')
        before={p:p.read_bytes() for p in [db,creds,log,self.manager.config_path]}
        self.sha=B;self.install(); self.manager.rollback()
        self.assertEqual(self.manager.pointer()['current'],A)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.install(); self.assertEqual(self.manager.pointer()['current'],B)
    def test_failure_before_switch_keeps_old_version(self):
        self.install();self.sha=B
        with patch.object(self.manager,'prepare_python',side_effect=tool.SetupError('failure')):
            with self.assertRaises(tool.SetupError):self.install()
        self.assertEqual(self.manager.pointer()['current'],A)
        self.assertTrue(self.manager.version(A).exists());self.assertFalse(self.manager.version(B).exists())
    def test_pointer_write_failure_keeps_old_version(self):
        self.install();self.sha=B;real=tool.atomic_json
        def fail(path,value):
            if Path(path)==self.root/'current.json': raise OSError()
            return real(path,value)
        with patch.object(tool,'atomic_json',side_effect=fail),self.assertRaises(OSError):self.install()
        self.assertEqual(self.manager.pointer()['current'],A)
    def test_active_unknown_and_foreign_state_block_update(self):
        self.install();self.sha=B
        for data in [dict(cycle={'id':'x'}),dict(phase='WAIT_CANDLE'),dict(commands={'x':{'status':'UNKNOWN'}}),dict(backend='file_gateway_v1'),dict(schema=2)]:
            with self.subTest(data=data):
                self.state(**data)
                with self.assertRaises(tool.SetupError):self.install()
        self.assertEqual(self.manager.pointer()['current'],A)
    def test_orphan_journal_and_ledger_mismatch_block_update(self):
        self.install();p=self.state();self.sha=B
        with sqlite3.connect(p) as db:
            db.execute("INSERT INTO mt5_commands VALUES('orphan','{}')")
        with self.assertRaises(tool.SetupError):self.install()
        with sqlite3.connect(p) as db:
            db.execute('DELETE FROM mt5_commands')
            db.execute("INSERT INTO consumed_deals VALUES('orphan','{}')")
        with self.assertRaises(tool.SetupError):self.install()
    def test_missing_bound_state_never_resets(self):
        self.install();p=self.state()
        tool.atomic_json(self.root/'data'/'state-binding.json',{'path':str(p)})
        p.unlink();self.sha=B
        with self.assertRaises(tool.SetupError):self.install()
        self.assertFalse(p.exists())
    def test_missing_existing_config_never_recreated(self):
        self.install();self.manager.config_path.unlink();self.sha=B
        with self.assertRaises(tool.SetupError):self.install()
        self.assertFalse(self.manager.config_path.exists())
    def test_unknown_data_not_adopted(self):
        (self.root/'data').mkdir(parents=True);(self.root/'data'/'state.sqlite3').write_bytes(b'unknown')
        with self.assertRaises(Exception):self.install()
        self.assertFalse((self.root/'current.json').exists())
        self.assertEqual((self.root/'data'/'state.sqlite3').read_bytes(),b'unknown')
    def test_install_lock_blocks_and_recovers_after_release(self):
        with tool.lock(self.root/'operation.lock'):
            with self.assertRaises(tool.SetupError):self.install()
        self.install()
    def test_cross_process_lock(self):
        ctx=multiprocessing.get_context('spawn');a,b=ctx.Pipe()
        with tool.lock(self.root/'operation.lock'):
            p=ctx.Process(target=lock_child,args=(str(self.root/'operation.lock'),b));p.start()
            self.assertTrue(a.poll(10));self.assertEqual(a.recv(),'blocked');p.join(10);self.assertEqual(p.exitcode,0)
        a.close();b.close()
    def test_lock_released_after_process_crash(self):
        ctx=multiprocessing.get_context('spawn');a,b=ctx.Pipe()
        p=ctx.Process(target=crash_lock_child,args=(str(self.root/'operation.lock'),b));p.start()
        self.assertTrue(a.poll(10));self.assertEqual(a.recv(),'locked');p.join(10);self.assertEqual(p.exitcode,0)
        a.close();b.close();self.install()
    def test_old_direct_service_account_lock_blocks_update(self):
        self.install();c=self.manager.config();c['expected_login']=123;tool.atomic_json(self.manager.config_path,c)
        with tool.account_lock(self.root,c):
            with self.assertRaises(tool.SetupError):self.install()
    def test_archive_rejects_traversal_duplicates_reserved_names_and_symlink(self):
        for path in ['../escape','AI-IMPULS-TRAIDER-'+A+'/../escape','AI-IMPULS-TRAIDER-'+A+'/NUL.txt','AI-IMPULS-TRAIDER-'+A+'/C:/x','AI-IMPULS-TRAIDER-'+A+'/tradfi_main.py','AI-IMPULS-TRAIDER-'+A+'/x\\y']:
            with self.subTest(path=path),self.assertRaises(tool.SetupError):
                tool.extract_archive(archive(A,[(path,b'x')]),self.root/'out',A)
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            entry=zipfile.ZipInfo('AI-IMPULS-TRAIDER-'+A+'/link');entry.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(entry,'../outside')
        with self.assertRaises(tool.SetupError):tool.extract_archive(out.getvalue(),self.root/'out',A)
        self.assertFalse((self.root/'out').exists())
    def test_archive_bomb_and_corruption(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('AI-IMPULS-TRAIDER-'+A+'/bomb',b'0'*100000)
        with self.assertRaises(tool.SetupError):tool.extract_archive(out.getvalue(),self.root/'out',A)
        with self.assertRaises(zipfile.BadZipFile):tool.extract_archive(b'bad',self.root/'out',A)
    def test_release_contract_and_requirements_checked(self):
        self.install();code=self.manager.version(A)/'code'
        (code/'managed_release.json').write_text('{}')
        with self.assertRaises(tool.SetupError):tool.validate_release(code)
        tool.atomic_json(code/'managed_release.json',tool.CONTRACT)
        (code/'requirements-tradfi-mt5.txt').write_text('--extra-index-url https://evil.invalid')
        with self.assertRaises(tool.SetupError):tool.validate_release(code)
    def test_rollback_blocks_running_and_active_state(self):
        self.install();self.sha=B;self.install()
        with tool.lock(self.root/'operation.lock'),self.assertRaises(tool.SetupError):self.manager.rollback()
        self.state(cycle={'id':'active'})
        with self.assertRaises(tool.SetupError):self.manager.rollback()
        self.assertEqual(self.manager.pointer()['current'],B)
    def test_network_failure_does_not_touch_pointer(self):
        self.install();self.manager.fetch=lambda *args: (_ for _ in ()).throw(OSError('offline'))
        with self.assertRaises(OSError):self.install()
        self.assertEqual(self.manager.pointer()['current'],A)
    def test_unmanaged_runtime_guard_is_noop(self):
        with managed.runtime_guard(self.root/'missing.sqlite3'): pass
        self.assertIsNone(managed.stop_request())
        self.assertFalse(self.root.exists())
    def test_invalid_sha(self):
        for value in ['../x','z'*40,None,1]:
            with self.assertRaises(tool.SetupError):tool.latest_sha(lambda *args:json.dumps({'ref':'refs/heads/work','object':{'type':'commit','sha':value}}).encode())
    def test_download_limit_and_redirect(self):
        class Response(io.BytesIO):
            headers={};url='https://codeload.github.com/archive'
        with patch('urllib.request.urlopen',return_value=Response(b'012345')):
            with self.assertRaises(tool.SetupError):tool.download('https://github.com/x',5)
        r=Response(b'x');r.url='http://evil.invalid'
        with patch('urllib.request.urlopen',return_value=r),self.assertRaises(tool.SetupError):tool.download('https://github.com/x')
    def test_dependency_commands_use_pypi_binary_wheels_and_no_secret_output(self):
        self.prepare.stop();calls=[]
        def run(args,**kw):calls.append((args,kw));return SimpleNamespace(returncode=0)
        self.manager.runner=run
        with patch.dict(os.environ,{'PIP_EXTRA_INDEX_URL':'https://bad','TRADFI_TELEGRAM_TOKEN':'secret'}):
            self.manager.prepare_python(self.root/'release')
        self.assertEqual(len(calls),3)
        self.assertIn('https://pypi.org/simple',calls[1][0]);self.assertIn('--only-binary=:all:',calls[1][0])
        self.assertNotIn('PIP_EXTRA_INDEX_URL',calls[1][1]['env'])
        self.assertNotIn('TRADFI_TELEGRAM_TOKEN',calls[1][1]['env'])
    def test_launch_readonly_default_and_live_only_explicit(self):
        self.install()
        with patch('subprocess.Popen') as popen:
            self.manager.launch();args=popen.call_args.args[0];self.assertNotIn('--execute',args)
            self.manager.launch(True);self.assertIn('--execute',popen.call_args.args[0])
        self.assertFalse(self.manager.config()['enable_trading'])
    def test_managed_direct_runtime_blocks_update(self):
        self.install();code=self.manager.version(A)/'code';state=tool.state_path(self.root,self.manager.config())
        with patch.object(managed,'__file__',str(code/'tradfi'/'managed.py')):
            with managed.runtime_guard(state):
                with self.assertRaises(tool.SetupError):self.install()
            self.assertFalse((self.root/'data'/'state-binding.json').exists())
            state.touch();managed.bind_state(state)
            state.unlink()
            with self.assertRaises(tool.SetupError):
                with managed.runtime_guard(state):pass
    def test_session_stop_request(self):
        self.install();code=self.manager.version(A)/'code';session='1'*32
        with patch.object(managed,'__file__',str(code/'tradfi'/'managed.py')),patch.dict(os.environ,{'TRADFI_LAUNCH_SESSION':session}):
            event=managed.stop_request();self.assertFalse(event.is_set())
            event.path.touch();self.assertTrue(event.is_set())

if __name__=='__main__':unittest.main()
