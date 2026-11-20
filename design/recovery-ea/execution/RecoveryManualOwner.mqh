#ifndef RECOVERY_MANUAL_OWNER_MQH
#define RECOVERY_MANUAL_OWNER_MQH
#include "RecoveryPrimarySubmit.mqh"
#include "RecoveryAdoptionInventory.mqh"
#include "RecoveryAlternatingEvidence.mqh"
#include "RecoveryOpeningDispatch.mqh"

enum RecoveryManualPhase {RMO_UNUSED=0,RMO_PLANNED,RMO_ATTEMPTED,RMO_ADOPTED};

struct RecoveryManualCandidate {
 RecoveryInventoryScope request;
 ulong sequence,position_id,position_ticket,position_magic;
 int side;
 long entry_ticks,volume_steps,sl_ticks,tp_ticks;
 string entry_wire;
};

struct RecoveryManualOwner {
 RecoveryManualPhase phase;
 RecoveryManualCandidate candidate;
 ulong history_sequence;
 string history_nonce;
 RRRangeState range; // adopted position outside the configured ranges
 RROpeningPair opening;
 RRAlternationLedger ledger;
 RecoveryIntentKey sl_key,reverse_key;
 RecoveryPairSendResult receipts;
 RecoveryAdoptionBinding check;
 RecoveryAdoptionIntent intent;
};

bool RecoveryManualMode(const RecoveryPanelControl &panel) {
 return panel.mode==REC_CONTROL_MANUAL_WAIT && !panel.configured[0] && !panel.configured[1];
}

bool RecoveryManualScope(const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &q) {
 return RecoveryPrimarySession(life,live,scope) && RecoveryInvScopeOK(q) &&
    q.executor_epoch==scope.executor_epoch && q.login==scope.login && q.server==scope.server &&
    q.symbol==scope.symbol && q.magic==scope.magic && q.verifier_chart!=live.chart &&
    q.generation>0 && (ulong)q.generation==scope.generation;
}

// A complete snapshot for this symbol has exactly one position; its
// origin/magic is intentionally not an eligibility filter.
bool RecoveryManualCandidateFold(const RecoveryInventorySnapshot &inventory,
    const RecoveryInventoryScope &request,const ulong verify_after,
    const double tick,const double step,RecoveryManualCandidate &out) {
 ZeroMemory(out);
 if(!RecoveryInvScopeOK(request) || !inventory.usable || !inventory.complete ||
    inventory.sequence<=0 || (ulong)inventory.sequence<=verify_after ||
    inventory.position_count!=1 || inventory.order_count<0 || inventory.order_count>128 ||
    ArraySize(inventory.rows)!=1+inventory.order_count) return false;
 RecoveryManualCandidate found;
 ZeroMemory(found);
 string f[];
 if(StringSplit(inventory.rows[0],9,f)!=9 || !RecoveryInvPosition(f) ||
    !RecoveryAdoptionUnsigned(f[1],found.position_id) || found.position_id==0 ||
    !RecoveryAdoptionUnsigned(f[2],found.position_ticket) || found.position_ticket==0 ||
    !RecoveryAdoptionUnsigned(f[3],found.position_magic) ||
    !RecoveryAdoptionUnits(f[5],step,false,found.volume_steps) ||
    !RecoveryAdoptionUnits(f[6],tick,false,found.entry_ticks) ||
    !RecoveryAdoptionUnits(f[7],tick,true,found.sl_ticks) ||
    !RecoveryAdoptionUnits(f[8],tick,true,found.tp_ticks)) return false;
 found.side=f[4]=="BUY" ? 1 : -1;found.entry_wire=f[6];found.request=request;
 found.sequence=(ulong)inventory.sequence;
 // Don't adopt a changing partial entry whose remainder is still working.
 const string id=(string)found.position_id,ticket=(string)found.position_ticket;
 for(int i=1;i<ArraySize(inventory.rows);i++)
   {
    if(StringSplit(inventory.rows[i],9,f)!=12 || !RecoveryInvOrder(f) ||
       f[1]==id || f[1]==ticket || f[3]==id) return false;
   }
 out=found;
 return true;
}

