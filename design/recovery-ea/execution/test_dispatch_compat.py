"""Run actual gateway logic against terminal and persistence doubles only."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

MOCK = r"""
#include <cassert>
#include <cstdint>
#include <cmath>
#include <string>
using string=std::string;
using ulong=std::uint64_t;
using uint=unsigned;
enum {RR_INITIAL_SL=1,RR_SUPPORT_SL,RR_CANCEL,RR_PRIMARY,RR_REVERSE,RR_REENTRY,RR_MOVE_SUPPORT_OPPOSITE,
      POSITION_IDENTIFIER,POSITION_TICKET,POSITION_TYPE,POSITION_TYPE_BUY,POSITION_TYPE_SELL,
      POSITION_SYMBOL,POSITION_SL,POSITION_TP,ORDER_STATE_PLACED,ORDER_TYPE_BUY_STOP,
      ORDER_TYPE_SELL_STOP,TRADE_ACTION_PENDING,TRADE_ACTION_SLTP,TRADE_ACTION_REMOVE,TRADE_ACTION_MODIFY,
      TRADE_RETCODE_DONE,TRADE_RETCODE_DONE_PARTIAL,TRADE_RETCODE_PLACED,
      TRADE_RETCODE_REJECT,TRADE_RETCODE_INVALID_PRICE,TRADE_RETCODE_INVALID_STOPS,TRADE_RETCODE_FROZEN};
struct RRAction {int kind,side;ulong position_ticket,position_id,order_ticket,generation;long
                 tp_ticks,entry_ticks,sl_ticks,volume_steps;};
struct RRKnownOrder {ulong ticket{}; int side{}; long entry_ticks{},sl_ticks{},volume_steps{};};
struct RRRangeState {long live_sl; RRKnownOrder retire{};};
struct RecoveryIntentKey {long login,magic,generation; string server,symbol;};
enum {RI_NO_RECORD,RI_UNRESOLVED,RI_TICKET,RI_UNAVAILABLE};
struct MqlTradeRequest {int action;};
struct MqlTradeCheckResult {uint retcode;};
struct MqlTradeResult {uint retcode;ulong order;};
enum {RECOVERY_EXACT_ORDER_OBSERVED,RECOVERY_EXACT_ORDER_CURRENT};
struct RecoveryExactOrderScope {long login;string server,symbol;ulong magic,ticket;};
struct RecoveryExactOrderObservation {int source,state,type;ulong position_identifier;double
   tp,stop_limit,price,sl,initial_volume,remaining_volume;};
template<class T> void ZeroMemory(T& a){a=T{};}
bool context=true,preflight=true,can_reserve=true,reserved=false,can_record=true,acked=true;
bool own_order=true,valid_build=true,mutate_after_reserve=false;
int send_calls=0,async_calls=0,check_calls=0,change_sl=0,check_code=0;
uint send_code=TRADE_RETCODE_PLACED;
ulong broker_ticket=9007199254740993ULL,recorded=0;
bool RecoveryIntentContextOK(const RecoveryIntentKey&){return context;}
int RecoveryIntentLookup(const RecoveryIntentKey&,string&){return reserved?RI_UNRESOLVED:RI_NO_RECORD;}
bool RecoveryIntentReserve(const RecoveryIntentKey&){if(!can_reserve||reserved)return false;
    reserved=true;if(mutate_after_reserve)change_sl=1;return true;}
bool RecoveryIntentRecordTicket(const RecoveryIntentKey&,ulong ticket){
    if(can_record)recorded=ticket;
    return can_record;}
bool RecoveryBuildStagedRequest(const RRRangeState&,const RRAction& a,const string&,ulong,double,double,
    MqlTradeRequest &r){ r.action=a.kind==RR_SUPPORT_SL?TRADE_ACTION_SLTP:
        (a.kind==RR_CANCEL?TRADE_ACTION_REMOVE:a.kind==RR_MOVE_SUPPORT_OPPOSITE?TRADE_ACTION_MODIFY:TRADE_ACTION_PENDING);return valid_build; }
