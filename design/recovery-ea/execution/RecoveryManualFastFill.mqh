#ifndef RECOVERY_MANUAL_FAST_FILL_MQH
#define RECOVERY_MANUAL_FAST_FILL_MQH
#include "RecoveryManualHandoff.mqh"

struct RecoveryManualFastFill {
 RecoveryManualHandoff projected; // private until independent finished fill
 RRAction reverse;
 RecoveryIntentKey reverse_key;
 ulong ticket;
};

bool RecoveryManualFastSource(const RecoveryManualChart &task,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope) {
 if(task.owner.phase!=RMO_ATTEMPTED || !RecoveryManualMode(panel) ||
    (task.phase!=RMC_EFFECT_VISIBLE && task.phase!=RMC_EFFECT_REPORT && task.phase!=RMC_UNKNOWN) ||
    !RecoveryManualScope(life,live,scope,task.owner.candidate.request) ||
    !RecoveryAlternatingSameSession(task.owner.candidate.request,task.confirm) ||
    task.owner.intent.scope!=scope.scope || task.owner.range.scope!=scope.scope ||
    task.owner.range.cycle.generation!=scope.generation || task.owner.range.cycle.range_id!=1 ||
    !RROpeningBound(task.owner.range,task.owner.opening) ||
    !RRAltBound(task.owner.range,task.owner.ledger))return false;
 const RRRangeState range=task.owner.range;
 const ulong exact=task.owner.intent.opposite_ticket;
 return exact!=0 && task.owner.receipts.reverse.state==RD_TICKET_RECORDED &&
    task.owner.receipts.reverse.order==exact && range.cycle.position_id==task.owner.candidate.position_id &&
    range.position_ticket==task.owner.candidate.position_ticket && range.cycle.active_order==task.history.anchor_order &&
    range.cycle.actual_entry_ticks==task.owner.candidate.entry_ticks &&
    range.cycle.side==task.owner.candidate.side && range.cycle.filled_steps==task.owner.candidate.volume_steps &&
    range.live_tp==task.owner.intent.original_tp_ticks && range.live_tp==task.owner.candidate.tp_ticks &&
    range.live_sl==task.owner.candidate.sl_ticks && range.initial_sl==task.owner.intent.expected_sl_ticks;
}

// Exactly one in-session receipt key is admissible. No similar stop lookup.
bool RecoveryManualFastKey(const RecoveryManualChart &task,const RecoverySessionIntentScope &scope,
    const double tick,const double step,RecoveryIntentKey &key) {
 string ticket;
 return tick==task.owner.check.price_tick && step==task.owner.check.volume_step &&
    RecoveryBuildSessionIntentKey(scope,task.owner.opening.reverse,tick,step,key) &&
    RecoveryIntentKeysMatch(key,task.owner.reverse_key) &&
    RecoveryIntentLookup(key,ticket)==RI_TICKET && ticket==(string)task.owner.intent.opposite_ticket;
}

bool RecoverySeedManualFastFill(RecoveryManualFastFill &fast,const RecoveryManualChart &task,
    const RecoveryPanelControl &panel,const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RRSettings &cfg,const double tick,const double step) {
 if(fast.projected.seeded || !RecoveryManualFastSource(task,panel,life,live,scope) ||
    !RRValid(task.owner.range,cfg) || !RecoveryManualFastKey(task,scope,tick,step,fast.reverse_key))return false;
 ulong first_magic;
 if(!RecoveryManualOriginMagic(task,first_magic))return false;
 const RRRangeState s=task.owner.range;
 RecoveryManualHandoff next;
 ZeroMemory(next);
 next.first_position_id=s.cycle.position_id;next.first_position_ticket=s.position_ticket;
 next.first_position_magic=first_magic;
 next.backing.mode=REC_CONTROL_RANGES;next.backing.settings=cfg;
 next.backing.configured[0]=true;next.backing.ranges[0]=s;
 next.ordinary.phase=RRO_ORDINARY;next.ordinary.range_id=1;
 next.ordinary.executor_epoch=scope.executor_epoch;next.ordinary.scope=scope.scope;
 next.ordinary.generation=scope.generation;next.ordinary.revision=1;
 next.ordinary.opening=task.owner.opening;next.ordinary.ledger=task.owner.ledger;
 next.ordinary.next_exact_ticket=task.owner.intent.opposite_ticket;
 if(!RecoveryRangeOwnerBound(next.ordinary,next.backing,life,live,scope))return false;
 next.seeded=true;fast.projected=next;fast.ticket=next.ordinary.next_exact_ticket;
 fast.reverse=task.owner.opening.reverse;
 return true;
}

