"""Offline compatibility: actual scoped verifier parsers and proof fold."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RECOVERY_SOURCE_ROOT", HERE.parent))
sys.path.insert(0, str(ROOT / "verifier"))
from test_reader_compat import SHIM, as_cpp

ORDER = 9007199254740997
POSITION = 9007199254740998
TICKET = 9007199254740999
ZERO = "0.0000000000000000"
VOL = "0.0200000000000000"
HALF = "0.0100000000000000"
PRICE = "3999.3000000000000000"
SL = "4000.1000000000000000"
ENTRY = "3999.2000000000000000"
SESSION = ("2"*32, "3"*32, 7, 5, "Test-MT5", "XAUUSD.s", 700, 99)

def report(kind, nonce, seq, header, rows):
    prefix = "\t".join(map(str, (kind, nonce*32, *SESSION, seq, *header)))
    return prefix+"\n"+"\n".join(rows+["END", ""])

E = f"E\thistory\t{POSITION}\t700\t5\t4\t{VOL}\t{ZERO}\t{PRICE}\t{SL}\t{ZERO}\t{ZERO}"
H = f"H\t{ORDER}\t700\t5\t4\t1900\t{VOL}\t{ZERO}\t{PRICE}\t{SL}\t{ZERO}"
D = f"D\t111\t{ORDER}\t700\tSELL\t0\t3\t1900\t{VOL}\t{ENTRY}\t{SL}\t{ZERO}"
P = f"P\t{POSITION}\t{TICKET}\t700\tSELL\t{VOL}\t{ENTRY}\t{SL}\t{ZERO}"
OLD = f"P\t82\t83\t700\tBUY\t{HALF}\t{SL}\t{PRICE}\t{ZERO}"
O = f"O\t{ORDER}\t700\t0\t5\t1\t{VOL}\t{VOL}\t{PRICE}\t{SL}\t{ZERO}\t{ZERO}"
def erow(row=E, seq=2):
    return report("RECOVERY_EXACT_ORDER_V1", "1", seq, [ORDER,"observed","ok"], [row])

def hrows(rows=(D,), order=H, seq=3):
    return report("RECOVERY_HISTORY_V1", "4", seq, [POSITION,ORDER,"observed","ok",1,len(rows)],
                  [order,*rows])

def irows(positions=(P,), orders=(), seq=4):
    return report("RECOVERY_INVENTORY_V1", "5", seq, ["complete","ok",len(positions),len(orders)],
                  [*positions,*orders])

class AlternatingEvidenceCompat(unittest.TestCase):
    def test_join_uses_three_exact_correlated_reports(self):
        cycle, verifier = ROOT/"cycle", ROOT/"verifier"
        checkpoint = (cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
        checkpoint = checkpoint[:checkpoint.index("bool RCCPStageFromWire")]+checkpoint[checkpoint.index("void RCCPClearPlan"):]
        keys = (cycle/"RecoveryCloseEvidence.mqh").read_text()
        checkpoint = checkpoint.replace('#include "RecoveryCloseWire.mqh"', '#include "RecoveryCycle.mqh"\n'+
                    keys[keys.index('struct RCCloseEvidenceKey'):keys.index('struct RCExitHistory')]+
                    keys[keys.index('bool RCCloseKeysEqual'):keys.index('bool RCExitSame')])
        ado = (ROOT/"execution"/"RecoveryAdoptionInventory.mqh").read_text()
        exact = (verifier/"RecoveryExactOrderReader.mqh").read_text().split("// Publisher tag",1)[0]+"\n#endif\n"
        history = (verifier/"RecoveryHistoryReader.mqh").read_text().split("// Publisher tag",1)[0]+"\n#endif\n"
        adapter = (HERE/"RecoveryAlternatingEvidence.mqh").read_text().split("bool RecoveryReadAlternatingEvidence(",1)[0]+"\n#endif\n"
        body = as_cpp((verifier/"RecoveryInventoryReader.mqh").read_text()) + exact + history
        body += ado[ado.index("bool RecoveryAdoptionUnsigned"):ado.index("bool RecoveryAdoptionBound")] + adapter
        body = "\n".join(l for l in body.splitlines() if not l.startswith("#include"))
        body = re.sub(r"\(string\)([\w.]+)", r"std::to_string(\1)", body)
        body = re.sub(r"string (fields|f|rows)\[\];", r"std::vector<string> \1;", body)
        body = body.replace("const string &p[]", "const std::vector<string> &p")
        body = body.replace("deal_tickets[128], rows[];", "deal_tickets[128]; std::vector<string> rows;")
        d1 = D.replace(VOL,HALF).replace(ENTRY,"3999.1900000000000000")
        d2 = D.replace(VOL,HALF).replace(ENTRY,"3999.2100000000000000").replace("\t111\t","\t112\t")
        negative = [
          (erow(E.replace("history","current")),hrows(),irows()),
          (erow(E.replace(f"\t4\t{VOL}",f"\t3\t{VOL}")),hrows(),irows()),
          (erow(E.replace(str(POSITION),str(POSITION+1))),hrows(),irows()),
          (erow(E.replace(SL,ENTRY)),hrows(),irows()),
          (erow(),hrows(order=H.replace(SL,ENTRY)),irows()),
          (erow(),hrows([D.replace(str(ORDER),str(ORDER+1))]),irows()),
          (erow(),hrows([D.replace(VOL,HALF)]),irows()),
          (erow(),hrows([D,D]),irows()),
          (erow(),hrows([d1,d2.replace("3999.210","3999.220")]),irows()),
          (erow(),hrows([D.replace("SELL\t0","SELL\t1")]),irows()),
          (erow(),hrows(),irows([])),
          (erow(),hrows(),irows([P], [O])),
          (erow(),hrows(),irows([P.replace(SL,PRICE)])),
          (erow(),hrows(),irows([P.replace(f"\t{TICKET}\t", "\t83\t")])),
          (erow(),hrows(),irows([P.replace("\t700\t", "\t701\t")])),
          (erow(),hrows(seq=2),irows()),
          (erow(),hrows(),irows(seq=3)),
          (erow(),hrows(),irows().replace("Test-MT5","Other")),
        ]
        neg = "\n".join("assert(!apply("+",".join(json.dumps(x) for x in packet)+
                        ") && !obs.fill.independently_verified);" for packet in negative)
        main = f"""
