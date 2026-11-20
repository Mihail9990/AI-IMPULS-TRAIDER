#include <cassert>
#include <cstdint>
#include <sstream>
#include <string>
#include <vector>
using ulong=std::uint64_t;
using string=std::string;
template<class T> void ZeroMemory(T &s) {s={};}
int ArraySize(const std::vector<string> &a) {return static_cast<int>(a.size());}
int StringSplit(const string &s,int delim,std::vector<string> &a) {
 a.clear();std::stringstream input(s);string cell;
 while(std::getline(input,cell,static_cast<char>(delim)))a.push_back(cell);
 return ArraySize(a);
}
bool RecoveryInvOrder(const std::vector<string> &a) {return a[0]=="O";}
struct RecoveryInventoryScope {string nonce;};
struct RecoveryManualPendingCandidate {
 RecoveryInventoryScope request;ulong sequence,ticket,order_magic;
 int side;long entry_ticks,sl_ticks,tp_ticks,volume_steps;
};
struct RecoverySessionLifecycle {};
struct RecoverySessionContext {};
struct RecoverySessionIntentScope {ulong scope,generation;string executor_epoch;};
struct RRSettings {ulong scope,generation;long initial_sl_ticks;};
struct RecoveryInventorySnapshot {long sequence;int position_count;string reason;std::vector<string> rows;};
struct RRAction {int kind,range_id,side;ulong scope,generation,serial,order_ticket;long entry_ticks,sl_ticks,tp_ticks,volume_steps;};
struct RecoveryIntentKey {};
struct RecoveryManualPendingOwner {
 int phase; RecoveryManualPendingCandidate original;ulong scope,generation;
 string executor_epoch;double price_tick,volume_step;long target_sl,time_type,expiration;
 RRAction action; RecoveryIntentKey key; bool local_filled_before_prepare; struct {int state;} receipt;
};
enum {RMP_IDLE,RMP_ATTEMPTED,RD_NOT_SENT,RR_MANUAL_PENDING_SL};
bool RCBoundedPositive(long x) {return x>0;}
bool RRSettingsValid(const RRSettings &s) {return s.initial_sl_ticks>0;}
bool RecoveryManualPendingScope(const RecoverySessionLifecycle &,const RecoverySessionContext &,
 const RecoverySessionIntentScope &,const RecoveryInventoryScope &q) {return q.nonce=="boot:1";}
bool RecoveryManualPendingInventoryOK(const RecoveryInventorySnapshot &s,ulong floor) {
 return s.sequence>0 && (ulong)s.sequence>floor;
}
void RRClearAction(RRAction &a) {a={};}
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope &,const RRAction &,double,double,RecoveryIntentKey &) {
 return true;
}
bool terminal_filled=true;
long terminal_expiration=99;
bool RecoveryManualPendingLocalFilled(const RecoveryManualPendingCandidate &c,const RecoveryIntentKey &,
 double,double,long &type,long &expiration) {
 type=2;expiration=terminal_expiration;return terminal_filled && c.ticket==9007199254740999UL;
}
bool RecoveryManualPendingBuildKey(const RecoveryManualPendingOwner &,const RecoverySessionIntentScope &,
 RecoveryIntentKey &) {return true;}
// ACTUAL
int main() {
 RecoveryManualPendingEarly f{{{},0,9007199254740999UL,0,1,400000,0,400150,1},2,99};
 RecoveryManualPendingOwner o{};
 const RecoverySessionIntentScope s{3,4,"boot"};
 const RRSettings c{3,4,80};
 const RecoveryInventorySnapshot missing{61,0,"ok",{}};
 assert(!RecoveryPrepareManualPendingEarly(o,false,{}, {},s,c,{"boot:1"},missing,f,.01,.01));
 assert(o.phase==RMP_IDLE); // canceled before delayed response
 assert(!RecoveryPrepareManualPendingEarly(o,true,{}, {},s,c,{"boot:2"},missing,f,.01,.01));
 terminal_filled=false;
 assert(!RecoveryPrepareManualPendingEarly(o,true,{}, {},s,c,{"boot:1"},missing,f,.01,.01));
 terminal_filled=true;terminal_expiration=100;
 assert(!RecoveryPrepareManualPendingEarly(o,true,{}, {},s,c,{"boot:1"},missing,f,.01,.01));
 terminal_expiration=99;
 auto present=missing;present.rows={"O\t9007199254740999\t0\t0\t4\t1\t1\t1\t400000\t0\t400150\t0"};
 assert(!RecoveryPrepareManualPendingEarly(o,true,{}, {},s,c,{"boot:1"},present,f,.01,.01));
 assert(RecoveryPrepareManualPendingEarly(o,true,{}, {},s,c,{"boot:1"},missing,f,.01,.01));
 assert(o.phase==RMP_ATTEMPTED && o.receipt.state==RD_NOT_SENT && o.local_filled_before_prepare);
 assert(o.target_sl==399920 && o.original.sequence==61 && o.original.request.nonce=="boot:1");
 assert(!RecoveryPrepareManualPendingEarly(o,true,{}, {},s,c,{"boot:1"},missing,f,.01,.01));
 o={};f.original.side=-1;
 assert(RecoveryPrepareManualPendingEarly(o,true,{}, {},s,c,{"boot:1"},missing,f,.01,.01));
 assert(o.target_sl==400080 && o.action.tp_ticks==400150);
 auto change=f.original;change.tp_ticks++;
 assert(!RecoveryManualPendingEarlyTerms(change,f.original));
}
