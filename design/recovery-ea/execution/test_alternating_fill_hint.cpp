#include <cassert>
#include <cstdint>
#include <string>
using string=std::string;
using ulong=std::uint64_t;
struct RecoveryInventoryScope {string symbol,boot;long login;};
struct RecoveryAlternatingEvidenceBinding {
 struct {RecoveryInventoryScope binding;ulong order_ticket;} exact;
};
struct RRRangeState {struct {ulong active_order,position_id;int range_id;} cycle;ulong position_ticket;};
struct RROpeningPair {bool valid;int side;};
enum {RAP_UNUSED,RAP_READY};
struct RecoveryAlternatingPump {
 int state;
 ulong prior_order,prior_position,prior_ticket;
 int range_id;
 struct {int side;} wanted;
};
int begins=0;
bool connected=true;
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b){
 return a.symbol==b.symbol && a.boot==b.boot && a.login==b.login;
}
bool RecoveryAlternatingPumpSession(const RecoveryInventoryScope &){return connected;}
bool RecoveryAlternatingPumpBegin(RecoveryAlternatingPump &p,const RRRangeState &s,
    const RROpeningPair &o,const RecoveryAlternatingEvidenceBinding &b,const string &tag){
 ++begins;
 if(!o.valid || !b.exact.order_ticket || tag.empty())return false;
 p={RAP_READY,s.cycle.active_order,s.cycle.position_id,s.position_ticket,s.cycle.range_id,{o.side}};
 return true;
}
enum {TRADE_TRANSACTION_HISTORY_ADD,TRADE_TRANSACTION_HISTORY_UPDATE,TRADE_TRANSACTION_DEAL_ADD,
      TRADE_TRANSACTION_ORDER_DELETE,TRADE_TRANSACTION_REQUEST,ORDER_STATE_FILLED,ORDER_STATE_PARTIAL,
      ORDER_STATE_CANCELED,ORDER_TYPE_BUY_STOP,ORDER_TYPE_SELL_STOP};
struct MqlTradeTransaction {int type;ulong order;string symbol;int order_state,order_type;};

#include "FillHintUnderTest"

int main(){
 const ulong ticket=9007199254740997ULL;
 const RecoveryInventoryScope live{"XAUUSD.s","boot#1",7}, old{"XAUUSD.s","boot#0",7};
 const RecoveryAlternatingEvidenceBinding b{{live,ticket}};
 RRRangeState s{{12,13,1},14};RROpeningPair opening{true,-1};
 RecoveryFillHint h{};RecoveryAlternatingPump p{};
 const MqlTradeTransaction actual{TRADE_TRANSACTION_HISTORY_ADD,ticket,"XAUUSD.s",
                                  ORDER_STATE_FILLED,ORDER_TYPE_SELL_STOP};
 auto t=actual;
 assert(!RecoveryQueueFillHint(actual,live,h));
 assert(RecoveryArmFillHint(h,s,opening,b,"nonce-tag") && begins==1);
 assert(!RecoveryArmFillHint(h,s,opening,b,"nonce-tag"));
 t.order++;assert(!RecoveryQueueFillHint(t,live,h));t=actual;
 t.symbol="EURUSD";assert(!RecoveryQueueFillHint(t,live,h));t=actual;
 t.order_state=ORDER_STATE_PARTIAL;assert(!RecoveryQueueFillHint(t,live,h));
 t.order_state=ORDER_STATE_CANCELED;assert(!RecoveryQueueFillHint(t,live,h));t=actual;
 t.order_type=ORDER_TYPE_BUY_STOP;assert(!RecoveryQueueFillHint(t,live,h));t=actual;
 t.type=TRADE_TRANSACTION_DEAL_ADD;assert(!RecoveryQueueFillHint(t,live,h));
 t.type=TRADE_TRANSACTION_ORDER_DELETE;assert(!RecoveryQueueFillHint(t,live,h));
 t.type=TRADE_TRANSACTION_REQUEST;assert(!RecoveryQueueFillHint(t,live,h));
 assert(!RecoveryQueueFillHint(actual,old,h));connected=false;
 assert(!RecoveryQueueFillHint(actual,live,h));connected=true;
 assert(RecoveryQueueFillHint(actual,live,h) && begins==1);
 assert(!RecoveryQueueFillHint(actual,live,h) && begins==1);
 p.state=RAP_READY;
 assert(!RecoveryStartHintPump(h,live,s,opening,p) && h.state==RFH_QUEUED);
 p.state=RAP_UNUSED;
 assert(RecoveryStartHintPump(h,live,s,opening,p) && begins==2 && p.state==RAP_READY);
 assert(!RecoveryStartHintPump(h,live,s,opening,p) && !RecoveryQueueFillHint(actual,live,h));

 h={};p={};assert(RecoveryArmFillHint(h,s,opening,b,"nonce-tag"));
 t=actual;t.type=TRADE_TRANSACTION_HISTORY_UPDATE;
 assert(RecoveryQueueFillHint(t,live,h));
 ++s.position_ticket;
 assert(!RecoveryStartHintPump(h,live,s,opening,p) && h.state==RFH_DISARMED && begins==3);
 --s.position_ticket;
 assert(!RecoveryStartHintPump(h,live,s,opening,p));

 h={};p={};assert(RecoveryArmFillHint(h,s,opening,b,"nonce-tag"));
 assert(RecoveryQueueFillHint(actual,live,h));
 assert(!RecoveryStartHintPump(h,old,s,opening,p) && h.state==RFH_DISARMED && begins==4);

 h={};p={};assert(RecoveryArmFillHint(h,s,opening,b,"nonce-tag"));
 assert(RecoveryQueueFillHint(actual,live,h));opening.valid=false;
 assert(!RecoveryStartHintPump(h,live,s,opening,p) && h.state==RFH_USED && begins==6);
 assert(!RecoveryStartHintPump(h,live,s,opening,p) && begins==6);
}
