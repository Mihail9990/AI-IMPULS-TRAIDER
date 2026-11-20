#include <cassert>
#include <climits>
#include <cmath>
#include <cstdint>
#include <string>
using string=std::string;
using ulong=std::uint64_t;
template<class T> void ZeroMemory(T &x){x=T{};}
enum {RR_NONE,RR_REENTRY,RR_ACTIVE,RR_ARM_REENTRY,RRO_ORDINARY,RRO_FINAL,RSRE_IDLE,RSRE_UNRESOLVED,
      RD_NOT_SENT,RD_RESERVED_NOT_SENT,RD_OUTCOME_UNRESOLVED,
      RECOVERY_EXACT_ORDER_OBSERVED,RECOVERY_EXACT_ORDER_CURRENT,RECOVERY_EXACT_ORDER_HISTORY,
      ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,ORDER_STATE_PLACED=1,ORDER_STATE_FILLED=4};
enum RecoveryIntentState {RI_NO_RECORD,RI_UNRESOLVED,RI_TICKET};
enum RecoverySupportClosePhase {RSCO_REQUEST,RSCO_READ,RSCO_UNRESOLVED,RSCO_STAGED,RSCO_DISPATCHED};
enum RecoveryAlternatingPumpResult {RAP_WAIT,RAP_PROGRESS,RAP_PROOF,RAP_STOPPED};
enum RRAlternatingCloseResult {RR_ALT_CLOSE_APPLIED,RR_ALT_CLOSE_DUPLICATE,RR_ALT_CLOSE_UNRESOLVED};
enum RRSupportGapResult {RR_GAP_UNRESOLVED,RR_GAP_NEXT_PAIR,RR_GAP_FINAL_PROTECTED};
struct RRAction {int kind;long serial;};
struct RRKnownOrder {ulong ticket;int side;long entry_ticks,sl_ticks,volume_steps;};
struct RCRange {ulong generation,position_id,active_order;int range_id,slot;};
struct RRRangeState {
 RCRange cycle;ulong position_ticket,action_serial;
 int phase,support_stage,reentry_slot,reentry_side;
 long live_sl,initial_sl,live_tp,opposite_anchor_ticks,reentry_ticks;
 RRKnownOrder entry,retire,opposite;RRAction pending;
};
struct RRSettings {long initial_sl_ticks,support_first_ticks,support_offset_ticks;};
struct RCCloseCheckpoint {long key,reentry_distance;};
struct RRFilled {struct {ulong position_id,executed_order;} entry;};
struct RRSupportGap {
 bool reserved,have_close,have_fill;int support_stage;
 RCRange before;ulong position_ticket;long support_level,initial_level,tp,opposite_anchor;
 RRKnownOrder opposite;RCCloseCheckpoint close;
};
struct RecoverySessionLifecycle {bool active;};
struct RecoverySessionContext {long login,magic;string server,symbol;};
struct RecoverySessionIntentScope {string executor_epoch;};
struct RecoveryInventoryScope {string epoch;};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
struct RecoveryExactEffectBinding {
 RecoveryExactReportScope request;double price_tick,volume_step;ulong verify_after;
};
struct RecoveryIntentKey {string id;};
struct RecoveryDispatchResult {int state;ulong order;int send_retcode;bool async_sl_submitted;};
struct RecoverySupportCloseOwner {
 RecoverySupportClosePhase phase;RRSupportGap gap;RRAction reentry;RecoveryIntentKey key;
 RecoveryExactEffectBinding working;ulong owner_revision,working_sequence;
};
struct RecoverySupportReentry {int state;ulong ticket,revision;RecoveryDispatchResult receipt;};
struct RROpeningPair {bool reserved,sl_required;RRAction reverse;};
struct RRAlternationLedger {
 int confirmed_full_sl_count,position_count,strategy_slot;
 struct {RCRange bound_entry;} positions[24];
};
struct RecoveryRangeOwner {
 int phase,range_id;ulong revision,next_exact_ticket,armed_position;
 int ordinary;RROpeningPair opening;RRAlternationLedger ledger;
};
struct RecoveryPanelControl {RRRangeState ranges[2];RRSettings settings;};
struct RecoveryEvidenceOwner {int marker;};
struct RecoveryAlternatingEvidenceBinding {RecoveryInventoryScope inventory;double price_tick,volume_step;};
struct RecoveryOwnerEvidence {RecoveryAlternatingEvidenceBinding binding;string publisher_tag;};
struct RecoveryAlternatingPump {RecoveryAlternatingEvidenceBinding binding;};
struct RRAlternatingObservation {
 bool predecessor_inventory_complete,predecessor_present;
 RRKnownOrder executed_order;RRFilled fill;
};
struct RecoveryExactOrderScope {long login;string server,symbol;ulong magic,ticket;};
struct RecoveryExactOrderObservation {
 int source,state;ulong ticket,magic,position_identifier;string symbol;int type;
 double tp,stop_limit,price,sl,initial_volume,remaining_volume;
};
RecoveryIntentState journal=RI_NO_RECORD;
RecoveryIntentState RecoveryIntentLookup(const RecoveryIntentKey &,string &value){
 value=journal==RI_TICKET?"123":"";
 return journal;
}
bool local_filled=false,predecessor_present=false,proof_valid=true;
int reserve=0,pump_steps=0,sends=0,advances=0;
RecoveryAlternatingPumpResult next_step=RAP_WAIT;
const RRKnownOrder opposite{9007199254740997ULL,-1,399920,400000,1};
const ulong successor=9007199254740999ULL;
bool RecoveryRangeOwnerBound(const RecoveryRangeOwner &o,const RecoveryPanelControl &,
    const RecoverySessionLifecycle &s,const RecoverySessionContext &,const RecoverySessionIntentScope &){
 return s.active&&o.phase==RRO_ORDINARY&&o.range_id==1;
}
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b){return a.epoch==b.epoch;}
bool RecoverySupportCloseOwnerBound(const RecoverySupportCloseOwner &c,const RecoveryRangeOwner &o,
    const RecoveryPanelControl &,const RecoverySessionLifecycle &,const RecoverySessionContext &,
    const RecoverySessionIntentScope &,const RecoveryInventoryScope &){return c.gap.reserved&&o.revision==c.owner_revision;}
