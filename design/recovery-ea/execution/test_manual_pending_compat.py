"""Actual manual STOP owner and inventory parser with an inert broker double."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))
sys.path.insert(0,str(ROOT/"verifier"))
from test_reader_compat import SHIM,as_cpp

HARNESS=r"""
#include <openssl/sha.h>
#include <cmath>
#include <cstdio>
using ulong=std::uint64_t;
using datetime=long;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathRound(double n){return std::round(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
template<class T> void ZeroMemory(T &out){out=T{};}
enum {ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,ORDER_STATE_PLACED=1,ORDER_STATE_PARTIAL=3,ORDER_STATE_FILLED=4};
enum {ORDER_REASON_CLIENT=0,ORDER_REASON_MOBILE,ORDER_REASON_WEB,ORDER_REASON_EXPERT};
enum ENUM_ORDER_TYPE_TIME {ORDER_TIME_GTC=0,ORDER_TIME_DAY,ORDER_TIME_SPECIFIED,ORDER_TIME_SPECIFIED_DAY};
enum {ORDER_TICKET,ORDER_MAGIC,ORDER_POSITION_ID,ORDER_TYPE,ORDER_STATE,ORDER_REASON,
      ORDER_TYPE_TIME,ORDER_TIME_EXPIRATION,ORDER_SYMBOL,ORDER_VOLUME_INITIAL,ORDER_VOLUME_CURRENT,
      ORDER_PRICE_OPEN,ORDER_SL,ORDER_TP,ORDER_PRICE_STOPLIMIT};
enum {TRADE_ACTION_MODIFY=7,TRADE_RETCODE_DONE=10009};
struct RecoverySessionContext {long chart,login,magic;string server,symbol;};
struct RecoverySessionLifecycle {string epoch;bool active;};
struct RecoverySessionIntentScope {string executor_epoch;long login,magic;string server,symbol;ulong scope,generation;};
struct RRSettings {ulong scope,generation;long initial_sl_ticks;};
struct RRAction {
 ulong scope,generation,serial,order_ticket;
 int range_id,kind,side;
 long entry_ticks,sl_ticks,tp_ticks,volume_steps;
};
void RRClearAction(RRAction &order){order={};}
bool RRSettingsValid(const RRSettings &cfg){return cfg.scope>0&&cfg.generation>0&&cfg.initial_sl_ticks>0;}
bool RCBoundedPositive(long t){return t>0&&t<=1000000000000;}
bool RCValidSide(int d){return d==1||d==-1;}
bool RecoveryPrimarySession(const RecoverySessionLifecycle &s,const RecoverySessionContext &l,
 const RecoverySessionIntentScope &c){
 return s.active&&s.epoch==c.executor_epoch&&l.login==c.login&&l.magic==c.magic&&
    l.symbol==c.symbol&&l.server==c.server;
}
struct RecoveryIntentKey {string id,fingerprint,symbol;long login{12};string server{"Test"};};
bool RecoveryIntentKeyOK(const RecoveryIntentKey& k){return RecoveryInvHex(k.id)&&k.fingerprint.size()==64;}
bool live_ok=true;
bool RecoveryIntentContextOK(const RecoveryIntentKey& k){return live_ok&&RecoveryIntentKeyOK(k)&&k.symbol=="XAUUSD.s";}
bool RecoveryIntentKeysMatch(const RecoveryIntentKey&a,const RecoveryIntentKey&b){
 return a.id==b.id&&a.fingerprint==b.fingerprint&&a.symbol==b.symbol;
}
string RecoverySessionSha256(const string &s){
 unsigned char bytes[32];char out[65];
 SHA256(reinterpret_cast<const unsigned char*>(s.data()),s.size(),bytes);
 for(int i=0;i<32;i++)std::snprintf(out+i*2,3,"%02x",bytes[i]);
 return out;
}
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope &c,const RRAction&a,double,double,
 RecoveryIntentKey &out){
 out.symbol=c.symbol;
 const auto id=c.executor_epoch+std::to_string(a.scope)+std::to_string(a.generation)+std::to_string(a.serial);
 out.id=RecoverySessionSha256(id).substr(0,32);
 out.fingerprint=RecoverySessionSha256(id+std::to_string(a.order_ticket)+std::to_string(a.sl_ticks));
 return true;
}
enum {RI_NO_RECORD,RI_UNRESOLVED,RI_CONFLICT};
RecoveryIntentKey reserved{};
int RecoveryIntentLookup(const RecoveryIntentKey& key,string &unused) {
 unused="";
 if(reserved.id.empty())return RI_NO_RECORD;
 return RecoveryIntentKeysMatch(reserved,key)?RI_UNRESOLVED:RI_CONFLICT;
}
bool RecoveryIntentReserve(const RecoveryIntentKey &key){
 if(!reserved.id.empty())return false;
 reserved=key;return true;
}
bool RecoveryDispatchUnits(double v,double unit,bool allow_zero,long expected){
 return (allow_zero || v>0)&&std::abs(v/unit-expected)<1.e-7;
}
enum {RD_NOT_SENT,RD_EXISTING_INTENT,RD_RESERVED_NOT_SENT,RD_OUTCOME_UNRESOLVED};
struct RecoveryDispatchResult {int state;unsigned check_retcode,send_retcode;};
struct MqlTradeRequest {
 int action;ulong order;string symbol;double price,sl,tp,volume;ENUM_ORDER_TYPE_TIME type_time;datetime expiration;
};
struct MqlTradeCheckResult {unsigned retcode;};
struct MqlTradeResult {unsigned retcode;};
struct TerminalOrder {
 ulong ticket;
 long magic,side,state,reason,time_type,expiration;
 double volume,remaining,entry,sl,tp;
};
TerminalOrder current{};
int selection_calls=0,checks=0,sends=0,mutation=0;
bool currently_pending=true,accepted=false,historical_fill=false;
MqlTradeRequest last{};
bool OrderSelect(ulong ticket){++selection_calls;return currently_pending&&ticket==current.ticket;}
bool OrderGetInteger(int field,long &out){
 switch(field) {
 case ORDER_TICKET: out=static_cast<long>(current.ticket);return true;
 case ORDER_MAGIC: out=current.magic;return true;
 case ORDER_POSITION_ID: out=0;return true;
 case ORDER_TYPE: out=current.side==1?ORDER_TYPE_BUY_STOP:ORDER_TYPE_SELL_STOP;return true;
 case ORDER_STATE: out=current.state;return true;
 case ORDER_REASON: out=current.reason;return true;
 case ORDER_TYPE_TIME: out=current.time_type;return true;
 case ORDER_TIME_EXPIRATION: out=current.expiration;return true;
 }
 return false;
}
bool OrderGetString(int field,string &out){if(field!=ORDER_SYMBOL)return false;out="XAUUSD.s";return true;}
bool OrderGetDouble(int field,double &out){
 switch(field) {
 case ORDER_VOLUME_INITIAL: out=current.volume;return true;
 case ORDER_VOLUME_CURRENT: out=current.remaining;return true;
 case ORDER_PRICE_OPEN: out=current.entry;return true;
 case ORDER_SL: out=current.sl;return true;
 case ORDER_TP: out=current.tp;return true;
 case ORDER_PRICE_STOPLIMIT: out=0;return true;
 }
 return false;
}
enum {RECOVERY_EXACT_ORDER_OBSERVED=1,RECOVERY_EXACT_ORDER_HISTORY=2};
struct RecoveryExactOrderScope {long login;string server,symbol;ulong magic,ticket;};
struct RecoveryExactOrderObservation {
 int source;ulong ticket,magic,position_identifier;string symbol;int state,type;
 double remaining_volume,stop_limit,initial_volume,price,sl,tp;
};
int RecoveryExactOrderSample(const RecoveryExactOrderScope &q,RecoveryExactOrderObservation &r){
 if(!historical_fill || q.ticket!=current.ticket || q.magic!=static_cast<ulong>(current.magic))return 0;
 r={RECOVERY_EXACT_ORDER_HISTORY,current.ticket,q.magic,1999,q.symbol,ORDER_STATE_FILLED,
     current.side==1?ORDER_TYPE_BUY_STOP:ORDER_TYPE_SELL_STOP,0,0,current.volume,current.entry,current.sl,current.tp};
 return RECOVERY_EXACT_ORDER_OBSERVED;
}
bool HistoryOrderGetInteger(ulong ticket,int field,long& value){return historical_fill&&ticket==current.ticket&&OrderGetInteger(field,value);}
bool OrderCheck(MqlTradeRequest &cmd,MqlTradeCheckResult &r){
 ++checks;assert(cmd.action==TRADE_ACTION_MODIFY);
 if(mutation==1)current.tp+=.01;
 if(mutation==2)currently_pending=false;
 if(mutation==3)current.expiration++;
 r.retcode=TRADE_RETCODE_DONE;return true;
}
bool OrderSend(MqlTradeRequest &cmd,MqlTradeResult &r){
 ++sends;last=cmd;
 r.retcode=accepted?TRADE_RETCODE_DONE:10012;
 return accepted;
}
"""

TEST=r"""
string fixed(long t){
 auto cents=std::to_string(t%100);
 return std::to_string(t/100)+"."+(cents.size()==1?"0":"")+cents+"00000000000000";
}
string inv(const RecoveryInventoryScope &q,int seq,int count,const string& orders) {
 return "RECOVERY_INVENTORY_V1\t"+q.nonce+"\t"+q.executor_epoch+"\t"+q.verifier_epoch+
   "\t7\t12\tTest\tXAUUSD.s\t700\t99\t"+std::to_string(seq)+"\tcomplete\tok\t0\t"+
   std::to_string(count)+"\n"+orders+"END\n";
}
string row(ulong t,int side,long stop,long take,int magic=0,int state=1,long rem=3) {
 return "O\t"+std::to_string(t)+"\t"+std::to_string(magic)+"\t0\t"+(side==1?"4":"5")+"\t"+
   std::to_string(state)+"\t"+fixed(3)+"\t"+fixed(rem)+"\t"+fixed(400005)+"\t"+
   fixed(stop)+"\t"+fixed(take)+"\t"+fixed(0)+"\n";
}
int main(){
 const string epoch(32,'2');
 const RecoverySessionContext live{11,12,700,"Test","XAUUSD.s"};
 RecoverySessionLifecycle session{epoch,true};
 const RecoverySessionIntentScope scope{epoch,12,700,"Test","XAUUSD.s",5,7};
 const RRSettings cfg{5,7,80};
 const RecoveryInventoryScope q{string(32,'1'),epoch,string(32,'3'),7,12,"Test","XAUUSD.s",700,99};
 auto q2=q;q2.nonce=string(32,'9');
 constexpr ulong ticket=9007199254740997ULL;
 for(int side:{-1,1}) {
    for(int scenario=0;scenario<9;scenario++) {
       const long existing=(scenario==0 || scenario==8)?400005-side*47:0;
       const long target=existing!=0?existing:400005-side*80;
       const long tp=400005+side*132;
       current={ticket,0,side,ORDER_STATE_PLACED,ORDER_REASON_MOBILE,ORDER_TIME_SPECIFIED,1741111111,
                   .03,.03,4000.05,existing*.01,tp*.01};
       reserved={};selection_calls=checks=sends=mutation=0;currently_pending=live_ok=true;historical_fill=false;
       accepted=(side>0);
       RecoveryManualPendingOwner owner{};
       RecoveryInventorySnapshot first{},later{};
       const auto original=row(ticket,side,existing,tp);
       auto prepare=[&](bool active,ulong select=0){
         return RecoveryPrepareManualPending(owner,active,session,live,scope,cfg,q,first,1,select,.01,.01);
       };
       auto attempt=[&](bool active){
         return RecoveryAttemptManualPendingSL(owner,active,session,live,scope);
       };
       auto confirm=[&](bool active,const RecoveryInventoryScope &req){
          return RecoveryConfirmManualPendingSL(owner,active,session,live,scope,req,later);
       };
       assert(RecoveryInvParse(inv(q,5,1,original),q,first));
       assert(!prepare(false)&&selection_calls==0&&sends==0);
       assert(RecoveryInvParse(inv(q,5,2,original+row(ticket+1,side,0,tp)),q,first));
       assert(!prepare(true)&&selection_calls==0&&sends==0); // no guessing
       if(scenario==6){
          current.reason=ORDER_REASON_EXPERT; // no magic does not make it a user order
          assert(!prepare(true,ticket)&&owner.phase==RMP_IDLE&&sends==0);
          continue;
       }
       if(scenario==5){
          assert(RecoveryInvParse(inv(q,5,1,row(ticket,side,existing,tp,0,3,2)),q,first));
          assert(!prepare(true)&&owner.phase==RMP_IDLE&&sends==0);
          continue;
       }
       if(scenario>=7) {
          assert(RecoveryInvParse(inv(q,5,1,original),q,first));
          currently_pending=false;historical_fill=true;
          current.tp+=.01;assert(!prepare(true)&&owner.phase==RMP_IDLE);
          current.tp=tp*.01;
          assert(prepare(true)&&owner.local_filled_before_prepare&&!attempt(true)&&sends==0&&checks==0);
          assert(owner.phase==(scenario==7?RMP_ATTEMPTED:RMP_READY));
          if(scenario==7)assert(owner.receipt.state==RD_NOT_SENT);
          continue;
       }
       // Explicit ticket may be provided by an authorized controller. In the
       // normal one-button path there is no ticket and only one is eligible.
       assert(prepare(true,ticket)&&owner.original.ticket==ticket&&!prepare(true,ticket));
       assert(owner.original.tp_ticks==tp&&owner.target_sl==target&&sends==0);
       if(scenario==0){
          assert(owner.phase==RMP_READY&&owner.confirmed_sequence==5&&!attempt(true)&&sends==0);
          continue;
       }
       assert(owner.phase==RMP_PREPARED&&!attempt(false)&&checks==0);
       if(scenario==2)mutation=1;
       if(scenario==3)mutation=2;
       if(scenario==4)mutation=3;
       const bool sent=attempt(true);
       assert(owner.phase==RMP_ATTEMPTED&&checks==1);
       assert(!attempt(true));
       if(scenario>=2) {
          assert(!sent&&sends==0&&owner.receipt.state==RD_RESERVED_NOT_SENT);
          continue;
       }
       assert(sent&&sends==1&&last.action==TRADE_ACTION_MODIFY&&last.order==ticket&&last.volume==0);
       assert(last.price==4000.05&&last.tp==current.tp&&std::abs(last.sl-target*.01)<1.e-9);
       assert(last.type_time==ORDER_TIME_SPECIFIED&&last.expiration==1741111111);
       assert(owner.receipt.state==RD_OUTCOME_UNRESOLVED&&owner.confirmed_sequence==0);
       assert(RecoveryInvParse(inv(q2,5,1,row(ticket,side,target,tp)),q2,later));
       assert(!confirm(true,q2)); // repeated old sequence
       assert(RecoveryInvParse(inv(q,6,1,row(ticket,side,target,tp)),q,later));
       assert(!confirm(true,q)); // same nonce
       assert(RecoveryInvParse(inv(q2,6,1,row(ticket,side,target,tp+1)),q2,later));
       assert(!confirm(true,q2)); // edited TP differs
       assert(RecoveryInvParse(inv(q2,6,1,row(ticket+1,side,target,tp)),q2,later));
       assert(!confirm(true,q2)); // different exact ticket
       assert(RecoveryInvParse(inv(q2,7,1,row(ticket,side,target,tp)),q2,later));
       assert(!confirm(false,q2)&&sends==1&&owner.phase==RMP_ATTEMPTED); // cancel only disarms
       assert(confirm(true,q2)&&owner.phase==RMP_READY&&owner.confirmed_sequence==7);
    }
 }
}
"""

class ManualPendingCompat(unittest.TestCase):
    def test_manual_pending_one_exact_modify_and_independent_confirmation(self):
        exe_path=ROOT/"execution"
        ver=ROOT/"verifier"
        adoption=(exe_path/"RecoveryAdoptionInventory.mqh").read_text()
        adoption=adoption[adoption.index("bool RecoveryAdoptionUnsigned"):adoption.index("bool RecoveryAdoptionBound")]
        alt=(exe_path/"RecoveryAlternatingEvidence.mqh").read_text()
        alt=alt[alt.index("bool RecoveryAlternatingSameSession"):alt.index("bool RecoveryAlternatingWanted")]
        body=(HERE/"RecoveryManualPending.mqh").read_text()
        body="\n".join(x for x in body.splitlines() if not x.startswith("#include"))
        body=body.replace("string fields[];","std::vector<string> fields;")
        body=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",body)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            cpp=root/"case.cpp";exe=root/"case"
            cpp.write_text(SHIM+as_cpp((ver/"RecoveryInventoryReader.mqh").read_text())+HARNESS+adoption+alt+body+TEST)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            "-fno-sanitize-recover=all",str(cpp),"-lcrypto","-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":unittest.main()
