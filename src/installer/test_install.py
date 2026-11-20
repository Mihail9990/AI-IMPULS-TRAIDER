"""Full install into temporary Windows folders with a fake terminal and real wheels."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import build_package
import install
from package_spec import HELPERS, SHORTCUT, digest


class InstallerTests(unittest.TestCase):
    def test_os_owner_conflicts_stop_install_before_any_switch(self):
        with tempfile.TemporaryDirectory(prefix="recovery-install-locks-") as td:
            root=Path(td)
            data=root/"data"
            (data/"MQL5/Files").mkdir(parents=True)
            cfg={"data_dir":str(data)}
            k=ctypes.WinDLL("kernel32",use_last_error=True)
            k.CreateMutexW.argtypes=[wintypes.LPVOID,wintypes.BOOL,wintypes.LPCWSTR]
            k.CreateMutexW.restype=wintypes.HANDLE
            k.CloseHandle.argtypes=[wintypes.HANDLE]
            private=root/"control/python-v1"
            mutex="Local\\RECOVERY.Python."+hashlib.sha256(str(private).casefold().encode()).hexdigest()[:24]
            handle=k.CreateMutexW(None,False,mutex)
            self.assertTrue(handle)
            try:
                with self.assertRaisesRegex(ValueError,"Контроллер Python"):
                    install.preflight(root,cfg)
            finally:
                k.CloseHandle(handle)
            with install.check_owner_files(cfg):
                with self.assertRaisesRegex(ValueError,"удерживает управление"):
                    with install.check_owner_files(cfg):
                        self.fail("owner reused")
            self.assertFalse(private.exists())

    def test_offline_integrity_migration_shortcut_and_safe_repeat(self):
        with tempfile.TemporaryDirectory(prefix="recovery-offline-test-") as td:
            root = Path(td)
            package, source, sums = build_package.build(SOURCE, HELPERS_DIR, WHEEL_DIR, root/"release")
            self.assertIn(digest(package), sums.read_text())
            with ZipFile(source) as z:
                self.assertNotIn("machine.json", z.namelist())
                self.assertIn("trade_wakeup.py", z.namelist())
                self.assertIn("control_process.py", z.namelist())
                self.assertTrue(all(not n.endswith((".whl",".ex5",".jsonl",".sqlite3")) for n in z.namelist()))
            kit = root/"kit"
            with ZipFile(package) as z:
                z.extractall(kit)
            base, data, desk = root/"local", root/"terminal-data", root/"windows-desktop"
            (data/"MQL5/Files").mkdir(parents=True)
            exe = root/"terminal64.exe"
            exe.write_bytes(b"inert test fixture")
            desk.mkdir()
            old = base/"control/desk"
            old.mkdir(parents=True)
            binding=dict(login=12345,server="Fixture",symbol="TEST",magic=987)
            (old/"settings.json").write_text(json.dumps(dict(data_dir=str(data),terminal_exe=str(exe))))
            (base/"recovery-profile.json").write_text(json.dumps(dict(mt5=dict(login=12345,server="Fixture",symbol="TEST"),panel_key="synthetic-only")))
            (base/"role-session.json").write_text(json.dumps(dict(expected_login=12345,expected_server="Fixture",
                                                                expected_symbol="TEST",strategy_magic=987)))
            before=(base/"recovery-profile.json").read_bytes()
            redirect=install.shortcut
            with patch.object(install,"shortcut",lambda target:redirect(target,desk)):
                version=install.install(kit,base,interactive=False)
                target=base/"control/python-builds"/version
                self.assertTrue((target/"recovery_desktop.pyw").is_file())
                self.assertEqual((target/"trade_wakeup.py").read_bytes(), (SOURCE/"trade_wakeup.py").read_bytes())
                self.assertEqual((target/"control_process.py").read_bytes(), (SOURCE/"control_process.py").read_bytes())
                self.assertTrue((desk/SHORTCUT).is_file())
                local=base/"control/python-v1"
                cfg=install.read(local/"machine.json")
                self.assertTrue(all(cfg[k]==v for k,v in binding.items()))
                self.assertNotIn("panel_key",cfg)
                for stem in HELPERS:
                    self.assertEqual(install.read_preset(local/"presets"/(stem+".set")), install.preset_values(cfg))
                    self.assertEqual(digest(data/"MQL5/Experts/RECOVERY-Python"/(stem+".ex5")),digest(HELPERS_DIR/(stem+".ex5")))
                db=local/"recovery.sqlite3"
                conn = sqlite3.connect(db)
                try:
                    conn.execute("CREATE TABLE retained_legacy_state(k INTEGER PRIMARY KEY, wire TEXT)")
                    conn.execute("INSERT INTO retained_legacy_state VALUES (1, 'expected')")
                    conn.commit()
                finally:
                    conn.close()
                previous_db = db.read_bytes()
                self.assertEqual(install.install(kit,base,interactive=False),version)
                self.assertEqual(install.read(local/"machine.json"),cfg)
                self.assertEqual(db.read_bytes(),previous_db)
                self.assertEqual((base/"recovery-profile.json").read_bytes(),before)
                self.assertFalse((local/"status.json").exists())
                self.assertFalse((local/"python-runtime.log").exists())
                mismatch=local/"presets/RecoveryPythonSL.set"
                changed=mismatch.read_bytes()+("ExpectedOther=x\r\n").encode("utf-16-le")
                mismatch.write_bytes(changed)
                with self.assertRaises(ValueError):
                    install.prepare_data(kit,local,cfg)
                self.assertEqual(mismatch.read_bytes(),changed)
                (kit/"app/recovery_desktop.pyw").write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    install.install(kit,base,verify_only=True)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    for k in ("source","helpers","wheels"):
        p.add_argument("--"+k,required=True,type=Path)
    args=p.parse_args()
    SOURCE, HELPERS_DIR, WHEEL_DIR = args.source,args.helpers,args.wheels
    unittest.main(argv=[sys.argv[0]])
