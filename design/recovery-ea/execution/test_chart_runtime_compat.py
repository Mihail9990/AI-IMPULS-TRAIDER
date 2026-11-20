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
struct RecoverySessionContext {long login,magic;string server,symbol;long chart=99;};
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
enum {REC_CONTROL_IDLE,REC_CONTROL_RANGES,REC_CONTROL_MANUAL_WAIT,REC_CONTROL_ADOPTED,REC_CONTROL_PENDING_WAIT};
struct TestCycle {ulong position_id,generation,active_order;int range_id;long actual_entry_ticks;};
struct TestKnown {ulong ticket;};
struct RRRangeState {TestKnown entry,opposite;TestCycle cycle;ulong scope,position_ticket;long initial_sl,live_sl;int support_stage;struct {int kind;} pending;};
enum {RR_NONE=0};
using TestRange=RRRangeState;
struct RecoveryPanelControl {int mode;bool configured[2];TestRange ranges[2];RRSettings settings;};
struct RecoveryEvidenceOwner {string verifier_epoch;long verifier_chart;ulong serial;};
enum {RRO_UNUSED,RRO_PRIMARY_PENDING,RRO_ORDINARY,RRO_FINAL,RRO_PRIMARY_CLAIMED};
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
 struct PrimaryPump{RecoveryPrimaryFillBinding binding;};PrimaryPump primary_pump;TestOpening opening;ulong next_exact_ticket,armed_position,revision;int ledger;TestOrdinary ordinary;
};
struct RecoverySessionIntentScope {string executor_epoch,server,symbol;long login,magic;ulong scope,generation;};
struct RecoveryInventoryScope {string nonce,executor_epoch,verifier_epoch,server,symbol;long login,magic,generation,verifier_chart;};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
enum RecoveryOcoCancelResult {ROC_WAIT=0,ROC_REQUESTED,ROC_CANCELED,ROC_UNKNOWN};
struct RecoveryOcoCancel {int attempts,steps;};
int cancel_sends=0,canceled=0,cancel_begins=0,reverse_sends=0;
bool confirm_cancel=false;
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
 assert(c.attempts==1);
 if(c.steps++==0){++cancel_sends;return ROC_REQUESTED;}
 if(!confirm_cancel)return ROC_WAIT;
 ++canceled;return ROC_CANCELED;
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
bool RecoveryInvHex(const string& s){return s.size()==32;}
bool RCBoundedPositive(long n){return n>0;}
struct RecoverySupportRuntime {int x;};
enum {RCP_IDLE,RCP_DONE,RCP_UNRESOLVED,RC_INITIAL_LEVEL,RC_SUPPORT_LEVEL,RC_SUPPORT_SL,RC_FULL_SL,RR_ALT_CLOSE_APPLIED};
enum RecoveryClosePollResult{RCP_PROOF};
struct RecoveryRangeClosePoll{int state;ulong verified_sequence;};
struct RCCloseCheckpoint {int value,before,proof;struct {ulong scope;} key;};
enum {RSCO_IDLE,RSCO_REQUEST,RSCO_READ,RSCO_STAGED,RSCO_DISPATCHED};
struct RecoverySupportCloseOwner{int phase,reentry;RecoveryIntentKey key;ulong working_sequence;};
int reentry_stages=0,full_counts=0;
int RCClassifyClose(int,int proof){return proof;}
int RRRecordAlternatingClose(int& count,const RCCloseCheckpoint& re){
 assert(re.proof==RC_FULL_SL);++count;++full_counts;return RR_ALT_CLOSE_APPLIED;
}
bool RecoveryBeginSupportCloseOwner(RecoverySupportCloseOwner& task,const RecoveryRangeOwner&,
 const RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,RecoveryEvidenceOwner&,const RCCloseCheckpoint& proof,ulong seq,double,double){
 assert(seq==19&&proof.proof==RC_SUPPORT_SL);task.phase=RSCO_REQUEST;return true;
}
void RecoveryStepSupportCloseOwner(RecoverySupportCloseOwner& task,RecoveryRangeOwner&,
 RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryInventoryScope&){
 assert(task.phase==RSCO_REQUEST);task.phase=RSCO_STAGED;++reentry_stages;
}
struct RCProtectionFact {ulong scope,generation,position_id,anchor_order;int range_id,stop_class;
                         long stop_ticks;bool confirmed;};
