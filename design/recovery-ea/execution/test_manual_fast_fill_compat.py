"""Chart-side manual flow with real parsers and a fake terminal."""
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))
sys.path.insert(0,str(ROOT/"verifier"))
from test_reader_compat import SHIM,as_cpp

class ManualFastFillCompat(unittest.TestCase):
    def test_exact_fill_before_manual_adoption_confirmation(self):
        cycle,verifier=ROOT/"cycle",ROOT/"verifier"
        checkpoint=(cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
        checkpoint=checkpoint[:checkpoint.index("bool RCCPStageFromWire")]+checkpoint[checkpoint.index("void RCCPClearPlan"):]
        keys=(cycle/"RecoveryCloseEvidence.mqh").read_text()
        checkpoint=checkpoint.replace('#include "RecoveryCloseWire.mqh"','#include "RecoveryCycle.mqh"\n'+
            keys[keys.index("struct RCCloseEvidenceKey"):keys.index("struct RCExitHistory")]+
            keys[keys.index("bool RCCloseKeysEqual"):keys.index("bool RCExitSame")])
        hr=(verifier/"RecoveryHistoryReader.mqh").read_text().split("// Publisher tag",1)[0]+"\n#endif\n"
        gate=(ROOT/"execution/RecoveryAdoptionGate.mqh").read_text()
        alt=(ROOT/"execution/RecoveryAlternatingEvidence.mqh").read_text()
        matching=alt[alt.index("bool RecoveryAlternatingSameSession"):alt.index("bool RecoveryAlternatingWanted")]
        inventory=(ROOT/"execution/RecoveryAdoptionInventory.mqh").read_text()
        body=as_cpp((verifier/"RecoveryInventoryReader.mqh").read_text())+hr+gate+inventory+matching+(ROOT/"execution/RecoveryManualOwner.mqh").read_text()
        body="\n".join(line for line in body.splitlines() if not line.startswith("#include"))
        body=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",body)
        body=re.sub(r"string (fields|f|rows)\[\];",r"std::vector<string> \1;",body)
        body=body.replace("const string &p[]","const std::vector<string> &p")
        body=body.replace("deal_tickets[128], rows[];","deal_tickets[128]; std::vector<string> rows;")
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/"cycle").mkdir()
            (root/"execution").mkdir()
            for name in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                (root/"cycle"/name).write_bytes((cycle/name).read_bytes())
            for name in ("RecoveryPanelControl.mqh","RecoverySessionLifecycle.mqh"):
                (root/"execution"/name).write_bytes((ROOT/"execution"/name).read_bytes())
            (root/"cycle/RecoveryCloseCheckpoint.mqh").write_text(checkpoint+"\n#endif\n")
            path=root/"manual.cpp"; exe=path.with_suffix("")
            baseline=runpy.run_path(str(ROOT/"execution/test_manual_owner_compat.py"))
            prefix=baseline["HARNESS"].replace('"RecoveryAlternatingFill.mqh"','"cycle/RecoveryAlternatingFill.mqh"')\
                         .replace('"RecoveryPanelControl.mqh"','"execution/RecoveryPanelControl.mqh"')\
                         .replace('"RecoverySessionLifecycle.mqh"','"execution/RecoverySessionLifecycle.mqh"')
            adapter=(ROOT/"execution/RecoveryManualChart.mqh").read_text()
            adapter="\n".join(l for l in adapter.splitlines() if not l.startswith("#include"))
            adapter=re.sub(r"\(string\)([\w.]+)", r"std::to_string(\1)", adapter)
            cpp=(ROOT/"execution/test_manual_chart.cpp").read_text()
            deps, test=cpp.split("// TEST AFTER ADAPTER",1)
            handoff=(ROOT/"execution/RecoveryManualHandoff.mqh").read_text()
            handoff="\n".join(l for l in handoff.splitlines() if not l.startswith("#include"))
            handoff=handoff.replace("string fields[];","std::vector<string> fields;")
            fast=(HERE/"RecoveryManualFastFill.mqh").read_text()
            fast="\n".join(l for l in fast.splitlines() if not l.startswith("#include"))
            fast=re.sub(r"\(string\)([\w.]+)", r"std::to_string(\1)", fast)
            bits=(HERE/"test_manual_fast_fill.cpp").read_text().split("// TEST AFTER FAST FILL",1)
            path.write_text(SHIM+prefix+body+baseline["TEST"].split("int main()",1)[0]+deps+adapter+"\n"+bits[0]+handoff+"\n"+fast+bits[1])
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            "-fno-sanitize-recover=all",str(path),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
