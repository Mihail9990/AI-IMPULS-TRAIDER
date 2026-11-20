"""Host test of MQL ordinary RECOVERY strategy+accounting split; not a terminal test."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class AlternatingFillCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_exact_fill_advances_independently_of_close(self):
        # Pure replay functions are compiled unchanged. The wire/effect readers
        # are covered by their own compat tests and are not called by this one.
        cp = (HERE / "RecoveryCloseCheckpoint.mqh").read_text()
        cp = cp.split("// Canonical snapshot",1)[0].replace(
            '#include "RecoveryCloseWire.mqh"', '#include "RecoveryCycle.mqh"')
        cp = cp[:cp.index('bool RCCPStageFromWire')]+cp[cp.index('void RCCPClearPlan'):]
        keys = (HERE / "RecoveryCloseEvidence.mqh").read_text()
        start = keys.index('struct RCCloseEvidenceKey')
        end = keys.index('struct RCExitHistory')
        match_start = keys.index('bool RCCloseKeysEqual')
        match_end = keys.index('bool RCExitSame')
        cp = cp.replace('#include "RecoveryCycle.mqh"', '#include "RecoveryCycle.mqh"\n'+
                        keys[start:end]+keys[match_start:match_end],1)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ('RecoveryCycle.mqh','RecoveryRangeCoordinator.mqh','RecoveryOpeningPair.mqh','RecoveryAlternatingFill.mqh','test_alternating_fill.cpp'):
                shutil.copy2(HERE / name,tmp / name)
            (tmp / 'RecoveryCloseCheckpoint.mqh').write_text(cp+'\n#endif\n')
            exe = tmp / 'cycle'
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(tmp / 'test_alternating_fill.cpp'),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)


if __name__=="__main__":
    unittest.main()
