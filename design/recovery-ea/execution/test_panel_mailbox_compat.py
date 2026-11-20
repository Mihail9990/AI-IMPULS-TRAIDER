"""Run the mailbox against a fake terminal file sandbox, no terminal or orders."""
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
class PanelMailboxCompat(unittest.TestCase):
    def test_rotating_poll_and_bound_receipts(self):
        source=(HERE/"RecoveryPanelMailbox.mqh").read_text()
        source="\n".join(x for x in source.splitlines() if not x.startswith("#include"))
        source=source.replace("uchar bytes[];","std::vector<uchar> bytes;")
        fixture=(HERE/"test_panel_mailbox.cpp").read_text().replace(
            '#include "MailboxUnderTest"',source)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"mailbox.cpp"; exe=path.with_suffix("")
            path.write_text(fixture)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined","-fno-sanitize-recover=all",str(path),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":
    unittest.main()
