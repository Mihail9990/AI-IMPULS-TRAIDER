"""Offline LIVE-only installation regression tests; no external processes."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import independent_install as independent
import install_tradfi as legacy
try:
    from tests import test_installer as fixtures
except ImportError:
    import test_installer as fixtures

A=fixtures.A
B=fixtures.B

def archive(sha):
    return fixtures.archive(sha)

class IndependentInstallationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'live';self.sha=A
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':self.tmp.name});self.env.start();self.addCleanup(self.env.stop)
        def fetch(url,limit=legacy.MAX_DOWNLOAD):
            return json.dumps({'ref':'refs/heads/work','object':{'type':'commit','sha':self.sha}}).encode() if '/git/ref/' in url else archive(self.sha)
        self.manager=independent.IndependentManager(self.root,'live',fetch=fetch,report=lambda _:None)
        self.prep=patch.object(self.manager,'prepare_python');self.prep.start();self.addCleanup(self.prep.stop)
    def test_initial_install_is_live_only_and_safe(self):
        self.manager.install()
        self.assertEqual(legacy.read_json(self.root/'application.json'),{'format':1,'role':'live'})
        self.assertFalse(self.manager.config()['enable_trading'])
        self.assertEqual(self.manager.pointer()['current'],A)
        self.assertEqual(self.manager.config()['web']['port'],8765)
    def test_unknown_role_rejected_before_writes(self):
        with self.assertRaises(legacy.SetupError):independent.IndependentManager(self.root,'unrelated')
        self.assertFalse(self.root.exists())
    def test_update_and_rollback_preserve_config(self):
        self.manager.install();before=self.manager.config_path.read_bytes()
        self.sha=B;self.manager.install();self.manager.rollback()
        self.assertEqual(self.manager.pointer()['current'],A)
        self.assertEqual(self.manager.config_path.read_bytes(),before)
    def test_legacy_contract_rejected_with_pointer_and_data_intact(self):
        self.manager.install();before=self.manager.config_path.read_bytes();self.sha=B
        blob=archive(B);out=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blob)) as src,zipfile.ZipFile(out,'w') as dst:
            for item in src.infolist():
                data=src.read(item)
                if item.filename.endswith('independent-install-contract.json'):
                    data=json.dumps({'format':1,'roles':['live','paper'],'layout':'independent_roots_v1'}).encode()
                dst.writestr(item,data)
        old=self.manager.fetch
        self.manager.fetch=lambda url,limit=legacy.MAX_DOWNLOAD:old(url,limit) if '/git/ref/' in url else out.getvalue()
        with self.assertRaises(legacy.SetupError):self.manager.install()
        self.assertEqual(self.manager.pointer()['current'],A)
        self.assertEqual(self.manager.config_path.read_bytes(),before)
    def test_running_live_blocks_update_and_rollback(self):
        self.manager.install();self.sha=B;self.manager.install()
        with legacy.lock(self.root/'instance-live.lock'):
            with self.assertRaises(legacy.SetupError):self.manager.install()
            with self.assertRaises(legacy.SetupError):self.manager.rollback()
    def test_missing_config_never_recreated(self):
        self.manager.install();self.manager.config_path.unlink()
        with self.assertRaises(legacy.SetupError):self.manager.install()
        self.assertFalse(self.manager.config_path.exists())
    def test_foreign_root_rejected(self):
        self.manager.install();legacy.atomic_json(self.root/'application.json',{'format':1,'role':'foreign'})
        with self.assertRaises(legacy.SetupError):self.manager.install()
    def test_live_launch_is_read_only_by_default(self):
        self.manager.install();config=self.manager.config();config['web']['password']='local-password';legacy.atomic_json(self.manager.config_path,config)
        with patch('subprocess.Popen') as proc:self.manager.launch()
        args=proc.call_args.args[0]
        self.assertEqual(args[args.index('--mode')+1],'mt5');self.assertNotIn('--execute',args)
    def test_old_current_release_can_update_but_not_rollback_to_old_contract(self):
        self.manager.install()
        old=self.manager.version(A)/'code'/'independent-install-contract.json'
        legacy.atomic_json(old,{'format':1,'roles':['live','paper'],'layout':'independent_roots_v1'})
        before=self.manager.config_path.read_bytes();self.sha=B;self.manager.install()
        with self.assertRaises(legacy.SetupError):self.manager.rollback()
        self.assertEqual(self.manager.pointer()['current'],B)
        self.assertEqual(self.manager.config_path.read_bytes(),before)

if __name__=='__main__':unittest.main()
