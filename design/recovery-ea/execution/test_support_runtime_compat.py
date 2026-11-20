"""Test the scoped support scheduler with fake terminal and broker transport."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
class SupportRuntimeCompat(unittest.TestCase):
    def test_one_watch_one_support_and_delayed_confirmation(self):
        header=(HERE/"RecoverySupportRuntime.mqh").read_text()
        retry=(HERE/"RecoveryProofRetry.mqh").read_text()
        header=retry+"\n"+"\n".join(l for l in header.splitlines() if not l.startswith("#include"))
        header=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",header)
        source=(HERE/"test_support_runtime.cpp").read_text().replace(
            '#include "SupportRuntimeUnderTest"',header)
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/"test.cpp";exe=src.with_suffix("")
            src.write_text(source)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined","-fno-sanitize-recover=all",str(src),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