int main() {{
    RRRangeState s{{}};
    s.scope=19;s.cycle.generation=7;s.cycle.range_id=1;s.cycle.side=1;s.cycle.position_id=82;
    s.cycle.active_order=81;s.position_ticket=83;s.cycle.filled_steps=2;
    s.initial_sl=399930;s.phase=RR_ACTIVE;s.pending.kind=RR_NONE;s.opposite={{{ORDER}ULL,-1,399930,400010,2}};
    RROpeningPair opening{{}};
    RecoveryInventoryScope sc{{"{'1'*32}","{'2'*32}","{'3'*32}",7,5,"Test-MT5","XAUUSD.s",700,99}};
    RecoveryAlternatingEvidenceBinding b{{}};
    b.exact.binding=sc;b.exact.order_ticket={ORDER}ULL;
    b.history.binding=sc;b.history.binding.nonce="{'4'*32}";
    b.history.position_identifier={POSITION}ULL;b.history.anchor_order={ORDER}ULL;
    b.inventory=sc;b.inventory.nonce="{'5'*32}";b.scope=19;b.verify_after=1;
    b.price_tick=0.01;b.volume_step=0.01;
    RRAlternatingObservation obs{{}};
    auto apply=[&](const string &a,const string &h,const string &i) {{
        ZeroMemory(obs);RecoveryExactReport x;RecoveryHistorySnapshot y;RecoveryInventorySnapshot z;
        return RecoveryExactReportParse(a,b.exact,x) && RecoveryHistoryParse(h,b.history,y) &&
               RecoveryInvParse(i,b.inventory,z) && RecoveryAlternatingEvidenceFold(s,opening,b,x,y,z,obs);
    }};
    assert(apply({json.dumps(erow())},{json.dumps(hrows())},{json.dumps(irows())}));
    assert(obs.fill.position_ticket=={TICKET}ULL && obs.fill.entry.actual_entry_ticks==399920 &&
           obs.executed_order.ticket=={ORDER}ULL && obs.fill.sl_ticks==400010 &&
           obs.predecessor_inventory_complete && !obs.predecessor_present && obs.fill.entry.planned_steps==2);
    assert(apply({json.dumps(erow())},{json.dumps(hrows([d1,d2]))},{json.dumps(irows([P,OLD]))}));
    assert(obs.predecessor_present && obs.predecessor_position_id==82 && obs.predecessor_remaining_steps==1);
    {neg}
    b.verify_after=2;
    assert(!apply({json.dumps(erow())},{json.dumps(hrows())},{json.dumps(irows())}));
    b.verify_after=1;s.opposite.ticket++;
    assert(!apply({json.dumps(erow())},{json.dumps(hrows())},{json.dumps(irows())}));
    s.opposite.ticket=0;s.phase=RR_ARM_REVERSE;s.live_sl=s.initial_sl;s.action_serial=9;
    s.pending={{19,7,9,1,RR_REVERSE,-1,0,0,0,0,399930,400010,0,2}};
    opening.reserved=true; opening.active_order=81;opening.position_id=82;opening.position_ticket=83;
    opening.sl_verified=true;opening.reverse=s.pending;b.routed=s.pending;
    assert(apply({json.dumps(erow())},{json.dumps(hrows())},{json.dumps(irows())}));
    assert(obs.paired_order_ticket=={ORDER}ULL && RRActionEqual(obs.paired_intent,s.pending));
    b.routed.serial++;
    assert(!apply({json.dumps(erow())},{json.dumps(hrows())},{json.dumps(irows())}));
    b.routed=s.pending;
    opening.reverse_verified=true; opening.reverse_ticket={ORDER+1}ULL;
    assert(!apply({json.dumps(erow())},{json.dumps(hrows())},{json.dumps(irows())}));
}}
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                (root/name).write_bytes((cycle/name).read_bytes())
            (root/"RecoveryCloseCheckpoint.mqh").write_text(checkpoint+"\n#endif\n")
            test = root/"adapter.cpp"
            test.write_text(SHIM+"""
#include <cmath>
#include <climits>
using ulong=std::uint64_t;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathRound(double n){return std::round(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
enum {ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,ORDER_STATE_FILLED=4,DEAL_ENTRY_IN=0};
template<class T> void ZeroMemory(T &out){out=T{};}
#include "RecoveryAlternatingFill.mqh"
"""+body+main)
            exe = root/"test"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(test),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__ == "__main__":
    unittest.main()
