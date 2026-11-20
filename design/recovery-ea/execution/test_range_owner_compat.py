"""Run the actual owner and services against an inert verifier/transport."""
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

TYPES = r"""
#include <map>
#include "RecoveryPanelControl.mqh"
#include "RecoverySessionLifecycle.mqh"
#include "RecoverySessionIntentKey.mqh"
enum RecoveryDispatchState {RD_NOT_SENT,RD_OUTCOME_UNRESOLVED,RD_TICKET_RECORDED};
struct RecoveryDispatchResult {RecoveryDispatchState state;ulong order;};
struct RecoveryInventoryScope {
 string nonce,executor_epoch,verifier_epoch;long generation,login;string server,symbol;
 long magic,verifier_chart;
};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
struct RecoveryHistoryScope {RecoveryInventoryScope binding;ulong position_identifier,anchor_order;};
struct RecoveryPrimaryFillBinding {
 RecoveryExactReportScope exact;RecoveryHistoryScope history;RecoveryInventoryScope inventory;
 ulong scope,verify_after;RRAction routed;double price_tick,volume_step;
};
struct RecoveryEvidenceOwner {RecoverySessionContext context;string executor_epoch,verifier_epoch;long verifier_chart;ulong serial;};
struct RecoveryAlternatingEvidenceBinding {RecoveryInventoryScope inventory;ulong order_ticket;};
struct RecoveryOwnerEvidence {RecoveryAlternatingEvidenceBinding binding;string publisher_tag;};
struct MqlTradeTransaction {ulong ticket;};
std::map<string,ulong> journal;
int primary_sends=0,opening_sends=0,ordinary_sends=0,disk_reads=0;
bool uncertain=false;
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope& a,const RecoveryInventoryScope& b) {
 return a.executor_epoch==b.executor_epoch&&a.verifier_epoch==b.verifier_epoch&&a.verifier_chart==b.verifier_chart&&
   a.generation==b.generation&&a.login==b.login&&a.server==b.server&&a.symbol==b.symbol&&a.magic==b.magic;
}
const int RI_TICKET=2;
int RecoveryIntentLookup(const RecoveryIntentKey& key,string& order){
 auto it=journal.find(key.id);
 if(it==journal.end())return 0;
 order=std::to_string(it->second);return RI_TICKET;
}
string RecoveryOwnerNonce(const string& lane,const string& identity) {
 return RecoverySessionSha256(lane+"\t"+identity).substr(0,32);
}
RecoveryDispatchState RecoveryDispatchOnce(const RRRangeState& r,const RRAction& a,const RecoveryIntentKey& k,
 double,double,RecoveryDispatchResult& out){
 assert(RRActionEqual(r.pending,a)&&RecoveryIntentContextOK(k));
 ++primary_sends;
 if(uncertain){out={RD_OUTCOME_UNRESOLVED,0};return out.state;}
 const ulong ticket=100+static_cast<ulong>(a.range_id);
 journal[k.id]=ticket;out={RD_TICKET_RECORDED,ticket};return out.state;
}
enum RecoveryPairSendState {RPS_NOT_STARTED,RPS_AWAIT_VERIFIER};
struct RecoveryPairSendResult {RecoveryDispatchResult reverse;};
RecoveryPairSendState RecoveryDispatchOpeningPair(const RRRangeState& s,const RROpeningPair& p,const RecoveryIntentKey& sl,
 const RecoveryIntentKey& reverse,double,double,RecoveryPairSendResult& out){
 assert(!p.sl_required&&RROpeningBound(s,p)&&sl.id.empty()&&!reverse.id.empty());
 ++opening_sends;
 const ulong ticket=200+static_cast<ulong>(s.cycle.range_id);
 journal[reverse.id]=ticket;out.reverse={RD_TICKET_RECORDED,ticket};
 return RPS_AWAIT_VERIFIER;
}
bool RecoveryReadPrimaryFill(const RRRangeState&,const RRSettings&,const RecoveryPrimaryFillBinding&,RRFilled&){
 ++disk_reads;return false;
}
bool RecoveryStagePrimaryOpening(RRRangeState& s,const RRSettings& cfg,const RecoveryPrimaryFillBinding& b,
 const RRFilled& f,RROpeningPair& p,RRAlternationLedger& ledger){
 assert(f.independently_verified&&b.exact.order_ticket==f.entry.executed_order);
 if(p.reserved||ledger.scope!=0)return false;
 assert(RRConfirm(s,{s.pending,true,b.exact.order_ticket}));
 assert(RRObserveFill(s,cfg,f));
 assert(RRAfterActualEntryNoSL(s,cfg)&&s.phase==RR_ARM_REVERSE);
 assert(RRStageOpeningPair(s,cfg,p)&&!p.sl_required);
 return RRStartAlternation(s,0,ledger);
}
enum RecoveryPrimaryPumpState {RPP_UNUSED,RPP_WAIT_TERMINAL,RPP_DONE};
enum RecoveryPrimaryPumpResult {RPP_WAIT,RPP_PROGRESS,RPP_PROOF,RPP_STOPPED};
struct RecoveryPrimaryFillPump {RecoveryPrimaryPumpState state;RecoveryPrimaryFillBinding binding;};
bool primary_ready=false;
bool RecoveryPrimaryPumpBegin(RecoveryPrimaryFillPump& p,const RRRangeState& s,const RRSettings&,
 const RecoveryPrimaryFillBinding& b,const string& tag){
 assert(p.state==RPP_UNUSED&&b.exact.order_ticket==100+static_cast<ulong>(s.cycle.range_id)&&
   b.history.anchor_order==b.exact.order_ticket&&b.history.position_identifier==0);
 assert(b.exact.binding.nonce!=b.history.binding.nonce&&b.inventory.nonce!=b.history.binding.nonce&&
   b.inventory.nonce!=b.exact.binding.nonce&&RecoveryInvHex(tag));
 assert(RRActionEqual(s.pending,b.routed));
 p.binding=b;p.state=RPP_WAIT_TERMINAL;return true;
}
RecoveryPrimaryPumpResult RecoveryPrimaryPumpStep(RecoveryPrimaryFillPump& p,const RRRangeState& s,
 const RRSettings& cfg,RRFilled& f){
 if(p.state!=RPP_WAIT_TERMINAL)return RPP_STOPPED;
 if(!primary_ready)return RPP_WAIT;
 const ulong id=500+static_cast<ulong>(s.cycle.range_id),ord=p.binding.exact.order_ticket;
 const int side=s.primary_side;
 const long vol=s.pending.volume_steps;
 f={{cfg.generation,s.cycle.range_id,ord,ord,id,side,vol,vol,vol,s.primary_ticks,true,true,true},
    id+100,0,s.pending.sl_ticks,0,true};
 p.binding.history.position_identifier=id;p.state=RPP_DONE;
 return RPP_PROOF;
}
enum RecoveryAlternatingServiceResult {RAS_WAITING,RAS_VERIFYING,RAS_ADVANCED,RAS_NO_EFFECT};
struct RecoverySessionAlternatingResult {RRAlternatingResult transition;RecoveryDispatchResult receipt;};
struct RecoveryOrdinaryEventLoop {ulong armed;bool queued;};
bool RecoveryReserveOwnerEvidence(RecoveryEvidenceOwner& v,const RecoverySessionLifecycle& s,const RecoverySessionContext&,
 const RRRangeState& r,const RROpeningPair&,ulong ticket,ulong,double,double,RecoveryOwnerEvidence& e) {
 e.binding.inventory.executor_epoch=s.epoch;e.binding.order_ticket=ticket;++v.serial;
 assert(ticket>=201&&r.cycle.position_id>0);return true;
}
bool RecoveryOrdinaryEventBind(RecoveryOrdinaryEventLoop& loop,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoveryAlternatingState&,const RecoveryAlternatingEvidenceBinding& bind,const string&) {
 loop={bind.order_ticket,false};return true;
}
bool RecoveryOrdinaryOnTrade(const MqlTradeTransaction& e,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoveryInventoryScope&,RecoveryOrdinaryEventLoop& loop){
 if(e.ticket!=loop.armed||loop.queued)return false;
 loop.queued=true;return true;
}
RecoveryAlternatingServiceResult RecoveryOrdinaryOnTimer(const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoveryInventoryScope&,RecoveryOrdinaryEventLoop& loop,RecoveryAlternatingState& s,
 RecoveryAlternatingOutbox& out,const RRSettings& cfg,RecoverySessionAlternatingResult& result) {
 if(!loop.queued)return RAS_WAITING;
 // The owner must use current panel state, not its earlier snapshot.
 assert(s.range.live_sl==s.range.cycle.actual_entry_ticks-10);
 const auto f=evidence(s.range,s.opening,loop.armed,999,s.opening.reverse.entry_ticks-9);
 result.transition=RRAdvanceAlternatingFill(s.range,cfg,s.opening,s.ledger,f);
 assert(result.transition==RR_ALT_NEXT_OPPOSITE);
 ++s.revision;out.revision=s.revision;
 ++ordinary_sends;
 result.receipt={RD_TICKET_RECORDED,301};
 loop.queued=false;
 return RAS_ADVANCED;
}
"""

