"""One break-even move, exact confirmation, no changing opposing trigger."""
from pathlib import Path
import os,re,runpy,shutil,subprocess,tempfile,unittest
HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))

WATCH=r"""
struct RecoveryPriceScope { string executor_epoch,server,symbol;long login;ulong generation; };
int publications=0;long quote=0;
bool RecoveryWatchedScope(const RRRangeState& s,const RRSettings& cfg,const RecoveryPriceScope&,double){
 return RRValid(s,cfg)&&s.phase==RR_ACTIVE&&s.pending.kind==RR_NONE;
}
bool RecoveryPriceFollow(const RecoveryPriceScope&,int stage,const string&){
 assert(stage==2);++publications;return true;
}
bool RecoveryReadWatchedPeak(RRRangeState& s,const RRSettings& cfg,const RecoveryPriceScope&,ulong,double,RRAction& out){
 return RRStage(s,cfg,quote,quote,out);
}
"""
DISPATCH=r"""
int sends=0;
struct RecoveryDispatchResult{int status;};
bool RecoveryDispatchOpeningSLAsync(const RRRangeState& s,const RRAction& a,const RecoveryIntentKey& k,
 double,double,RecoveryDispatchResult& result){
 assert(RRActionEqual(s.pending,a)&&RecoveryIntentContextOK(k));
 ++sends;result.status=1;return true;
}
"""
EFFECT=r"""
struct TestInvScope {string executor_epoch,server,symbol;long login,magic,generation;};
struct RecoveryPositionEffectBinding {TestInvScope request;RRAction action;};
bool effect_observed=false;
bool RecoveryReadPositionEffect(const RRRangeState&,const RecoveryPositionEffectBinding& proof,RREffect& effect){
 if(!effect_observed)return false;
 effect.action=proof.action;effect.terminal_verified=true;effect.created_order=0;return true;
}
"""
TEST=r"""
#include "RecoverySingleSupportService.mqh"
int main(){
 auto cfg=config();
 cfg.support_first_ticks=0;cfg.support_offset_ticks=0;cfg.support_second_ticks=40;
 RecoverySessionContext live{10,12,18,"server","XAUUSD.s","fixed"};
 string epoch(32,'a');
 RecoverySessionLifecycle life{};
 assert(RecoverySessionInit(life,live,true,epoch)==RSL_NEW);
 RecoverySessionIntentScope scope{epoch,12,18,"server","XAUUSD.s",cfg.scope,cfg.generation};
 for(int side : {-1,1}) {
  auto s=first(cfg,side,1000);
  s.phase=RR_ACTIVE;
  const long anchor=s.cycle.actual_entry_ticks-side*cfg.initial_sl_ticks;
  s.opposite={81,-side,anchor,s.cycle.actual_entry_ticks,1};
  const auto preserved=s.opposite;
  RecoveryPriceScope watch{epoch,"server","XAUUSD.s",12,cfg.generation};
  RecoverySingleSupport task{};
  RecoveryDispatchResult receipt{};
  assert(RecoveryStartSingleSupport(task,life,live,s,cfg,watch,"fresh",.01));
  assert(!RecoveryStartSingleSupport(task,life,live,s,cfg,watch,"fresh",.01));
  const int before=sends;
  quote=s.cycle.actual_entry_ticks+side*25;
  assert(!RecoverySubmitSingleSupport(task,life,live,scope,s,cfg,.01,.01,receipt));
  assert(sends==before&&s.support_stage==0&&s.pending.kind==RR_NONE);
  quote=s.cycle.actual_entry_ticks+side*40;
  assert(RecoverySubmitSingleSupport(task,life,live,scope,s,cfg,.01,.01,receipt));
  assert(s.pending.kind==RR_SUPPORT_SL&&s.pending.sl_ticks==s.cycle.actual_entry_ticks);
  assert(s.support_stage==0&&sends==before+1&&RRKnownEqual(s.opposite,preserved));
  assert(!RecoverySubmitSingleSupport(task,life,live,scope,s,cfg,.01,.01,receipt)&&sends==before+1);
  RecoveryPositionEffectBinding proof{{epoch,"server","XAUUSD.s",12,18,static_cast<long>(cfg.generation)},task.action};
  effect_observed=false;assert(!RecoveryConfirmSingleSupport(task,s,proof));
  effect_observed=true;
  auto forged=proof;forged.request.magic++;
  assert(!RecoveryConfirmSingleSupport(task,s,forged));
  assert(RecoveryConfirmSingleSupport(task,s,proof)&&s.support_stage==2);
  assert(s.live_sl==s.cycle.actual_entry_ticks&&RRKnownEqual(s.opposite,preserved));
  assert(!RecoveryConfirmSingleSupport(task,s,proof));
 }
 assert(publications==2&&sends==2);
}
"""
class SingleSupportCompat(unittest.TestCase):
    def test_mirrored_break_even_only_after_verification(self):
        cycle=ROOT/"cycle"; execution=ROOT/"execution"
        shim=runpy.run_path(str(execution/"test_session_intent_compat.py"))["SHIM"]
        shim=re.sub(r"^(?:enum RecoveryPairSendState|struct RecoveryDispatchResult).*\n","",
                    shim.split("int sends=",1)[0],flags=re.M)
        with tempfile.TemporaryDirectory() as d:
            base=Path(d); c=base/"cycle"; c.mkdir(); q=base/"execution"; q.mkdir()
            for n in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                shutil.copy2(cycle/n,c/n)
            cp=(cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
            cp=cp.replace('#include "RecoveryCloseWire.mqh"','#include "RecoveryCycle.mqh"')
            cp=cp[:cp.index("bool RCCPStageFromWire")]+cp[cp.index("void RCCPClearPlan"):]
            e=(cycle/"RecoveryCloseEvidence.mqh").read_text()
            cp=cp.replace('#include "RecoveryCycle.mqh"','#include "RecoveryCycle.mqh"\n'+
                e[e.index("struct RCCloseEvidenceKey"):e.index("struct RCExitHistory")]+
                e[e.index("bool RCCloseKeysEqual"):e.index("bool RCExitSame")],1)
            (c/"RecoveryCloseCheckpoint.mqh").write_text(cp+"\n#endif\n")
            shutil.copy2(execution/"RecoverySessionLifecycle.mqh",q/"RecoverySessionLifecycle.mqh")
            key=(execution/"RecoverySessionIntentKey.mqh").read_text().replace('#include "RecoveryIntentJournal.mqh"','')
            key=key.replace("uchar bytes[],unused[],digest[];","std::vector<uchar> bytes,unused,digest;")
            key=re.sub(r"\(string\)((?:scope|action)\.\w+)",r"std::to_string(\1)",key)
            (q/"RecoverySessionIntentKey.mqh").write_text(key)
            for name,text in (("RecoveryWatchedSupport.mqh",WATCH),("RecoveryDispatch.mqh",DISPATCH),("RecoveryPositionEffect.mqh",EFFECT)):
                (q/name).write_text(text)
            shutil.copy2(HERE/"RecoverySingleSupportService.mqh",q/"RecoverySingleSupportService.mqh")
            fixture=(cycle/"test_alternating_fill.cpp").read_text().split("static RRAlternatingObservation evidence(")[0]
            fixture=fixture.replace('"RecoveryAlternatingFill.mqh"','"../cycle/RecoveryAlternatingFill.mqh"')
            fixture=fixture.replace("constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();","")
            (q/"check.cpp").write_text(shim+fixture+TEST)
            exe=base/"check"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            str(q/"check.cpp"),"-lcrypto","-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)
if __name__=="__main__":unittest.main()
