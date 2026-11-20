"""Host-only manual adoption/confirmation against real verifier record parsers."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))
sys.path.insert(0,str(ROOT/"verifier"))
from test_reader_compat import SHIM,as_cpp

HARNESS=r"""
#include <climits>
#include <cmath>
using ulong=std::uint64_t;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathRound(double n){return std::round(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
enum {ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,
      ORDER_STATE_PLACED=1,ORDER_STATE_CANCELED=2,ORDER_STATE_FILLED=4,
      ORDER_STATE_REJECTED=5,ORDER_STATE_EXPIRED=6,DEAL_ENTRY_IN=0};
template<class T> void ZeroMemory(T &out){out=T{};}
#include "RecoveryAlternatingFill.mqh"
#include "RecoveryPanelControl.mqh"
#include "RecoverySessionLifecycle.mqh"
struct RecoverySessionIntentScope {string executor_epoch;long login,magic;string server,symbol;ulong scope,generation;};
struct RecoveryIntentKey {string id;};
bool RecoveryPrimarySession(const RecoverySessionLifecycle& s,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope){
 return RecoverySessionCurrent(s,live)&&s.epoch==scope.executor_epoch&&live.login==scope.login&&
        live.server==scope.server&&live.symbol==scope.symbol&&live.magic==scope.magic&&scope.scope>0;
}
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope& scope,const RRAction& a,double,double,RecoveryIntentKey& out){
 out.id=scope.executor_epoch+":"+std::to_string(a.serial);return a.scope==scope.scope;
}
bool RecoveryIntentContextOK(const RecoveryIntentKey& key){return !key.id.empty();}
enum {POSITION_SL,POSITION_TP};
ulong live_ticket=9007199254740994ULL;
double live_sl=0,live_tp=0;
bool PositionSelectByTicket(ulong p){return p==live_ticket;}
bool PositionGetDouble(int field,double& out){out=(field==POSITION_SL?live_sl:live_tp);return true;}
bool RecoveryDispatchUnits(double v,double unit,bool allow_zero,long expected){
 return (allow_zero || v>0) && std::abs(v/unit-expected)<1.e-8;
}
enum {RD_TICKET_RECORDED=4};
struct RecoveryPairSendResult {struct Receipt {int state;ulong order;};Receipt reverse;};
int sends=0,sl_arms=0;
bool receipt_known=true;
void RecoveryDispatchOpeningPair(const RRRangeState &r,const RROpeningPair &p,const RecoveryIntentKey& sl,
 const RecoveryIntentKey& reverse,double,double,RecoveryPairSendResult& out){
 assert(RROpeningBound(r,p)&&!reverse.id.empty());
 assert(p.sl_required==!sl.id.empty());
 if(p.sl_required){assert(p.sl.sl_ticks==p.reverse.entry_ticks&&p.sl.tp_ticks==r.live_tp);++sl_arms;}
 ++sends;
 if(receipt_known)out.reverse={RD_TICKET_RECORDED,44};
}
"""

TEST=r"""
std::string fixed(long t){
 auto cents=std::to_string(t%100);
 return std::to_string(t/100)+"."+(cents.size()==1?"0":"")+cents+"00000000000000";
}
string inv(const RecoveryInventoryScope& q,int seq,int count,const string& positions,const string& order=""){
 return "RECOVERY_INVENTORY_V1\t"+q.nonce+"\t"+q.executor_epoch+"\t"+q.verifier_epoch+"\t7\t5\tTest\tXAUUSD.s\t700\t99\t"+
   std::to_string(seq)+"\tcomplete\tok\t"+std::to_string(count)+"\t"+(order.empty()?"0":"1")+"\n"+positions+order+"END\n";
}
string pos(int side,long sl,long tp,const string& ids="9007199254740993\t9007199254740994"){
 return "P\t"+ids+"\t0\t"+(side==1?"BUY":"SELL")+"\t"+fixed(2)+"\t"+fixed(400000)+"\t"+fixed(sl)+"\t"+fixed(tp)+"\n";
}
string hist(const RecoveryHistoryScope& q,int seq,int side,string deal_order="17"){
 return "RECOVERY_HISTORY_V1\t"+q.binding.nonce+"\t"+q.binding.executor_epoch+"\t"+q.binding.verifier_epoch+
   "\t7\t5\tTest\tXAUUSD.s\t700\t99\t"+std::to_string(seq)+"\t9007199254740993\t17\tobserved\tok\t1\t1\n"+
   "H\t17\t0\t0\t4\t7\t"+fixed(2)+"\t"+fixed(0)+"\t"+fixed(400000)+"\t"+fixed(0)+"\t"+fixed(0)+"\n"+
   "D\t19\t"+deal_order+"\t0\t"+(side==1?"BUY":"SELL")+"\t0\t3\t8\t"+
   fixed(2)+"\t"+fixed(400000)+"\t"+fixed(0)+"\t"+fixed(0)+"\nEND\n";
}
string opposite(int side,long entry,long sl) {
 return "O\t44\t700\t0\t"+std::to_string(side==1?5:4)+"\t1\t"+fixed(1)+"\t"+fixed(1)+"\t"+
   fixed(entry)+"\t"+fixed(sl)+"\t"+fixed(0)+"\t"+fixed(0)+"\n";
}
int main(){
 const RecoverySessionContext live{11,5,700,"Test","XAUUSD.s","fixed"};
 RecoverySessionLifecycle life{};
 const string epoch(32,'2');
 assert(RecoverySessionInit(life,live,true,epoch)==RSL_NEW);
 RecoverySessionIntentScope scope{epoch,5,700,"Test","XAUUSD.s",1,7};
 const RecoveryInventoryScope inventory{string(32,'1'),epoch,string(32,'3'),7,5,"Test","XAUUSD.s",700,99};
 auto final=inventory;final.nonce=string(32,'9');
 RecoveryHistoryScope h{inventory,9007199254740993ULL,17};h.binding.nonce=string(32,'4');
 RRSettings cfg{};cfg.scope=1;cfg.generation=7;cfg.initial_sl_ticks=80;
 cfg.support_first_ticks=0;cfg.support_second_ticks=40;cfg.support_offset_ticks=0;
 for(auto& n:cfg.lot_steps)n=1;
 for(int side:{1,-1}) {
    const long prior=side==1?399965:0;
    const long tp=side==1?400150:399850;
    live_sl=prior*.01;live_tp=tp*.01;
    const long target=prior==0?400000-side*80:prior;
    const auto original=pos(side,prior,tp);
    RecoveryPanelControl panel{};
    RecoveryInventorySnapshot inventory_read{};
    RecoveryHistorySnapshot history_read{};
    assert(RecoveryInvParse(inv(inventory,7,1,original),inventory,inventory_read));
    assert(RecoveryHistoryParse(hist(h,8,side),h,history_read));
    RecoveryManualOwner owner{};
    auto plan=[&](){
       return RecoveryPlanManualOpening(owner,panel,life,live,scope,cfg,
              inventory,inventory_read,h,history_read,6,.01,.01);
    };
    assert(!plan()&&!owner.opening.reserved&&panel.mode==REC_CONTROL_IDLE);
    panel.mode=REC_CONTROL_MANUAL_WAIT;
    assert(RecoveryInvParse(inv(inventory,7,2,original+pos(side,prior,tp,"900\t901")),
           inventory,inventory_read));
    assert(!plan());
    assert(RecoveryInvParse(inv(inventory,7,1,original),inventory,inventory_read));
    assert(RecoveryHistoryParse(hist(h,8,-side),h,history_read));
    assert(!plan()); // anchor in unrelated direction is not opening proof
    assert(RecoveryHistoryParse(hist(h,8,side),h,history_read));
    assert(plan()&&!plan()&&owner.phase==RMO_PLANNED&&panel.mode==REC_CONTROL_MANUAL_WAIT);
    assert(owner.range.cycle.active_order==17&&owner.range.cycle.position_id==9007199254740993ULL);
    assert(owner.range.live_tp==tp&&owner.range.live_sl==prior&&owner.opening.sl_required==(prior==0));
    assert(owner.opening.reverse.entry_ticks==target&&owner.opening.reverse.sl_ticks==target+side*80);
    const auto ladder=owner.ledger.strategy_slot;
    assert(ladder==1&&owner.opening.reverse.volume_steps==1);
    auto changed=owner;
    live_tp+=.01;
    assert(!RecoveryAttemptManualOpening(changed,panel,life,live,scope)&&changed.phase==RMO_ATTEMPTED);
    live_tp=tp*.01;
    assert(!RecoveryAttemptManualOpening(changed,panel,life,live,scope));
    auto unknown=owner;
    receipt_known=false;
    assert(RecoveryAttemptManualOpening(unknown,panel,life,live,scope)&&unknown.intent.opposite_ticket==0);
    assert(!RecoveryAttemptManualOpening(unknown,panel,life,live,scope));
    receipt_known=true;
    assert(RecoveryAttemptManualOpening(owner,panel,life,live,scope)&&owner.intent.opposite_ticket==44);
    assert(panel.mode==REC_CONTROL_MANUAL_WAIT&&!RecoveryAttemptManualOpening(owner,panel,life,live,scope));
    const auto waiting=pos(side,target,tp)+string();
    const auto order=opposite(side,target,target+side*80);
    assert(RecoveryInvParse(inv(final,8,1,waiting,order),final,inventory_read));
    assert(RecoveryConfirmManualOpening(owner,panel,life,live,scope,final,inventory_read)==RA_UNRESOLVED);
    assert(RecoveryInvParse(inv(final,9,1,pos(side,target,tp+1),order),final,inventory_read));
    assert(RecoveryConfirmManualOpening(owner,panel,life,live,scope,final,inventory_read)==RA_CONFLICT);
    assert(RecoveryInvParse(inv(final,10,1,waiting,order),final,inventory_read));
    assert(RecoveryConfirmManualOpening(unknown,panel,life,live,scope,final,inventory_read)==RA_UNRESOLVED);
    assert(RecoveryConfirmManualOpening(owner,panel,life,live,scope,final,inventory_read)==RA_CONFIRMED);
    assert(panel.mode==REC_CONTROL_ADOPTED&&!panel.configured[0]&&!panel.configured[1]&&owner.range.phase==RR_ACTIVE);
    assert(owner.range.live_sl==target&&owner.range.live_tp==tp&&owner.range.opposite.ticket==44);
 }
 assert(sends==4&&sl_arms==2);
}
"""

class ManualOwnerCompat(unittest.TestCase):
    def test_manual_candidate_pair_and_later_confirmation(self):
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
        inventory=(HERE/"RecoveryAdoptionInventory.mqh").read_text()
        body=as_cpp((verifier/"RecoveryInventoryReader.mqh").read_text())+hr+gate+inventory+matching+(HERE/"RecoveryManualOwner.mqh").read_text()
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
            prefix=HARNESS.replace('"RecoveryAlternatingFill.mqh"','"cycle/RecoveryAlternatingFill.mqh"')\
                         .replace('"RecoveryPanelControl.mqh"','"execution/RecoveryPanelControl.mqh"')\
                         .replace('"RecoverySessionLifecycle.mqh"','"execution/RecoverySessionLifecycle.mqh"')
            path.write_text(SHIM+prefix+body+TEST)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            "-fno-sanitize-recover=all",str(path),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
