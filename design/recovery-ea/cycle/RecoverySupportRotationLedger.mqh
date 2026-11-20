#ifndef RECOVERY_SUPPORT_ROTATION_LEDGER_MQH
#define RECOVERY_SUPPORT_ROTATION_LEDGER_MQH
#include "RecoveryAlternatingFill.mqh"
#include "RecoverySupportRotation.mqh"

// A break-even reentry stays on the same lot rung. Replace only the last
// position's already recorded break-even entry in the in-session ledger;
// detached predecessors and their not-yet-reconciled losses remain intact.
bool RRStageSupportRotationLedger(RRRangeState &range,const RRSettings &cfg,
    RRAlternationLedger &ledger,const RRFilled &filled,const RRKnownOrder &survivor,
    RRSupportRotation &rotation) {
 if(cfg.support_first_ticks!=0 || cfg.support_offset_ticks!=0 || rotation.reserved ||
    !RRHasSupportAlternatives(range) || !RRValid(range,cfg) ||
    ledger.scope!=range.scope || ledger.generation!=range.cycle.generation ||
    ledger.range_id!=range.cycle.range_id || ledger.strategy_slot!=RCCurrentSlot(range.cycle) ||
    ledger.position_count<1 || ledger.position_count>24 ||
    ledger.position_count>ledger.strategy_slot || ledger.confirmed_full_sl_count<0 ||
    ledger.confirmed_full_sl_count>ledger.strategy_slot-1)
    return false;
 const int i=ledger.position_count-1;
 const RRAlternatingPosition previous=ledger.positions[i];
 if(previous.detached || !previous.exit_recorded || previous.exit_class!=RC_SUPPORT_SL ||
    previous.reported_stop!=previous.bound_entry.actual_entry_ticks ||
    previous.bound_entry.position_id==0 ||
    previous.bound_entry.position_id!=range.cycle.last_closed_position ||
    previous.bound_entry.active_order!=range.cycle.last_closed_order ||
    previous.bound_entry.side!=range.reentry_side ||
    previous.bound_entry.actual_entry_ticks+previous.bound_entry.side*cfg.initial_sl_ticks!=range.reentry_ticks)
    return false;
 for(int j=0;j<ledger.position_count;j++)
    if(ledger.positions[j].bound_entry.position_id==filled.entry.position_id ||
       ledger.positions[j].bound_entry.active_order==filled.entry.executed_order)
       return false;
 RRRangeState next=range;
 RRSupportRotation proposed;
 proposed.reserved=false;
 if(!RRStageSupportRotation(next,cfg,filled,survivor,proposed) || !proposed.actual_entry_only ||
    RCCurrentSlot(next.cycle)!=ledger.strategy_slot)return false;
 RRAlternationLedger updated=ledger;
 RRAltCapture(next,updated.positions[i]);
 if(!RRAltBound(next,updated))return false;
 range=next;ledger=updated;rotation=proposed;
 return true;
}
#endif
