#ifndef RECOVERY_ALTERNATING_EVIDENCE_MQH
#define RECOVERY_ALTERNATING_EVIDENCE_MQH
#include "../cycle/RecoveryAlternatingFill.mqh"
#include "../verifier/RecoveryExactOrderReader.mqh"
#include "../verifier/RecoveryHistoryReader.mqh"
#include "RecoveryAdoptionInventory.mqh"

struct RecoveryAlternatingEvidenceBinding {
 RecoveryExactReportScope exact;
 RecoveryHistoryScope history;
 RecoveryInventoryScope inventory;
 ulong scope,verify_after;
 RRAction routed; // exact current opening pair intent; NONE if already verified
 double price_tick,volume_step;
};

bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b) {
 return RecoveryInvScopeOK(a) && RecoveryInvScopeOK(b) && a.verifier_chart==b.verifier_chart &&
        a.executor_epoch==b.executor_epoch && a.verifier_epoch==b.verifier_epoch &&
        a.login==b.login && a.server==b.server && a.symbol==b.symbol &&
        a.magic==b.magic && a.generation==b.generation;
}

bool RecoveryAlternatingWanted(const RRRangeState &s,const RROpeningPair &opening,
      const RecoveryAlternatingEvidenceBinding &b,RRKnownOrder &wanted) {
 if(s.scope==0 || s.scope!=b.scope || !RCValidSide(s.cycle.side) ||
    s.cycle.position_id==0 || s.cycle.active_order==0 || s.position_ticket==0 ||
    s.entry.ticket!=0 || s.retire.ticket!=0 || s.reentry_slot!=0 ||
    !RecoveryAlternatingSameSession(b.exact.binding,b.history.binding) ||
    !RecoveryAlternatingSameSession(b.history.binding,b.inventory) ||
    b.exact.binding.nonce==b.history.binding.nonce || b.exact.binding.nonce==b.inventory.nonce ||
    b.history.binding.nonce==b.inventory.nonce || b.exact.binding.generation<=0 ||
    (ulong)b.exact.binding.generation!=s.cycle.generation ||
    b.exact.order_ticket==0 || b.exact.order_ticket!=b.history.anchor_order ||
    b.exact.order_ticket==s.cycle.active_order || b.history.position_identifier==0 ||
    b.history.position_identifier==s.cycle.position_id) return false;
 if(opening.reserved) {
    if(!RROpeningBound(s,opening) || b.routed.kind!=RR_REVERSE ||
       !RRActionEqual(b.routed,opening.reverse) ||
       (opening.reverse_verified && opening.reverse_ticket!=b.exact.order_ticket) ||
       b.routed.tp_ticks!=0) return false;
    wanted.ticket=b.exact.order_ticket; wanted.side=b.routed.side;
    wanted.entry_ticks=b.routed.entry_ticks; wanted.sl_ticks=b.routed.sl_ticks;
    wanted.volume_steps=b.routed.volume_steps;
 } else {
    if(b.routed.kind!=RR_NONE || s.phase!=RR_ACTIVE ||
       (s.pending.kind!=RR_NONE && s.pending.kind!=RR_SUPPORT_SL) ||
       s.opposite.ticket!=b.exact.order_ticket) return false;
    wanted=s.opposite;
 }
 return wanted.side==-s.cycle.side && wanted.entry_ticks==RROppositeAnchor(s) &&
        RCBoundedPositive(wanted.entry_ticks) && RCBoundedPositive(wanted.sl_ticks) &&
        RCBoundedPositive(wanted.volume_steps);
}

