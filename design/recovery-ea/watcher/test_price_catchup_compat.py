"""Offline bounded tick-history fold check, not a MetaEditor compilation."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
MOCK=r"""
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <vector>
using ulong=std::uint64_t;
struct MqlTick {long time_msc;double bid,ask;};
int ArraySize(const std::vector<MqlTick> &v){return static_cast<int>(v.size());}
bool MathIsValidNumber(double x){return std::isfinite(x);}
double MathMin(double a,double b){return std::min(a,b);}
"""
MAIN=r"""
int main(){
  RecoveryPricePeak p;RecoveryPriceReset(p);
  long anchor;ulong cursor;
  assert(!RecoveryPriceAnchor(0,100,anchor,cursor));
  assert(RecoveryPriceAnchor(101,100,anchor,cursor) && anchor==101 && cursor==102);
  assert(RecoveryPriceAnchor(90,100,anchor,cursor) && anchor==100 && cursor==101);
  assert(!RecoveryPriceTickRecord(100,anchor,102));
  assert(RecoveryPriceTickRecord(101,anchor,102));
  assert(!RecoveryPriceTickRecord(103,anchor,102));

  // The trading EA sees only a retrace; the earlier greater price must survive.
  const std::vector<MqlTick> buy={{101,4000.41,4000.51},{102,4000.27,4000.37}};
  assert(RecoveryPriceStage(1,4000,4000.27,4000.37,.25,.40)==1);
  assert(RecoveryPriceCatchupFold(buy,2,true,1,4000,.25,.40,anchor,102,cursor,p));
  assert(RecoveryPriceReached(p,2) && p.selected==4000.41 && cursor==102);
  const std::vector<MqlTick> same_msc={{102,4000.27,4000.37},{102,4000.42,4000.52},{103,4000.28,4000.38}};
  assert(RecoveryPriceCatchupFold(same_msc,3,true,1,4000,.25,.40,anchor,103,cursor,p));
  assert(p.stage==2 && p.selected==4000.42 && cursor==103);

  RecoveryPriceReset(p); cursor=101;
  const std::vector<MqlTick> sell={{101,3999.49,3999.59},{102,3999.63,3999.73}};
  assert(RecoveryPriceStage(-1,4000,3999.63,3999.73,.25,.40)==1);
  assert(RecoveryPriceCatchupFold(sell,2,true,-1,4000,.25,.40,anchor,102,cursor,p));
  assert(RecoveryPriceReached(p,2) && p.selected==3999.59 && cursor==102);

  RecoveryPriceReset(p); cursor=101;
  const std::vector<MqlTick> caps(RECOVERY_PRICE_TICK_CAP,{101,4000.10,4000.20});
  assert(RecoveryPriceCatchupFold(caps,RECOVERY_PRICE_TICK_CAP,true,1,4000,.25,.40,anchor,110,cursor,p));
  assert(p.stage==0 && cursor==101);
  assert(!RecoveryPriceCatchupFold(caps,RECOVERY_PRICE_TICK_CAP+1,true,1,4000,.25,.40,anchor,110,cursor,p));
  assert(RecoveryPriceCatchupFold(buy,2,false,1,4000,.25,.40,anchor,102,cursor,p));
  assert(p.stage==2 && cursor==101); // partial sync retains fact but cannot advance
  assert(!RecoveryPriceCatchupFold({},0,true,1,4000,.25,.40,anchor,110,cursor,p));
  assert(!RecoveryPriceCatchupFold({{100,4010,4011}},1,true,1,4000,.25,.40,anchor,110,cursor,p));
  assert(!RecoveryPriceCatchupFold({{102,4010,4011},{101,4010,4011}},2,true,1,4000,.25,.40,anchor,110,cursor,p));
  assert(cursor==101 && p.selected==4000.41);
  RecoveryPriceReset(p);
  assert(RecoveryPriceCatchupFold(buy,2,true,1,4000,.25,.40,anchor,101,cursor,p));
  assert(cursor==101 && p.selected==4000.41); // no use of later rows before upper bracket
}
"""


class PriceCatchupCompat(unittest.TestCase):
    def test_chart_batch_and_cursor(self):
        compiler=shutil.which("c++")
        self.assertIsNotNone(compiler)
        stage=(HERE/"RecoveryPriceStage.mqh").read_text()
        catch=(HERE/"RecoveryPriceCatchup.mqh").read_text()
        catch=catch.replace('#include "RecoveryPriceStage.mqh"',"")
        catch=catch.replace("const MqlTick &ticks[]","const std::vector<MqlTick> &ticks")
        with tempfile.TemporaryDirectory() as d:
            cpp=Path(d)/"catch.cpp"; out=cpp.with_suffix("")
            cpp.write_text(MOCK+stage+catch+MAIN)
            subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(out)],check=True)
            subprocess.run([str(out)],check=True)

    def test_only_observer_has_history_fetch(self):
        source=(HERE/"RecoveryPriceWatcher.mq5").read_text()
        self.assertEqual(source.count("=CopyTicks("),1)
        self.assertIn("!PriceActivePosition(i,entry)",source)
        self.assertIn("PriceActivePosition(i,check) && check==entry",source)
        self.assertIn("tick.time_msc<=Subs[i].anchor",source)
        self.assertIn("CopyTicks(ExpectedSymbol,ticks,COPY_TICKS_ALL,Subs[i].cursor,",source)
        self.assertNotRegex(source,r"\b(?:OrderSend(?:Async)?|CTrade|TRADE_ACTION_\w+)\b")


if __name__=="__main__":
    unittest.main()
