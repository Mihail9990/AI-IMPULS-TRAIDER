#ifndef RECOVERY_CYCLE_MQH
#define RECOVERY_CYCLE_MQH

// Offline transition rules. Prices are exact broker ticks, volume is exact broker
// volume steps. Account/symbol/epoch, order ownership, history completeness,
// evidence immutability and durable state + intent commit belong to the caller.
// No terminal reads, trade API, timers, retries, or guessed executions.
#define RC_LIMIT 24
#define RC_MAX_UNITS 1000000000000

enum RCCloseReason { RC_SL=1, RC_MANUAL=2, RC_TP=3 };
enum RCCloseClass { RC_UNRESOLVED=0, RC_FULL_SL=1, RC_SUPPORT_SL=2,
                    RC_MANUAL_EXIT=3, RC_TP_EXIT=4 };
enum RCStopClassMask { RC_INITIAL_LEVEL=1, RC_SUPPORT_LEVEL=2 };

struct RCRange {
 ulong generation;
 int range_id;
 int full_sl_count;
 bool requires_manual_ranges;
 ulong active_order, position_id;
 int side;
 long filled_steps, actual_entry_ticks;
 ulong last_closed_order, last_closed_position;
};

struct RCEntryProof {
 ulong generation;
 int range_id;
 ulong expected_order, executed_order, position_id;
 int side;
 long planned_steps, filled_steps, active_steps, actual_entry_ticks;
 bool independently_verified, history_complete, remainder_reconciled;
};

struct RCCloseProof {
 ulong generation;
 int range_id;
 ulong order, position_id;
 long filled_steps, exited_steps, remaining_steps;
 bool independently_verified, history_complete;
 // Fold every exact exit deal. Mixed reasons or levels are not classified;
 // a current position SL, a send acknowledgement and P/L are not evidence.
 int reason;
 long exit_stop_ticks;
 int confirmed_stop_mask;
 bool uniform_exit_reason_and_stop;
};

struct RCClosePlan {
 int classification;
 int current_slot, reentry_slot;
 int reentry_side;
 long reentry_ticks;
 bool keep_existing_opposite;
 bool cancel_remaining_range_orders;
};

struct RCStopProposal {
 int stage;
 long target_ticks;
};

bool RCBoundedPositive(const long value) { return value>0 && value<=RC_MAX_UNITS; }
bool RCValidSide(const int side) { return side==1 || side==-1; }
bool RCBound(const RCRange &r,const ulong generation,const int range_id) {
 return r.generation>0 && r.generation==generation && r.range_id==range_id &&
        (range_id==1 || range_id==2) && r.full_sl_count>=0 && r.full_sl_count<=RC_LIMIT;
}

// Slot 1 is the first entry. Prearming uses the projected next full-stop slot;
// a pre-armed opposite order's size is never changed on manual or support exit.
int RCCurrentSlot(const RCRange &r) {
 if(!RCBound(r,r.generation,r.range_id) || r.requires_manual_ranges || r.full_sl_count>=RC_LIMIT)return 0;
 return r.full_sl_count+1;
}
int RCReverseSlot(const RCRange &r) {
 int current=RCCurrentSlot(r);
 if(current==0 || current>=RC_LIMIT)return 0;
 return current+1;
}

// Default sizing in exact hundredths of a lot. A saved, separately validated
// table can override slots 2..24 at the caller; slot 1 and the cap are fixed.
int RCDefaultLotHundredths(const int slot) {
 if(slot<1 || slot>RC_LIMIT)return 0;
 if(slot<=5)return 1;
 if(slot<=8)return 2;
 return 3+(slot-9)/2;
}

bool RCApplyEntry(RCRange &r,const RCEntryProof &p) {
 if(!RCBound(r,p.generation,p.range_id) || RCCurrentSlot(r)==0 ||
    r.active_order!=0 || r.position_id!=0 || !p.independently_verified || !p.history_complete ||
    !p.remainder_reconciled || p.expected_order==0 || p.expected_order!=p.executed_order ||
    p.position_id==0 || p.expected_order==r.last_closed_order || p.position_id==r.last_closed_position ||
    !RCValidSide(p.side) || !RCBoundedPositive(p.planned_steps) || !RCBoundedPositive(p.filled_steps) ||
    p.filled_steps>p.planned_steps || p.active_steps!=p.filled_steps || !RCBoundedPositive(p.actual_entry_ticks))
  return false;
 r.active_order=p.executed_order;
 r.position_id=p.position_id;
 r.filled_steps=p.filled_steps;
 r.actual_entry_ticks=p.actual_entry_ticks;
 r.side=p.side;
 return true;
}

