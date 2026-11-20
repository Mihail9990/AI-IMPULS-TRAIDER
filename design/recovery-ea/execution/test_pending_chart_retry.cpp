#include <cassert>
#include <climits>
#include <map>
#include <string>
#include <vector>

using string=std::string;
using ulong=unsigned long;
template<class T> void ZeroMemory(T &value){value={};}
enum {REC_CONTROL_IDLE,REC_CONTROL_PENDING_WAIT,REC_CONTROL_ADOPTED};
enum {RMP_PREPARED=1,RMP_ATTEMPTED,ORDER_STATE,ORDER_STATE_FILLED};
enum {RMPF_UNUSED,RMPF_TERMINAL,RMPF_PROOF};
struct RecoveryPanelControl {int mode;};
struct RecoverySessionLifecycle {string epoch;};
struct RecoverySessionContext {string symbol;};
struct RecoverySessionIntentScope {string executor_epoch;ulong generation;};
struct RecoveryInventoryScope {string verifier_epoch,nonce;long verifier_chart;};
struct RecoveryEvidenceOwner {string executor_epoch,verifier_epoch;long verifier_chart;RecoverySessionContext context;ulong serial;};
struct RecoveryManualPendingCandidate {ulong ticket;long entry;};
struct RecoveryManualPendingEarly {RecoveryManualPendingCandidate original;long time_type,expiration;};
struct RecoveryManualPendingOwner {ulong ticket;int phase;long time_type,expiration;};
struct RecoveryManualPendingFill {int state;};
struct RecoveryInventorySnapshot {bool parsable,complete;std::vector<RecoveryManualPendingCandidate> orders;};
struct RROpeningPair {bool sl_required;int sl,reverse;};
struct RecoveryManualHandoff {bool seeded;struct {int ranges[2];} backing;struct {RROpeningPair opening;ulong next_exact_ticket;} ordinary;};
struct RecoveryPairSendResult {int reverse;};
struct RRSettings {};
struct RecoveryIntentKey {};
// CHART TYPES

const RecoveryManualPendingCandidate original{71,40000},replacement{72,41000};
std::vector<RecoveryManualPendingCandidate> working{original};
ulong filled=0,modified=0,dispatched=0;
bool verifier_proved_fill=false;
std::vector<string> requests;
std::map<string,RecoveryInventorySnapshot> files;

bool RecoveryPendingChartManualOrder(const string &,ulong &ticket){
 if(working.size()!=1)return false;
 ticket=working[0].ticket;return true;
}
bool RecoveryCaptureManualPendingEarly(ulong ticket,const RecoverySessionContext &,double,double,RecoveryManualPendingEarly &out){
 if(working.size()!=1 || ticket!=working[0].ticket)return false;
 out={working[0],1,99};return true;
}
bool HistoryOrderSelect(ulong ticket){return filled==ticket;}
bool HistoryOrderGetInteger(ulong ticket,int,long &state){state=ORDER_STATE_FILLED;return filled==ticket;}
bool RecoveryManualPendingEarlyTerms(const RecoveryManualPendingCandidate &a,const RecoveryManualPendingCandidate &b){
 return a.ticket==b.ticket && a.entry==b.entry;
}
template<class... T> bool RecoveryManualPendingScope(T&&...){return true;}
bool RecoverySessionScopeEqual(const RecoverySessionContext &a,const RecoverySessionContext &b){return a.symbol==b.symbol;}
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b){
 return a.verifier_epoch==b.verifier_epoch && a.verifier_chart==b.verifier_chart;
}
bool RecoveryInvScopeOK(const RecoveryInventoryScope &s){return !s.nonce.empty();}
bool RecoveryInvHex(const string &s){return !s.empty();}
string RecoveryOwnerNonce(const string &kind,const string &id){return kind+":"+id;}
string filename(const string &nonce){return "RECOVERY_VERIFY\\"+nonce+".snapshot";}
bool FileIsExist(const string &name){return files.count(name)!=0;}
bool RecoveryManualPublishInventory(const RecoveryInventoryScope &s){
 for(const auto &prior:requests)assert(prior!=s.nonce);
 requests.push_back(s.nonce);return true;
}
bool RecoveryInvRead(const RecoveryInventoryScope &s,RecoveryInventorySnapshot &out){
 auto it=files.find(filename(s.nonce));
 if(it==files.end() || !it->second.parsable)return false;
 out=it->second;return true;
}
bool RecoveryFindManualPending(const RecoveryInventorySnapshot &s,const RecoveryInventoryScope &,ulong,ulong,
    double,double,RecoveryManualPendingCandidate &candidate){
 if(!s.complete || s.orders.size()!=1)return false;
 candidate=s.orders[0];return true;
}
bool RecoveryPrepareManualPending(RecoveryManualPendingOwner &o,bool,const RecoverySessionLifecycle &,
    const RecoverySessionContext &,const RecoverySessionIntentScope &,const RRSettings &,const RecoveryInventoryScope &,
    const RecoveryInventorySnapshot &,ulong,ulong ticket,double,double){
 if(working.size()!=1 || working[0].ticket!=ticket)return false;
 o={ticket,RMP_PREPARED,1,99};return true;
}
bool RecoveryPrepareManualPendingEarly(RecoveryManualPendingOwner &o,bool,const RecoverySessionLifecycle &,
    const RecoverySessionContext &,const RecoverySessionIntentScope &,const RRSettings &,const RecoveryInventoryScope &,
    const RecoveryInventorySnapshot &s,const RecoveryManualPendingEarly &frozen,double,double){
 if(!s.complete || filled!=frozen.original.ticket)return false;
 for(const auto &row:s.orders)if(row.ticket==filled)return false;
 o={filled,RMP_ATTEMPTED,1,99};return true;
}
template<class... T> void RecoveryAttemptManualPendingSL(RecoveryManualPendingOwner &o,T&&...){
 assert(modified==0);modified=o.ticket;o.phase=RMP_ATTEMPTED;
}
template<class... T> void RecoveryManualPendingFillBegin(RecoveryManualPendingFill &f,T&&...){f.state=RMPF_TERMINAL;}
template<class... T> bool RecoveryPendingChartSLVisible(T&&...){return false;}
template<class... T> bool RecoveryConfirmManualPendingSL(T&&...){return false;}
int RecoveryManualPendingFillStep(const RecoveryManualPendingFill &,const RecoveryManualPendingOwner &o,ulong){
 return verifier_proved_fill && filled==o.ticket ? RMPF_PROOF : RMPF_TERMINAL;
}
template<class... T> bool RecoveryProjectManualPendingOpening(RecoveryManualHandoff &handoff,T&&...){
 handoff.seeded=true;return true;
}
template<class... T> bool RecoveryBuildSessionIntentKey(T&&...){return true;}
template<class... T> void RecoveryDispatchOpeningPair(T&&...){assert(dispatched==0);dispatched=filled;}
int RecoveryRangeOwnerReceiptTicket(int r){return r;}
// CHART TIMER

