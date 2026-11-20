"""Exercises the MQL lifecycle and diagnostic event wrapper using fake MT5 events."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
#include <cassert>
#include <string>
struct string : std::string {
 using std::string::string;
 string(std::string s) : std::string(s) {}
 string()=default;
 explicit string(long x): std::string(std::to_string(x)) {}
 explicit string(unsigned long x): std::string(std::to_string(x)) {}
};
using ulong=unsigned long;
const int ACCOUNT_LOGIN=1,ACCOUNT_SERVER=2,REASON_ACCOUNT=4,
          REASON_REMOVE=5,INIT_FAILED=6,INIT_SUCCEEDED=0;
static long chart_id=100,login=200;
static string server="Broker";
string _Symbol="XAUUSD.s";
bool timer_set=false,timer_allowed=true;
int timer_attempts=0;
long ChartID(){return chart_id;}
long AccountInfoInteger(int p){assert(p==ACCOUNT_LOGIN);return login;}
string AccountInfoString(int p){assert(p==ACCOUNT_SERVER);return server;}
bool EventSetTimer(int seconds){assert(seconds==1);++timer_attempts;return timer_set=timer_allowed;}
void EventKillTimer(){timer_set=false;}
long TimeLocal(){return 101010;}
ulong GetTickCount64(){return 200020;}
ulong GetMicrosecondCount(){return 300030;}
template<class... T> void PrintFormat(T&&...) {}
template<class... T> void Print(T&&...) {}

#include "probe/RecoverySessionLifecycleProbe.mqh"

int main() {
 RecoverySessionLifecycle life{};
 RecoverySessionContext context{100,200,1,"Broker","XAUUSD.s","config=abc"};
 assert(RecoverySessionInit(life,context,true,"epoch1")==RSL_NEW);
 assert(RecoverySessionCurrent(life,context));
 RecoverySessionReset(life);
 assert(!RecoverySessionCurrent(life,context)&&life.epoch=="");
 auto next=context;
 assert(RecoverySessionInit(life,next,true,"epoch2")==RSL_NEW&&life.epoch=="epoch2");
 assert(RecoverySessionInit(life,next,false,"epoch3")==RSL_INACTIVE);
 assert(!life.active&&life.epoch=="");
 assert(RecoverySessionInit(life,next,true,"")==RSL_INACTIVE);
 // Changes in any scope field never authorize the old context.
 for(int i=0;i<6;i++){
   RecoverySessionReset(life);
   assert(RecoverySessionInit(life,context,true,"old")==RSL_NEW);
   next=context;
   if(i==0)next.chart++;
   if(i==1)next.login++;
   if(i==2)next.magic++;
   if(i==3)next.server="Other Broker";
   if(i==4)next.symbol="EURUSD";
   if(i==5)next.binding="config=def";
   assert(!RecoverySessionCurrent(life,next));
   RecoverySessionReset(life);
   assert(RecoverySessionInit(life,next,true,"new")==RSL_NEW&&life.epoch=="new");
 }
 next=context;next.login=0;
 assert(RecoverySessionInit(life,next,true,"invalid")==RSL_INACTIVE);

 // Test actual wrapper body, compiled without the MT5 property declarations.
 assert(OnInit()==INIT_SUCCEEDED&&timer_set&&SeenProof==0);
 const string initial=Session.epoch;
 OnTimer();OnTimer();assert(SeenProof==42&&Transitions==1);
 OnDeinit(REASON_REMOVE);assert(!timer_set&&SeenProof==0);
 _Symbol="EURUSD";
 assert(OnInit()==INIT_SUCCEEDED&&Session.epoch!=initial&&SeenProof==0);
 const string second=Session.epoch;
 OnTimer();assert(SeenProof==42);
 OnDeinit(REASON_REMOVE);_Symbol="XAUUSD.s";
 assert(OnInit()==INIT_SUCCEEDED&&Session.epoch!=initial&&Session.epoch!=second&&SeenProof==0);
 OnTimer();
 OnDeinit(REASON_ACCOUNT);assert(!timer_set&&SeenProof==0);
 ++login;
 assert(OnInit()==INIT_SUCCEEDED&&timer_set&&Transitions==0);
 OnTimer();
 OnDeinit(REASON_REMOVE);timer_allowed=false;
 assert(OnInit()==INIT_FAILED&&!Session.active&&!timer_set&&SeenProof==0);
 timer_allowed=true;
 assert(OnInit()==INIT_SUCCEEDED&&timer_set&&SeenProof==0);
 const auto tries=timer_attempts;
 OnDeinit(REASON_REMOVE);
 RequiredSymbol="EURUSD";
 assert(OnInit()==INIT_FAILED&&!Session.active&&!timer_set&&timer_attempts==tries);
 RequiredSymbol="XAUUSD.s";
 assert(OnInit()==INIT_SUCCEEDED&&timer_set);
 OnTimer(); ++login;
 OnTimer();assert(!Session.active&&!timer_set&&Transitions==0);
}
"""

class SessionLifecycleCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_session_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "execution").mkdir()
            (stage / "probe").mkdir()
            helper = ROOT / "execution/RecoverySessionLifecycle.mqh"
            shutil.copy2(helper, stage / "execution" / helper.name)
            wrapper = (ROOT / "probe/RecoverySessionLifecycleProbe.mq5").read_text()
            lines = (line.removeprefix("input ") for line in wrapper.splitlines()
                     if not line.startswith("#property"))
            (stage / "probe/RecoverySessionLifecycleProbe.mqh").write_text("\n".join(lines))
            test = stage / "test.cpp"
            test.write_text(HARNESS)
            exe = stage / "test"
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-pedantic", "-fsanitize=undefined", str(test), "-o", str(exe)],
                           check=True)
            subprocess.run([str(exe)], check=True)

if __name__=="__main__":
    unittest.main()
