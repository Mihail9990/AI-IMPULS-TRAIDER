#ifndef RECOVERY_CLOSE_CHECKPOINT_MQH
#define RECOVERY_CLOSE_CHECKPOINT_MQH
#include "RecoveryCloseWire.mqh"

// One in-memory record binds a confirmed close to the current session and
// its derived follow-up plan. It is not a submission receipt or send permission.
struct RCCloseCheckpoint {
   RCCloseEvidenceKey key;
   RCRange before;
   RCCloseProof proof;
   long reentry_distance;
};
enum RCCheckpointResult { RCCP_CONFLICT=0,RCCP_APPLIED=1,RCCP_ALREADY_APPLIED=2 };

bool RCCPSameRange(const RCRange &a,const RCRange &b) {
   return a.generation==b.generation && a.range_id==b.range_id &&
          a.full_sl_count==b.full_sl_count && a.requires_manual_ranges==b.requires_manual_ranges &&
          a.active_order==b.active_order && a.position_id==b.position_id &&
          a.side==b.side && a.filled_steps==b.filled_steps && a.actual_entry_ticks==b.actual_entry_ticks &&
          a.last_closed_order==b.last_closed_order && a.last_closed_position==b.last_closed_position;
}

bool RCCPDerive(const RCCloseCheckpoint &record,RCRange &after,RCClosePlan &plan) {
   if(!RCCloseKeysEqual(record.key,record.key) ||
      !RCBound(record.before,record.key.generation,record.key.range_id) ||
      record.before.position_id!=record.key.position_id ||
      record.before.active_order!=record.key.anchor_order ||
      record.before.requires_manual_ranges || !RCBoundedPositive(record.before.actual_entry_ticks) ||
      record.before.last_closed_order==record.before.active_order ||
      record.before.last_closed_position==record.before.position_id ||
      record.reentry_distance<0 || record.reentry_distance>RC_MAX_UNITS) return false;
   after=record.before;
   return RCApplyClose(after,record.proof,record.reentry_distance,plan);
}

bool RCCPStageFromWire(const RCRange &before,const RCCloseWireBinding &binding,
                     const string history_wire,const string inventory_wire,
                     const RCProtectionFact &protection[],const long reentry_distance,
                     RCCloseCheckpoint &out) {
   out.key.scope=0; // invalid sentinel even if caller reused an output variable
   RCCloseCheckpoint candidate;
   if(RCBuildCloseFromWire(before,binding,history_wire,inventory_wire,protection,candidate.proof)
      !=RCE_FULLY_CLASSIFIED)return false;
   candidate.key=binding.key;candidate.before=before;candidate.reentry_distance=reentry_distance;
   RCRange after;
   RCClosePlan plan;
   if(!RCCPDerive(candidate,after,plan)) return false;
   out=candidate;
   return true;
}

RCEvidenceResult RCCPObserveFromReports(const RCRange &before,const RCCloseWireBinding &binding,
                       const RecoveryHistorySnapshot &history,const RecoveryInventorySnapshot &inventory,
                       const RCProtectionFact &protection[],const long reentry_distance,
                       RCCloseCheckpoint &out,const bool include_unclassified=false) {
   out.key.scope=0;
   RCCloseCheckpoint candidate;
   const RCEvidenceResult status=RCBuildCloseFromReports(before,binding,history,inventory,protection,
                                                         candidate.proof,include_unclassified);
   if(status!=RCE_FULLY_CLASSIFIED && status!=RCE_CLOSED_UNCLASSIFIED)return status;
   candidate.key=binding.key;candidate.before=before;candidate.reentry_distance=reentry_distance;
   out=candidate;
   return status;
}

bool RCCPStageFromReports(const RCRange &before,const RCCloseWireBinding &binding,
                       const RecoveryHistorySnapshot &history,const RecoveryInventorySnapshot &inventory,
                       const RCProtectionFact &protection[],const long reentry_distance,
                       RCCloseCheckpoint &out) {
   out.key.scope=0;
   RCCloseCheckpoint candidate;
   if(RCCPObserveFromReports(before,binding,history,inventory,protection,reentry_distance,candidate)
      !=RCE_FULLY_CLASSIFIED)return false;
   RCRange after;
   RCClosePlan plan;
   if(!RCCPDerive(candidate,after,plan))return false;
   out=candidate;
   return true;
}

void RCCPClearPlan(RCClosePlan &plan) {
   plan.classification=RC_UNRESOLVED;plan.current_slot=0;plan.reentry_slot=0;
   plan.reentry_side=0;plan.reentry_ticks=0;plan.keep_existing_opposite=false;
   plan.cancel_remaining_range_orders=false;
}

RCCheckpointResult RCCPApply(const RCCloseCheckpoint &record,RCRange &current,RCClosePlan &plan) {
   RCCPClearPlan(plan);
   RCRange after;
   RCClosePlan derived;
   if(!RCCPDerive(record,after,derived))return RCCP_CONFLICT;
   if(RCCPSameRange(current,after))return RCCP_ALREADY_APPLIED;
   if(!RCCPSameRange(current,record.before))return RCCP_CONFLICT;
   current=after;plan=derived;
   return RCCP_APPLIED;
}

// Canonical snapshot serialization is intentionally absent: session only.
#endif