int main(){
 RecoveryManualPendingChart chart{};
 RecoveryManualHandoff adopted{};
 RecoveryPanelControl panel{REC_CONTROL_PENDING_WAIT};
 RecoverySessionContext live{"X"};
 RecoveryEvidenceOwner verifier{"exec","verify",42,live,0};
 const RecoveryInventoryScope scope{"verify","base",42};
 auto tick=[&](){
    return RecoveryManualPendingChartTimer(chart,adopted,panel,verifier,{"exec"},live,{"exec",1},scope,{},.01,.01);
 };
 auto advance=[&](int count){for(int i=0;i<count;i++)assert(!tick());};
 auto next_request=[&](){
    auto previous=requests.size();
    for(int i=0;i<64 && requests.size()==previous;i++)assert(!tick());
    assert(requests.size()==previous+1);return filename(requests.back());
 };

 auto first=next_request();
 advance(80);assert(requests.size()==1); // outstanding, no published reply
 files[first]={false,false,{}}; // immutable but malformed
 auto second=next_request();
 files[second]={true,false,{}}; // verifier reported "changed"
 auto third=next_request();
 working={original,replacement};
 files[third]={true,true,working};
 advance(80);assert(requests.size()==3 && modified==0 && !adopted.seeded);

 working={replacement};
 advance(80);assert(requests.size()==3 && modified==0); // never switch to 72
 filled=original.ticket;
 auto fourth=next_request();
 files[fourth]={true,true,working};
 advance(3);
 assert(chart.origin.ticket==original.ticket && modified==0 && dispatched==0 && !adopted.seeded);
 verifier_proved_fill=true;assert(tick());
 assert(panel.mode==REC_CONTROL_ADOPTED && adopted.seeded && dispatched==original.ticket);
 advance(3);

 // Cancel before the new verifier response; a new arm gets its own nonce.
 RecoveryManualPendingChartReset(chart);adopted={};filled=0;dispatched=0;verifier_proved_fill=false;
 panel.mode=REC_CONTROL_PENDING_WAIT;working={original};
 auto canceled=next_request();files[canceled]={true,false,{}};
 auto late=next_request();
 panel.mode=REC_CONTROL_IDLE;advance(1);RecoveryManualPendingChartReset(chart);
 files[late]={true,true,{original}};
 working={replacement};panel.mode=REC_CONTROL_PENDING_WAIT;
 auto fresh=next_request();
 advance(40);assert(modified==0 && dispatched==0);
 files[fresh]={true,true,{replacement}};advance(5);
 assert(modified==replacement.ticket && dispatched==0);
}
