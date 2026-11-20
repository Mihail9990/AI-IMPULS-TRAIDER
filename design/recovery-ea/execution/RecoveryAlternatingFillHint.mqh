#ifndef RECOVERY_ALTERNATING_FILL_HINT_MQH
#define RECOVERY_ALTERNATING_FILL_HINT_MQH
#include "RecoveryAlternatingPump.mqh"

enum RecoveryFillHintState { RFH_UNUSED=0,RFH_ARMED,RFH_QUEUED,RFH_USED,RFH_DISARMED };
struct RecoveryFillHint {
 RecoveryFillHintState state;
 RecoveryAlternatingEvidenceBinding binding;
 string publisher_tag;
 ulong prior_order,prior_position,prior_ticket;
 int range_id,side;
};

// Called by the owner only after the exact opposite ticket is associated
// with this session/range. No broker or file operation occurs at arm time.
bool RecoveryArmFillHint(RecoveryFillHint &h,const RRRangeState &s,const RROpeningPair &opening,
    const RecoveryAlternatingEvidenceBinding &binding,const string publisher_tag) {
 if(h.state!=RFH_UNUSED)return false;
 RecoveryAlternatingPump validation;
 validation.state=RAP_UNUSED;
 if(!RecoveryAlternatingPumpBegin(validation,s,opening,binding,publisher_tag))return false;
 h.binding=binding;h.publisher_tag=publisher_tag;
 h.prior_order=validation.prior_order;h.prior_position=validation.prior_position;
 h.prior_ticket=validation.prior_ticket;h.range_id=validation.range_id;
 h.side=validation.wanted.side;h.state=RFH_ARMED;
 return true;
}

bool RecoveryFillHintContext(const RecoveryFillHint &h,const RecoveryInventoryScope &live) {
 // live comes from the current boot owner, never from the callback or hint.
 return RecoveryAlternatingSameSession(h.binding.exact.binding,live) &&
        RecoveryAlternatingPumpSession(live);
}

// OnTradeTransaction may call this once per armed range. It only records an
// advisory candidate; no history access, probe, state advance or OrderSend.
bool RecoveryQueueFillHint(const MqlTradeTransaction &t,const RecoveryInventoryScope &live,
                           RecoveryFillHint &h) {
 if(h.state!=RFH_ARMED ||
    (t.type!=TRADE_TRANSACTION_HISTORY_ADD && t.type!=TRADE_TRANSACTION_HISTORY_UPDATE) ||
    t.order!=h.binding.exact.order_ticket || t.symbol!=h.binding.exact.binding.symbol ||
    t.order_state!=ORDER_STATE_FILLED ||
    t.order_type!=(h.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
    !RecoveryFillHintContext(h,live)) return false;
 h.state=RFH_QUEUED;
 return true;
}

// Called later from the main timer. Starting a pump does not make a claim
// about execution; only its three correlated verifier reports can do so.
bool RecoveryStartHintPump(RecoveryFillHint &h,const RecoveryInventoryScope &live,
    const RRRangeState &s,const RROpeningPair &opening,RecoveryAlternatingPump &pump) {
 if(h.state!=RFH_QUEUED || pump.state!=RAP_UNUSED)return false;
 if(!RecoveryFillHintContext(h,live) || h.prior_order!=s.cycle.active_order ||
    h.prior_position!=s.cycle.position_id || h.prior_ticket!=s.position_ticket ||
    h.range_id!=s.cycle.range_id) { h.state=RFH_DISARMED;return false; }
 h.state=RFH_USED; // Consume before begin: failure cannot admit a duplicate.
 return RecoveryAlternatingPumpBegin(pump,s,opening,h.binding,h.publisher_tag);
}
#endif
