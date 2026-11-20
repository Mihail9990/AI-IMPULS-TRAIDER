#ifndef RECOVERY_FINAL_LEG_MQH
#define RECOVERY_FINAL_LEG_MQH
#include "RecoveryRangeOwner.mqh"
#include "RecoveryRangeClosePoll.mqh"
#include "../cycle/RecoveryFinalSlot.mqh"

struct RecoveryFinalLeg {
 bool attempted;
 ulong ticket;
 RecoveryIntentKey key;
 RecoveryProofRetry retry;
 RecoveryPrimaryFillPump fill;
};

// A final support reentry has no surviving opposite. Never place a second
// order based on an ambiguous submission or a missed transaction callback.
void RecoveryFinalReentry(RecoveryFinalLeg &task,RecoveryRangeOwner &owner,RRRangeState &range,
    const RRSettings &cfg,const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,
    RecoveryEvidenceOwner &evidence,const ulong close_sequence,const double tick,const double step) {
 if(range.phase!=RR_ARM_REENTRY || range.reentry_slot!=RC_LIMIT || range.opposite.ticket!=0 ||
    owner.revision==ULONG_MAX)return;
 if(!task.attempted) {
    if(close_sequence==0 || close_sequence>(ulong)LONG_MAX-3 || evidence.serial==ULONG_MAX ||
       range.pending.kind!=RR_NONE)return;
    RRRangeState next=range;
    RRAction a;
    RecoveryIntentKey key;
    if(!RRStage(next,cfg,0,0,a) || a.kind!=RR_REENTRY ||
       !RecoveryBuildSessionIntentKey(scope,a,tick,step,key))return;
    task.attempted=true;
    range=next;
    RecoveryDispatchResult receipt;
    if(RecoveryDispatchOnce(range,a,key,tick,step,receipt)!=RD_TICKET_RECORDED || receipt.order==0)return;
    task.key=key;task.ticket=receipt.order;
 }
 if(task.ticket==0)return;
 if(task.fill.state==RPP_UNRESOLVED) {
    if(!RecoveryProofRetryDue(task.retry,GetTickCount64()))return;
    ZeroMemory(task.fill);
 }
 if(task.fill.state==RPP_UNUSED) {
    if(evidence.serial==ULONG_MAX)return;
    string recorded;
    RecoveryIntentKey expected;
    if(!RecoveryBuildSessionIntentKey(scope,range.pending,tick,step,expected) ||
       !RecoveryIntentKeysMatch(task.key,expected) ||
       RecoveryIntentLookup(expected,recorded)!=RI_TICKET || recorded!=(string)task.ticket)return;
    const ulong serial=++evidence.serial;
    const string id=expected.id+":"+(string)task.ticket+":"+(string)serial;
    RecoveryPrimaryFillBinding b;
    ZeroMemory(b);
    b.exact.binding=verifier;b.history.binding=verifier;b.inventory=verifier;
    b.exact.binding.nonce=RecoveryOwnerNonce("final-exact",id);
    b.history.binding.nonce=RecoveryOwnerNonce("final-history",id);
    b.inventory.nonce=RecoveryOwnerNonce("final-inventory",id);
    b.exact.order_ticket=task.ticket;b.history.anchor_order=task.ticket;
    b.scope=scope.scope;b.verify_after=close_sequence;b.routed=range.pending;b.price_tick=tick;b.volume_step=step;
    if(!RecoveryPrimaryPumpBegin(task.fill,range,cfg,b,RecoveryOwnerNonce("final-publisher",id)))
       task.fill.state=RPP_UNRESOLVED;
    return;
 }
 RRFilled observed;
 const RecoveryPrimaryPumpResult result=RecoveryPrimaryPumpStep(task.fill,range,cfg,observed);
 if(result==RPP_WAIT) {
    const RecoveryPrimaryFillBinding b=task.fill.binding;
    if((task.fill.state==RPP_WAIT_EXACT && FileIsExist("RECOVERY_VERIFY\\"+b.exact.binding.nonce+".order")) ||
       (task.fill.state==RPP_WAIT_HISTORY && FileIsExist("RECOVERY_VERIFY\\"+b.history.binding.nonce+".history")) ||
       (task.fill.state==RPP_WAIT_INVENTORY && FileIsExist("RECOVERY_VERIFY\\"+b.inventory.nonce+".snapshot")))
       task.fill.state=RPP_UNRESOLVED;
 }
 if(result==RPP_PROOF &&
    RRFinalReentryFill(range,cfg,owner.ledger,task.fill.wanted,observed)) {
    owner.revision++;ZeroMemory(task);
 }
}

bool RecoveryFinalStep(RecoveryFinalLeg &task,RecoveryRangeOwner &owner,RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,RecoveryEvidenceOwner &evidence,
    const RecoveryRangeClosePoll &poll,const RCCloseCheckpoint &close,const double tick,const double step) {
 if(owner.phase!=RRO_FINAL || owner.opening.reserved || owner.revision==ULONG_MAX ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    !RecoverySessionScopeEqual(evidence.context,live) || evidence.executor_epoch!=scope.executor_epoch ||
    evidence.verifier_epoch!=verifier.verifier_epoch || evidence.verifier_chart!=verifier.verifier_chart ||
    verifier.generation!=(long)scope.generation)return false;
 const int i=owner.range_id-1;
 if(panel.ranges[i].phase==RR_ACTIVE && poll.state==RCP_DONE &&
    poll.live_ticket==panel.ranges[i].position_ticket && RCCloseKeysEqual(close.key,poll.binding.key) &&
    RRFinalClose(panel.ranges[i],panel.settings,owner.ledger,close))owner.revision++;
 RecoveryFinalReentry(task,owner,panel.ranges[i],panel.settings,scope,verifier,evidence,poll.verified_sequence,tick,step);
 return RRFinalSettled(panel.ranges[i],owner.ledger);
}
#endif
