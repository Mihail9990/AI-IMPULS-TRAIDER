"""Drive the final-rung submit/read controller with a simulated verifier and broker."""
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SHIM = r"""
#include <cassert>
#include <climits>
#include <string>
using string=std::string;
using ulong=unsigned long;
template<class T> void ZeroMemory(T& v){v=T{};}
enum {RR_NONE,RR_ARM_REENTRY,RR_REENTRY,RR_ACTIVE,RC_LIMIT=24,RI_TICKET,RD_TICKET_RECORDED};
enum {RPP_UNUSED,RPP_WAIT_EXACT,RPP_WAIT_HISTORY,RPP_WAIT_INVENTORY,RPP_UNRESOLVED,RPP_DONE};
enum RecoveryPrimaryPumpResult {RPP_WAIT,RPP_PROOF};
struct RRAction {int kind;};
struct RRRangeState {int phase,reentry_slot;struct {ulong ticket;} opposite;RRAction pending;};
struct RRSettings {};
struct RecoveryRangeOwner {ulong revision;int ledger;};
struct RecoverySessionIntentScope {string generation;ulong scope;};
struct RecoveryInventoryScope {string nonce;};
struct RecoveryEvidenceOwner {ulong serial;};
struct RecoveryIntentKey {string id;};
struct RecoveryDispatchResult {ulong order;};
struct RecoveryPrimaryFillBinding {
 struct {RecoveryInventoryScope binding;ulong order_ticket;} exact;
 struct {RecoveryInventoryScope binding;ulong anchor_order;} history;
 RecoveryInventoryScope inventory;ulong scope,verify_after;RRAction routed;double price_tick,volume_step;
};
struct RecoveryPrimaryFillPump {int state,wanted;RecoveryPrimaryFillBinding binding;};
struct RRFilled {};
int sends=0,bindings=0;bool ticket=true,published_bad=false,proof=false;
ulong clock_ms=100;
ulong GetTickCount64(){return clock_ms;}
bool FileIsExist(const string&){return published_bad;}
bool RRStage(RRRangeState& r,const RRSettings&,int,int,RRAction& a){r.pending.kind=RR_REENTRY;a=r.pending;return true;}
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope& scope,const RRAction& a,double,double,RecoveryIntentKey& k){
 k.id=scope.generation;return a.kind==RR_REENTRY;
}
int RecoveryDispatchOnce(const RRRangeState&,const RRAction&,const RecoveryIntentKey&,double,double,RecoveryDispatchResult& r){
 ++sends;r.order=ticket?71:0;return ticket?RD_TICKET_RECORDED:0;
}
bool RecoveryIntentKeysMatch(const RecoveryIntentKey& a,const RecoveryIntentKey& b){return a.id==b.id;}
int RecoveryIntentLookup(const RecoveryIntentKey&,string& out){out="71";return RI_TICKET;}
string RecoveryOwnerNonce(const string& lane,const string& id){return lane+id;}
bool RecoveryPrimaryPumpBegin(RecoveryPrimaryFillPump& p,const RRRangeState&,const RRSettings&,const RecoveryPrimaryFillBinding& b,const string&){
 assert(!b.exact.binding.nonce.empty()&&b.exact.binding.nonce!=b.history.binding.nonce);
 assert(b.exact.order_ticket==71&&b.history.anchor_order==71&&b.verify_after==9);
 ++bindings;p.binding=b;p.state=RPP_WAIT_EXACT;return true;
}
RecoveryPrimaryPumpResult RecoveryPrimaryPumpStep(RecoveryPrimaryFillPump& p,const RRRangeState&,const RRSettings&,RRFilled&){
 if(proof){p.state=RPP_DONE;return RPP_PROOF;}return RPP_WAIT;
}
bool RRFinalReentryFill(RRRangeState& r,const RRSettings&,int&,int,const RRFilled&){r.phase=RR_ACTIVE;return true;}
"""
TEST = r"""
int main(){
 RecoveryRangeOwner owner{3,0};RecoveryFinalLeg task{};RRSettings cfg{};RRRangeState range{};
 RecoverySessionIntentScope scope{"generation-1",1};
 RecoveryEvidenceOwner verifier{0};RecoveryInventoryScope v{};
 auto tick=[&]{RecoveryFinalReentry(task,owner,range,cfg,scope,v,verifier,9,.01,.01);};
 range.phase=RR_ARM_REENTRY;range.reentry_slot=24;
 tick();assert(sends==1&&bindings==1&&task.ticket==71);
 string first=task.fill.binding.exact.binding.nonce;
 for(int i=0;i<4;i++)tick();
 assert(sends==1&&bindings==1); // no report published yet
 published_bad=true;tick();assert(task.fill.state==RPP_UNRESOLVED);
 published_bad=false;tick();clock_ms+=999;tick();assert(bindings==1);
 scope.generation="old";++clock_ms;tick();assert(bindings==1&&sends==1);
 scope.generation="generation-1";tick();
 assert(bindings==2&&task.fill.binding.exact.binding.nonce!=first&&sends==1);
 proof=true;tick();assert(range.phase==RR_ACTIVE&&owner.revision==4);
 tick();assert(sends==1&&bindings==2);
 // Unknown submission outcome is never retried, including after timers and re-reads.
 range={};range.phase=RR_ARM_REENTRY;range.reentry_slot=24;task={};
 ticket=false;proof=false;tick();
 for(int i=0;i<10;i++){clock_ms+=300000;tick();}
 assert(sends==2&&bindings==2&&range.pending.kind==RR_REENTRY);
}
"""


class FinalReentryTest(unittest.TestCase):
    def test_read_retries_preserve_the_only_ticket_and_intent(self):
        retry = (HERE / "RecoveryProofRetry.mqh").read_text()
        final = (HERE / "RecoveryFinalLeg.mqh").read_text()
        controller = final[final.index("struct RecoveryFinalLeg"):final.index("bool RecoveryFinalStep")]
        for value in ("task.ticket", "serial"):
            controller = controller.replace("(string)" + value, "std::to_string(" + value + ")")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.cpp"
            path.write_text(SHIM + retry + controller + TEST)
            exe = path.with_suffix("")
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                            "-fsanitize=undefined", str(path), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    unittest.main()