int support_ticks=0,close_reads=0,manual_watches=0;
bool closing=false;int close_type=RC_SUPPORT_SL;
void RecoverySupportRuntimeTimer(RecoverySupportRuntime&,RecoveryEvidenceOwner&,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoverySessionIntentScope&,const RecoveryInventoryScope&,const string&,
 long watcher,RRRangeState& range,const RRSettings&,double,double,bool adopted,ulong magic){
 if(adopted){assert(magic==0);++manual_watches;}
 assert(watcher==77&&range.cycle.position_id!=0);
 ++support_ticks;range.support_stage=close_type==RC_SUPPORT_SL?2:0;range.live_sl=range.cycle.actual_entry_ticks;
}
bool RecoveryBeginRangeClosePoll(RecoveryRangeClosePoll& probe,RecoveryEvidenceOwner&,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoveryInventoryScope&,const RRRangeState&,const RRSettings&,double,double){
 if(!closing)return false;
 probe.state=22;return true;
}
RecoveryClosePollResult RecoveryRangeClosePollStep(RecoveryRangeClosePoll& probe,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RRRangeState&,const RRSettings&,const RCProtectionFact* f,RCCloseCheckpoint& out){
 assert(f[0].confirmed&&f[0].stop_class==RC_INITIAL_LEVEL&&f[0].stop_ticks==399920);
 assert(f[1].stop_class==RC_SUPPORT_LEVEL&&f[1].stop_ticks==(close_type==RC_SUPPORT_SL?400000:0));
 ++close_reads;out.value=1;out.proof=close_type;probe.state=RCP_DONE;probe.verified_sequence=19;return RCP_PROOF;
}

