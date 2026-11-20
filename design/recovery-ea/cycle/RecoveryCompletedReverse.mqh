#ifndef RECOVERY_COMPLETED_REVERSE_MQH
#define RECOVERY_COMPLETED_REVERSE_MQH
#include "RecoveryOpeningPair.mqh"

// A reverse pending order may already be filled before any inventory snapshot
// shows it working. Use independently bound execution history instead of waiting
// for impossible PLACED evidence. Never treat a broker send return as a fill.
struct RRCompletedReverse {
 RRAction routed_action;  // immutable reverse from pre-send strategy outbox
 ulong routed_order;      // broker ticket already independently bound to that action
 RCCloseCheckpoint predecessor_close; // inked independently; retired predecessor
 RRFilled successor;      // historically filled, remaining reconciled, exact live position
};

enum RRCompletedReverseResult {
 RR_COMPLETED_UNRESOLVED=0, RR_COMPLETED_NEXT_PAIR=1,
 RR_COMPLETED_PROTECTION_ONLY=2, RR_COMPLETED_FINAL_PROTECTED=3
};

// Only the first-fill/initial-SL lineage is handled here. A partially executed
// reverse, a predecessor that is still open, a stop different from the paired
// SL, or a close of another type belongs to normal reconciliation. This is not
// a policy for sudden overlapping hedged positions.
RRCompletedReverseResult RRAdvanceCompletedReverse(RRRangeState &s,const RRSettings &cfg,
      RROpeningPair &opening,const RRCompletedReverse &e,RRAction &protection_only) {
 RRClearAction(protection_only);
 if(!RRValid(s,cfg) || !RROpeningBound(s,opening) ||
    !RRActionEqual(e.routed_action,opening.reverse) || e.routed_order==0 ||
    (opening.reverse_verified && opening.reverse_ticket!=e.routed_order) ||
    !e.successor.independently_verified || !e.successor.entry.independently_verified ||
    !e.successor.entry.history_complete || !e.successor.entry.remainder_reconciled ||
    e.successor.entry.executed_order!=e.routed_order ||
    e.successor.entry.expected_order!=e.routed_order ||
    e.successor.entry.position_id==s.cycle.position_id ||
    e.successor.entry.side!=opening.reverse.side ||
    e.successor.entry.planned_steps!=opening.reverse.volume_steps ||
    e.successor.entry.filled_steps!=opening.reverse.volume_steps ||
    e.successor.working_other_ticket!=0 ||
    e.predecessor_close.key.scope!=s.scope ||
    RCClassifyClose(s.cycle,e.predecessor_close.proof)!=RC_FULL_SL ||
    e.predecessor_close.proof.exit_stop_ticks!=opening.reverse.entry_ticks)
    return RR_COMPLETED_UNRESOLVED;

 RRRangeState next=s;
 RROpeningPair prev=opening;
 // A fully proved stop exit at this exact intended level proves the preceding
 // SL effect. Do not infer it from the fact that a reverse filled.
 if(prev.sl_required && !prev.sl_verified) {
    RREffect protection;protection.action=prev.sl;protection.terminal_verified=true;
    protection.created_order=0;
    const RROpeningResult result=RRConfirmOpeningPair(next,prev,protection);
    if(result==RR_PAIR_CONFLICT)return RR_COMPLETED_UNRESOLVED;
 }
 if(prev.reserved) {
    RREffect filled;filled.action=prev.reverse;filled.terminal_verified=true;
    filled.created_order=e.routed_order;
    if(RRConfirmOpeningPair(next,prev,filled)!=RR_PAIR_CONFIRMED)
       return RR_COMPLETED_UNRESOLVED;
 }
 if(!RRApplyDurableClose(next,e.predecessor_close) ||
    !RRObserveFill(next,cfg,e.successor))return RR_COMPLETED_UNRESOLVED;

 // Build follow-up from successor.actual_entry_ticks and this generation's
 // configured distance, never from the trigger's requested/slipped price.
 RROpeningPair follow;
 follow.reserved=false;
 if(RCReverseSlot(next.cycle)!=0) {
    if(!RRStageOpeningPair(next,cfg,follow)) return RR_COMPLETED_UNRESOLVED;
    s=next;opening=follow;
    return RR_COMPLETED_NEXT_PAIR;
 }
 // Slot 24 may need exact protection adjustment, but never arms slot 25.
 if(next.phase==RR_PROTECT) {
    RRAction protect;
    if(!RRStage(next,cfg,0,0,protect) || protect.kind!=RR_INITIAL_SL)
       return RR_COMPLETED_UNRESOLVED;
    s=next;opening=prev;protection_only=protect;
    return RR_COMPLETED_PROTECTION_ONLY;
 }
 if(next.phase!=RR_ACTIVE)return RR_COMPLETED_UNRESOLVED;
 s=next;opening=prev;
 return RR_COMPLETED_FINAL_PROTECTED;
}
#endif
