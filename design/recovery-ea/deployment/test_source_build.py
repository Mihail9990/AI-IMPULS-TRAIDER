import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from source_build import (BASE, CANDIDATE, DIAGNOSTIC, ROLE, SCHEMA, SUPPORT, TRADER, check_candidate_results, compile_sources, package, read_package,
                          file_product_version, result_counts, validated_path, validated_targets)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.repo=self.root/"repo"
        self.repo.mkdir()
        self.git("init","-q")
        self.git("config","user.name","Test")
        self.git("config","user.email","test@example.invalid")
        self.source(DIAGNOSTIC[0],'#include "part.mqh"\n')
        self.source(DIAGNOSTIC[1],'#include "watcher.mqh"\n')
        self.source(DIAGNOSTIC[2],'#include "../verifier/part.mqh"\n')
        self.source('watcher/watcher.mqh', '// Price statistics\n')
        self.source("verifier/part.mqh","// Git-committed source\n")
        self.commit=self.checkpoint()
        self.archive=self.root/"a.zip"

    def source(self,name,value):
        path=self.repo/BASE/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(value,encoding="utf-8")
        return path

    def git(self,*args):
        return subprocess.check_output(["git","-C",str(self.repo),*args]).decode().strip()

    def checkpoint(self):
        self.git("add",".")
        self.git("commit","-qm","test")
        return self.git("rev-parse","HEAD")

    def test_diagnostic_immutable_deterministic_and_hash_bound(self):
        self.source("verifier/part.mqh","// Uncommitted changes\n")
        self.source("runtime/RecoveryTrader.mq5","")
        self.source("deployment/token.txt","not packaged")
        package(self.repo,self.commit,self.archive)
        other=self.root/"b.zip"
        package(self.repo,self.commit,other)
        self.assertEqual(sha(self.archive),sha(other))
        m,files=read_package(self.archive,self.commit,sha(other))
        self.assertEqual(m["mode"],"diagnostic")
        self.assertEqual(m["schema"],SCHEMA)
        self.assertEqual(m["targets"],list(DIAGNOSTIC))
        self.assertEqual(set(files),{*DIAGNOSTIC,"verifier/part.mqh","watcher/watcher.mqh"})
        self.assertIn(b"Git-committed",files["verifier/part.mqh"])
        with self.assertRaisesRegex(ValueError,"ZIP SHA"):
            read_package(self.archive,self.commit,"f"*64)
        with self.assertRaises(FileExistsError):
            package(self.repo,self.commit,other)

    def test_candidate_path_requires_different_commit(self):
        trader=TRADER
        self.source("execution/OtherTrader.mq5","")
        self.source(trader,'#include "../verifier/part.mqh"\n')
        with self.assertRaises(ValueError):
            package(self.repo,self.commit,self.archive,trader)
        commit=self.checkpoint()
        with self.assertRaises(ValueError):
            package(self.repo,commit,self.archive,"execution/OtherTrader.mq5")
        m = package(self.repo,commit,self.archive,trader)
        self.assertEqual(m["mode"],"candidate")
        self.assertEqual(m["targets"],[*SUPPORT,trader])
        _, files = read_package(self.archive, commit, sha(self.archive))
        self.assertIn(DIAGNOSTIC[1], files)
        self.assertNotIn(DIAGNOSTIC[2], files)

    def test_old_role_manifests_and_missing_watcher_rejected(self):
        with self.assertRaises(ValueError):
            validated_targets("diagnostic", [DIAGNOSTIC[0],"probe/RecoveryPairProbe.mq5"])
        with self.assertRaises(ValueError):
            validated_targets("candidate", list(DIAGNOSTIC))
        with self.assertRaises(ValueError):
            validated_targets("diagnostic", list(reversed(DIAGNOSTIC)))

        self.source(DIAGNOSTIC[1], "").unlink()
        without_watcher = self.checkpoint()
        with self.assertRaisesRegex(ValueError, "RecoveryPriceWatcher"):
            package(self.repo,without_watcher,self.archive)
        self.assertFalse(self.archive.exists())

    def test_escaped_include_and_reserved_path_rejected(self):
        self.source(DIAGNOSTIC[0],'#include "../../../other.mqh"\n')
        with self.assertRaises(ValueError):
            package(self.repo,self.checkpoint(),self.archive)
        for invalid in ("execution/COM1.mq5","aux/t.mqh","x/../a.mq5"):
            with self.assertRaises(ValueError):
                validated_path(invalid)


    def test_candidate_allowlist_and_dependency_closure_checked_on_read(self):
        self.source(TRADER, '#include "own.mqh" // pure source\n')
        self.source("execution/own.mqh", '#include "..\\verifier\\part.mqh"\n')
        commit=self.checkpoint()
        original=package(self.repo,commit,self.archive,TRADER)
        _, files=read_package(self.archive,commit,sha(self.archive))
        self.assertEqual(set(files), {*CANDIDATE, "execution/own.mqh", "verifier/part.mqh", "watcher/watcher.mqh"})

        def forged(name, patch_files):
            payload=dict(files)
            patch_files(payload)
            out=self.root/name
            manifest=dict(original)
            manifest["sha256"]={k:hashlib.sha256(v).hexdigest() for k,v in payload.items()}
            with zipfile.ZipFile(out, "w") as z:
                z.writestr("manifest.json", json.dumps(manifest))
                for k,v in payload.items():
                    z.writestr("src/"+k, v)
            return out

        for file, change, error in (
            ("extra.zip", lambda entries: entries.update({"execution/extra.mqh":b"// stray"}), "outside"),
            ("missing.zip", lambda entries: entries.pop("execution/own.mqh"), "Missing local header"),
            ("external.zip", lambda entries: entries.update({TRADER:b"#include <Trade.mqh>"}), "Unsupported"),
            ("resource.zip", lambda entries: entries.update({TRADER:b'#resource "private.bin"'}), "Unsupported"),
            ("dll.zip", lambda entries: entries.update({TRADER:b'#import "custom.dll"'}), "Unsupported"),
        ):
            out=forged(file,change)
            with self.assertRaisesRegex(ValueError, error):
                read_package(out,commit,sha(out))

        self.source(TRADER, '#include "own.mqh"\n#include "OWN.mqh"\n')
        self.source("execution/OWN.mqh","// case collision")
        with self.assertRaisesRegex(ValueError, "Windows filename collision"):
            package(self.repo,self.checkpoint(),self.root/"collision.zip",TRADER)
        self.source(TRADER, '#include "../probe/RecoveryCompileProbe.mq5"\n')
        with self.assertRaisesRegex(ValueError, "Only local MQL headers"):
            package(self.repo,self.checkpoint(),self.root/"entry-leak.zip",TRADER)

    def test_native_candidate_provenance_consistency(self):
        self.source(TRADER, '#include "../verifier/part.mqh"\n')
        commit=self.checkpoint()
        manifest=package(self.repo,commit,self.archive,TRADER)
        ed=self.root/"MetaEditor64.exe"
        ed.write_bytes(b"fake-editor")
        def fake(command, **kwargs):
            src=Path(re.search(r'/compile:"([^"]+)"',command).group(1))
            src.with_suffix(".log").write_text("Result: 0 errors, 1 warning", encoding="utf-16")
            src.with_suffix(".ex5").write_bytes(src.name.encode())
            return subprocess.CompletedProcess(command,0)
        output=self.root/"out"
        archive_hash=sha(self.archive)
        with patch("source_build.sys.platform","win32"), patch("source_build.file_product_version",return_value=("5.0.0.6308","5.0.0.6308")), patch("source_build.subprocess.run",side_effect=fake):
            report=compile_sources(self.archive,commit,archive_hash,ed,output,"candidate")
        self.assertEqual([i["role"] for i in report["targets"]], [ROLE[m] for m in CANDIDATE])
        self.assertEqual(check_candidate_results(self.archive,commit,archive_hash,output),report)
        for t in report["targets"]:
            self.assertEqual(t["source_sha256"], manifest["sha256"][t["source"]])
            self.assertEqual(t["log_sha256"], sha(output/t["log"]))
            self.assertEqual(t["ex5_sha256"], sha(output/t["ex5"]))
        log=output/report["targets"][-1]["log"]
        raw=log.read_bytes()
        log.write_bytes(b"Result: 0 errors, 2 warnings")
        with self.assertRaisesRegex(ValueError,"mismatched log"):
            check_candidate_results(self.archive,commit,archive_hash,output)
        log.write_bytes(raw)
        ex5=output/report["targets"][-1]["ex5"]
        clean=ex5.read_bytes()
        ex5.unlink()
        with self.assertRaisesRegex(ValueError,"mismatched ex5"):
            check_candidate_results(self.archive,commit,archive_hash,output)
        ex5.write_bytes(clean)
        (output/"extra.ex5").touch()
        with self.assertRaisesRegex(ValueError,"Unexpected files"):
            check_candidate_results(self.archive,commit,archive_hash,output)

    def test_source_mutation_during_mock_compile_never_attested(self):
        package(self.repo,self.commit,self.archive)
        ed=self.root/"MetaEditor64.exe"
        ed.touch()
        def fake(command, **kwargs):
            src=Path(re.search(r'/compile:"([^"]+)"',command).group(1))
            src.write_text("// not pinned")
            src.with_suffix(".log").write_text("Result: 0 errors, 0 warnings")
            src.with_suffix(".ex5").write_bytes(b"unproven")
            return subprocess.CompletedProcess(command,0)
        output=self.root/"outputs"
        with patch("source_build.sys.platform","win32"), patch("source_build.file_product_version",return_value=("5.0.0.6308","5.0.0.6308")), patch("source_build.subprocess.run",side_effect=fake) as proc:
            report=compile_sources(self.archive,self.commit,sha(self.archive),ed,output)
        self.assertEqual(proc.call_count,1)
        self.assertTrue(all(r["reason"]=="pinned_sources_changed_or_unreadable" for r in report["targets"]))
        self.assertFalse(list(output.glob("*.ex5")))

    def test_compiler_log_counts(self):
        line="Result: 0 errors, 1 warning, 20 msec elapsed"
        for enc in ("utf-16","utf-16-le","utf-8"):
            self.assertEqual(result_counts(line.encode(enc)),(0,1))
        with self.assertRaises(ValueError):
            result_counts(b"build requested; no summary")
        with self.assertRaises(ValueError):
            result_counts((line+"\n"+line).encode())

    def test_compiler_double_compilation_only_result_and_failure(self):
        package(self.repo,self.commit,self.archive)
        ed=self.root/"MetaEditor64.exe"
        ed.touch()
        def fake(command, **kwargs):
            self.assertIn(' /compile:"',command)
            path=Path(re.search(r'/compile:"([^"]+)"',command).group(1))
            self.assertEqual(kwargs["executable"],str(ed))
            self.assertNotIn(str(self.repo),str(path))
            good=path.name!="RecoveryCompileProbe.mq5"
            path.with_suffix(".log").write_text(f"Result: {0 if good else 1} errors, 0 warnings",encoding="utf-16")
            if good:
                path.with_suffix(".ex5").write_bytes(b"fake-ex5")
            return subprocess.CompletedProcess(command,1)
        with patch("source_build.sys.platform","win32"), patch("source_build.file_product_version",return_value=("5.0.0.6308","5.0.0.6307")), patch("source_build.subprocess.run",side_effect=fake):
            output=self.root/"out"
            report=compile_sources(self.archive,self.commit,sha(self.archive),ed,output)
            self.assertEqual([r["status"] for r in report["targets"]],["compiled","compiled","failed"])
            self.assertEqual(len(list(output.glob("*.ex5"))),2)
            self.assertEqual(len(list(output.glob("*.log"))),3)
            payload = (output/"compile-report.json").read_text()
            self.assertNotIn(str(self.root), payload)
            self.assertEqual(json.loads(payload)["status"],"compile_only_not_deployment")
            self.assertEqual(report["compiler"], {"sha256":sha(ed), "file_version":"5.0.0.6308", "product_version":"5.0.0.6307"})

    def test_actual_fixed_version_fields_not_reported_on_invalid_resource(self):
        ver = (ctypes.c_uint32*13)(0xfeef04bd,0,0x50000,6308,0x50000,6307,*([0]*7))
        api = Mock()
        api.GetFileVersionInfoSizeW.return_value = ctypes.sizeof(ver)
        def load(_path, _reserved, size, buf):
            ctypes.memmove(buf, ver, size)
            return 1
        def query(buf, node, value, length):
            self.assertEqual(node, "\\")
            ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(buf)
            ctypes.cast(length, ctypes.POINTER(wintypes.UINT)).contents.value = ctypes.sizeof(ver)
            return 1
        api.GetFileVersionInfoW.side_effect = load
        api.VerQueryValueW.side_effect = query
        with patch("ctypes.WinDLL",return_value=api,create=True):
            self.assertEqual(file_product_version(self.root/"unused"),("5.0.0.6308","5.0.0.6307"))
            ver[0] = 0
            with self.assertRaises(ValueError):
                file_product_version(self.root/"unused")

    def test_executable_changed_and_sensitive_exception_not_disclosed(self):
        package(self.repo,self.commit,self.archive)
        ed=self.root/"MetaEditor64.exe"
        ed.write_bytes(b"original")
        def changed(command, **kwargs):
            ed.write_bytes(b"different")
            return subprocess.CompletedProcess(command,0)
        def throws(command, **kwargs):
            raise OSError(str(self.root/"private-machine"))
        with patch("source_build.sys.platform","win32"), patch("source_build.file_product_version",return_value=("5.0.0.6308","5.0.0.6308")):
            with patch("source_build.subprocess.run", side_effect=changed) as fake:
                report=compile_sources(self.archive,self.commit,sha(self.archive),ed,self.root/"changed")
                self.assertEqual(len(list((self.root/"changed").glob("*.ex5"))),0)
                self.assertEqual(fake.call_count,1)
                self.assertTrue(all(x["reason"]=="compiler_provenance_changed_or_unreadable" for x in report["targets"]))
            with patch("source_build.subprocess.run", side_effect=throws):
                report=compile_sources(self.archive,self.commit,sha(self.archive),ed,self.root/"throws")
                self.assertNotIn(str(self.root), json.dumps(report))
                self.assertTrue(all(x["reason"]=="compiler_process_failed" for x in report["targets"]))
        with patch("source_build.sys.platform","win32"), patch("source_build.file_product_version", side_effect=ValueError("Unavailable")):
            with self.assertRaises(ValueError):
                compile_sources(self.archive,self.commit,sha(self.archive),ed,self.root/"unavailable")
            self.assertFalse((self.root/"unavailable").exists())

if __name__=="__main__":
    unittest.main()

