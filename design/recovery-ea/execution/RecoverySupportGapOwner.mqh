#ifndef RECOVERY_SUPPORT_GAP_OWNER_MQH
#define RECOVERY_SUPPORT_GAP_OWNER_MQH
#include "RecoverySupportReentry.mqh"
#include "RecoveryAlternatingPump.mqh"

enum RecoverySupportGapOwnerState {RSG_IDLE=0,RSG_PROBE,RSG_STAGED,RSG_SENT,RSG_FINAL_STATE,RSG_STOP};
enum RecoverySupportGapOwnerResult {RSG_WAIT=0,RSG_PROGRESS,RSG_READY,RSG_FINAL,RSG_UNKNOWN};

struct RecoverySupportGapOwner {
 RecoverySupportGapOwnerState state;
 RecoveryAlternatingPump pump;
 RRRangeState before;
 RRSupportGap gap;
 ulong owner_revision;
 RecoverySupportClosePhase close_phase;
 RecoveryIntentKey next_key;
 RecoveryDispatchResult next_receipt;
};

bool RecoverySupportGapNoRepeat(const RecoverySupportCloseOwner &close,const RecoverySupportReentry &repeat) {
 if(repeat.ticket!=0)return false;
 if(close.phase==RSCO_REQUEST || close.phase==RSCO_READ || close.phase==RSCO_UNRESOLVED)
    return repeat.state==RSRE_IDLE && repeat.revision==0 && close.gap.reserved &&
           close.reentry.kind==RR_NONE && close.key.id=="";
 if(close.gap.reserved || close.reentry.kind!=RR_REENTRY)return false;
 string recorded;
 if(close.phase==RSCO_STAGED)
    return repeat.state==RSRE_IDLE && repeat.revision==0 &&
           RecoveryIntentLookup(close.key,recorded)==RI_NO_RECORD;
 // RR_REENTRY uses the synchronous typed dispatcher. RD_NOT_SENT and
 // RD_RESERVED_NOT_SENT are exits before its OrderSend call (including the
 // final exact opposite preflight). An entered OrderSend is always unresolved
 // or receipt-bearing, even when the broker call fails. Never treat a missing
 // ticket in that latter case as permission to synthesize a gap.
 if(close.phase!=RSCO_DISPATCHED || repeat.state!=RSRE_UNRESOLVED ||
    close.owner_revision==0 || close.owner_revision>=ULONG_MAX-1 ||
    repeat.revision!=close.owner_revision+1 || repeat.receipt.order!=0 ||
    repeat.receipt.send_retcode!=0 || repeat.receipt.async_sl_submitted)return false;
 const RecoveryIntentState journal=RecoveryIntentLookup(close.key,recorded);
 return recorded=="" &&
        ((repeat.receipt.state==RD_NOT_SENT && journal==RI_NO_RECORD) ||
         (repeat.receipt.state==RD_RESERVED_NOT_SENT && journal==RI_UNRESOLVED));
}

bool RecoverySupportGapBase(const RecoverySupportCloseOwner &close,const RecoverySupportReentry &repeat,
    const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,
    RRRangeState &before,RRSupportGap &gap) {
 if(!RecoverySupportGapNoRepeat(close,repeat) || owner.revision==0 || owner.revision==ULONG_MAX ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) || owner.opening.reserved ||
    !RecoveryAlternatingSameSession(close.working.request.binding,verifier) ||
    close.gap.support_stage!=2 || !close.gap.have_close || close.gap.have_fill ||
    close.gap.close.reentry_distance!=panel.settings.initial_sl_ticks ||
    panel.settings.support_first_ticks!=0 || panel.settings.support_offset_ticks!=0)
    return false;
 const int i=owner.range_id-1;
 gap=close.gap;before=panel.ranges[i];
 if(close.phase==RSCO_STAGED || close.phase==RSCO_DISPATCHED) {
    if(!RecoverySupportReentryBound(close,owner,panel,session,live,scope,verifier))return false;
    // Scratch predecessor only. Preserve the staged action serial so the new
    // reverse has a distinct intention even though that repeat never ran.
    before.cycle=gap.before;before.position_ticket=gap.position_ticket;
    before.phase=RR_ACTIVE;before.live_sl=gap.support_level;before.initial_sl=gap.initial_level;
    before.live_tp=gap.tp;before.support_stage=gap.support_stage;before.opposite=gap.opposite;
    before.opposite_anchor_ticks=gap.opposite_anchor;
    before.reentry_slot=0;before.reentry_side=0;before.reentry_ticks=0;
    RRClearKnown(before.entry);RRClearKnown(before.retire);RRClearAction(before.pending);
    gap.reserved=true;
 } else if(!RecoverySupportCloseOwnerBound(close,owner,panel,session,live,scope,verifier))return false;
 return RRSupportGapBound(before,gap) && RRAltBound(before,owner.ledger);
}