bool RecoverySupportReentryBound(const RecoverySupportCloseOwner &c,const RecoveryRangeOwner &o,
    const RecoveryPanelControl &p,const RecoverySessionLifecycle &,const RecoverySessionContext &,
    const RecoverySessionIntentScope &,const RecoveryInventoryScope &){
 return !c.gap.reserved&&o.revision==c.owner_revision+1&&p.ranges[0].phase==RR_ARM_REENTRY;
}
void RRClearKnown(RRKnownOrder &k){k={};}
void RRClearAction(RRAction &a){a={};}
bool RRSupportGapBound(const RRRangeState &s,const RRSupportGap &g){
 return g.reserved&&s.phase==RR_ACTIVE&&s.pending.kind==RR_NONE&&s.cycle.position_id==g.before.position_id&&
        s.live_sl==g.support_level&&s.opposite.ticket==g.opposite.ticket;
}
bool RRAltBound(const RRRangeState &s,const RRAlternationLedger &l){
 return l.position_count==1&&l.positions[0].bound_entry.position_id==s.cycle.position_id&&l.strategy_slot==s.cycle.slot;
}
int RecoveryObserveExactOrder(const RecoveryExactOrderScope &s,RecoveryExactOrderObservation &r){
 assert(s.ticket==opposite.ticket&&s.magic==700);
 r={local_filled?RECOVERY_EXACT_ORDER_HISTORY:RECOVERY_EXACT_ORDER_CURRENT,
    local_filled?ORDER_STATE_FILLED:ORDER_STATE_PLACED,s.ticket,s.magic,local_filled?successor:0,
    s.symbol,ORDER_TYPE_SELL_STOP,0,0,3999.20,4000,.01,local_filled?0:.01};
 return RECOVERY_EXACT_ORDER_OBSERVED;
}
bool RecoveryDispatchUnits(double d,double q,bool,long expect){return std::lround(d/q)==expect;}
bool RecoveryReserveOwnerEvidence(RecoveryEvidenceOwner &,const RecoverySessionLifecycle &,
    const RecoverySessionContext &,const RRRangeState &r,const RROpeningPair &,ulong t,ulong floor,double tick,
    double step,RecoveryOwnerEvidence &out){
 assert(r.phase==RR_ACTIVE&&r.position_ticket==45&&t==opposite.ticket&&(floor==10||floor==12));
 ++reserve;out={{"epoch",tick,step},"tag"};return true;
}
bool RecoveryAlternatingPumpBegin(RecoveryAlternatingPump &p,const RRRangeState &,const RROpeningPair &,
    const RecoveryAlternatingEvidenceBinding &b,const string &){p.binding=b;return true;}
