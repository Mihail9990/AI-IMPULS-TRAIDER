"""Host fake-reader tests of the native exact-ticket scheduling fallback."""
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent

class AlternatingFillFallbackTest(unittest.TestCase):
    def test_only_exact_history_can_queue_and_no_duplicate_read(self):
        header=(HERE/"RecoveryAlternatingFillFallback.mqh").read_text()
        header="\n".join(line for line in header.splitlines() if not line.startswith("#include"))
        fixture=(HERE/"test_alternating_fill_fallback.cpp").read_text().replace(
            '#include "FallbackUnderTest"',header)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"test.cpp"
            path.write_text(fixture)
            binary=path.with_suffix("")
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(path),"-o",str(binary)],check=True)
            subprocess.run([str(binary)],check=True)
if __name__=="__main__":
    unittest.main()
