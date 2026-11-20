"""Strict host test of owner-bound verifier tokens and existing pump/hint contract."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RECOVERY_SOURCE_ROOT", HERE.parent)) / "execution"

HASH_SHIM = r"""
#include <openssl/sha.h>
#include <climits>
using uchar=unsigned char;
int StringLen(const string& s){return s.size();}
string StringSubstr(const string& s,int p,int n){return s.substr(p,n);}
template<class T> int ArraySize(const std::vector<T>& x){return x.size();}
template<class T> int ArrayResize(std::vector<T>& x,int n){x.resize(n);return n;}
const int WHOLE_ARRAY=-1,CP_UTF8=3,CRYPT_HASH_SHA256=4;
int hash_limit=-1,hash_calls=0;
int StringToCharArray(const string& s,std::vector<uchar>& x,int,int,int){
 x.assign(s.begin(),s.end());x.push_back(0);return x.size();
}
int CryptEncode(int,const std::vector<uchar>& bytes,const std::vector<uchar>&,
                std::vector<uchar>& out){
 if(hash_limit>=0 && hash_calls++>=hash_limit)return 0;
 out.resize(32);SHA256(bytes.data(),bytes.size(),out.data());return 32;
}
bool RecoveryInvContext(const string& s){
 return !s.empty()&&s.size()<=100&&s.find_first_of("\t\n\r")==string::npos;
}
enum {TRADE_TRANSACTION_HISTORY_ADD,TRADE_TRANSACTION_HISTORY_UPDATE,ORDER_STATE_FILLED,
      ORDER_TYPE_BUY_STOP,ORDER_TYPE_SELL_STOP};