bool MathIsValidNumber(double x){return std::isfinite(x);}
string DoubleToString(double x,int){return std::to_string(x);}
bool RecoveryAdoptionUnits(const string& s,double step,bool allow_zero,long &n){
    double x=std::stod(s);n=std::lround(x/step);
    return (allow_zero||x>0)&&std::abs(x-n*step)<1e-8;
}
bool PositionSelectByTicket(ulong t){return t==55;}
bool PositionGetInteger(int prop,long &v){
    v=prop==POSITION_IDENTIFIER?444:(prop==POSITION_TICKET?55:POSITION_TYPE_BUY);return true;}
bool PositionGetString(int,string &s){s="XAUUSD.s";return true;}
bool PositionGetDouble(int prop,double &v){v=prop==POSITION_TP?4000.8:3999.3+change_sl;return true;}
int RecoveryObserveExactOrder(const RecoveryExactOrderScope&,RecoveryExactOrderObservation& o){
    o={RECOVERY_EXACT_ORDER_CURRENT,ORDER_STATE_PLACED,ORDER_TYPE_SELL_STOP,0,0,0,3999.3,4000,.02,
       own_order?.02:.01};return RECOVERY_EXACT_ORDER_OBSERVED;}
bool OrderCheck(MqlTradeRequest&,MqlTradeCheckResult &r){++check_calls;r.retcode=check_code;return preflight;}
bool OrderSend(MqlTradeRequest&,MqlTradeResult &r){
    assert(reserved);++send_calls;r.order=broker_ticket;r.retcode=send_code;return acked;}
bool OrderSendAsync(MqlTradeRequest&,MqlTradeResult &r){
    assert(reserved);++async_calls;r.retcode=send_code;return acked;}
void reset(){context=preflight=can_reserve=can_record=acked=own_order=valid_build=true;
    reserved=mutate_after_reserve=false;send_calls=async_calls=check_calls=change_sl=check_code=0;
    send_code=TRADE_RETCODE_PLACED;broker_ticket=9007199254740993ULL;recorded=0;}
