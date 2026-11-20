"""Host shim for exact-order verifier to coordinator effect conversion."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
VERIFIER = HERE.parent / "verifier"
CYCLE = HERE.parent / "cycle"
sys.path.insert(0, str(VERIFIER))
from test_reader_compat import SHIM, as_cpp

A, B, C = "1"*32, "2"*32, "3"*32
TICKET = 9007199254740997
HEAD = f"RECOVERY_EXACT_ORDER_V1\t{A}\t{B}\t{C}\t7\t5\tDemo\tXAUUSD.s\t700\t99\t2\t{TICKET}"
ROW = "E\tcurrent\t0\t700\t4\t1\t0.0200000000000000\t0.0200000000000000\t4000.0000000000000000\t3999.3000000000001819\t0.0000000000000000\t0.0000000000000000"
LIVE = f"{HEAD}\tobserved\tok\n{ROW}\nEND\n"
CANCEL = LIVE.replace("\tcurrent\t", "\thistory\t").replace("\t4\t1\t", "\t4\t2\t")


class ExactOrderEffectCompat(unittest.TestCase):
    def test_host_adapter_fails_closed(self):
        compiler = shutil.which("g++")
        self.assertIsNotNone(compiler)
        coordinator = (CYCLE/"RecoveryRangeCoordinator.mqh").read_text()
        structs = coordinator[coordinator.index("enum RRPhase"):coordinator.index("bool RRKnownEqual")]
        eq = coordinator[coordinator.index("bool RRActionEqual"):coordinator.index("void RRSetKnown")]
        clear = coordinator[coordinator.index("void RRClearAction"):coordinator.index("bool RRSettingsValid")]
        reader = (VERIFIER/"RecoveryExactOrderReader.mqh").read_text().split("// Publisher tag",1)[0]+"\n#endif\n"
        reader = re.sub(r"\(string\)(scope\.\w+(?:\.\w+)?)", r"std::to_string(\1)", reader)
        ado = (HERE/"RecoveryAdoptionInventory.mqh").read_text()
        units = ado[ado.index("bool RecoveryAdoptionUnsigned"):ado.index("bool RecoveryAdoptionBound")]
        adapter = (HERE/"RecoveryExactOrderEffect.mqh").read_text().split("// Transport entry point;",1)[0]+"\n#endif\n"
        adapter = adapter.replace("(string)binding.request.binding.magic","std::to_string(binding.request.binding.magic)")
        prefix = SHIM + f'\n#include <cmath>\n#include "{CYCLE/"RecoveryCycle.mqh"}"\n'
        prefix += """
