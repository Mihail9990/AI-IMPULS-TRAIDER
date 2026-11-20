#include <cassert>
#include <cmath>
#include <string>
#include <vector>
#include <cstdint>
using string=std::string;
using ulong=std::uint64_t;
template<class T> void ZeroMemory(T &out){out=T{};}
bool MathIsValidNumber(double v){return std::isfinite(v);}
bool RCValidSide(int v){return v==1 || v==-1;}
bool RCBoundedPositive(long v){return v>0;}
enum {RR_NONE,RR_REVERSE,RR_ACTIVE,RR_SUPPORT_SL};
struct RRAction {int kind,side;long entry_ticks,sl_ticks,volume_steps,tp_ticks;};
struct RRKnownOrder {ulong ticket;int side;long entry_ticks,sl_ticks,volume_steps;};
struct RRRangeState {
 ulong scope,position_ticket;
 struct {ulong active_order,position_id,generation;int side,range_id;} cycle;
 int support_stage,reentry_slot,phase;
 RRKnownOrder entry,retire,opposite;
 RRAction pending;
 long initial_sl,opposite_anchor_ticks;
};
long RROppositeAnchor(const RRRangeState &s){return s.opposite_anchor_ticks>0 ? s.opposite_anchor_ticks : s.initial_sl;}
struct RROpeningPair {bool reserved,reverse_verified;RRAction reverse;ulong reverse_ticket;};
bool RROpeningBound(const RRRangeState &,const RROpeningPair &p){return p.reserved;}
bool RRActionEqual(const RRAction &a,const RRAction &b){return a.kind==b.kind && a.side==b.side && a.entry_ticks==b.entry_ticks && a.sl_ticks==b.sl_ticks && a.tp_ticks==b.tp_ticks && a.volume_steps==b.volume_steps;}
bool RRKnownEqual(const RRKnownOrder &a,const RRKnownOrder &b){return a.ticket==b.ticket && a.side==b.side && a.entry_ticks==b.entry_ticks && a.sl_ticks==b.sl_ticks && a.volume_steps==b.volume_steps;}
struct RecoveryInventoryScope {string nonce,executor_epoch,verifier_epoch;long generation,login;string server,symbol;long magic,verifier_chart;};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
struct RecoveryHistoryScope {RecoveryInventoryScope binding;ulong position_identifier,anchor_order;};
struct RecoveryExactReport {bool usable,observed;string reason,source;long sequence;string position_identifier,row;};
struct RecoveryHistorySnapshot {bool usable,observed;string reason;long sequence;int order_count,deal_count;};
struct RecoveryInventorySnapshot {bool complete;long sequence;};
struct RRAlternatingObservation {bool accepted;ulong position;};
bool RecoveryInvHex(const string &s){return s.size()==32 && s.find_first_not_of("0123456789abcdef")==string::npos;}
bool RecoveryInvScopeOK(const RecoveryInventoryScope &s){return RecoveryInvHex(s.nonce);}
void RecoveryExactReportClear(RecoveryExactReport &r){r={};}
void RecoveryHistoryClear(RecoveryHistorySnapshot &r){r={};}
bool RecoveryAdoptionUnsigned(const string &s,ulong &v){try{v=std::stoull(s);return true;}catch(...){return false;}}
bool RecoveryAdoptionUnits(const string &s,double,bool,long &v){v=std::stol(s);return true;}
int StringSplit(const string &s,int sep,std::vector<string> &out) {
 size_t start=0;for(;;){auto end=s.find(char(sep),start);out.push_back(s.substr(start,end==string::npos?end:end-start));if(end==string::npos)break;start=end+1;}
 return int(out.size());
}
enum {TERMINAL_CONNECTED,ACCOUNT_LOGIN,ACCOUNT_SERVER};
bool connected=true;
long login=5;
string _Symbol="XAUUSD.s";
int TerminalInfoInteger(int key){assert(key==TERMINAL_CONNECTED);return connected;}
long AccountInfoInteger(int key){assert(key==ACCOUNT_LOGIN);return login;}
string AccountInfoString(int key){assert(key==ACCOUNT_SERVER);return "Test";}
bool exact_ready=false,hist_ready=false,inv_ready=false,publication_ok=true;
int req_exact=0,req_hist=0,req_inv=0;
RecoveryExactReport report{true,true,"ok","history",12,"9007199254740999",
    "E\thistory\t9007199254740999\t700\t5\t4\t2\t0\t399930\t400010\t0\t0"};
RecoveryHistorySnapshot hist{true,true,"ok",13,1,1};
RecoveryInventorySnapshot inv{true,14};
bool RecoveryExactReportRequest(const RecoveryExactReportScope &s,const string &tag){
 assert(s.binding.nonce==string(32,'1') && s.order_ticket==9007199254740997ULL && tag==string(32,'6'));++req_exact;return publication_ok;
}
bool RecoveryExactReportRead(const RecoveryExactReportScope &,RecoveryExactReport &r){if(!exact_ready)return false;r=report;return true;}
bool RecoveryHistoryRequest(const RecoveryHistoryScope &s,const string &tag){
 assert(s.binding.nonce==string(32,'4') && s.anchor_order==9007199254740997ULL && s.position_identifier==9007199254740999ULL && tag==string(32,'6'));++req_hist;return publication_ok;
}
bool RecoveryHistoryRead(const RecoveryHistoryScope &,RecoveryHistorySnapshot &r){if(!hist_ready)return false;r=hist;return true;}
bool RecoveryRequestInventory(const string &nonce,const string &,const string &,long generation,long login_arg,
    const string &,const string &,long magic) {
 assert(nonce==string(32,'5') && generation==7 && login_arg==5 && magic==700);++req_inv;return publication_ok;
}
bool RecoveryInvRead(const RecoveryInventoryScope &,RecoveryInventorySnapshot &r){if(!inv_ready)return false;r=inv;return true;}

