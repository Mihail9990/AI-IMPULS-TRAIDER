import io
import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch
import setup_core as s

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.payload=self.root/'payload'; self.payload.mkdir()
        (self.payload/'requirements.txt').write_text('fastapi')
        (self.payload/'run_recovery_native.py').write_text('# no execution')
        self.python=self.root/'python.exe'; self.python.touch()
    def tearDown(self): self.tmp.cleanup()
    def test_existing_not_modified(self):
        dst=self.root/'server'; dst.mkdir(); (dst/'state.db').write_text('precious')
        with self.assertRaises(s.SetupError): s.copy_server(self.payload,dst)
        self.assertEqual((dst/'state.db').read_text(),'precious')
    def test_install_command_contract(self):
        calls=[]
        def runner(args,**kw):
            self.assertFalse(kw['shell']); calls.append(args)
            if args[1:3]==['-m','venv']: Path(args[-1]).mkdir()
            return SimpleNamespace(returncode=0,stdout='OK',stderr='')
        dst=s.install(self.payload,self.root/'server',self.python,log=lambda _:None,runner=runner,platform='nt')
        self.assertTrue((dst/'.venv-native/installed-versions.txt').exists())
        joined=' '.join(' '.join(c) for c in calls)
        self.assertIn('--only-binary=:all:',joined)
        for forbidden in ('order_send(', 'initialize(', 'Start-Native', 'firewall', 'ExecutionPolicy'):
            self.assertNotIn(forbidden,joined)
    def test_bad_python_no_copy(self):
        with self.assertRaises(s.SetupError):
            s.install(self.payload,self.root/'server',self.python,log=lambda _:None,platform='nt',runner=lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='bad'))
        self.assertFalse((self.root/'server').exists())
    def test_ea_never_overwrites(self):
        src=self.payload/'recovery/mql5'; src.mkdir(parents=True); (src/'RecoveryEvidenceBridge.mq5').write_text('source')
        data=self.root/'terminal'; (data/'MQL5/Experts').mkdir(parents=True)
        p=s.prepare_ea(self.payload,data); self.assertEqual(p.read_text(),'source')
        with self.assertRaises(s.SetupError): s.prepare_ea(self.payload,data)
    def archive(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            z.writestr('caddy.exe',b'MZsafe fixture'); z.writestr('../evil',b'ignored')
        return b.getvalue()
    def test_caddy_hash_and_exclusive(self):
        raw=self.archive(); server=self.root/'server'; server.mkdir()
        with patch.object(s,'CADDY_SHA256',hashlib.sha256(raw).hexdigest()):
            target=s.install_caddy(server,log=lambda _:None,opener=lambda *a,**k:io.BytesIO(raw))
        self.assertEqual(target.read_bytes(),b'MZsafe fixture'); self.assertFalse((server/'tools/evil').exists())
        with self.assertRaises(s.SetupError): s.install_caddy(server,log=lambda _:None)
    def test_caddy_checksum_failure_then_retry(self):
        raw=self.archive(); server=self.root/'server'; server.mkdir()
        with self.assertRaises(s.SetupError): s.install_caddy(server,log=lambda _:None,opener=lambda *a,**k:io.BytesIO(raw))
        self.assertFalse((server/'tools/caddy/caddy.exe').exists())
        with patch.object(s,'CADDY_SHA256',hashlib.sha256(raw).hexdigest()):
            self.assertTrue(s.install_caddy(server,log=lambda _:None,opener=lambda *a,**k:io.BytesIO(raw)).exists())
    def test_caddy_network_failure_no_executable(self):
        server=self.root/'server'; server.mkdir()
        def bad(*a,**k): raise OSError('offline')
        with self.assertRaises(OSError): s.install_caddy(server,log=lambda _:None,opener=bad)
        self.assertFalse((server/'tools/caddy/caddy.exe').exists())
    def test_preexisting_stage_preserved(self):
        server=self.root/'server'; folder=server/'tools/caddy'; folder.mkdir(parents=True)
        stage=folder/'caddy.exe.prepared'; stage.write_text('old incomplete file')
        raw=self.archive()
        with patch.object(s,'CADDY_SHA256',hashlib.sha256(raw).hexdigest()):
            with self.assertRaises(FileExistsError): s.install_caddy(server,log=lambda _:None,opener=lambda *a,**k:io.BytesIO(raw))
        self.assertEqual(stage.read_text(),'old incomplete file')

class ResumeTests(SetupTests):
    def installed(self):
        target=s.copy_server(self.payload,self.root/'installed')
        v=target/'.venv-native'; (v/'Scripts').mkdir(parents=True)
        (v/'Scripts/python.exe').write_bytes(b'MZfixture')
        (v/'pyvenv.cfg').write_text('home = C:/Python')
        (v/'installed-versions.txt').write_text('fastapi==0.1')
        return target
    def test_adopt_no_execute_preserves_data(self):
        t=self.installed(); (t/'settings.json').write_text('keep')
        with patch.object(s.subprocess,'run',side_effect=AssertionError('must not run')):
            self.assertEqual(s.adopt_existing(self.payload,t),t)
        self.assertEqual((t/'settings.json').read_text(),'keep')
    def test_adopt_changed_source_rejected(self):
        t=self.installed(); (t/'run_recovery_native.py').write_text('changed')
        with self.assertRaises(s.SetupError):s.adopt_existing(self.payload,t)
    def test_adopt_incomplete_venv_rejected(self):
        t=self.installed(); (t/'.venv-native/installed-versions.txt').unlink()
        with self.assertRaises(s.SetupError):s.adopt_existing(self.payload,t)
    def test_local_caddy_no_network(self):
        t=self.installed(); archive=self.root/'caddy.zip'; raw=self.archive(); archive.write_bytes(raw)
        with patch.object(s,'CADDY_SHA256',hashlib.sha256(raw).hexdigest()):
            dest=s.install_caddy(t,log=lambda _:None,local_archive=archive,opener=lambda *a,**k: self.fail('network called'))
        self.assertEqual(dest.read_bytes(),b'MZsafe fixture')
    def test_local_caddy_mismatch_no_install(self):
        t=self.installed(); archive=self.root/'bad.zip'; archive.write_bytes(b'bad')
        with self.assertRaises(s.SetupError):s.install_caddy(t,log=lambda _:None,local_archive=archive)
        self.assertFalse((t/'tools/caddy/caddy.exe').exists())

if __name__=='__main__': unittest.main()
