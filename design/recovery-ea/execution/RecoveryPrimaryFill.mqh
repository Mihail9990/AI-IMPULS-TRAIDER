#ifndef RECOVERY_PRIMARY_FILL_MQH
#define RECOVERY_PRIMARY_FILL_MQH
#include "RecoveryAlternatingEvidence.mqh"

struct RecoveryPrimaryFillBinding {
 RecoveryExactReportScope exact;
 RecoveryHistoryScope history;
 RecoveryInventoryScope inventory;
 ulong scope,verify_after;
 RRAction routed; // RR_PRIMARY if still pending, RR_NONE if verified working
 double price_tick,volume_step;
};

bool RecoveryPrimaryFillWanted(const RRRangeState &s,const RRSettings &cfg,
    const RecoveryPrimaryFillBinding &b,RRKnownOrder &wanted) {
 if(!RRValid(s,cfg) || s.scope!=b.scope || s.scope==0 || s.cycle.position_id!=0 ||
    s.cycle.active_order!=0 || RCCurrentSlot(s.cycle)!=1 || s.cycle.last_closed_order!=0 ||
    s.cycle.last_closed_position!=0 || s.position_ticket!=0 || s.support_stage!=0 ||
    s.opposite.ticket!=0 || s.retire.ticket!=0 || s.reentry_slot!=0 ||
    !RecoveryAlternatingSameSession(b.exact.binding,b.history.binding) ||
    !RecoveryAlternatingSameSession(b.exact.binding,b.inventory) ||
    b.exact.binding.nonce==b.history.binding.nonce || b.exact.binding.nonce==b.inventory.nonce ||
    b.history.binding.nonce==b.inventory.nonce ||
    (ulong)b.exact.binding.generation!=s.cycle.generation ||
    b.exact.order_ticket==0 || b.exact.order_ticket!=b.history.anchor_order ||
    b.history.position_identifier==0) return false;
 RRAction expected;
 RRClearAction(expected);
 if(!RRMakeEntry(cfg,s.primary_side,s.primary_ticks,1,expected))return false;
 if(s.phase==RR_NEW) {
    if(s.entry.ticket!=0 || b.routed.kind!=RR_PRIMARY || b.routed.serial==0 ||
       b.routed.serial!=s.action_serial ||
       b.routed.scope!=s.scope || b.routed.generation!=s.cycle.generation ||
       b.routed.range_id!=s.cycle.range_id || b.routed.order_ticket!=0 ||
       b.routed.position_id!=0 || b.routed.position_ticket!=0 || b.routed.stage!=0 ||
       b.routed.tp_ticks!=0 || !RRActionEqual(b.routed,s.pending))return false;
    RRSetKnown(wanted,b.exact.order_ticket,b.routed);
 } else if(s.phase==RR_WAIT) {
    if(b.routed.kind!=RR_NONE || s.pending.kind!=RR_NONE || s.entry.ticket!=b.exact.order_ticket)
       return false;
    wanted=s.entry;
 } else return false;
 return wanted.side==expected.side && wanted.entry_ticks==expected.entry_ticks &&
        wanted.sl_ticks==expected.sl_ticks && wanted.volume_steps==expected.volume_steps;
}

bool RecoveryPrimaryFillTerms(const string type_token,const string state_token,const string initial_token,
    const string remainder_token,const string price_token,const string sl_token,const string tp_token,
    const RRKnownOrder &wanted,const RecoveryPrimaryFillBinding &b,ulong &terminal_state,long &remaining) {
 ulong order_type;
 long initial,price,sl,tp;
 return RecoveryAdoptionUnsigned(type_token,order_type) &&
        order_type==(ulong)(wanted.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) &&
        RecoveryAdoptionUnsigned(state_token,terminal_state) &&
        (terminal_state==(ulong)ORDER_STATE_FILLED || terminal_state==(ulong)ORDER_STATE_CANCELED) &&
        RecoveryAdoptionUnits(initial_token,b.volume_step,false,initial) && initial==wanted.volume_steps &&
        RecoveryAdoptionUnits(remainder_token,b.volume_step,true,remaining) &&
        (terminal_state==(ulong)ORDER_STATE_FILLED ? remaining==0 : remaining>0 && remaining<initial) &&
        RecoveryAdoptionUnits(price_token,b.price_tick,false,price) && price==wanted.entry_ticks &&
        RecoveryAdoptionUnits(sl_token,b.price_tick,false,sl) && sl==wanted.sl_ticks &&
        RecoveryAdoptionUnits(tp_token,b.price_tick,true,tp) && tp==0;
}

