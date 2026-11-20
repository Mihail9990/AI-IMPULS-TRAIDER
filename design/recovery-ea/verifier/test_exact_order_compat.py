"""Mock broker getters and exercise the exact-ticket MQL observation logic."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).with_name("RecoveryExactOrder.mqh")
SHIM = r"""
#include <cassert>
#include <cmath>
#include <limits>
#include <string>
using string=std::string;
using ulong=unsigned long;
enum ENUM_ORDER_PROPERTY_INTEGER { ORDER_TICKET,ORDER_MAGIC,ORDER_POSITION_ID,ORDER_TYPE,ORDER_STATE };
enum ENUM_ORDER_PROPERTY_DOUBLE { ORDER_VOLUME_INITIAL,ORDER_VOLUME_CURRENT,ORDER_PRICE_OPEN,ORDER_SL,ORDER_TP,ORDER_PRICE_STOPLIMIT };
enum { TERMINAL_CONNECTED,ACCOUNT_LOGIN,ACCOUNT_SERVER,ORDER_SYMBOL };
struct MockOrder {
  bool current=true, history=false, wrong_ticket=false, bad_field=false;
  bool current_to_history=false, disappear=false, changed=false, switch_account=false;
  long login=5, magic=700, pos=0, type=5, state=1;
  string server="Demo", symbol="XAUUSD.s";
  double remaining=0.01;
  int selections=0, hist_selections=0;
};
MockOrder mock;
constexpr ulong known=9007199254740997UL;
long TerminalInfoInteger(int) {return 1;}
long AccountInfoInteger(int) {return mock.login;}
string AccountInfoString(int) {return mock.server;}
bool MathIsValidNumber(double x) {return std::isfinite(x);}
bool OrderSelect(ulong ticket) {
  assert(ticket==known);
  ++mock.selections;
  if(mock.selections==2) {
    if(mock.current_to_history) {mock.current=false; mock.history=true;}
    if(mock.disappear) {mock.current=false; mock.history=false;}
    if(mock.changed) {mock.remaining=0.0;}
    if(mock.switch_account) {mock.login=6;}
  }
  return mock.current;
}
bool HistoryOrderSelect(ulong ticket) {
  assert(ticket==known);
  ++mock.hist_selections;
  return mock.history;
}
bool OrderGetInteger(ENUM_ORDER_PROPERTY_INTEGER k,long& x) {
  switch(k) {
    case ORDER_TICKET: x=mock.wrong_ticket ? known+1 : known; return true;
    case ORDER_MAGIC: x=mock.magic; return true;
    case ORDER_POSITION_ID: x=mock.pos; return true;
    case ORDER_TYPE: x=mock.type; return true;
    case ORDER_STATE: x=mock.state; return true;
  }
  return false;
}
bool OrderGetDouble(ENUM_ORDER_PROPERTY_DOUBLE k,double& x) {
  switch(k) {
    case ORDER_VOLUME_INITIAL: x=0.02; return true;
    case ORDER_VOLUME_CURRENT: x=mock.remaining; return true;
    case ORDER_PRICE_OPEN: x=2999.55; return true;
    case ORDER_SL: x=2999.85; return !mock.bad_field;
    case ORDER_TP: x=0.0; return true;
    case ORDER_PRICE_STOPLIMIT: x=0.0; return true;
  }
  return false;
}
bool OrderGetString(int,string& x) {x=mock.symbol;return true;}
bool HistoryOrderGetInteger(ulong t,ENUM_ORDER_PROPERTY_INTEGER k,long& x) {
  assert(t==known);return OrderGetInteger(k,x);
}
bool HistoryOrderGetDouble(ulong t,ENUM_ORDER_PROPERTY_DOUBLE k,double& x) {
  assert(t==known);return OrderGetDouble(k,x);
}
bool HistoryOrderGetString(ulong t,int k,string& x) {
  assert(t==known);return OrderGetString(k,x);
}
"""
CASES = r"""
int main() {
  RecoveryExactOrderScope scope{5,"Demo","XAUUSD.s",700,known};
  RecoveryExactOrderObservation obs;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_OBSERVED);
  assert(obs.source==RECOVERY_EXACT_ORDER_CURRENT && obs.ticket==known &&
         obs.position_identifier==0 && obs.type==5 && obs.remaining_volume==0.01);
  assert(mock.selections==2 && mock.hist_selections==0);

  mock={}; mock.current=false; mock.history=true; mock.state=2;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_OBSERVED);
  assert(obs.source==RECOVERY_EXACT_ORDER_HISTORY && obs.position_identifier==0 && obs.state==2);
  assert(mock.hist_selections==2);
  mock={}; mock.pos=9007199254741100UL;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_OBSERVED);
  assert(obs.position_identifier==9007199254741100UL);

  mock={}; mock.current=false;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_UNKNOWN);
  assert(obs.source==RECOVERY_EXACT_ORDER_NONE && obs.ticket==0);
  mock={}; mock.wrong_ticket=true; mock.history=true;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_MISMATCH && mock.hist_selections==0);
  mock={}; mock.magic=701;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_MISMATCH);
  mock={}; mock.symbol="other";
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_MISMATCH);
  mock={}; mock.server="other";
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_SESSION && mock.selections==0);
  mock={}; mock.switch_account=true;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_SESSION);
  mock={}; mock.current_to_history=true;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_CHANGED);
  mock={}; mock.disappear=true;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_CHANGED);
  mock={}; mock.changed=true;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_CHANGED);
  mock={}; mock.bad_field=true;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_READ_ERROR);
  mock={}; mock.remaining=0.03;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_READ_ERROR);
  mock={}; mock.remaining=std::numeric_limits<double>::quiet_NaN();
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_READ_ERROR);
  mock={}; scope.ticket=0;
  assert(RecoveryObserveExactOrder(scope,obs)==RECOVERY_EXACT_ORDER_INVALID_SCOPE && mock.selections==0);
}
"""


class ExactOrderCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_broker_read_simulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "check.cpp"
            exe = Path(tmp) / "check"
            source.write_text(SHIM + SOURCE.read_text() + CASES)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            str(source), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)

    def test_no_trade_write_time_or_wire_changes(self):
        source = SOURCE.read_text()
        self.assertNotRegex(source, re.compile(
            r"\b(?:OrderSend(?:Async)?|CTrade|TRADE_ACTION_\w+|PositionClose\w*|OrderDelete|Time(?:Local|Current|TradeServer)|FileOpen)\b"
        ))


if __name__ == "__main__":
    unittest.main()
