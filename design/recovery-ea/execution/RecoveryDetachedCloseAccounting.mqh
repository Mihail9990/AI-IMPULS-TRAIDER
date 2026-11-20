#ifndef RECOVERY_DETACHED_CLOSE_ACCOUNTING_MQH
#define RECOVERY_DETACHED_CLOSE_ACCOUNTING_MQH
#include "RecoveryRangeOwner.mqh"
#include "RecoveryRangeClosePoll.mqh"

enum RecoveryDetachedCloseResult {RDC_IDLE=0,RDC_PROGRESS,RDC_PROOF,RDC_UNRESOLVED};

struct RecoveryDetachedCloseAccounting {
 RecoveryRangeClosePoll polls[24];
 RCCloseCheckpoint completed[24];
 int cursor;
};

bool RecoveryDetachedCloseOwned(const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope) {
 const RRAlternationLedger ledger=owner.ledger;
 return (owner.phase==RRO_ORDINARY || owner.phase==RRO_FINAL) &&
        RecoveryRangeOwnerBound(owner,panel,session,live,scope) &&
        ledger.scope==scope.scope && ledger.generation==scope.generation && ledger.range_id==owner.range_id &&
        ledger.position_count>=1 && ledger.position_count<=24;
}

bool RecoveryDetachedCloseEntry(const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,const int i,
                                RRRangeState &scratch) {
 ZeroMemory(scratch);
 if(i<0 || i>=owner.ledger.position_count || i>=24)return false;
 const RRAlternatingPosition e=owner.ledger.positions[i];
 if(!e.detached || e.exit_recorded || e.position_ticket==0 ||
    e.bound_entry.active_order==0 || e.bound_entry.position_id==0 ||
    e.bound_entry.range_id!=owner.range_id || e.bound_entry.generation!=owner.generation ||
    (owner.range_id!=1 && owner.range_id!=2))return false;
 const RRRangeState current=panel.ranges[owner.range_id-1];
 if(e.bound_entry.position_id==current.cycle.position_id || e.position_ticket==current.position_ticket ||
    e.bound_entry.active_order==current.cycle.active_order)return false;
 scratch.scope=owner.scope;scratch.cycle=e.bound_entry;scratch.position_ticket=e.position_ticket;
 return RRValid(scratch,panel.settings);
}

void RecoveryDetachedCloseFact(const RRRangeState &s,const long stop,const int kind,RCProtectionFact &out) {
 out.scope=s.scope;out.generation=s.cycle.generation;out.position_id=s.cycle.position_id;
 out.anchor_order=s.cycle.active_order;out.range_id=s.cycle.range_id;out.stop_class=kind;
 out.stop_ticks=stop;out.confirmed=RCBoundedPositive(stop);
}

RecoveryDetachedCloseResult RecoveryDetachedCloseAccountingTimer(RecoveryDetachedCloseAccounting &task,
    const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,RecoveryEvidenceOwner &verifier_owner,
    const RecoveryInventoryScope &verifier_context,const double tick,const double step,RCCloseCheckpoint &out) {
 out.key.scope=0;
 if(!RecoveryDetachedCloseOwned(owner,panel,session,live,scope) ||
    !RecoveryInvScopeOK(verifier_context) ||
    !RecoveryAlternatingPumpSession(verifier_context))return RDC_UNRESOLVED;
 const int count=owner.ledger.position_count;
 if(task.cursor<0 || task.cursor>=count)task.cursor=0;
 const int at=task.cursor;
 task.cursor=(at+1)%count; // At most one exact predecessor per timer, no starvation.
 RRRangeState scratch;
 if(!RecoveryDetachedCloseEntry(owner,panel,at,scratch))return RDC_IDLE;
 if(task.polls[at].state==RCP_IDLE) {
    if(!RecoveryBeginRangeClosePoll(task.polls[at],verifier_owner,session,live,verifier_context,
                                  scratch,panel.settings,tick,step))return RDC_IDLE;
    return RDC_PROGRESS;
 }
 if(task.polls[at].state==RCP_DONE || task.polls[at].state==RCP_UNRESOLVED)return RDC_IDLE;
 if(!RecoveryAlternatingSameSession(task.polls[at].binding.inventory,verifier_context))
    return RDC_UNRESOLVED;
 RCProtectionFact facts[2];
 const RRAlternatingPosition e=owner.ledger.positions[at];
 RecoveryDetachedCloseFact(scratch,e.confirmed_initial_sl,RC_INITIAL_LEVEL,facts[0]);
 RecoveryDetachedCloseFact(scratch,e.confirmed_support_sl,RC_SUPPORT_LEVEL,facts[1]);
 const RecoveryClosePollResult result=RecoveryRangeClosePollStep(task.polls[at],session,live,
                                         scratch,panel.settings,facts,out);
 if(result==RCP_PROOF) {task.completed[at]=out;return RDC_PROOF;}
 if(result==RCP_PROGRESS)return RDC_PROGRESS;
 if(result==RCP_WAIT)return RDC_IDLE;
 return RDC_UNRESOLVED;
}

// Explicit owner-side merge, at a chart-safe point (no in-flight transition
// tied to this revision). Never mutate active range/pending/order identities.
bool RecoveryCommitDetachedCloseAccounting(const RecoveryDetachedCloseAccounting &task,
    RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RCCloseCheckpoint &proof) {
 if(owner.revision==0 || owner.revision==ULONG_MAX ||
    !RecoveryDetachedCloseOwned(owner,panel,session,live,scope) ||
    proof.key.scope!=scope.scope || proof.key.generation!=scope.generation ||
    proof.key.range_id!=owner.range_id) return false;
 int at=-1;
 for(int i=0;i<owner.ledger.position_count;i++) {
    const RRAlternatingPosition e=owner.ledger.positions[i];
    if(e.bound_entry.position_id!=proof.key.position_id && e.bound_entry.active_order!=proof.key.anchor_order)
       continue;
    if(at!=-1 || !e.detached || e.exit_recorded ||
       !RCCPSameRange(e.bound_entry,proof.before))return false;
    at=i;
 }
 RRRangeState scratch;
 if(!RecoveryDetachedCloseEntry(owner,panel,at,scratch) ||
    task.polls[at].state!=RCP_DONE ||
    !RCCloseKeysEqual(proof.key,task.completed[at].key) ||
    !RCCPSameRange(task.completed[at].before,scratch.cycle))return false;
 RRAlternationLedger ledger=owner.ledger;
 if(RRRecordAlternatingClose(ledger,task.completed[at])!=RR_ALT_CLOSE_APPLIED)return false;
 owner.ledger=ledger;owner.revision++;
 return true;
}
#endif
