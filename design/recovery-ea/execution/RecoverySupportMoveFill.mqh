#ifndef RECOVERY_SUPPORT_MOVE_FILL_MQH
#define RECOVERY_SUPPORT_MOVE_FILL_MQH
#include "RecoverySupportRotationOwner.mqh"
#include "RecoveryAlternatingService.mqh"

enum RecoverySupportMoveFillState {
 RSMF_IDLE=0,RSMF_REQUEST,RSMF_READ,RSMF_FOLD,RSMF_DONE,RSMF_UNRESOLVED
};
enum RecoverySupportMoveFillResult {RSMF_WAIT=0,RSMF_PROGRESS,RSMF_ADVANCED,RSMF_STOPPED};

struct RecoverySupportMoveFill {
 RecoverySupportMoveFillState state;
 RecoveryAlternatingEvidenceBinding binding;
 RecoveryAlternatingPump pump;
 RRKnownOrder chosen;
 ulong revision,previous_position;
 bool edited;
 string publisher_tag;
};

bool RecoverySupportMoveFillCurrent(const RecoverySupportRotationOwner &rotation,
    const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier) {
 if((rotation.phase!=RSRM_REQUEST && rotation.phase!=RSRM_READ && rotation.phase!=RSRM_UNRESOLVED) ||
    owner.phase!=RRO_ORDINARY || owner.opening.reserved || rotation.revision==0 ||
    owner.revision!=rotation.revision || owner.revision==ULONG_MAX ||
    !rotation.plan.actual_entry_only || rotation.plan.sl_required || !rotation.plan.move_required ||
    rotation.plan.move_verified || rotation.plan.previous.ticket==0 ||
    rotation.plan.previous.ticket!=rotation.effect.request.order_ticket ||
    !RRActionEqual(rotation.plan.move,rotation.effect.action) ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    !RecoveryAlternatingSameSession(rotation.effect.request.binding,verifier) ||
    !RecoveryWorkingReverseContext(session,live,scope,rotation.effect.request))
    return false;
 const RRRangeState range=panel.ranges[owner.range_id-1];
 if(!RRAltBound(range,owner.ledger) || !RRSupportRotationBound(range,rotation.plan) ||
    !RecoveryExactEffectBound(range,rotation.effect))return false;
 RecoveryIntentKey expected;
 return RecoveryBuildSessionIntentKey(scope,rotation.plan.move,rotation.effect.price_tick,
         rotation.effect.volume_step,expected) && RecoveryIntentKeysMatch(expected,rotation.key);
}

// Scratch view of the already-owned survivor at exactly one candidate shape.
// Converting the pending state here does not confirm the modification or
// affect live broker state. Never store this scratch view before full proof.
bool RecoverySupportMoveFillView(const RRRangeState &current,const RRSupportRotation &rotation,
                               const RRKnownOrder &chosen,RRRangeState &shadow) {
 if(!RRSupportRotationBound(current,rotation) || chosen.ticket!=rotation.previous.ticket ||
    chosen.side!=rotation.previous.side || chosen.volume_steps!=rotation.previous.volume_steps ||
    !RCBoundedPositive(chosen.entry_ticks) || !RCBoundedPositive(chosen.sl_ticks))return false;
 RRKnownOrder edited;
 RRSetKnown(edited,rotation.previous.ticket,rotation.move);
 if(!RRKnownEqual(chosen,rotation.previous) && !RRKnownEqual(chosen,edited))return false;
 RRRangeState next=current;
 next.phase=RR_ACTIVE;next.opposite=chosen;next.opposite_anchor_ticks=chosen.entry_ticks;
 RRClearKnown(next.retire);RRClearAction(next.pending);
 shadow=next;
 return true;
}

bool RecoverySupportMoveFillLocal(const RecoverySupportRotationOwner &rotation) {
 const RecoveryInventoryScope q=rotation.effect.request.binding;
 RecoveryExactOrderScope wanted;
 wanted.login=q.login;wanted.server=q.server;wanted.symbol=q.symbol;
 wanted.magic=(ulong)q.magic;wanted.ticket=rotation.plan.previous.ticket;
 RecoveryExactOrderObservation row;
 return RecoveryObserveExactOrder(wanted,row)==RECOVERY_EXACT_ORDER_OBSERVED &&
        row.source==RECOVERY_EXACT_ORDER_HISTORY && row.ticket==wanted.ticket &&
        row.state==ORDER_STATE_FILLED && row.position_identifier!=0 &&
        row.position_identifier!=rotation.plan.position_id;
}

