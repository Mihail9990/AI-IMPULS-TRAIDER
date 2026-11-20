"""Use actual coordinator and intent key derivation with an inert fake dispatcher."""
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RECOVERY_SOURCE_ROOT", HERE.parent))

DISPATCH = r"""
enum RecoveryDispatchState {RD_NOT_SENT,RD_EXISTING_INTENT,RD_RESERVED_NOT_SENT,
                            RD_OUTCOME_UNRESOLVED,RD_TICKET_RECORDED};
struct RecoveryDispatchResult {RecoveryDispatchState state;ulong order;};
const RecoveryPanelControl *witness=nullptr;
int sends=0;
RecoveryDispatchState next_result=RD_TICKET_RECORDED;
RecoveryDispatchState RecoveryDispatchOnce(const RRRangeState &r,const RRAction &a,
          const RecoveryIntentKey &key,double,double,RecoveryDispatchResult &out){
 assert(witness!=nullptr&& &r==&witness->ranges[a.range_id-1] && RRActionEqual(r.pending,a));
 assert(r.phase==RR_NEW&&a.kind==RR_PRIMARY&&a.serial==1&&RecoveryIntentContextOK(key));
 ++sends;
 // Even a spurious numeric 'order' must not escape an unresolved result.
 out.state=next_result;out.order=9007199254740993ULL;
 return out.state;
}
"""
TEST = r"""
#include "RecoveryPrimarySubmit.mqh"
int main() {
 const auto cfg=config();
 RecoverySessionContext live{7,12,18,"server","XAUUSD.s","settings"};
 const string epoch(32,'a');
 RecoverySessionLifecycle lifecycle{};
 assert(RecoverySessionInit(lifecycle,live,true,epoch)==RSL_NEW);
 RecoverySessionIntentScope scope{epoch,12,18,"server","XAUUSD.s",cfg.scope,cfg.generation};
 RecoveryControlCommand command{REC_CONTROL_CONFIGURE,2,{{1,1,400000},{2,-1,399800}}};
 RecoveryPanelControl panel{};
 RecoveryPrimarySubmitResult out{};
 out.ticket=77;
 witness=&panel;
 assert(!RecoverySubmitPanelPrimary(panel,1,lifecycle,live,scope,.01,.01,out));
 assert(out.ticket==77&&sends==0);
 assert(RecoveryApplyPanelControl(panel,command,cfg)==REC_CONTROL_APPLIED);
 auto submit=[&](int id,const RecoverySessionIntentScope& s,double tick=.01){
   return RecoverySubmitPanelPrimary(panel,id,lifecycle,live,s,tick,.01,out);
 };
 assert(!submit(0,scope)&&!submit(3,scope));
 auto invalid=scope;invalid.scope++;
 assert(!submit(1,invalid));invalid=scope;invalid.generation++;
 assert(!submit(1,invalid));invalid=scope;invalid.executor_epoch=string(32,'f');
 assert(!submit(1,invalid));invalid=scope;invalid.magic++;
 assert(!submit(1,invalid));assert(!submit(1,scope,0));
 login=20;assert(!submit(1,scope));login=12;
 usable_hash=false;assert(!submit(1,scope));usable_hash=true;
 panel.ranges[0].cycle.generation++;
 assert(!submit(1,scope));panel.ranges[0].cycle.generation--;
 assert(sends==0&&out.ticket==77&&panel.ranges[0].action_serial==0);

 assert(submit(1,scope)&&sends==1&&out.ticket==9007199254740993ULL);
 assert(out.action.side==1&&out.action.entry_ticks==400000&&out.action.sl_ticks==399920);
 assert(out.action.volume_steps==cfg.lot_steps[0]&&out.key.magic==scope.magic);
 const auto known=out;
 assert(!submit(1,scope)&&sends==1&&out.ticket==known.ticket);
 assert(panel.ranges[0].phase==RR_NEW&&panel.ranges[0].entry.ticket==0);
 // Even if some later code erroneously clears the pending marker, the
 // session's action serial prevents an uncommanded replacement primary.
 RRClearAction(panel.ranges[0].pending);
 assert(!submit(1,scope)&&sends==1);

 next_result=RD_OUTCOME_UNRESOLVED;
 assert(submit(2,scope)&&sends==2&&out.ticket==0&&out.receipt.state==RD_OUTCOME_UNRESOLVED);
 assert(out.action.side==-1&&out.action.entry_ticks==399800&&out.action.sl_ticks==399880);
 assert(out.key.id!=known.key.id);
 assert(!submit(2,scope)&&sends==2&&out.ticket==0);
 assert(!RecoverySubmitPanelPrimary(panel,2,{},live,scope,.01,.01,out));
}
"""

class PrimarySubmitCompat(unittest.TestCase):
    def test_configured_stops_one_attempt_each(self):
        cycle = ROOT / "cycle"
        execution = ROOT / "execution"
        shim = runpy.run_path(str(execution / "test_session_intent_compat.py"))["SHIM"]
        shim = re.sub(r"^(?:enum RecoveryPairSendState|struct RecoveryDispatchResult).*\n",
                      "", shim.split("int sends=", 1)[0], flags=re.M)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base/"cycle").mkdir()
            (base/"execution").mkdir()
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
            shutil.copy2(HERE/"RecoveryPrimarySubmit.mqh",base/"execution/RecoveryPrimarySubmit.mqh")
            key=(execution/"RecoverySessionIntentKey.mqh").read_text().replace('#include "RecoveryIntentJournal.mqh"', '')
            key=key.replace("uchar bytes[],unused[],digest[];","std::vector<uchar> bytes,unused,digest;")
            key=re.sub(r"\(string\)((?:scope|action)\.\w+)",r"std::to_string(\1)",key)
            (base/"execution/RecoverySessionIntentKey.mqh").write_text(key)
            (base/"execution/RecoveryDispatch.mqh").write_text(DISPATCH)
            fixture=(cycle/"test_alternating_fill.cpp").read_text().split("static RRRangeState first(")[0]
            fixture=fixture.replace('"RecoveryAlternatingFill.mqh"','"../cycle/RecoveryAlternatingFill.mqh"')
            source=base/"execution/check.cpp"
            source.write_text(shim+fixture.replace("constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();", "")+TEST)
            exe=base/"check"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(source),"-lcrypto","-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
