"""Strict C++/UBSan test for the native dual evidence pump via fake transport."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
HERE=Path(__file__).resolve().parent

class DualSupportCompat(unittest.TestCase):
    def test_proved_winner_and_survivor_or_deterministic_collision(self):
        header=(HERE/"RecoveryDualSupportPump.mqh").read_text()
        header="\n".join(x for x in header.splitlines() if not x.startswith("#include"))
        header=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",header)
        header=re.sub(r"string (fields|f)\[\];",r"std::vector<string> \1;",header)
        fixture=(HERE/"test_dual_support.cpp").read_text().replace('#include "DualSupportUnderTest"',header)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"test.cpp"; binary=path.with_suffix("")
            path.write_text(fixture)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            "-fno-sanitize-recover=all",str(path),"-o",str(binary)],check=True)
            subprocess.run([str(binary)],check=True)
if __name__=="__main__":
    unittest.main()