// Internal fold: use only reports independently loaded through the typed
// scope/epoch/nonce decoders. FILE/ACK/transaction hints are not evidence.
bool RecoveryPrimaryFillFold(const RRRangeState &s,const RRSettings &cfg,
    const RecoveryPrimaryFillBinding &b,const RecoveryExactReport &exact,
    const RecoveryHistorySnapshot &history,const RecoveryInventorySnapshot &inventory,RRFilled &out) {
 ZeroMemory(out);
 RRKnownOrder wanted;
 if(!RecoveryPrimaryFillWanted(s,cfg,b,wanted) || !exact.usable || !exact.observed ||
    exact.source!="history" || exact.reason!="ok" || exact.sequence<=0 ||
    (ulong)exact.sequence<=b.verify_after || exact.position_identifier!=(string)b.history.position_identifier ||
    !history.usable || !history.observed || history.reason!="ok" || history.sequence<=exact.sequence ||
    history.order_count!=1 || history.deal_count<1 || history.deal_count>128 ||
    ArraySize(history.rows)!=1+history.deal_count || !inventory.usable || !inventory.complete ||
    inventory.reason!="ok" || inventory.sequence<=history.sequence || inventory.position_count<1 ||
    inventory.position_count>128 || inventory.order_count<0 || inventory.order_count>128 ||
    ArraySize(inventory.rows)!=inventory.position_count+inventory.order_count)
    return false;
 const string magic=(string)b.exact.binding.magic,anchor=(string)wanted.ticket;
 const string side=wanted.side==1 ? "BUY" : "SELL",position=exact.position_identifier;
 string f[];
 ulong order_state,history_state,done;
 long remaining,history_remaining,stoplimit;
 if(StringSplit(exact.row,9,f)!=12 || f[0]!="E" || f[1]!="history" ||
    f[2]!=position || f[3]!=magic ||
    !RecoveryPrimaryFillTerms(f[4],f[5],f[6],f[7],f[8],f[9],f[10],wanted,b,order_state,remaining) ||
    !RecoveryAdoptionUnits(f[11],b.price_tick,true,stoplimit) || stoplimit!=0 ||
    StringSplit(history.rows[0],9,f)!=11 || f[0]!="H" || f[1]!=anchor || f[2]!=magic ||
    !RecoveryAdoptionUnsigned(f[5],done) || done==0 ||
    !RecoveryPrimaryFillTerms(f[3],f[4],f[6],f[7],f[8],f[9],f[10],wanted,b,history_state,history_remaining) ||
    history_state!=order_state || history_remaining!=remaining) return false;
 const long required=wanted.volume_steps-remaining;

 ulong position_ticket=0;
 long actual=0,live_sl=0;
 for(int i=0;i<inventory.position_count;i++) {
    if(StringSplit(inventory.rows[i],9,f)!=9 || f[0]!="P")return false;
    if(f[1]!=position)continue;
    long volume,tp;
    if(position_ticket!=0 || !RecoveryAdoptionUnsigned(f[2],position_ticket) || position_ticket==0 ||
       f[3]!=magic || f[4]!=side || !RecoveryAdoptionUnits(f[5],b.volume_step,false,volume) ||
       volume!=required || !RecoveryAdoptionUnits(f[6],b.price_tick,false,actual) ||
       !RecoveryAdoptionUnits(f[7],b.price_tick,true,live_sl) ||
       !RecoveryAdoptionUnits(f[8],b.price_tick,true,tp) || tp!=0)return false;
 }
 if(position_ticket==0)return false;
 for(int i=inventory.position_count;i<ArraySize(inventory.rows);i++) {
    if(StringSplit(inventory.rows[i],9,f)!=12 || f[0]!="O" || f[1]==anchor || f[3]==position)
       return false;
 }

 long filled=0,weighted_difference=0;
 for(int i=1;i<ArraySize(history.rows);i++) {
    ulong entry;
    long steps,ticks;
    if(StringSplit(history.rows[i],9,f)!=12 || f[0]!="D" || f[2]!=anchor || f[3]!=magic ||
       f[4]!=side || !RecoveryAdoptionUnsigned(f[5],entry) || entry!=(ulong)DEAL_ENTRY_IN ||
       !RecoveryAdoptionUnits(f[8],b.volume_step,false,steps) || steps>required-filled ||
       !RecoveryAdoptionUnits(f[9],b.price_tick,false,ticks))return false;
    filled+=steps;
    const long diff=ticks-actual;
    if(diff>LONG_MAX/steps || diff<(-LONG_MAX)/steps)return false;
    const long part=diff*steps;
    if((part>0 && weighted_difference>LONG_MAX-part) ||
       (part<0 && weighted_difference<(-LONG_MAX)-part))return false;
    weighted_difference+=part;
 }
 if(filled!=required || weighted_difference!=0)return false;
 out.position_ticket=position_ticket;out.sl_ticks=live_sl;
 out.entry.generation=s.cycle.generation;out.entry.range_id=s.cycle.range_id;
 out.entry.expected_order=wanted.ticket;out.entry.executed_order=wanted.ticket;
 out.entry.position_id=b.history.position_identifier;out.entry.side=wanted.side;
 out.entry.planned_steps=wanted.volume_steps;out.entry.filled_steps=filled;
 out.entry.active_steps=filled;out.entry.actual_entry_ticks=actual;
 out.entry.history_complete=true;out.entry.remainder_reconciled=true;
 out.entry.independently_verified=true;out.independently_verified=true;
 return true;
}

