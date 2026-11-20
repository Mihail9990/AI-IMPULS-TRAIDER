"""Offline threshold and native-reader logic checks, not MT5 compilation."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
VERIFIER = HERE.parent / "verifier"
sys.path.insert(0, str(VERIFIER))
from test_reader_compat import SHIM, as_cpp

EXTRA = r"""
#include <cmath>
#include <cstdio>
#include <limits>
using ulong=std::uint64_t;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathMin(double a,double b){return std::min(a,b);}
string DoubleToString(double n,int p){
    char buffer[100];
    std::snprintf(buffer,sizeof buffer,"%.*f",p,n);
    return buffer;
}
"""
MAIN=r"""
int main(){
  assert(RecoveryPriceStage( 1,3000,3000.25,3000.45,.25,.40)==1);
  assert(RecoveryPriceStage( 1,3000,3000.40,3000.55,.25,.40)==2);
  assert(RecoveryPriceStage( 1,3000,3000.24,3000.50,.25,.40)==0);
  assert(RecoveryPriceStage(-1,3000,2999.50,2999.75,.25,.40)==1);
  assert(RecoveryPriceStage(-1,3000,2999.40,2999.60,.25,.40)==2);
  assert(RecoveryPriceStage(-1,3000,2999.50,2999.76,.25,.40)==0);
  assert(RecoveryPriceStage( 1,3000,3000.40,3000.50,.35,.60)==1);
  assert(RecoveryPriceStage( 1,3000,3000.60,3000.60,.35,.60)==2);
  assert(RecoveryPriceStage( 1,3000,3000,3000,1e-13,.60)==0);
  assert(RecoveryPriceStage( 0,3000,3000.6,3000.6,.35,.60)==-1);
  assert(RecoveryPriceStage( 1,3000,3000.6,3000.4,.35,.60)==-1);
  assert(RecoveryPriceStage( 1,3000,3000.6,3000.6,.60,.60)==-1);
  assert(RecoveryPriceStage( 1,3000,3000.6,3000.6,0,.60)==-1);
  assert(RecoveryPriceStage( 1,3000,3000.6,3000.6,.35,std::numeric_limits<double>::quiet_NaN())==-1);

  RecoveryPriceScope s{string(32,'1'),string(32,'2'),string(32,'3'),7,6,42,"Demo","XAUUSD.s",
      9007199254740993ULL,772,1,DoubleToString(3000,16),DoubleToString(.35,16),DoubleToString(.60,16)};
  const string request_prefix="RECOVERY_PRICE_REQUEST_V1\t"+s.nonce+"\t"+s.executor_epoch+"\t"+s.watcher_epoch+
      "\t7\t6\tDemo\tXAUUSD.s\t9007199254740993\t772\tBUY\t";
  assert(RecoveryPriceRequestText(s)==request_prefix+s.actual_entry+"\t"+s.first_advance+"\t"+s.second_advance+"\t42\tEND\n");

  const string head="RECOVERY_PRICE_V1\t"+s.nonce+"\t"+s.executor_epoch+"\t"+s.watcher_epoch+
      "\t7\t6\tDemo\tXAUUSD.s\t42\t9\t9007199254740993\t772\tBUY\t"+
      s.actual_entry+"\t"+s.first_advance+"\t"+s.second_advance+"\t";
  const string quote=DoubleToString(3000.40,16);
  const string live=head+"observed\t1\t"+quote+"\tEND\n";
  RecoveryPriceSignal out;
  assert(RecoveryPriceParse(live,s,8,out) && out.usable && out.observed && out.stage==1 &&
         out.sequence==9 && out.selected_quote==quote);
  assert(!RecoveryPriceParse(live,s,9,out) && !out.usable && !out.observed && out.stage==-1);
  assert(!RecoveryPriceParse(head+"observed\t2\t"+quote+"\tEND\n",s,0,out));
  assert(!RecoveryPriceParse(live+"\n",s,0,out));
  assert(!RecoveryPriceParse(live.substr(0,live.size()-1),s,0,out));
  assert(RecoveryPriceParse(head+"unavailable\t-1\t0.0000000000000000\tEND\n",s,0,out) &&
         out.usable && !out.observed && out.selected_quote.empty());
  RecoveryPricePeak peak;
  RecoveryPriceReset(peak);
  assert(!RecoveryPriceReached(peak,1));
  assert(RecoveryPriceAccumulate(peak,1,0,3000.10) && !RecoveryPriceReached(peak,1));
  assert(RecoveryPriceAccumulate(peak,1,1,3000.35) && RecoveryPriceReached(peak,1));
  assert(!RecoveryPriceAccumulate(peak,1,0,3000.10) && peak.selected==3000.35);
  assert(RecoveryPriceAccumulate(peak,1,2,3000.60) && RecoveryPriceReached(peak,2));
  assert(!RecoveryPriceAccumulate(peak,1,1,3000.50) && peak.selected==3000.60);
  RecoveryPriceReset(peak);
  assert(RecoveryPriceAccumulate(peak,-1,2,2999.40) && RecoveryPriceReached(peak,2));
  assert(!RecoveryPriceAccumulate(peak,-1,2,2999.41) && peak.selected==2999.40);
  assert(RecoveryPriceAccumulate(peak,-1,2,2999.30) && peak.selected==2999.30);
  assert(!RecoveryPriceAccumulate(peak,-1,-1,2999.20) && peak.selected==2999.30);
  assert(RecoveryPriceFollowText(s,0).empty());
  assert(RecoveryPriceFollowText(s,2).find("\t2\t42\tEND\n")!=string::npos);

  string v2=head;
  v2.replace(0,string("RECOVERY_PRICE_V1").size(),"RECOVERY_PRICE_PEAK_V2");
  assert(RecoveryPricePeakParse(v2+"1\tobserved\t1\t"+quote+"\tEND\n",s,8,1,out));
  assert(!RecoveryPricePeakParse(v2+"1\tobserved\t1\t"+quote+"\tEND\n",s,8,2,out));
  assert(!RecoveryPricePeakParse(v2+"2\tobserved\t1\t"+quote+"\tEND\n",s,8,2,out));
  assert(!RecoveryPricePeakParse(v2+"2\tunavailable\t-1\t0.0000000000000000\tEND\n",s,8,2,out));
  assert(RecoveryPricePeakParse(v2+"2\tobserved\t2\t"+DoubleToString(3000.61,16)+"\tEND\n",s,8,2,out));
  assert(!RecoveryPricePeakParse(v2+"2\tobserved\t2\t"+DoubleToString(3000.61,16)+"\tEND\n",s,9,2,out));
  auto other=s;other.nonce[0]='9'; assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.watcher_epoch[0]='9'; assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.executor_epoch[0]='9'; assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.position_identifier++;assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.live_ticket++;assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.generation++;assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.watcher_chart++;assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.login++;assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.symbol="XAUUSD";assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.side=-1;assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.first_advance=DoubleToString(.40,16);assert(!RecoveryPriceParse(live,other,0,out));
  other=s;other.second_advance=other.first_advance;assert(RecoveryPriceRequestText(other).empty());
}
"""


class PriceWatcherCompat(unittest.TestCase):
    def test_thresholds_scope_and_no_replay(self):
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler)
        inv = as_cpp((VERIFIER/"RecoveryInventoryReader.mqh").read_text())
        stage = (HERE/"RecoveryPriceStage.mqh").read_text()
        reader = (HERE/"RecoveryPriceReader.mqh").read_text().split("bool RecoveryPricePublishLocal(",1)[0]+"\n#endif\n"
        reader = re.sub(r"^#include .*\n","",reader,flags=re.M)
        reader = re.sub(r"\(string\)(s\.\w+|desired_stage)",r"std::to_string(\1)",reader)
        reader = reader.replace("string f[];","std::vector<string> f;")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d)/"watch.cpp"
            binary = src.with_suffix("")
            src.write_text(SHIM+EXTRA+inv+stage+reader+MAIN)
            subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(src),"-o",str(binary)],check=True)
            subprocess.run([str(binary)],check=True)

    def test_observer_only_and_create_only(self):
        src=(HERE/"RecoveryPriceWatcher.mq5").read_text()
        self.assertNotRegex(src,r"\b(?:OrderSend(?:Async)?|CTrade|TRADE_ACTION_\w+|TimeCurrent|TimeLocal|FILE_REWRITE)\b")
        self.assertIn("POSITION_IDENTIFIER",src)
        self.assertIn("POSITION_PRICE_OPEN",src)
        self.assertIn("SymbolInfoTick(ExpectedSymbol,tick)",src)
        self.assertIn("FileMove(temp,0,prefix+suffix,0)",src)
        self.assertIn("void OnTick()",src)
        self.assertIn("RecoveryPriceAccumulate(Subs[i].peak",src)
        self.assertIn("PriceWrite(Subs[i].query,Subs[i].peak.stage,Subs[i].peak.selected)",src)
        self.assertIn('PriceLoad(Subs[i].query.nonce+".drop",drop,true,true)',src)


if __name__=="__main__":
    unittest.main()
