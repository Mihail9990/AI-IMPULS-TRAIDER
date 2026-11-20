"""Use real range staging+intent-key with an inert dispatch and verifier shim."""
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))

class OcoCancelCompat(unittest.TestCase):
    def test_one_typed_cancel_and_independent_confirmation(self):
        cycle=ROOT/"cycle"; execution=ROOT/"execution"
        shim=runpy.run_path(str(execution/"test_session_intent_compat.py"))["SHIM"]
        shim=re.sub(r"^(?:enum RecoveryPairSendState|struct RecoveryDispatchResult).*\n",
                    "",shim.split("int sends=",1)[0],flags=re.M)
        header=(HERE/"RecoveryOcoCancel.mqh").read_text()
        header="\n".join(l for l in header.splitlines() if not l.startswith("#include"))
        header=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",header)
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            (base/"cycle").mkdir();(base/"execution").mkdir()
            for name in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                shutil.copy2(cycle/name,base/"cycle"/name)
            cp=(cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
            cp=cp.replace('#include "RecoveryCloseWire.mqh"','#include "RecoveryCycle.mqh"')
            cp=cp[:cp.index("bool RCCPStageFromWire")]+cp[cp.index("void RCCPClearPlan"):]
            e=(cycle/"RecoveryCloseEvidence.mqh").read_text()
            cp=cp.replace('#include "RecoveryCycle.mqh"','#include "RecoveryCycle.mqh"\n'+
                e[e.index("struct RCCloseEvidenceKey"):e.index("struct RCExitHistory")]+
                e[e.index("bool RCCloseKeysEqual"):e.index("bool RCExitSame")],1)
            (base/"cycle/RecoveryCloseCheckpoint.mqh").write_text(cp+"\n#endif\n")
            for name in ("RecoverySessionLifecycle.mqh","RecoveryPanelControl.mqh"):
                shutil.copy2(execution/name,base/"execution"/name)
            key=(execution/"RecoverySessionIntentKey.mqh").read_text().replace('#include "RecoveryIntentJournal.mqh"', '')
            key=key.replace("uchar bytes[],unused[],digest[];","std::vector<uchar> bytes,unused,digest;")
            key=re.sub(r"\(string\)((?:scope|action)\.\w+)",r"std::to_string(\1)",key)
            (base/"execution/RecoverySessionIntentKey.mqh").write_text(key)
            fixture=(cycle/"test_alternating_fill.cpp").read_text().split("static RRRangeState first(")[0]
            fixture=fixture.replace('"RecoveryAlternatingFill.mqh"','"../cycle/RecoveryAlternatingFill.mqh"')
            source=base/"execution/check.cpp"
            test=(HERE/"test_oco_cancel.cpp").read_text().replace('#include "OcoCancelUnderTest"',header)
            source.write_text(shim+fixture.replace("constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();","")+test)
            exe=base/"check"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            "-fno-sanitize-recover=all",str(source),"-lcrypto","-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
