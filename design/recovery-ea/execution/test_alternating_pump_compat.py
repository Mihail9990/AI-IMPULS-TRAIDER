"""Host-test the MQL pump with fake verifier transport; no terminal calls."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RECOVERY_SOURCE_ROOT", HERE.parent))

class AlternatingPumpCompat(unittest.TestCase):
    def test_correlated_chain_no_duplicate_io(self):
        source = (HERE/"RecoveryAlternatingPump.mqh").read_text()
        evidence = (ROOT/"execution"/"RecoveryAlternatingEvidence.mqh").read_text()
        scopes = evidence[evidence.index("struct RecoveryAlternatingEvidenceBinding"):evidence.index("bool RecoveryAlternatingOrderTerms")]
        code = scopes + source
        code = "\n".join(line for line in code.splitlines() if not line.startswith("#include"))
        code = re.sub(r"\(string\)([\w.]+)", r"std::to_string(\1)", code)
        code = code.replace("string f[];", "std::vector<string> f;")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            shim = (HERE/"test_alternating_pump.cpp").read_text()
            (path/"fake.cpp").write_text(shim.replace('#include "PumpUnderTest"', code))
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(path/"fake.cpp"),"-o",str(path/"test")],check=True)
            subprocess.run([str(path/"test")],check=True)

if __name__ == "__main__":
    unittest.main()
