"""Exercise detached accounting with a broker-free verifier transport."""
from pathlib import Path
import subprocess
import tempfile
import unittest
HERE=Path(__file__).resolve().parent

class DetachedCloseAccountingCompat(unittest.TestCase):
    def test_later_owned_close_survives_blocker_and_successor_advancement(self):
        header=(HERE/"RecoveryDetachedCloseAccounting.mqh").read_text()
        header="\n".join(line for line in header.splitlines() if not line.startswith("#include"))
        fixture=(HERE/"test_detached_close_accounting.cpp").read_text().replace(
            '#include "DetachedAccountingUnderTest"',header)
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/"host.cpp"; exe=src.with_suffix("")
            src.write_text(fixture)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined","-fno-sanitize-recover=all",str(src),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
