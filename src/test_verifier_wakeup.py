"""Run the verifier's event, scheduling and priority-scan code against a synthetic clock and files."""
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "mql/RecoveryPythonVerifier.mq5").read_text(encoding="utf-8")
HOST = r"""
#include <cassert>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <vector>
using string=std::string;
using ulong=unsigned long;
using Strings=std::vector<string>;
int StringLen(const string& s){return s.size();}
int StringSplit(const string& s,int sep,Strings& out){
  std::size_t start=0,end;
  do{end=s.find(char(sep),start);out.push_back(s.substr(start,end-start));start=end+1;}while(end!=string::npos);
  return out.size();
}
bool PyNat(const string& s,ulong& n){if(s.empty()||s.find_first_not_of("0123456789")!=string::npos)return false;n=std::stoul(s);return std::to_string(n)==s;}
string Box="fixture/",Instance="current",ExpectedSymbol="TEST";
ulong clock_ms=10000;
ulong GetTickCount64(){return clock_ms;}
std::map<string,string> files;
std::set<string> handled;
Strings visited;
int inventories=0,pulses=0,history_scans=0,order_scans=0;
bool session=true,publish_ok=true,inventory_changed=true,pulse_ok=true;
bool PySession(){return session;}
bool PyRead(const string& f,string& data,int){if(!files.count(f))return false;data=files[f];return true;}
bool HistoryRequest(const string& p,bool){visited.push_back(p);return handled.insert(p).second;}
bool OrderRequest(const string& p){visited.push_back(p);return handled.insert(p).second;}
bool HistoryScan(bool){++history_scans;return false;}
bool OrderScan(){++order_scans;return false;}
void EndHistoryScan(){} void EndOrderScan(){}
bool PublishInventory(bool& changed){inventories++;if(!publish_ok)return false;changed=inventory_changed;inventory_changed=false;return true;}
bool SignalWake(){if(!pulse_ok)return false;pulses++;return true;}
bool PyInit(const string&){return true;}bool EventSetMillisecondTimer(int t){assert(t==125);return true;}void PyClose(){}
enum {INIT_FAILED=-1,INIT_SUCCEEDED=0,TRADE_TRANSACTION_REQUEST=1};
struct MqlTradeTransaction{string symbol;int type;};struct MqlTradeRequest{string symbol;};struct MqlTradeResult{};
"""
MAIN = r"""
int main(){
  assert(OnInit()==INIT_SUCCEEDED && inventories==1 && pulses==1);
  // A burst retains a transaction that arrives before Python has requested either proof.
  OnTradeTransaction({"TEST",2},{""},{});
  clock_ms+=125;OnTimer();assert(pulses==2);const int last=pulses;
  clock_ms+=125;OnTimer();assert(pulses==last);
  files[Box+"history-priority.tsv"]="current\t10250\t7\t8\t9\t10\t11\t12\tEND\n";
  files[Box+"order-priority.tsv"]="current\t10250\t70\tEND\n";
  clock_ms+=125;OnTimer();assert(handled.size()==5 && pulses==last+1);
  clock_ms+=125;OnTimer();assert(handled.size()==7 && pulses==last+2);
  for(ulong t=clock_ms+125;t<15500;t+=125){clock_ms=t;OnTimer();}
  assert(pulses==last+2);
  // A sleeping timer does not rebuild inventory or walk disk on every 125-ms call.
  const int prior=inventories, scans=history_scans;
  clock_ms=16000;OnTimer();clock_ms+=125;OnTimer();clock_ms+=125;OnTimer();
  assert(inventories-prior<=1 && history_scans-scans<=1);
  // Unrelated transactions have no effect. A symbol-less broker transaction does wake.
  const int old=pulses;
  OnTradeTransaction({"OTHER",2},{"TEST"},{});OnTradeTransaction({"",TRADE_TRANSACTION_REQUEST},{"OTHER"},{});
  clock_ms+=125;OnTimer();assert(pulses==old);
  OnTradeTransaction({"",2},{""},{});OnTradeTransaction({"",TRADE_TRANSACTION_REQUEST},{"TEST"},{});
  publish_ok=false;clock_ms+=125;OnTimer();assert(pulses==old && WakePending);
  publish_ok=true;pulse_ok=false;clock_ms+=125;OnTimer();assert(pulses==old && WakePending);
  pulse_ok=true;clock_ms+=125;OnTimer();assert(pulses==old+1 && !WakePending);
  // An expired/wrong-incarnation index falls back to the bounded legacy scan.
  files[Box+"history-priority.tsv"]="previous\t16300\t66\tEND\n";
  files[Box+"order-priority.tsv"]="current\t1\t77\tEND\n";
  const int before=history_scans,orders_before=order_scans;clock_ms+=125;OnTimer();
  assert(history_scans==before+1 && order_scans==orders_before+1);
  assert(!handled.count("history-66.req")&&!handled.count("order-77.req"));
  std::cout<<"Verifier startup, coalescing, retained proofs, priority fairness, scope, publication retry and idle fallback passed\n";
}
"""


def main():
    priority = SOURCE[SOURCE.index("bool PriorityScan("):SOURCE.index("// This file is solely")]
    priority = priority.replace("string data,fields[];", "string data;Strings fields;")
    scheduler = SOURCE[SOURCE.index("ulong BurstUntil="):SOURCE.index("void OnDeinit(")]
    with tempfile.TemporaryDirectory(prefix="recovery-verifier-events-") as tmp:
        source = Path(tmp) / "check.cpp"
        exe = Path(tmp) / "check.exe"
        source.write_text(HOST + priority + scheduler + MAIN, encoding="utf-8")
        subprocess.run([os.environ.get("CXX","c++"), "-std=c++17", "-fsanitize=undefined",
                        str(source), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
