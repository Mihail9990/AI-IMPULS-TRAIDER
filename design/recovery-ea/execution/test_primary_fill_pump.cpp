#include <cassert>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>
using string=std::string;
using ulong=std::uint64_t;
template<class T> void ZeroMemory(T &out){out=T{};}
bool MathIsValidNumber(double q){return std::isfinite(q);}
string DoubleToString(double q,int d){assert(d==16);return std::to_string(q);}
int StringSplit(const string &s,int sep,std::vector<string> &out){
 size_t start=0;
 for(;;){
  auto stop=s.find(char(sep),start);
  out.push_back(s.substr(start,stop==string::npos?stop:stop-start));
  if(stop==string::npos)break;
  start=stop+1;
 }
 return int(out.size());
}
bool RecoveryInvHex(const string &s){
 return s.size()==32 && s.find_first_not_of("0123456789abcdef")==string::npos;
}
struct RRRangeState {struct {int range_id;} cycle;long serial;};
struct RRSettings {long generation;};
struct RRKnownOrder {ulong ticket;int side;long entry_ticks,sl_ticks,volume_steps;};
bool RRKnownEqual(const RRKnownOrder &a,const RRKnownOrder &b){
 return a.ticket==b.ticket&&a.side==b.side&&a.entry_ticks==b.entry_ticks&&
        a.sl_ticks==b.sl_ticks&&a.volume_steps==b.volume_steps;
}
struct RecoveryInventoryScope {string nonce,executor_epoch,verifier_epoch;long generation,login;string server,symbol;long magic;};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
struct RecoveryHistoryScope {RecoveryInventoryScope binding;ulong position_identifier,anchor_order;};
struct RecoveryPrimaryFillBinding {
 RecoveryExactReportScope exact;RecoveryHistoryScope history;RecoveryInventoryScope inventory;
 ulong scope,verify_after;long routed;double price_tick,volume_step;
};
struct RecoveryExactReport {bool usable,observed;string reason,source;long sequence;string position_identifier,row;};
struct RecoveryHistorySnapshot {bool usable,observed;string reason;long sequence;int order_count,deal_count;};
struct RecoveryInventorySnapshot {bool complete;long sequence;};
struct RRFilled {bool asserted;long filled_steps;};
struct RecoveryExactOrderScope {long login;string server,symbol;ulong magic,ticket;};
struct RecoveryExactOrderObservation {
 int source;ulong ticket,magic,position_identifier;
 string symbol;long type,state;double initial_volume,remaining_volume,price,sl,tp,stop_limit;
};
enum {RECOVERY_EXACT_ORDER_OBSERVED,RECOVERY_EXACT_ORDER_UNKNOWN,
      RECOVERY_EXACT_ORDER_HISTORY,RECOVERY_EXACT_ORDER_CURRENT,
      ORDER_STATE_CANCELED=2,ORDER_STATE_PARTIAL=3,ORDER_STATE_FILLED=4};
const ulong ticket=9007199254740997ULL, position=9007199254740999ULL;
RecoveryExactOrderObservation local{RECOVERY_EXACT_ORDER_CURRENT,ticket,700,position,"XAUUSD.s",4,ORDER_STATE_FILLED,
                                    .02,0,4000,3999.20,0,0};
RecoveryExactReport exact{true,true,"ok","history",12,std::to_string(position),
 "E\thistory\t"+std::to_string(position)+"\t700\t4\t4\t0.02\t0\t4000\t3999.20\t0\t0"};