// Triggered from a local exact-ticket FILLED hint only; the hint never
// determines terms, position/side, or permission to continue trading.
bool RecoveryBeginSupportMoveFill(RecoverySupportMoveFill &task,
    const RecoverySupportRotationOwner &rotation,const RecoveryRangeOwner &owner,
    const RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &context,RecoveryEvidenceOwner &verifier) {
 if(task.state!=RSMF_IDLE || !RecoverySupportMoveFillCurrent(rotation,owner,panel,session,live,scope,context) ||
    verifier.executor_epoch!=session.epoch || verifier.verifier_epoch!=context.verifier_epoch ||
    verifier.verifier_chart!=context.verifier_chart || !RecoverySessionScopeEqual(verifier.context,live) ||
    verifier.serial==ULONG_MAX || rotation.effect.verify_after>(ulong)LONG_MAX-3 ||
    !RecoverySupportMoveFillLocal(rotation))return false;
 const ulong serial=verifier.serial+1;
 const string id=scope.executor_epoch+":"+verifier.verifier_epoch+":"+rotation.key.id+":"+
    (string)rotation.plan.previous.ticket+":"+(string)serial;
 RecoverySupportMoveFill next;
 ZeroMemory(next);
 next.binding.exact.binding=context;next.binding.history.binding=context;next.binding.inventory=context;
 next.binding.exact.binding.nonce=RecoveryOwnerNonce("support-move-fill-exact",id);
 next.binding.history.binding.nonce=RecoveryOwnerNonce("support-move-fill-history",id);
 next.binding.inventory.nonce=RecoveryOwnerNonce("support-move-fill-inventory",id);
 next.publisher_tag=RecoveryOwnerNonce("support-move-fill-publisher",id);
 string tokens[5];
 tokens[0]=next.binding.exact.binding.nonce;tokens[1]=next.binding.history.binding.nonce;
 tokens[2]=next.binding.inventory.nonce;tokens[3]=next.publisher_tag;
 tokens[4]=rotation.effect.request.binding.nonce;
 for(int i=0;i<5;i++) {
    if(!RecoveryInvHex(tokens[i]))return false;
    for(int j=0;j<i;j++)if(tokens[i]==tokens[j])return false;
 }
 next.binding.exact.order_ticket=rotation.plan.previous.ticket;
 next.binding.history.anchor_order=rotation.plan.previous.ticket;
 next.binding.scope=scope.scope;next.binding.verify_after=rotation.effect.verify_after;
 next.binding.price_tick=rotation.effect.price_tick;next.binding.volume_step=rotation.effect.volume_step;
 next.revision=owner.revision;next.previous_position=rotation.plan.position_id;
 next.state=RSMF_REQUEST;
 task=next;verifier.serial=serial;
 return true;
}

// The independent exact report must match exactly one full historical
// shape. Old terms do not claim a definite reject. Edited terms additionally
// require this session's submitted MOVE intent and no definite reject.
bool RecoverySupportMoveFillSelect(RecoverySupportMoveFill &task,
    const RecoverySupportRotationOwner &rotation,const RRRangeState &range,
    const RecoveryExactReport &report) {
 int matches=0;
 RRKnownOrder chosen;
 RecoveryAlternatingPump selected;
 bool edited=false;
 for(int i=0;i<2;i++) {
    RRKnownOrder candidate=rotation.plan.previous;
    if(i==1) {
       string unused;
       if(rotation.dispatch.move.synchronous_move_rejected ||
          rotation.dispatch.move.state!=RD_OUTCOME_UNRESOLVED ||
          RecoveryIntentLookup(rotation.key,unused)!=RI_UNRESOLVED)continue;
       RRSetKnown(candidate,rotation.plan.previous.ticket,rotation.plan.move);
    }
    RRRangeState shadow;
    RROpeningPair empty;
    RecoveryAlternatingPump branch;
    ZeroMemory(empty);ZeroMemory(branch);
    ulong successor;
    if(!RecoverySupportMoveFillView(range,rotation.plan,candidate,shadow) ||
       !RecoveryAlternatingPumpBegin(branch,shadow,empty,task.binding,task.publisher_tag) ||
       !RecoveryAlternatingPumpExact(branch,report,successor))continue;
    // First report is consumed here; the ordinary pump obtains later named
    // history and inventory reports with their own independently bound IDs.
    branch.exact=report;branch.binding.history.position_identifier=successor;
    branch.state=RAP_REQUEST_HISTORY;
    selected=branch;chosen=candidate;edited=i==1;
    matches++;
 }
 if(matches!=1)return false;
 task.chosen=chosen;task.pump=selected;task.edited=edited;
 return true;
}

