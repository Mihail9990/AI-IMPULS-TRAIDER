"""Host-test support gap ownership and no-send eligibility with fake terminal."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
HERE=Path(__file__).resolve().parent

class SupportGapOwnerCompat(unittest.TestCase):
    def test_no_proof_no_effect_and_single_owned_gap(self):
        header=(HERE/"RecoverySupportGapOwner.mqh").read_text()
        header="\n".join(line for line in header.splitlines() if not line.startswith("#include"))
        header=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",header)
        fixture=(HERE/"test_support_gap_owner.cpp").read_text().replace('#include "GapOwnerUnderTest"',header)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"shim.cpp"; exe=path.with_suffix("")
            path.write_text(fixture)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined","-fno-sanitize-recover=all",str(path),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":
    unittest.main()