enum { RSRE_IDLE,RSRE_RECEIPT,RSRE_REQUEST,RSRE_READ,RSRE_WORKING,RSRE_UNRESOLVED };
enum { RDS_IDLE,RDS_LOCAL,RDS_DONE,RDS_STOP,RDS_WAIT,RDS_PROOF };
enum { RSRM_IDLE,RSRM_SEND,RSRM_REQUEST,RSRM_DONE,RSRM_UNRESOLVED };
struct RecoverySupportReentry {int state;ulong ticket;};
struct RecoverySupportRotationOwner {int phase;};
struct RecoveryDualSupportPump {int state;ulong verified_sequence;};
struct RecoveryDualSupportBinding {
 RecoveryExactReportScope entry,opposite;
 struct {RecoveryInventoryScope binding;} history;
 RecoveryInventoryScope inventory;
 string publisher_tag;
 ulong verify_after;
 double price_tick,volume_step;
};
using RRKnownOrder=TestKnown;
struct RREffect {ulong created_order;};
bool fast_entry=false,emit_dual=false;
int submits_reentry=0,rotation_calls=0,dual_begins=0;
bool RecoverySubmitSupportReentry(RecoverySupportReentry& t,RecoverySupportCloseOwner& c,RecoveryRangeOwner&,
 const RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryInventoryScope&) {
 assert(c.phase==RSCO_STAGED);c.phase=RSCO_DISPATCHED;t.ticket=92;c.key.id="support";
 c.working_sequence=20;t.state=RSRE_RECEIPT;++submits_reentry;return true;
}
bool RecoveryBeginSupportReentryVerification(RecoverySupportReentry& t,const RecoverySupportCloseOwner&,
 const RecoveryRangeOwner&,const RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryInventoryScope&,RecoveryEvidenceOwner&) {
 assert(t.state==RSRE_RECEIPT);t.state=RSRE_REQUEST;return true;
}
void RecoveryStepSupportReentry(RecoverySupportReentry& t,const RecoverySupportCloseOwner&,
 RecoveryRangeOwner& r,RecoveryPanelControl& p,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryInventoryScope&) {
 if(t.state==RSRE_REQUEST){t.state=RSRE_READ;return;}
 auto& s=p.ranges[r.id-1];
 s.cycle.position_id=0;
 if(fast_entry){t.state=RSRE_UNRESOLVED;return;}
 s.entry.ticket=92;s.opposite.ticket=77;t.state=RSRE_WORKING;
}
bool RRHasSupportAlternatives(const RRRangeState& s){return s.entry.ticket!=0&&s.opposite.ticket!=0;}
bool RecoveryDualPendingReentryView(const RRRangeState& s,const RRSettings&,const RecoverySessionIntentScope&,
 int,const RecoveryIntentKey& k,ulong t,double,double,RRRangeState& v) {
 assert(k.id=="support"&&t==92);
 v=s;v.entry.ticket=t;v.opposite.ticket=77;return true;
}
bool RecoveryBeginDualSupport(RecoveryDualSupportPump& p,const RRRangeState&,const RRSettings&,
 const RecoveryDualSupportBinding& b){
 assert(b.entry.order_ticket==92&&b.opposite.order_ticket==77&&b.verify_after==20);
 p.state=RDS_LOCAL;++dual_begins;return true;
}
int RecoveryStepDualSupport(RecoveryDualSupportPump& p,const RRRangeState&,const RRSettings&,RRFilled& f,RRKnownOrder& o){
 if(!emit_dual)return RDS_WAIT;
 p.state=RDS_DONE;p.verified_sequence=44;f.entry.executed_order=92;o.ticket=77;return RDS_PROOF;
}
bool RecoveryDualPendingReentryEffect(const RecoveryDualSupportPump& p,const RRRangeState&,const RRSettings&,
 const RecoverySessionIntentScope&,int,const RecoveryIntentKey&,ulong t,RREffect& e) {
 assert(fast_entry&&p.state==RDS_DONE);e.created_order=t;return true;
}
bool RRConfirm(RRRangeState& s,const RREffect& e){s.entry.ticket=e.created_order;return true;}
bool RecoveryBeginSupportRotationOwner(RecoverySupportRotationOwner& t,RecoveryRangeOwner& u,
 RecoveryPanelControl& panel,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,RecoveryEvidenceOwner&,const RecoveryInventoryScope&,
 const RRFilled& f,const RRKnownOrder& s,ulong seq,double,double) {
 assert(panel.ranges[u.id-1].entry.ticket==f.entry.executed_order&&s.ticket==77&&seq==44);
 t.phase=RSRM_SEND;++rotation_calls;return true;
}
void RecoveryStepSupportRotationOwner(RecoverySupportRotationOwner& t,RecoveryRangeOwner&,
 RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryInventoryScope&) {
 if(t.phase==RSRM_SEND)t.phase=RSRM_REQUEST;
 else if(t.phase==RSRM_REQUEST)t.phase=RSRM_DONE;
}
struct RecoveryManualChart {bool attempted;};
enum {RMC_WAIT,RMC_PROOF};
bool manual_arm=false,manual_cancel=false,manual_attempt=false,manual_proof=false;
int manual_calls=0;
bool RecoveryManualChartMayCancel(const RecoveryManualChart& t){return !t.attempted;}
bool RecoveryManualChartClearWaiting(RecoveryManualChart& t){
 if(t.attempted)return false;
 t={};return true;
}
int RecoveryManualChartTimer(RecoveryManualChart& t,RecoveryPanelControl& p,bool active,RecoveryEvidenceOwner&,
 const RecoverySessionLifecycle&,const RecoverySessionContext&,const RecoverySessionIntentScope&,
 const RecoveryInventoryScope&,const RRSettings&,double,double){
 assert(p.mode==REC_CONTROL_MANUAL_WAIT&&!active);
 ++manual_calls;
 if(manual_attempt)t.attempted=true;
 if(manual_proof){assert(t.attempted);p.mode=REC_CONTROL_ADOPTED;return RMC_PROOF;}
 return RMC_WAIT;
}
struct RecoveryManualHandoff{bool seeded;RecoveryRangeOwner ordinary;RecoveryPanelControl backing;};
int manual_handoffs=0,manual_events=0;
bool RecoveryManualHandoffOrigin(const RecoveryManualHandoff& h,const RRRangeState& r,ulong& m){
 assert(h.seeded&&r.cycle.position_id==101);m=0;return true;
}
bool RecoverySeedManualHandoff(RecoveryManualHandoff& h,const RecoveryManualChart& t,
 const RecoveryPanelControl& p,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RRSettings& cfg) {
 assert(t.attempted&&p.mode==REC_CONTROL_ADOPTED&& !p.configured[0]&&!p.configured[1]&&!h.seeded);
 h.seeded=true;h.ordinary.phase=RRO_ORDINARY;h.ordinary.id=1;
 h.backing.mode=REC_CONTROL_RANGES;h.backing.settings=cfg;h.backing.configured[0]=true;
 h.backing.ranges[0].cycle.position_id=101;h.backing.ranges[0].position_ticket=102;
 ++manual_handoffs;return true;
}
struct RecoverySessionAlternatingResult{};
struct RecoveryDetachedCloseAccounting {RCCloseCheckpoint completed[24];};
int detached_merges=0;
bool detached_proof=false;
void RecoveryDetachedCloseAccountingTimer(RecoveryDetachedCloseAccounting& t,const RecoveryRangeOwner&,
 const RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,const RecoverySessionIntentScope&,
 RecoveryEvidenceOwner&,const RecoveryInventoryScope&,double,double,RCCloseCheckpoint&) {
 if(detached_proof){t.completed[0].key.scope=1;detached_proof=false;}
}
bool RecoveryCommitDetachedCloseAccounting(const RecoveryDetachedCloseAccounting&,RecoveryRangeOwner&,
 const RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RCCloseCheckpoint& p){
 assert(p.key.scope==1);++detached_merges;return true;
}

