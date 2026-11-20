"""Host coverage for coordinator-to-MT5 request assembly (never sends orders)."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_order_request_compat import MOCK


class TestActionRequest(unittest.TestCase):
    def test_staged_requested_identity_and_target(self):
        gxx = shutil.which("g++")
        self.assertIsNotNone(gxx)
        here = Path(__file__).resolve().parent
        cycle = here.parent / "cycle"
        coordinator = (cycle / "RecoveryRangeCoordinator.mqh").read_text()
        structs = coordinator[coordinator.index("enum RRPhase"):coordinator.index("bool RRKnownEqual")]
        equality = coordinator[coordinator.index("bool RRActionEqual"):coordinator.index("void RRSetKnown")]
        build = "\n".join(line for name in ("RecoveryOrderRequest.mqh","RecoveryActionRequest.mqh")
                          for line in (here / name).read_text().splitlines() if not line.startswith("#include"))
        self.assertNotRegex(build, r"OrderSend|CTrade")
        test = r"""
int main(){
    RRRangeState range{};
    range.scope=9;range.cycle.generation=5;range.cycle.range_id=1;
    range.pending={9,5,1,1,RR_PRIMARY,1,0,0,0,0,400000,399930,0,2};
    auto incoming=range.pending;
    MqlTradeRequest request{};
    assert(RecoveryBuildStagedRequest(range,incoming,"XAUUSD.s",18,.01,.01,request));
    assert(request.action==TRADE_ACTION_PENDING && near(request.volume,.02));
    incoming.serial++;assert(!RecoveryBuildStagedRequest(range,incoming,"XAUUSD.s",18,.01,.01,request));
    incoming=range.pending;incoming.range_id=2;
    assert(!RecoveryBuildStagedRequest(range,incoming,"XAUUSD.s",18,.01,.01,request));
    range.cycle.position_id=753;range.position_ticket=55;range.cycle.active_order=29;
    range.cycle.side=1;range.live_tp=400080;
    range.pending={9,5,2,1,RR_SUPPORT_SL,1,2,29,753,55,0,400020,400080,0};
    assert(RecoveryBuildStagedRequest(range,range.pending,"XAUUSD.s",18,.01,.01,request));
    assert(request.action==TRADE_ACTION_SLTP && request.position==55 && request.order==0 && near(request.tp,4000.80));
    range.pending.tp_ticks=0;
    assert(!RecoveryBuildStagedRequest(range,range.pending,"XAUUSD.s",18,.01,.01,request));
    range.opposite={77,-1,399930,400000,2};
    range.pending={9,5,3,1,RR_CANCEL,-1,0,77,0,0,399930,400000,0,2};
    range.phase=RR_ACTIVE;
    assert(!RecoveryBuildStagedRequest(range,range.pending,"XAUUSD.s",18,.01,.01,request));
    range.phase=RR_DRAIN;
    assert(RecoveryBuildStagedRequest(range,range.pending,"XAUUSD.s",18,.01,.01,request));
    assert(request.action==TRADE_ACTION_REMOVE && request.order==77);
    range.pending.entry_ticks++;
    assert(!RecoveryBuildStagedRequest(range,range.pending,"XAUUSD.s",18,.01,.01,request));
}
"""
        with tempfile.TemporaryDirectory() as d:
            cpp = Path(d) / "action.cpp"
            exe = cpp.with_suffix("")
            cpp.write_text(MOCK+'\n#include "'+str(cycle/'RecoveryCycle.mqh')+'"\n'+
                           "template<class T> void ZeroMemory(T &x){x=T{};}\n"+structs+equality+build+test)
            subprocess.run([gxx,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
