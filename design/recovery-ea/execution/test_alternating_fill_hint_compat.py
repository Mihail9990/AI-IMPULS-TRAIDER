"""Exercise the exact-ticket event hint with a fake pump and broker-free context."""
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
class FillHintCompat(unittest.TestCase):
    def test_hint_is_once_and_never_calls_transport(self):
        source=(HERE/"RecoveryAlternatingFillHint.mqh").read_text()
        source="\n".join(x for x in source.splitlines() if not x.startswith("#include"))
        fixture=(HERE/"test_alternating_fill_hint.cpp").read_text().replace(
            '#include "FillHintUnderTest"',source)
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/"test.cpp"; exe=src.with_suffix("")
            src.write_text(fixture)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(src),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":
    unittest.main()
