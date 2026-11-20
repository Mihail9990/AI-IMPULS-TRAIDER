#ifndef RECOVERY_SUPPORT_ROTATION_OWNER_MQH
#define RECOVERY_SUPPORT_ROTATION_OWNER_MQH
#include "RecoveryRangeOwner.mqh"
#include "RecoverySupportRotationDispatch.mqh"
#include "RecoveryExactOrderEffect.mqh"
#include "RecoveryWorkingReverse.mqh"
#include "../cycle/RecoverySupportRotationLedger.mqh"

enum RecoverySupportRotationPhase { RSRM_IDLE=0,RSRM_SEND,RSRM_REQUEST,RSRM_READ,
                                    RSRM_DONE,RSRM_UNRESOLVED };
struct RecoverySupportRotationOwner {
 RecoverySupportRotationPhase phase;
 RRSupportRotation plan;
 RecoveryExactEffectBinding effect;
 RecoveryIntentKey key;
 RecoveryRotationDispatchResult dispatch;
 ulong revision;
 string publisher_tag;
};

bool RecoveryBeginSupportRotationOwner(RecoverySupportRotationOwner &task,RecoveryRangeOwner &owner,
    RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,RecoveryEvidenceOwner &verifier,const RecoveryInventoryScope &context,
    const RRFilled &winner,const RRKnownOrder &survivor,const ulong verify_after,const double tick,const double step) {
 if(task.phase!=RSRM_IDLE || owner.phase!=RRO_ORDINARY || owner.opening.reserved || owner.revision==0 ||
    owner.revision>ULONG_MAX-2 || verifier.serial==ULONG_MAX || verify_after==0 ||
    verify_after>=(ulong)LONG_MAX || !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    verifier.executor_epoch!=session.epoch || !RecoverySessionScopeEqual(verifier.context,live) ||
    verifier.verifier_epoch!=context.verifier_epoch || verifier.verifier_chart!=context.verifier_chart ||
    !RecoveryInvScopeOK(context) || !MathIsValidNumber(tick) || tick<=0 ||
    !MathIsValidNumber(step) || step<=0)
    return false;
 const int i=owner.range_id-1;
 RRRangeState updated=panel.ranges[i];
 RRAlternationLedger ledger=owner.ledger;
 RecoverySupportRotationOwner next;
 ZeroMemory(next);
 if(!RRStageSupportRotationLedger(updated,panel.settings,ledger,winner,survivor,next.plan))
    return false;
 if(next.plan.reserved) {
    if(next.plan.sl_required || !next.plan.move_required ||
       !RecoveryBuildSessionIntentKey(scope,next.plan.move,tick,step,next.key) ||
       !RecoveryIntentContextOK(next.key))return false;
    const ulong serial=verifier.serial+1;
    const string identity=scope.executor_epoch+"\t"+next.key.id+"\t"+(string)serial;
    next.effect.request.binding=context;
    next.effect.request.binding.nonce=RecoveryOwnerNonce("support-rotate-exact",identity);
    next.publisher_tag=RecoveryOwnerNonce("support-rotate-publisher",identity);
    next.effect.request.order_ticket=survivor.ticket;
    next.effect.action=next.plan.move;
    next.effect.verify_after=verify_after;
    next.effect.price_tick=tick;next.effect.volume_step=step;
    if(!RecoveryInvHex(next.publisher_tag) || next.publisher_tag==next.effect.request.binding.nonce ||
       !RecoveryWorkingReverseContext(session,live,scope,next.effect.request) ||
       !RecoveryExactEffectBound(updated,next.effect))return false;
    verifier.serial=serial;
    next.phase=RSRM_SEND;
 } else next.phase=RSRM_DONE;

 panel.ranges[i]=updated;
 owner.ledger=ledger;owner.revision++;
 owner.next_exact_ticket=survivor.ticket;owner.armed_position=0;
 RecoveryOrdinaryEventReset(owner.ordinary);
 next.revision=owner.revision;
 task=next;
 return true;
}

// At most one MODIFY. Completion is only independent exact PLACED evidence
// for the same ticket and the new price, SL, type and lot; no cancel/create
// fallback. Retain task.plan and dispatch receipt for exact fast-fill recovery.
RecoverySupportRotationPhase RecoveryStepSupportRotationOwner(RecoverySupportRotationOwner &task,
    RecoveryRangeOwner &owner,RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &verifier) {
 if(task.phase!=RSRM_SEND && task.phase!=RSRM_REQUEST && task.phase!=RSRM_READ)return task.phase;
 if(owner.phase!=RRO_ORDINARY || owner.opening.reserved || owner.revision!=task.revision ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    !RecoveryAlternatingSameSession(task.effect.request.binding,verifier) ||
    !RecoveryWorkingReverseContext(session,live,scope,task.effect.request)) {
    task.phase=RSRM_UNRESOLVED;return task.phase;
 }
 const int i=owner.range_id-1;
 RRRangeState range=panel.ranges[i];
 if(!RRAltBound(range,owner.ledger) || !RRSupportRotationBound(range,task.plan) ||
    !RecoveryExactEffectBound(range,task.effect)) {
    task.phase=RSRM_UNRESOLVED;return task.phase;
 }
 if(task.phase==RSRM_SEND) {
    task.phase=RSRM_UNRESOLVED; // consume before entering the broker seam
    RecoveryIntentKey unused;
    ZeroMemory(unused);
    if(RecoveryDispatchSupportRotation(range,task.plan,unused,task.key,
          task.effect.price_tick,task.effect.volume_step,task.dispatch) &&
       task.dispatch.move.state==RD_OUTCOME_UNRESOLVED)
       task.phase=RSRM_REQUEST;
    return task.phase;
 }
 if(task.phase==RSRM_REQUEST) {
    task.phase=RecoveryExactReportRequest(task.effect.request,task.publisher_tag)?RSRM_READ:RSRM_UNRESOLVED;
    return task.phase;
 }
 RecoveryExactReport report;
 if(!RecoveryExactReportRead(task.effect.request,report))return task.phase;
 RREffect effect;
 if(!RecoveryExactEffectFold(report,task.effect,effect) ||
    RRConfirmSupportRotation(range,task.plan,effect)!=RR_PAIR_CONFIRMED || range.phase!=RR_ACTIVE ||
    range.opposite.ticket!=task.effect.request.order_ticket) {
    task.phase=RSRM_UNRESOLVED;return task.phase;
 }
 panel.ranges[i]=range;owner.revision++;task.phase=RSRM_DONE;
 return task.phase;
}
#endif
