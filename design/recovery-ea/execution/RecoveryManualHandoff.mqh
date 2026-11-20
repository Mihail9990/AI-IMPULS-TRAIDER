#ifndef RECOVERY_MANUAL_HANDOFF_MQH
#define RECOVERY_MANUAL_HANDOFF_MQH
#include "RecoveryManualChart.mqh"
#include "RecoveryRangeOwner.mqh"

// The internal slot is a projection for the ordinary owner APIs only. It
// must never be exposed or passed to the configured-range/primary/OCO path.
struct RecoveryManualHandoff {
 bool seeded;
 ulong first_position_id,first_position_ticket,first_position_magic;
 RecoveryPanelControl backing;
 RecoveryRangeOwner ordinary;
};

bool RecoveryManualHandoffPublic(const RecoveryPanelControl &panel) {
 return panel.mode==REC_CONTROL_ADOPTED && !panel.configured[0] && !panel.configured[1];
}

// Prove the originally adopted position's magic from its exact pre-attempt
// independent inventory. Its magic need not equal the EA's order magic.
bool RecoveryManualOriginMagic(const RecoveryManualChart &task,ulong &magic) {
 magic=0;
 const RecoveryManualCandidate f=task.owner.candidate;
 if(!task.inventory.usable || !task.inventory.complete || task.inventory.position_count!=1 ||
    task.inventory.sequence<=0 || (ulong)task.inventory.sequence!=f.sequence ||
    ArraySize(task.inventory.rows)<1 || f.request.nonce!=task.initial.nonce ||
    !RecoveryAlternatingSameSession(f.request,task.initial))return false;
 string fields[];
 ulong id,ticket;
 return StringSplit(task.inventory.rows[0],9,fields)==9 && RecoveryInvPosition(fields) &&
        RecoveryAdoptionUnsigned(fields[1],id) && id==f.position_id &&
        RecoveryAdoptionUnsigned(fields[2],ticket) && ticket==f.position_ticket &&
        RecoveryAdoptionUnsigned(fields[3],magic);
}

// Only the exact original adopted identity may use an external position magic.
// New successor positions must use the configured strategy magic.
bool RecoveryManualHandoffOrigin(const RecoveryManualHandoff &handoff,const RRRangeState &range,
                                  ulong &expected_magic) {
 expected_magic=0;
 if(!handoff.seeded || range.scope!=handoff.ordinary.scope ||
    range.cycle.generation!=handoff.ordinary.generation || range.cycle.range_id!=handoff.ordinary.range_id ||
    range.cycle.position_id==0 || range.cycle.position_id!=handoff.first_position_id ||
    range.position_ticket!=handoff.first_position_ticket)return false;
 expected_magic=handoff.first_position_magic;
 return true;
}

bool RecoverySeedManualHandoff(RecoveryManualHandoff &handoff,const RecoveryManualChart &task,
   const RecoveryPanelControl &panel,const RecoverySessionLifecycle &life,
   const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,const RRSettings &cfg) {
 if(handoff.seeded || task.phase!=RMC_ADOPTED || task.owner.phase!=RMO_ADOPTED ||
    !RecoveryManualHandoffPublic(panel) || !RecoveryManualScope(life,live,scope,task.confirm) ||
    !RecoveryAlternatingSameSession(task.owner.candidate.request,task.confirm) ||
    cfg.scope!=scope.scope || cfg.generation!=scope.generation || !RRSettingsValid(cfg))return false;
 const RRRangeState s=task.owner.range;
 const ulong exact=task.owner.intent.opposite_ticket;
 if(!RRValid(s,cfg) || !RRAltBound(s,task.owner.ledger) ||
    s.cycle.range_id!=1 || s.phase!=RR_ACTIVE || s.pending.kind!=RR_NONE ||
    s.cycle.position_id!=task.owner.candidate.position_id || s.position_ticket!=task.owner.candidate.position_ticket ||
    s.cycle.active_order!=task.history.anchor_order || task.history.position_identifier!=s.cycle.position_id ||
    task.owner.history_nonce!=task.history.binding.nonce ||
    s.cycle.actual_entry_ticks!=task.owner.candidate.entry_ticks ||
    s.cycle.side!=task.owner.candidate.side || s.cycle.filled_steps!=task.owner.candidate.volume_steps ||
    s.initial_sl!=task.owner.intent.expected_sl_ticks || s.live_sl!=s.initial_sl ||
    s.live_tp!=task.owner.intent.original_tp_ticks || s.live_tp!=task.owner.candidate.tp_ticks ||
    (task.owner.candidate.sl_ticks>0 && s.live_sl!=task.owner.candidate.sl_ticks) ||
    s.entry.ticket!=0 || s.retire.ticket!=0 || s.reentry_slot!=0 ||
    task.owner.opening.reserved || !task.owner.opening.sl_verified || !task.owner.opening.reverse_verified ||
    exact==0 || s.opposite.ticket!=exact || task.owner.opening.reverse_ticket!=exact ||
    task.owner.receipts.reverse.state!=RD_TICKET_RECORDED || task.owner.receipts.reverse.order!=exact ||
    s.opposite.side!=-s.cycle.side || s.opposite.entry_ticks!=RROppositeAnchor(s) ||
    s.opposite.sl_ticks!=task.owner.opening.reverse.sl_ticks ||
    s.opposite.volume_steps!=task.owner.opening.reverse.volume_steps)return false;
 ulong first_magic;
 if(!RecoveryManualOriginMagic(task,first_magic))return false;
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
 next.ordinary.next_exact_ticket=exact;
 if(!RecoveryRangeOwnerBound(next.ordinary,next.backing,life,live,scope))return false;
 next.seeded=true;handoff=next;
 return true;
}

bool RecoveryManualHandoffTrade(RecoveryManualHandoff &handoff,const RecoveryPanelControl &panel,
   const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
   const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,
   const MqlTradeTransaction &event) {
 return handoff.seeded && RecoveryManualHandoffPublic(panel) &&
    RecoveryRangeOwnerOrdinaryTrade(handoff.ordinary,handoff.backing,life,live,scope,verifier,event);
}

RecoveryAlternatingServiceResult RecoveryManualHandoffTimer(RecoveryManualHandoff &handoff,
   const RecoveryPanelControl &panel,const RecoverySessionLifecycle &life,
   const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
   RecoveryEvidenceOwner &owner,const RecoveryInventoryScope &verifier,
   const double tick,const double step,RecoverySessionAlternatingResult &result) {
 ZeroMemory(result);
 if(!handoff.seeded || !RecoveryManualHandoffPublic(panel) ||
    !RecoveryRangeOwnerBound(handoff.ordinary,handoff.backing,life,live,scope))
    return RAS_NO_EFFECT;
 RecoveryRangeOwnerArmOrdinary(handoff.ordinary,handoff.backing,life,live,scope,owner,0,tick,step);
 return RecoveryRangeOwnerOrdinaryTimer(handoff.ordinary,handoff.backing,life,live,scope,verifier,result);
}
#endif
