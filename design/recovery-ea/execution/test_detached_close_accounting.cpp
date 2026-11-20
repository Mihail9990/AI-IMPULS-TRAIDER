#include <cassert>
#include <climits>
#include <cstdint>
using ulong=std::uint64_t;
template<class T> void ZeroMemory(T &x){x=T{};}
enum {RCP_IDLE=0,RCP_DONE,RCP_UNRESOLVED,RCP_HISTORY_REQUEST,RRO_ORDINARY,RRO_FINAL,
      RC_INITIAL_LEVEL,RC_SUPPORT_LEVEL,RR_ALT_CLOSE_APPLIED,
      RR_ALT_CLOSE_UNRESOLVED};
enum RecoveryClosePollResult {RCP_WAIT,RCP_PROGRESS,RCP_PROOF,RCP_NOT_CONFIRMED};
struct RCRange {ulong generation,active_order,position_id;int range_id;};
struct RRRangeState {ulong scope,position_ticket;RCRange cycle;};
struct RRSettings {ulong scope,generation;};
struct RRAlternatingPosition {
 RCRange bound_entry;ulong position_ticket;bool detached,exit_recorded;
 long planned_sl,confirmed_initial_sl,confirmed_support_sl;
};
struct RRAlternationLedger {
 ulong scope,generation;int range_id,position_count,confirmed_full_sl_count;
 RRAlternatingPosition positions[24];
};
struct RecoveryRangeOwner {int phase,range_id;ulong scope,generation,revision;RRAlternationLedger ledger;};
struct RecoveryPanelControl {RRRangeState ranges[2];RRSettings settings;};
struct RecoverySessionLifecycle {bool active;};
struct RecoverySessionContext {};
struct RecoverySessionIntentScope {ulong scope,generation;};
struct RecoveryEvidenceOwner {int nonce_count;};
struct RecoveryInventoryScope {int epoch;};
struct RCCloseEvidenceKey {ulong scope,generation,position_id,anchor_order,observation;int range_id;};
struct RCCloseCheckpoint {RCCloseEvidenceKey key;RCRange before;int classification;};
struct RCProtectionFact {ulong scope,generation,position_id,anchor_order;int range_id,stop_class;long stop_ticks;bool confirmed;};
struct RecoveryRangeClosePoll {
 int state;
 struct {RecoveryInventoryScope inventory;} binding;
 ulong position;
};
void RecoveryRearmCloseRead(RecoveryRangeClosePoll&){}
const ulong older=9007199254740997ULL,newer=9007199254740999ULL,still_owned=9007199254741001ULL;
bool old_failed=false;
int merged=0,steps=0;
bool RecoveryRangeOwnerBound(const RecoveryRangeOwner &o,const RecoveryPanelControl &,const RecoverySessionLifecycle &s,
 const RecoverySessionContext &,const RecoverySessionIntentScope &q) {
 return s.active&&o.scope==q.scope&&o.generation==q.generation;
}
bool RecoveryInvScopeOK(const RecoveryInventoryScope &q){return q.epoch>0;}
bool RecoveryAlternatingPumpSession(const RecoveryInventoryScope &q){return q.epoch==7;}
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b){return a.epoch==b.epoch;}
bool RRValid(const RRRangeState &s,const RRSettings &q){return s.scope==q.scope&&s.cycle.generation==q.generation;}
bool RCBoundedPositive(long n){return n>0;}
bool RecoveryBeginRangeClosePoll(RecoveryRangeClosePoll &p,RecoveryEvidenceOwner &o,
    const RecoverySessionLifecycle &,const RecoverySessionContext &,
    const RecoveryInventoryScope &v,const RRRangeState &s,const RRSettings &,double,double) {
 assert(s.cycle.position_id==older||s.cycle.position_id==newer);
 ++o.nonce_count;p.state=RCP_HISTORY_REQUEST;p.binding.inventory=v;p.position=s.cycle.position_id;
 return true;
}
RecoveryClosePollResult RecoveryRangeClosePollStep(RecoveryRangeClosePoll &p,const RecoverySessionLifecycle &,
    const RecoverySessionContext &,const RRRangeState &s,const RRSettings &,const RCProtectionFact *facts,
    RCCloseCheckpoint &out) {
 ++steps;
 assert(p.position==s.cycle.position_id&&s.position_ticket>0&&p.state==RCP_HISTORY_REQUEST);
 assert(facts[0].position_id==s.cycle.position_id&&facts[0].anchor_order==s.cycle.active_order);
 assert(facts[0].stop_class==RC_INITIAL_LEVEL);
 if(p.position==older) {
   assert(!facts[0].confirmed && facts[0].stop_ticks==0); // a planned manual SL is not proof
   assert(facts[1].stop_class==RC_SUPPORT_LEVEL&&!facts[1].confirmed);
   if(old_failed){p.state=RCP_UNRESOLVED;return RCP_NOT_CONFIRMED;}
   return RCP_WAIT;
 }
 assert(facts[0].confirmed&&facts[0].stop_ticks==400000);
 assert(facts[1].stop_class==RC_SUPPORT_LEVEL&&facts[1].confirmed&&facts[1].stop_ticks==400080);
 p.state=RCP_DONE;
 out={{s.scope,s.cycle.generation,s.cycle.position_id,s.cycle.active_order,40,s.cycle.range_id},s.cycle,RC_INITIAL_LEVEL};
 return RCP_PROOF;
}
bool RCCloseKeysEqual(const RCCloseEvidenceKey &a,const RCCloseEvidenceKey &b){
 return a.scope==b.scope&&a.generation==b.generation&&a.position_id==b.position_id&&
        a.anchor_order==b.anchor_order&&a.observation==b.observation&&a.range_id==b.range_id;
}
bool RCCPSameRange(const RCRange &a,const RCRange &b){
 return a.generation==b.generation&&a.position_id==b.position_id&&a.active_order==b.active_order&&a.range_id==b.range_id;
}
int RRRecordAlternatingClose(RRAlternationLedger &a,const RCCloseCheckpoint &proof){
 assert(proof.classification==RC_INITIAL_LEVEL);
 for(int i=0;i<a.position_count;i++)if(RCCPSameRange(a.positions[i].bound_entry,proof.before)&& !a.positions[i].exit_recorded){
   a.positions[i].exit_recorded=true;++a.confirmed_full_sl_count;++merged;return RR_ALT_CLOSE_APPLIED;
 }
 return RR_ALT_CLOSE_UNRESOLVED;
}
#include "DetachedAccountingUnderTest"
int main(){
 RecoveryRangeOwner owner{};owner.phase=RRO_ORDINARY;owner.scope=19;owner.generation=7;owner.range_id=1;owner.revision=4;
 owner.ledger.scope=19;owner.ledger.generation=7;owner.ledger.range_id=1;owner.ledger.position_count=3;
 owner.ledger.positions[0]={{7,100,older,1},200,true,false,400000,0,0};
 owner.ledger.positions[1]={{7,101,newer,1},201,true,false,400000,400000,400080};
 owner.ledger.positions[2]={{7,102,still_owned,1},202,false,false,400000,400000,0};
 RecoveryPanelControl panel{};panel.settings={19,7};panel.ranges[0]={19,202,{7,102,still_owned,1}};
 RecoverySessionLifecycle session{true};RecoverySessionContext live;RecoverySessionIntentScope scope{19,7};
 RecoveryEvidenceOwner verifier{};RecoveryInventoryScope ctx{7};
 RecoveryDetachedCloseAccounting task{};RCCloseCheckpoint proof{};
 auto timer=[&](){return RecoveryDetachedCloseAccountingTimer(task,owner,panel,session,live,scope,verifier,ctx,.01,.01,proof);};
 assert(timer()==RDC_PROGRESS&&timer()==RDC_PROGRESS&&timer()==RDC_IDLE&&verifier.nonce_count==2);
 assert(timer()==RDC_IDLE&&steps==1); // old response waits, round-robin continues.
 panel.ranges[0].cycle.position_id=still_owned+1;owner.revision=5; // successor advances during proof.
 assert(timer()==RDC_PROOF&&proof.key.position_id==newer&&owner.revision==5&&merged==0);
 auto bad=proof;bad.key.observation++;
 assert(!RecoveryCommitDetachedCloseAccounting(task,owner,panel,session,live,scope,bad)&&merged==0);
 auto snapshot=proof;
 old_failed=true;
 assert(timer()==RDC_IDLE&&timer()==RDC_UNRESOLVED&&steps==3);
 assert(timer()==RDC_IDLE&&timer()==RDC_IDLE&&timer()==RDC_IDLE&&steps==3);
 // Merge only once and from internally held proof, not caller-mutated fields.
 snapshot.classification=1234;
 assert(RecoveryCommitDetachedCloseAccounting(task,owner,panel,session,live,scope,snapshot)&&merged==1&&owner.revision==6);
 assert(owner.ledger.confirmed_full_sl_count==1&&!owner.ledger.positions[0].exit_recorded&&owner.ledger.positions[1].exit_recorded);
 assert(!RecoveryCommitDetachedCloseAccounting(task,owner,panel,session,live,scope,snapshot)&&merged==1);
 assert(panel.ranges[0].cycle.position_id==still_owned+1&&verifier.nonce_count==2);
}