RecoveryHistorySnapshot hist{true,true,"ok",13,1,1};
RecoveryInventorySnapshot inv{true,14};
bool connected=true,can_exact=false,can_hist=false,can_inv=false,allow_write=true,valid_fold=true;
bool local_missing=false;
int req_exact=0,req_hist=0,req_inv=0,reads=0;
bool RecoveryPrimaryFillWanted(const RRRangeState &s,const RRSettings &cfg,
     const RecoveryPrimaryFillBinding &b,RRKnownOrder &o){
 if(b.exact.order_ticket!=ticket || b.history.anchor_order!=ticket || !b.history.position_identifier ||
    s.serial!=b.routed || cfg.generation!=b.exact.binding.generation)return false;
 o={ticket,1,400000,399920,2};return true;
}
bool RecoveryAlternatingPumpSession(const RecoveryInventoryScope &s){
 return connected && s.symbol=="XAUUSD.s" && s.login==5;
}
int RecoveryObserveExactOrder(const RecoveryExactOrderScope &r,RecoveryExactOrderObservation &o){
 assert(r.symbol=="XAUUSD.s" && r.login==5 && r.server=="Test" && r.magic==700 && r.ticket==ticket);
 ++reads;o=local;return local_missing?RECOVERY_EXACT_ORDER_UNKNOWN:RECOVERY_EXACT_ORDER_OBSERVED;
}
bool RecoveryPrimaryFillTerms(const string &kind,const string &state,const string &vol,const string &rem,
    const string &entry,const string &sl,const string &tp,const RRKnownOrder &,const RecoveryPrimaryFillBinding &,
    ulong &terminal,long &remaining){
 terminal=std::stoul(state);remaining=std::stod(rem)>0;
 return kind=="4" && std::stod(vol)==.02 && std::stod(entry)==4000 &&
        std::stod(sl)==3999.20 && std::stod(tp)==0 &&
        ((terminal==ORDER_STATE_FILLED&&remaining==0) ||
         (terminal==ORDER_STATE_CANCELED&&std::stod(rem)==.01));
}
bool RecoveryAdoptionUnsigned(const string &s,ulong &v){
 try {v=std::stoull(s);return true;}catch(...){return false;}
}
bool RecoveryAdoptionUnits(const string &s,double,bool,long &v){
 try {v=std::stol(s);return true;}catch(...){return false;}
}
void RecoveryExactReportClear(RecoveryExactReport &e){e={};}
void RecoveryHistoryClear(RecoveryHistorySnapshot &h){h={};}
bool RecoveryExactReportRequest(const RecoveryExactReportScope &s,const string &tag){
 assert(s.order_ticket==ticket && s.binding.nonce==string(32,'1') && tag==string(32,'6'));
 ++req_exact;return allow_write;
}
bool RecoveryExactReportRead(const RecoveryExactReportScope &,RecoveryExactReport &out){
 if(!can_exact)return false;
 out=exact;return true;
}
bool RecoveryHistoryRequest(const RecoveryHistoryScope &s,const string &tag){
 assert(s.anchor_order==ticket && s.position_identifier==position && s.binding.nonce==string(32,'4') &&
        tag==string(32,'6'));
 ++req_hist;return allow_write;
}
bool RecoveryHistoryRead(const RecoveryHistoryScope &,RecoveryHistorySnapshot &out){
 if(!can_hist)return false;
 out=hist;return true;
}
bool RecoveryRequestInventory(const string &nonce,const string &,const string &,long generation,long login,
      const string &,const string &,long magic){
 assert(nonce==string(32,'5')&&generation==7&&login==5&&magic==700);
 ++req_inv;return allow_write;
}
bool RecoveryInvRead(const RecoveryInventoryScope &,RecoveryInventorySnapshot &out){
 if(!can_inv)return false;
 out=inv;return true;
}
bool RecoveryPrimaryFillFold(const RRRangeState &,const RRSettings &,const RecoveryPrimaryFillBinding &b,
    const RecoveryExactReport &e,const RecoveryHistorySnapshot &h,const RecoveryInventorySnapshot &i,RRFilled &out){
 if(!valid_fold || !i.complete || i.sequence<=h.sequence || b.history.position_identifier!=position)return false;
 out.asserted=true;out.filled_steps=e.row.find("\t2\t0.02\t0.01\t")!=string::npos ? 1 : 2;
 return true;
}
#include "PrimaryPumpUnderTest"