bool RecoveryReadPrimaryFill(const RRRangeState &s,const RRSettings &cfg,
                             const RecoveryPrimaryFillBinding &b,RRFilled &out) {
 ZeroMemory(out);
 RRKnownOrder target;
 if(!RecoveryPrimaryFillWanted(s,cfg,b,target))return false;
 RecoveryExactReport exact;
 RecoveryHistorySnapshot history;
 RecoveryInventorySnapshot inventory;
 return RecoveryExactReportRead(b.exact,exact) && RecoveryHistoryRead(b.history,history) &&
        RecoveryInvRead(b.inventory,inventory) && RecoveryPrimaryFillFold(s,cfg,b,exact,history,inventory,out);
}

// Atomic RAM-only transition. Caller passes only a normalized independent
// fill from the evidence gate above; the flags are not an authentication mechanism.
bool RecoveryStagePrimaryOpening(RRRangeState &s,const RRSettings &cfg,
    const RecoveryPrimaryFillBinding &b,const RRFilled &f,RROpeningPair &opening,
    RRAlternationLedger &ledger) {
 RRKnownOrder wanted;
 if(opening.reserved || ledger.scope!=0 || !RecoveryPrimaryFillWanted(s,cfg,b,wanted) ||
    f.entry.expected_order!=wanted.ticket || f.entry.executed_order!=wanted.ticket ||
    f.entry.position_id!=b.history.position_identifier) return false;
 RRRangeState next=s;
 RROpeningPair pair;
 pair.reserved=false;
 RRAlternationLedger chain;
 ZeroMemory(chain);
 if(next.phase==RR_NEW) {
    if(!f.independently_verified || !f.entry.independently_verified ||
       !f.entry.history_complete || !f.entry.remainder_reconciled)return false;
    RREffect filled;
    filled.action=b.routed;filled.created_order=wanted.ticket;filled.terminal_verified=true;
    if(!RRConfirm(next,filled))return false;
 }
 // The order's original protective SL and current position SL are independent
 // facts. The next ordinary order is priced only from the actual fill.
 if(next.phase!=RR_WAIT || !RRKnownEqual(next.entry,wanted) ||
    next.pending.kind!=RR_NONE || f.working_other_ticket!=0 ||
    !f.independently_verified || !f.entry.independently_verified ||
    !f.entry.history_complete || !f.entry.remainder_reconciled ||
    f.position_ticket==0 || f.sl_ticks<0 || f.sl_ticks>RC_MAX_UNITS || f.tp_ticks!=0 ||
    f.entry.side!=wanted.side || f.entry.planned_steps!=wanted.volume_steps ||
    !RCBoundedPositive(f.entry.actual_entry_ticks) || !RCApplyEntry(next.cycle,f.entry))return false;
 next.position_ticket=f.position_ticket;next.initial_sl=wanted.sl_ticks;
 next.live_sl=f.sl_ticks;next.live_tp=f.tp_ticks;
 next.support_stage=0;RRClearKnown(next.entry);
 if(!RRAfterActualEntryNoSL(next,cfg) || !RRStageOpeningPair(next,cfg,pair) ||
    pair.sl_required || !RRStartAlternation(next,0,chain))return false;
 s=next;opening=pair;ledger=chain;
 return true;
}
#endif
