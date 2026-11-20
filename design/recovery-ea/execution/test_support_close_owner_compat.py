"""Actual support-close staged action and exact working report fold; no broker."""
from pathlib import Path
import os
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = Path(os.getenv("RECOVERY_SOURCE_ROOT", HERE.parent))

TYPES = r"""
#include <sstream>
#include "RecoverySessionLifecycle.mqh"
#include "RecoverySessionIntentKey.mqh"
#include "RecoverySupportGap.mqh"
struct RecoveryInventoryScope {
 string nonce,executor_epoch,verifier_epoch;long generation,login;string server,symbol;
 long magic,verifier_chart;
};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
struct RecoveryExactReport {bool usable,observed;string reason,source;long sequence;string position_identifier,row;};
struct RecoveryEvidenceOwner {
 RecoverySessionContext context;string executor_epoch,verifier_epoch;long verifier_chart;ulong serial;
};
enum RecoveryRangeOwnerPhase {RRO_UNUSED,RRO_PRIMARY_PENDING,RRO_ORDINARY,RRO_FINAL};
struct RecoveryRangeOwner {
 RecoveryRangeOwnerPhase phase;int range_id;string executor_epoch;
 ulong scope,generation,revision;
 RROpeningPair opening;RRAlternationLedger ledger;
};
struct RecoveryPanelControl {int mode;bool configured[2];RRSettings settings;RRRangeState ranges[2];};
const int REC_CONTROL_RANGES=3,ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,
          ORDER_STATE_PLACED=1,ORDER_STATE_CANCELED=2;
bool RecoveryInvScopeOK(const RecoveryInventoryScope& s) {
 return RecoveryInvHex(s.nonce)&&RecoveryInvHex(s.executor_epoch)&&RecoveryInvHex(s.verifier_epoch)&&
        s.login>0&&s.magic>0&&s.generation>0&&s.verifier_chart>0;
}
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope& a,const RecoveryInventoryScope& b) {
 return a.executor_epoch==b.executor_epoch&&a.verifier_epoch==b.verifier_epoch&&a.verifier_chart==b.verifier_chart&&
        a.login==b.login&&a.magic==b.magic&&a.server==b.server&&a.symbol==b.symbol&&a.generation==b.generation;
}
bool RecoveryPrimarySession(const RecoverySessionLifecycle& life,const RecoverySessionContext& live,
 const RecoverySessionIntentScope& scope) {
 return RecoverySessionCurrent(life,live)&&scope.executor_epoch==life.epoch&&scope.login==live.login&&
        scope.magic==live.magic&&scope.server==live.server&&scope.symbol==live.symbol;
}
string RecoveryOwnerNonce(const string& lane,const string& identity) {
 return RecoverySessionSha256(lane+"\t"+identity).substr(0,32);
}
int StringSplit(const string& s,int ch,std::vector<string>& out) {
 out.clear();std::stringstream r(s);string field;
 while(std::getline(r,field,static_cast<char>(ch)))out.push_back(field);
 return out.size();
}
bool RecoveryAdoptionUnsigned(const string& s,ulong& n) {
 if(s.empty()||s.find_first_not_of("0123456789")!=string::npos)return false;
 try {n=std::stoull(s);return true;}catch(...){return false;}
}
bool RecoveryAdoptionUnits(const string& s,double unit,bool allow_zero,long& n) {
 try {const auto units=std::stod(s)/unit;n=std::lround(units);
  return n>=0&&(allow_zero||n>0)&&std::abs(units-n)<.00001;
 }catch(...){return false;}
}
RecoveryExactReportScope requested{};
RecoveryExactReport answer{};
bool published=false,has_answer=false,allow_publish=true;
int publications=0;
bool RecoveryExactReportRequest(const RecoveryExactReportScope& b,const string& tag){
 assert(RecoveryInvHex(tag)&&tag!=b.binding.nonce);
 ++publications;published=allow_publish;
 if(published)requested=b;
 return published;
}
bool RecoveryExactReportRead(const RecoveryExactReportScope& b,RecoveryExactReport& out){
 if(!has_answer||!published||b.binding.nonce!=requested.binding.nonce||b.order_ticket!=requested.order_ticket)
  return false;
 out=answer;return true;
}
"""

class SupportCloseOwnerCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_verified_support_closes_once_and_stages_only_same_side(self):
        cycle, execution = ROOT / "cycle", ROOT / "execution"
        shim = runpy.run_path(str(execution / "test_session_intent_compat.py"))["SHIM"].split("int sends=", 1)[0]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = root / "cycle"
            q = root / "execution"
            c.mkdir()
            q.mkdir()
            for fn in ("RecoveryCycle.mqh", "RecoveryRangeCoordinator.mqh", "RecoveryOpeningPair.mqh",
                       "RecoveryAlternatingFill.mqh", "RecoverySupportGap.mqh"):
                shutil.copy2(cycle / fn, c)
            cp = (cycle / "RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot", 1)[0]
            cp = cp.replace('#include "RecoveryCloseWire.mqh"', '#include "RecoveryCycle.mqh"')
            cp = cp[:cp.index("bool RCCPStageFromWire")] + cp[cp.index("void RCCPClearPlan"):]
            evidence = (cycle / "RecoveryCloseEvidence.mqh").read_text()
            cp = cp.replace('#include "RecoveryCycle.mqh"', '#include "RecoveryCycle.mqh"\n' +
                evidence[evidence.index("struct RCCloseEvidenceKey"):evidence.index("struct RCExitHistory")] +
                evidence[evidence.index("bool RCCloseKeysEqual"):evidence.index("bool RCExitSame")], 1)
            (c / "RecoveryCloseCheckpoint.mqh").write_text(cp + "\n#endif\n")
            shutil.copy2(execution / "RecoverySessionLifecycle.mqh", q)
            key = (execution / "RecoverySessionIntentKey.mqh").read_text()
            key = key.replace('#include "RecoveryIntentJournal.mqh"', "")
            key = key.replace("uchar bytes[],unused[],digest[];", "std::vector<uchar> bytes,unused,digest;")
            key = re.sub(r"\(string\)((?:scope|action)\.\w+)", r"std::to_string(\1)", key)
            (q / "RecoverySessionIntentKey.mqh").write_text(key)
            bound = (execution / "RecoveryRangeOwner.mqh").read_text()
            bound = bound[bound.index("bool RecoveryRangeOwnerBound("):bound.index("bool RecoveryRangeOwnerSubmit(")]
            fold = (execution / "RecoveryExactOrderEffect.mqh").read_text()
            binder = fold[fold.index("struct RecoveryExactEffectBinding"):fold.index("bool RecoveryExactEffectBound")]
            fold = fold[fold.index("bool RecoveryExactEffectFold"):fold.index("bool RecoveryBuildExactOrderEffect")]
            fold = fold.replace("string f[];", "std::vector<string> f;").replace(
                "(string)binding.request.binding.magic", "std::to_string(binding.request.binding.magic)")
            source = (HERE / "RecoverySupportCloseOwner.mqh").read_text()
            source = "\n".join(l for l in source.splitlines() if not l.startswith("#include"))
            source = re.sub(r"\(string\)([\w.]+)", r"std::to_string(\1)", source)
            fixture = (cycle / "test_alternating_fill.cpp").read_text().split("static RRFilled ", 1)[0]
            # Keep the host header and baseline config/first entry helpers; no other fixtures.
            if "static RRAlternatingObservation" in fixture:
                fixture = fixture.split("static RRAlternatingObservation", 1)[0]
            fixture = fixture.replace('#include "RecoveryAlternatingFill.mqh"', '#include "../cycle/RecoveryAlternatingFill.mqh"')
            # Climits is included by the intent shim but the fixture uses an explicit unsigned type.
            fixture = fixture.replace("constexpr auto ULONG_MAX", "#undef ULONG_MAX\nconstexpr auto ULONG_MAX")
            src = q / "test.cpp"
            cpp = (HERE / "test_support_close_owner.cpp").read_text()
            src.write_text(shim + fixture + TYPES.replace('"RecoverySupportGap.mqh"', '"../cycle/RecoverySupportGap.mqh"')
                           + bound + binder + fold + source + "\n" + cpp)
            exe = q / "test"
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all", str(src), "-lcrypto",
                            "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)

if __name__ == "__main__":
    unittest.main()
