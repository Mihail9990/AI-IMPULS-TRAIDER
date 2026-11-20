#ifndef RECOVERY_ALTERNATING_FILL_FALLBACK_MQH
#define RECOVERY_ALTERNATING_FILL_FALLBACK_MQH
#include "RecoveryAlternatingFillHint.mqh"
#include "../verifier/RecoveryExactOrder.mqh"

// Timer-only read of the already-armed ticket. This is a scheduling hint,
// not an independently verified fill. Never select by price or by volume.
bool RecoveryPollExactFillHint(RecoveryFillHint &h,const RecoveryInventoryScope &live,
                              const RRRangeState &s,const RROpeningPair &opening) {
 if(h.state!=RFH_ARMED)return false;
 if(!RecoveryAlternatingSameSession(h.binding.exact.binding,live) ||
    h.prior_order!=s.cycle.active_order || h.prior_position!=s.cycle.position_id ||
    h.prior_ticket!=s.position_ticket || h.range_id!=s.cycle.range_id ||
    h.binding.scope!=s.scope || (ulong)live.generation!=s.cycle.generation) {
    h.state=RFH_DISARMED;
    return false;
 }
 if(!RecoveryFillHintContext(h,live))return false;
 RecoveryExactOrderScope request;
 request.login=live.login;request.server=live.server;request.symbol=live.symbol;
 request.magic=(ulong)live.magic;request.ticket=h.binding.exact.order_ticket;
 RecoveryExactOrderObservation observed;
 if(RecoveryObserveExactOrder(request,observed)!=RECOVERY_EXACT_ORDER_OBSERVED ||
    observed.source!=RECOVERY_EXACT_ORDER_HISTORY || observed.ticket!=request.ticket ||
    observed.magic!=request.magic || observed.symbol!=request.symbol ||
    observed.position_identifier==0 || observed.position_identifier==h.prior_position ||
    observed.state!=ORDER_STATE_FILLED || observed.remaining_volume!=0.0 ||
    observed.type!=(h.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
    !RecoveryFillHintContext(h,live))return false;
 RecoveryAlternatingEvidenceBinding check=h.binding;
 check.history.position_identifier=observed.position_identifier;
 RRKnownOrder wanted;
 if(!RecoveryAlternatingWanted(s,opening,check,wanted) || wanted.ticket!=request.ticket ||
    wanted.side!=h.side)return false;
 // Do not pass the local position identifier or prices into the pump.
 // Its own exact/history/inventory reports must determine those independently.
 h.state=RFH_QUEUED;
 return true;
}
#endif
