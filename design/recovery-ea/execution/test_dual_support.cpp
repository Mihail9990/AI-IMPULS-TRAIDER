#include <cassert>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <vector>
using string=std::string;
using ulong=std::uint64_t;
template<class T> void ZeroMemory(T &v){v=T{};}
int ArraySize(const std::vector<string> &a){return int(a.size());}
bool MathIsValidNumber(double v){return std::isfinite(v);}
int StringSplit(const string &s,int t,std::vector<string> &out){
 out.clear();size_t first=0;
 for(;;){auto p=s.find(char(t),first);out.push_back(s.substr(first,p==string::npos?p:p-first));if(p==string::npos)break;first=p+1;}
 return int(out.size());
}
enum {RR_NONE,RR_REENTRY,RR_WAIT,RR_ARM_REENTRY,RI_TICKET,RECOVERY_EXACT_ORDER_OBSERVED,
      RECOVERY_EXACT_ORDER_HISTORY,RECOVERY_EXACT_ORDER_CURRENT,ORDER_STATE_PLACED=1,ORDER_STATE_FILLED=4,
      ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,DEAL_ENTRY_IN=0};
struct RRKnownOrder {ulong ticket;int side;long entry_ticks,sl_ticks,volume_steps;};
struct RRAction {int kind;ulong serial;int side;long entry_ticks,sl_ticks,volume_steps;};
struct RRRangeState {
 ulong scope,action_serial,position_ticket;
 int phase,reentry_side,reentry_slot;long reentry_ticks;
 RRKnownOrder entry,opposite,retire;RRAction pending;
 struct {ulong generation,position_id,active_order;int range_id;} cycle;
};
struct RRSettings {long lot_steps[24];ulong scope,generation;};
struct RRFilled {
 ulong position_ticket,working_other_ticket;long sl_ticks;bool independently_verified;
 struct {ulong generation,expected_order,executed_order,position_id;int range_id,side;
         long planned_steps,filled_steps,active_steps,actual_entry_ticks;
         bool history_complete,remainder_reconciled,independently_verified;} entry;
};
struct RREffect {RRAction action;ulong created_order;bool terminal_verified;};
struct RecoverySessionIntentScope {string executor_epoch,server,symbol;long login,magic;ulong scope,generation;};
struct RecoveryIntentKey {long id;};
struct RecoveryInventoryScope {string nonce,executor_epoch,verifier_epoch;long generation,login,magic;string server,symbol;};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
struct RecoveryHistoryScope {RecoveryInventoryScope binding;ulong anchor_order,position_identifier;};
struct RecoveryHistorySnapshot {bool usable,observed;long sequence;int order_count,deal_count;string reason;std::vector<string> rows;};
struct RecoveryInventorySnapshot {bool usable,complete;long sequence;int position_count,order_count;string reason;std::vector<string> rows;};
struct RecoveryExactOrderScope {long login;string server,symbol;ulong magic,ticket;};
struct RecoveryExactOrderObservation {int source,state;ulong ticket,position_identifier;};
struct RecoveryExactReport {bool usable,observed;long sequence;string source,reason,position_identifier,row;};
bool RRKnownEqual(const RRKnownOrder &a,const RRKnownOrder &b){
 return a.ticket==b.ticket&&a.side==b.side&&a.entry_ticks==b.entry_ticks&&a.sl_ticks==b.sl_ticks&&a.volume_steps==b.volume_steps;
}
bool RRActionEqual(const RRAction &a,const RRAction &b){return a.kind==b.kind&&a.serial==b.serial;}
void RRClearKnown(RRKnownOrder &o){o={};}
void RRClearAction(RRAction &a){a={};}
void RRSetKnown(RRKnownOrder &o,ulong ticket,const RRAction &a){o={ticket,a.side,a.entry_ticks,a.sl_ticks,a.volume_steps};}
bool RRValid(const RRRangeState &r,const RRSettings &c){return r.scope==c.scope&&r.cycle.generation==c.generation;}
bool RRHasSupportAlternatives(const RRRangeState &r){return r.phase==RR_WAIT&&r.reentry_slot>0&&r.entry.ticket&&r.opposite.ticket;}
bool RCValidSide(int side){return side==1||side==-1;}
int RCReverseSlot(const decltype(RRRangeState::cycle)&){return 2;}
bool RecoveryInvHex(const string &s){return s.size()==32 && s.find_first_not_of("abcdef0123456789")==string::npos;}
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b){
 return a.executor_epoch==b.executor_epoch&&a.verifier_epoch==b.verifier_epoch&&a.generation==b.generation&&
        a.login==b.login&&a.magic==b.magic&&a.server==b.server&&a.symbol==b.symbol&&RecoveryInvHex(a.nonce)&&RecoveryInvHex(b.nonce);
}
bool connected=true,journal_ok=true,verifier_ready=false,entry_filled=true,opposite_filled=false,show_survivor=true;
long exact_requests=0,history_requests=0,inventory_requests=0;
const RRKnownOrder entry{9007199254740997ULL,1,400080,400000,2},opp{9007199254740999ULL,-1,399920,400000,2};
const ulong id1=9007199254741021ULL,id2=9007199254741022ULL,pos=9007199254741023ULL;
string digits(ulong x){return std::to_string(x);}
bool RecoveryAlternatingPumpSession(const RecoveryInventoryScope &s){return connected&&s.login==5;}
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope &,const RRAction &a,double,double,RecoveryIntentKey &out){
 out.id=static_cast<long>(a.serial);return true;
}
bool RecoveryIntentKeysMatch(const RecoveryIntentKey &a,const RecoveryIntentKey &b){return a.id==b.id;}
int RecoveryIntentLookup(const RecoveryIntentKey &,string &t){t=digits(entry.ticket);return journal_ok?RI_TICKET:0;}
int RecoveryObserveExactOrder(const RecoveryExactOrderScope &q,RecoveryExactOrderObservation &o){
 const bool en=q.ticket==entry.ticket;assert(en||q.ticket==opp.ticket);
 assert(q.login==5&&q.magic==700&&q.server=="Test"&&q.symbol=="XAUUSD.s");
 const bool fill=en?entry_filled:opposite_filled;
 o={fill?RECOVERY_EXACT_ORDER_HISTORY:RECOVERY_EXACT_ORDER_CURRENT,
    fill?ORDER_STATE_FILLED:ORDER_STATE_PLACED,q.ticket,fill?(en?id1:id2):0};
 return RECOVERY_EXACT_ORDER_OBSERVED;
}
bool RecoveryAdoptionUnsigned(const string &s,ulong &v){
 try{size_t n;v=std::stoull(s,&n);return n==s.size();}catch(...){return false;}
}
bool RecoveryAdoptionUnits(const string &s,double,bool zero,long &v){
 try{size_t n;v=std::stol(s,&n);return n==s.size()&&(v>0||(zero&&v==0));}catch(...){return false;}
}
bool RecoveryExactReportRequest(const RecoveryExactReportScope &r,const string &tag){
 assert((r.order_ticket==entry.ticket&&r.binding.nonce==string(32,'1'))||
        (r.order_ticket==opp.ticket&&r.binding.nonce==string(32,'2')));
 assert(tag==string(32,'5'));++exact_requests;return true;
}
bool RecoveryExactReportRead(const RecoveryExactReportScope &r,RecoveryExactReport &o){
 if(!verifier_ready)return false;
 const bool en=r.order_ticket==entry.ticket,fill=en?entry_filled:opposite_filled;
 const auto &known=en?entry:opp;
 const string side=en?"4":"5",source=fill?"history":"current",position=fill?digits(en?id1:id2):"0";
 o={true,true,en?11:12,source,"ok",position,
    "E\t"+source+"\t"+position+"\t700\t"+side+"\t"+(fill?"4":"1")+"\t2\t"+(fill?"0":"2")+"\t"+
    std::to_string(known.entry_ticks)+"\t400000\t0\t0"};
 return true;
}
bool RecoveryHistoryRequest(const RecoveryHistoryScope &q,const string &tag){
 assert(q.binding.nonce==string(32,'3')&&tag==string(32,'5'));
 assert(q.anchor_order==(entry_filled?entry.ticket:opp.ticket)&&q.position_identifier==(entry_filled?id1:id2));
 ++history_requests;return true;
}
bool RecoveryHistoryRead(const RecoveryHistoryScope &q,RecoveryHistorySnapshot &o){
 const bool en=q.anchor_order==entry.ticket;
 const string anchor=digits(q.anchor_order),type=en?"4":"5",side=en?"BUY":"SELL";
 const string initial=en?"400080":"399920", actual=en?"400085":"399915";
 o={true,true,13,1,1,"ok",
    {"H\t"+anchor+"\t700\t"+type+"\t4\t7\t2\t0\t"+initial+"\t400000\t0",
     "D\t1\t"+anchor+"\t700\t"+side+"\t0\t3\t8\t2\t"+actual+"\t400000\t0"}};
 return true;
}
bool RecoveryRequestInventory(const string &nonce,const string &,const string &,long generation,long login,
    const string &,const string &,long magic){
 assert(nonce==string(32,'4')&&generation==7&&login==5&&magic==700);++inventory_requests;return true;
}
bool RecoveryInvRead(const RecoveryInventoryScope &,RecoveryInventorySnapshot &o){
 const bool e=entry_filled;
 const auto &other=e?opp:entry;
 const string side=e?"BUY":"SELL",actual=e?"400085":"399915";
 o={true,true,14,1,0,"ok",{"P\t"+digits(e?id1:id2)+"\t"+digits(pos)+"\t700\t"+side+"\t2\t"+actual+"\t400000\t0"}};
 if(show_survivor){
  o.order_count=1;
  o.rows.push_back("O\t"+digits(other.ticket)+"\t700\t0\t"+(e?"5":"4")+"\t1\t2\t2\t"+
                   std::to_string(other.entry_ticks)+"\t400000\t0\t0");
 }
 return true;
}
#include "DualSupportUnderTest"
int main(){
 const RecoveryInventoryScope base{string(32,'1'),string(32,'a'),string(32,'b'),7,5,700,"Test","XAUUSD.s"};
 RecoveryDualSupportBinding b{{base,entry.ticket},{base,opp.ticket},{base,0,0},base,string(32,'5'),10,.01,.01};
 b.opposite.binding.nonce=string(32,'2');b.history.binding.nonce=string(32,'3');b.inventory.nonce=string(32,'4');
 RRRangeState range{19,8,0,RR_WAIT,1,1,400080,entry,opp,{},{},{7,0,0,1}};
 RRSettings cfg{};cfg.scope=19;cfg.generation=7;cfg.lot_steps[0]=2;
 RecoveryDualSupportPump pump{};RRFilled f{};RRKnownOrder remain{};
 auto begin=[&](){assert(RecoveryBeginDualSupport(pump,range,cfg,b));};
 auto step=[&](){return RecoveryStepDualSupport(pump,range,cfg,f,remain);};
 begin();assert(!RecoveryBeginDualSupport(pump,range,cfg,b));
 entry_filled=false;
 assert(step()==RDS_WAIT && exact_requests==0);
 entry_filled=true;
 assert(step()==RDS_PROGRESS && step()==RDS_PROGRESS && step()==RDS_WAIT && exact_requests==1);
 verifier_ready=true;
 for(int n=0;n<6;n++)assert(step()==RDS_PROGRESS);
 assert(step()==RDS_PROOF && f.independently_verified && f.entry.executed_order==entry.ticket &&
        f.entry.position_id==id1 && f.entry.actual_entry_ticks==400085 &&
        f.working_other_ticket==opp.ticket && RRKnownEqual(opp,remain) && pump.verified_sequence==14);
 assert(step()==RDS_UNKNOWN && !f.independently_verified && remain.ticket==0);
 assert(exact_requests==2&&history_requests==1&&inventory_requests==1);
 pump={};entry_filled=false;opposite_filled=true;begin();
 for(int n=0;n<8;n++)assert(step()==RDS_PROGRESS);
 assert(step()==RDS_PROOF && f.entry.executed_order==opp.ticket && f.entry.actual_entry_ticks==399915 &&
        RRKnownEqual(entry,remain));
 pump={};entry_filled=true;begin();
 for(int n=0;n<4;n++)assert(step()==RDS_PROGRESS);
 assert(step()==RDS_COLLISION && !f.independently_verified && remain.ticket==0);
 assert(history_requests==2&&inventory_requests==2);
 pump={};opposite_filled=false;show_survivor=false;begin();
 for(int n=0;n<8;n++)assert(step()==RDS_PROGRESS);
 assert(step()==RDS_UNKNOWN && !f.independently_verified && remain.ticket==0);
 show_survivor=true;
 RRRangeState staged=range;staged.phase=RR_ARM_REENTRY;staged.entry={};
 RRAction action{RR_REENTRY,8,1,400080,400000,2};staged.pending=action;
 const RecoverySessionIntentScope scope{base.executor_epoch,base.server,base.symbol,base.login,base.magic,19,7};
 RRRangeState transport{};
 journal_ok=false;
 assert(!RecoveryDualPendingReentryView(staged,cfg,scope,action,{8},entry.ticket,.01,.01,transport));
 journal_ok=true;
 assert(RecoveryDualPendingReentryView(staged,cfg,scope,action,{8},entry.ticket,.01,.01,transport));
 assert(staged.phase==RR_ARM_REENTRY&&staged.entry.ticket==0&&transport.phase==RR_WAIT);
 pump={};assert(RecoveryBeginDualSupport(pump,transport,cfg,b));
 RREffect effect{};
 assert(!RecoveryDualPendingReentryEffect(pump,staged,cfg,scope,action,{8},entry.ticket,effect));
 range=transport;
 for(int n=0;n<8;n++)assert(step()==RDS_PROGRESS);
 assert(step()==RDS_PROOF);
 assert(RecoveryDualPendingReentryEffect(pump,staged,cfg,scope,action,{8},entry.ticket,effect) &&
        effect.created_order==entry.ticket && effect.terminal_verified && staged.entry.ticket==0);
}