RecoverySupportMoveFillResult RecoveryStopSupportMoveFill(RecoverySupportMoveFill &task) {
 task.state=RSMF_UNRESOLVED;return RSMF_STOPPED;
}

// Once fallback request begins, the chart routes that rotation through this
// function rather than racing the ordinary PLACED confirmation in parallel.
RecoverySupportMoveFillResult RecoveryStepSupportMoveFill(RecoverySupportMoveFill &task,
    RecoverySupportRotationOwner &rotation,RecoveryRangeOwner &owner,RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,
    RecoverySessionAlternatingResult &result) {
 ZeroMemory(result);
 if(task.state!=RSMF_REQUEST && task.state!=RSMF_READ && task.state!=RSMF_FOLD)return RSMF_STOPPED;
 if(task.revision!=owner.revision || task.previous_position!=rotation.plan.position_id ||
    task.binding.exact.order_ticket!=rotation.plan.previous.ticket ||
    !RecoveryAlternatingSameSession(task.binding.exact.binding,verifier) ||
    !RecoverySupportMoveFillCurrent(rotation,owner,panel,session,live,scope,verifier))
    return RecoveryStopSupportMoveFill(task);
 const int i=owner.range_id-1;
 if(task.state==RSMF_REQUEST) {
    task.state=RSMF_READ;
    if(!RecoveryExactReportRequest(task.binding.exact,task.publisher_tag))
       return RecoveryStopSupportMoveFill(task);
    return RSMF_PROGRESS;
 }
 if(task.state==RSMF_READ) {
    RecoveryExactReport report;
    if(!RecoveryExactReportRead(task.binding.exact,report))return RSMF_WAIT;
    if(!RecoverySupportMoveFillSelect(task,rotation,panel.ranges[i],report))
       return RecoveryStopSupportMoveFill(task);
    task.state=RSMF_FOLD;
    return RSMF_PROGRESS;
 }
 RecoveryAlternatingState next;
 RecoveryRangeOwnerSnapshot(owner,panel,next);
 if(!RecoverySupportMoveFillView(panel.ranges[i],rotation.plan,task.chosen,next.range))
    return RecoveryStopSupportMoveFill(task);
 RRAlternatingObservation proof;
 const RecoveryAlternatingPumpResult observed=
    RecoveryAlternatingPumpStep(task.pump,next.range,next.opening,proof);
 if(observed==RAP_WAIT)return RSMF_WAIT;
 if(observed==RAP_PROGRESS)return RSMF_PROGRESS;
 if(observed!=RAP_PROOF || !RRKnownEqual(proof.executed_order,task.chosen) ||
    !RecoverySupportMoveFillCurrent(rotation,owner,panel,session,live,scope,verifier))
    return RecoveryStopSupportMoveFill(task);

 RecoveryAlternatingOutbox outbox;
 ZeroMemory(outbox);
 if(!RecoveryApplyVerifiedAlternating(scope,next,outbox,panel.settings,proof,
                       task.binding.price_tick,task.binding.volume_step,result))
    return RecoveryStopSupportMoveFill(task);
 panel.ranges[i]=next.range;owner.opening=next.opening;owner.ledger=next.ledger;owner.revision=next.revision;
 owner.outbox=outbox;owner.next_exact_ticket=RecoveryRangeOwnerReceiptTicket(result.receipt);
 owner.armed_position=0;RecoveryOrdinaryEventReset(owner.ordinary);
 if(result.transition==RR_ALT_FINAL_NO_TRIGGER)owner.phase=RRO_FINAL;
 rotation.plan.reserved=false;rotation.phase=RSRM_DONE;task.state=RSMF_DONE;
 return RSMF_ADVANCED;
}
#endif
