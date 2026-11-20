"""Exercise the MQL5 close reducers with a C++ shim and synthetic wire rows."""
import dataclasses
import json
from pathlib import Path
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
from test_history_record import S, wire as history_wire
from test_reader_compat import SHIM, as_cpp
from test_record import P, wire as inventory_wire


def bare(text):
    return "\n".join(line for line in text.splitlines() if not line.startswith("#include")) + "\n"


def arrays(text):
    return (text.replace("const RCExitDeal &deals[]", "const std::vector<RCExitDeal> &deals")
            .replace("const RCProtectionFact &protection[]", "const std::vector<RCProtectionFact> &protection")
            .replace("RCExitDeal exits[];", "std::vector<RCExitDeal> exits;")
            .replace("string fields[];", "std::vector<string> fields;"))


class CloseEvidenceCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_reducer_and_reader_wiring(self):
        binding = dataclasses.replace(S.inventory_scope, server="Test-MT5", generation=5)
        history_scope = dataclasses.replace(S, inventory_scope=binding, anchor_order=30,
                                            position_identifier=9007199254740993)
        inv_scope = dataclasses.replace(binding, nonce="4"*32)
        anchor = ("H\t30\t745\t4\t4\t10001\t0.0700000000000000\t0.0000000000000000"
                  "\t3010.0000000000000000\t3009.7000000000000000\t0.0000000000000000")
        partial = anchor.replace("\t4\t10001\t0.0700000000000000\t0.0000000000000000",
                                 "\t2\t10001\t0.1000000000000000\t0.0300000000000000")
        entered = ("D\t100\t30\t745\tBUY\t0\t3\t10001\t0.0700000000000000"
                   "\t3010.0000000000000000\t3009.7000000000000000\t0.0000000000000000")
        exited = ("D\t200\t40\t745\tSELL\t1\t4\t10002\t0.0700000000000000"
                  "\t3009.6000000000000000\t3009.7000000000000000\t0.0000000000000000")
        supported = exited.replace("3009.7000000000000000", "3009.7500000000000000")
        hist = lambda deals, orders=(anchor,): history_wire(orders, deals, scope=history_scope)
        inv = inventory_wire(scope=inv_scope)
        keep = inventory_wire([P], scope=inv_scope)
        split = (exited.replace("0.0700000000000000", "0.0600000000000000"),
                 exited.replace("0.0700000000000000", "0.0100000000000000").replace("\t200\t", "\t201\t"))
        good = hist([entered, exited])
        cases = [
            (good,inv,"RCE_FULLY_CLASSIFIED",5,0),
            (hist([entered,supported]),inv,"RCE_FULLY_CLASSIFIED",4,301045),
            (hist([entered,*split]),inv,"RCE_FULLY_CLASSIFIED",5,0),
            (hist([entered,exited], [partial]),inv,"RCE_FULLY_CLASSIFIED",5,0),
            (hist([entered,split[0]]),keep,"RCE_OPEN_OR_PARTIAL",4,0),
            (hist([entered,split[0]]),inv,"RCE_UNRESOLVED",4,0),
            (hist([entered,exited]),keep,"RCE_UNRESOLVED",4,0),
            (hist([entered]),inv,"RCE_UNRESOLVED",4,0),
            (hist([entered,exited], []),inv,"RCE_UNRESOLVED",4,0),
            (hist([entered,exited,exited]),inv,"RCE_UNRESOLVED",4,0),
            (hist([entered.replace("\t30\t", "\t31\t"),exited]),inv,"RCE_UNRESOLVED",4,0),
            (hist([entered,exited.replace("\t1\t4\t", "\t3\t4\t")]),inv,"RCE_UNRESOLVED",4,0),
            (hist([entered,exited.replace("\t1\t4\t", "\t1\t3\t")]),inv,"RCE_UNRESOLVED",4,0),
            (hist([entered,exited.replace("\t1\t4\t", "\t1\t0\t")]),inv,"RCE_FULLY_CLASSIFIED",4,0),
            (hist([entered,exited.replace("\t1\t4\t", "\t1\t5\t")]),inv,"RCE_UNRESOLVED",4,0),
            (hist([entered,split[0],split[1].replace("3009.7000000000000000","3009.7500000000000000")]),
             inv,"RCE_UNRESOLVED",4,0),
            (good,inventory_wire(coverage="unavailable", reason="read", scope=inv_scope),"RCE_UNRESOLVED",4,0),
            (good,inventory_wire(scope=binding),"RCE_UNRESOLVED",4,0),
        ]
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
        cpp += (HERE / "test_close_evidence.cpp").read_text().replace(
            '#include "RecoveryCloseEvidence.cpp.inc"', '').replace(
            "template<class T> int ArraySize(const std::vector<T>& a){return static_cast<int>(a.size());}", ""
        ).replace("int main()", "void test_normalized()")
        q = lambda s: json.dumps(s.decode())
        snippets = "\n".join(f"""
 r=initial; assert(RCApplyCloseFromWire(r,bind,{q(h)},{q(i)},stops,70,plan)=={result});
 assert(r.full_sl_count=={count} && plan.reentry_ticks=={reentry});
""" for h,i,result,count,reentry in cases)
        # Monetary observations use the same exact V1 reports as the trading
        # path, including a completely closed position with mixed exit reasons.
        money_cases = [
            (hist([entered, exited.replace("\t1\t4\t", "\t1\t5\t")]), "RCE_FULLY_CLASSIFIED", 7),
            (hist([entered, split[0], split[1].replace("\t1\t4\t", "\t1\t0\t")]),
             "RCE_CLOSED_UNCLASSIFIED", 7),
            (hist([entered, split[0]]), "RCE_UNRESOLVED", 0),
        ]
        money_checks = "\n".join(f"""
 assert(RecoveryHistoryParse({q(h)},hs,raw_history));
 assert(RCBuildCloseFromReports(initial,bind,raw_history,raw_inventory,stops,economic,true)=={status});
 assert(economic.exited_steps=={steps} && economic.independently_verified==({steps}!=0));
""" for h, status, steps in money_cases)
        cpp += f"""
int main() {{
 test_normalized();
 auto initial=range();auto r=initial;
 auto k=key(r);
 RecoveryInventoryScope ib{{"{inv_scope.nonce}","{binding.executor_epoch}","{binding.verifier_epoch}",
  {binding.generation},{binding.login},"{binding.server}","{binding.symbol}",{binding.magic},{binding.verifier_chart}}};
 auto hb=ib;hb.nonce="{binding.nonce}";
 RecoveryHistoryScope hs{{hb,k.position_id,k.anchor_order}};
 RCCloseWireBinding bind{{k,hs,ib,0,0,.01,.01}};
 const std::vector<RCProtectionFact> stops{{level(k,RC_INITIAL_LEVEL,300970),level(k,RC_SUPPORT_LEVEL,300975)}};
 RCClosePlan plan{{}};
 RecoveryHistorySnapshot raw_history;RecoveryInventorySnapshot raw_inventory;RCCloseProof economic;
 assert(RecoveryInvParse({q(inv)},ib,raw_inventory));
 {money_checks}
 {snippets}
 r=initial;assert(RCApplyCloseFromWire(r,bind,{q(good)},{q(inv)},stops,70,plan)==RCE_FULLY_CLASSIFIED);
 assert(RCApplyCloseFromWire(r,bind,{q(good)},{q(inv)},stops,70,plan)==RCE_UNRESOLVED);
 r=initial;bind.history_after=9999;
 assert(RCApplyCloseFromWire(r,bind,{q(good)},{q(inv)},stops,70,plan)==RCE_UNRESOLVED);
}}
"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "evidence.cpp"
            executable = Path(tmp) / "close"
            source.write_text(cpp)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                            "-fsanitize=undefined", str(source), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
