from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_opening_dispatch_compat import CXX

EXTRA=r"""
struct RRSupportRotation {bool sl_required,sl_verified,move_required,move_verified;
  ulong position_id,position_ticket; RRAction sl,move;};
bool RRSupportRotationRequestState(const RRRangeState &r,const RRSupportRotation &rot,
                                  const RRAction &a,RRRangeState &out){
 out=r;return can_build && (a.kind==RR_INITIAL_SL ? !rot.sl_verified : !rot.move_verified);}
"""
TEST=r"""
int main(){
 RRRangeState r{{1,400001,2},399900,400080};
 RRSupportRotation rotation{true,false,true,false,99,88,
  {RR_INITIAL_SL,1,3,99,88,399930,0,400080},{7,-1,3,0,0,400000,399930,0}};
 RecoveryIntentKey sl{"sl",7,77,3,"XAUUSD.s","Demo"}, move=sl;move.id="move";
 RecoveryRotationDispatchResult result{};
 auto call=[&]{return RecoveryDispatchSupportRotation(r,rotation,sl,move,.01,.01,result);};
 ack=false;assert(call()&&sequence=="SR");
 assert(call()&&sequence=="SR");
 sent.clear();sequence="";move.id="sl";assert(!call()&&sequence.empty());
 move.id="move";fill_still=false;assert(!call()&&sequence.empty());
 fill_still=true;rotation.sl_verified=true;sl.login=0;
 assert(call()&&sequence=="R");
 rotation.move_verified=true;assert(!call());
}
"""
def mql_code(path):
    return "\n".join(line for line in path.read_text().splitlines() if not line.startswith("#include"))
class TestSupportRotationDispatch(unittest.TestCase):
    def test_move_is_not_replaced_or_blocked_by_sl_ack(self):
        self.assertIsNotNone(shutil.which("g++"))
        here=Path(__file__).resolve().parent
        source=mql_code(here/"RecoveryOpeningDispatch.mqh")+mql_code(here/"RecoverySupportRotationDispatch.mqh")
        self.assertNotRegex(source,r"TRADE_ACTION_REMOVE|TRADE_ACTION_PENDING|RR_CANCEL")
        with tempfile.TemporaryDirectory() as d:
            cpp=Path(d)/"rotation.cpp"; exe=cpp.with_suffix("")
            cpp.write_text(CXX+EXTRA+"\n"+mql_code(here/"RecoveryOpeningDispatch.mqh")+"\n"+
                           mql_code(here/"RecoverySupportRotationDispatch.mqh")+"\n"+TEST)
            subprocess.run(["g++","-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":
    unittest.main()
