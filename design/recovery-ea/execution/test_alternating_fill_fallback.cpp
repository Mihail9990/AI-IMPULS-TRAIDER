#include <cassert>
#include <string>
#include <cstdint>
using string=std::string;
using ulong=std::uint64_t;
struct RecoveryInventoryScope {long login,generation,magic;string server,symbol,epoch;};
struct RecoveryExactOrderScope {long login;string server,symbol;ulong magic,ticket;};
struct RecoveryExactOrderObservation {int source;ulong ticket,magic,position_identifier;string symbol;int state;double remaining_volume;int type;};
struct RecoveryAlternatingEvidenceBinding {
 struct {RecoveryInventoryScope binding;ulong order_ticket;} exact;
 struct {ulong position_identifier;} history;
 ulong scope;
};
enum {RFH_UNUSED,RFH_ARMED,RFH_QUEUED,RFH_USED,RFH_DISARMED};
enum {RECOVERY_EXACT_ORDER_OBSERVED,RECOVERY_EXACT_ORDER_UNKNOWN,RECOVERY_EXACT_ORDER_HISTORY,
      RECOVERY_EXACT_ORDER_CURRENT,ORDER_STATE_FILLED,ORDER_STATE_PARTIAL,ORDER_STATE_CANCELED,
      ORDER_TYPE_BUY_STOP,ORDER_TYPE_SELL_STOP};
struct RecoveryFillHint {int state;RecoveryAlternatingEvidenceBinding binding;ulong prior_order,prior_position,prior_ticket;int range_id,side;};
struct RRRangeState {struct {ulong active_order,position_id,generation;int range_id;} cycle;ulong position_ticket,scope;};
struct RROpeningPair {bool usable;int side;};
struct RRKnownOrder {ulong ticket;int side;};
static constexpr ulong ticket=9007199254740997ULL;
static constexpr ulong successor=9007199254740999ULL;
static const RecoveryInventoryScope live{5,7,700,"Test-MT5","XAUUSD.s","boot1"};
static const RecoveryExactOrderObservation filled{RECOVERY_EXACT_ORDER_HISTORY,ticket,700,successor,
                                                 "XAUUSD.s",ORDER_STATE_FILLED,0.0,ORDER_TYPE_SELL_STOP};
bool connected=true,lose_during_read=false;
int probes=0,lookup_result=RECOVERY_EXACT_ORDER_OBSERVED;
RecoveryExactOrderObservation next=filled;
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b) {
 return a.login==b.login && a.generation==b.generation && a.magic==b.magic &&
        a.server==b.server && a.symbol==b.symbol && a.epoch==b.epoch;
}
bool RecoveryFillHintContext(const RecoveryFillHint &h,const RecoveryInventoryScope &b) {
 return connected && RecoveryAlternatingSameSession(h.binding.exact.binding,b);
}
int RecoveryObserveExactOrder(const RecoveryExactOrderScope &r,RecoveryExactOrderObservation &o) {
 assert(r.login==live.login && r.server==live.server && r.symbol==live.symbol &&
        r.magic==static_cast<ulong>(live.magic) && r.ticket==ticket);
 ++probes;o=next;if(lose_during_read)connected=false;return lookup_result;
}
bool RecoveryAlternatingWanted(const RRRangeState &,const RROpeningPair &p,
    const RecoveryAlternatingEvidenceBinding &b,RRKnownOrder &o) {
 assert(b.history.position_identifier==successor);
 o={b.exact.order_ticket,p.side};
 return p.usable;
}
#include "FallbackUnderTest"
int main(){
 const RecoveryFillHint armed{RFH_ARMED,{{live,ticket},{0},19},12,13,14,1,-1};
 const RRRangeState range{{12,13,7,1},14,19};
 const RROpeningPair pair{true,-1};
 RecoveryFillHint h=armed;
 assert(RecoveryPollExactFillHint(h,live,range,pair) && h.state==RFH_QUEUED && probes==1);
 assert(h.binding.history.position_identifier==0);
 assert(!RecoveryPollExactFillHint(h,live,range,pair) && probes==1);
 h.state=RFH_USED;
 assert(!RecoveryPollExactFillHint(h,live,range,pair) && probes==1);
 h=armed;
 auto expect_rejected=[&](){
  assert(!RecoveryPollExactFillHint(h,live,range,pair) && h.state==RFH_ARMED);
  next=filled;
 };
 lookup_result=RECOVERY_EXACT_ORDER_UNKNOWN;expect_rejected();
 lookup_result=RECOVERY_EXACT_ORDER_OBSERVED;
 next.source=RECOVERY_EXACT_ORDER_CURRENT;expect_rejected();
 next.state=ORDER_STATE_CANCELED;expect_rejected();
 next.state=ORDER_STATE_PARTIAL;expect_rejected();
 next.remaining_volume=.01;expect_rejected();
 next.ticket++;expect_rejected();
 next.magic++;expect_rejected();
 next.symbol="EURUSD";expect_rejected();
 next.position_identifier=0;expect_rejected();
 next.position_identifier=h.prior_position;expect_rejected();
 next.type=ORDER_TYPE_BUY_STOP;expect_rejected();
 lose_during_read=true;expect_rejected();
 int before=probes;
 assert(!RecoveryPollExactFillHint(h,live,range,pair) && probes==before);
 connected=true;lose_during_read=false;
 assert(!RecoveryPollExactFillHint(h,live,range,{false,-1}) && h.state==RFH_ARMED);
 before=probes;
 auto old=live;old.epoch="boot0";
 assert(!RecoveryPollExactFillHint(h,old,range,pair) && h.state==RFH_DISARMED && probes==before);
 h=armed;auto changed=range;changed.position_ticket++;
 assert(!RecoveryPollExactFillHint(h,live,changed,pair) && h.state==RFH_DISARMED && probes==before);
 h=armed;changed=range;changed.scope++;
 assert(!RecoveryPollExactFillHint(h,live,changed,pair) && h.state==RFH_DISARMED && probes==before);
 h=armed;
 assert(RecoveryPollExactFillHint(h,live,range,pair) && h.state==RFH_QUEUED);
}
