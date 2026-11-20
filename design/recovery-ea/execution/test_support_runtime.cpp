#include <cassert>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <string>
using string=std::string;
using ulong=std::uint64_t;
template<class T> void ZeroMemory(T &x){x=T{};}
bool MathIsValidNumber(double q){return std::isfinite(q);}
string DoubleToString(double q,int d){char s[80];assert(d==16);std::snprintf(s,sizeof s,"%.16f",q);return s;}
bool RCValidSide(int side){return side==1||side==-1;}
bool RCBoundedPositive(long v){return v>0&&v<=1000000000000L;}
enum { RR_NONE,RR_ACTIVE,RR_SUPPORT_SL,RD_OUTCOME_UNRESOLVED,RD_NOT_SENT,
       TERMINAL_CONNECTED,ACCOUNT_LOGIN,ACCOUNT_SERVER,POSITION_IDENTIFIER,POSITION_TICKET,
       POSITION_TYPE,POSITION_MAGIC,POSITION_SYMBOL,POSITION_PRICE_OPEN,POSITION_VOLUME,
       POSITION_SL,POSITION_TP,POSITION_TYPE_BUY,POSITION_TYPE_SELL };
struct RRAction {int kind,stage;long sl_ticks;};
struct RRRangeState {
 ulong scope,position_ticket,opposite;
 struct {ulong generation,active_order,position_id;int range_id,side;long actual_entry_ticks,filled_steps;} cycle;
 RRAction pending;int phase,support_stage;long live_sl,live_tp;
};
struct RRSettings {ulong scope,generation;long support_first_ticks,support_second_ticks,support_offset_ticks;};
struct RecoverySessionContext {long chart,login,magic;string server,symbol;};
struct RecoverySessionLifecycle {string epoch;bool active;};
struct RecoverySessionIntentScope {string executor_epoch;long login,magic;string server,symbol;ulong scope,generation;};
struct RecoveryInventoryScope {string nonce,executor_epoch,verifier_epoch;long generation,login,verifier_chart;string server,symbol;long magic;};
struct RecoveryEvidenceOwner {RecoverySessionContext context;string executor_epoch,verifier_epoch;long verifier_chart;ulong serial;};
struct RecoveryPriceScope {
 string nonce,executor_epoch,watcher_epoch;ulong generation;long login,watcher_chart;
 string server,symbol;ulong position_identifier,live_ticket;int side;string actual_entry,first_advance,second_advance;
};
struct RecoverySingleSupport {int state;RecoveryPriceScope request;RRAction action;};
struct RecoveryPositionEffectBinding {
 RecoveryInventoryScope request;RRAction action;ulong verify_after,prior_position_magic;
 string prior_position_entry;double price_tick,volume_step;
};
struct RecoveryDispatchResult {int state;};
struct RecoveryInventorySnapshot {bool ok;};
const string _Symbol="XAUUSD.s";
bool connected=true,has_pos=true,price_reached=false,follow_ok=true,submit_ok=true,inv_request_ok=true,inv_ready=false,inv_proof=true;
int follows=0,sends=0,queries=0,confirms=0,drops=0;
long local_id=15,local_ticket=14,local_magic=700,local_login=5;
double local_sl=3999.20;
bool RecoveryInvHex(const string &s){return s.size()==32 && s.find_first_not_of("abcdef0123456789")==string::npos;}
bool RecoveryInvScopeOK(const RecoveryInventoryScope &v){return RecoveryInvHex(v.nonce)&&v.verifier_chart>0;}
bool RecoverySessionScopeEqual(const RecoverySessionContext &a,const RecoverySessionContext &b){
 return a.chart==b.chart && a.login==b.login && a.magic==b.magic && a.server==b.server && a.symbol==b.symbol;
}
bool RecoverySessionCurrent(const RecoverySessionLifecycle &l,const RecoverySessionContext &){return l.active;}
bool RRValid(const RRRangeState &r,const RRSettings &c){return r.scope==c.scope&&r.cycle.generation==c.generation;}
long TerminalInfoInteger(int property){assert(property==TERMINAL_CONNECTED);return connected;}
long AccountInfoInteger(int property){assert(property==ACCOUNT_LOGIN);return local_login;}
string AccountInfoString(int property){assert(property==ACCOUNT_SERVER);return "Test-MT5";}
bool PositionSelectByTicket(ulong ticket){return has_pos&&ticket==(ulong)local_ticket;}
bool PositionGetInteger(int property,long &out){
 switch(property){
 case POSITION_IDENTIFIER:out=local_id;return true;
 case POSITION_TICKET:out=local_ticket;return true;
 case POSITION_TYPE:out=POSITION_TYPE_BUY;return true;
 case POSITION_MAGIC:out=local_magic;return true;
 }
 return false;
}
bool PositionGetString(int property,string &out){assert(property==POSITION_SYMBOL);out=_Symbol;return true;}
bool PositionGetDouble(int property,double &out){
 switch(property){
 case POSITION_PRICE_OPEN:out=4000;return true;
 case POSITION_VOLUME:out=.01;return true;
 case POSITION_SL:out=local_sl;return true;
 case POSITION_TP:out=0;return true;
 }
 return false;
}
bool RecoveryDispatchUnits(double d,double step,bool allowzero,long expected){
 return (d!=0 || allowzero)&&std::lround(d/step)==expected;
}
string RecoveryOwnerNonce(const string &lane,const string &identity){
 assert(!identity.empty());
 if(lane=="support-price")return string(32,'1');
 if(lane=="support-inventory")return string(32,'2');
 assert(lane=="support-publisher");return string(32,'3');
}
bool RecoveryPriceScopeOK(const RecoveryPriceScope &w){return RecoveryInvHex(w.nonce);}
bool RecoveryPriceDrop(const RecoveryPriceScope &w,int stage,const string &tag){
 assert(w.nonce==string(32,'1')&&stage==2&&tag==string(32,'3'));++drops;return true;
}
bool RecoveryStartSingleSupport(RecoverySingleSupport &t,const RecoverySessionLifecycle &,
    const RecoverySessionContext &,const RRRangeState &r,const RRSettings &c,const RecoveryPriceScope &w,
    const string &tag,double tick){
 assert(w.position_identifier==r.cycle.position_id && w.live_ticket==r.position_ticket &&
        w.first_advance=="0.0000000000000000"&&w.second_advance==DoubleToString(c.support_second_ticks*tick,16)&&
        w.actual_entry==DoubleToString(4000,16)&&tag==string(32,'3'));
 ++follows;t.state=1;t.request=w;return follow_ok;
}
bool RecoverySubmitSingleSupport(RecoverySingleSupport &t,const RecoverySessionLifecycle &,
    const RecoverySessionContext &,const RecoverySessionIntentScope &,RRRangeState &r,
    const RRSettings &,double,double,RecoveryDispatchResult &out){
 if(!price_reached)return false;
 assert(r.opposite==50 && !r.pending.kind && t.state==1);
 ++sends;t.state=2;t.action={RR_SUPPORT_SL,2,r.cycle.actual_entry_ticks};r.pending=t.action;
 out.state=submit_ok?RD_OUTCOME_UNRESOLVED:RD_NOT_SENT;
 return true;
}
bool RecoveryPositionEffectBound(const RRRangeState &r,const RecoveryPositionEffectBinding &b){
 return r.pending.kind==RR_SUPPORT_SL&&r.pending.sl_ticks==b.action.sl_ticks&&
        b.prior_position_entry==DoubleToString(4000,16)&&b.prior_position_magic==700;
}
bool RecoveryRequestInventory(const string &nonce,const string &,const string &,long generation,long login,
    const string &,const string &,long magic) {
 assert(nonce==string(32,'2')&&generation==7&&login==5&&magic==700);
 ++queries;return inv_request_ok;
}
bool RecoveryInvRead(const RecoveryInventoryScope &q,RecoveryInventorySnapshot &r){
 assert(q.nonce==string(32,'2'));r.ok=inv_proof;return inv_ready;
}
bool RecoveryConfirmSingleSupport(const RecoverySingleSupport &t,RRRangeState &r,
                                   const RecoveryPositionEffectBinding &b){
 ++confirms;
 if(!inv_proof || !RecoveryPositionEffectBound(r,b) || t.action.sl_ticks!=400000)return false;
 assert(r.opposite==50);r.support_stage=2;r.live_sl=t.action.sl_ticks;r.pending={};
 return true;
}
#include "SupportRuntimeUnderTest"
int main(){
 const string boot(32,'a'), ver_epoch(32,'b'), watcher(32,'c');
 RecoverySessionContext ctx{11,5,700,"Test-MT5","XAUUSD.s"};
 RecoverySessionLifecycle session{boot,true};
 RecoverySessionIntentScope scope{boot,5,700,"Test-MT5","XAUUSD.s",19,7};
 RecoveryInventoryScope verifier{boot,boot,ver_epoch,7,5,12,"Test-MT5","XAUUSD.s",700};
 RecoveryEvidenceOwner owner{ctx,boot,ver_epoch,12,0};
 const RRRangeState initial{19,14,50,{7,13,15,1,1,400000,1},{},RR_ACTIVE,0,399920,0};
 RRRangeState range=initial;
 const RRSettings cfg{19,7,0,40,0};
 RecoverySupportRuntime task{};
 auto step=[&](){return RecoverySupportRuntimeTimer(task,owner,session,ctx,scope,verifier,watcher,99,range,cfg,.01,.01);};
 local_magic=701;assert(step()==RSR_WAIT && !follows);local_magic=700;
 assert(step()==RSR_WATCHING && follows==1 && owner.serial==1 && !sends);
 for(int i=0;i<3;i++)assert(step()==RSR_WAIT && follows==1 && !sends);
 price_reached=true;assert(step()==RSR_SUBMITTED && sends==1 && !queries && range.opposite==50);
 for(int i=0;i<3;i++)assert(step()==RSR_WAIT && sends==1 && !queries && range.support_stage==0);
 local_sl=4000;assert(step()==RSR_REQUESTED && queries==1 && !confirms && range.support_stage==0);
 assert(step()==RSR_WAIT && queries==1 && !confirms);
 inv_ready=true;
 assert(step()==RSR_CONFIRMED && confirms==1 && range.support_stage==2 && range.opposite==50 && range.live_sl==400000);
 assert(step()==RSR_WAIT && follows==1 && sends==1 && queries==1);
 range=initial;task={};local_sl=3999.20;follow_ok=false;
 assert(step()==RSR_UNKNOWN && follows==2);assert(step()==RSR_UNKNOWN && follows==2);follow_ok=true;
 range=initial;task={};assert(step()==RSR_WATCHING && follows==3);
 ++range.cycle.position_id;assert(step()==RSR_WAIT && drops==1 && task.state==RSS_IDLE);
 range=initial;task={};assert(step()==RSR_WATCHING);submit_ok=false;
 assert(step()==RSR_UNKNOWN && sends==2 && queries==1 && range.opposite==50);
 assert(step()==RSR_UNKNOWN && sends==2);
 range=initial;local_sl=0;range.live_sl=0;task={};
 assert(step()==RSR_WAIT && task.state==RSS_IDLE);
}
