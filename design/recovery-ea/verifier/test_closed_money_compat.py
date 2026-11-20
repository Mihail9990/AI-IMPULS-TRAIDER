"""Host checks for exact exit economics; no MetaTrader or broker connection."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
SHIM=r"""
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>
using string=std::string;
using ulong=std::uint64_t;
template<class T> int ArraySize(const std::vector<T>& x){return static_cast<int>(x.size());}
template<class T> int ArrayResize(std::vector<T>& x,int n){ x.resize(n);return n;}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
enum {TERMINAL_CONNECTED,ACCOUNT_LOGIN,ACCOUNT_SERVER,ACCOUNT_CURRENCY,DEAL_TICKET,
      DEAL_POSITION_ID,DEAL_ORDER,DEAL_ENTRY,DEAL_TYPE,DEAL_SYMBOL,DEAL_VOLUME,
      DEAL_PROFIT,DEAL_COMMISSION,DEAL_SWAP,DEAL_FEE,DEAL_ENTRY_IN,DEAL_ENTRY_OUT,
      DEAL_TYPE_BUY,DEAL_TYPE_SELL};
constexpr ulong Pos=9007199254740997ULL, First=9007199254741123ULL, Second=9007199254741130ULL;
struct Terminal {
 long login=99;
 string server="Demo",currency="USD",symbol="XAUUSD.s";
 ulong selected=0,missing=0;
 int count=0;
 bool wrong_position=false, wrong_order=false, wrong_volume=false, swap_entry=false;
 bool mutate_profit=false, switch_account=false, reject_fee=false;
};
Terminal mock;
long TerminalInfoInteger(int){return 1;}
long AccountInfoInteger(int){return mock.login;}
string AccountInfoString(int k){return k==ACCOUNT_SERVER?mock.server:mock.currency;}
bool HistoryDealSelect(ulong ticket){
 if(ticket==mock.missing) return false;
 mock.selected=ticket; ++mock.count;
 if(mock.switch_account && mock.count==3) mock.login++;
 return ticket==First || ticket==Second;
}
bool HistoryDealGetInteger(ulong ticket,int k,long& n) {
 assert(ticket==mock.selected);
 switch(k) {
   case DEAL_TICKET:n=ticket;return true;
   case DEAL_POSITION_ID:n=mock.wrong_position?Pos+1:Pos;return true;
   case DEAL_ORDER:n=mock.wrong_order?78:77;return true;
   case DEAL_ENTRY:n=mock.swap_entry?DEAL_ENTRY_IN:DEAL_ENTRY_OUT;return true;
   case DEAL_TYPE:n=DEAL_TYPE_SELL;return true;
 }
 return false;
}
bool HistoryDealGetString(ulong ticket,int,string& x){
 assert(ticket==mock.selected);x=mock.symbol;return true;
}
bool HistoryDealGetDouble(ulong ticket,int k,double& x) {
 assert(ticket==mock.selected);
 switch(k) {
   case DEAL_VOLUME:x=mock.wrong_volume?0.01:ticket==First?0.02:0.03;return true;
   case DEAL_PROFIT:x=ticket==First?-3.12:-2.34; if(mock.mutate_profit && mock.count>2)x=-1;return true;
   case DEAL_COMMISSION:x=ticket==First?-0.12:-0.23;return true;
   case DEAL_SWAP:x=ticket==First?0.03:0.08;return true;
   case DEAL_FEE:x=ticket==First?-0.04:-0.06;return !mock.reject_fee;
 }
 return false;
}
bool near(double a,double b){return std::abs(a-b)<0.000000001;}
"""
MAIN=r"""
int main(){
 RecoveryCloseMoneyScope scope{99,"Demo","XAUUSD.s","USD",Pos,1,5,0.01};
 std::vector<RecoveryCloseMoneyExit> exits{{First,Pos,77,-1,2},{Second,Pos,77,-1,3}};
 RecoveryCloseMoneyTotal out;
 assert(RecoveryReadClosedMoney(scope, exits, out));
 assert(out.observed && out.closed_steps==5 && out.deal_count==2 && mock.count==4);
 assert(near(out.profit,-5.46) && near(out.commission,-.35) && near(out.swap,.11) &&
        near(out.fee,-.10) && near(out.net,-5.80) && near(out.loss,5.80));

 std::vector<RecoveryCloseMoneyRow> costs{{exits[1],-2.34,-.23,.08,-.06},{exits[0],-3.12,-.12,.03,-.04}};
 assert(RecoveryCloseMoneyAggregate(scope,exits,costs,out) && near(out.loss,5.80));
 costs[0].profit=8;
 assert(RecoveryCloseMoneyAggregate(scope,exits,costs,out) && out.loss==0 && out.net>0);
 costs[0].key=exits[0];
 assert(!RecoveryCloseMoneyAggregate(scope,exits,costs,out) && !out.observed && out.net==0);
 costs[0].key=exits[1];costs[0].key.position_identifier++;
 assert(!RecoveryCloseMoneyAggregate(scope,exits,costs,out));
 costs[0].key=exits[1];costs[0].fee=std::numeric_limits<double>::infinity();
 assert(!RecoveryCloseMoneyAggregate(scope,exits,costs,out));
 auto bad=exits;bad[1].ticket=bad[0].ticket;
 assert(!RecoveryReadClosedMoney(scope,bad,out));
 bad=exits;bad.pop_back();
 assert(!RecoveryReadClosedMoney(scope,bad,out));
 bad=exits;bad[0].exit_side=1;
 assert(!RecoveryReadClosedMoney(scope,bad,out));
 mock={};mock.missing=Second;assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.wrong_position=true;assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.wrong_order=true;assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.wrong_volume=true;assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.swap_entry=true;assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.mutate_profit=true;assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.switch_account=true;assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.currency="EUR";assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.symbol="other";assert(!RecoveryReadClosedMoney(scope,exits,out));
 mock={};mock.reject_fee=true;assert(!RecoveryReadClosedMoney(scope,exits,out));
 assert(!out.observed && out.net==0 && out.loss==0);
}
"""


class ClosedMoneyCompat(unittest.TestCase):
    def test_exact_closed_economics(self):
        compiler=shutil.which("c++")
        self.assertIsNotNone(compiler)
        src=(HERE/"RecoveryClosedMoney.mqh").read_text()
        self.assertNotRegex(src,r"\b(?:OrderSend(?:Async)?|TRADE_ACTION_\w+|CTrade|RRConfirm|RCApply\w*)\b")
        for typename, argument in (("RecoveryCloseMoneyExit","exits"),("RecoveryCloseMoneyRow","rows")):
            src=src.replace(f"const {typename} &{argument}[]", f"const std::vector<{typename}> &{argument}")
        src=src.replace("RecoveryCloseMoneyRow first[];", "std::vector<RecoveryCloseMoneyRow> first;")
        with tempfile.TemporaryDirectory() as tmp:
            cpp=Path(tmp)/"money.cpp"; binary=cpp.with_suffix("")
            cpp.write_text(SHIM+src+MAIN)
            subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(binary)],check=True)
            subprocess.run([str(binary)],check=True)


if __name__=="__main__":
    unittest.main()