struct RecoveryManualPendingChart {int state;};
bool pending_arm=false,pending_proof=false;
int pending_calls=0,pending_opening=0;
void RecoveryManualPendingChartReset(RecoveryManualPendingChart& p){p={};}
bool RecoveryManualPendingChartTimer(RecoveryManualPendingChart& in,RecoveryManualHandoff& h,
 RecoveryPanelControl& p,RecoveryEvidenceOwner&,const RecoverySessionLifecycle& life,
 const RecoverySessionContext& live,const RecoverySessionIntentScope& scope,const RecoveryInventoryScope&,
 const RRSettings& cfg,double,double){
 assert(p.mode==REC_CONTROL_PENDING_WAIT);++pending_calls;in.state=1;
 if(!pending_proof)return false;
 p.mode=REC_CONTROL_ADOPTED;
 RecoveryManualChart m{true};
 return RecoverySeedManualHandoff(h,m,p,life,live,scope,cfg);
}
void RecoveryManualPendingChartConfirmOpening(RecoveryManualPendingChart& in,RecoveryManualHandoff&,
 const RecoverySessionLifecycle&,const RecoverySessionContext&,const RecoverySessionIntentScope&){
 if(in.state)++pending_opening;
}
struct RecoveryManualFastFill {int state;};
enum {RAS_NO_EFFECT,RAS_ADVANCED};
bool manual_fast_proof=false;
int manual_fast_paths=0,manual_fast_events=0;
int RecoveryManualFastFillTimer(RecoveryManualFastFill& f,const RecoveryManualChart& m,RecoveryManualHandoff& h,
 RecoveryPanelControl& p,const RecoverySessionLifecycle& life,const RecoverySessionContext& live,
 const RecoverySessionIntentScope& scope,RecoveryEvidenceOwner&,const RecoveryInventoryScope&,
 const RRSettings& cfg,double,double,RecoverySessionAlternatingResult&) {
 assert(p.mode==REC_CONTROL_MANUAL_WAIT);
 if(!m.attempted)return RAS_NO_EFFECT;
 ++manual_fast_paths;f.state=1;
 if(!manual_fast_proof)return RAS_NO_EFFECT;
 p.mode=REC_CONTROL_ADOPTED;assert(RecoverySeedManualHandoff(h,m,p,life,live,scope,cfg));
 return RAS_ADVANCED;
}

