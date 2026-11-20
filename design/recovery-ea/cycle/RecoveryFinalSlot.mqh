#ifndef RECOVERY_FINAL_SLOT_MQH
#define RECOVERY_FINAL_SLOT_MQH
#include "RecoveryAlternatingFill.mqh"

// Only an independently classified exit of the exact last position may
// exhaust the cycle or reserve another entry at the same final rung.
bool RRFinalClose(RRRangeState &range,const RRSettings &cfg,RRAlternationLedger &ledger,
                  const RCCloseCheckpoint &close) {
 if(!RRValid(range,cfg) || !RRAltBound(range,ledger) || ledger.strategy_slot!=RC_LIMIT ||
    range.phase!=RR_ACTIVE || range.position_ticket==0 || range.entry.ticket!=0 ||
    range.opposite.ticket!=0 || range.retire.ticket!=0 ||
    !RCCPSameRange(close.before,range.cycle) || close.key.scope!=range.scope ||
    close.reentry_distance!=cfg.initial_sl_ticks)return false;
 const int kind=RCClassifyClose(range.cycle,close.proof);
 if(kind!=RC_FULL_SL && kind!=RC_SUPPORT_SL)return false;
 if(kind==RC_SUPPORT_SL && (range.support_stage!=2 || range.live_sl!=range.cycle.actual_entry_ticks ||
    close.proof.exit_stop_ticks!=range.live_sl))return false;
 if(kind==RC_FULL_SL && close.proof.exit_stop_ticks!=range.initial_sl)return false;
 RRRangeState next=range;
 if(next.pending.kind==RR_SUPPORT_SL && next.pending.position_ticket==range.position_ticket &&
    next.pending.position_id==range.cycle.position_id)RRClearAction(next.pending);
 if(!RRApplySessionClose(next,close) ||
    (kind==RC_FULL_SL && next.phase!=RR_DONE) ||
    (kind==RC_SUPPORT_SL && (next.phase!=RR_ARM_REENTRY || next.reentry_slot!=RC_LIMIT)))return false;
 if(RRRecordAlternatingClose(ledger,close)!=RR_ALT_CLOSE_APPLIED)return false;
 range=next;
 return true;
}

bool RRFinalReentryFill(RRRangeState &s,const RRSettings &cfg,RRAlternationLedger &ledger,
                        const RRKnownOrder &wanted,const RRFilled &f) {
 if(!RRValid(s,cfg) || ledger.scope!=s.scope || ledger.generation!=s.cycle.generation ||
    ledger.range_id!=s.cycle.range_id || ledger.strategy_slot!=RC_LIMIT ||
    ledger.position_count<1 || ledger.position_count>RC_LIMIT ||
    s.phase!=RR_ARM_REENTRY || s.reentry_slot!=RC_LIMIT || RCCurrentSlot(s.cycle)!=RC_LIMIT ||
    s.pending.kind!=RR_REENTRY || s.entry.ticket!=0 || s.opposite.ticket!=0 || s.retire.ticket!=0 ||
    s.cycle.position_id!=0 || s.position_ticket!=0 || wanted.ticket==0 ||
    wanted.side!=s.reentry_side || wanted.entry_ticks!=s.reentry_ticks ||
    wanted.sl_ticks!=s.pending.sl_ticks || wanted.volume_steps!=cfg.lot_steps[RC_LIMIT-1] ||
    !f.independently_verified || f.working_other_ticket!=0 || f.position_ticket==0 || f.tp_ticks!=0 ||
    f.sl_ticks<0 || f.sl_ticks>RC_MAX_UNITS || f.entry.side!=wanted.side ||
    f.entry.executed_order!=wanted.ticket || f.entry.expected_order!=wanted.ticket ||
    f.entry.planned_steps!=wanted.volume_steps || f.entry.filled_steps!=wanted.volume_steps)return false;
 const int last=ledger.position_count-1;
 const RRAlternatingPosition prev=ledger.positions[last];
 if(!prev.exit_recorded || prev.exit_class!=RC_SUPPORT_SL || prev.detached ||
    prev.bound_entry.position_id!=s.cycle.last_closed_position ||
    prev.bound_entry.active_order!=s.cycle.last_closed_order ||
    prev.reported_stop!=wanted.sl_ticks || prev.bound_entry.side!=wanted.side)return false;
 for(int i=0;i<ledger.position_count;i++)
    if(ledger.positions[i].bound_entry.position_id==f.entry.position_id ||
       ledger.positions[i].bound_entry.active_order==wanted.ticket)return false;
 RRRangeState next=s;
 if(!RCApplyEntry(next.cycle,f.entry))return false;
 next.position_ticket=f.position_ticket;next.initial_sl=wanted.sl_ticks;
 next.live_sl=f.sl_ticks;next.live_tp=0;next.support_stage=0;
 next.reentry_slot=0;next.reentry_side=0;next.reentry_ticks=0;
 RRClearAction(next.pending);
 if(!RRAfterActualEntryNoSL(next,cfg) || next.phase!=RR_ACTIVE)return false;
 RRAltCapture(next,ledger.positions[last]);
 s=next;return true;
}

bool RRFinalSettled(const RRRangeState &s,const RRAlternationLedger &ledger) {
 if(s.phase!=RR_DONE || !s.cycle.requires_manual_ranges || s.cycle.full_sl_count!=RC_LIMIT ||
    s.cycle.active_order!=0 || s.cycle.position_id!=0 || s.position_ticket!=0 ||
    s.entry.ticket!=0 || s.opposite.ticket!=0 || s.retire.ticket!=0 || s.pending.kind!=RR_NONE ||
    s.scope!=ledger.scope || s.cycle.generation!=ledger.generation || s.cycle.range_id!=ledger.range_id ||
    ledger.strategy_slot!=RC_LIMIT || ledger.position_count<1 || ledger.position_count>RC_LIMIT)return false;
 for(int i=0;i<ledger.position_count;i++)
    if(!ledger.positions[i].exit_recorded)return false;
 const RRAlternatingPosition last=ledger.positions[ledger.position_count-1];
 return !last.detached && last.exit_class==RC_FULL_SL &&
        last.bound_entry.position_id==s.cycle.last_closed_position &&
        last.bound_entry.active_order==s.cycle.last_closed_order;
}
#endif
