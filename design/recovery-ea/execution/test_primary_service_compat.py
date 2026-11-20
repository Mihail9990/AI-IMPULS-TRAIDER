"""Fake terminal I/O around real first-pair dispatch orchestration."""
from pathlib import Path
import os,re,runpy,shutil,subprocess,tempfile,unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))
FILL=r"""
struct RecoveryPrimaryFillBinding {
 struct Scope{ulong scope_unused;string executor_epoch,server,symbol;long login,magic,generation;};
 Scope inventory;
 struct Exact{ulong order_ticket;};Exact exact;
 ulong scope; double price_tick,volume_step;
};
bool proof_ready=false;
bool RecoveryReadPrimaryFill(const RRRangeState&,const RRSettings&,const RecoveryPrimaryFillBinding&,RRFilled&){
 return proof_ready;
}
bool RecoveryStagePrimaryOpening(RRRangeState& next,const RRSettings& cfg,const RecoveryPrimaryFillBinding&,
 const RRFilled&,RROpeningPair& p,RRAlternationLedger& ledger){
 if(p.reserved||ledger.scope!=0)return false;
 p.reserved=true;
 next.phase=RR_ARM_REVERSE;
 p.reverse=next.pending;
 p.reverse.kind=RR_REVERSE;p.reverse.side=-1;
 p.reverse.entry_ticks=399939;p.reverse.sl_ticks=p.reverse.entry_ticks+cfg.initial_sl_ticks;
 ++p.reverse.serial;
 RRClearAction(p.sl);p.sl_required=false;
 ledger.scope=next.scope;
 return true;
}
"""
DISPATCH=r"""
const int RI_TICKET=2;
string precise="51";
int RecoveryIntentLookup(const RecoveryIntentKey&,string& out){out=precise;return RI_TICKET;}
enum RecoveryPairSendState{RPS_NOT_STARTED,RPS_AWAIT_VERIFIER};
struct RecoveryPairSendResult{int count;};
int sends=0;
RecoveryPairSendState RecoveryDispatchOpeningPair(const RRRangeState& s,const RROpeningPair& p,
  const RecoveryIntentKey& sl,const RecoveryIntentKey& reverse,double,double,RecoveryPairSendResult& result){
 assert(s.phase==RR_ARM_REVERSE&&p.reserved&&!p.sl_required&&p.sl.kind==RR_NONE);
 assert(sl.id.empty()&&!reverse.id.empty());
 ++sends;result.count=1;
 return RPS_AWAIT_VERIFIER;
}
"""
TEST=r"""
#include "RecoveryPrimaryService.mqh"
int main(){
 auto cfg=config();
 RRRangeState state{};
 const RecoverySessionContext live{1,12,18,"server","XAUUSD.s","fixed"};
 const string epoch(32,'a');
 RecoverySessionIntentScope scope{epoch,12,18,"server","XAUUSD.s",cfg.scope,cfg.generation};
 RecoverySessionLifecycle life{};
 assert(RecoverySessionInit(life,live,true,epoch)==RSL_NEW);
 assert(RRNewRange(state,cfg,1,1,400000));
 RRAction initial{};
 assert(RRStage(state,cfg,0,0,initial));
 RecoveryPrimaryFillBinding b{{0,epoch,"server","XAUUSD.s",12,18,static_cast<long>(cfg.generation)},
                              {51},cfg.scope,.01,.01};
 RROpeningPair opening{};
 RRAlternationLedger ledger{};
 RecoveryPrimaryOpenResult out{};
 assert(!RecoveryOpenVerifiedPrimary(b,life,live,scope,state,cfg,opening,ledger,out));
 assert(!opening.reserved&&sends==0);
 proof_ready=true;precise="52";
 assert(!RecoveryOpenVerifiedPrimary(b,life,live,scope,state,cfg,opening,ledger,out));
 precise="51";
 assert(RecoveryOpenVerifiedPrimary(b,life,live,scope,state,cfg,opening,ledger,out));
 assert(sends==1&&opening.reserved&&out.disposition==RPS_AWAIT_VERIFIER);
 assert(!RecoveryOpenVerifiedPrimary(b,life,live,scope,state,cfg,opening,ledger,out));
 assert(sends==1);
}
"""
class PrimaryServiceCompat(unittest.TestCase):
    def test_primary_dispatch_once_after_exact_evidence(self):
        cycle=ROOT/"cycle";execution=ROOT/"execution"
        base=runpy.run_path(str(execution/"test_session_intent_compat.py"))["SHIM"]
        base=re.sub(r"^(?:enum RecoveryPairSendState|struct RecoveryDispatchResult).*\n",
                    "",base.split("int sends=",1)[0],flags=re.M)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/"cycle").mkdir();q=p/"execution";q.mkdir()
            for name in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                shutil.copy2(cycle/name,p/"cycle"/name)
            cp=(cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
            cp=cp.replace('#include "RecoveryCloseWire.mqh"','#include "RecoveryCycle.mqh"')
            cp=cp[:cp.index("bool RCCPStageFromWire")]+cp[cp.index("void RCCPClearPlan"):]
            e=(cycle/"RecoveryCloseEvidence.mqh").read_text()
            cp=cp.replace('#include "RecoveryCycle.mqh"','#include "RecoveryCycle.mqh"\n'+
                 e[e.index("struct RCCloseEvidenceKey"):e.index("struct RCExitHistory")]+
                 e[e.index("bool RCCloseKeysEqual"):e.index("bool RCExitSame")],1)
            (p/"cycle/RecoveryCloseCheckpoint.mqh").write_text(cp+"\n#endif\n")
            shutil.copy2(execution/"RecoverySessionLifecycle.mqh",q/"RecoverySessionLifecycle.mqh")
            key=(execution/"RecoverySessionIntentKey.mqh").read_text().replace('#include "RecoveryIntentJournal.mqh"','')
            key=key.replace("uchar bytes[],unused[],digest[];","std::vector<uchar> bytes,unused,digest;")
            key=re.sub(r"\(string\)((?:scope|action)\.\w+)",r"std::to_string(\1)",key)
            (q/"RecoverySessionIntentKey.mqh").write_text(key)
            (q/"RecoveryPrimarySubmit.mqh").write_text("""#include "RecoverySessionLifecycle.mqh"
#include "RecoverySessionIntentKey.mqh"
bool RecoveryPrimarySession(const RecoverySessionLifecycle& s,const RecoverySessionContext& c,
 const RecoverySessionIntentScope& i) {return RecoverySessionCurrent(s,c) && s.epoch==i.executor_epoch;}
""")
            (q/"RecoveryPrimaryFill.mqh").write_text(FILL)
            (q/"RecoveryOpeningDispatch.mqh").write_text(DISPATCH)
            svc=(HERE/"RecoveryPrimaryService.mqh").read_text().replace("(string)binding.exact.order_ticket","std::to_string(binding.exact.order_ticket)")
            (q/"RecoveryPrimaryService.mqh").write_text(svc)
            f=(cycle/"test_alternating_fill.cpp").read_text().split("static RRRangeState first(")[0]
            f=f.replace('"RecoveryAlternatingFill.mqh"','"../cycle/RecoveryAlternatingFill.mqh"')
            (q/"check.cpp").write_text(base+f.replace("constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();", "")+TEST)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                           str(q/"check.cpp"),"-lcrypto","-o",str(p/"check")],check=True)
            subprocess.run([str(p/"check")],check=True)
if __name__=="__main__":unittest.main()
