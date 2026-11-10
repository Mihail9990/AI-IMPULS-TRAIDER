import io
import json
import os
import struct
import tempfile
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch
import unittest
import https_setup as h
from test_https_setup import Fake

class LocalZip(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        h.save(self.root/'managed-install.json',{'format':2,'role':'live','layout':'independent_roots_v1'})
        h.save(self.root/'application.json',{'format':1,'role':'live'})
        (self.root/'data').mkdir();(self.root/'data'/'service.json').write_text(json.dumps({'enable_trading':False,'web':{'password':'fixture'*8}}))
        self.a=Fake();self.s=h.Setup(self.root,self.a);self.data=self.a.fetch();self.path=self.root/'download.zip';self.path.write_bytes(self.data)
        self.patch=patch.object(h,'ARCHIVE_SHA256',h.digest(self.data));self.patch.start();self.addCleanup(self.patch.stop)
    def data_with(self,entries):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            for name,value in entries:z.writestr(name,value)
        return data.getvalue()
    def pe(self):
        with zipfile.ZipFile(io.BytesIO(self.data)) as z:return z.read('caddy.exe')
    def malformed(self,data):
        with patch.object(h,'ARCHIVE_SHA256',h.digest(data)),self.assertRaises(h.HttpsError):h.validate_archive(data)
    def test_good_archive_selected_without_network(self):
        self.a.fetch=lambda:(_ for _ in ()).throw(AssertionError('No network download allowed'))
        self.assertTrue(self.s.select_local_zip(self.path));p=self.s.prepare('38.60.203.9')
        self.assertEqual(p['archive_source'],'LOCAL_OFFICIAL_SHA256');self.s.apply(p,True)
        self.assertEqual((self.s.home/'caddy.exe').read_bytes(),self.pe())
        self.assertNotIn(str(self.path),self.s.attempt_path.read_text())
    def test_changed_file_after_verification_uses_same_bytes(self):
        self.s.select_local_zip(self.path);self.path.write_bytes(b'changed after selection')
        self.a.fetch=lambda:(_ for _ in ()).throw(AssertionError())
        self.s.apply(self.s.prepare('38.60.203.9'),True)
        self.assertEqual((self.s.home/'caddy.exe').read_bytes(),self.pe())
    def test_file_deleted_after_selection(self):
        self.s.select_local_zip(self.path);self.path.unlink();self.s.apply(self.s.prepare('38.60.203.9'),True)
        self.assertTrue((self.s.home/'caddy.exe').exists())
    def test_wrong_hash_or_version_rejected(self):
        self.path.write_bytes(b'other version')
        with self.assertRaisesRegex(h.HttpsError,'SHA256'):self.s.select_local_zip(self.path)
        self.assertIsNone(self.s.local_archive);self.assertFalse(self.s.home.exists())
    def test_wrong_architecture_rejected_even_with_matching_test_digest(self):
        exe=bytearray(self.pe());struct.pack_into('<H',exe,68,0x14c)
        self.malformed(self.data_with([('caddy.exe',exe)]))
    def test_wrong_pe32_format(self):
        exe=bytearray(self.pe());struct.pack_into('<H',exe,88,0x10b)
        self.malformed(self.data_with([('caddy.exe',exe)]))
    def test_not_pe(self):self.malformed(self.data_with([('caddy.exe',b'not executable')]))
    def test_corrupt_zip(self):self.malformed(b'broken')
    def test_missing_exe(self):self.malformed(self.data_with([('README.md',b'hi')]))
    def test_path_traversal_absolute_extra_entries_rejected(self):
        for name in ('../owned','/absolute','C:/temp/evil','folder/caddy.exe','caddy.EXE','other.exe'):
            with self.subTest(name=name):self.malformed(self.data_with([('caddy.exe',self.pe()),(name,b'bad')]))
    def test_duplicate_exe_rejected(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore');self.malformed(self.data_with([('caddy.exe',self.pe()),('caddy.exe',self.pe())]))
    def test_symlink_rejected(self):
        entry=zipfile.ZipInfo('caddy.exe');entry.create_system=3;entry.external_attr=0o120777<<16
        self.malformed(self.data_with([(entry,b'/target')]))
    def test_size_limit_before_read(self):
        with patch.object(h,'MAX_ARCHIVE',1),self.assertRaises(h.HttpsError):self.s.select_local_zip(self.path)
    def test_cancel_preserves_selection(self):
        self.s.select_local_zip(self.path);self.assertFalse(self.s.select_local_zip(''));self.assertEqual(self.s.local_archive,self.data)
    def test_bad_reselection_clears_previous_selection(self):
        self.s.select_local_zip(self.path);self.path.write_bytes(b'bad')
        with self.assertRaises(h.HttpsError):self.s.select_local_zip(self.path)
        self.assertIsNone(self.s.local_archive)
    def test_read_failure_is_redacted(self):
        self.s.select_local_zip(self.path)
        with patch.object(Path,'open',side_effect=PermissionError('secret path/password')),self.assertRaises(h.HttpsError) as e:self.s.select_local_zip(self.path)
        self.assertNotIn('secret',str(e.exception));self.assertIsNone(self.s.local_archive)
    def test_network_path_rejected(self):
        with self.assertRaises(h.HttpsError):self.s.select_local_zip(r'\\server\private\file.zip')
    def test_source_change_requires_new_plan(self):
        p=self.s.prepare('38.60.203.9');self.s.select_local_zip(self.path)
        with self.assertRaises(h.AttemptError):self.s.apply(p,True)
        self.assertFalse(self.s.home.exists())
    def test_previous_tls_failure_requires_recovery_then_local(self):
        p=self.s.prepare('38.60.203.9');self.a.fetch=lambda:(_ for _ in ()).throw(urllib.error.URLError('TLS simulated'))
        with self.assertRaises(h.AttemptError):self.s.apply(p,True)
        self.s.select_local_zip(self.path)
        with self.assertRaises(h.HttpsError):self.s.apply(p,True)
        with self.assertRaises(h.HttpsError):self.s.prepare('38.60.203.9')
        self.s.recover_preparation(True);self.s.apply(self.s.prepare('38.60.203.9'),True)
        self.assertEqual(h.read(self.s.attempt_path)['archive_source'],'LOCAL_OFFICIAL_SHA256')
        self.assertTrue(any((self.root/'https-recovery').glob('*.json')))
    def test_local_selection_does_not_bypass_unknown_service_result(self):
        self.a.fail='create'
        with self.assertRaises(h.AttemptError):self.s.apply(self.s.prepare('38.60.203.9'),True)
        self.s.select_local_zip(self.path)
        with self.assertRaises(h.HttpsError):self.s.recover_preparation(True)
    def test_return_to_https_is_explicit(self):
        self.s.select_local_zip(self.path);self.s.use_download();self.assertEqual(self.s.archive_source(),'OFFICIAL_HTTPS_DOWNLOAD')
    def test_repeat_apply_does_not_duplicate_local_service(self):
        self.s.select_local_zip(self.path);p=self.s.prepare('38.60.203.9');self.s.apply(p,True)
        with self.assertRaises(h.HttpsError):self.s.apply(p,True)
        self.assertEqual(sum(c[0]=='create' for c in self.a.calls),1)

if __name__=='__main__':unittest.main()
