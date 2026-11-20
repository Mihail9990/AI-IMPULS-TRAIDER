#ifndef RECOVERY_OWNER_EVIDENCE_MQH
#define RECOVERY_OWNER_EVIDENCE_MQH
#include "RecoveryAlternatingFillHint.mqh"
#include "RecoverySessionIntentKey.mqh"
#include "RecoverySessionLifecycle.mqh"

// Boot-local, one instance owned by the main EA for both ranges. Never restore
// from disk and never reset the counter inside the same executor epoch.
struct RecoveryEvidenceOwner {
 RecoverySessionContext context;
 string executor_epoch,verifier_epoch;
 long verifier_chart;
 ulong serial;
};
struct RecoveryOwnerEvidence {
 RecoveryAlternatingEvidenceBinding binding;
 string publisher_tag;
 ulong serial,prior_order,prior_position,prior_ticket;
 int range_id;
};

bool RecoveryBeginEvidenceOwner(RecoveryEvidenceOwner &owner,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const string verifier_epoch,const long verifier_chart) {
 if(owner.executor_epoch!="" || owner.serial!=0 || !RecoverySessionCurrent(session,live) ||
    !RecoveryInvHex(session.epoch) || !RecoveryInvHex(verifier_epoch) ||
    session.epoch==verifier_epoch || verifier_chart<=0 || verifier_chart==live.chart ||
    !RecoveryInvContext(live.server) || !RecoveryInvContext(live.symbol))
    return false;
 owner.context=live;owner.executor_epoch=session.epoch;owner.verifier_epoch=verifier_epoch;
 owner.verifier_chart=verifier_chart;
 return true;
}

string RecoveryOwnerNonce(const string lane,const string identity) {
 const string digest=RecoverySessionSha256("RECOVERY_EVIDENCE_OWNER_V1\t"+lane+"\t"+identity);
 if(StringLen(digest)!=64)return "";
 return StringSubstr(digest,0,32);
}

// exact_ticket MUST be the owner-linked prior request's exact broker order
// ticket, never an inferred price/position match or a callback's ticket. For
// a verified working order it is checked against the confirmed state; for the
// pending opening-pair route, the caller supplies that immutable association.
bool RecoveryReserveOwnerEvidence(RecoveryEvidenceOwner &owner,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RRRangeState &range,const RROpeningPair &opening,const ulong exact_ticket,
    const ulong verifier_after,const double price_tick,const double volume_step,
    RecoveryOwnerEvidence &out) {
 ZeroMemory(out);
 if(!RecoverySessionCurrent(session,live) || session.epoch!=owner.executor_epoch ||
    !RecoverySessionScopeEqual(owner.context,live) || owner.serial==ULONG_MAX ||
    range.scope==0 || (range.cycle.range_id!=1 && range.cycle.range_id!=2) ||
    range.cycle.generation==0 || range.cycle.generation>(ulong)LONG_MAX ||
    exact_ticket==0 || verifier_after>(ulong)LONG_MAX-3 ||
    !MathIsValidNumber(price_tick) || price_tick<=0 ||
    !MathIsValidNumber(volume_step) || volume_step<=0)
    return false;

 const ulong serial=owner.serial+1;
 const string identity=owner.executor_epoch+"\t"+owner.verifier_epoch+"\t"+
    (string)owner.verifier_chart+"\t"+(string)live.login+"\t"+live.server+"\t"+live.symbol+"\t"+
    (string)live.magic+"\t"+(string)range.scope+"\t"+(string)range.cycle.generation+"\t"+
    (string)range.cycle.range_id+"\t"+(string)exact_ticket+"\t"+(string)serial;
 RecoveryInventoryScope ids;
 ids.nonce=RecoveryOwnerNonce("exact",identity);
 ids.executor_epoch=owner.executor_epoch;ids.verifier_epoch=owner.verifier_epoch;
 ids.generation=(long)range.cycle.generation;ids.login=live.login;
 ids.server=live.server;ids.symbol=live.symbol;ids.magic=live.magic;
 ids.verifier_chart=owner.verifier_chart;
 RecoveryAlternatingEvidenceBinding b;
 ZeroMemory(b);
 b.exact.binding=ids;b.exact.order_ticket=exact_ticket;
 ids.nonce=RecoveryOwnerNonce("history",identity);
 b.history.binding=ids;b.history.anchor_order=exact_ticket;
 b.history.position_identifier=0; // Filled only after the exact-order report.
 ids.nonce=RecoveryOwnerNonce("inventory",identity);
 b.inventory=ids;b.scope=range.scope;b.verify_after=verifier_after;
 b.price_tick=price_tick;b.volume_step=volume_step;
 if(opening.reserved)b.routed=opening.reverse;
 const string tag=RecoveryOwnerNonce("publisher",identity);
 if(tag==b.exact.binding.nonce || tag==b.history.binding.nonce || tag==b.inventory.nonce)
    return false;

 // Existing pump validator checks exact current order semantics and all
 // nonce bindings; this call stages no verifier I/O or broker operation.
 RecoveryAlternatingPump validation;
 validation.state=RAP_UNUSED;
 if(!RecoveryAlternatingPumpBegin(validation,range,opening,b,tag))return false;
 owner.serial=serial;
 out.binding=b;out.publisher_tag=tag;out.serial=serial;
 out.range_id=validation.range_id;out.prior_order=validation.prior_order;
 out.prior_position=validation.prior_position;out.prior_ticket=validation.prior_ticket;
 return true;
}

bool RecoveryArmFrozenOwnerEvidence(RecoveryFillHint &hint,const RecoveryEvidenceOwner &owner,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RRRangeState &range,const RROpeningPair &opening,const RecoveryOwnerEvidence &frozen) {
 const RecoveryInventoryScope ids=frozen.binding.exact.binding;
 if(!RecoverySessionCurrent(session,live) || session.epoch!=owner.executor_epoch ||
    !RecoverySessionScopeEqual(owner.context,live) || ids.executor_epoch!=owner.executor_epoch ||
    ids.verifier_epoch!=owner.verifier_epoch || ids.verifier_chart!=owner.verifier_chart ||
    ids.login!=live.login || ids.server!=live.server || ids.symbol!=live.symbol ||
    ids.magic!=live.magic || frozen.serial==0 || frozen.serial>owner.serial ||
    frozen.range_id!=range.cycle.range_id ||
    frozen.prior_order!=range.cycle.active_order || frozen.prior_position!=range.cycle.position_id ||
    frozen.prior_ticket!=range.position_ticket)
    return false;
 return RecoveryArmFillHint(hint,range,opening,frozen.binding,frozen.publisher_tag);
}
#endif