"""
TEST = r"""
int main(){
    RecoveryIntentKey k{12,45,8,"Test-Server","XAUUSD.s"};
    RRRangeState s{399930};
    RRAction a{RR_PRIMARY,1,0,0,0,8,0,400000,399930,2};
    RecoveryDispatchResult r{};
    auto call=[&]{return RecoveryDispatchOnce(s,a,k,.01,.01,r);};
    reset();assert(call()==RD_TICKET_RECORDED && r.order==9007199254740993ULL);
    assert(recorded==r.order && send_calls==1);
    assert(call()==RD_EXISTING_INTENT && send_calls==1 && r.order==0);
    reset();can_reserve=false;assert(call()==RD_EXISTING_INTENT && send_calls==0);
    reset();preflight=false;assert(call()==RD_NOT_SENT && !reserved && send_calls==0);
    reset();check_code=187;assert(call()==RD_NOT_SENT && !reserved && send_calls==0);
    reset();context=false;assert(call()==RD_NOT_SENT && send_calls==0);
    reset();valid_build=false;assert(call()==RD_NOT_SENT && check_calls==0);
    reset();acked=false;assert(call()==RD_OUTCOME_UNRESOLVED && recorded==0 && send_calls==1);
    assert(call()==RD_EXISTING_INTENT && send_calls==1);
    reset();can_record=false;assert(call()==RD_OUTCOME_UNRESOLVED && r.order==0 && send_calls==1);
    reset();send_code=9999;assert(call()==RD_OUTCOME_UNRESOLVED && recorded==0);
    reset();a={RR_SUPPORT_SL,1,55,444,29,8,400080,0,399975,0};
    change_sl=1;assert(call()==RD_NOT_SENT && check_calls==0);
    reset();mutate_after_reserve=true;
    assert(call()==RD_RESERVED_NOT_SENT && reserved && send_calls==0);
    assert(call()==RD_EXISTING_INTENT && send_calls==0);
    reset();assert(call()==RD_OUTCOME_UNRESOLVED && send_calls==1 && recorded==0 && !r.synchronous_sl_ack);
    reset();send_code=TRADE_RETCODE_DONE;
    assert(call()==RD_OUTCOME_UNRESOLVED && r.synchronous_sl_ack && recorded==0);
    reset();send_code=TRADE_RETCODE_DONE;acked=false;
    assert(call()==RD_OUTCOME_UNRESOLVED && !r.synchronous_sl_ack);
    reset();preflight=false;
    assert(RecoveryDispatchOpeningSLAsync(s,a,k,.01,.01,r)==RD_OUTCOME_UNRESOLVED &&
           r.async_sl_submitted && check_calls==0 && send_calls==0 && async_calls==1);
    assert(RecoveryDispatchOpeningSLAsync(s,a,k,.01,.01,r)==RD_EXISTING_INTENT && async_calls==1);
    reset();acked=false;
    assert(RecoveryDispatchOpeningSLAsync(s,a,k,.01,.01,r)==RD_RESERVED_NOT_SENT &&
           !r.async_sl_submitted && check_calls==0 && async_calls==1);
    reset();a={RR_CANCEL,-1,0,0,77,8,0,399930,400000,2};
    own_order=false;assert(call()==RD_NOT_SENT && check_calls==0);
    reset();assert(call()==RD_OUTCOME_UNRESOLVED && send_calls==1);
    auto cancel_async=[&]{return RecoveryDispatchExactCancelAsync(s,a,k,.01,.01,r);};
    reset();preflight=false;
    assert(cancel_async()==RD_OUTCOME_UNRESOLVED && check_calls==0 && send_calls==0 &&
           async_calls==1 && !r.async_sl_submitted && r.order==0);
    assert(cancel_async()==RD_EXISTING_INTENT && async_calls==1);
    reset();own_order=false;
    assert(cancel_async()==RD_NOT_SENT && async_calls==0 && check_calls==0);
    reset();acked=false;
    assert(cancel_async()==RD_RESERVED_NOT_SENT && async_calls==1 && send_calls==0);
    reset();a={RR_PRIMARY,1,0,0,0,8,0,400000,399930,2};
    assert(cancel_async()==RD_NOT_SENT && !reserved && async_calls==0 && check_calls==0);
    reset();a={RR_MOVE_SUPPORT_OPPOSITE,-1,0,0,77,8,0,399950,400030,2};
    s.retire={77,-1,399930,400000,2};
    assert(call()==RD_OUTCOME_UNRESOLVED && send_calls==1);
    reset();send_code=TRADE_RETCODE_REJECT;
    assert(call()==RD_OUTCOME_UNRESOLVED && r.synchronous_move_rejected);
    reset();send_code=TRADE_RETCODE_REJECT;acked=false;
    assert(call()==RD_OUTCOME_UNRESOLVED && !r.synchronous_move_rejected);
    reset();send_code=9999;
    assert(call()==RD_OUTCOME_UNRESOLVED && !r.synchronous_move_rejected);
    reset();s.retire.entry_ticks=399931;
    assert(call()==RD_NOT_SENT && send_calls==0 && check_calls==0);
}
"""
class DispatchCompat(unittest.TestCase):
    def test_once_and_no_ack_as_execution(self):
        gxx=shutil.which("g++")
        self.assertIsNotNone(gxx)
        text=Path(__file__).with_name("RecoveryDispatch.mqh").read_text()
        self.assertEqual(text.count("OrderSend("),1)
        source="\n".join(line for line in text.splitlines() if not line.startswith("#include"))
        with tempfile.TemporaryDirectory() as d:
            cpp=Path(d)/"dispatch.cpp"
            exe=cpp.with_suffix("")
            cpp.write_text(MOCK+source+"\n"+TEST)
            subprocess.run([gxx,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
