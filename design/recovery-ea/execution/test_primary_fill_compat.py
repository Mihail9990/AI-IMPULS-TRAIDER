"""Read actual verifier formats and stage a primary opening in an offline host shim."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))
sys.path.extend((str(ROOT/"verifier"),str(ROOT/"execution")))
from test_reader_compat import SHIM, as_cpp
from test_alternating_evidence_compat import report, ORDER, POSITION, TICKET, ZERO, VOL, HALF

def packet(side, slipped, partial=False):
    kind, side_word=(4,"BUY") if side==1 else (5,"SELL")
    stop="3999.2000000000000000" if side==1 else "4000.8000000000000000"
    entry=("4000.0500000000000000" if side==1 else "3999.9500000000000000") if slipped else "4000.0000000000000000"
    nominal="4000.0000000000000000"
    rem,size,state=(HALF,HALF,2) if partial else (ZERO,VOL,4)
    rows_e=f"E\thistory\t{POSITION}\t700\t{kind}\t{state}\t{VOL}\t{rem}\t{nominal}\t{stop}\t{ZERO}\t{ZERO}"
    row_h=f"H\t{ORDER}\t700\t{kind}\t{state}\t1900\t{VOL}\t{rem}\t{nominal}\t{stop}\t{ZERO}"
    row_d=f"D\t102\t{ORDER}\t700\t{side_word}\t0\t3\t1900\t{size}\t{entry}\t{stop}\t{ZERO}"
    row_p=f"P\t{POSITION}\t{TICKET}\t700\t{side_word}\t{size}\t{entry}\t{stop}\t{ZERO}"
    return (report("RECOVERY_EXACT_ORDER_V1","1",2,[ORDER,"observed","ok"],[rows_e]),
            report("RECOVERY_HISTORY_V1","4",3,[POSITION,ORDER,"observed","ok",1,1],[row_h,row_d]),
            report("RECOVERY_INVENTORY_V1","5",4,["complete","ok",1,0],[row_p]))

class PrimaryFillCompat(unittest.TestCase):
    def test_first_primary_verifier_fold_and_atomic_geometry(self):
        cycle,verifier=ROOT/"cycle",ROOT/"verifier"
        checkpoint=(cycle/"RecoveryCloseCheckpoint.mqh").read_text().split("// Canonical snapshot",1)[0]
        checkpoint=checkpoint[:checkpoint.index("bool RCCPStageFromWire")]+checkpoint[checkpoint.index("void RCCPClearPlan"):]
        keys=(cycle/"RecoveryCloseEvidence.mqh").read_text()
        checkpoint=checkpoint.replace('#include "RecoveryCloseWire.mqh"','#include "RecoveryCycle.mqh"\n'+
            keys[keys.index("struct RCCloseEvidenceKey"):keys.index("struct RCExitHistory")]+
            keys[keys.index("bool RCCloseKeysEqual"):keys.index("bool RCExitSame")])
        exact=(verifier/"RecoveryExactOrderReader.mqh").read_text().split("// Publisher tag",1)[0]+"\n#endif\n"
        history=(verifier/"RecoveryHistoryReader.mqh").read_text().split("// Publisher tag",1)[0]+"\n#endif\n"
        ado=(ROOT/"execution"/"RecoveryAdoptionInventory.mqh").read_text()
        altern=(ROOT/"execution"/"RecoveryAlternatingEvidence.mqh").read_text()
        session=altern[altern.index("bool RecoveryAlternatingSameSession"):altern.index("bool RecoveryAlternatingWanted")]
        header=(HERE/"RecoveryPrimaryFill.mqh").read_text()
        header=header[:header.index("bool RecoveryReadPrimaryFill")] + header[header.index("// Atomic RAM-only transition."):]
        body=as_cpp((verifier/"RecoveryInventoryReader.mqh").read_text())+exact+history
        body+=ado[ado.index("bool RecoveryAdoptionUnsigned"):ado.index("bool RecoveryAdoptionBound")]+session+header
        body="\n".join(line for line in body.splitlines() if not line.startswith("#include"))
        body=re.sub(r"\(string\)([\w.]+)",r"std::to_string(\1)",body)
        body=re.sub(r"string (fields|f|rows)\[\];",r"std::vector<string> \1;",body)
        body=body.replace("const string &p[]","const std::vector<string> &p")
        body=body.replace("deal_tickets[128], rows[];","deal_tickets[128]; std::vector<string> rows;")
        args=lambda values:",".join(json.dumps(x) for x in values)
        buy=packet(1,True)
        sell=packet(-1,False)
        partial=packet(-1,True,True)
        scope='{"'+'1'*32+'","'+'2'*32+'","'+'3'*32+'",7,5,"Test-MT5","XAUUSD.s",700,99}'
        main=f'''
int main() {{
 RRSettings cfg{{}};cfg.scope=21;cfg.generation=7;cfg.initial_sl_ticks=80;
 cfg.support_first_ticks=25;cfg.support_second_ticks=40;cfg.support_offset_ticks=25;
 for(int i=0;i<24;i++)cfg.lot_steps[i]=2;
 RecoveryPrimaryFillBinding b{{}};
 const RecoveryInventoryScope scope={scope};
 b.exact.binding=scope;b.exact.order_ticket={ORDER}ULL;
 b.history.binding=scope;b.history.binding.nonce="{'4'*32}";
 b.history.anchor_order={ORDER}ULL;b.history.position_identifier={POSITION}ULL;
 b.inventory=scope;b.inventory.nonce="{'5'*32}";b.verify_after=1;b.scope=cfg.scope;
 b.price_tick=.01;b.volume_step=.01;
 RRRangeState s{{}};RROpeningPair opening{{}};RRAlternationLedger ledger{{}};RRFilled f{{}};
 auto new_range=[&](int side,bool already_working) {{
    s={{}};opening={{}};ledger={{}};
    assert(RRNewRange(s,cfg,1,side,400000));
    RRAction primary{{}};
    assert(RRStage(s,cfg,0,0,primary));
    RRClearAction(b.routed);
    if(already_working)assert(RRConfirm(s,{{primary,true,{ORDER}ULL}}));
    else b.routed=primary;
 }};
 auto reports=[&](const string &e,const string &h,const string &i) {{
    ZeroMemory(f);RecoveryExactReport x;RecoveryHistorySnapshot y;RecoveryInventorySnapshot z;
    return RecoveryExactReportParse(e,b.exact,x) && RecoveryHistoryParse(h,b.history,y) &&
           RecoveryInvParse(i,b.inventory,z) && RecoveryPrimaryFillFold(s,cfg,b,x,y,z,f);
 }};
 new_range(1,false);
 assert(reports({args(buy)}) && s.phase==RR_NEW && s.pending.kind==RR_PRIMARY);
 auto wrong=f;wrong.entry.position_id++;
 assert(!RecoveryStagePrimaryOpening(s,cfg,b,wrong,opening,ledger)&&s.phase==RR_NEW&&!opening.reserved&&!ledger.scope);
 wrong=f;wrong.entry.history_complete=false;
 assert(!RecoveryStagePrimaryOpening(s,cfg,b,wrong,opening,ledger)&&s.phase==RR_NEW&&!opening.reserved);
 assert(RecoveryStagePrimaryOpening(s,cfg,b,f,opening,ledger));
 assert(s.cycle.actual_entry_ticks==400005 && s.position_ticket=={TICKET}ULL &&
        s.phase==RR_ARM_REVERSE && s.live_sl==399920 && s.initial_sl==399920 && RROppositeAnchor(s)==399925);
 assert(!opening.sl_required && opening.sl.kind==RR_NONE);
 assert(opening.reverse.kind==RR_REVERSE && opening.reverse.side==-1 &&
        opening.reverse.entry_ticks==399925 && opening.reverse.sl_ticks==400005);
 assert(ledger.strategy_slot==1 && ledger.confirmed_full_sl_count==0 && ledger.position_count==1);
 assert(!RecoveryStagePrimaryOpening(s,cfg,b,f,opening,ledger));
 new_range(-1,true);
 assert(reports({args(sell)}) && RecoveryStagePrimaryOpening(s,cfg,b,f,opening,ledger));
 assert(!opening.sl_required && opening.reverse.entry_ticks==400080 && opening.reverse.sl_ticks==400000);
 new_range(-1,false);
 assert(reports({args(partial)}) && f.entry.filled_steps==1 && f.entry.planned_steps==2 && f.entry.remainder_reconciled);
 assert(RecoveryStagePrimaryOpening(s,cfg,b,f,opening,ledger));
 assert(s.cycle.filled_steps==1 && s.cycle.actual_entry_ticks==399995 &&
        opening.reverse.entry_ticks==400075 && !opening.sl_required);
 new_range(1,false);
 assert(reports({args((buy[0],buy[1],buy[2].replace("3999.2000000000000000","0.0000000000000000")))}) &&
        f.sl_ticks==0 && RecoveryStagePrimaryOpening(s,cfg,b,f,opening,ledger));
 assert(s.live_sl==0 && s.initial_sl==399920 && opening.reverse.entry_ticks==399925 && !opening.sl_required);
 new_range(1,false);
 b.routed.serial++;
 assert(!reports({args(buy)}));
 b.routed=s.pending;
 b.verify_after=2;
 assert(!reports({args(buy)}));
 b.verify_after=1;
 assert(!reports({args((buy[0].replace("\thistory\t","\tcurrent\t"),*buy[1:]))}));
 assert(!reports({args((buy[0],buy[1].replace(str(ORDER)+"\t700\tBUY",str(ORDER+1)+"\t700\tBUY"),buy[2]))}));
 assert(!reports({args((buy[0],buy[1],buy[2].replace(str(TICKET),str(TICKET).replace("9","8",1)).replace("4000.050","4000.060")))}));
 new_range(-1,false);
 assert(!reports({args((partial[0],partial[1],partial[2].replace(HALF,VOL)))}));
 assert(!reports({args((partial[0],partial[1].replace(HALF,VOL),partial[2]))}));
}}
'''
        prefix = SHIM+'''
#include <cmath>
#include <climits>
using ulong=std::uint64_t;
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double n){return std::isfinite(n);}
double MathRound(double n){return std::round(n);}
double MathMax(double a,double b){return std::max(a,b);}
double MathAbs(double n){return std::abs(n);}
enum {ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5,ORDER_STATE_CANCELED=2,ORDER_STATE_FILLED=4,DEAL_ENTRY_IN=0};
template<class T> void ZeroMemory(T &out){out=T{};}
#include "RecoveryAlternatingFill.mqh"
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ("RecoveryCycle.mqh","RecoveryRangeCoordinator.mqh","RecoveryOpeningPair.mqh","RecoveryAlternatingFill.mqh"):
                (root/name).write_bytes((cycle/name).read_bytes())
            (root/"RecoveryCloseCheckpoint.mqh").write_text(checkpoint+"\n#endif\n")
            path=root/"primary.cpp"; exe=path.with_suffix("")
            path.write_text(prefix+body+main)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            "-fno-sanitize-recover=all",str(path),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