bool RecoveryManualFastStillBound(const RecoveryManualFastFill &fast,const RecoveryManualChart &task,
    const RecoveryPanelControl &panel,const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope) {
 if(!fast.projected.seeded || !RecoveryManualFastSource(task,panel,life,live,scope) ||
    fast.ticket!=task.owner.intent.opposite_ticket || fast.projected.ordinary.next_exact_ticket!=fast.ticket ||
    !RecoveryIntentKeysMatch(fast.reverse_key,task.owner.reverse_key) ||
    !RRActionEqual(fast.reverse,task.owner.opening.reverse) ||
    !RecoveryRangeOwnerBound(fast.projected.ordinary,fast.projected.backing,life,live,scope))return false;
 const RRRangeState prior=fast.projected.backing.ranges[0];
 return RCCPSameRange(prior.cycle,task.owner.range.cycle) &&
    prior.position_ticket==task.owner.range.position_ticket &&
    prior.live_sl==task.owner.range.live_sl && prior.live_tp==task.owner.range.live_tp &&
    RRActionEqual(prior.pending,task.owner.range.pending);
}

bool RecoveryManualFastFillTrade(RecoveryManualFastFill &fast,const RecoveryManualChart &task,
   const RecoveryPanelControl &panel,const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
   const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,
   const MqlTradeTransaction &event) {
 return RecoveryManualFastStillBound(fast,task,panel,life,live,scope) &&
    RecoveryRangeOwnerOrdinaryTrade(fast.projected.ordinary,fast.projected.backing,life,live,scope,verifier,event);
}

// Reuses the same complete exact/order-history/inventory pump as ordinary
// fills. A notification or receipt alone never publishes ADOPTED. The original
// SL and TP are untouched; any prior close must be proved independently.
RecoveryAlternatingServiceResult RecoveryManualFastFillTimer(RecoveryManualFastFill &fast,
    const RecoveryManualChart &task,RecoveryManualHandoff &adopted,RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,RecoveryEvidenceOwner &owner,
    const RecoveryInventoryScope &verifier,const RRSettings &cfg,const double tick,const double step,
    RecoverySessionAlternatingResult &result) {
 ZeroMemory(result);
 if(adopted.seeded ||
    !RecoveryAlternatingSameSession(task.owner.candidate.request,verifier))return RAS_NO_EFFECT;
 if(!fast.projected.seeded && !RecoverySeedManualFastFill(fast,task,panel,life,live,scope,cfg,tick,step))
    return RAS_NO_EFFECT;
 RecoveryIntentKey current;
 if(!RecoveryManualFastStillBound(fast,task,panel,life,live,scope) ||
    !RecoveryManualFastKey(task,scope,tick,step,current) ||
    !RecoveryIntentKeysMatch(current,fast.reverse_key))return RAS_NO_EFFECT;
 RecoveryRangeOwnerArmOrdinary(fast.projected.ordinary,fast.projected.backing,
                               life,live,scope,owner,task.owner.history_sequence,tick,step);
 const RecoveryAlternatingServiceResult ready=RecoveryRangeOwnerOrdinaryTimer(fast.projected.ordinary,
                               fast.projected.backing,life,live,scope,verifier,result);
 if(ready!=RAS_ADVANCED)return ready;
 // Preserve the owner's revision, ledger and exact new pending intent/receipt.
 // The original manual attempt remains ATTEMPTED, not falsely confirmed.
 adopted=fast.projected;panel.mode=REC_CONTROL_ADOPTED;
 return RAS_ADVANCED;
}
#endif
