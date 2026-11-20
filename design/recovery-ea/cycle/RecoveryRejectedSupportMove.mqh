#ifndef RECOVERY_REJECTED_SUPPORT_MOVE_MQH
#define RECOVERY_REJECTED_SUPPORT_MOVE_MQH
#include "RecoverySupportRotation.mqh"

struct RRRejectedSupportMove {
 RRAction rejected_action; // exact durable modification intent
 bool definite_broker_rejection; // mapped final rejection, never timeout/unavailable
 RRKnownOrder executed_original; // verifier history proves old unmodified parameters
 RCCloseCheckpoint predecessor_close;
 RRFilled successor; // confirmed active volume and attached stop
};
enum RRRejectedMoveResult { RR_REJECTED_MOVE_UNRESOLVED=0, RR_REJECTED_MOVE_NEW_OPPOSITE=1,
                            RR_REJECTED_MOVE_FINAL_PROTECTED=2 };

// A definitive MODIFY rejection never permits cancellation/recreation of the old
// order. If the independently proven original later fills, adopt its already
// attached SL, not a replacement calculated from relative fill distance.
RRRejectedMoveResult RRAdvanceRejectedSupportMove(RRRangeState &s,const RRSettings &cfg,
      RRSupportRotation &rotation,const RRRejectedSupportMove &e,RROpeningPair &new_opposite) {
 if(new_opposite.reserved || !RRValid(s,cfg) || !RRSupportRotationBound(s,rotation) ||
    !rotation.move_required || rotation.move_verified || !e.definite_broker_rejection ||
    !RRActionEqual(e.rejected_action,rotation.move) ||
    !RRKnownEqual(e.executed_original,rotation.previous) ||
    !e.successor.independently_verified || !e.successor.entry.independently_verified ||
    !e.successor.entry.history_complete || !e.successor.entry.remainder_reconciled ||
    e.successor.entry.executed_order!=rotation.previous.ticket ||
    e.successor.entry.expected_order!=rotation.previous.ticket ||
    e.successor.entry.position_id==s.cycle.position_id ||
    e.successor.entry.side!=rotation.previous.side ||
    e.successor.entry.planned_steps!=rotation.previous.volume_steps ||
    e.successor.entry.filled_steps!=rotation.previous.volume_steps ||
    e.successor.working_other_ticket!=0 ||
    e.successor.sl_ticks!=rotation.previous.sl_ticks || !RCBoundedPositive(e.successor.sl_ticks) ||
    e.predecessor_close.key.scope!=s.scope) return RR_REJECTED_MOVE_UNRESOLVED;

 const int kind=RCClassifyClose(s.cycle,e.predecessor_close.proof);
 if(kind!=RC_FULL_SL && kind!=RC_MANUAL_EXIT)return RR_REJECTED_MOVE_UNRESOLVED;
 if(kind==RC_FULL_SL && e.predecessor_close.proof.exit_stop_ticks!=s.live_sl &&
    e.predecessor_close.proof.exit_stop_ticks!=s.initial_sl)return RR_REJECTED_MOVE_UNRESOLVED;

 RRRangeState n=s;
 // History and the completed close settle the predecessor, not the rejected
 // modify or the optional SL call. Preserve its actual receipts in the journal.
 n.opposite=rotation.previous;RRClearKnown(n.retire);RRClearAction(n.pending);
 if(!RRApplyDurableClose(n,e.predecessor_close) ||
    !RRObserveFill(n,cfg,e.successor)) return RR_REJECTED_MOVE_UNRESOLVED;
 n.initial_sl=e.successor.sl_ticks; // exclusively this documented exception
 n.live_sl=e.successor.sl_ticks;
 RRAfterProtection(n);
 if(RCReverseSlot(n.cycle)==0) {
    if(n.phase!=RR_ACTIVE)return RR_REJECTED_MOVE_UNRESOLVED;
    s=n;rotation.reserved=false;
    return RR_REJECTED_MOVE_FINAL_PROTECTED;
 }
 RROpeningPair pair;
 pair.reserved=false;
 if(!RRStageOpeningPair(n,cfg,pair) || pair.sl_required ||
    pair.reverse.entry_ticks!=e.successor.sl_ticks) return RR_REJECTED_MOVE_UNRESOLVED;
 s=n;rotation.reserved=false;new_opposite=pair;
 return RR_REJECTED_MOVE_NEW_OPPOSITE;
}
#endif
