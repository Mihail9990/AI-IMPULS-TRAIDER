"""Event loop glue against fake terminal I/O; underlying pump has separate tests."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
import importlib.util

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))

HINT=r"""
#include "RecoveryAlternatingPump.mqh"
#include <string>
enum RecoveryFillHintState { RFH_UNUSED,RFH_ARMED,RFH_QUEUED,RFH_USED,RFH_DISARMED };
struct RecoveryFillHint { RecoveryFillHintState state{}; ulong prior_position{}; RecoveryAlternatingEvidenceBinding binding; };
struct MqlTradeTransaction { ulong order; };
bool RecoveryFillHintContext(const RecoveryFillHint&,const RecoveryInventoryScope&){return true;}
bool RecoveryArmFillHint(RecoveryFillHint& h,const RRRangeState& r,const RROpeningPair&,
 const RecoveryAlternatingEvidenceBinding& b,const std::string&) {
 if(b.exact.order_ticket==0)return false;
 h.binding=b;h.state=RFH_ARMED;h.prior_position=r.cycle.position_id;return true;
}
bool RecoveryQueueFillHint(const MqlTradeTransaction& t,const RecoveryInventoryScope&,RecoveryFillHint& h) {
 if(t.order==0||h.state!=RFH_ARMED)return false;
 h.state=RFH_QUEUED;return true;
}
bool RecoveryStartHintPump(RecoveryFillHint& h,const RecoveryInventoryScope&,const RRRangeState&,
 const RROpeningPair&,RecoveryAlternatingPump& p) {
 if(h.state!=RFH_QUEUED||p.state!=RAP_UNUSED)return false;
 p.binding=h.binding;h.state=RFH_USED;p.state=RAP_RUNNING;return true;
}
"""
FALLBACK=r"""
#include "RecoveryAlternatingFillHint.mqh"
bool mock_fill=false;
bool RecoveryPollExactFillHint(RecoveryFillHint& h,const RecoveryInventoryScope&,
 const RRRangeState&,const RROpeningPair&) {
 if(!mock_fill||h.state!=RFH_ARMED)return false;
 h.state=RFH_QUEUED;return true;
}
"""
JOURNAL=r"""
const int RI_TICKET=2;
string known_ticket;
int RecoveryIntentLookup(const RecoveryIntentKey&,string& exact){exact=known_ticket;return RI_TICKET;}
"""
TEST=r"""
#include "RecoveryOrdinaryEventLoop.mqh"
int main(){
 const auto cfg=config();
 RecoveryAlternatingState state{};
 state.revision=1;state.range=first(cfg,1,1000);
 assert(RRStartAlternation(state.range,0,state.ledger));
 assert(RRStageOpeningPair(state.range,cfg,state.opening));
 const RecoverySessionContext context{100,12,18,"server","XAUUSD.s","config"};
 RecoverySessionLifecycle life{};
 const string boot=string(32,'a'),pub=string(32,'b');
 assert(RecoverySessionInit(life,context,true,boot)==RSL_NEW);
 RecoveryInventoryScope scope{boot,"server","XAUUSD.s",12,18,static_cast<long>(cfg.generation)};
 RecoveryAlternatingEvidenceBinding binding{scope,cfg.scope,.01,.01,{51}};
 RecoveryOrdinaryEventLoop loop{};
 RecoveryAlternatingOutbox outbox{};
 RecoverySessionAlternatingResult result{};
 assert(RecoveryOrdinaryOnTimer(life,context,scope,loop,state,outbox,cfg,result)==RAS_WAITING);
 known_ticket="52";
 assert(!RecoveryOrdinaryEventBind(loop,life,context,state,binding,pub));
 known_ticket="51";
 assert(RecoveryOrdinaryEventBind(loop,life,context,state,binding,pub));
 assert(!RecoveryOrdinaryEventBind(loop,life,context,state,binding,pub));
 assert(RecoveryOrdinaryOnTimer(life,context,scope,loop,state,outbox,cfg,result)==RAS_WAITING);
 auto wrong=scope;wrong.executor_epoch=string(32,'z');
 assert(!RecoveryOrdinaryOnTrade({51},life,context,wrong,loop));
 assert(RecoveryOrdinaryOnTrade({51},life,context,scope,loop));
 assert(!RecoveryOrdinaryOnTrade({51},life,context,scope,loop)&&sends==0);
 loop.pump.next=RAP_PROGRESS;
 assert(RecoveryOrdinaryOnTimer(life,context,scope,loop,state,outbox,cfg,result)==RAS_VERIFYING&&sends==0);
 loop.pump.proof=evidence(state.range,state.opening,51,1001,state.opening.reverse.entry_ticks-11);
 loop.pump.next=RAP_PROOF;
 assert(RecoveryOrdinaryOnTimer(life,context,scope,loop,state,outbox,cfg,result)==RAS_ADVANCED&&sends==1);
 assert(RecoveryOrdinaryOnTimer(life,context,scope,loop,state,outbox,cfg,result)==RAS_NO_EFFECT&&sends==1);
 binding.exact.order_ticket=52;
 known_ticket="52";
 assert(RecoveryOrdinaryEventBind(loop,life,context,state,binding,pub));
 mock_fill=true;loop.pump.next=RAP_PROGRESS;
 assert(RecoveryOrdinaryOnTimer(life,context,scope,loop,state,outbox,cfg,result)==RAS_VERIFYING);
 assert(loop.hint.state==RFH_USED&&sends==1);
 RecoverySessionReset(life);
 assert(RecoveryOrdinaryOnTimer(life,context,scope,loop,state,outbox,cfg,result)==RAS_NO_EFFECT);
 assert(loop.hint.state==RFH_DISARMED&&sends==1);
}
"""
class OrdinaryEventLoopCompat(unittest.TestCase):
    def test_typed_callback_and_timer_join(self):
        spec=importlib.util.spec_from_file_location('base_test',ROOT/'execution/test_session_intent_compat.py')
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        cycle=ROOT/'cycle'
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            (d/'cycle').mkdir();(d/'execution').mkdir()
            for name in ('RecoveryCycle.mqh','RecoveryRangeCoordinator.mqh','RecoveryOpeningPair.mqh',
                         'RecoveryAlternatingFill.mqh'):
                shutil.copy2(cycle/name,d/'cycle'/name)
            cp=(cycle/'RecoveryCloseCheckpoint.mqh').read_text().split('// Canonical snapshot',1)[0]
            cp=cp.replace('#include "RecoveryCloseWire.mqh"','#include "RecoveryCycle.mqh"')
            cp=cp[:cp.index('bool RCCPStageFromWire')]+cp[cp.index('void RCCPClearPlan'):]
            e=(cycle/'RecoveryCloseEvidence.mqh').read_text()
            cp=cp.replace('#include "RecoveryCycle.mqh"','#include "RecoveryCycle.mqh"\n'
            +e[e.index('struct RCCloseEvidenceKey'):e.index('struct RCExitHistory')]
            +e[e.index('bool RCCloseKeysEqual'):e.index('bool RCExitSame')],1)
            (d/'cycle/RecoveryCloseCheckpoint.mqh').write_text(cp+'\n#endif\n')
            exe=d/'execution'
            for name in ('RecoveryAlternatingPlan.mqh','RecoveryAlternatingDispatch.mqh',
                         'RecoveryAlternatingService.mqh','RecoverySessionAlternating.mqh','RecoverySessionLifecycle.mqh'):
                shutil.copy2(ROOT/'execution'/name,exe/name)
            (exe/'RecoveryOpeningDispatch.mqh').write_text(helper.SHIM+JOURNAL)
            pump=helper.PUMP.replace('double price_tick,volume_step;',
                  'double price_tick,volume_step;struct Exact{ulong order_ticket;};Exact exact;')
            pump=pump.replace('bool stopped=false;','bool stopped=false;int state=0;')
            pump=pump.replace('enum RecoveryAlternatingPumpResult', 'enum { RAP_UNUSED, RAP_RUNNING, RAP_DONE };\nenum RecoveryAlternatingPumpResult')
            pump=pump.replace('if(prior==RAP_PROOF)p.next=RAP_STOPPED;', 'if(prior==RAP_PROOF){p.next=RAP_STOPPED;p.state=RAP_DONE;}')
            (exe/'RecoveryAlternatingPump.mqh').write_text("#pragma once\n"+pump)
            (exe/'RecoveryAlternatingFillHint.mqh').write_text(HINT)
            (exe/'RecoveryAlternatingFillFallback.mqh').write_text(FALLBACK)
            key=(ROOT/'execution/RecoverySessionIntentKey.mqh').read_text().replace('#include "RecoveryIntentJournal.mqh"','')
            key=key.replace("uchar bytes[],unused[],digest[];", "std::vector<uchar> bytes,unused,digest;")
            key=helper.re.sub(r"\(string\)((?:scope|action)\.\w+)",r"std::to_string(\1)",key)
            (exe/'RecoverySessionIntentKey.mqh').write_text(key)
            loop=(HERE/'RecoveryOrdinaryEventLoop.mqh').read_text().replace('(string)b.exact.order_ticket','std::to_string(b.exact.order_ticket)')
            (exe/'RecoveryOrdinaryEventLoop.mqh').write_text(loop)
            fixture=(cycle/'test_alternating_fill.cpp').read_text().split('static RCCloseCheckpoint closure(')[0]
            source=exe/'check.cpp'
            source.write_text(fixture.replace('"RecoveryAlternatingFill.mqh"','"../cycle/RecoveryAlternatingFill.mqh"')+TEST)
            binary=d/'check'
            subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-pedantic',
                            '-fsanitize=undefined',str(source),'-lcrypto','-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True)

if __name__=='__main__':
    unittest.main()
