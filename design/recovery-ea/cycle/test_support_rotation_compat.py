"""Host test of MQL support reentry and remaining-stop rotation; not a terminal test."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class SupportRotationCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_both_support_paths_keep_same_remaining_ticket(self):
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
            for name in ('RecoveryCycle.mqh','RecoveryRangeCoordinator.mqh','RecoveryOpeningPair.mqh','RecoverySupportRotation.mqh','RecoveryAlternatingFill.mqh','RecoverySupportRotationLedger.mqh','test_support_rotation.cpp'):
                shutil.copy2(HERE / name,tmp / name)
            (tmp / 'RecoveryCloseCheckpoint.mqh').write_text(cp+'\n#endif\n')
            exe = tmp / 'cycle'
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(tmp / 'test_support_rotation.cpp'),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)


if __name__=="__main__":
    unittest.main()