// Advisory exact local read for choosing whether to request independent gap
// evidence. PLACED is usable only as a last read before the typed reentry
// dispatch; neither observation is proof of a close or execution.
enum RecoverySupportGapFixedStatus {RSG_FIXED_UNKNOWN=0,RSG_FIXED_WORKING,RSG_FIXED_HISTORICAL};
RecoverySupportGapFixedStatus RecoverySupportGapFixed(const RecoverySupportCloseOwner &close,
    const RecoverySessionContext &live) {
 const RRKnownOrder known=close.gap.opposite;
 const double tick=close.working.price_tick,step=close.working.volume_step;
 RecoveryExactOrderScope scope;
 scope.login=live.login;scope.server=live.server;scope.symbol=live.symbol;
 scope.magic=(ulong)live.magic;scope.ticket=known.ticket;
 RecoveryExactOrderObservation r;
 if(RecoveryObserveExactOrder(scope,r)!=RECOVERY_EXACT_ORDER_OBSERVED ||
    r.ticket!=known.ticket || r.magic!=scope.magic || r.symbol!=scope.symbol ||
    r.type!=(known.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
    r.tp!=0.0 || r.stop_limit!=0.0 ||
    !RecoveryDispatchUnits(r.price,tick,false,known.entry_ticks) ||
    !RecoveryDispatchUnits(r.sl,tick,false,known.sl_ticks) ||
    !RecoveryDispatchUnits(r.initial_volume,step,false,known.volume_steps))return RSG_FIXED_UNKNOWN;
 if(r.source==RECOVERY_EXACT_ORDER_CURRENT && r.state==ORDER_STATE_PLACED &&
    r.position_identifier==0 && RecoveryDispatchUnits(r.remaining_volume,step,false,known.volume_steps))
    return RSG_FIXED_WORKING;
 if(r.source==RECOVERY_EXACT_ORDER_HISTORY && r.state==ORDER_STATE_FILLED &&
    r.position_identifier!=0 && r.position_identifier!=close.gap.before.position_id &&
    r.remaining_volume==0.0)return RSG_FIXED_HISTORICAL;
 return RSG_FIXED_UNKNOWN;
}

bool RecoveryBeginSupportGapOwner(RecoverySupportGapOwner &task,const RecoverySupportCloseOwner &close,
    const RecoverySupportReentry &repeat,const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,RecoveryEvidenceOwner &verifier,
    const RecoveryInventoryScope &verifier_context,const double tick,const double step) {
 if(task.state!=RSG_IDLE || tick!=close.working.price_tick || step!=close.working.volume_step)
    return false;
 RRRangeState before;
 RRSupportGap gap;
 if(!RecoverySupportGapBase(close,repeat,owner,panel,session,live,scope,verifier_context,before,gap) ||
    RecoverySupportGapFixed(close,live)!=RSG_FIXED_HISTORICAL)return false;
 RROpeningPair none;
 ZeroMemory(none);
 RecoveryOwnerEvidence q;
 const ulong floor=close.working_sequence>close.working.verify_after ?
                   close.working_sequence : close.working.verify_after;
 if(!RecoveryReserveOwnerEvidence(verifier,session,live,before,none,gap.opposite.ticket,floor,tick,step,q) ||
    !RecoveryAlternatingPumpBegin(task.pump,before,none,q.binding,q.publisher_tag))
    return false;
 task.before=before;task.gap=gap;task.owner_revision=owner.revision;
 task.close_phase=close.phase;task.state=RSG_PROBE;
 return true;
}

RecoverySupportGapOwnerResult RecoveryStopSupportGapOwner(RecoverySupportGapOwner &task) {
 task.state=RSG_STOP;return RSG_UNKNOWN;
}

RecoverySupportGapOwnerResult RecoveryStepSupportGapOwner(RecoverySupportGapOwner &task,
    const RecoverySupportCloseOwner &close,const RecoverySupportReentry &repeat,
    RecoveryRangeOwner &owner,RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &verifier_context) {
 if(task.state!=RSG_PROBE)return RSG_UNKNOWN;
 RRRangeState before;RRSupportGap gap;
 if(task.owner_revision!=owner.revision || task.close_phase!=close.phase ||
    !RecoverySupportGapBase(close,repeat,owner,panel,session,live,scope,verifier_context,before,gap) ||
    before.action_serial!=task.before.action_serial ||
    !RRSupportGapBound(before,task.gap) || !RCCloseKeysEqual(gap.close.key,task.gap.close.key) ||
    !RecoveryAlternatingSameSession(task.pump.binding.inventory,verifier_context))
    return RecoveryStopSupportGapOwner(task);
 RROpeningPair none;
 ZeroMemory(none);
 RRAlternatingObservation observation;
 const RecoveryAlternatingPumpResult status=RecoveryAlternatingPumpStep(task.pump,before,none,observation);
 if(status==RAP_WAIT)return RSG_WAIT;
 if(status==RAP_PROGRESS)return RSG_PROGRESS;
 if(status!=RAP_PROOF || !observation.predecessor_inventory_complete || observation.predecessor_present ||
    !RRStageSupportGapFill(before,gap,observation.executed_order,observation.fill))
    return RecoveryStopSupportGapOwner(task);
 RRAlternationLedger ledger=owner.ledger;
 const int count=ledger.confirmed_full_sl_count;
 const RRAlternatingCloseResult closed=RRRecordAlternatingClose(ledger,gap.close);
 if((closed!=RR_ALT_CLOSE_APPLIED && closed!=RR_ALT_CLOSE_DUPLICATE) ||
    ledger.confirmed_full_sl_count!=count)
    return RecoveryStopSupportGapOwner(task);
 for(int i=0;i<ledger.position_count;i++)
    if(ledger.positions[i].bound_entry.position_id==observation.fill.entry.position_id ||
       ledger.positions[i].bound_entry.active_order==observation.fill.entry.executed_order)
       return RecoveryStopSupportGapOwner(task);
 RRAction protection;
 const RRSupportGapResult advanced=RRAdvanceSupportGap(before,panel.settings,gap,none,protection);
 if((advanced!=RR_GAP_NEXT_PAIR && advanced!=RR_GAP_FINAL_PROTECTED) ||
    RCCurrentSlot(before.cycle)!=ledger.strategy_slot)
    return RecoveryStopSupportGapOwner(task);
 RRAltCapture(before,ledger.positions[ledger.position_count-1]);
 if(!RRAltBound(before,ledger))return RecoveryStopSupportGapOwner(task);
 if(advanced==RR_GAP_NEXT_PAIR &&
    (none.sl_required || !RecoveryBuildSessionIntentKey(scope,none.reverse,task.pump.binding.price_tick,
                                                       task.pump.binding.volume_step,task.next_key) ||
     !RecoveryIntentContextOK(task.next_key)))
    return RecoveryStopSupportGapOwner(task);

 panel.ranges[owner.range_id-1]=before;owner.ledger=ledger;owner.opening=none;owner.revision++;
 owner.next_exact_ticket=0;owner.armed_position=0;RecoveryOrdinaryEventReset(owner.ordinary);
 task.owner_revision=owner.revision;
 if(advanced==RR_GAP_FINAL_PROTECTED) {owner.phase=RRO_FINAL;task.state=RSG_FINAL_STATE;return RSG_FINAL;}
 task.state=RSG_STAGED;
 return RSG_READY;
}

// Optional chart writer seam, once only after RSG_READY. A failed/uncertain
// ticket does not authorize another attempt. Raw file proof cannot call this.
bool RecoveryDispatchSupportGapOwner(RecoverySupportGapOwner &task,RecoveryRangeOwner &owner,
    const RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope) {
 if(task.state!=RSG_STAGED || owner.revision!=task.owner_revision ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) || owner.phase!=RRO_ORDINARY ||
    !owner.opening.reserved || owner.opening.sl_required)return false;
 const RRRangeState range=panel.ranges[owner.range_id-1];
 RecoveryIntentKey key;
 if(!RecoveryBuildSessionIntentKey(scope,owner.opening.reverse,task.pump.binding.price_tick,
                                   task.pump.binding.volume_step,key) ||
    !RecoveryIntentKeysMatch(task.next_key,key))
    return false;
 task.state=RSG_SENT;
 RecoveryContinueOpeningPair(range,owner.opening,task.next_key,task.pump.binding.price_tick,
                            task.pump.binding.volume_step,task.next_receipt);
 owner.next_exact_ticket=RecoveryRangeOwnerReceiptTicket(task.next_receipt);
 return true;
}
#endif
