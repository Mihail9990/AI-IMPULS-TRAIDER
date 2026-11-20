"""Host integration of panel commands to the existing range coordinator."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))

WIRE_SHIM = r"""
#include <string>
#include <vector>
using string=std::string;
template<class T> void ZeroMemory(T& o){o=T{};}
template<class T> int ArraySize(const std::vector<T>& o){return o.size();}
template<class T> int ArrayResize(std::vector<T>& o,int size,int){o.resize(size);return size;}
template<class T> void ArrayFree(std::vector<T>& o){o.clear();}
int StringLen(const string& s){return s.size();}
int StringReplace(string& s,const string& from,const string& to){
 int count=0;size_t pos=0;
 while((pos=s.find(from,pos))!=string::npos){s.replace(pos,from.size(),to);pos+=to.size();++count;}
 return count;
}
bool RecoveryInvHex(const string& s){return s.size()==32&&s.find_first_not_of("abcdef0123456789")==string::npos;}
enum RecoveryPanelOperation {RPC_NONE,RPC_MANUAL_WAIT,RPC_MANUAL_CANCEL,RPC_CONFIGURE_RANGES};
struct RecoveryPanelRange{int range_id,side;long price_ticks;};
struct RecoveryPanelCommand{RecoveryPanelOperation operation;string command_id,content_sha256;int range_count;RecoveryPanelRange ranges[2];};
struct RecoveryPanelCommandScope{string scope_id,boot_id,context_id,generation_id;};
bool RecoveryDecodePanelCommand(const string& wire,const string& expected,const RecoveryPanelCommandScope&,double,
     RecoveryPanelCommand& out){
 out={};out.command_id=expected;out.content_sha256=wire;
 if(wire=="wait")out.operation=RPC_MANUAL_WAIT;
 else if(wire=="cancel")out.operation=RPC_MANUAL_CANCEL;
 else if(wire=="ranges"){out.operation=RPC_CONFIGURE_RANGES;out.range_count=1;out.ranges[0]={1,1,400000};}
 else return false;
 return true;
}
"""

class PanelControlCompat(unittest.TestCase):
    def test_panel_commands(self):
        cycle=ROOT/"cycle"
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            (d/"cycle").mkdir();(d/"execution").mkdir()
            for name in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                shutil.copy2(cycle/name,d/"cycle"/name)
            cp=(cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
            cp=cp.replace('#include "RecoveryCloseWire.mqh"','#include "RecoveryCycle.mqh"')
            cp=cp[:cp.index("bool RCCPStageFromWire")]+cp[cp.index("void RCCPClearPlan"):]
            e=(cycle/"RecoveryCloseEvidence.mqh").read_text()
            cp=cp.replace('#include "RecoveryCycle.mqh"','#include "RecoveryCycle.mqh"\n'+
                e[e.index("struct RCCloseEvidenceKey"):e.index("struct RCExitHistory")]+
                e[e.index("bool RCCloseKeysEqual"):e.index("bool RCExitSame")],1)
            (d/"cycle/RecoveryCloseCheckpoint.mqh").write_text(cp+"\n#endif\n")
            shutil.copy2(ROOT/"execution/RecoverySessionLifecycle.mqh",d/"execution/RecoverySessionLifecycle.mqh")
            shutil.copy2(HERE/"RecoveryPanelControl.mqh",d/"execution/RecoveryPanelControl.mqh")
            (d/"execution/RecoveryPanelCommandWire.mqh").write_text("enum RecoveryPanelOperation"+WIRE_SHIM.split("enum RecoveryPanelOperation",1)[1])
            apply=(HERE/"RecoveryPanelSessionApply.mqh").read_text().replace(
                "RecoveryPanelSeen seen[];", "std::vector<RecoveryPanelSeen> seen;")
            (d/"execution/RecoveryPanelSessionApply.mqh").write_text(apply)
            fixture=(cycle/"test_alternating_fill.cpp").read_text().split("static RRRangeState first(")[0]
            fixture=fixture.replace('"RecoveryAlternatingFill.mqh"','"../cycle/RecoveryAlternatingFill.mqh"')
            source=d/"execution/check.cpp"
            source.write_text(WIRE_SHIM.split("enum RecoveryPanelOperation")[0]+fixture+(HERE/"test_panel_control.cpp").read_text())
            exe=d/"check"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(source),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":
    unittest.main()
