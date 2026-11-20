"""Diagnostic host shim of the chart controller; no terminal actions."""
from pathlib import Path
import shutil,subprocess,tempfile,unittest
HERE=Path(__file__).resolve().parent
SHIM=r"""
#include <cassert>
#include <climits>
#include <cmath>
#include <string>
using string=std::string;
using ulong=unsigned long;
struct RecoverySessionContext {long login,magic;string server,symbol;};
struct RecoverySessionLifecycle {string epoch;};
struct RRSettings {ulong scope,generation;long support_first_ticks,support_offset_ticks;};
bool RRSettingsValid(const RRSettings& c){return c.scope&&c.generation;}
bool MathIsValidNumber(double x){return std::isfinite(x);}
template<class T> void ZeroMemory(T& t){t=T{};}
struct RecoveryLiveSession{
 bool owns_mail;
 RecoverySessionLifecycle lifecycle;
 int mailbox,mail,panel_seen;
};
enum {REC_CONTROL_IDLE,REC_CONTROL_RANGES};
struct TestCycle {ulong position_id;};
struct TestRange {TestCycle cycle;};
struct RecoveryPanelControl {int mode;bool configured[2];TestRange ranges[2];};
struct RecoveryEvidenceOwner {string verifier_epoch;long verifier_chart;};
enum {RRO_UNUSED,RRO_PRIMARY_PENDING,RRO_ORDINARY,RRO_FINAL};
struct TestOpening {bool reserved,sl_required;int reverse;};
struct TestOrdinary {struct Hint{int state;};struct Pump{int state;};Hint hint;Pump pump;};
struct RecoveryIntentKey {string id;};
struct RecoveryRangeOco {int winner_range_id;ulong winner_order;};
struct RecoveryPrimarySubmitResult {ulong ticket;RecoveryIntentKey key;};
struct RecoveryPrimaryFillBinding {};
struct RRFilled {struct Entry {ulong executed_order;};Entry entry;};
struct RecoveryRangeOwner {
 int phase,sent,received,id;
 RecoveryPrimarySubmitResult primary;
 RRFilled first_proof;
 struct PrimaryPump{RecoveryPrimaryFillBinding binding;};PrimaryPump primary_pump;TestOpening opening;ulong next_exact_ticket,armed_position;TestOrdinary ordinary;
};
struct RecoverySessionIntentScope {string executor_epoch,server,symbol;long login,magic;ulong scope,generation;};
struct RecoveryInventoryScope {string nonce,executor_epoch,verifier_epoch,server,symbol;long login,magic,generation,verifier_chart;};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
enum RecoveryOcoCancelResult {ROC_WAIT=0,ROC_REQUESTED,ROC_CANCELED,ROC_UNKNOWN};
struct RecoveryOcoCancel {int attempts;};
int canceled=0,cancel_begins=0;
ulong canceled_ticket=0;
bool RecoveryBeginOcoCancel(RecoveryOcoCancel& c,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryPanelControl&,int winner,const RecoveryPrimaryFillBinding&,
 const RRFilled& proof,int loser,const RecoveryPrimarySubmitResult& primary,
 const RecoveryExactReportScope& request,ulong,const string&,double,double){
 assert(winner+loser==3&&proof.entry.executed_order==static_cast<ulong>(50+winner));
 assert(request.order_ticket==primary.ticket&&primary.ticket==static_cast<ulong>(50+loser));
 canceled_ticket=request.order_ticket;++cancel_begins;++c.attempts;return true;
}
RecoveryOcoCancelResult RecoveryStepOcoCancel(RecoveryOcoCancel& c,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoverySessionIntentScope&,const RecoveryPanelControl&){
 assert(c.attempts==1);++canceled;return ROC_CANCELED;
}
enum {RFH_QUEUED=2,RAP_UNUSED=0,RWR_IDLE=0,RWR_ACTIVE=2};
struct RecoveryWorkingReverse {int state;};
int working=0;
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope&,int,double,double,RecoveryIntentKey& out){
 out.id="exact";return true;
}
string RecoveryOwnerNonce(const string& label,const string& identity){return label+identity;}
bool RecoveryBeginWorkingReverse(RecoveryWorkingReverse& w,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const TestRange&,const TestOpening&,const RecoveryIntentKey&,
 const RecoveryExactReportScope& request,ulong,const string&,double,double){
 assert(request.order_ticket==77);w.state=1;return true;
}
int RecoveryStepWorkingReverse(RecoveryWorkingReverse& w,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,TestRange&,TestOpening& p){++working;w.state=2;p.reserved=false;return RWR_ACTIVE;}
void RecoveryOrdinaryEventReset(TestOrdinary& o){o={};}
struct RecoverySessionAlternatingResult{};
struct RecoveryPanelApplyResult{};
struct MqlTradeTransaction {int range;};
int publishes=0,polls=0,submits=0,primary_reads=0,ordinary_reads=0;
bool good=true,fail_publish=false,configure=false,two_ranges=true;
int proof_range=0;
void RecoveryLiveSessionStop(RecoveryLiveSession& u){u={};}
bool RecoveryLiveSessionCurrent(const RecoveryLiveSession& u,const RecoverySessionContext&){return u.owns_mail&&good;}
bool RecoveryLiveSessionStart(RecoveryLiveSession& u,const RecoverySessionContext&,const string&,const string& seed){
 u.owns_mail=true;u.lifecycle.epoch=seed;return true;
}
bool RecoveryBeginEvidenceOwner(RecoveryEvidenceOwner& v,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const string& e,long id){if(id==0)return false;v.verifier_chart=id;v.verifier_epoch=e;return true;}
bool RecoveryLiveSessionPublish(const RecoveryLiveSession&,const RecoverySessionContext&,int){
 if(fail_publish)return false;
 ++publishes;return true;
}
bool RecoveryPanelMailboxPoll(int&,int&,const RecoverySessionLifecycle&,const RecoverySessionContext&,const RRSettings&,
 double tick,int&,RecoveryPanelControl& panel,RecoveryPanelApplyResult&) {
 assert(tick==.01);++polls;
 if(!configure)return false;
 configure=false;panel.mode=REC_CONTROL_RANGES;
 panel.configured[0]=true;panel.configured[1]=two_ranges;panel.ranges[0].cycle.position_id=10;panel.ranges[1].cycle.position_id=11;
 return true;
}
bool RecoveryRangeOwnerSubmit(RecoveryRangeOwner& u,RecoveryPanelControl&,int id,const RecoverySessionLifecycle& l,
 const RecoverySessionContext&,const RecoverySessionIntentScope& scope,double tick,double step){
 assert(id>=1&&id<=2&&tick==.01&&step==.02&&l.epoch==scope.executor_epoch);
 assert(u.phase==RRO_UNUSED);
 ++submits;++u.sent;u.id=id;u.primary.ticket=50+id;u.primary.key.id="primary";u.phase=RRO_PRIMARY_PENDING;return true;
}
bool RecoveryRangeOwnerBindPrimary(RecoveryRangeOwner&,const RecoveryPanelControl&,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoverySessionIntentScope&,RecoveryEvidenceOwner&,ulong,double,double){return true;}
void RecoveryRangeOwnerPrimaryTimer(RecoveryRangeOwner& u,RecoveryPanelControl&,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoverySessionIntentScope&,const RecoveryInventoryScope& v,RecoveryRangeOco& oco){
 assert(v.verifier_chart==2&&oco.winner_range_id==0);++primary_reads;
 if(proof_range==u.id) {
  oco.winner_range_id=u.id;oco.winner_order=u.primary.ticket;u.first_proof.entry.executed_order=u.primary.ticket;
  u.phase=RRO_ORDINARY;u.opening.reserved=true;u.next_exact_ticket=77;
 }
}
bool RecoveryRangeOwnerArmOrdinary(RecoveryRangeOwner&,const RecoveryPanelControl&,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoverySessionIntentScope&,RecoveryEvidenceOwner&,ulong,double,double){return true;}
void RecoveryRangeOwnerOrdinaryTimer(RecoveryRangeOwner& u,RecoveryPanelControl&,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoverySessionIntentScope&,const RecoveryInventoryScope&,RecoverySessionAlternatingResult&){
 assert(u.phase==RRO_ORDINARY);++ordinary_reads;
}
void RecoveryRangeOwnerOrdinaryTrade(RecoveryRangeOwner& u,const RecoveryPanelControl&,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoverySessionIntentScope&,const RecoveryInventoryScope&,const MqlTradeTransaction& event){
 if(u.phase==RRO_ORDINARY&&event.range==u.id)++u.received;
}
"""
TEST=r"""
#include "RecoveryChartRuntime.mqh"
int main(){
 const RecoverySessionContext live{123,456,"server","XAUUSD.s"};
 const RRSettings settings{1,2,0,0};
 for(int winner : {1,2}) {
   RecoveryChartRuntime chart{};
   good=true;fail_publish=false;configure=false;proof_range=0;
   publishes=polls=submits=primary_reads=ordinary_reads=working=canceled=cancel_begins=0;
   canceled_ticket=0;
   auto invalid=settings;invalid.support_first_ticks=25;
   assert(!RecoveryChartStart(chart,live,"profile","seed","verifier",2,invalid,.01,.02));
   assert(!RecoveryChartStart(chart,live,"profile","seed","verifier",0,settings,.01,.02));
   assert(!chart.live_session.owns_mail&&publishes==0);
   assert(RecoveryChartStart(chart,live,"profile","seed","verifier",2,settings,.01,.02));
   assert(RecoveryChartOnTimer(chart,live)&&submits==0&&publishes==1);
   configure=true;fail_publish=true;
   assert(RecoveryChartOnTimer(chart,live)&&submits==2&&primary_reads==2&&ordinary_reads==0);
   assert(chart.publish_needed&&canceled==0);
   proof_range=winner;fail_publish=false;
   assert(RecoveryChartOnTimer(chart,live)&&submits==2&&primary_reads==2+winner&&ordinary_reads==1&&working==1);
   assert(!chart.publish_needed&&publishes==2&&chart.oco.winner_range_id==winner);
   const int loser=3-winner;
   assert(chart.ranges[loser-1].phase==RRO_PRIMARY_PENDING&&!chart.panel.configured[loser-1]);
   assert(canceled==1&&cancel_begins==1&&canceled_ticket==static_cast<ulong>(50+loser));
   assert(RecoveryChartOnTimer(chart,live)&&primary_reads==2+winner&&ordinary_reads==2);
   assert(canceled==1&&cancel_begins==1);
   RecoveryChartOnTrade(chart,live,{winner});assert(chart.ranges[winner-1].received==1);
   RecoveryChartOnTrade(chart,live,{loser});assert(chart.ranges[loser-1].received==0);
   good=false;
   RecoveryChartOnTrade(chart,live,{winner});assert(chart.ranges[winner-1].received==1);
   assert(!RecoveryChartOnTimer(chart,live)&&ordinary_reads==2);
   RecoveryChartStop(chart);
   assert(!chart.live_session.owns_mail&&chart.ranges[0].sent==0&&chart.oco.winner_range_id==0);
 }
 RecoveryChartRuntime single{};
 good=true;two_ranges=false;configure=true;proof_range=1;
 canceled=cancel_begins=0;
 const int before=submits;
 assert(RecoveryChartStart(single,live,"profile","single","verifier",2,settings,.01,.02));
 assert(RecoveryChartOnTimer(single,live));
 assert(submits==before+1&&single.oco.winner_range_id==1&&cancel_begins==0);
 assert(RecoveryChartOnTimer(single,live)&&canceled==0);
 RecoveryChartStop(single);
}
"""
class ChartRuntimeCompat(unittest.TestCase):
    def test_fresh_session_primary_and_ordinary_timer_flow(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)
            src=(HERE/"RecoveryChartRuntime.mqh").read_text()
            for cast in ("request.order_ticket","q.order_ticket","chart.oco.winner_order"):
                src=src.replace("(string)"+cast,"std::to_string("+cast+")")
            (path/"RecoveryChartRuntime.mqh").write_text("\n".join(x for x in src.splitlines() if not x.startswith("#include")))
            (path/"check.cpp").write_text(SHIM+TEST)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            str(path/"check.cpp"),"-o",str(path/"check")],check=True)
            subprocess.run([str(path/"check")],check=True)
if __name__=="__main__":unittest.main()
