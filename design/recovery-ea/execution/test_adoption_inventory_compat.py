"""Host shim for logical verification only. Not a MetaEditor compilation."""
from pathlib import Path
import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
VERIFIER = HERE.parent / "verifier"
sys.path.insert(0, str(VERIFIER))
from test_reader_compat import SHIM, as_cpp
from test_record import O, P, SCOPE, wire


class AdoptionInventoryCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_bind_and_fail_without_guessing(self):
        scope = dataclasses.replace(SCOPE, server="Test-MT5")
        epsilon_sl = "3009.6999999999998181"
        position = P.replace("3009.7000000000000000", epsilon_sl)
        entry = "3010.0000000000000000"
        order = O.replace("0.0100000000000000", "0.0200000000000000").replace(
            "3009.7000000000000000", epsilon_sl)
        good = wire([position], [order], scope=scope)
        cases = [(good, "RA_CONFIRMED"),
                 (wire([position], [O], scope=scope), "RA_UNRESOLVED"),
                 (wire([position], [order.replace("\t5\t1\t", "\t5\t3\t")], scope=scope), "RA_UNRESOLVED"),
                 (wire([position], [order.replace("\t745\t", "\t746\t")], scope=scope), "RA_CONFLICT"),
                 (wire([position], [order.replace("\t773\t", "\t774\t")], scope=scope), "RA_UNRESOLVED"),
                 (wire([position], [order.replace("\t0\t5\t", "\t99\t5\t")], scope=scope), "RA_CONFLICT"),
                 (wire([position], [order.replace("\t5\t1\t", "\t6\t1\t")], scope=scope), "RA_CONFLICT"),
                 (wire([position], [order.replace("\t5\t1\t", "\t4\t1\t")], scope=scope), "RA_CONFLICT"),
                 (wire([position], [order.replace("\t5\t1\t", "\t5\t2\t")], scope=scope), "RA_UNRESOLVED"),
                 (wire([position], [order.replace("\t5\t1\t", "\t5\t6\t")], scope=scope), "RA_UNRESOLVED"),
                 (wire([position], [order.replace(epsilon_sl, "3009.7050000000000000")], scope=scope), "RA_UNRESOLVED"),
                 (wire([position], [order.replace(epsilon_sl, "3009.7000000010000000")], scope=scope), "RA_UNRESOLVED"),
                 (wire([position], [order.replace("3010.4000000000000000", "3010.4100000000000000")], scope=scope), "RA_CONFLICT"),
                 (wire([position], [order.rsplit("\t", 1)[0]+"\t3010.0000000000000000"], scope=scope), "RA_CONFLICT"),
                 (wire([position], [order.replace("0.0000000000000000", "3010.0000000000000000", 1)], scope=scope), "RA_CONFLICT"),
                 (wire([position.replace("\t772\t", "\t771\t")], [order], scope=scope), "RA_CONFLICT"),
                 (wire([position.replace("9007199254740993", "9007199254740992")], [order], scope=scope), "RA_CONFLICT"),
                 (wire([position.replace(entry, "3010.0100000000000000")], [order], scope=scope), "RA_CONFLICT"),
                 (wire([position.replace("\tBUY\t", "\tSELL\t")], [order], scope=scope), "RA_CONFLICT"),
                 (wire([position.replace("3011.0000000000000000", "3011.0100000000000000")], [order], scope=scope), "RA_CONFLICT"),
                 (wire([position.replace("0.0100000000000000", "0.0200000000000000")], [order], scope=scope), "RA_CONFLICT"),
                 (wire([position.replace("\t0\tBUY\t", "\t1234\tBUY\t")], [order], scope=scope), "RA_CONFIRMED"),
                 (wire([position, position.replace("9007199254740993\t772", "1000\t2000")], [order], scope=scope), "RA_CONFIRMED"),
                 (wire([position, position], [order], scope=scope), "RA_UNRESOLVED"),
                 (wire(orders=[order], scope=scope), "RA_UNRESOLVED"),
                 (wire([position], scope=scope), "RA_UNRESOLVED"),
                 (wire(scope=scope, coverage="unavailable", reason="changed"), "RA_UNRESOLVED"),
                 (good.replace(b"\t17\t", b"\t16\t"), "RA_UNRESOLVED"),
                 (good.replace(scope.nonce.encode(), b"4"*32), "RA_UNRESOLVED")]
        code = SHIM + r"""
#include <cmath>
#include <limits>
using ulong=std::uint64_t;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathRound(double n){return std::round(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
enum { ORDER_TYPE_BUY_STOP=4, ORDER_TYPE_SELL_STOP=5 };
enum { ORDER_STATE_PLACED=1, ORDER_STATE_CANCELED=2, ORDER_STATE_FILLED=4,
       ORDER_STATE_REJECTED=5, ORDER_STATE_EXPIRED=6 };
"""
        code += as_cpp((VERIFIER / "RecoveryInventoryReader.mqh").read_text())
        code += (HERE / "RecoveryAdoptionGate.mqh").read_text()
        adapter = (HERE / "RecoveryAdoptionInventory.mqh").read_text()
        code += "\n".join(l for l in adapter.splitlines() if not l.startswith("#include"))
        code = code.replace("   string fields[];", "   std::vector<string> fields;")
        checks = "\n".join(f"  assert(RecoveryCheckAdoptionInventory({json.dumps(b.decode())}, request, binding, intent)=={result});"
                            for b, result in cases)
        code += f"""
int main() {{
  RecoveryInventoryScope request{{"{scope.nonce}","{scope.executor_epoch}","{scope.verifier_epoch}",
   {scope.generation},{scope.login},"{scope.server}","{scope.symbol}",{scope.magic},{scope.verifier_chart}}};
  RecoveryAdoptionBinding binding{{111,request,0.01,0.01,1,"{entry}"}};
  binding.inventory_scope.nonce="";
  RecoveryAdoptionIntent intent{{111,16,9007199254740993ULL,772,773,RA_SIDE_BUY,300970,301100,301040,2}};
{checks}
  ulong ticket;
  assert(RecoveryAdoptionUnsigned("18446744073709551615",ticket) && ticket==std::numeric_limits<ulong>::max());
  assert(!RecoveryAdoptionUnsigned("18446744073709551616",ticket));
  assert(!RecoveryAdoptionUnsigned("0001",ticket));
  long number;
  assert(RecoveryAdoptionUnits("0.0000000000000000",0.01,true,number) && number==0);
  assert(!RecoveryAdoptionUnits("0.0000000000000000",0.01,false,number));
  assert(!RecoveryAdoptionUnits("0.0000000000000001",1e-16,false,number));
  assert(!RecoveryAdoptionUnits("3009.7000000000000000",0.0,false,number));
  assert(RecoveryAdoptionUnits("0.2999999999999999",0.1,false,number) && number==3);
  assert(!RecoveryAdoptionUnits("0.2999999999999900",0.1,false,number));
  assert(!RecoveryAdoptionUnits("0.0099999990000000",0.01,false,number));
  assert(!RecoveryAdoptionUnits("0.0100000000000000",std::numeric_limits<double>::quiet_NaN(),false,number));
  auto split = request; split.executor_epoch="{scope.verifier_epoch}";
  assert(!RecoveryAdoptionBound(binding,split,intent));
  split=request; split.generation++;
  assert(!RecoveryAdoptionBound(binding,split,intent));
  intent.opposite_ticket=0;
  assert(RecoveryCheckAdoptionInventory({json.dumps(good.decode())}, request, binding, intent)==RA_UNRESOLVED);
  intent.opposite_ticket=773;
  binding.intent_scope++;
  assert(RecoveryCheckAdoptionInventory({json.dumps(good.decode())}, request, binding, intent)==RA_UNRESOLVED);
}}
"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "adoption.cpp"
            binary = Path(tmp) / "adoption"
            source.write_text(code)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                            "-fsanitize=undefined", str(source), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