TEST = r"""
#include "RecoveryRangeOwner.mqh"
int main(){
 const auto cfg=config();
 const RecoverySessionContext live{1,12,18,"server","XAUUSD.s","fixed"};
 const string epoch(32,'a');
 const RecoverySessionIntentScope scope{epoch,12,18,"server","XAUUSD.s",cfg.scope,cfg.generation};
 RecoverySessionLifecycle life{};
 assert(RecoverySessionInit(life,live,true,epoch)==RSL_NEW);
 RecoveryPanelControl panel{};
 RecoveryControlCommand cmd{REC_CONTROL_CONFIGURE,2,{{1,1,400000},{2,-1,399800}}};
 assert(RecoveryApplyPanelControl(panel,cmd,cfg)==REC_CONTROL_APPLIED);
 RecoveryRangeOwner owners[2]{};
 RecoveryEvidenceOwner v{live,epoch,string(32,'3'),99,0};
 const RecoveryInventoryScope verifier{string(32,'1'),epoch,v.verifier_epoch,static_cast<long>(cfg.generation),
              live.login,live.server,live.symbol,live.magic,99};
 assert(RecoveryRangeOwnerSubmit(owners[0],panel,1,life,live,scope,.01,.01));
 auto duplicate=owners[1];
 assert(!RecoveryRangeOwnerSubmit(duplicate,panel,1,life,live,scope,.01,.01));
 assert(!RecoveryRangeOwnerSubmit(owners[0],panel,1,life,live,scope,.01,.01)&&primary_sends==1);
 uncertain=true;
 assert(RecoveryRangeOwnerSubmit(owners[1],panel,2,life,live,scope,.01,.01)&&primary_sends==2);
 assert(!RecoveryRangeOwnerBindPrimary(owners[1],panel,life,live,scope,v,0,.01,.01));
 assert(!RecoveryRangeOwnerBindPrimary(owners[0],panel,life,live,scope,v,0,.02,.01)&&v.serial==0);
 assert(RecoveryRangeOwnerBindPrimary(owners[0],panel,life,live,scope,v,0,.01,.01)&&v.serial==1);
 assert(!RecoveryRangeOwnerBindPrimary(owners[0],panel,life,live,scope,v,0,.01,.01)&&v.serial==1);
 assert(RecoveryRangeOwnerPrimaryTimer(owners[0],panel,life,live,scope,verifier)==RPP_WAIT&&opening_sends==0);
 auto other=verifier;other.verifier_chart++;
 assert(RecoveryRangeOwnerPrimaryTimer(owners[0],panel,life,live,scope,other)==RPP_STOPPED&&opening_sends==0);
 primary_ready=true;
 assert(RecoveryRangeOwnerPrimaryTimer(owners[0],panel,life,live,scope,verifier)==RPP_PROOF&&opening_sends==1);
 assert(disk_reads==0&&owners[0].phase==RRO_ORDINARY&&owners[0].next_exact_ticket==201&&owners[0].revision==1);
 assert(panel.ranges[0].cycle.position_id==501&&panel.ranges[1].cycle.position_id==0);
 assert(RecoveryRangeOwnerPrimaryTimer(owners[0],panel,life,live,scope,verifier)==RPP_STOPPED&&opening_sends==1);
 assert(RecoveryRangeOwnerArmOrdinary(owners[0],panel,life,live,scope,v,0,.01,.01));
 assert(!RecoveryRangeOwnerArmOrdinary(owners[0],panel,life,live,scope,v,0,.01,.01)&&v.serial==2);
 assert(!RecoveryRangeOwnerOrdinaryTrade(owners[0],panel,life,live,scope,verifier,{202}));
 assert(RecoveryRangeOwnerOrdinaryTrade(owners[0],panel,life,live,scope,verifier,{201}));
 assert(!RecoveryRangeOwnerOrdinaryTrade(owners[0],panel,life,live,scope,verifier,{201}));
 panel.ranges[0].live_sl=panel.ranges[0].cycle.actual_entry_ticks-10;
 RecoverySessionAlternatingResult result{};
 assert(RecoveryRangeOwnerOrdinaryTimer(owners[0],panel,life,live,scope,verifier,result)==RAS_ADVANCED);
 assert(ordinary_sends==1&&owners[0].next_exact_ticket==301&&owners[0].revision==2);
 assert(panel.ranges[0].cycle.position_id==999&&panel.ranges[1].phase==RR_NEW);
 assert(RecoveryRangeOwnerOrdinaryTimer(owners[0],panel,life,live,scope,verifier,result)==RAS_WAITING&&ordinary_sends==1);
 assert(RecoveryRangeOwnerArmOrdinary(owners[0],panel,life,live,scope,v,0,.01,.01)&&v.serial==3);
}
"""

