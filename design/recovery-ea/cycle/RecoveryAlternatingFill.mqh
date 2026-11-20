#ifndef RECOVERY_ALTERNATING_FILL_MQH
#define RECOVERY_ALTERNATING_FILL_MQH
#include "RecoveryOpeningPair.mqh"

// strategy_slot is a bounded ordinal of actual successive entries. It is not
// evidence that any previous position closed, nor a realized-loss count.
// RRRangeState's legacy full_sl_count is mirrored from slot-1 for its lot/cap
// helpers only while this ledger owns the range. Never expose it as actual SLs.
struct RRAlternatingPosition {
 RCRange bound_entry;
 ulong position_ticket;
 long last_confirmed_sl, planned_sl;
 RRAction pending_at_handoff;
 RROpeningPair opening_at_handoff;
 bool detached,exit_recorded,observed_open_at_handoff;
 long observed_remaining_at_handoff;
 int exit_class;
 long reported_stop;
};
struct RRAlternationLedger {
 ulong scope,generation;
 int range_id,strategy_slot,confirmed_full_sl_count,position_count;
 RRAlternatingPosition positions[24];
};
struct RRAlternatingObservation {
 ulong scope;
 RRAction paired_intent; // exact prior intent if its opening pair is pending
 ulong paired_order_ticket; // persisted exact broker-effect identity, not price matching
 RRKnownOrder executed_order; // actual original order parameters proven by history
 RRFilled fill; // exact complete history plus independently matched live position
 bool predecessor_inventory_complete,predecessor_present;
 ulong predecessor_position_id;
 long predecessor_remaining_steps;
};
enum RRAlternatingResult { RR_ALT_UNRESOLVED=0,RR_ALT_NEXT_OPPOSITE=1,RR_ALT_FINAL_PROTECTED=2 };
enum RRAlternatingCloseResult { RR_ALT_CLOSE_UNRESOLVED=0,RR_ALT_CLOSE_APPLIED=1,
                               RR_ALT_CLOSE_DUPLICATE=2 };

void RRAltCapture(const RRRangeState &s,RRAlternatingPosition &p) {
 p.bound_entry=s.cycle;p.position_ticket=s.position_ticket;
 p.last_confirmed_sl=s.live_sl;p.planned_sl=s.initial_sl;
 p.detached=false;p.exit_recorded=false;p.exit_class=0;p.reported_stop=0;
 p.observed_open_at_handoff=false;p.observed_remaining_at_handoff=0;
 p.opening_at_handoff.reserved=false;RRClearAction(p.pending_at_handoff);
}

bool RRStartAlternation(const RRRangeState &s,const int confirmed_count,RRAlternationLedger &out) {
 const int slot=RCCurrentSlot(s.cycle);
 if(out.scope!=0 || s.scope==0 || slot<1 || slot>24 ||
    confirmed_count<0 || confirmed_count>slot-1 ||
    s.cycle.position_id==0 || s.cycle.active_order==0 || s.position_ticket==0)return false;
 out.scope=s.scope;out.generation=s.cycle.generation;out.range_id=s.cycle.range_id;
 out.strategy_slot=slot;out.confirmed_full_sl_count=confirmed_count;out.position_count=1;
 RRAltCapture(s,out.positions[0]);
 return true;
}

bool RRAltBound(const RRRangeState &s,const RRAlternationLedger &a) {
 return a.scope>0 && a.scope==s.scope && a.generation==s.cycle.generation &&
        a.range_id==s.cycle.range_id && a.strategy_slot==RCCurrentSlot(s.cycle) &&
        a.position_count>=1 && a.position_count<=a.strategy_slot &&
        a.confirmed_full_sl_count>=0 && a.confirmed_full_sl_count<=a.strategy_slot &&
        RCCPSameRange(a.positions[a.position_count-1].bound_entry,s.cycle) &&
        !a.positions[a.position_count-1].detached;
}

