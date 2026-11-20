#ifndef RECOVERY_RANGE_OWNER_MQH
#define RECOVERY_RANGE_OWNER_MQH
#include "RecoveryPrimaryService.mqh"
#include "RecoveryPrimaryFillPump.mqh"
#include "RecoveryOrdinaryEventLoop.mqh"
#include "RecoveryOwnerEvidence.mqh"

enum RecoveryRangeOwnerPhase {
 RRO_UNUSED=0,RRO_PRIMARY_PENDING,RRO_ORDINARY,RRO_FINAL,
 RRO_PRIMARY_CLAIMED,RRO_PRIMARY_DISPATCH_ATTEMPTED
};

// Shared by both ranges in a fresh boot. A winning exact fill is latched
// before any follow-up submission. Broker fill chronology is not inferred.
struct RecoveryRangeOco {
 string executor_epoch;
 ulong scope,generation,winner_order,winner_position;
 int winner_range_id;
};

bool RecoveryRangeOcoWithin(const RecoveryRangeOco &oco,const RecoverySessionIntentScope &scope) {
 return (oco.winner_range_id==1 || oco.winner_range_id==2) &&
    oco.winner_order!=0 && oco.winner_position!=0 &&
    oco.executor_epoch==scope.executor_epoch && oco.scope==scope.scope &&
    oco.generation==scope.generation;
}

bool RecoveryRangeOcoMaySubmit(const RecoveryRangeOco &oco) {
 return oco.winner_range_id==0 && oco.winner_order==0 && oco.winner_position==0 &&
        oco.scope==0 && oco.generation==0 && oco.executor_epoch=="";
}

// One boot-local owner per configured panel slot. The authoritative range is
// always panel.ranges[range_id-1]; this object never stores a second range.
struct RecoveryRangeOwner {
 RecoveryRangeOwnerPhase phase;
 int range_id;
 string executor_epoch;
 ulong scope,generation,revision,next_exact_ticket,armed_position;
 RecoveryPrimarySubmitResult primary;
 RRFilled first_proof; // exact winning proof, retained for the cancel service
 RecoveryPrimaryOpenResult first_open;
 RecoveryPrimaryFillPump primary_pump;
 RROpeningPair opening;
 RRAlternationLedger ledger;
 RecoveryAlternatingOutbox outbox;
 RecoveryOrdinaryEventLoop ordinary;
};

bool RecoveryRangeOwnerBound(const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope) {
 if(owner.phase==RRO_UNUSED || owner.range_id<1 || owner.range_id>2 ||
    !RecoveryPrimarySession(session,live,scope) || owner.executor_epoch!=scope.executor_epoch ||
    owner.scope!=scope.scope || owner.generation!=scope.generation ||
    panel.mode!=REC_CONTROL_RANGES || !panel.configured[owner.range_id-1] ||
    panel.settings.scope!=owner.scope || panel.settings.generation!=owner.generation)
    return false;
 const RRRangeState range=panel.ranges[owner.range_id-1];
 return range.scope==owner.scope && range.cycle.generation==owner.generation &&
        range.cycle.range_id==owner.range_id && RRValid(range,panel.settings);
}

bool RecoveryRangeOwnerSubmit(RecoveryRangeOwner &owner,RecoveryPanelControl &panel,
    const int range_id,const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const double tick,const double step) {
 if(owner.phase!=RRO_UNUSED)return false;
 RecoveryPrimarySubmitResult result;
 if(!RecoverySubmitPanelPrimary(panel,range_id,session,live,scope,tick,step,result))return false;
 owner.range_id=range_id;owner.scope=scope.scope;owner.generation=scope.generation;
 owner.executor_epoch=scope.executor_epoch;owner.primary=result;owner.phase=RRO_PRIMARY_PENDING;
 return true; // a single attempt, even when no exact ticket was assigned
}

