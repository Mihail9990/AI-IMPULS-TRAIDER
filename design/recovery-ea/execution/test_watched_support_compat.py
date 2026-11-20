"""Host test of the watcher-to-controller adapter; no terminal/network."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

MOCK=r"""
#include <cassert>
#include <cmath>
#include <string>
#include <cstdint>
using string=std::string;
using ulong=std::uint64_t;
#include "RecoveryCycle.mqh"
enum {RR_ACTIVE=5,RR_NONE=0};
struct RRAction{int kind;};
struct RRSettings{long support_first_ticks,support_second_ticks,support_offset_ticks;};
struct RRRangeState{RCRange cycle;int phase,support_stage;RRAction pending; ulong position_ticket;long live_sl;};
struct RecoveryPriceScope{ulong position_identifier,live_ticket,generation;int side;string actual_entry,first_advance,second_advance;};
struct RecoveryPriceSignal{bool usable,observed;long sequence;int stage;string selected_quote;};
RecoveryPriceSignal response{true,true,2,1,"4000.25"};
void RRClearAction(RRAction &a){a.kind=RR_NONE;}
bool RRValid(const RRRangeState &,const RRSettings &){return true;}
bool RecoveryAdoptionUnits(const string&s,double g,bool,long &n){double x=std::stod(s);
    n=std::lround(x/g); return std::abs(n*g-x)<1e-9;}
bool RecoveryPriceParse(const string& s,const RecoveryPriceScope&,long after,RecoveryPriceSignal &out){
    out=response;return s=="exact" && response.sequence>after;}
bool RecoveryPriceRead(const RecoveryPriceScope&s,long n,RecoveryPriceSignal &out){return RecoveryPriceParse("exact",s,n,out);}
bool RecoveryPricePeakRead(const RecoveryPriceScope &key,ulong after,int desired,RecoveryPriceSignal &out){
    return RecoveryPriceParse("exact",key,(long)after,out) && desired<=out.stage;
}
bool RRStage(RRRangeState &s,const RRSettings&,long bid,long ask,RRAction &out){
    assert(bid==ask);out.kind=4;s.pending=out;return true;}
"""
CASES=r"""
int main(){
    RRSettings cfg{25,40,25};
    RRRangeState s{};s.phase=RR_ACTIVE;s.position_ticket=70;s.live_sl=399920;
    s.cycle.side=1;s.cycle.position_id=65;s.cycle.generation=3;s.cycle.range_id=1;
    s.cycle.active_order=75;s.cycle.actual_entry_ticks=400000;
    RecoveryPriceScope key{65,70,3,1,"4000.00",".25",".40"};
    RRAction action{};
    auto got=[&]{return RecoveryStageWatchedSupport("exact",s,cfg,key,1,.01,action);};
    assert(got() && action.kind==4);
    assert(!got() && action.kind==0);
    s.pending.kind=0;response.selected_quote="4000.40";response.stage=1;
    assert(!got());response.stage=2;assert(got());
    s.pending.kind=0;key.second_advance=".50";assert(!got());key.second_advance=".40";
    key.position_identifier=999;assert(!got());key.position_identifier=65;
    key.live_ticket=71;assert(!got());key.live_ticket=70;
    key.generation=4;assert(!got());key.generation=3;
    assert(!RecoveryStageWatchedSupport("wrong",s,cfg,key,1,.01,action));
    assert(!RecoveryStageWatchedSupport("exact",s,cfg,key,2,.01,action));
    s.cycle.side=key.side=-1;s.live_sl=400080;
    response.selected_quote="3999.60";response.stage=2;
    assert(RecoveryReadWatchedSupport(s,cfg,key,1,.01,action));
    s.pending.kind=0;s.support_stage=1;
    assert(RecoveryReadWatchedPeak(s,cfg,key,1,.01,action));
    s.pending.kind=0;s.support_stage=2;
    assert(!RecoveryReadWatchedPeak(s,cfg,key,1,.01,action));
}
"""
class WatchedSupportTest(unittest.TestCase):
    def test_scope_and_actual_config(self):
        self.assertIsNotNone(shutil.which("g++"))
        here=Path(__file__).resolve().parent
        mql=here/"RecoveryWatchedSupport.mqh"
        body="\n".join(l for l in mql.read_text().splitlines() if not l.startswith("#include"))
        self.assertNotRegex(body,r"OrderSend|CTrade")
        with tempfile.TemporaryDirectory() as folder:
            cpp=Path(folder)/"watched.cpp"; exe=cpp.with_suffix("")
            cpp.write_text(MOCK+body+"\n"+CASES)
            subprocess.run(["g++","-I",str(here.parent/"cycle"),"-std=c++17","-Wall","-Wextra","-Werror",
                            "-fsanitize=undefined",str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":
    unittest.main()
