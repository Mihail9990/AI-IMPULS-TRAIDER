"""Simulate OnInit/OnTimer orchestration with a fake terminal; not MetaEditor."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PREAMBLE = r"""
#include <cassert>
#include <string>
using string = std::string;
struct RecoveryInventoryScope {
    string nonce, executor_epoch, verifier_epoch;
    long generation, login;
    string server, symbol;
    long magic, verifier_chart;
};
struct RecoveryInventorySnapshot {bool usable, complete; string reason; int position_count, order_count; };
enum {TERMINAL_CONNECTED, ACCOUNT_LOGIN, ACCOUNT_SERVER, ACCOUNT_TRADE_MODE,
      ACCOUNT_TRADE_MODE_DEMO, MQL_TESTER, INIT_FAILED, INIT_PARAMETERS_INCORRECT, INIT_SUCCEEDED};
string _Symbol="TEST", test_server="TESTSERVER";
long test_login=10, test_chart=21;
bool connected=true, demo=true, tester=false, exists=false, timer_ok=true, timer_on=false;
bool request_ok=true, read_ok=false, usable=true, complete=true;
int requests=0, reads=0;
long TerminalInfoInteger(int) { return connected; }
long AccountInfoInteger(int type) { return type==ACCOUNT_LOGIN?test_login:(demo?ACCOUNT_TRADE_MODE_DEMO:0); }
string AccountInfoString(int) { return test_server; }
long MQLInfoInteger(int) { return tester; }
long ChartID() { return test_chart; }
bool EventSetTimer(int seconds) { assert(seconds==1); return timer_on=timer_ok; }
void EventKillTimer() { timer_on=false; }
template<class... T> void Print(T...) {}
template<class... T> void PrintFormat(T...) {}
bool FileIsExist(const string &) { return exists; }
bool RecoveryInvScopeOK(const RecoveryInventoryScope &s) { return s.login>0 && s.verifier_chart>0; }
bool RecoveryRequestInventory(const string &nonce,const string &,const string &,long,long,const string &,
                              const string &,long) {
    assert(nonce=="test-nonce");
    ++requests;
    return request_ok;
}
bool RecoveryInvRead(const RecoveryInventoryScope &s,RecoveryInventorySnapshot &out) {
    assert(s.verifier_chart==22 && s.nonce=="test-nonce");
    ++reads;
    out.usable=usable; out.complete=complete;
    return read_ok;
}
"""
SCENARIOS = r"""
int main() {
    ExpectedLogin=10; ExpectedServer="TESTSERVER"; ExpectedSymbol="TEST"; StrategyMagic=4;
    VerifierChart=22; ProbeNonce="test-nonce";
    demo=false; assert(OnInit()==INIT_FAILED && !timer_on);
    demo=true; tester=true; assert(OnInit()==INIT_FAILED && !timer_on);
    tester=false; VerifierChart=21; assert(OnInit()==INIT_PARAMETERS_INCORRECT && !timer_on);
    VerifierChart=22; timer_ok=false; assert(OnInit()==INIT_FAILED && !timer_on);
    timer_ok=true; assert(OnInit()==INIT_SUCCEEDED && timer_on);
    connected=false; OnTimer(); assert(requests==0);
    connected=true; exists=true; OnTimer(); assert(!timer_on && ProbeFinished && requests==0);
    exists=false; assert(OnInit()==INIT_SUCCEEDED && timer_on);
    request_ok=false; OnTimer(); assert(requests==1 && !ProbePublished);
    request_ok=true; OnTimer(); assert(requests==2 && ProbePublished && reads==0);
    OnTimer(); assert(reads==1 && !ProbeFinished && requests==2);
    test_login=11; OnTimer(); assert(reads==1);
    test_login=10; read_ok=true; usable=false; OnTimer(); assert(reads==2 && !ProbeFinished);
    usable=true; OnTimer(); assert(reads==3 && ProbeFinished && !timer_on);
    OnTimer(); assert(reads==3 && requests==2);
    OnDeinit(0);
    assert(OnInit()==INIT_SUCCEEDED && timer_on);
    OnTimer(); complete=false; OnTimer(); assert(ProbeFinished && !timer_on);
}
"""


class PairProbeTest(unittest.TestCase):
    def test_orchestration(self):
        gxx = shutil.which("g++")
        self.assertIsNotNone(gxx, "g++ required")
        source = Path(__file__).with_name("RecoveryPairProbe.mq5").read_text()
        self.assertNotRegex(source, r"OrderSend|TRADE_ACTION|CTrade")
        source = "\n".join(line.removeprefix("input ") for line in source.splitlines()
                           if not line.startswith(("#property", "#include")))
        with tempfile.TemporaryDirectory() as directory:
            cpp = Path(directory) / "probe.cpp"
            app = cpp.with_suffix("")
            cpp.write_text(PREAMBLE + source + SCENARIOS)
            subprocess.run([gxx, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", str(cpp), "-o", str(app)], check=True)
            subprocess.run([str(app)], check=True)


if __name__ == "__main__":
    unittest.main()
