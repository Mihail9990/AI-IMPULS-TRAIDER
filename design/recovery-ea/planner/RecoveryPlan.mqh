#ifndef RECOVERY_PLAN_MQH
#define RECOVERY_PLAN_MQH
// Pure planning only. No terminal reads, files, network, or trading operations.
// Unit counts validate representability; broker SL/TP are never round-tripped.
enum RSide { R_BUY=1, R_SELL=-1 };
enum RPhase { R_IDLE=0, R_RESERVED=1, R_UNKNOWN=2, R_REJECTED=3, R_CONFIRMED=4 };
struct RScope { long login; string server; string symbol; };
struct RPosition {
 RScope scope; ulong ticket; ulong identifier; long magic; int side;
 double entry; double volume; double sl; double tp;
 bool broker_confirmed; bool adopted;
};
struct RMarket {
 double tick; double volume_step; double min_volume; double max_volume;
 double bid; double ask; double stop_distance; double freeze_distance;
 bool hedging; bool snapshot_complete;
};
struct RPlan {
 RScope scope;
 ulong ticket; ulong identifier; int reverse_side;
 double original_sl; double original_tp; double expected_volume; double expected_entry;
 double required_sl; double reverse_price; double reverse_sl; double reverse_volume;
 bool needs_sl; bool valid;
};
struct RPending {
 RScope scope; ulong ticket; int side; double price; double volume; double sl; double tp;
 bool broker_confirmed; bool owned; bool is_plain_stop;
};
bool RFinitePositive(const double n) { return MathIsValidNumber(n) && n>0; }
bool RScopeEqual(const RScope &a,const RScope &b) {
 return a.login>0 && a.login==b.login && a.server!="" && a.server==b.server && a.symbol!="" && a.symbol==b.symbol;
}
// Tiny tolerance only for floating point division noise, NOT half-tick rounding.
// Caller must supply actual SYMBOL_TRADE_TICK_SIZE and SYMBOL_VOLUME_STEP.
bool RGrid(const double value,const double step,long &units) {
 units=0;
 if(!RFinitePositive(value)||!RFinitePositive(step))return false;
 double q=value/step;
 if(!MathIsValidNumber(q)||q>1000000000000.0)return false;
 double rounded=MathRound(q);
 if(MathAbs(q-rounded)>0.000001)return false;
 units=(long)rounded; return units>0;
}
bool RVolume(const double value,const RMarket &m) {
 long units;
 return RFinitePositive(m.min_volume)&&RFinitePositive(m.max_volume)&&m.min_volume<=m.max_volume &&
 value>=m.min_volume && value<=m.max_volume && RGrid(value,m.volume_step,units);
}
bool RMarketOK(const RMarket &m) {
 return m.snapshot_complete && m.hedging && RFinitePositive(m.tick) &&
 RFinitePositive(m.bid)&&RFinitePositive(m.ask)&&m.bid<=m.ask &&
 MathIsValidNumber(m.stop_distance)&&m.stop_distance>=0 &&
 MathIsValidNumber(m.freeze_distance)&&m.freeze_distance>=0;
}
bool RPositionOK(const RPosition &p,const RScope &scope,const RMarket &m) {
 long units;
 return RScopeEqual(p.scope,scope)&&p.ticket>0&&p.identifier>0&&p.broker_confirmed &&
 (p.side==R_BUY||p.side==R_SELL)&&RFinitePositive(p.entry)&&RVolume(p.volume,m)&&
 MathIsValidNumber(p.sl)&&p.sl>=0&&MathIsValidNumber(p.tp)&&p.tp>=0 &&
 (p.sl==0||RGrid(p.sl,m.tick,units))&&(p.tp==0||RGrid(p.tp,m.tick,units));
}
// Never silently adopt the first matching position. Require a unique explicitly
// selected manual candidate and exact identifier, not merely symbol or comment.
bool RManualCandidate(const RPosition &p,const RScope &scope,const RMarket &m,
 const ulong selected_ticket,const ulong selected_identifier,const int matching_candidates) {
 return matching_candidates==1 && selected_ticket>0 && selected_identifier>0 &&
 p.ticket==selected_ticket&&p.identifier==selected_identifier&&!p.adopted &&
 RMarketOK(m)&&RPositionOK(p,scope,m);
}
bool RTriggerGeometry(const int side,const double price,const RMarket &m) {
 long units;
 if(!RMarketOK(m)||!RGrid(price,m.tick,units))return false;
 double gap=side==R_BUY?price-m.ask:side==R_SELL?m.bid-price:-1;
 // Strict positive excludes marketable stop orders. Do not shift rejected price.
 return gap>0&&gap>=m.stop_distance&&gap>m.freeze_distance;
}
bool RMakeManualPlan(const RPosition &p,const RScope &scope,const RMarket &m,
 const ulong ticket,const ulong identifier,const int candidates,
 const double configured_distance,const double reverse_volume,RPlan &out) {
 out.valid=false;
 if(!RManualCandidate(p,scope,m,ticket,identifier,candidates)||!RVolume(reverse_volume,m))return false;
 double sl=p.sl;
 if(!RFinitePositive(configured_distance)||configured_distance<0.60)return false;
 if(sl==0)sl=p.entry-p.side*configured_distance;
 double reverse_sl=sl+p.side*configured_distance;
 long sl_units;
 if(!RGrid(reverse_sl,m.tick,sl_units))return false;
 // Existing SL may already be trailed beyond entry. Do not apply initial-risk
 // direction rule to it; market geometry still must allow exact reverse price.
 if(!RTriggerGeometry(-p.side,sl,m))return false;
 out.scope=p.scope;out.ticket=p.ticket;out.identifier=p.identifier;out.reverse_side=-p.side;
 out.original_sl=p.sl;out.original_tp=p.tp;out.expected_volume=p.volume;out.expected_entry=p.entry;
 out.required_sl=sl;out.reverse_price=sl;out.reverse_sl=reverse_sl;out.reverse_volume=reverse_volume;
 out.needs_sl=p.sl==0;out.valid=true;return true;
}
// Re-read exact selected position immediately before any future dispatch.
// This is not an atomic snapshot or durable authorization; ports must provide both.
bool RPlanStillMatches(const RPlan &plan,const RPosition &p,const RScope &scope,const RMarket &m) {
 return plan.valid&&RScopeEqual(plan.scope,scope)&&RPositionOK(p,scope,m)&&p.ticket==plan.ticket&&p.identifier==plan.identifier &&
 p.entry==plan.expected_entry&&p.volume==plan.expected_volume&&p.tp==plan.original_tp&&
 p.side==-plan.reverse_side&&p.sl==plan.original_sl&&RTriggerGeometry(plan.reverse_side,plan.reverse_price,m)&&
 plan.reverse_price==plan.required_sl&&RVolume(plan.reverse_volume,m);
}
// Proof of protection after the two requests; the submit schedule is not
// set by this helper. In missing-SL mode do not mark the pair active until both are proven.
bool RProtectionConfirmed(const RPlan &plan,const RPosition &p,const RScope &scope,const RMarket &m) {
 return plan.valid&&RScopeEqual(plan.scope,scope)&&RPositionOK(p,scope,m)&&p.ticket==plan.ticket&&p.identifier==plan.identifier&&
 p.side==-plan.reverse_side&&p.entry==plan.expected_entry&&p.volume==plan.expected_volume&&
 p.sl==plan.required_sl&&p.tp==plan.original_tp&&RTriggerGeometry(plan.reverse_side,plan.reverse_price,m);
}
bool RReverseConfirmed(const RPlan &plan,const RPending &order,const RScope &scope,const RMarket &m) {
 return plan.valid&&RScopeEqual(plan.scope,scope)&&order.is_plain_stop&&order.broker_confirmed&&order.owned&&order.ticket>0&&RScopeEqual(order.scope,scope)&&
 order.side==plan.reverse_side&&order.price==plan.reverse_price&&order.volume==plan.reverse_volume&&
 order.sl==plan.reverse_sl&&order.tp==0&&RVolume(order.volume,m);
}
// A terminal rejection or uncertain result is never permission to retry. Operator
// reconciliation/new intent is outside this pure draft. No cancel-replace helper.
bool RCanReserve(const int phase,const bool durable_unique_intent,const bool exclusive_writer) {
 return phase==R_IDLE&&durable_unique_intent&&exclusive_writer;
}
int RAfterAttempt(const bool transport_known,const bool broker_rejected,const bool proof_confirmed) {
 if(!transport_known)return R_UNKNOWN;
 if(broker_rejected&&proof_confirmed)return R_UNKNOWN;
 if(broker_rejected)return R_REJECTED;
 if(proof_confirmed)return R_CONFIRMED;
 return R_UNKNOWN;
}
#endif