struct MqlTradeTransaction {int type;ulong order;string symbol;int order_state,order_type;};
"""

TEST = r"""
#include <set>
int main(){
 RecoverySessionContext live{7,5,700,"Test","XAUUSD.s","config"};
 const string boot(32,'2'),verifier(32,'3');
 RecoverySessionLifecycle session{};
 assert(RecoverySessionInit(session,live,true,boot)==RSL_NEW);
 RecoveryEvidenceOwner owner{};
 assert(!RecoveryBeginEvidenceOwner(owner,session,live,boot,99));
 assert(!RecoveryBeginEvidenceOwner(owner,session,live,verifier,live.chart));
 assert(RecoveryBeginEvidenceOwner(owner,session,live,verifier,99));
 assert(!RecoveryBeginEvidenceOwner(owner,session,live,verifier,100));

 RRRangeState range{};
 range.scope=19;range.position_ticket=83;range.cycle={81,82,7,1,1};
 range.phase=RR_ACTIVE;range.initial_sl=399930;
 const ulong ticket=9007199254740997ULL;
 range.opposite={ticket,-1,399930,400010,2};
 RROpeningPair opening{};
 RecoveryOwnerEvidence a{},b{};
 assert(!RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket+1,11,.01,.01,a));
 assert(owner.serial==0&&a.serial==0&&a.publisher_tag.empty());
 hash_calls=0;hash_limit=2;
 assert(!RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket,11,.01,.01,a));
 assert(owner.serial==0&&a.serial==0);
 hash_limit=-1;
 assert(!RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket,LONG_MAX,.01,.01,a));
 assert(!RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket,11,0,.01,a));
 auto changed_account=live;changed_account.login++;
 assert(!RecoveryReserveOwnerEvidence(owner,session,changed_account,range,opening,ticket,11,.01,.01,a));
 assert(RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket,11,.01,.01,a));
 assert(a.serial==1&&a.range_id==1&&a.prior_order==81&&a.prior_position==82&&a.prior_ticket==83);
 assert(a.binding.exact.order_ticket==ticket&&a.binding.history.anchor_order==ticket&&
        a.binding.history.position_identifier==0&&a.binding.verify_after==11&&a.binding.routed.kind==RR_NONE);
 assert(a.binding.exact.binding.executor_epoch==boot&&a.binding.exact.binding.verifier_epoch==verifier&&
        a.binding.exact.binding.verifier_chart==99&&a.binding.exact.binding.generation==7);
 const auto tokens=[](const RecoveryOwnerEvidence& e){
   return std::set<string>{e.binding.exact.binding.nonce,e.binding.history.binding.nonce,
                           e.binding.inventory.nonce,e.publisher_tag};
 };
 const auto ta=tokens(a);
 assert(ta.size()==4);
 assert(RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket,11,.01,.01,b));
 const auto tb=tokens(b);
 assert(b.serial==2&&tb.size()==4);
 for(const auto& x:tb)assert(!ta.count(x)&&RecoveryInvHex(x));
 // Independent context fixtures intentionally start from the same serial so
 // tests would detect an omitted identity field despite sequence differences.
 const auto base=[&](const RRRangeState& r,const RecoverySessionLifecycle& ss,ulong t){
   RecoveryEvidenceOwner o{};RecoveryOwnerEvidence x{};
   assert(RecoveryBeginEvidenceOwner(o,ss,live,verifier,99));
   assert(RecoveryReserveOwnerEvidence(o,ss,live,r,opening,t,11,.01,.01,x));
   assert(x.serial==1);
   return tokens(x);
 };
 auto altered=range;altered.cycle.range_id=2;
 assert(base(altered,session,ticket)!=ta);
 altered=range;altered.cycle.generation++;
 assert(base(altered,session,ticket)!=ta);
 altered=range;altered.scope++;
 assert(base(altered,session,ticket)!=ta);
 altered=range;altered.opposite.ticket++;
 assert(base(altered,session,ticket+1)!=ta);
 RecoverySessionLifecycle next{};
 assert(RecoverySessionInit(next,live,true,string(32,'f'))==RSL_NEW);
 assert(base(range,next,ticket)!=ta);
 RecoveryFillHint hint{};altered=range;altered.position_ticket++;
 assert(!RecoveryArmFrozenOwnerEvidence(hint,owner,next,live,range,opening,a));
 assert(!RecoveryArmFrozenOwnerEvidence(hint,owner,session,live,altered,opening,a));
 assert(RecoveryArmFrozenOwnerEvidence(hint,owner,session,live,range,opening,a));
 assert(hint.state==RFH_ARMED);
 assert(!RecoveryArmFrozenOwnerEvidence(hint,owner,session,live,range,opening,b));

 // Pending pair may arm only from its caller-owned ticket association; when
 // already verified the known ticket is cross-checked.
 opening.reserved=true;opening.reverse_verified=true;opening.reverse_ticket=ticket;
 opening.reverse={RR_REVERSE,-1,399930,400010,2,0};
 assert(!RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket+1,11,.01,.01,b));
 assert(RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket,11,.01,.01,b));
 assert(b.binding.routed.kind==RR_REVERSE);
 owner.serial=ULONG_MAX;
 assert(!RecoveryReserveOwnerEvidence(owner,session,live,range,opening,ticket,11,.01,.01,b));
 assert(owner.serial==ULONG_MAX&&b.serial==0);
 assert(req_exact==0&&req_hist==0&&req_inv==0);
}
"""

class OwnerEvidenceCompat(unittest.TestCase):
    def test_scope_tokens_and_inactive_invalid(self):
        src = lambda path: (ROOT / path).read_text()
        shim = src("test_alternating_pump.cpp").split('#include "PumpUnderTest"')[0]
        inv = (ROOT.parent / "verifier/RecoveryInventoryReader.mqh").read_text()
        inv = inv[inv.index("bool RecoveryInvScopeOK"):inv.index("bool RecoveryInvNatural")]
        shim = re.sub(r"bool RecoveryInvScopeOK[^\n]*", "", shim)
        e = src("RecoveryAlternatingEvidence.mqh")
        h = src("RecoverySessionIntentKey.mqh")
        snippets = HASH_SHIM + inv + h[h.index("string RecoverySessionSha256"):h.index("// ID identifies")] + \
            e[e.index("struct RecoveryAlternatingEvidenceBinding"):e.index("bool RecoveryAlternatingOrderTerms")] + \
            src("RecoverySessionLifecycle.mqh") + src("RecoveryAlternatingPump.mqh") + \
            src("RecoveryAlternatingFillHint.mqh") + (HERE / "RecoveryOwnerEvidence.mqh").read_text()
        snippets = "\n".join(x for x in snippets.splitlines() if not x.startswith("#include"))
        snippets = re.sub(r"\(string\)([\w.]+)", r"std::to_string(\1)", snippets)
        snippets = re.sub(r"\buchar bytes\[\],unused\[\],digest\[\];", r"std::vector<uchar> bytes,unused,digest;", snippets)
        snippets = snippets.replace("string f[];", "std::vector<string> f;")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cpp = root / "test.cpp"
            cpp.write_text("#include <openssl/sha.h>\n#include <climits>\n"+shim+snippets+TEST)
            exe = root / "test"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(cpp),"-lcrypto","-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
