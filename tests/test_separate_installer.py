"""Offline role separation checks: no MT5, pip, network, GUI or child process."""
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch, Mock
import install_tradfi as tool
try:
    from tests import test_installer as fixtures
except ImportError:
    import test_installer as fixtures

A=fixtures.A;B=fixtures.B

class SeparateInstallerTest(unittest.TestCase):
    setUp=fixtures.InstallerTest.setUp
    install=fixtures.InstallerTest.install
    state=fixtures.InstallerTest.state

    def paper_state(self):
        from tradfi.paper import PaperSession
        path=tool.state_path(self.root,self.manager.config('paper'))
        session=PaperSession(path,Mock(_expected_account='123@Bybit-Live-6', market_time_offset_seconds=0),clock=lambda:1,monotonic=lambda:1)
        session.exchange.p['paused_gap']=True
        session.exchange.p['order_history']=[{'test':'preserved'}]
        session.exchange.persist();identity=session.exchange.account
        session.close()
        return path,identity

    def passwords(self):
        for role in ('live','paper'):
            c=self.manager.config(role);c['web']['password']='separate-'+role
            tool.atomic_json(self.manager.path_for(role),c)

    def test_paper_config_allowlist_no_secrets_and_preserves_live(self):
        self.install()
        self.manager.path_for('paper').unlink()
        live=self.manager.config();live.update(expected_login=123,password='never-copy',token='secret',market_time_offset_seconds=7200)
        live['web']['password']='live-secret';tool.atomic_json(self.manager.config_path,live)
        before=self.manager.config_path.read_bytes();self.manager.prepare_settings('paper')
        paper=self.manager.config('paper')
        self.assertEqual(self.manager.config_path.read_bytes(),before)
        self.assertEqual(paper['expected_login'],123)
        self.assertEqual(paper['market_time_offset_seconds'],7200)
        paper['market_time_offset_seconds']=0
        tool.atomic_json(self.manager.path_for('paper'),paper)
        self.manager.prepare_paper()
        self.assertEqual(self.manager.config('paper')['market_time_offset_seconds'],0)
        self.assertEqual(self.manager.config()['market_time_offset_seconds'],7200)
        self.assertNotIn('password',paper);self.assertNotIn('token',paper)
        self.assertEqual(paper['web'],dict(enabled=True,host='127.0.0.1',port=8766,password=''))
        self.assertEqual(paper['backend'],'paper_mt5_quotes_v1');self.assertFalse(paper['enable_trading'])
        live_path=tool.state_path(self.root,live)
        self.assertEqual(Path(paper['state_file']),live_path.with_name(live_path.stem+'.paper.sqlite3'))

    def test_two_role_launches_have_own_config_mode_session(self):
        self.install();self.passwords()
        with patch('subprocess.Popen') as proc:
            _,live=self.manager.launch(instance='live')
            _,paper=self.manager.launch(instance='paper')
        self.assertNotEqual(live,paper)
        for role,call in zip(('live','paper'),proc.call_args_list):
            args=call.args[0]
            self.assertEqual(args[args.index('--mode')+1],'mt5' if role=='live' else 'paper')
            self.assertEqual(args[args.index('--config')+1],str(self.manager.path_for(role)))
            self.assertIn('--service',args);self.assertNotIn('--execute',args)
            self.assertEqual(call.kwargs['env']['TRADFI_LAUNCH_INSTANCE'],role)
        with patch('subprocess.Popen') as proc,self.assertRaises(tool.SetupError):
            self.manager.launch(True,'paper')
        proc.assert_not_called()

    def test_stop_requests_are_role_and_session_specific(self):
        self.install();session='1'*32
        live=self.manager.request_stop(session,'live')
        self.assertTrue(live.exists())
        self.assertFalse((self.root/'data'/('stop-paper-'+session+'.request')).exists())
        paper=self.manager.request_stop(session,'paper')
        self.assertNotEqual(live,paper)
        self.assertTrue(live.exists());self.assertTrue(paper.exists())
        with self.assertRaises(tool.SetupError):self.manager.request_stop('../escape','paper')

    def test_paper_password_does_not_inherit_live_environment(self):
        self.install()
        with patch.dict('os.environ',{'TRADFI_WEB_PASSWORD':'live-environment-password'}),patch('subprocess.Popen') as proc:
            with self.assertRaises(tool.SetupError):self.manager.launch(instance='paper')
            proc.assert_not_called()
            self.passwords();self.manager.launch(instance='paper')
            self.assertNotIn('TRADFI_WEB_PASSWORD',proc.call_args.kwargs['env'])

    def test_old_release_rejects_paper_before_process_without_state_change(self):
        self.install();self.passwords();paper,_=self.paper_state()
        before=paper.read_bytes()
        (self.manager.version(A)/'code'/'tradfi'/'paper_runtime.py').unlink()
        with patch('subprocess.Popen') as proc:
            with self.assertRaisesRegex(tool.SetupError,'не поддерживает отдельный симулятор'):
                self.manager.launch(instance='paper')
            proc.assert_not_called()
            self.manager.launch(instance='live');proc.assert_called_once()
        self.assertEqual(paper.read_bytes(),before)

    def test_live_role_does_not_block_paper_launch(self):
        self.install();self.passwords()
        with tool.lock(self.root/'instance-live.lock'),patch('subprocess.Popen') as proc:
            self.manager.launch(instance='paper');proc.assert_called_once()
            with self.assertRaises(tool.SetupError):self.manager.launch(instance='live')

    def test_either_running_role_blocks_update_rollback(self):
        self.install();self.sha=B;self.install()
        for role in ('live','paper'):
            with tool.lock(self.root/('instance-'+role+'.lock')):
                with self.assertRaises(tool.SetupError):self.install()
                with self.assertRaises(tool.SetupError):self.manager.rollback()
        self.assertEqual(self.manager.pointer()['current'],B)

    def test_both_databases_backed_up_with_paper_history(self):
        self.install();self.state();paper,identity=self.paper_state()
        before=paper.read_bytes();self.sha=B;self.install()
        self.assertEqual(paper.read_bytes(),before)
        manifest=next((self.root/'backups').glob('*/manifest.json'))
        data=tool.read_json(manifest)
        for role in ('live','paper'):
            self.assertTrue(data['states'][role]['copied'])
            with sqlite3.connect(manifest.parent/data['states'][role]['backup']) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                if role=='paper':
                    saved=json.loads(db.execute('SELECT payload FROM paper_exchange').fetchone()[0])
                    self.assertEqual(saved['identity'],identity);self.assertTrue(saved['paused_gap'])
                    self.assertEqual(saved['order_history'],[{'test':'preserved'}])
        self.manager.rollback();self.assertEqual(paper.read_bytes(),before)

    def test_legacy_default_paper_database_adopted_without_reset(self):
        self.install();paper,identity=self.paper_state()
        self.manager.path_for('paper').unlink();before=paper.read_bytes()
        self.sha=B;self.install()
        self.assertEqual(tool.state_path(self.root,self.manager.config('paper')),paper)
        self.assertEqual(paper.read_bytes(),before)

    def test_bound_paper_missing_or_moved_rejected(self):
        self.install();paper,_=self.paper_state()
        tool.atomic_json(tool.binding_path(self.root,'paper'),{'path':str(paper)})
        paper.unlink();self.sha=B
        with self.assertRaises(tool.SetupError):self.install()
        self.assertFalse(paper.exists())
        with self.assertRaises(tool.SetupError):self.manager.rollback()

    def test_paper_database_lock_blocks_update(self):
        self.install();paper,_=self.paper_state();self.sha=B
        with tool.lock(str(paper)+'.lock'),self.assertRaises(tool.SetupError):self.install()
        self.assertEqual(self.manager.pointer()['current'],A)

    def test_equal_database_paths_rejected(self):
        self.install();c=self.manager.config('paper');c['state_file']=self.manager.config()['state_file']
        tool.atomic_json(self.manager.path_for('paper'),c)
        with self.assertRaises(tool.SetupError):self.install()
        with patch('subprocess.Popen') as proc,self.assertRaises(tool.SetupError):self.manager.launch(instance='paper')
        proc.assert_not_called()

if __name__=='__main__':unittest.main()
