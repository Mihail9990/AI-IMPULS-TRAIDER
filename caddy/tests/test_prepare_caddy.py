import io,zipfile,struct,tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import archive_verifier as v
import prepare_caddy as p
class Tests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)/'AI_IMPULS_RECOVERY_WEB';self.root.mkdir(parents=True)
  exe=bytearray(128);exe[:2]=b'MZ';struct.pack_into('<I',exe,0x3c,64);exe[64:68]=b'PE\0\0';struct.pack_into('<H',exe,68,0x8664);struct.pack_into('<H',exe,84,2);struct.pack_into('<H',exe,88,0x20b);self.exe=bytes(exe)
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w') as z:z.writestr('caddy.exe',self.exe)
  self.data=out.getvalue();self.archive=self.root.parent/'official.zip';self.archive.write_bytes(self.data)
  self.addCleanup(patch.stopall);patch.object(v,'ARCHIVE_SHA256',v.digest(self.data)).start();patch.object(p,'EXE_SHA256',v.digest(self.exe)).start()
 def test_manual_zip_no_network_no_execution(self):
  result=p.prepare_caddy(self.root,self.archive,confirmed=True,opener=lambda *a,**k:self.fail('network'))
  self.assertEqual(result['status'],'prepared');self.assertFalse(result['caddy_started']);self.assertEqual((self.root/'tools/caddy/caddy.exe').read_bytes(),self.exe)
  self.assertEqual(p.prepare_caddy(self.root,self.archive,confirmed=True)['status'],'existing_verified')
 def test_confirmation_and_old_root_denied(self):
  with self.assertRaises(v.HttpsError):p.prepare_caddy(self.root,self.archive)
  with self.assertRaises(v.HttpsError):p.prepare_caddy(self.root.parent.parent/'AI_IMPULS_LIVE'/'server',self.archive,confirmed=True)
  self.assertFalse((self.root/'tools').exists())
 def test_bad_archive_no_exe_and_foreign_exe_preserved(self):
  self.archive.write_bytes(b'bad')
  with self.assertRaises(v.HttpsError):p.prepare_caddy(self.root,self.archive,confirmed=True)
  target=self.root/'tools/caddy/caddy.exe';target.parent.mkdir(parents=True);target.write_bytes(b'foreign')
  with self.assertRaises(v.HttpsError):p.prepare_caddy(self.root,self.archive,confirmed=True)
  self.assertEqual(target.read_bytes(),b'foreign')
 def test_official_download_path(self):
  called=[]
  result=p.prepare_caddy(self.root,confirmed=True,opener=lambda url,**kw:called.append(url) or io.BytesIO(self.data))
  self.assertEqual(called,[v.DOWNLOAD]);self.assertEqual(result['status'],'prepared')
 def test_symlink_denied(self):
  (self.root/'tools').symlink_to(self.root.parent,target_is_directory=True)
  with self.assertRaises(v.HttpsError):p.prepare_caddy(self.root,self.archive,confirmed=True)
if __name__=='__main__':unittest.main()
