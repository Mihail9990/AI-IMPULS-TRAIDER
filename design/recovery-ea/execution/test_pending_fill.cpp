#include <algorithm>
#include <cassert>
#include <climits>
#include <cstdint>
#include <sstream>
#include <string>
#include <vector>
using ulong=std::uint64_t;
using string=std::string;
#include "RecoveryAlternatingFill.mqh"
const ulong ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,ORDER_STATE_PLACED=1,ORDER_STATE_CANCELED=2,ORDER_STATE_FILLED=4,DEAL_ENTRY_IN=0;
static bool RecoveryAdoptionUnsigned(const string &s,ulong &out) {
 if(s.empty() || s.find_first_not_of("0123456789")!=string::npos)return false;
 try {out=std::stoull(s);return true;} catch(...) {return false;}
}
static bool RecoveryAdoptionUnits(const string &s,double scale,bool zero,long &out) {
 ulong raw;
 if(scale!=1. || !RecoveryAdoptionUnsigned(s,raw) || raw>LONG_MAX || (raw==0&&!zero))return false;
 out=static_cast<long>(raw);return true;
}
static int StringSplit(const string &s,int delim,std::vector<string>& f) {
 f.clear();std::stringstream input(s);string word;
 while(std::getline(input,word,static_cast<char>(delim)))f.push_back(word);
 return static_cast<int>(f.size());
}
static int ArraySize(const std::vector<string>& v) {return static_cast<int>(v.size());}
template<class T> void ZeroMemory(T& a) {a={};}
struct RecoveryHistorySnapshot {bool usable,observed;string reason;long sequence;int order_count,deal_count;std::vector<string> rows;};
struct RecoveryInventorySnapshot {bool usable,complete;string reason;long sequence;int position_count,order_count;std::vector<string> rows;};
const int RMPF_DONE=7;
struct RecoveryInventoryScope {long generation;string nonce;long magic;};
struct RecoverySessionLifecycle {};
struct RecoverySessionContext {};
struct RecoverySessionIntentScope {};
struct RecoveryIntentKey {};
struct RecoveryManualHandoff {
 bool seeded;
 ulong first_position_magic;
 struct {ulong next_exact_ticket,revision;RROpeningPair opening;RRAlternationLedger ledger;} ordinary;
 struct {RRRangeState ranges[2];} backing;
};
bool RecoveryInvPosition(const std::vector<string> &s) {return s[0]=="P";}
bool RecoveryInvOrder(const std::vector<string> &s) {return s[0]=="O";}
bool RecoveryManualScope(const RecoverySessionLifecycle &,const RecoverySessionContext &,
 const RecoverySessionIntentScope &,const RecoveryInventoryScope &) {return true;}
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b) {
 return a.generation==b.generation && a.magic==b.magic;
}
bool RecoveryRangeOwnerBound(const decltype(RecoveryManualHandoff::ordinary) &,
 const decltype(RecoveryManualHandoff::backing) &,const RecoverySessionLifecycle &,
 const RecoverySessionContext &,const RecoverySessionIntentScope &) {return true;}
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope &,const RRAction &,
 double,double,RecoveryIntentKey &) {return true;}
