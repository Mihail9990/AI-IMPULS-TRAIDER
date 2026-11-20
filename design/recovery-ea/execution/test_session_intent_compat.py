"""Host ABI shim; verifies exact key and in-session dispatch under a fake broker."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RECOVERY_SOURCE_ROOT", HERE.parent))
SHIM = r"""
#include <openssl/sha.h>
#include <cmath>
#include <climits>
#include <cstdio>
#include <string>
#include <vector>
using string=std::string;
using uchar=unsigned char;
int StringLen(const string& s){return s.size();}
string StringSubstr(const string& s,int pos,int len){return s.substr(pos,len);}
bool RecoveryInvHex(const string& s){return s.size()==32 && s.find_first_not_of("0123456789abcdef")==string::npos;}
bool RecoveryInvContext(const string& s){return !s.empty()&&s.size()<=100&&s.find_first_of("\t\n\r")==string::npos;}
bool MathIsValidNumber(double v){return std::isfinite(v);}
string DoubleToString(double v,int){char b[64];std::snprintf(b,sizeof(b),"%.16e",v);return b;}
template<class T> int ArraySize(const std::vector<T>& x){return x.size();}
template<class T> int ArrayResize(std::vector<T>& x,int n){x.resize(n);return n;}
const int ACCOUNT_LOGIN=1,ACCOUNT_SERVER=2,WHOLE_ARRAY=-1,CP_UTF8=3,CRYPT_HASH_SHA256=4;
long login=12;
string _Symbol="XAUUSD.s",server="server";
bool usable_hash=true;
long AccountInfoInteger(int){return login;}
string AccountInfoString(int){return server;}
int StringToCharArray(const string& s,std::vector<uchar>& x,int,int,int){
 x.assign(s.begin(),s.end());x.push_back(0);return x.size();
}
int CryptEncode(int,const std::vector<uchar>& bytes,const std::vector<uchar>&,
                std::vector<uchar>& out){
 if(!usable_hash)return 0;
 out.resize(32);SHA256(bytes.data(),bytes.size(),out.data());return 32;
}
enum RecoveryPairSendState { RPS_NOT_STARTED, RPS_FILL_NOT_BOUND, RPS_REVERSE_NOT_SENT, RPS_AWAIT_VERIFIER };
struct RecoveryIntentKey { string id,fingerprint,server,symbol;long login,magic,generation; };
struct RecoveryDispatchResult { int status; };
template<class T> void ZeroMemory(T& out){out=T{};}
bool RecoveryIntentKeyOK(const RecoveryIntentKey& k) {
 return RecoveryInvHex(k.id)&&k.fingerprint.size()==64&&k.login>0&&k.magic>0&&k.generation>0;
}
bool RecoveryIntentContextOK(const RecoveryIntentKey& k) {
 return RecoveryIntentKeyOK(k)&&k.login==login&&k.server==server&&k.symbol==_Symbol;
}
int sends=0;
RecoveryPairSendState RecoveryContinueOpeningPair(const RRRangeState& r,const RROpeningPair& p,
 const RecoveryIntentKey& k,double,double,RecoveryDispatchResult& out) {
 assert(RecoveryIntentContextOK(k)&&RROpeningBound(r,p)&&p.reverse.entry_ticks==RROppositeAnchor(r));
 ++sends;out.status=1;return RPS_AWAIT_VERIFIER;
}
"""
PUMP = r"""
#include <string>
enum RecoveryAlternatingPumpResult { RAP_WAIT, RAP_PROGRESS, RAP_PROOF, RAP_STOPPED };
struct RecoveryInventoryScope { std::string executor_epoch,server,symbol;long login,magic,generation; };
struct RecoveryAlternatingEvidenceBinding {
 RecoveryInventoryScope inventory;ulong scope;double price_tick,volume_step;
};
struct RecoveryAlternatingPump {
 RecoveryAlternatingEvidenceBinding binding;RRAlternatingObservation proof;
 RecoveryAlternatingPumpResult next;bool stopped=false;
};
RecoveryAlternatingPumpResult RecoveryAlternatingPumpStop(RecoveryAlternatingPump& p) {
 p.stopped=true;return RAP_STOPPED;
}
RecoveryAlternatingPumpResult RecoveryAlternatingPumpStep(RecoveryAlternatingPump& p,const RRRangeState&,
        const RROpeningPair&,RRAlternatingObservation& out) {
 if(p.stopped)return RAP_STOPPED;
 out=p.proof;const auto prior=p.next;
 if(prior==RAP_PROOF)p.next=RAP_STOPPED;
 return prior;
}
"""
TEST = r"""
#include "RecoveryAlternatingService.mqh"
int main(){
 assert(RecoverySessionSha256("abc")=="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
 const auto cfg=config();
 for(int side : {-1,1}) {
   sends=0;
   RecoveryAlternatingState s{};
   s.revision=7;s.range=first(cfg,side,1000);
   assert(RRStartAlternation(s.range,0,s.ledger));
   assert(RRStageOpeningPair(s.range,cfg,s.opening));
   RecoverySessionIntentScope ctx{string(32,'a'),12,18,"server","XAUUSD.s",s.range.scope,cfg.generation};
   auto proof=evidence(s.range,s.opening,51,1001,s.opening.reverse.entry_ticks+19);
   RecoveryAlternatingOutbox pending{};
   RecoverySessionAlternatingResult result{};
   login=99;
   assert(!RecoveryApplyVerifiedAlternating(ctx,s,pending,cfg,proof,.01,.01,result));
   login=12;usable_hash=false;
   assert(!RecoveryApplyVerifiedAlternating(ctx,s,pending,cfg,proof,.01,.01,result));
   assert(s.revision==7 && sends==0);
   usable_hash=true;
   assert(RecoveryApplyVerifiedAlternating(ctx,s,pending,cfg,proof,.01,.01,result));
   assert(result.transition==RR_ALT_NEXT_OPPOSITE && result.dispatch==RPS_AWAIT_VERIFIER && sends==1);
   assert(s.revision==8&&pending.action.entry_ticks==proof.fill.entry.actual_entry_ticks+side*80&&pending.action.side==side);
   auto changed=pending.action;changed.sl_ticks++;
   RecoveryIntentKey changed_key,other_session,other_grid;
   assert(RecoveryBuildSessionIntentKey(ctx,changed,.01,.01,changed_key));
   assert(changed_key.id==result.key.id&&changed_key.fingerprint!=result.key.fingerprint);
   assert(RecoveryBuildSessionIntentKey(ctx,pending.action,.02,.01,other_grid));
   assert(other_grid.id==result.key.id&&other_grid.fingerprint!=result.key.fingerprint);
   auto fresh=ctx;fresh.executor_epoch=string(32,'b');
   assert(RecoveryBuildSessionIntentKey(fresh,pending.action,.01,.01,other_session));
   assert(other_session.id!=result.key.id);
   assert(!RecoveryApplyVerifiedAlternating(ctx,s,pending,cfg,proof,.01,.01,result)&&sends==1);
   RecoverySessionContext live{7,12,18,"server","XAUUSD.s","configured"};
   RecoverySessionLifecycle session{};
   assert(RecoverySessionInit(session,live,true,ctx.executor_epoch)==RSL_NEW);
   RecoveryAlternatingPump pump{{{ctx.executor_epoch,ctx.server,ctx.symbol,ctx.login,ctx.magic,
      static_cast<long>(cfg.generation)},ctx.scope,.01,.01}, {}, RAP_WAIT};
   assert(RecoveryServiceOrdinaryAlternating(session,live,pump,s,pending,cfg,result)==RAS_WAITING&&sends==1);
   pump.next=RAP_PROGRESS;
   assert(RecoveryServiceOrdinaryAlternating(session,live,pump,s,pending,cfg,result)==RAS_VERIFYING&&sends==1);
   pump.next=RAP_PROOF;
   pump.proof=evidence(s.range,s.opening,53,1003,s.opening.reverse.entry_ticks-11);
   auto wrong=pump;wrong.binding.inventory.executor_epoch=string(32,'x');
   assert(RecoveryServiceOrdinaryAlternating(session,live,wrong,s,pending,cfg,result)==RAS_NO_EFFECT);
   assert(wrong.stopped&&sends==1);
   assert(RecoveryServiceOrdinaryAlternating(session,live,pump,s,pending,cfg,result)==RAS_ADVANCED&&sends==2);
   assert(RecoveryServiceOrdinaryAlternating(session,live,pump,s,pending,cfg,result)==RAS_NO_EFFECT&&sends==2);
   for(int n=4;n<=24;n++){
     auto successor=evidence(s.range,s.opening,50+n,1000+n,s.opening.reverse.entry_ticks-11);
     assert(RecoveryApplyVerifiedAlternating(ctx,s,pending,cfg,successor,.01,.01,result));
   }
   assert(sends==22&&pending.revision==0&&result.transition==RR_ALT_FINAL_PROTECTED);
 }
}
"""
class SessionIntentTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "host C++ unavailable")
    def test_verified_chain_fixed_intent_and_no_repeat(self):
        cycle=ROOT/"cycle"
        with tempfile.TemporaryDirectory() as folder:
            d=Path(folder)
            (d/"cycle").mkdir();(d/"execution").mkdir()
            for name in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                shutil.copy2(cycle/name,d/"cycle"/name)
            cp=(cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
            cp=cp.replace('#include "RecoveryCloseWire.mqh"', '#include "RecoveryCycle.mqh"')
            cp=cp[:cp.index('bool RCCPStageFromWire')]+cp[cp.index('void RCCPClearPlan'):]
            e=(cycle/"RecoveryCloseEvidence.mqh").read_text()
            cp=cp.replace('#include "RecoveryCycle.mqh"', '#include "RecoveryCycle.mqh"\n'+
                e[e.index('struct RCCloseEvidenceKey'):e.index('struct RCExitHistory')]+
                e[e.index('bool RCCloseKeysEqual'):e.index('bool RCExitSame')],1)
            (d/"cycle/RecoveryCloseCheckpoint.mqh").write_text(cp+"\n#endif\n")
            for name in ("RecoveryAlternatingPlan.mqh","RecoveryAlternatingDispatch.mqh"):
                shutil.copy2(ROOT/"execution"/name,d/"execution"/name)
            (d/"execution/RecoveryOpeningDispatch.mqh").write_text(SHIM)
            key=(HERE/"RecoverySessionIntentKey.mqh").read_text().replace('#include "RecoveryIntentJournal.mqh"','')
            key=key.replace("uchar bytes[],unused[],digest[];","std::vector<uchar> bytes,unused,digest;")
            key=re.sub(r"\(string\)((?:scope|action)\.\w+)",r"std::to_string(\1)",key)
            (d/"execution/RecoverySessionIntentKey.mqh").write_text(key)
            for name in ("RecoverySessionAlternating.mqh","RecoverySessionLifecycle.mqh","RecoveryAlternatingService.mqh"):
                path=HERE/name
                if not path.exists():path=ROOT/"execution"/name
                shutil.copy2(path,d/"execution"/name)
            (d/"execution/RecoveryAlternatingPump.mqh").write_text(PUMP)
            fixture=(cycle/"test_alternating_fill.cpp").read_text().split("static RCCloseCheckpoint closure(")[0]
            cpp=d/"execution/test.cpp"
            cpp.write_text(fixture.replace('"RecoveryAlternatingFill.mqh"','"../cycle/RecoveryAlternatingFill.mqh"')+TEST)
            exe=d/"check"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(cpp),"-lcrypto","-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
