"""Offline compile of the MQL close-checkpoint codec and replay semantics."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
VERIFIER = HERE.parent / "verifier"
EXECUTION = HERE.parent / "execution"
sys.path.insert(0, str(VERIFIER))
from test_history_reader_compat import history_as_cpp
from test_reader_compat import SHIM, as_cpp
from test_close_evidence_compat import arrays, bare


class CloseCheckpointCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_codec_and_replay(self):
        cpp = SHIM + r"""
#include <cmath>
using ulong=std::uint64_t;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathRound(double n){return std::round(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
enum { ORDER_TYPE_BUY=0, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT,
       ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP, ORDER_TYPE_BUY_STOP_LIMIT, ORDER_TYPE_SELL_STOP_LIMIT };
enum { ORDER_STATE_PLACED=1, ORDER_STATE_CANCELED=2, ORDER_STATE_FILLED=4,
       ORDER_STATE_REJECTED=5, ORDER_STATE_EXPIRED=6 };
enum { DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1 };
enum { DEAL_REASON_CLIENT=0, DEAL_REASON_MOBILE, DEAL_REASON_WEB, DEAL_REASON_EXPERT,
       DEAL_REASON_SL, DEAL_REASON_TP };
template<class T> int ArraySize(const std::vector<T>& a){return static_cast<int>(a.size());}
template<class T> int ArrayResize(std::vector<T>& a,int n){a.resize(n);return n;}
"""
        cpp += as_cpp((VERIFIER / "RecoveryInventoryReader.mqh").read_text())
        cpp += history_as_cpp((VERIFIER / "RecoveryHistoryReader.mqh").read_text())
        cpp += bare((EXECUTION / "RecoveryAdoptionGate.mqh").read_text())
        cpp += arrays(bare((EXECUTION / "RecoveryAdoptionInventory.mqh").read_text()))
        cpp += bare((HERE / "RecoveryCycle.mqh").read_text())
        cpp += arrays(bare((HERE / "RecoveryCloseEvidence.mqh").read_text()))
        cpp += arrays(bare((HERE / "RecoveryCloseWire.mqh").read_text()))
        checkpoint = (HERE / "RecoveryCloseCheckpoint.mqh").read_text().split("// Caller owns naming",1)[0] + "\n#endif\n"
        checkpoint = re.sub(r"\(string\)(r\.\w+\.\w+|r\.reentry_distance|\(r\.before\.side==1\?1:2\))",
                            r"std::to_string(\1)",checkpoint)
        cpp += arrays(bare(checkpoint)).replace("string f[];","std::vector<string> f;")
        cpp += r"""
static string change(string s,const string &old,const string &replacement) {
 const auto at=s.find(old); assert(at!=string::npos); s.replace(at,old.size(),replacement); return s;
}

int main() {
 RCCloseCheckpoint record{};
 auto &before=record.before;
 before.generation=909;before.range_id=2;before.full_sl_count=4;
 before.active_order=18446744073709551615ULL;before.position_id=9007199254740993ULL;
 before.side=1;before.filled_steps=7;before.actual_entry_ticks=301000;
 record.key={100,11,before.generation,before.range_id,before.position_id,before.active_order};
 record.reentry_distance=70;
 record.proof={before.generation,before.range_id,before.active_order,before.position_id,
               7,7,0,true,true,RC_SL,300975,RC_SUPPORT_LEVEL,true};
 const auto text=RCCPEncode(record);
 assert(!text.empty());
 RCCloseCheckpoint decoded;
 assert(RCCPDecode(text,record.key,decoded));
 assert(RCCPEncode(decoded)==text && RCCPSameRange(decoded.before,before));
 RCRange current=before;
 RCClosePlan plan;
 assert(RCCPApply(decoded,current,plan)==RCCP_APPLIED);
 assert(plan.reentry_ticks==301045 && plan.reentry_slot==5 && plan.keep_existing_opposite);
 auto final=current;
 assert(RCCPApply(decoded,current,plan)==RCCP_ALREADY_APPLIED);
 assert(plan.reentry_ticks==0 && !plan.keep_existing_opposite && RCCPSameRange(final,current));
 current.last_closed_order--;
 assert(RCCPApply(decoded,current,plan)==RCCP_CONFLICT);
 current=before;current.full_sl_count++;
 assert(RCCPApply(decoded,current,plan)==RCCP_CONFLICT && current.active_order==before.active_order);
 current=before;current.actual_entry_ticks++;
 assert(RCCPApply(decoded,current,plan)==RCCP_CONFLICT);
 current=before;current.generation++;
 assert(RCCPApply(decoded,current,plan)==RCCP_CONFLICT);
 auto expected=record.key;expected.scope++;
 assert(!RCCPDecode(text,expected,decoded) && decoded.key.scope==0);
 expected=record.key;expected.observation++;
 assert(!RCCPDecode(text,expected,decoded));
 assert(!RCCPDecode("",record.key,decoded));
 assert(!RCCPDecode(text.substr(0,text.size()-1),record.key,decoded));
 assert(!RCCPDecode(text+"END\n",record.key,decoded));
 assert(!RCCPDecode(change(text,"\t301000\t","\t0301000\t"),record.key,decoded));
 assert(!RCCPDecode(change(text,"\t300975\t2\t","\t300975\t3\t"),record.key,decoded));
 assert(!RCCPDecode(change(text,"\t18446744073709551615\t","\t18446744073709551616\t"),record.key,decoded));
 auto partial=record;partial.proof.exited_steps--;
 assert(RCCPEncode(partial).empty());
 partial=record;partial.proof.independently_verified=false;
 assert(RCCPEncode(partial).empty());
 partial=record;partial.proof.confirmed_stop_mask=RC_INITIAL_LEVEL|RC_SUPPORT_LEVEL;
 assert(RCCPEncode(partial).empty());
 auto last=record;last.before.full_sl_count=23;
 last.proof.confirmed_stop_mask=RC_INITIAL_LEVEL;
 current=last.before;
 assert(RCCPApply(last,current,plan)==RCCP_APPLIED);
 assert(current.full_sl_count==24 && current.requires_manual_ranges && plan.cancel_remaining_range_orders);
 assert(RCCPApply(last,current,plan)==RCCP_ALREADY_APPLIED && !plan.cancel_remaining_range_orders);
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "checkpoint.cpp"
            executable = Path(tmp) / "checkpoint"
            source.write_text(cpp)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(source),"-o",str(executable)],check=True)
            subprocess.run([str(executable)],check=True)

    def test_publish_does_not_overwrite(self):
        source=(HERE / "RecoveryCloseCheckpoint.mqh").read_text()
        self.assertIn('FileMove(tmp,0,final,0)',source)
        self.assertIn('FileIsExist(final)',source)
        self.assertNotIn('FILE_REWRITE',source)


if __name__=="__main__":
    unittest.main()