using ulong=std::uint64_t;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathRound(double n){return std::round(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
enum {ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,ORDER_STATE_PLACED=1,ORDER_STATE_CANCELED=2};
"""
        rotation = (CYCLE/"RecoverySupportRotation.mqh").read_text()
        move_kind = next(line for line in rotation.splitlines() if line.startswith("#define RR_MOVE_SUPPORT_OPPOSITE "))
        body = move_kind+"\n"+structs+eq+clear+as_cpp((VERIFIER/"RecoveryInventoryReader.mqh").read_text())+reader+units+adapter
        body = "\n".join(l for l in body.splitlines() if not l.startswith("#include"))
        body = re.sub(r"string (fields|f)\[\];", r"std::vector<string> \1;", body)
        create_bad = (
            f"{HEAD}\tunavailable\tunknown\nEND\n", CANCEL,
            LIVE.replace("\t4\t1\t", "\t4\t3\t"),
            LIVE.replace("\t4\t1\t", "\t5\t1\t"),
            LIVE.replace("\tcurrent\t0\t", "\tcurrent\t902\t"),
            LIVE.replace("\t99\t2\t", "\t98\t2\t"),
            LIVE.replace(f"\t{TICKET}\t",f"\t{TICKET+1}\t"),
            LIVE.replace(A, "4"*32), LIVE.replace("\tDemo\t", "\tOther\t"),
            LIVE.replace("4000.0000000000000000","4000.0100000000000000"),
            LIVE.replace("3999.3000000000001819","3999.3100000000000000"),
            LIVE.replace("3999.3000000000001819","3999.3050000000000000"),
            LIVE.replace("0.0200000000000000\t4000","0.0100000000000000\t4000"),
            LIVE.replace("3999.3000000000001819\t0.0000000000000000",
                         "3999.3000000000001819\t4020.0000000000000000"),
            LIVE.replace("0.0000000000000000\nEND","4000.0000000000000000\nEND"),
        )
        cancel_bad = (
            LIVE, CANCEL.replace("\t4\t2\t","\t4\t4\t"),
            CANCEL.replace("\t4\t2\t","\t4\t6\t"),
            CANCEL.replace("\thistory\t0\t","\thistory\t201\t"),
            CANCEL.replace("0.0200000000000000\t4000","0.0100000000000000\t4000"),
        )
        negative = lambda rows: "\n".join(
            f"assert(!RecoveryBuildExactOrderEffect({json.dumps(r)},s,b,e) && !e.terminal_verified && !e.created_order);"
            for r in rows)
        move = LIVE.replace("4000.0000000000000000","4000.1400000000000000").replace(
            "3999.3000000000001819","3999.4400000000000000")
        move_bad = (
            LIVE, CANCEL,
            move.replace(f"\t{TICKET}\t", f"\t{TICKET+1}\t"),
            move.replace("\tcurrent\t", "\thistory\t"),
            move.replace("\t4\t1\t", "\t4\t3\t"),
            move.replace("\t4\t1\t", "\t5\t1\t"),
            move.replace("0.0200000000000000\t4000", "0.0100000000000000\t4000"),
            move.replace("3999.4400000000000000","3999.4300000000000000"),
            move.replace("3999.4400000000000000\t0.0000000000000000",
                         "3999.4400000000000000\t4001.0000000000000000"),
        )
        main = f"""
int main() {{
  RRRangeState s{{}}; s.scope=19; s.cycle.generation=7; s.cycle.range_id=1;
  s.pending={{19,7,3,1,RR_PRIMARY,1,0,0,0,0,400000,399930,0,2}};
  RecoveryExactEffectBinding b{{{{{{"{A}","{B}","{C}",7,5,"Demo","XAUUSD.s",700,99}},{TICKET}ULL}},s.pending,1,0.01,0.01}};
  RREffect e;
  assert(RecoveryBuildExactOrderEffect({json.dumps(LIVE)},s,b,e));
  assert(e.terminal_verified && e.created_order=={TICKET}ULL && RRActionEqual(e.action,s.pending));
  {negative(create_bad)}
  b.verify_after=2; assert(!RecoveryBuildExactOrderEffect({json.dumps(LIVE)},s,b,e));
  b.verify_after=1; s.pending.serial++; assert(!RecoveryBuildExactOrderEffect({json.dumps(LIVE)},s,b,e));
  s.pending.kind=RR_CANCEL; s.pending.order_ticket={TICKET}ULL; b.action=s.pending;
  assert(RecoveryBuildExactOrderEffect({json.dumps(CANCEL)},s,b,e));
  assert(e.terminal_verified && e.created_order==0 && RRActionEqual(e.action,s.pending));
  {negative(cancel_bad)}
  b.request.order_ticket++; assert(!RecoveryBuildExactOrderEffect({json.dumps(CANCEL)},s,b,e));
  b.request.order_ticket={TICKET}ULL;
  s.pending.kind=RR_MOVE_SUPPORT_OPPOSITE; s.pending.entry_ticks=400014;
  s.pending.sl_ticks=399944; s.pending.tp_ticks=0; b.action=s.pending;
  s.retire={{{TICKET}ULL,1,400000,399930,2}};
  assert(RecoveryBuildExactOrderEffect({json.dumps(move)},s,b,e));
  assert(e.terminal_verified && e.created_order==0 && e.action.order_ticket=={TICKET}ULL && RRActionEqual(e.action,s.pending));
  {negative(move_bad)}
  s.retire.volume_steps=3; assert(!RecoveryBuildExactOrderEffect({json.dumps(move)},s,b,e));
  s.retire.volume_steps=2; s.retire.ticket++;
  assert(!RecoveryBuildExactOrderEffect({json.dumps(move)},s,b,e));s.retire.ticket--;
  s.retire.entry_ticks=s.pending.entry_ticks;s.retire.sl_ticks=s.pending.sl_ticks;
  assert(!RecoveryBuildExactOrderEffect({json.dumps(move)},s,b,e));
  s.retire.sl_ticks--;
  b.request.order_ticket++; assert(!RecoveryBuildExactOrderEffect({json.dumps(move)},s,b,e));
}}
"""
        with tempfile.TemporaryDirectory() as tmp:
            cpp = Path(tmp)/"effect.cpp"
            exe = cpp.with_suffix("")
            cpp.write_text(prefix+body+main)
            subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)


if __name__=="__main__":
    unittest.main()