const int RI_TICKET=3;
int RecoveryIntentLookup(const RecoveryIntentKey &,string &ticket) {ticket="51";return RI_TICKET;}
bool RecoveryManualPendingInventoryOK(const RecoveryInventorySnapshot &i,ulong floor) {
 return i.usable && i.complete && i.sequence>0 && (ulong)i.sequence>floor &&
     ArraySize(i.rows)==i.position_count+i.order_count;
}
struct RecoveryManualPendingFill {
 int state;
 struct {ulong sequence,ticket,order_magic;int side;long entry_ticks,sl_ticks,tp_ticks,volume_steps;RecoveryInventoryScope request;} original;
 long target_sl,observed_sl,remaining;
 ulong terminal_state,verified_sequence;
 double tick,step;
 struct {ulong anchor_order,position_identifier; RecoveryInventoryScope binding;} history;
 RecoveryInventoryScope inventory;
 RRFilled proof;
};
//ACTUAL_SEAM
int main() {
 RecoveryManualPendingFill p{};
 p.state=RMPF_DONE;p.original={15,9007199254740999UL,0,1,400000,399900,400200,3,{11,"original",700}};
 p.target_sl=p.observed_sl=399900; p.terminal_state=ORDER_STATE_FILLED;p.tick=p.step=1.;
 p.history={p.original.ticket,9007199254740998UL,{11,"history",700}};p.inventory={11,"after",700};
 RecoveryHistorySnapshot h{true,true,"ok",21,1,2,{
   "H\t9007199254740999\t0\t4\t4\t100\t3\t0\t400000\t399900\t400200",
   "D\t101\t9007199254740999\t0\tBUY\t0\t0\t100\t2\t400002\t0\t0",
   "D\t102\t9007199254740999\t0\tBUY\t0\t0\t100\t1\t400008\t0\t0"}};
 RecoveryInventorySnapshot i{true,true,"ok",22,1,0,{
   "P\t9007199254740998\t9007199254740997\t0\tBUY\t3\t400004\t399950\t400200"}};
 assert(RecoveryManualPendingFillFold(p,11,h,i,p.proof));
 RRSettings cfg{};cfg.scope=7;cfg.generation=11;cfg.initial_sl_ticks=80;cfg.support_second_ticks=40;
 std::fill_n(cfg.lot_steps,24,1L);
 RRRangeState s{};RROpeningPair pair{};RRAlternationLedger ledger{};
 assert(RecoveryManualPendingOpening(p,cfg,s,pair,ledger));
 assert(!pair.sl_required && pair.reverse.entry_ticks==399924 && pair.reverse.sl_ticks==400004);
 assert(s.live_sl==399950 && s.live_tp==400200 && s.initial_sl==399900);
 assert(ledger.positions[0].confirmed_initial_sl==0);
 assert(!RecoveryManualPendingOpening(p,cfg,s,pair,ledger));
 auto bad=i;bad.rows[0].replace(bad.rows[0].find("400004"),6,"400005");
 assert(!RecoveryManualPendingFillFold(p,11,h,bad,p.proof));
 bad=i;bad.sequence=h.sequence;assert(!RecoveryManualPendingFillFold(p,11,h,bad,p.proof));
 bad=i;bad.rows[0].replace(bad.rows[0].find("400200"),6,"400201");
 assert(!RecoveryManualPendingFillFold(p,11,h,bad,p.proof));
 auto wrong=h;wrong.rows[0].replace(wrong.rows[0].find("\t0\t4")+1,1,"1");
 assert(!RecoveryManualPendingFillFold(p,11,wrong,i,p.proof));
 p.original.sl_ticks=p.observed_sl=0;
 h.rows[0].replace(h.rows[0].find("399900"),6,"0");
 i.rows[0].replace(i.rows[0].find("399950"),6,"0");
 assert(RecoveryManualPendingFillFold(p,11,h,i,p.proof));
 s={};pair={};ledger={};
 assert(RecoveryManualPendingOpening(p,cfg,s,pair,ledger));
 assert(pair.sl_required && pair.sl.kind==RR_INITIAL_SL && pair.sl.sl_ticks==399924);
 assert(pair.sl.tp_ticks==400200 && pair.reverse.entry_ticks==399924 && pair.reverse.sl_ticks==400004);
 assert(s.live_sl==0 && ledger.positions[0].confirmed_initial_sl==0);
 p.verified_sequence=22;
 RecoveryManualHandoff adoption{};adoption.seeded=true;adoption.ordinary.next_exact_ticket=51;
 adoption.ordinary.revision=1;adoption.ordinary.opening=pair;adoption.ordinary.ledger=ledger;
 adoption.backing.ranges[0]=s;
 auto request=RecoveryInventoryScope{11,"verify-pair",700};
 RecoveryInventorySnapshot effects{true,true,"ok",23,1,1,{
   "P\t9007199254740998\t9007199254740997\t0\tBUY\t3\t400004\t399924\t400200",
   "O\t51\t700\t0\t5\t1\t1\t1\t399924\t400004\t0\t0"}};
 auto partial=effects;partial.rows[1].replace(partial.rows[1].find("399924"),6,"399923");
 assert(!RecoveryManualPendingConfirmPair(adoption,p,{},{},{},request,partial));
 assert(!RecoveryManualPendingConfirmPair(adoption,p,{},{},{},p.inventory,effects));
 assert(RecoveryManualPendingConfirmPair(adoption,p,{},{},{},request,effects));
 assert(adoption.backing.ranges[0].phase==RR_ACTIVE && adoption.backing.ranges[0].opposite.ticket==51);
 assert(adoption.ordinary.ledger.positions[0].confirmed_initial_sl==399924 && adoption.ordinary.revision==2);
 assert(!RecoveryManualPendingConfirmPair(adoption,p,{},{},{},request,effects));

 // BUY 4000.00 -> 4000.10, with no pending-side SL established.
 p.original.side=1;
 h.rows[1].replace(h.rows[1].find("400002"),6,"400010");
 h.rows[2].replace(h.rows[2].find("400008"),6,"400010");
 i.rows[0].replace(i.rows[0].find("400004"),6,"400010");
 assert(RecoveryManualPendingFillFold(p,11,h,i,p.proof));
 s={};pair={};ledger={};
 assert(RecoveryManualPendingOpening(p,cfg,s,pair,ledger));
 assert(pair.sl_required && pair.sl.sl_ticks==399930 && pair.reverse.side==-1 &&
        pair.reverse.entry_ticks==399930 && pair.reverse.sl_ticks==400010);
 assert(!RecoveryManualPendingOpening(p,cfg,s,pair,ledger));
 // SELL 4000.00 -> 3999.90, mirrored broker geometry.
 p.original.side=-1;p.target_sl=400080;
 h.rows[0].replace(h.rows[0].find("\t4\t4")+1,1,"5");
 for(int n=1;n<=2;n++) {
   h.rows[n].replace(h.rows[n].find("BUY"),3,"SELL");
   h.rows[n].replace(h.rows[n].find("400010"),6,"399990");
 }
 i.rows[0].replace(i.rows[0].find("BUY"),3,"SELL");
 i.rows[0].replace(i.rows[0].find("400010"),6,"399990");
 assert(RecoveryManualPendingFillFold(p,11,h,i,p.proof));
 s={};pair={};ledger={};
 assert(RecoveryManualPendingOpening(p,cfg,s,pair,ledger));
 assert(pair.sl_required && pair.sl.sl_ticks==400070 && pair.reverse.side==1 &&
        pair.reverse.entry_ticks==400070 && pair.reverse.sl_ticks==399990);
}