enum {RSMF_IDLE,RSMF_REQUEST,RSMF_DONE};
enum {RSG_IDLE,RSG_PROBE,RSG_STAGED,RSG_SENT,RSG_FINAL_STATE};
struct RecoverySupportMoveFill {int state;};
struct RecoverySupportGapOwner {int state;};
bool gap_hint=false,survivor_hint=false;
int gap_sends=0,survivor_proofs=0;
bool RecoveryBeginSupportMoveFill(RecoverySupportMoveFill& t,const RecoverySupportRotationOwner&,
 const RecoveryRangeOwner&,const RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryInventoryScope&,RecoveryEvidenceOwner&){
 if(!survivor_hint)return false;
 t.state=RSMF_REQUEST;return true;
}
void RecoveryStepSupportMoveFill(RecoverySupportMoveFill& t,RecoverySupportRotationOwner& rot,
 RecoveryRangeOwner&,RecoveryPanelControl&,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryInventoryScope&,RecoverySessionAlternatingResult&){
 assert(t.state==RSMF_REQUEST);++survivor_proofs;t.state=RSMF_DONE;rot.phase=RSRM_DONE;
}
bool RecoveryBeginSupportGapOwner(RecoverySupportGapOwner& t,const RecoverySupportCloseOwner& c,
 const RecoverySupportReentry&,const RecoveryRangeOwner&,const RecoveryPanelControl&,
 const RecoverySessionLifecycle&,const RecoverySessionContext&,const RecoverySessionIntentScope&,
 RecoveryEvidenceOwner&,const RecoveryInventoryScope&,double,double){
 if(!gap_hint||c.phase!=RSCO_STAGED)return false;
 t.state=RSG_PROBE;return true;
}
void RecoveryStepSupportGapOwner(RecoverySupportGapOwner& t,const RecoverySupportCloseOwner&,
 const RecoverySupportReentry&,RecoveryRangeOwner&,RecoveryPanelControl&,const RecoverySessionLifecycle&,
 const RecoverySessionContext&,const RecoverySessionIntentScope&,const RecoveryInventoryScope&){
 assert(t.state==RSG_PROBE);t.state=RSG_STAGED;
}
void RecoveryDispatchSupportGapOwner(RecoverySupportGapOwner& t,RecoveryRangeOwner&,const RecoveryPanelControl&,
 const RecoverySessionLifecycle&,const RecoverySessionContext&,const RecoverySessionIntentScope&){
 assert(t.state==RSG_STAGED);t.state=RSG_SENT;++gap_sends;
}