// The primary pump may use this check before starting independent reports.
bool RecoveryRangeOwnerPrimaryTicket(const RecoveryRangeOwner &owner,
    const RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,ulong &ticket) {
 ticket=0;
 if(owner.phase!=RRO_PRIMARY_PENDING || !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    owner.primary.ticket==0 || owner.primary.receipt.state!=RD_TICKET_RECORDED ||
    owner.primary.ticket!=owner.primary.receipt.order)
    return false;
 const RRRangeState range=panel.ranges[owner.range_id-1];
 if(range.phase!=RR_NEW || !RRActionEqual(range.pending,owner.primary.action))
    return false;
 ticket=owner.primary.ticket;return true;
}

ulong RecoveryRangeOwnerReceiptTicket(const RecoveryDispatchResult &receipt) {
 return receipt.state==RD_TICKET_RECORDED ? receipt.order : 0;
}

// Allocate primary verifier messages from the same monotone boot owner as
// the ordinary loop, but distinct lanes. No verifier or broker I/O at bind.
bool RecoveryRangeOwnerBindPrimary(RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,RecoveryEvidenceOwner &verifier,
    const ulong verify_after,const double tick,const double step) {
 ulong ticket;
 if(owner.primary_pump.state!=RPP_UNUSED ||
    !RecoveryRangeOwnerPrimaryTicket(owner,panel,session,live,scope,ticket) ||
    verifier.executor_epoch!=session.epoch || !RecoverySessionScopeEqual(verifier.context,live) ||
    verifier.serial==ULONG_MAX || verify_after>(ulong)LONG_MAX-3)
    return false;
 RecoveryIntentKey rebuilt;
 if(!RecoveryBuildSessionIntentKey(scope,owner.primary.action,tick,step,rebuilt) ||
    rebuilt.id!=owner.primary.key.id || rebuilt.fingerprint!=owner.primary.key.fingerprint)
    return false;
 const ulong serial=verifier.serial+1;
 const string identity=scope.executor_epoch+"\t"+verifier.verifier_epoch+"\t"+
    (string)verifier.verifier_chart+"\t"+(string)live.login+"\t"+live.server+"\t"+live.symbol+
    "\t"+(string)live.magic+"\t"+(string)scope.scope+"\t"+(string)scope.generation+
    "\t"+(string)owner.range_id+"\t"+(string)ticket+"\t"+(string)serial;
 RecoveryInventoryScope context;
 context.executor_epoch=scope.executor_epoch;context.verifier_epoch=verifier.verifier_epoch;
 context.generation=(long)scope.generation;context.login=live.login;
 context.server=live.server;context.symbol=live.symbol;context.magic=live.magic;
 context.verifier_chart=verifier.verifier_chart;
 RecoveryPrimaryFillBinding binding;
 ZeroMemory(binding);
 context.nonce=RecoveryOwnerNonce("primary-exact",identity);
 binding.exact.binding=context;binding.exact.order_ticket=ticket;
 context.nonce=RecoveryOwnerNonce("primary-history",identity);
 binding.history.binding=context;binding.history.anchor_order=ticket;
 context.nonce=RecoveryOwnerNonce("primary-inventory",identity);
 binding.inventory=context;binding.scope=scope.scope;binding.verify_after=verify_after;
 binding.routed=owner.primary.action;binding.price_tick=tick;binding.volume_step=step;
 const string tag=RecoveryOwnerNonce("primary-publisher",identity);
 if(tag==binding.exact.binding.nonce || tag==binding.history.binding.nonce || tag==binding.inventory.nonce ||
    !RecoveryPrimaryPumpBegin(owner.primary_pump,panel.ranges[owner.range_id-1],panel.settings,binding,tag))
    return false;
 verifier.serial=serial;
 return true;
}