bool RCCloseKeysEqual(long a,long b){return a==b;}
RecoveryAlternatingPumpResult RecoveryAlternatingPumpStep(RecoveryAlternatingPump &,const RRRangeState &,
    const RROpeningPair &,RRAlternatingObservation &o) {
 ++pump_steps;o={true,predecessor_present,opposite,{{successor,opposite.ticket}}};
 return next_step;
}
bool RRStageSupportGapFill(const RRRangeState &,RRSupportGap &g,const RRKnownOrder &,
     const RRFilled &){
 if(!proof_valid)return false;
 g.have_fill=true;return true;
}
RRAlternatingCloseResult RRRecordAlternatingClose(RRAlternationLedger &,const RCCloseCheckpoint &){
 return RR_ALT_CLOSE_APPLIED;
}
RRSupportGapResult RRAdvanceSupportGap(RRRangeState &r,const RRSettings &,RRSupportGap &g,RROpeningPair &p,RRAction &){
 assert(g.have_close&&g.have_fill);++advances;
 r.cycle.position_id=successor;r.cycle.active_order=opposite.ticket;
 p={true,false,{5,static_cast<long>(r.action_serial+1)}};
 return RR_GAP_NEXT_PAIR;
}
int RCCurrentSlot(const RCRange &s){return s.slot;}
void RRAltCapture(const RRRangeState &s,decltype(RRAlternationLedger::positions[0]) &p){p.bound_entry=s.cycle;}
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope &,const RRAction &a,double,double,RecoveryIntentKey &k){
 k.id=std::to_string(a.serial);return true;
}
bool RecoveryIntentContextOK(const RecoveryIntentKey &){return true;}
bool RecoveryIntentKeysMatch(const RecoveryIntentKey &a,const RecoveryIntentKey &b){return a.id==b.id;}
void RecoveryOrdinaryEventReset(int &r){r=0;}
void RecoveryContinueOpeningPair(const RRRangeState &r,const RROpeningPair &o,const RecoveryIntentKey &key,
   double,double,RecoveryDispatchResult &result){
 assert(r.cycle.position_id==successor&&o.reserved&&key.id==std::to_string(o.reverse.serial));
 ++sends;result={3,901,0,false};
}
ulong RecoveryRangeOwnerReceiptTicket(const RecoveryDispatchResult &r){return r.order;}
#include "GapOwnerUnderTest"
int main(){
 RecoverySessionLifecycle session{true};
 RecoverySessionContext live{5,700,"Test","XAUUSD.s"};
 RecoverySessionIntentScope scope{"epoch"};
 RecoveryInventoryScope context{"epoch"};
 RecoveryEvidenceOwner verifier{};
 RecoveryRangeOwner owner{};owner.phase=RRO_ORDINARY;owner.range_id=1;owner.revision=4;
 RecoveryPanelControl panel{};panel.settings={80,0,0};
 RRRangeState original{};original.cycle={7,44,43,1,1};original.position_ticket=45;
 original.phase=RR_ACTIVE;original.action_serial=8;original.support_stage=2;original.live_sl=400000;
 original.initial_sl=399920;original.opposite=opposite;
 panel.ranges[0]=original;
 owner.ledger.position_count=1;owner.ledger.strategy_slot=1;owner.ledger.positions[0].bound_entry=original.cycle;
 RecoverySupportCloseOwner close{RSCO_READ,{true,true,false,2,original.cycle,45,400000,399920,0,399920,
     opposite,{19,80}},{},{},{{context,opposite.ticket},.01,.01,10},4,0};
 RecoverySupportReentry repeat{RSRE_IDLE,0,0,{}};
 RecoverySupportGapOwner task{};
 auto begin=[&](){return RecoveryBeginSupportGapOwner(task,close,repeat,owner,panel,session,live,scope,verifier,context,.01,.01);};
 auto step=[&](){return RecoveryStepSupportGapOwner(task,close,repeat,owner,panel,session,live,scope,context);};
 assert(!begin() && reserve==0 && RecoverySupportGapFixed(close,live)==RSG_FIXED_WORKING);
 local_filled=true;
 assert(begin() && reserve==1 && RecoverySupportGapFixed(close,live)==RSG_FIXED_HISTORICAL);
 assert(step()==RSG_WAIT && !advances && !sends);
 next_step=RAP_PROOF;predecessor_present=true;
 assert(step()==RSG_UNKNOWN && !advances && !sends);
 predecessor_present=false;task={};assert(begin());
 assert(step()==RSG_READY && owner.revision==5 && owner.ledger.strategy_slot==1 &&
        panel.ranges[0].cycle.position_id==successor && advances==1 && !sends);
 assert(RecoveryDispatchSupportGapOwner(task,owner,panel,session,live,scope) && owner.next_exact_ticket==901);
 assert(!RecoveryDispatchSupportGapOwner(task,owner,panel,session,live,scope) && sends==1);

 // Same verified support close, with a repeat staged or conclusively blocked
 // before send. Presence of no ticket alone never unlocks this path.
 panel.ranges[0]=original;
 panel.ranges[0].cycle.position_id=0;panel.ranges[0].position_ticket=0;
 panel.ranges[0].phase=RR_ARM_REENTRY;panel.ranges[0].action_serial=9;
 close.gap.reserved=false;close.reentry={RR_REENTRY,9};close.key.id="9";close.working_sequence=12;
 owner.opening={};owner.ledger.positions[0].bound_entry=original.cycle;task={};
 close.phase=RSCO_DISPATCHED;
 repeat={RSRE_UNRESOLVED,0,5,{RD_OUTCOME_UNRESOLVED,0,0,false}};
 journal=RI_UNRESOLVED;assert(!begin());
 repeat.receipt.state=RD_NOT_SENT;assert(!begin());journal=RI_NO_RECORD;assert(RecoverySupportGapNoRepeat(close,repeat));
 repeat.receipt.state=RD_RESERVED_NOT_SENT;assert(!begin());journal=RI_UNRESOLVED;
 repeat.receipt.send_retcode=10012;assert(!begin());repeat.receipt.send_retcode=0;
 assert(begin());
 assert(step()==RSG_READY && owner.revision==6 && owner.opening.reverse.serial==10 && sends==1);
}
