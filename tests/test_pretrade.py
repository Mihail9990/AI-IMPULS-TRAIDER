"""Pretrade installer and capability tests; no real pip/MT5/network/process launch."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import install_tradfi as install
import pretrade_launcher as launcher
import pretrade_checks as checks
import test_installer as fixtures

class PretradeInstallerTest(unittest.TestCase):
    setUp=fixtures.InstallerTest.setUp
    install=fixtures.InstallerTest.install
    state=fixtures.InstallerTest.state
    def test_backup_consistent_db_preserves_secret_files_in_place(self):
        self.install();p=self.state();before=p.read_bytes()
        c=self.root/'data'/'telegram.json';c.write_text('{"private":"do-not-copy"}')
        cfg=self.manager.config_path.read_bytes();self.sha=fixtures.B;self.install()
        backups=list((self.root/'backups').glob('*/state.sqlite3'));self.assertEqual(len(backups),1)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertIn('PAUSED',db.execute('SELECT payload FROM snapshot').fetchone()[0])
        self.assertEqual(c.read_text(),'{"private":"do-not-copy"}');self.assertEqual(self.manager.config_path.read_bytes(),cfg)
        self.assertFalse(list((self.root/'backups').rglob('telegram.json')))
        self.assertTrue(self.manager.version(fixtures.A).exists())
    def test_failed_selftests_preserve_pointer_state_and_quarantine_new_code(self):
        self.install();self.state();self.sha=fixtures.B
        with patch.object(self.manager,'verify_prepared',side_effect=install.SetupError('tests failed')):
            with self.assertRaises(install.SetupError):self.install()
        self.assertEqual(self.manager.pointer()['current'],fixtures.A)
        self.assertTrue(list((self.root/'failed').glob(fixtures.B+'-*')))
        self.assertTrue(list((self.root/'backups').glob('*/state.sqlite3')))
    def test_after_pointer_replace_exception_preserves_referenced_release(self):
        self.install();self.state();self.sha=fixtures.B
        original=install.atomic_json
        def after(path,value):
            original(path,value)
            if Path(path)==self.root/'current.json':raise OSError('uncertain completion')
        with patch.object(install,'atomic_json',side_effect=after):
            with self.assertRaises(OSError):self.install()
        self.assertEqual(self.manager.pointer()['current'],fixtures.B)
        self.assertTrue(self.manager.version(fixtures.B).exists());self.manager.current_release()

    def test_bound_state_lock_blocks_update(self):
        self.install();p=self.state();self.sha=fixtures.B
        with install.lock(str(p)+'.lock'):
            with self.assertRaises(install.SetupError):self.install()
        self.assertEqual(self.manager.pointer()['current'],fixtures.A)

class PretradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.blob=fixtures.archive(fixtures.A);(self.root/'release.zip').write_bytes(self.blob)
        install.atomic_json(self.root/'release.json',dict(commit=fixtures.A,sha256=hashlib.sha256(self.blob).hexdigest()))
        self.messages=[];self.manager=launcher.PretradeManager(self.root/'install',self.root,report=self.messages.append)
    def test_local_payload_hash_verified_no_remote_fetch(self):
        self.assertEqual(self.manager.local_release('https://github.com/x/'+fixtures.A+'.zip'),self.blob)
        (self.root/'release.zip').write_bytes(b'bad')
        with self.assertRaisesRegex(install.SetupError,'SHA256'):self.manager.local_release('/git/ref/heads/work')
    def test_no_live_launch_even_explicit_argument(self):
        with patch('subprocess.Popen') as popen:
            for mode in (True,False):
                with self.assertRaises(install.SetupError):self.manager.launch(mode)
            popen.assert_not_called()
    def test_checks_only_allowlist_and_sanitized_failure(self):
        calls=[]
        def run(args,**kwargs):
            calls.append((args,kwargs));return NS(returncode=1,stdout='SECRET',stderr='TOKEN account password')
        self.manager.runner=run
        self.assertEqual(self.manager.run_check('mt5',self.root)['status'],'FAIL')
        self.assertNotIn('SECRET',''.join(self.messages));self.assertNotIn('TOKEN',''.join(self.messages))
        self.assertNotIn('--execute',calls[0][0]);self.assertNotIn('--service',calls[0][0]);self.assertEqual(calls[0][1]['timeout'],45)
        with self.assertRaises(install.SetupError):self.manager.run_check('live',self.root)
    def test_config_report_omits_credentials_and_account(self):
        terminal=self.root/'terminal.exe';terminal.touch()
        c=dict(backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',expected_server='Bybit-Live-6',
            expected_login=987654321,symbol='XAUUSD.s',terminal_path=str(terminal),enable_trading=True,password='secret-value')
        p=self.root/'config.json';install.atomic_json(p,c)
        loaded,report=checks.config_check(p);self.assertEqual(report['status'],'PASS')
        self.assertFalse(report['diagnostic_execution_enabled']);self.assertNotIn('secret-value',json.dumps(report))
        self.assertNotIn('987654321',json.dumps(report));self.assertEqual(loaded,c)
    def test_readonly_capability_blocks_all_trading_and_login(self):
        class API:
            def __getattr__(self,name):raise AssertionError('Underlying capability was accessed: '+name)
        api=checks.ReadOnlyMT5(API())
        for method in ('order_send','order_check','login','symbol_select','market_book_add'):
            with self.assertRaises(PermissionError):getattr(api,method)
    def test_mt5_readonly_reads_no_mutations_and_no_utc_claim(self):
        calls=[]
        class API:
            def initialize(self,*a,**k):calls.append('initialize');return True
            def shutdown(self):calls.append('shutdown')
            def account_info(self):return NS(login=123,server='Bybit-Live-6',currency='UST',margin_mode=2)
            def terminal_info(self):return NS(connected=True)
            def symbol_info(self,s):return NS(visible=True)
            def symbol_info_tick(self,s):return NS(bid=4000.,ask=4000.1,time_msc=1791800000000)
            def positions_get(self,**k):return []
            def orders_get(self,**k):return []
            def copy_rates_from_pos(self,*a):return []
            def order_send(self,*a):raise AssertionError('trade')
            def login(self,*a):raise AssertionError('login')
        def offline(*a,**k):raise OSError('private-secret')
        report=checks.inspect_mt5(dict(terminal_path='fixture',expected_login=123,expected_server='Bybit-Live-6',symbol='XAUUSD.s',enable_trading=True),api=API(),get=offline)
        self.assertEqual(report['mode'],'READ_ONLY');self.assertFalse(report['execution_ready'])
        self.assertEqual(report['quote_freshness'],'UNVERIFIED');self.assertIsNone(report['quote_age_seconds'])
        self.assertEqual(calls,['initialize','shutdown']);self.assertNotIn('private-secret',json.dumps(report))
    def test_offline_selftests_must_pass_before_promotion(self):
        (self.root/'code'/'tools').mkdir(parents=True);(self.root/'code'/'tests').mkdir()
        (self.root/'code'/'tools'/'run_offline_tests.py').touch()
        self.manager.runner=lambda *a,**k:NS(returncode=1,stdout='',stderr='Ran 5 tests\nFAILED')
        with self.assertRaises(install.SetupError):self.manager.verify_prepared(self.root)
        self.manager.runner=lambda *a,**k:NS(returncode=0,stdout='Blocked real network attempts: 0',stderr='Ran 495 tests\nOK')
        self.manager.verify_prepared(self.root)

    def test_zero_tests_and_missing_tests_never_promote(self):
        self.manager.runner=lambda *a,**k:NS(returncode=0,stdout='Blocked real network attempts: 0',stderr='Ran 0 tests\nOK')
        self.assertEqual(self.manager.run_check('offline',self.root)['status'],'FAIL')
        with self.assertRaises(install.SetupError):self.manager.verify_prepared(self.root)
    def test_mock_preview_is_file_only_no_server(self):
        release=self.root/'release';page=release/'code'/'web'/'local-panel'/'index.html';page.parent.mkdir(parents=True);page.touch()
        with patch.object(self.manager,'current_release',return_value=release),patch('webbrowser.open') as browser:
            self.manager.preview();self.assertEqual(browser.call_args.args[0],page.as_uri())
    def test_offline_runner_uses_isolated_localappdata(self):
        calls=[]
        def run(args,**kwargs):
            calls.append(kwargs);self.assertNotEqual(kwargs['env']['LOCALAPPDATA'],str(self.root))
            self.assertTrue(Path(kwargs['env']['LOCALAPPDATA']).is_dir())
            return NS(returncode=0,stdout='Blocked real network attempts: 0',stderr='Ran 495 tests\nOK')
        self.manager.runner=run;self.manager.run_check('offline',self.root)
        self.assertFalse(Path(calls[0]['env']['LOCALAPPDATA']).exists())

    def test_binding_import_and_shutdown_failures_are_redacted(self):
        with patch('importlib.import_module',side_effect=RuntimeError('secret')):
            report=checks.inspect_mt5({})
        self.assertEqual(report['status'],'FAIL');self.assertNotIn('secret',json.dumps(report))
        class API:
            def initialize(self,*a,**k):return False
            def shutdown(self):raise RuntimeError('secret')
        report=checks.inspect_mt5({'terminal_path':'fixture'},api=API())
        self.assertEqual(report['status'],'FAIL');self.assertNotIn('secret',json.dumps(report))