struct RecoveryPanelApplyResult{};
struct MqlTradeTransaction {int range;};
bool RecoveryManualFastFillTrade(RecoveryManualFastFill& f,const RecoveryManualChart&,
 const RecoveryPanelControl& p,const RecoverySessionLifecycle&,const RecoverySessionContext&,
 const RecoverySessionIntentScope&,const RecoveryInventoryScope&,const MqlTradeTransaction&) {
 assert(p.mode==REC_CONTROL_MANUAL_WAIT);
 if(!f.state)return false;
 ++manual_fast_events;return true;
}
bool RecoveryManualHandoffTrade(RecoveryManualHandoff& h,const RecoveryPanelControl& p,
 const RecoverySessionLifecycle&,const RecoverySessionContext&,const RecoverySessionIntentScope&,
 const RecoveryInventoryScope&,const MqlTradeTransaction&) {
 assert(h.seeded&&p.mode==REC_CONTROL_ADOPTED);++manual_events;return true;
}
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
 double tick,int&,RecoveryPanelControl& panel,RecoveryPanelApplyResult&,bool diagnostic,bool may_cancel) {
 assert(tick==.01&&!diagnostic);++polls;
 if(pending_arm){pending_arm=false;panel.mode=REC_CONTROL_PENDING_WAIT;return true;}
 if(manual_arm){manual_arm=false;panel.mode=REC_CONTROL_MANUAL_WAIT;return true;}
 if(manual_cancel){manual_cancel=false;if(may_cancel)panel.mode=REC_CONTROL_IDLE;return true;}
 if(!configure)return false;
 configure=false;panel.mode=REC_CONTROL_RANGES;
 panel.configured[0]=true;panel.configured[1]=two_ranges;panel.ranges[0].cycle.position_id=10;panel.ranges[1].cycle.position_id=11;
 for(auto &range:panel.ranges) {
   range.position_ticket=13;range.cycle.actual_entry_ticks=400000;range.initial_sl=399920;
 }
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
  u.phase=RRO_PRIMARY_CLAIMED;
 }
}
bool RecoveryRangeOwnerDispatchClaimedPrimary(RecoveryRangeOwner& u,RecoveryPanelControl& panel,
 const RecoverySessionLifecycle&,const RecoverySessionContext&,const RecoverySessionIntentScope&,
 const RecoveryRangeOco& oco){
 assert(u.phase==RRO_PRIMARY_CLAIMED&&oco.winner_range_id==u.id);
 // Simulate an arbitrarily slow synchronous winner broker response by
 // observing the state when entering send: cancel already submitted,
 // independent verifier has not responded and need not have.
 assert(cancel_sends==int(panel.configured[2-u.id])&&canceled==0);
 ++reverse_sends;u.phase=RRO_ORDINARY;u.opening.reserved=true;u.next_exact_ticket=77;
 return true;
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
   publishes=polls=submits=primary_reads=ordinary_reads=working=canceled=cancel_begins=cancel_sends=reverse_sends=0;confirm_cancel=false;
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
   assert(chart.ranges[loser-1].phase==RRO_PRIMARY_PENDING&&chart.panel.configured[loser-1]);
   assert(cancel_sends==1&&reverse_sends==1&&canceled==0&&cancel_begins==1&&canceled_ticket==static_cast<ulong>(50+loser));
   confirm_cancel=true;
   assert(RecoveryChartOnTimer(chart,live)&&primary_reads==2+winner&&ordinary_reads==2);
   assert(canceled==1&&cancel_begins==1&&!chart.panel.configured[loser-1]);
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
 canceled=cancel_begins=cancel_sends=reverse_sends=0;confirm_cancel=false;
 const int before=submits;
 assert(RecoveryChartStart(single,live,"profile","single","verifier",2,settings,.01,.02,string(32,'a'),77));
 assert(RecoveryChartOnTimer(single,live));
 assert(submits==before+1&&single.oco.winner_range_id==1&&cancel_begins==0);
 assert(support_ticks==1&&close_reads==0);
 closing=true;
 assert(RecoveryChartOnTimer(single,live)&&canceled==0);
 assert(support_ticks==2&&close_reads==1&&single.close_proof[0].value==1);
 assert(RecoveryChartOnTimer(single,live)&&close_reads==1&&reentry_stages==1);
 assert(RecoveryChartOnTimer(single,live)&&reentry_stages==1);
 assert(submits_reentry==1&&dual_begins==1);
 emit_dual=true;
 detached_proof=true;
 assert(RecoveryChartOnTimer(single,live)&&rotation_calls==1);
 assert(single.detached_close[0].completed[0].key.scope==1&&detached_merges==0);
 assert(RecoveryChartOnTimer(single,live)&&single.rotation[0].phase==RSRM_DONE);
 emit_dual=false;
 close_type=RC_FULL_SL;
 single.panel.ranges[0].cycle.position_id=90;
 assert(RecoveryChartOnTimer(single,live)&&full_counts==1&&reentry_stages==1);
 assert(detached_merges==1);
 RecoveryChartStop(single);
 // Journal-backed fast fill arrives before PLACED proof.
 fast_entry=true;close_type=RC_SUPPORT_SL;closing=false;configure=true;
 assert(RecoveryChartStart(single,live,"profile","fast","verifier",2,settings,.01,.02,string(32,'a'),77));
 assert(RecoveryChartOnTimer(single,live));
 closing=true;
 assert(RecoveryChartOnTimer(single,live)&&RecoveryChartOnTimer(single,live));
 emit_dual=true;
 assert(RecoveryChartOnTimer(single,live)&&rotation_calls==2&&submits_reentry==2);
 survivor_hint=true;
 assert(RecoveryChartOnTimer(single,live)&&survivor_proofs==1&&single.rotation[0].phase==RSRM_DONE);
 RecoveryChartStop(single);
 survivor_hint=false;gap_hint=true;configure=true;closing=false;emit_dual=false;
 assert(RecoveryChartStart(single,live,"profile","gap","verifier",2,settings,.01,.02,string(32,'a'),77));
 assert(RecoveryChartOnTimer(single,live));
 closing=true;
 assert(RecoveryChartOnTimer(single,live)&&RecoveryChartOnTimer(single,live));
 assert(gap_sends==1&&submits_reentry==2);
 RecoveryChartStop(single);
 gap_hint=false;
 RecoveryChartRuntime wait{};
 assert(RecoveryChartStart(wait,live,"profile","manual","verifier",2,settings,.01,.02,string(32,'a'),77));
 closing=false;manual_arm=true;
 assert(RecoveryChartOnTimer(wait,live)&&wait.panel.mode==REC_CONTROL_MANUAL_WAIT&&manual_calls==1);
 manual_cancel=true;
 assert(RecoveryChartOnTimer(wait,live)&&wait.panel.mode==REC_CONTROL_IDLE&&manual_calls==1);
 manual_arm=true;manual_attempt=true;
 assert(RecoveryChartOnTimer(wait,live)&&wait.manual.attempted&&manual_calls==2);
 manual_cancel=true;
 assert(RecoveryChartOnTimer(wait,live)&&wait.panel.mode==REC_CONTROL_MANUAL_WAIT&&manual_calls==3);
 manual_proof=true;
 assert(RecoveryChartOnTimer(wait,live)&&wait.panel.mode==REC_CONTROL_ADOPTED&&manual_calls==4);
 assert(RecoveryChartOnTimer(wait,live)&&manual_calls==4);
 assert(manual_handoffs==1&&manual_watches==2&&wait.ranges[0].phase==RRO_UNUSED);
 RecoveryChartOnTrade(wait,live,{1});
 assert(manual_events==1);
 RecoveryChartStop(wait);
 assert(RecoveryChartStart(wait,live,"profile","manual-fill-first","verifier",2,settings,.01,.02,string(32,'a'),77));
 manual_proof=false;manual_arm=true;manual_attempt=true;
 assert(RecoveryChartOnTimer(wait,live)&&wait.panel.mode==REC_CONTROL_MANUAL_WAIT&&!wait.adopted.seeded);
 RecoveryChartOnTrade(wait,live,{1});
 assert(manual_fast_events==1);
 const int calls=manual_fast_paths;
 manual_fast_proof=true;
 assert(RecoveryChartOnTimer(wait,live)&&wait.panel.mode==REC_CONTROL_ADOPTED&&wait.adopted.seeded);
 assert(RecoveryChartOnTimer(wait,live)&&manual_fast_paths==calls+1&&wait.ranges[0].phase==RRO_UNUSED);
 RecoveryChartStop(wait);
 assert(RecoveryChartStart(wait,live,"profile","pending-manual","verifier",2,settings,.01,.02,string(32,'a'),77));
 pending_arm=true;
 assert(RecoveryChartOnTimer(wait,live)&&pending_calls==1&&wait.panel.mode==REC_CONTROL_PENDING_WAIT);
 manual_cancel=true;
 assert(RecoveryChartOnTimer(wait,live)&&pending_calls==1&&wait.panel.mode==REC_CONTROL_IDLE&&!wait.pending_manual.state);
 pending_arm=true;
 assert(RecoveryChartOnTimer(wait,live)&&pending_calls==2&&wait.panel.mode==REC_CONTROL_PENDING_WAIT);
 pending_proof=true;
 assert(RecoveryChartOnTimer(wait,live)&&pending_calls==3&&wait.panel.mode==REC_CONTROL_ADOPTED&&pending_opening==1);
 assert(wait.adopted.seeded&&!wait.panel.configured[0]&&!wait.panel.configured[1]);
 RecoveryChartStop(wait);

}
"""
class ChartRuntimeCompat(unittest.TestCase):
    def test_fresh_session_primary_and_ordinary_timer_flow(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)
            src=(HERE/"RecoveryChartRuntime.mqh").read_text()
            for cast in ("request.order_ticket","q.order_ticket","chart.oco.winner_order","serial"):
                src=src.replace("(string)"+cast,"std::to_string("+cast+")")
            (path/"RecoveryChartRuntime.mqh").write_text("\n".join(x for x in src.splitlines() if not x.startswith("#include")))
            (path/"check.cpp").write_text(SHIM+TEST)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            str(path/"check.cpp"),"-o",str(path/"check")],check=True)
            subprocess.run([str(path/"check")],check=True)
if __name__=="__main__":unittest.main()
