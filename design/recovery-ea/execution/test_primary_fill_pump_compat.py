"""Host-test primary fill scheduling against a fake terminal and file transport."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
class PrimaryFillPumpCompat(unittest.TestCase):
    def test_known_ticket_and_single_independent_proof(self):
        source=(HERE/"RecoveryPrimaryFillPump.mqh").read_text()
        source="\n".join(line for line in source.splitlines() if not line.startswith("#include"))
        source=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",source)
        source=source.replace("string parts[];","std::vector<string> parts;")
        fixture=(HERE/"test_primary_fill_pump.cpp").read_text().replace(
            '#include "PrimaryPumpUnderTest"',source)
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/"shim.cpp"; exe=src.with_suffix("")
            src.write_text(fixture)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined","-fno-sanitize-recover=all",str(src),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