int main(){
 RRRangeState s{{1},32};RRSettings cfg{7};
 RecoveryInventoryScope scope{string(32,'1'),string(32,'2'),string(32,'3'),7,5,"Test","XAUUSD.s",700};
 RecoveryPrimaryFillBinding b{{scope,ticket},{scope,0,ticket},scope,3,11,32,.01,.01};
 b.history.binding.nonce=string(32,'4');b.inventory.nonce=string(32,'5');
 RecoveryPrimaryFillPump p{};RRFilled f{};
 const string tag(32,'6');
 auto step=[&](){return RecoveryPrimaryPumpStep(p,s,cfg,f);};
 auto begin=[&](){assert(RecoveryPrimaryPumpBegin(p,s,cfg,b,tag));};
 begin();
 assert(!RecoveryPrimaryPumpBegin(p,s,cfg,b,tag));
 local_missing=true;
 assert(step()==RPP_WAIT && req_exact==0 && !f.asserted);
 local_missing=false;
 assert(step()==RPP_WAIT && req_exact==0);
 local.source=RECOVERY_EXACT_ORDER_HISTORY;local.state=ORDER_STATE_PARTIAL;local.remaining_volume=.01;
 assert(step()==RPP_WAIT && req_exact==0);
 local.state=ORDER_STATE_FILLED;local.remaining_volume=0;
 assert(step()==RPP_PROGRESS && req_exact==0);
 assert(step()==RPP_PROGRESS && req_exact==1);
 for(int i=0;i<3;i++)assert(step()==RPP_WAIT && req_exact==1 && req_hist==0);
 can_exact=true;
 assert(step()==RPP_PROGRESS && p.binding.history.position_identifier==position);
 assert(step()==RPP_PROGRESS && req_hist==1);
 assert(step()==RPP_WAIT && req_hist==1);
 can_hist=true;
 assert(step()==RPP_PROGRESS && step()==RPP_PROGRESS && req_inv==1);
 assert(step()==RPP_WAIT && !f.asserted);
 can_inv=true;
 assert(step()==RPP_PROOF && f.asserted && f.filled_steps==2);
 assert(step()==RPP_STOPPED && !f.asserted && req_exact==1 && req_hist==1 && req_inv==1);

 // Terminal partial cancellation is eligible, but only final verifier fold
 // accepts actual protected position/history. No action submits cancellation.
 p={};begin();local.state=ORDER_STATE_CANCELED;local.remaining_volume=.01;
 exact.row="E\thistory\t"+std::to_string(position)+"\t700\t4\t2\t0.02\t0.01\t4000\t3999.20\t0\t0";
 for(int i=0;i<6;i++)assert(step()==RPP_PROGRESS);
 assert(step()==RPP_PROOF && f.asserted && f.filled_steps==1);
 p={};begin();for(int i=0;i<6;i++)assert(step()==RPP_PROGRESS);
 valid_fold=false;assert(step()==RPP_STOPPED && !f.asserted);valid_fold=true;
 p={};begin();assert(step()==RPP_PROGRESS);
 allow_write=false;assert(step()==RPP_STOPPED);allow_write=true;
 const int before=req_exact;assert(step()==RPP_STOPPED && req_exact==before);
 p={};begin();assert(step()==RPP_PROGRESS && step()==RPP_PROGRESS);
 exact.position_identifier="700799";
 assert(step()==RPP_STOPPED && !f.asserted);
 exact.position_identifier=std::to_string(position);
 p={};begin();assert(step()==RPP_PROGRESS);s.serial++;
 assert(step()==RPP_STOPPED && !f.asserted && p.state==RPP_UNRESOLVED);
 s.serial--;
 p={};begin();connected=false;
 const int previous=reads;
 assert(step()==RPP_STOPPED && reads==previous);
}
