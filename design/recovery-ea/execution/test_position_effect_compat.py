"""Offline check of position inventory -> exact RR SL effect."""
from pathlib import Path
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
VERIFIER=HERE.parent/"verifier"
CYCLE=HERE.parent/"cycle"
sys.path.insert(0,str(VERIFIER))
from test_reader_compat import SHIM, as_cpp
from test_record import P, SCOPE, wire


class PositionEffectCompat(unittest.TestCase):
    def test_tickets_and_reserved_intent(self):
        compiler=shutil.which("g++")
        self.assertIsNotNone(compiler)
        coord=(CYCLE/"RecoveryRangeCoordinator.mqh").read_text()
        structs=coord[coord.index("enum RRPhase"):coord.index("bool RRKnownEqual")]
        eq=coord[coord.index("bool RRActionEqual"):coord.index("void RRSetKnown")]
        clear=coord[coord.index("void RRClearAction"):coord.index("bool RRSettingsValid")]
        ado=(HERE/"RecoveryAdoptionInventory.mqh").read_text()
        units=ado[ado.index("bool RecoveryAdoptionUnsigned"):ado.index("bool RecoveryAdoptionBound")]
        source=(HERE/"RecoveryPositionEffect.mqh").read_text().split("// Transport entry point",1)[0]+"\n#endif\n"
        body=structs+eq+clear+as_cpp((VERIFIER/"RecoveryInventoryReader.mqh").read_text())+units+source
        body="\n".join(l for l in body.splitlines() if not l.startswith("#include"))
        body=re.sub(r"string f\[\];",r"std::vector<string> f;",body)
        prefix=SHIM+f'\n#include <cmath>\n#include "{CYCLE/"RecoveryCycle.mqh"}"\n'
        prefix += """
using ulong=std::uint64_t;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathRound(double n){return std::round(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
"""
        scope=dataclasses.replace(SCOPE, server="Test-MT5", generation=7)
        live=wire([P],scope=scope)
        support=wire([P.replace("3009.7000000000000000","3010.0000000000000000")],scope=scope)
        bad=(
            wire(scope=scope), wire(scope=scope,coverage="unavailable",reason="changed"),
            wire([P.replace("9007199254740993","9007199254740992")],scope=scope),
            wire([P.replace("\t772\t","\t773\t")],scope=scope),
            wire([P.replace("\t0\tBUY\t","\t6\tBUY\t")],scope=scope),
            wire([P.replace("\tBUY\t","\tSELL\t")],scope=scope),
            wire([P.replace("0.0100000000000000","0.0200000000000000")],scope=scope),
            wire([P.replace("3010.0000000000000000","3010.0100000000000000")],scope=scope),
            wire([P.replace("3009.7000000000000000","3009.6990000000000000")],scope=scope),
            wire([P.replace("3009.7000000000000000","0.0000000000000000")],scope=scope),
            wire([P.replace("3011.0000000000000000","3011.0100000000000000")],scope=scope),
            wire([P],scope=dataclasses.replace(scope,nonce="4"*32)),
            wire([P],scope=dataclasses.replace(scope,verifier_chart=88)),
            wire([P],scope=dataclasses.replace(scope,login=44)),
            wire([P],scope=dataclasses.replace(scope,generation=8)),
        )
        q=lambda x:json.dumps(x.decode())
        checks="\n".join(f'assert(!RecoveryBuildPositionEffect({q(r)},s,b,e) && !e.terminal_verified && !e.created_order);' for r in bad)
        main=f"""
int main() {{
  RRRangeState s{{}}; s.scope=19; s.cycle.generation=7; s.cycle.range_id=1;
  s.cycle.active_order=14; s.cycle.position_id=9007199254740993ULL; s.cycle.side=1;
  s.cycle.filled_steps=1; s.position_ticket=772; s.initial_sl=300970;
  s.phase=RR_PROTECT; s.live_tp=301100;
  s.pending={{19,7,3,1,RR_INITIAL_SL,1,0,14,s.cycle.position_id,772,0,300970,301100,0}};
  RecoveryPositionEffectBinding b{{{{"{scope.nonce}","{scope.executor_epoch}","{scope.verifier_epoch}",
   7,{scope.login},"{scope.server}","{scope.symbol}",{scope.magic},{scope.verifier_chart}}},s.pending,16,0,"3010.0000000000000000",0.01,0.01}};
  RREffect e;
  assert(RecoveryBuildPositionEffect({q(live)},s,b,e));
  assert(e.terminal_verified && e.created_order==0 && RRActionEqual(e.action,s.pending));
  {checks}
  b.verify_after=17;assert(!RecoveryBuildPositionEffect({q(live)},s,b,e));b.verify_after=16;
  b.prior_position_entry="";assert(!RecoveryBuildPositionEffect({q(live)},s,b,e));b.prior_position_entry="3010.0000000000000000";
  s.pending.kind=RR_NONE;assert(!RecoveryBuildPositionEffect({q(live)},s,b,e));
  s.pending=b.action;s.pending.kind=RR_SUPPORT_SL;s.pending.sl_ticks=301000;s.pending.stage=1;s.phase=RR_ACTIVE;b.action=s.pending;
  assert(RecoveryBuildPositionEffect({q(support)},s,b,e) && e.terminal_verified);
  s.support_stage=1;assert(!RecoveryBuildPositionEffect({q(support)},s,b,e));
}}
"""
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/"position.cpp"; exe=src.with_suffix("")
            src.write_text(prefix+body+main)
            subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(src),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)


if __name__=="__main__":
    unittest.main()
