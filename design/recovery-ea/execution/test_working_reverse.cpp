#include <cassert>
#include <climits>
#include <cstdint>
#include <string>
using string=std::string;
using ulong=std::uint64_t;
enum {RR_NONE,RR_REVERSE,RR_INITIAL_SL,RR_ARM_REVERSE,RR_ACTIVE};
enum {RI_NO_RECORD,RI_TICKET};
enum {RR_PAIR_CONFLICT,RR_PAIR_WAITING,RR_PAIR_CONFIRMED};
struct RRAction {int kind;long serial;};
struct RRRangeState {
 struct {ulong position_id,active_order;} cycle;
 ulong position_ticket,scope,opposite;
 int phase;
 RRAction pending;
};
struct RROpeningPair {bool sl_required,reserved;RRAction sl,reverse;};
struct RecoverySessionContext {long chart,login,magic;string server,symbol;};
struct RecoverySessionLifecycle {string epoch;bool active;};
struct RecoverySessionIntentScope {string executor_epoch;long login,magic;string server,symbol;ulong scope,generation;};
struct RecoveryInventoryScope {string nonce,executor_epoch;long verifier_chart,login,magic,generation;string server,symbol;};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
struct RecoveryExactEffectBinding {RecoveryExactReportScope request;RRAction action;ulong verify_after;double price_tick,volume_step;};
struct RecoveryExactReport {long sequence;bool placed,match_fields;};
struct RREffect {RRAction action;bool verified;ulong order;};
struct RecoveryIntentKey {string id,fingerprint;};
static constexpr ulong ticket=9007199254740997ULL;
bool available=true,can_publish=true,verifier_ready=false,fail_reducer=false;
int lookups=0,published=0,applied=0;
ulong journal=ticket;
RecoveryExactReport exact{11,true,true};
bool RecoveryInvHex(const string &s){return s.size()==32 && s.find_first_not_of("abcdef0123456789")==string::npos;}
bool RecoveryInvScopeOK(const RecoveryInventoryScope &q){return RecoveryInvHex(q.nonce)&&q.verifier_chart>0;}
bool RecoverySessionCurrent(const RecoverySessionLifecycle &s,const RecoverySessionContext &c){return available&&s.active&&c.chart>0;}
bool RRActionEqual(const RRAction &a,const RRAction &b){return a.kind==b.kind&&a.serial==b.serial;}
bool RROpeningRequestState(const RRRangeState &r,const RROpeningPair &p,const RRAction &a,RRRangeState &v){
 if(!p.reserved || !RRActionEqual(p.reverse,a) || r.phase!=RR_ARM_REVERSE)return false;
 v=r;v.pending=a;return true;
}
bool RecoveryExactEffectBound(const RRRangeState &r,const RecoveryExactEffectBinding &b){
 return RRActionEqual(r.pending,b.action)&&b.price_tick>0&&b.volume_step>0;
}
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope &s,const RRAction &a,double,double,
                                   RecoveryIntentKey &key){
 key={s.executor_epoch,std::to_string(a.serial)};return true;
}
bool RecoveryIntentKeysMatch(const RecoveryIntentKey &a,const RecoveryIntentKey &b){
 return a.id==b.id&&a.fingerprint==b.fingerprint;
}
int RecoveryIntentLookup(const RecoveryIntentKey &,string &t){
 ++lookups;t=(journal ? std::to_string(journal) : "");
 return journal?RI_TICKET:RI_NO_RECORD;
}
bool RecoveryExactReportRequest(const RecoveryExactReportScope &r,const string &p){
 assert(r.order_ticket==ticket && p==string(32,'3'));
 ++published;return can_publish;
}
bool RecoveryExactReportRead(const RecoveryExactReportScope &,RecoveryExactReport &out){
 if(!verifier_ready)return false;
 out=exact;return true;
}
bool RecoveryExactEffectFold(const RecoveryExactReport &r,const RecoveryExactEffectBinding &b,RREffect &e){
 if(!r.placed || !r.match_fields || r.sequence<=0 || (ulong)r.sequence<=b.verify_after)return false;
 e={b.action,true,b.request.order_ticket};
 return true;
}
int RRConfirmOpeningPair(RRRangeState &r,RROpeningPair &p,const RREffect &e){
 ++applied;
 if(fail_reducer){r.phase=RR_ACTIVE;return RR_PAIR_CONFLICT;}
 if(!e.verified || e.order!=ticket || !RRActionEqual(e.action,p.reverse))return RR_PAIR_CONFLICT;
 r.phase=RR_ACTIVE;r.opposite=e.order;p.reserved=false;
 return RR_PAIR_CONFIRMED;
}
#include "WorkingReverseUnderTest"
int main(){
 const string epoch(32,'1'),nonce(32,'2'),tag(32,'3');
 RecoverySessionContext live{1,5,700,"Test","XAUUSD.s"};
 RecoverySessionLifecycle session{epoch,true};
 RecoverySessionIntentScope scope{epoch,5,700,"Test","XAUUSD.s",19,7};
 RecoveryExactReportScope request{{nonce,epoch,24,5,700,7,"Test","XAUUSD.s"},ticket};
 RecoveryIntentKey key{epoch,"88"};
 const RRRangeState initial{{13,11},14,19,0,RR_ARM_REVERSE,{RR_REVERSE,88}};
 const RROpeningPair reserved{false,true,{RR_NONE,0},{RR_REVERSE,88}};
 auto r=initial;auto p=reserved;
 RecoveryWorkingReverse probe{};
 auto start=[&](){return RecoveryBeginWorkingReverse(probe,session,live,scope,r,p,key,request,10,tag,.01,.01);};
 auto step=[&](){return RecoveryStepWorkingReverse(probe,session,live,scope,r,p);};
 key.fingerprint="wrong";assert(!start());key.fingerprint="88";
 journal=0;assert(!start() && !published);journal=ticket+1;assert(!start());journal=ticket;
 p.sl_required=true;p.sl.kind=RR_INITIAL_SL;assert(!start());p=reserved;
 request.binding.executor_epoch=string(32,'b');assert(!start());request.binding.executor_epoch=epoch;
 assert(start() && !start() && r.phase==RR_ARM_REVERSE);
 assert(step()==RWR_PUBLISHED && published==1);
 for(int i=0;i<3;i++)assert(step()==RWR_WAIT && published==1 && !applied);
 verifier_ready=true;
 assert(step()==RWR_ACTIVE && applied==1 && r.phase==RR_ACTIVE && r.opposite==ticket && !p.reserved);
 assert(step()==RWR_STOPPED && applied==1 && published==1);
 auto reset=[&](){r=initial;p=reserved;probe={};assert(start() && step()==RWR_PUBLISHED);};
 reset();exact.placed=false;
 assert(step()==RWR_STOPPED && applied==1 && r.phase==RR_ARM_REVERSE);exact.placed=true;
 reset();exact.sequence=10;
 assert(step()==RWR_STOPPED && applied==1 && p.reserved);exact.sequence=11;
 reset();r.position_ticket++;
 assert(step()==RWR_STOPPED && applied==1);
 reset();journal=0;
 assert(step()==RWR_STOPPED && applied==1);journal=ticket;
 reset();fail_reducer=true;
 assert(step()==RWR_STOPPED && applied==2 && r.phase==RR_ARM_REVERSE && p.reserved);fail_reducer=false;
 probe={};assert(start());can_publish=false;
 assert(step()==RWR_STOPPED && r.phase==RR_ARM_REVERSE);
 const int count=published;assert(step()==RWR_STOPPED && published==count);
}