bool RecoveryAlternatingOrderTerms(const string type_token,const string state_token,const string vol_token,
      const string remainder_token,const string entry_token,const string sl_token,const string tp_token,
      const RRKnownOrder &wanted,const RecoveryAlternatingEvidenceBinding &b) {
 ulong type,state;
 long vol,remainder,entry,sl,tp;
 return RecoveryAdoptionUnsigned(type_token,type) &&
        type==(ulong)(wanted.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) &&
        RecoveryAdoptionUnsigned(state_token,state) && state==(ulong)ORDER_STATE_FILLED &&
        RecoveryAdoptionUnits(vol_token,b.volume_step,false,vol) && vol==wanted.volume_steps &&
        RecoveryAdoptionUnits(remainder_token,b.volume_step,true,remainder) && remainder==0 &&
        RecoveryAdoptionUnits(entry_token,b.price_tick,false,entry) && entry==wanted.entry_ticks &&
        RecoveryAdoptionUnits(sl_token,b.price_tick,false,sl) && sl==wanted.sl_ticks &&
        RecoveryAdoptionUnits(tp_token,b.price_tick,true,tp) && tp==0;
}

bool RecoveryAlternatingEvidenceFold(const RRRangeState &s,const RROpeningPair &opening,
      const RecoveryAlternatingEvidenceBinding &b,const RecoveryExactReport &exact,
      const RecoveryHistorySnapshot &history,const RecoveryInventorySnapshot &inventory,
      RRAlternatingObservation &out) {
 // Reports must be received through the scope-checking readers below.
 ZeroMemory(out);
 RRKnownOrder wanted;
 if(!RecoveryAlternatingWanted(s,opening,b,wanted) ||
    !exact.usable || !exact.observed || exact.source!="history" || exact.sequence<=0 ||
    (ulong)exact.sequence<=b.verify_after ||
    exact.position_identifier!=(string)b.history.position_identifier ||
    !history.usable || !history.observed || history.sequence<=exact.sequence ||
    history.order_count!=1 || history.deal_count<1 || history.deal_count>128 ||
    ArraySize(history.rows)!=history.order_count+history.deal_count ||
    !inventory.usable || !inventory.complete || inventory.sequence<=history.sequence ||
    inventory.position_count<1 || inventory.position_count>128 ||
    inventory.order_count<0 || inventory.order_count>128 ||
    ArraySize(inventory.rows)!=inventory.position_count+inventory.order_count) return false;
 const string magic=(string)b.exact.binding.magic, anchor=(string)wanted.ticket;
 const string side_text=wanted.side==1 ? "BUY" : "SELL";
 const string new_id=exact.position_identifier, old_id=(string)s.cycle.position_id;
 const string old_ticket=(string)s.position_ticket;
 string f[];
 long zero;
 if(StringSplit(exact.row,9,f)!=12 || f[0]!="E" || f[1]!="history" || f[2]!=new_id ||
    f[3]!=magic || !RecoveryAlternatingOrderTerms(f[4],f[5],f[6],f[7],f[8],f[9],f[10],wanted,b) ||
    !RecoveryAdoptionUnits(f[11],b.price_tick,true,zero) || zero!=0) return false;
 ulong done;
 if(StringSplit(history.rows[0],9,f)!=11 || f[0]!="H" || f[1]!=anchor || f[2]!=magic ||
    !RecoveryAdoptionUnsigned(f[5],done) || done==0 ||
    !RecoveryAlternatingOrderTerms(f[3],f[4],f[6],f[7],f[8],f[9],f[10],wanted,b)) return false;

 ulong successor_ticket=0;
 long entry=0, observed_sl=0, predecessor_steps=0;
 bool predecessor_present=false;
 for(int i=0;i<inventory.position_count;i++) {
    if(StringSplit(inventory.rows[i],9,f)!=9 || f[0]!="P") return false;
    if(f[1]==new_id) {
       long vol,tp;
       if(successor_ticket!=0 || f[2]==old_ticket || !RecoveryAdoptionUnsigned(f[2],successor_ticket) ||
          successor_ticket==0 || f[3]!=magic || f[4]!=side_text ||
          !RecoveryAdoptionUnits(f[5],b.volume_step,false,vol) || vol!=wanted.volume_steps ||
          !RecoveryAdoptionUnits(f[6],b.price_tick,false,entry) ||
          !RecoveryAdoptionUnits(f[7],b.price_tick,true,observed_sl) ||
          !RecoveryAdoptionUnits(f[8],b.price_tick,true,tp) || tp!=0) return false;
    } else if(f[1]==old_id) {
       if(predecessor_present || f[2]!=old_ticket || f[4]==side_text ||
          !RecoveryAdoptionUnits(f[5],b.volume_step,false,predecessor_steps) ||
          predecessor_steps>s.cycle.filled_steps) return false;
       predecessor_present=true;
    } else if(f[2]==old_ticket) return false;
 }
 if(successor_ticket==0) return false;
 for(int i=inventory.position_count;i<ArraySize(inventory.rows);i++) {
    if(StringSplit(inventory.rows[i],9,f)!=12 || f[0]!="O" || f[1]==anchor || f[3]==new_id)
       return false;
 }

 long filled=0, weighted_delta=0;
 for(int i=1;i<ArraySize(history.rows);i++) {
    ulong entry_type;
    long volume,ticks;
    if(StringSplit(history.rows[i],9,f)!=12 || f[0]!="D" || f[2]!=anchor || f[3]!=magic ||
       f[4]!=side_text || !RecoveryAdoptionUnsigned(f[5],entry_type) || entry_type!=(ulong)DEAL_ENTRY_IN ||
       !RecoveryAdoptionUnits(f[8],b.volume_step,false,volume) ||
       volume>wanted.volume_steps-filled || !RecoveryAdoptionUnits(f[9],b.price_tick,false,ticks))
       return false;
    filled+=volume;
    // Compare the exact weighted fills to the grid price reported from the live
    // position. Bound products and sum without rounded averages or overflow.
    const long delta=ticks-entry;
    if(delta>LONG_MAX/volume || delta<(-LONG_MAX)/volume) return false;
    const long part=delta*volume;
    if((part>0 && weighted_delta>LONG_MAX-part) ||
       (part<0 && weighted_delta<(-LONG_MAX)-part)) return false;
    weighted_delta+=part;
 }
 if(filled!=wanted.volume_steps || weighted_delta!=0) return false;

 out.scope=b.scope;
 if(opening.reserved) { out.paired_intent=b.routed;out.paired_order_ticket=wanted.ticket; }
 out.executed_order=wanted;
 // The submitted SL is already proved by the E and H rows and belongs
 // to executed_order. The live position can have a different SL or none.
 out.fill.position_ticket=successor_ticket;out.fill.sl_ticks=observed_sl;
 out.fill.entry.generation=s.cycle.generation;out.fill.entry.range_id=s.cycle.range_id;
 out.fill.entry.expected_order=wanted.ticket;out.fill.entry.executed_order=wanted.ticket;
 out.fill.entry.position_id=b.history.position_identifier;out.fill.entry.side=wanted.side;
 out.fill.entry.planned_steps=wanted.volume_steps;out.fill.entry.filled_steps=filled;
 out.fill.entry.active_steps=filled;out.fill.entry.actual_entry_ticks=entry;
 out.fill.entry.history_complete=true;out.fill.entry.remainder_reconciled=true;
 out.fill.entry.independently_verified=true;out.fill.independently_verified=true;
 out.predecessor_inventory_complete=true;out.predecessor_present=predecessor_present;
 if(predecessor_present) {
    out.predecessor_position_id=s.cycle.position_id;out.predecessor_remaining_steps=predecessor_steps;
 }
 return true;
}

bool RecoveryReadAlternatingEvidence(const RRRangeState &s,const RROpeningPair &opening,
      const RecoveryAlternatingEvidenceBinding &b,RRAlternatingObservation &out) {
 ZeroMemory(out);
 RRKnownOrder wanted;
 if(!RecoveryAlternatingWanted(s,opening,b,wanted)) return false;
 RecoveryExactReport exact;
 RecoveryHistorySnapshot history;
 RecoveryInventorySnapshot inventory;
 return RecoveryExactReportRead(b.exact,exact) && RecoveryHistoryRead(b.history,history) &&
        RecoveryInvRead(b.inventory,inventory) &&
        RecoveryAlternatingEvidenceFold(s,opening,b,exact,history,inventory,out);
}
#endif