// No prior close proof argument by design. The preceding position and pending
// receipt stay tracked; the new trigger proves neither closure nor P/L.
RRAlternatingResult RRAdvanceAlternatingFill(RRRangeState &s,const RRSettings &cfg,
       RROpeningPair &opening,RRAlternationLedger &ledger,const RRAlternatingObservation &e) {
 if(!RRValid(s,cfg) || !RRAltBound(s,ledger) || e.scope!=s.scope || ledger.strategy_slot>=24 ||
    ledger.position_count>=24 || s.support_stage!=0 || s.retire.ticket!=0 ||
    s.entry.ticket!=0 || s.reentry_slot!=0 || !e.fill.independently_verified ||
    !e.fill.entry.independently_verified || !e.fill.entry.history_complete ||
    !e.fill.entry.remainder_reconciled || e.fill.working_other_ticket!=0 ||
    e.fill.position_ticket==0 || e.fill.tp_ticks!=0 ||
    e.executed_order.ticket==0 || e.executed_order.ticket!=e.fill.entry.executed_order ||
    e.executed_order.ticket!=e.fill.entry.expected_order || e.executed_order.side!=-s.cycle.side ||
    e.executed_order.side!=e.fill.entry.side || e.executed_order.entry_ticks!=s.initial_sl ||
    e.executed_order.sl_ticks!=e.fill.sl_ticks || !RCBoundedPositive(e.fill.sl_ticks) ||
    e.fill.entry.planned_steps!=e.executed_order.volume_steps ||
    e.fill.entry.filled_steps!=e.executed_order.volume_steps)return RR_ALT_UNRESOLVED;
 for(int i=0;i<ledger.position_count;i++)
    if(ledger.positions[i].bound_entry.active_order==e.executed_order.ticket ||
       ledger.positions[i].bound_entry.position_id==e.fill.entry.position_id)
       return RR_ALT_UNRESOLVED;
 if(e.predecessor_inventory_complete && e.predecessor_present &&
    (e.predecessor_position_id!=s.cycle.position_id ||
     !RCBoundedPositive(e.predecessor_remaining_steps) ||
     e.predecessor_remaining_steps>s.cycle.filled_steps))return RR_ALT_UNRESOLVED;
 if(opening.reserved) {
    if(e.paired_order_ticket==0 || e.paired_order_ticket!=e.executed_order.ticket || !RROpeningBound(s,opening) || !RRActionEqual(e.paired_intent,opening.reverse) ||
       (opening.reverse_verified && opening.reverse_ticket!=e.executed_order.ticket) ||
       opening.reverse.entry_ticks!=e.executed_order.entry_ticks ||
       opening.reverse.sl_ticks!=e.executed_order.sl_ticks ||
       opening.reverse.volume_steps!=e.executed_order.volume_steps)return RR_ALT_UNRESOLVED;
 } else if(s.phase!=RR_ACTIVE || s.pending.kind!=RR_NONE ||
           !RRKnownEqual(s.opposite,e.executed_order))return RR_ALT_UNRESOLVED;

 RRRangeState n=s;
 // Previous IDs move out of strategy ownership only; they are NOT marked closed.
 n.cycle.active_order=0;n.cycle.position_id=0;n.cycle.side=0;
 n.cycle.filled_steps=0;n.cycle.actual_entry_ticks=0;
 n.cycle.full_sl_count=ledger.strategy_slot; // projected successor rung minus one
 if(!RCApplyEntry(n.cycle,e.fill.entry))return RR_ALT_UNRESOLVED;
 RRClearKnown(n.opposite);RRClearAction(n.pending);
 n.position_ticket=e.fill.position_ticket;n.support_stage=0;n.live_tp=0;
 n.live_sl=e.fill.sl_ticks;n.initial_sl=e.fill.sl_ticks;
 RRAfterProtection(n);

 RROpeningPair follow;
 follow.reserved=false;
 const bool final=RCReverseSlot(n.cycle)==0;
 if(final) { if(n.phase!=RR_ACTIVE)return RR_ALT_UNRESOLVED; }
 else if(!RRStageOpeningPair(n,cfg,follow) || follow.sl_required ||
         follow.reverse.entry_ticks!=e.fill.sl_ticks)return RR_ALT_UNRESOLVED;

 int at=ledger.position_count-1;
 ledger.positions[at].last_confirmed_sl=s.live_sl;
 ledger.positions[at].planned_sl=s.initial_sl;
 ledger.positions[at].pending_at_handoff=s.pending;
 if(opening.reserved)ledger.positions[at].opening_at_handoff=opening;
 ledger.positions[at].observed_open_at_handoff=e.predecessor_inventory_complete&&e.predecessor_present;
 ledger.positions[at].observed_remaining_at_handoff=ledger.positions[at].observed_open_at_handoff?
        e.predecessor_remaining_steps:0;
 ledger.positions[at].detached=true;
 RRAltCapture(n,ledger.positions[ledger.position_count]);
 ledger.position_count++;
 ledger.strategy_slot++;
 s=n;
 if(final)opening.reserved=false;
 else opening=follow;
 return final?RR_ALT_FINAL_PROTECTED:RR_ALT_NEXT_OPPOSITE;
}

// Optional history may arrive before or after strategy advancement. It affects
// only accounting. Stable identity and exact normalized closure are mandatory.
RRAlternatingCloseResult RRRecordAlternatingClose(RRAlternationLedger &ledger,
                                               const RCCloseCheckpoint &close) {
 if(ledger.scope==0 || close.key.scope!=ledger.scope ||
    close.key.generation!=ledger.generation || close.key.range_id!=ledger.range_id ||
    ledger.position_count<1 || ledger.position_count>24)return RR_ALT_CLOSE_UNRESOLVED;
 for(int i=0;i<ledger.position_count;i++) {
    if(close.key.position_id!=ledger.positions[i].bound_entry.position_id)continue;
    if(close.key.anchor_order!=ledger.positions[i].bound_entry.active_order ||
       !RCCPSameRange(close.before,ledger.positions[i].bound_entry))return RR_ALT_CLOSE_UNRESOLVED;
    RCRange proof_range;
    RCClosePlan proof_plan;
    if(!RCCPDerive(close,proof_range,proof_plan))return RR_ALT_CLOSE_UNRESOLVED;
    const int classification=proof_plan.classification;
    if(ledger.positions[i].exit_recorded)
       return (ledger.positions[i].exit_class==classification &&
               ledger.positions[i].reported_stop==close.proof.exit_stop_ticks)?
              RR_ALT_CLOSE_DUPLICATE:RR_ALT_CLOSE_UNRESOLVED;
    if(classification==RC_FULL_SL) {
       if(close.proof.exit_stop_ticks!=ledger.positions[i].last_confirmed_sl &&
          close.proof.exit_stop_ticks!=ledger.positions[i].planned_sl)return RR_ALT_CLOSE_UNRESOLVED;
       if(ledger.confirmed_full_sl_count>=ledger.strategy_slot)return RR_ALT_CLOSE_UNRESOLVED;
       ledger.confirmed_full_sl_count++;
    }
    ledger.positions[i].exit_recorded=true;
    ledger.positions[i].exit_class=classification;
    ledger.positions[i].reported_stop=close.proof.exit_stop_ticks;
    return RR_ALT_CLOSE_APPLIED;
 }
 return RR_ALT_CLOSE_UNRESOLVED;
}
#endif