// Exact larger/smaller comparison uses the executable side of the spread.
// A proposal never changes confirmed protection or changes exit classification.
bool RCProposeSupport(const RCRange &r,const long current_sl,const int confirmed_stage,
                      const long bid,const long ask,const long first_ticks,
                      const long second_ticks,const long support_offset_ticks,RCStopProposal &out) {
 out.stage=0;out.target_ticks=0;
 if(RCCurrentSlot(r)==0 || r.active_order==0 || r.position_id==0 ||
    !RCValidSide(r.side) || !RCBoundedPositive(r.actual_entry_ticks) ||
    !RCBoundedPositive(current_sl) || confirmed_stage<0 || confirmed_stage>2 ||
    !RCBoundedPositive(bid) || !RCBoundedPositive(ask) || ask<bid ||
    !RCBoundedPositive(first_ticks) || !RCBoundedPositive(second_ticks) ||
    !RCBoundedPositive(support_offset_ticks) || second_ticks<=first_ticks)return false;
 long progress=((r.side==1?bid:ask)-r.actual_entry_ticks)*r.side;
 int stage=progress>=second_ticks?2:(progress>=first_ticks?1:0);
 if(stage<=confirmed_stage)return false;
 long target=(stage==2?r.actual_entry_ticks:r.actual_entry_ticks-r.side*support_offset_ticks);
 if(!RCBoundedPositive(target) || (target-current_sl)*r.side<=0)return false;
 out.stage=stage;out.target_ticks=target;
 return true;
}

int RCClassifyClose(const RCRange &r,const RCCloseProof &p) {
 if(!RCBound(r,p.generation,p.range_id) || RCCurrentSlot(r)==0 ||
    !RCValidSide(r.side) || p.order==0 || p.position_id==0 ||
    p.order!=r.active_order || p.position_id!=r.position_id ||
    !p.independently_verified || !p.history_complete || !p.uniform_exit_reason_and_stop ||
    !RCBoundedPositive(r.filled_steps) || p.filled_steps!=r.filled_steps ||
    p.exited_steps!=p.filled_steps || p.remaining_steps!=0)return RC_UNRESOLVED;
 if(p.reason==RC_MANUAL && p.confirmed_stop_mask==0 && p.exit_stop_ticks==0)return RC_MANUAL_EXIT;
 if(p.reason==RC_TP && p.confirmed_stop_mask==0 && p.exit_stop_ticks==0)return RC_TP_EXIT;
 if(p.reason!=RC_SL || !RCBoundedPositive(p.exit_stop_ticks))return RC_UNRESOLVED;
 // A confirmed matching initial AND support price is ambiguous, not a full SL.
 if(p.confirmed_stop_mask==RC_INITIAL_LEVEL)return RC_FULL_SL;
 if(p.confirmed_stop_mask==RC_SUPPORT_LEVEL)return RC_SUPPORT_SL;
 return RC_UNRESOLVED;
}

bool RCApplyClose(RCRange &r,const RCCloseProof &p,const long reentry_distance,RCClosePlan &out) {
 out.classification=RC_UNRESOLVED;out.current_slot=0;out.reentry_slot=0;
 out.reentry_side=0;out.reentry_ticks=0;out.keep_existing_opposite=false;
 out.cancel_remaining_range_orders=false;
 int classification=RCClassifyClose(r,p);
 // Parity baseline has no autonomous policy for TP exits. Keep the active
 // checkpoint for reconciliation; no generated order action.
 if(classification==RC_UNRESOLVED || classification==RC_TP_EXIT)return false;
 long target=0;
 if(classification==RC_SUPPORT_SL) {
  if(!RCBoundedPositive(reentry_distance))return false;
  target=p.exit_stop_ticks+r.side*reentry_distance;
  if(!RCBoundedPositive(target))return false;
 }
 out.classification=classification;
 if(classification==RC_SUPPORT_SL) {
  out.reentry_ticks=target;out.reentry_side=r.side;out.reentry_slot=RCCurrentSlot(r);
 }
 if(classification==RC_FULL_SL)r.full_sl_count++;
 if(r.full_sl_count==RC_LIMIT) {
  r.requires_manual_ranges=true;
  // This signals reconciliation/cancellation, not a claim that broker orders
  // have already disappeared. Scope every future action to this generation/range.
  out.cancel_remaining_range_orders=true;
 }
 out.keep_existing_opposite=!r.requires_manual_ranges &&
                            (classification==RC_MANUAL_EXIT ||
                             classification==RC_FULL_SL || classification==RC_SUPPORT_SL);
 r.last_closed_order=r.active_order;r.last_closed_position=r.position_id;
 r.active_order=0;r.position_id=0;r.filled_steps=0;r.side=0;r.actual_entry_ticks=0;
 out.current_slot=RCCurrentSlot(r);
 return true;
}

#endif