// Only the owner of an exact, independently verified primary fill can
// claim. Once chosen, the other timer stops without inferring an outcome.
bool RecoveryRangeOcoClaim(RecoveryRangeOco &oco,
    const RecoveryRangeOwner &owner,const RecoverySessionIntentScope &scope,
    const RecoveryPrimaryFillBinding &binding,const RRFilled &proof) {
 if(!RecoveryRangeOcoMaySubmit(oco) || owner.phase!=RRO_PRIMARY_PENDING ||
    owner.executor_epoch!=scope.executor_epoch || owner.scope!=scope.scope || owner.generation!=scope.generation ||
    (owner.range_id!=1 && owner.range_id!=2) || owner.primary.ticket==0 ||
    !proof.independently_verified || !proof.entry.independently_verified ||
    !proof.entry.history_complete || !proof.entry.remainder_reconciled ||
    proof.entry.generation!=owner.generation || proof.entry.range_id!=owner.range_id ||
    proof.entry.position_id==0 || proof.position_ticket==0 ||
    proof.entry.expected_order!=owner.primary.ticket || proof.entry.executed_order!=owner.primary.ticket ||
    binding.scope!=owner.scope || binding.exact.order_ticket!=owner.primary.ticket ||
    binding.history.anchor_order!=owner.primary.ticket ||
    proof.entry.position_id!=binding.history.position_identifier ||
    !RCBoundedPositive(proof.entry.actual_entry_ticks) || proof.entry.side!=owner.primary.action.side || proof.entry.planned_steps!=owner.primary.action.volume_steps ||
    !RCBoundedPositive(proof.entry.filled_steps) || proof.entry.filled_steps>proof.entry.planned_steps ||
    proof.entry.active_steps!=proof.entry.filled_steps)
    return false;
 oco.executor_epoch=scope.executor_epoch;oco.scope=scope.scope;oco.generation=scope.generation;
 oco.winner_order=owner.primary.ticket;oco.winner_position=proof.entry.position_id;
 oco.winner_range_id=owner.range_id;
 return true;
}

// Advance only from the already-normalized proof yielded by this range's
// primary pump. A repeated broker notification cannot invoke this call twice.
RecoveryPrimaryPumpResult RecoveryRangeOwnerPrimaryTimer(RecoveryRangeOwner &owner,
    RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &live_verifier,RecoveryRangeOco &oco) {
 ulong ticket;
 if(owner.primary_pump.state==RPP_UNUSED ||
    !RecoveryRangeOcoMaySubmit(oco) ||
    !RecoveryRangeOwnerPrimaryTicket(owner,panel,session,live,scope,ticket) ||
    owner.primary_pump.binding.exact.order_ticket!=ticket ||
    !RecoveryAlternatingSameSession(owner.primary_pump.binding.inventory,live_verifier))
    return RPP_STOPPED;
 RRFilled proof;
 const RecoveryPrimaryPumpResult result=RecoveryPrimaryPumpStep(owner.primary_pump,
                              panel.ranges[owner.range_id-1],panel.settings,proof);
 if(result!=RPP_PROOF)return result;
 if(!RecoveryRangeOcoClaim(oco,owner,scope,owner.primary_pump.binding,proof))return RPP_STOPPED;
 owner.first_proof=proof;
 owner.phase=RRO_PRIMARY_CLAIMED;
 return RPP_PROOF;
}

// The chart calls this after attempting the sibling cancel, without waiting
// for a cancellation receipt. It consumes exactly one opening dispatch.
bool RecoveryRangeOwnerDispatchClaimedPrimary(RecoveryRangeOwner &owner,
    RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryRangeOco &oco) {
 if(owner.phase!=RRO_PRIMARY_CLAIMED || !RecoveryRangeOcoWithin(oco,scope) ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    oco.winner_range_id!=owner.range_id || oco.winner_order!=owner.primary.ticket ||
    oco.winner_order!=owner.first_proof.entry.executed_order ||
    oco.winner_position!=owner.first_proof.entry.position_id)
    return false;
 owner.phase=RRO_PRIMARY_DISPATCH_ATTEMPTED;
 if(!RecoveryOpenVerifiedPrimaryProof(owner.primary_pump.binding,session,live,scope,owner.first_proof,
            panel.ranges[owner.range_id-1],panel.settings,owner.opening,owner.ledger,owner.first_open))
    return false;
 owner.revision=1;owner.phase=RRO_ORDINARY;
 owner.next_exact_ticket=RecoveryRangeOwnerReceiptTicket(owner.first_open.receipts.reverse);
 return true; // staged reverse, not broker proof
}

