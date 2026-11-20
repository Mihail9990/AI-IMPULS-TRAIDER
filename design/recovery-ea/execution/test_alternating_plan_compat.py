"""Offline C++ compatibility for the MQL plan, with a fake dispatch seam."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RECOVERY_SOURCE_ROOT", HERE.parent))
FAKE = """
enum RecoveryPairSendState { RPS_NOT_STARTED, RPS_FILL_NOT_BOUND, RPS_REVERSE_NOT_SENT, RPS_AWAIT_VERIFIER };
struct RecoveryIntentKey { ulong generation, simulated_id; };
struct RecoveryDispatchResult { int status; };
template<class T> void ZeroMemory(T &out) { out = T{}; }
static ulong journal=0;
static int attempts=0;
void ResetDispatchFixture() { journal=0; attempts=0; }
int DispatchFixtureAttempts() { return attempts; }
RecoveryPairSendState RecoveryContinueOpeningPair(const RRRangeState &range,
    const RROpeningPair &pair,const RecoveryIntentKey &key,double,double,RecoveryDispatchResult &) {
  assert(key.generation==pair.reverse.generation && RROpeningBound(range,pair));
  if(journal==key.simulated_id)return RPS_REVERSE_NOT_SENT;
  journal=key.simulated_id;++attempts;
  return RPS_AWAIT_VERIFIER;
}
"""

class AlternatingPlanCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_preview_readback_and_duplicate(self):
        cycle = ROOT / "cycle"
        cp = (cycle / "RecoveryCloseCheckpoint.mqh").read_text()
        cp = cp.split("// Canonical snapshot", 1)[0].replace(
            '#include "RecoveryCloseWire.mqh"', '#include "RecoveryCycle.mqh"')
        cp = cp[:cp.index('bool RCCPStageFromWire')]+cp[cp.index('void RCCPClearPlan'):]
        keys = (cycle / "RecoveryCloseEvidence.mqh").read_text()
        cp = cp.replace('#include "RecoveryCycle.mqh"', '#include "RecoveryCycle.mqh"\n'+
                        keys[keys.index('struct RCCloseEvidenceKey'):keys.index('struct RCExitHistory')]+
                        keys[keys.index('bool RCCloseKeysEqual'):keys.index('bool RCExitSame')],1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cycle").mkdir()
            (root / "execution").mkdir()
            for name in ('RecoveryCycle.mqh','RecoveryRangeCoordinator.mqh','RecoveryOpeningPair.mqh','RecoveryAlternatingFill.mqh'):
                shutil.copy2(cycle / name, root / "cycle" / name)
            (root / 'cycle' / 'RecoveryCloseCheckpoint.mqh').write_text(cp+'\n#endif\n')
            for name in ("RecoveryAlternatingPlan.mqh", "RecoveryAlternatingDispatch.mqh"):
                shutil.copy2(HERE / name, root / "execution" / name)
            (root / "execution" / "RecoveryOpeningDispatch.mqh").write_text(FAKE)
            fixture = (cycle / "test_alternating_fill.cpp").read_text().split("static RCCloseCheckpoint closure(")[0]
            fixture = fixture.replace('"RecoveryAlternatingFill.mqh"', '"../cycle/RecoveryAlternatingFill.mqh"')
            cpp = root / 'execution' / 'test.cpp'
            cpp.write_text(fixture + (HERE / 'test_alternating_plan.cpp').read_text())
            exe = root / "check"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
