"""Test adjacent dispatch and uncertain-outcome behavior with fake transport."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

CXX = r"""
#include <cassert>
#include <cmath>
#include <cstdint>
#include <set>
#include <string>
using string=std::string;
using ulong=std::uint64_t;
enum {RR_NONE=0, RR_INITIAL_SL=2, RR_REVERSE=3, RI_NO_RECORD, RI_RESERVED,
      POSITION_IDENTIFIER,POSITION_TICKET,POSITION_TYPE,POSITION_TYPE_BUY,POSITION_TYPE_SELL,
      POSITION_SYMBOL,POSITION_PRICE_OPEN,POSITION_VOLUME};
enum RecoveryDispatchState {RD_NOT_SENT, RD_EXISTING_INTENT, RD_RESERVED_NOT_SENT,
                            RD_OUTCOME_UNRESOLVED, RD_TICKET_RECORDED};
struct RRAction {int kind,side;ulong generation,position_id,position_ticket;
     long sl_ticks,entry_ticks,tp_ticks;};
struct RRRangeState {struct {int side;long actual_entry_ticks,filled_steps;} cycle;long live_sl,live_tp;};
struct RROpeningPair {RRAction sl,reverse; ulong position_id,position_ticket;
     bool sl_verified, reverse_verified;};
struct RecoveryIntentKey {string id;long login,magic,generation;string symbol,server;};
struct RecoveryDispatchResult {bool synchronous_sl_ack;};
template<class T> void ZeroMemory(T &x){x=T{};}
std::set<string> sent;
string sequence;
bool ack=true, fill_still=true, can_build=true;
bool RROpeningRequestState(const RRRangeState &s,const RROpeningPair &p,const RRAction &a,RRRangeState &out){
    out=s;return can_build && (a.kind==RR_REVERSE?!p.reverse_verified:!p.sl_verified);}
bool RecoveryIntentContextOK(const RecoveryIntentKey &k){return k.login==7&&k.symbol=="XAUUSD.s";}
int RecoveryIntentLookup(const RecoveryIntentKey& k,string&){return sent.count(k.id)?RI_RESERVED:RI_NO_RECORD;}
bool PositionSelectByTicket(ulong x){return fill_still&&x==88;}
bool PositionGetInteger(int x,long &v){
    v=x==POSITION_IDENTIFIER?99:x==POSITION_TICKET?88:POSITION_TYPE_BUY;return true;}
bool PositionGetString(int,string &s){s="XAUUSD.s";return true;}
bool PositionGetDouble(int x,double &v){v=x==POSITION_VOLUME?.02:4000.01;return true;}
bool RecoveryDispatchUnits(double value,double unit,bool,long n){return std::abs(value-n*unit)<1e-8;}
RecoveryDispatchState RecoveryDispatchOnce(const RRRangeState&,const RRAction &a,const RecoveryIntentKey &k,
    double,double,RecoveryDispatchResult &out){
    out.synchronous_sl_ack=false;
    if(!sent.insert(k.id).second)return RD_EXISTING_INTENT;
    sequence+=a.kind==RR_INITIAL_SL?"S":"R";
    if(a.kind==RR_INITIAL_SL){out.synchronous_sl_ack=ack;return RD_OUTCOME_UNRESOLVED;}
    return RD_TICKET_RECORDED;
}
RecoveryDispatchState RecoveryDispatchOpeningSLAsync(const RRRangeState& r,const RRAction& a,
    const RecoveryIntentKey& k,double tick,double step,RecoveryDispatchResult& out){
    return RecoveryDispatchOnce(r,a,k,tick,step,out);}

"""
CHECKS = r"""
int main(){
 RRRangeState r{{1,400001,2},399900,400080};
 RROpeningPair p{{RR_INITIAL_SL,1,3,99,88,399930,0,400080},
                 {RR_REVERSE,-1,3,0,0,400000,399930,0},99,88,false,false};
 RecoveryIntentKey sl{"sl",7,77,3,"XAUUSD.s","Demo"}, reverse=sl;
 reverse.id="reverse";
 RecoveryPairSendResult out{};
 auto call=[&]{return RecoveryDispatchOpeningPair(r,p,sl,reverse,.01,.01,out);};
 assert(call()==RPS_AWAIT_VERIFIER && sequence=="SR");
 assert(call()==RPS_NOT_STARTED && sequence=="SR");
 sent.clear();sequence="";ack=false;
 assert(call()==RPS_AWAIT_VERIFIER && sequence=="SR"); // SL response not required.
 assert(call()==RPS_NOT_STARTED && sequence=="SR");
 sent.clear();sequence="";fill_still=false;
 assert(RecoveryContinueOpeningPair(r,p,reverse,.01,.01,out.reverse)==RPS_FILL_NOT_BOUND && sequence.empty());
 fill_still=true;
 assert(RecoveryContinueOpeningPair(r,p,reverse,.01,.01,out.reverse)==RPS_AWAIT_VERIFIER && sequence=="R");
 assert(RecoveryContinueOpeningPair(r,p,reverse,.01,.01,out.reverse)==RPS_REVERSE_NOT_SENT && sequence=="R");
 sent.clear();sequence="";p.sl.kind=RR_NONE;r.live_sl=399930;
 assert(call()==RPS_AWAIT_VERIFIER && sequence=="R");
 sent.clear();sequence="";fill_still=false;
 assert(call()==RPS_FILL_NOT_BOUND && sequence.empty());
 fill_still=true;p.sl.kind=RR_INITIAL_SL;p.sl_verified=false;
 p.sl.sl_ticks=399931;assert(call()==RPS_NOT_STARTED && sequence.empty());
 p.sl.sl_ticks=399930;reverse.id=sl.id;
 assert(call()==RPS_NOT_STARTED && sequence.empty());
 reverse.id="other";reverse.server="OTHER";
 assert(call()==RPS_NOT_STARTED && sequence.empty());
}
"""
class OpeningDispatchCompat(unittest.TestCase):
    def test_pair_sequence_and_followup(self):
        self.assertIsNotNone(shutil.which("g++"))
        body=Path(__file__).with_name("RecoveryOpeningDispatch.mqh").read_text()
        body="\n".join(l for l in body.splitlines() if not l.startswith("#include"))
        with tempfile.TemporaryDirectory() as folder:
            cpp=Path(folder)/"pair.cpp"
            exe=cpp.with_suffix("")
            cpp.write_text(CXX+body+"\n"+CHECKS)
            subprocess.run(["g++","-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":
    unittest.main()