// The typed history reader is tied to a position ID and an exact opening
// order ticket. Only a real same-side opening deal can bind that anchor.
bool RecoveryManualHistoryAnchor(const RecoveryManualCandidate &candidate,
    const RecoveryHistoryScope &request,const RecoveryHistorySnapshot &history) {
 if(!RecoveryHistoryScopeOK(request) ||
    !RecoveryAlternatingSameSession(candidate.request,request.binding) ||
    candidate.request.nonce==request.binding.nonce ||
    candidate.position_id==0 || candidate.position_id!=request.position_identifier ||
    !history.usable || !history.observed || history.sequence<=0 ||
    (ulong)history.sequence<=candidate.sequence ||
    history.order_count<0 || history.order_count>128 ||
    history.deal_count<1 || history.deal_count>128 ||
    ArraySize(history.rows)!=history.order_count+history.deal_count) return false;
 const string anchor=(string)request.anchor_order;
 string f[];
 for(int i=history.order_count;i<ArraySize(history.rows);i++) {
    ulong flow;
    if(StringSplit(history.rows[i],9,f)!=12 || !RecoveryHistoryDealFields(f) ||
       !RecoveryAdoptionUnsigned(f[5],flow))return false;
    if(f[2]==anchor && f[4]==(candidate.side==1 ? "BUY" : "SELL") && flow==(ulong)DEAL_ENTRY_IN)
       return true;
 }
 return false;
}

// Prepare from already scoped inventory and independently verified history.
// No requests are sent and panel remains MANUAL_WAIT until confirmation.
bool RecoveryPlanManualOpening(RecoveryManualOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RRSettings &cfg,
    const RecoveryInventoryScope &inventory_scope,const RecoveryInventorySnapshot &inventory,
    const RecoveryHistoryScope &history_scope,const RecoveryHistorySnapshot &history,
    const ulong verify_after,const double tick,const double step) {
 if(owner.phase!=RMO_UNUSED || !RecoveryManualMode(panel) ||
    !RecoveryManualScope(life,live,scope,inventory_scope) ||
    cfg.scope!=scope.scope || cfg.generation!=scope.generation || !RRSettingsValid(cfg))
    return false;
 RecoveryManualCandidate found;
 if(!RecoveryManualCandidateFold(inventory,inventory_scope,verify_after,tick,step,found) ||
    !RecoveryManualHistoryAnchor(found,history_scope,history))return false;
 const long initial=found.sl_ticks>0 ? found.sl_ticks : found.entry_ticks-found.side*cfg.initial_sl_ticks;
 if(!RCBoundedPositive(initial))return false;

 RecoveryManualOwner staged;
 ZeroMemory(staged);
 staged.candidate=found;staged.history_sequence=(ulong)history.sequence;
 staged.history_nonce=history_scope.binding.nonce;
 staged.range.scope=cfg.scope;staged.range.cycle.generation=cfg.generation;
 staged.range.cycle.range_id=1; // internal slot only; no configured range created
 staged.range.cycle.active_order=history_scope.anchor_order;
 staged.range.cycle.position_id=found.position_id;staged.range.cycle.side=found.side;
 staged.range.cycle.filled_steps=found.volume_steps;
 staged.range.cycle.actual_entry_ticks=found.entry_ticks;
 staged.range.position_ticket=found.position_ticket;
 staged.range.initial_sl=initial;staged.range.live_sl=found.sl_ticks;staged.range.live_tp=found.tp_ticks;
 staged.range.phase=found.sl_ticks>0 ? RR_ARM_REVERSE : RR_PROTECT;
 if(!RRStageOpeningPair(staged.range,cfg,staged.opening) ||
    !RRStartAlternation(staged.range,0,staged.ledger) ||
    !RecoveryBuildSessionIntentKey(scope,staged.opening.reverse,tick,step,staged.reverse_key))
    return false;
 if(staged.opening.sl_required &&
    !RecoveryBuildSessionIntentKey(scope,staged.opening.sl,tick,step,staged.sl_key))return false;

 staged.check.intent_scope=scope.scope;staged.check.inventory_scope=inventory_scope;
 staged.check.price_tick=tick;staged.check.volume_step=step;
 staged.check.position_volume_steps=found.volume_steps;staged.check.position_entry_wire=found.entry_wire;
 staged.intent.scope=scope.scope;staged.intent.verify_after=staged.history_sequence;
 staged.intent.position_id=found.position_id;staged.intent.position_ticket=found.position_ticket;
 staged.intent.position_side=found.side==1 ? RA_SIDE_BUY : RA_SIDE_SELL;
 staged.intent.expected_sl_ticks=initial;staged.intent.original_tp_ticks=found.tp_ticks;
 staged.intent.opposite_sl_ticks=staged.opening.reverse.sl_ticks;
 staged.intent.opposite_volume_steps=staged.opening.reverse.volume_steps;
 staged.phase=RMO_PLANNED;
 owner=staged;
 return true;
}