class RangeOwnerCompat(unittest.TestCase):
    def test_single_range_authority_and_exact_receipts(self):
        cycle = ROOT / "cycle"
        execution = ROOT / "execution"
        base = runpy.run_path(str(execution / "test_session_intent_compat.py"))["SHIM"]
        base = re.sub(r"^(?:enum RecoveryPairSendState|struct RecoveryDispatchResult).*\n",
                      "", base.split("int sends=",1)[0],flags=re.M)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/"cycle").mkdir();q=p/"execution";q.mkdir()
            for name in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                shutil.copy2(cycle/name,p/"cycle"/name)
            cp=(cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
            cp=cp.replace('#include "RecoveryCloseWire.mqh"','#include "RecoveryCycle.mqh"')
            cp=cp[:cp.index("bool RCCPStageFromWire")]+cp[cp.index("void RCCPClearPlan"):]
            e=(cycle/"RecoveryCloseEvidence.mqh").read_text()
            cp=cp.replace('#include "RecoveryCycle.mqh"','#include "RecoveryCycle.mqh"\n'+
                 e[e.index("struct RCCloseEvidenceKey"):e.index("struct RCExitHistory")]+
                 e[e.index("bool RCCloseKeysEqual"):e.index("bool RCExitSame")],1)
            (p/"cycle/RecoveryCloseCheckpoint.mqh").write_text(cp+"\n#endif\n")
            for name in ("RecoverySessionLifecycle.mqh","RecoveryPanelControl.mqh",
                         "RecoveryPrimarySubmit.mqh","RecoveryAlternatingPlan.mqh"):
                shutil.copy2(execution/name,q/name)
            key=(execution/"RecoverySessionIntentKey.mqh").read_text().replace('#include "RecoveryIntentJournal.mqh"','')
            key=key.replace("uchar bytes[],unused[],digest[];","std::vector<uchar> bytes,unused,digest;")
            key=re.sub(r"\(string\)((?:scope|action)\.\w+)",r"std::to_string(\1)",key)
            (q/"RecoverySessionIntentKey.mqh").write_text(key)
            for name in ("RecoveryPrimaryService.mqh","RecoveryRangeOwner.mqh"):
                src=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",(HERE/name).read_text())
                (q/name).write_text(src)
            for name in ("RecoveryDispatch.mqh","RecoveryPrimaryFill.mqh","RecoveryOpeningDispatch.mqh",
                         "RecoveryPrimaryFillPump.mqh","RecoveryOrdinaryEventLoop.mqh","RecoveryOwnerEvidence.mqh"):
                (q/name).touch()
            fixture=(cycle/"test_alternating_fill.cpp").read_text().split("static RCCloseCheckpoint closure(")[0]
            fixture=fixture.replace('"RecoveryAlternatingFill.mqh"','"../cycle/RecoveryAlternatingFill.mqh"')
            fixture=fixture.replace("constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();","")
            fixture=fixture.replace("static RRRangeState first(", "[[maybe_unused]] static RRRangeState first(")
            (q/"check.cpp").write_text(base+fixture+'#include "RecoveryAlternatingPlan.mqh"\n'+TYPES+TEST)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                           str(q/"check.cpp"),"-lcrypto","-o",str(p/"check")],check=True)
            subprocess.run([str(p/"check")],check=True)
if __name__=="__main__":unittest.main()