void RecoveryRangeOwnerSnapshot(const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    RecoveryAlternatingState &out) {
 out.range=panel.ranges[owner.range_id-1];out.opening=owner.opening;
 out.ledger=owner.ledger;out.revision=owner.revision;
}

bool RecoveryRangeOwnerArmOrdinary(RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,RecoveryEvidenceOwner &verifier,
    const ulong verify_after,const double tick,const double step) {
 if(owner.phase!=RRO_ORDINARY || !RecoveryRangeOwnerBound(owner,panel,session,live,scope))
    return false;
 RecoveryAlternatingState now;
 RecoveryRangeOwnerSnapshot(owner,panel,now);
 if(!RRAltBound(now.range,now.ledger) || owner.armed_position==now.range.cycle.position_id)
    return false;
 ulong ticket=owner.next_exact_ticket;
 if(now.opening.reserved && now.opening.reverse_verified)ticket=now.opening.reverse_ticket;
 else if(!now.opening.reserved && now.range.opposite.ticket!=0)ticket=now.range.opposite.ticket;
 if(ticket==0)return false;
 RecoveryOwnerEvidence evidence;
 RecoveryEvidenceOwner next_verifier=verifier;
 if(!RecoveryReserveOwnerEvidence(next_verifier,session,live,now.range,now.opening,ticket,
                                  verify_after,tick,step,evidence) ||
    !RecoveryOrdinaryEventBind(owner.ordinary,session,live,now,evidence.binding,evidence.publisher_tag))
    return false;
 verifier=next_verifier;
 owner.armed_position=now.range.cycle.position_id;
 return true;
}

bool RecoveryRangeOwnerOrdinaryTrade(RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &live_verifier,
    const MqlTradeTransaction &event) {
 return owner.phase==RRO_ORDINARY && RecoveryRangeOwnerBound(owner,panel,session,live,scope) &&
    RecoveryOrdinaryOnTrade(event,session,live,live_verifier,owner.ordinary);
}

RecoveryAlternatingServiceResult RecoveryRangeOwnerOrdinaryTimer(RecoveryRangeOwner &owner,
    RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &live_verifier,RecoverySessionAlternatingResult &result) {
 ZeroMemory(result);
 if(owner.phase!=RRO_ORDINARY || !RecoveryRangeOwnerBound(owner,panel,session,live,scope))
    return RAS_NO_EFFECT;
 // Scratch state exists only during this non-interleaved EA event; copy
 // from the current panel slot, never from a cached prior range snapshot.
 RecoveryAlternatingState now;
 RecoveryRangeOwnerSnapshot(owner,panel,now);
 if(!RRAltBound(now.range,now.ledger))return RAS_NO_EFFECT;
 const RecoveryAlternatingServiceResult action=RecoveryOrdinaryOnTimer(session,live,live_verifier,
                           owner.ordinary,now,owner.outbox,panel.settings,result);
 if(action==RAS_ADVANCED) {
    panel.ranges[owner.range_id-1]=now.range;
    owner.opening=now.opening;owner.ledger=now.ledger;owner.revision=now.revision;
    owner.next_exact_ticket=RecoveryRangeOwnerReceiptTicket(result.receipt);
    if(result.transition==RR_ALT_FINAL_NO_TRIGGER)owner.phase=RRO_FINAL;
 }
 return action;
}
#endif
