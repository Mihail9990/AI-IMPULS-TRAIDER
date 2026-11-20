import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from source_build import (BASE, DIAGNOSTIC, compile_sources, package, read_package,
                          result_counts, validated_path)


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
        self.source(DIAGNOSTIC[1],'#include "../verifier/part.mqh"\n')
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
        self.assertEqual(set(files),{*DIAGNOSTIC,"verifier/part.mqh"})
        self.assertIn(b"Git-committed",files["verifier/part.mqh"])
        with self.assertRaisesRegex(ValueError,"ZIP SHA"):
            read_package(self.archive,self.commit,"f"*64)
        with self.assertRaises(FileExistsError):
            package(self.repo,self.commit,other)

    def test_candidate_path_requires_different_commit(self):
        trader="runtime/RecoveryTrader.mq5"
        self.source(trader,'#include "../verifier/part.mqh"\n')
        with self.assertRaises(ValueError):
            package(self.repo,self.commit,self.archive,trader)
        commit=self.checkpoint()
        self.assertEqual(package(self.repo,commit,self.archive,trader)["mode"],"candidate")

    def test_escaped_include_and_reserved_path_rejected(self):
        self.source(DIAGNOSTIC[0],'#include "../../../other.mqh"\n')
        with self.assertRaises(ValueError):
            package(self.repo,self.checkpoint(),self.archive)
        for invalid in ("execution/COM1.mq5","aux/t.mqh","x/../a.mq5"):
            with self.assertRaises(ValueError):
                validated_path(invalid)

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
            good=path.name=="RecoveryVerifier.mq5"
            path.with_suffix(".log").write_text(f"Result: {0 if good else 1} errors, 0 warnings",encoding="utf-16")
            if good:
                path.with_suffix(".ex5").write_bytes(b"fake-ex5")
            return subprocess.CompletedProcess(command,1)
        with patch("source_build.sys.platform","win32"), patch("source_build.subprocess.run",side_effect=fake):
            output=self.root/"out"
            report=compile_sources(self.archive,self.commit,sha(self.archive),ed,output)
            self.assertEqual([r["status"] for r in report["targets"]],["compiled","failed"])
            self.assertEqual(len(list(output.glob("*.ex5"))),1)
            self.assertEqual(len(list(output.glob("*.log"))),2)
            self.assertEqual(json.loads((output/"compile-report.json").read_text())["status"],"compile_only_not_deployment")

if __name__=="__main__":
    unittest.main()