// Re-read stop/TP at the selected live ticket so an intervening user edit is
// not overwritten. Opening dispatch independently rechecks exact identity.
bool RecoveryManualProtectionUnchanged(const RecoveryManualOwner &owner) {
 if(!RecoveryIntentContextOK(owner.reverse_key))return false;
 double sl,tp;
 return PositionSelectByTicket(owner.candidate.position_ticket) &&
    PositionGetDouble(POSITION_SL,sl) && PositionGetDouble(POSITION_TP,tp) &&
    RecoveryDispatchUnits(sl,owner.check.price_tick,true,owner.candidate.sl_ticks) &&
    RecoveryDispatchUnits(tp,owner.check.price_tick,true,owner.candidate.tp_ticks);
}

// Consume before the one permitted attempt. Existing dispatcher uses async
// initial SL only when missing, then submits reverse without waiting for SL ACK.
bool RecoveryAttemptManualOpening(RecoveryManualOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope) {
 if(owner.phase!=RMO_PLANNED || !RecoveryManualMode(panel) ||
    !RecoveryManualScope(life,live,scope,owner.candidate.request) ||
    owner.intent.scope!=scope.scope || owner.range.scope!=scope.scope ||
    !RROpeningBound(owner.range,owner.opening))return false;
 owner.phase=RMO_ATTEMPTED;
 if(!RecoveryManualProtectionUnchanged(owner))return false;
 RecoveryDispatchOpeningPair(owner.range,owner.opening,owner.sl_key,owner.reverse_key,
       owner.check.price_tick,owner.check.volume_step,owner.receipts);
 if(owner.receipts.reverse.state==RD_TICKET_RECORDED && owner.receipts.reverse.order!=0)
    owner.intent.opposite_ticket=owner.receipts.reverse.order;
 return true;
}

// Merge only a newer, complete independent report with exact unchanged TP,
// protection and a working opposite. Neither a send ACK nor timeout adopts.
RecoveryAdoptionResult RecoveryConfirmManualOpening(RecoveryManualOwner &owner,
    RecoveryPanelControl &panel,const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &request,
    const RecoveryInventorySnapshot &inventory) {
 if(owner.phase!=RMO_ATTEMPTED || !RecoveryManualMode(panel) ||
    !RecoveryManualScope(life,live,scope,request) ||
    request.nonce==owner.candidate.request.nonce || request.nonce==owner.history_nonce ||
    !RecoveryAlternatingSameSession(owner.candidate.request,request) || inventory.position_count!=1)
    return RA_UNRESOLVED;
 const RecoveryAdoptionResult result=RecoveryFoldAdoptionInventory(inventory,request,owner.check,owner.intent);
 if(result!=RA_CONFIRMED)return result;
 string fields[];
 ulong magic;
 if(StringSplit(inventory.rows[0],9,fields)!=9 ||
    !RecoveryAdoptionUnsigned(fields[3],magic) || magic!=owner.candidate.position_magic)
    return RA_UNRESOLVED;

 RRRangeState next=owner.range;
 RROpeningPair pair=owner.opening;
 if(pair.sl_required) {
    RREffect sl;
    sl.action=pair.sl;sl.terminal_verified=true;sl.created_order=0;
    if(RRConfirmOpeningPair(next,pair,sl)!=RR_PAIR_WAITING)return RA_UNRESOLVED;
 }
 RREffect reverse;
 reverse.action=pair.reverse;reverse.terminal_verified=true;reverse.created_order=owner.intent.opposite_ticket;
 if(RRConfirmOpeningPair(next,pair,reverse)!=RR_PAIR_CONFIRMED)return RA_UNRESOLVED;
 owner.range=next;owner.opening=pair;owner.phase=RMO_ADOPTED;
 panel.mode=REC_CONTROL_ADOPTED;
 return RA_CONFIRMED;
}
#endif
