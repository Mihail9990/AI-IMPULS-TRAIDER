"""Compile the actual MQL request mapping with a C++ type/enum shim."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

MOCK = r"""
#include <cassert>
#include <cstdint>
#include <cmath>
#include <limits>
#include <string>
using string=std::string;
using ulong=std::uint64_t;
enum {TRADE_ACTION_PENDING=1,TRADE_ACTION_SLTP,TRADE_ACTION_MODIFY,TRADE_ACTION_REMOVE,
      ORDER_TYPE_BUY_STOP,ORDER_TYPE_SELL_STOP,ORDER_FILLING_RETURN,ORDER_TIME_GTC};
struct MqlTradeRequest {
    int action{},type{},type_filling{},type_time{};
    ulong magic{},position{},order{};
    string symbol;
    double price{},sl{},tp{},volume{};
};
void ZeroMemory(MqlTradeRequest &r){ r=MqlTradeRequest{}; }
bool MathIsValidNumber(double x) { return std::isfinite(x); }
bool near(double a,double b) { return std::abs(a-b)<1e-9; }
"""
TESTS = r"""
int main(){
    RecoveryOrderSpec buy{RRQ_STOP_ENTRY,"XAUUSD.s",18,0,0,1,400000,399930,400080,2,.01,.01};
    MqlTradeRequest r{};
    assert(RecoveryOrderBuild(buy,r));
    assert(r.action==TRADE_ACTION_PENDING && r.type==ORDER_TYPE_BUY_STOP &&
           r.type_filling==ORDER_FILLING_RETURN && r.type_time==ORDER_TIME_GTC &&
           near(r.price,4000) && near(r.sl,3999.30) && near(r.tp,4000.8) && near(r.volume,.02));
    auto s=buy; s.side=-1; s.stop_ticks=400070; s.take_ticks=0;
    assert(RecoveryOrderBuild(s,r) && r.type==ORDER_TYPE_SELL_STOP && near(r.sl,4000.7) && r.tp==0);
    s=buy; s.stop_ticks=400070;
    assert(!RecoveryOrderBuild(s,r) && r.action==0);
    s=buy; s.take_ticks=s.entry_ticks;
    assert(!RecoveryOrderBuild(s,r));
    s=buy; s.volume_steps=0;
    assert(!RecoveryOrderBuild(s,r));
    s=buy; s.magic=0;
    assert(!RecoveryOrderBuild(s,r));
    s=buy; s.symbol="";
    assert(!RecoveryOrderBuild(s,r));
    s=buy; s.tick=1e308;
    assert(!RecoveryOrderBuild(s,r));
    s=buy; s.step=std::numeric_limits<double>::quiet_NaN();
    assert(!RecoveryOrderBuild(s,r));
    s=buy; s.entry_ticks=-1;
    assert(!RecoveryOrderBuild(s,r));
    s=buy; s.kind=RRQ_MOVE_STOP_ENTRY; s.order_ticket=UINT64_MAX; s.volume_steps=0;
    assert(RecoveryOrderBuild(s,r) && r.action==TRADE_ACTION_MODIFY &&
           r.order==UINT64_MAX && r.position==0 && r.volume==0 && near(r.tp,4000.8));
    s.volume_steps=2;
    assert(!RecoveryOrderBuild(s,r));
    s={RRQ_POSITION_SL,"XAUUSD.s",18,UINT64_MAX,0,0,0,400000,400080,0,.01,.01};
    assert(RecoveryOrderBuild(s,r) && r.action==TRADE_ACTION_SLTP &&
           r.position==UINT64_MAX && near(r.sl,4000) && near(r.tp,4000.8));
    s.position_ticket=0;
    assert(!RecoveryOrderBuild(s,r));
    s={RRQ_CANCEL_STOP_ENTRY,"XAUUSD.s",18,0,77,-1,0,0,0,0,.01,.01};
    assert(RecoveryOrderBuild(s,r) && r.action==TRADE_ACTION_REMOVE && r.order==77);
    s.take_ticks=1;
    assert(!RecoveryOrderBuild(s,r));
}
"""
class TestRequest(unittest.TestCase):
    def test_request_mapping(self):
        gxx=shutil.which("g++")
        self.assertIsNotNone(gxx)
        source=Path(__file__).with_name("RecoveryOrderRequest.mqh").read_text()
        self.assertNotRegex(source,r"OrderSend|PositionSelect|HistorySelect")
        with tempfile.TemporaryDirectory() as d:
            cpp=Path(d)/"request.cpp"
            exe=cpp.with_suffix("")
            cpp.write_text(MOCK+source+"\n"+TESTS)
            subprocess.run([gxx,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
