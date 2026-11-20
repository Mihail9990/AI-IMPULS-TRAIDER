"""Run actual MQL staging and fold with host-compatible wire fixtures."""
from pathlib import Path
import os, subprocess, tempfile, re, shutil
HERE=Path(__file__).resolve().parent
CYCLE=Path(os.getenv("RECOVERY_CYCLE_DIR", str(HERE.parent/"cycle")))
if not CYCLE.exists():
    CYCLE=Path("/workspace/shared/recovery-actual-integrated/cycle")
header=next(path for path in (HERE/"RecoveryManualPendingFill.mqh",
    Path("/workspace/shared/recovery-actual-integrated/execution/RecoveryManualPendingFill.mqh")) if path.exists()).read_text()
terms=header[header.index("bool RecoveryManualPendingFillTerms"):header.index("bool RecoveryManualPendingFillBegin")]
fold=header[header.index("bool RecoveryManualPendingFillFold"):header.index("RecoveryManualPendingFillResult RecoveryManualPendingFillStop")].replace("string f[];", "std::vector<string> f;")
stage=header[header.index("bool RecoveryManualPendingOpening"):header.index("// Private ordinary owner projection")]
fold=re.sub(r'\(string\)(p(?:\.[a-zA-Z_]+)+)',r'std::to_string(\1)',fold)
cp=(CYCLE/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0].replace(
    '#include "RecoveryCloseWire.mqh"', '#include "RecoveryCycle.mqh"')
cp=cp[:cp.index("bool RCCPStageFromWire")]+cp[cp.index("void RCCPClearPlan"):]
keys=(CYCLE/"RecoveryCloseEvidence.mqh").read_text()
cp=cp.replace('#include "RecoveryCycle.mqh"', '#include "RecoveryCycle.mqh"\n'+
    keys[keys.index("struct RCCloseEvidenceKey"):keys.index("struct RCExitHistory")]+
    keys[keys.index("bool RCCloseKeysEqual"):keys.index("bool RCExitSame")],1)
confirm=header[header.index("bool RecoveryManualPendingConfirmPair"):header.rindex("#endif")]
confirm=confirm.replace("string f[];", "std::vector<string> f;").replace(
    "(string)(pair.reverse.side==1?ORDER_TYPE_BUY_STOP:ORDER_TYPE_SELL_STOP)",
    "std::to_string(pair.reverse.side==1?ORDER_TYPE_BUY_STOP:ORDER_TYPE_SELL_STOP)")
confirm=re.sub(r'\(string\)((?:[a-zA-Z_]+)(?:\.[a-zA-Z_]+)*)',r'std::to_string(\1)',confirm)
fixture=(HERE/"test_pending_fill.cpp").read_text().replace("//ACTUAL_SEAM", terms+fold+stage+confirm)
with tempfile.TemporaryDirectory() as d:
    src=Path(d)/"test.cpp"
    exe=src.with_suffix("")
    src.write_text(fixture)
    for name in ("RecoveryCycle.mqh", "RecoveryRangeCoordinator.mqh", "RecoveryOpeningPair.mqh",
                 "RecoveryAlternatingFill.mqh"):
        shutil.copyfile(CYCLE/name,Path(d)/name)
    (Path(d)/"RecoveryCloseCheckpoint.mqh").write_text(cp+"\n#endif\n")
    subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                    "-fsanitize=undefined","-fno-sanitize-recover=all","-I",str(CYCLE),str(src),"-o",str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
