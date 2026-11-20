"""Test one-shot working reverse confirmation with fake terminal transport."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
class WorkingReverseCompat(unittest.TestCase):
    def test_exact_key_ticket_and_independent_placed_proof(self):
        source=(HERE/"RecoveryWorkingReverse.mqh").read_text()
        source="\n".join(line for line in source.splitlines() if not line.startswith("#include"))
        source=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",source)
        fixture=(HERE/"test_working_reverse.cpp").read_text().replace(
            '#include "WorkingReverseUnderTest"',source)
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/"test.cpp"; exe=src.with_suffix("")
            src.write_text(fixture)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined","-fno-sanitize-recover=all",str(src),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":
    unittest.main()