struct RecoveryAlternatingEvidenceBinding;
bool RecoveryAlternatingOrderTerms(const string &type,const string &state,const string &vol,
    const string &rem,const string &entry,const string &sl,const string &tp,
    const RRKnownOrder &wanted,const RecoveryAlternatingEvidenceBinding &){
 return type=="5" && state=="4" && vol=="2" && wanted.side==-1 && rem=="0" &&
        entry=="399930" && sl=="400010" && tp=="0";
}
bool RecoveryAlternatingEvidenceFold(const RRRangeState &,const RROpeningPair &,
    const RecoveryAlternatingEvidenceBinding &,const RecoveryExactReport &e,const RecoveryHistorySnapshot &h,
    const RecoveryInventorySnapshot &i,RRAlternatingObservation &o) {
 if(!i.complete || i.sequence<=h.sequence)return false;
 o.accepted=true;o.position=std::stoull(e.position_identifier);return true;
}

#include "PumpUnderTest"

int main(){
 RRRangeState s{};
 s.scope=19;s.position_ticket=83;s.cycle={81,82,7,1,1};s.phase=RR_ACTIVE;s.initial_sl=399930;
 s.opposite={9007199254740997ULL,-1,399930,400010,2};
 RROpeningPair opening{};
 RecoveryInventoryScope scope{string(32,'1'),string(32,'2'),string(32,'3'),7,5,"Test","XAUUSD.s",700,99};
 RecoveryAlternatingEvidenceBinding b{{scope,9007199254740997ULL},{scope,0,9007199254740997ULL},scope,19,11,{},0.01,0.01};
 b.history.binding.nonce=string(32,'4');b.inventory.nonce=string(32,'5');
 const string tag(32,'6');
 RRAlternatingObservation obs{};
 RecoveryAlternatingPump p{};
 auto step=[&](){return RecoveryAlternatingPumpStep(p,s,opening,obs);};
 auto begin=[&](){assert(RecoveryAlternatingPumpBegin(p,s,opening,b,tag));};
 begin();
 assert(!RecoveryAlternatingPumpBegin(p,s,opening,b,tag));
 assert(step()==RAP_PROGRESS && req_exact==1);
 for(int k=0;k<5;k++)assert(step()==RAP_WAIT && !obs.accepted && req_exact==1 && req_hist==0);
 exact_ready=true;
 // A support update arriving while the pump waits must not break the exact,
 // unchanged opposite order. It is not evidence of any predecessor exit.
 s.opposite_anchor_ticks=399930;s.initial_sl=399950;s.support_stage=2;
 s.pending.kind=RR_SUPPORT_SL;
 assert(step()==RAP_PROGRESS && req_hist==0);
 assert(step()==RAP_PROGRESS && req_hist==1);
 for(int k=0;k<5;k++)assert(step()==RAP_WAIT && req_hist==1 && req_inv==0);
 hist_ready=true;
 assert(step()==RAP_PROGRESS && req_inv==0);
 assert(step()==RAP_PROGRESS && req_inv==1);
 assert(step()==RAP_WAIT && req_inv==1);
 inv_ready=true;
 assert(step()==RAP_PROOF && obs.accepted && obs.position==9007199254740999ULL);
 assert(step()==RAP_STOPPED && !obs.accepted && req_exact==1 && req_hist==1 && req_inv==1);
 s.pending.kind=RR_NONE;s.opposite_anchor_ticks=0;s.initial_sl=399930;s.support_stage=0;

 // Only new fixtures reuse tokens here to exercise isolated branches.
 p={};begin();publication_ok=false;
 assert(step()==RAP_STOPPED && req_exact==2);publication_ok=true;
 assert(step()==RAP_STOPPED && req_exact==2);
 p={};begin();assert(step()==RAP_PROGRESS);report.source="current";
 assert(step()==RAP_STOPPED && req_hist==1);report.source="history";
 p={};begin();assert(step()==RAP_PROGRESS);login=6;
 assert(step()==RAP_STOPPED && req_hist==1);login=5;
 p={};begin();assert(step()==RAP_PROGRESS);s.position_ticket++;
 assert(step()==RAP_STOPPED && req_hist==1);s.position_ticket--;
 p={};begin();assert(step()==RAP_PROGRESS && step()==RAP_PROGRESS && step()==RAP_PROGRESS);
 hist.sequence=12;
 assert(step()==RAP_STOPPED && req_inv==1);hist.sequence=13;
 p={};begin();for(int k=0;k<5;k++)assert(step()==RAP_PROGRESS);
 inv.sequence=13;
 assert(step()==RAP_STOPPED && !obs.accepted);inv.sequence=14;
 p={};b.inventory.nonce=b.history.binding.nonce;
 assert(!RecoveryAlternatingPumpBegin(p,s,opening,b,tag));
}
