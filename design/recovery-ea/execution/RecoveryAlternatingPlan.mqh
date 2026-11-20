#ifndef RECOVERY_ALTERNATING_PLAN_MQH
#define RECOVERY_ALTERNATING_PLAN_MQH
#include "../cycle/RecoveryAlternatingFill.mqh"

// Single writer owns revision, increments it for EVERY update (including
// accounting), and persists the complete state with the outbox atomically.
struct RecoveryAlternatingState {
 ulong revision;
 RRRangeState range;
 RROpeningPair opening;
 RRAlternationLedger ledger;
};
struct RecoveryAlternatingPlan {
 ulong base_revision,scope,generation,previous_order,previous_position;
 int range_id;
 RRAlternatingResult result;
 RecoveryAlternatingState next;
};

RRAlternatingResult RecoveryPreviewAlternating(const RecoveryAlternatingState &current,
    const RRSettings &cfg,const RRAlternatingObservation &proof,RecoveryAlternatingPlan &out) {
 out.result=RR_ALT_UNRESOLVED;
 if(current.revision==0 || current.revision==ULONG_MAX)return out.result;
 RecoveryAlternatingState next=current;
 const RRAlternatingResult result=RRAdvanceAlternatingFill(next.range,cfg,next.opening,next.ledger,proof);
 if(result==RR_ALT_UNRESOLVED)return out.result;
 next.revision++;
 out.base_revision=current.revision;
 out.scope=current.range.scope;out.generation=current.range.cycle.generation;
 out.range_id=current.range.cycle.range_id;
 out.previous_order=current.range.cycle.active_order;
 out.previous_position=current.range.cycle.position_id;
 out.next=next;out.result=result;
 return result;
}

// For the state store's CAS, before durable publication (not evidence that a
// write happened). The authenticated, normalized proof never enters the sender.
bool RecoveryAlternatingPlanCurrent(const RecoveryAlternatingState &current,
                                   const RecoveryAlternatingPlan &plan) {
 return (plan.result==RR_ALT_NEXT_OPPOSITE || plan.result==RR_ALT_FINAL_PROTECTED) &&
        plan.base_revision>0 && plan.base_revision<ULONG_MAX &&
        current.revision==plan.base_revision && plan.next.revision==plan.base_revision+1 &&
        current.range.scope==plan.scope && current.range.cycle.generation==plan.generation &&
        current.range.cycle.range_id==plan.range_id &&
        current.range.cycle.active_order==plan.previous_order &&
        current.range.cycle.position_id==plan.previous_position &&
        plan.next.range.scope==plan.scope && plan.next.range.cycle.generation==plan.generation &&
        plan.next.range.cycle.range_id==plan.range_id &&
        plan.next.range.cycle.active_order!=plan.previous_order &&
        plan.next.range.cycle.position_id!=plan.previous_position;
}

// Build dispatchable view only from the committed/read-back state, with exact
// revision and immutable staged action recovered from the same durable outbox.
bool RecoveryAlternatingDispatchView(const RecoveryAlternatingState &persisted,
    const ulong outbox_revision,const RRAction &outbox_action,RRRangeState &shadow) {
 if(outbox_revision==0 || outbox_revision!=persisted.revision ||
    !RRAltBound(persisted.range,persisted.ledger) || persisted.opening.sl_required ||
    outbox_action.kind!=RR_REVERSE || outbox_action.entry_ticks!=persisted.range.live_sl)
    return false;
 return RROpeningRequestState(persisted.range,persisted.opening,outbox_action,shadow);
}
#endif
