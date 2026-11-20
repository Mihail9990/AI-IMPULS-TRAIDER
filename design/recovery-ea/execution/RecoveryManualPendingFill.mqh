#ifndef RECOVERY_MANUAL_PENDING_FILL_MQH
#define RECOVERY_MANUAL_PENDING_FILL_MQH
#include "RecoveryManualPending.mqh"
#include "RecoveryManualHandoff.mqh"

enum RecoveryManualPendingFillState { RMPF_UNUSED=0,RMPF_TERMINAL,RMPF_HISTORY_REQUEST,
 RMPF_HISTORY_REPLY,RMPF_INVENTORY_REQUEST,RMPF_INVENTORY_REPLY,RMPF_DONE,RMPF_UNKNOWN };
enum RecoveryManualPendingFillResult { RMPF_WAIT=0,RMPF_PROGRESS,RMPF_PROOF,RMPF_UNRESOLVED };
struct RecoveryManualPendingFill {
 RecoveryManualPendingFillState state;
 RecoveryManualPendingCandidate original;
 long target_sl,observed_sl,remaining;
 ulong terminal_state,verified_sequence;
 double tick,step;
 RecoveryHistoryScope history;
 RecoveryInventoryScope inventory;
 string tag;
 RecoveryHistorySnapshot facts;
 RRFilled proof;
};

bool RecoveryManualPendingFillTerms(const RecoveryManualPendingFill &p,const string type,
    const string state,const string initial,const string remainder,const string price,
    const string sl,const string tp,ulong &terminal,long &residual,long &stop) {
 ulong kind;
 long full,level,target;
 return RecoveryAdoptionUnsigned(type,kind) &&
    kind==(ulong)(p.original.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) &&
    RecoveryAdoptionUnsigned(state,terminal) &&
    (terminal==(ulong)ORDER_STATE_FILLED || terminal==(ulong)ORDER_STATE_CANCELED) &&
    RecoveryAdoptionUnits(initial,p.step,false,full) && full==p.original.volume_steps &&
    RecoveryAdoptionUnits(remainder,p.step,true,residual) &&
    (terminal==(ulong)ORDER_STATE_FILLED ? residual==0 : residual>0 && residual<full) &&
    RecoveryAdoptionUnits(price,p.tick,false,level) && level==p.original.entry_ticks &&
    RecoveryAdoptionUnits(sl,p.tick,true,stop) && (stop==p.original.sl_ticks || stop==p.target_sl) &&
    RecoveryAdoptionUnits(tp,p.tick,true,target) && target==p.original.tp_ticks;
}

bool RecoveryManualPendingFillBegin(RecoveryManualPendingFill &p,const RecoveryManualPendingOwner &o,
    const RecoveryInventoryScope &history,const RecoveryInventoryScope &inventory,const string tag) {
 const RecoveryManualPendingCandidate a=o.original;
 if(p.state!=RMPF_UNUSED || (o.phase!=RMP_READY && o.phase!=RMP_ATTEMPTED && o.phase!=RMP_PREPARED) ||
    a.ticket==0 || a.sequence==0 || a.order_magic!=0 || !RCValidSide(a.side) ||
    !RCBoundedPositive(a.entry_ticks) || !RCBoundedPositive(a.volume_steps) ||
    a.sl_ticks<0 || a.sl_ticks>RC_MAX_UNITS || a.tp_ticks<0 || a.tp_ticks>RC_MAX_UNITS ||
    !RCBoundedPositive(o.target_sl) || (a.sl_ticks!=0 && a.sl_ticks!=o.target_sl) ||
    !MathIsValidNumber(o.price_tick) || o.price_tick<=0.0 ||
    !MathIsValidNumber(o.volume_step) || o.volume_step<=0.0 ||
    !RecoveryInvScopeOK(a.request) || !RecoveryInvScopeOK(history) || !RecoveryInvScopeOK(inventory) ||
    !RecoveryAlternatingSameSession(a.request,history) ||
    !RecoveryAlternatingSameSession(history,inventory) ||
    a.request.nonce==history.nonce || a.request.nonce==inventory.nonce ||
    history.nonce==inventory.nonce || !RecoveryInvHex(tag))return false;
 p.original=a;p.target_sl=o.target_sl;p.tick=o.price_tick;p.step=o.volume_step;
 p.history.binding=history;p.history.anchor_order=a.ticket;p.history.position_identifier=0;
 p.inventory=inventory;p.tag=tag;p.state=RMPF_TERMINAL;
 return true;
}

bool RecoveryManualPendingFillCurrent(const RecoveryManualPendingFill &p,const RecoveryManualPendingOwner &o) {
 const RecoveryManualPendingCandidate a=o.original;
 return (o.phase==RMP_READY || o.phase==RMP_ATTEMPTED || o.phase==RMP_PREPARED) &&
    o.original.request.nonce==p.original.request.nonce &&
    RecoveryAlternatingSameSession(o.original.request,p.original.request) &&
    a.sequence==p.original.sequence && a.ticket==p.original.ticket &&
    a.order_magic==p.original.order_magic && a.side==p.original.side &&
    a.entry_ticks==p.original.entry_ticks && a.sl_ticks==p.original.sl_ticks &&
    a.tp_ticks==p.original.tp_ticks && a.volume_steps==p.original.volume_steps &&
    o.target_sl==p.target_sl && o.price_tick==p.tick && o.volume_step==p.step &&
    RecoveryAlternatingPumpSession(p.inventory);
}

// The helper samples a specified exact ticket, with equality on magic=0, not a wildcard.
// This local view is only an advisory for the independent history request.
bool RecoveryManualPendingFillLocal(RecoveryManualPendingFill &p) {
 RecoveryExactOrderScope scope;
 scope.login=p.inventory.login;scope.server=p.inventory.server;scope.symbol=p.inventory.symbol;
 scope.magic=p.original.order_magic;scope.ticket=p.original.ticket;
 RecoveryExactOrderObservation a,b;
 if(RecoveryExactOrderSample(scope,a)!=RECOVERY_EXACT_ORDER_OBSERVED ||
    a.source!=RECOVERY_EXACT_ORDER_HISTORY || a.position_identifier==0 || a.stop_limit!=0.0 ||
    RecoveryExactOrderSample(scope,b)!=RECOVERY_EXACT_ORDER_OBSERVED ||
    !RecoveryExactOrderSame(a,b))return false;
 ulong terminal;
 long remainder,stop;
 if(!RecoveryManualPendingFillTerms(p,(string)a.type,(string)a.state,DoubleToString(a.initial_volume,16),
    DoubleToString(a.remaining_volume,16),DoubleToString(a.price,16),DoubleToString(a.sl,16),
    DoubleToString(a.tp,16),terminal,remainder,stop))return false;
 p.history.position_identifier=a.position_identifier;p.terminal_state=terminal;
 p.remaining=remainder;p.observed_sl=stop;
 return true;
}

bool RecoveryManualPendingFillFold(const RecoveryManualPendingFill &p,const ulong generation,
    const RecoveryHistorySnapshot &h,const RecoveryInventorySnapshot &i,RRFilled &out) {
 ZeroMemory(out);
 if(generation==0 || generation!=(ulong)p.inventory.generation || p.original.ticket==0 ||
    p.history.anchor_order!=p.original.ticket || p.history.position_identifier==0 ||
    !h.usable || !h.observed || h.reason!="ok" || h.sequence<=0 ||
    (ulong)h.sequence<=p.original.sequence || h.order_count!=1 || h.deal_count<1 || h.deal_count>128 ||
    ArraySize(h.rows)!=1+h.deal_count || !i.usable || !i.complete || i.reason!="ok" ||
    i.sequence<=h.sequence || i.position_count!=1 || i.order_count<0 || i.order_count>128 ||
    ArraySize(i.rows)!=i.position_count+i.order_count)return false;
 const string magic=(string)p.original.order_magic,anchor=(string)p.original.ticket;
 const string position=(string)p.history.position_identifier,side=p.original.side==1 ? "BUY" : "SELL";
 string f[];
 ulong order_state,done,position_ticket;
 long remaining,stop,actual,live_sl,live_tp,volume;
 if(StringSplit(h.rows[0],9,f)!=11 || f[0]!="H" || f[1]!=anchor || f[2]!=magic ||
    !RecoveryAdoptionUnsigned(f[5],done) || done==0 ||
    !RecoveryManualPendingFillTerms(p,f[3],f[4],f[6],f[7],f[8],f[9],f[10],order_state,remaining,stop) ||
    order_state!=p.terminal_state || remaining!=p.remaining || stop!=p.observed_sl)return false;
 const long required=p.original.volume_steps-remaining;
 if(StringSplit(i.rows[0],9,f)!=9 || f[0]!="P" || f[1]!=position ||
    !RecoveryAdoptionUnsigned(f[2],position_ticket) || position_ticket==0 ||
    f[3]!=magic || f[4]!=side || !RecoveryAdoptionUnits(f[5],p.step,false,volume) || volume!=required ||
    !RecoveryAdoptionUnits(f[6],p.tick,false,actual) || !RecoveryAdoptionUnits(f[7],p.tick,true,live_sl) ||
    !RecoveryAdoptionUnits(f[8],p.tick,true,live_tp) || live_tp!=p.original.tp_ticks)return false;
 for(int j=1;j<ArraySize(i.rows);j++)
    if(StringSplit(i.rows[j],9,f)!=12 || f[0]!="O" || f[1]==anchor || f[3]==position)return false;
 long fill=0,weighted=0;
 for(int j=1;j<ArraySize(h.rows);j++) {
    ulong entry;
    long steps,ticks;
    if(StringSplit(h.rows[j],9,f)!=12 || f[0]!="D" || f[2]!=anchor || f[3]!=magic ||
       f[4]!=side || !RecoveryAdoptionUnsigned(f[5],entry) || entry!=(ulong)DEAL_ENTRY_IN ||
       !RecoveryAdoptionUnits(f[8],p.step,false,steps) || steps>required-fill ||
       !RecoveryAdoptionUnits(f[9],p.tick,false,ticks))return false;
    fill+=steps;
    const long diff=ticks-actual;
    if(diff>LONG_MAX/steps || diff<(-LONG_MAX)/steps)return false;
    const long part=diff*steps;
    if((part>0 && weighted>LONG_MAX-part) || (part<0 && weighted<(-LONG_MAX)-part))return false;
    weighted+=part;
 }
 if(fill!=required || weighted!=0)return false;
 out.position_ticket=position_ticket;out.sl_ticks=live_sl;out.tp_ticks=live_tp;
 out.entry.generation=generation;out.entry.range_id=1;
 out.entry.expected_order=p.original.ticket;out.entry.executed_order=p.original.ticket;
 out.entry.position_id=p.history.position_identifier;out.entry.side=p.original.side;
 out.entry.planned_steps=p.original.volume_steps;out.entry.filled_steps=fill;
 out.entry.active_steps=fill;out.entry.actual_entry_ticks=actual;
 out.entry.history_complete=true;out.entry.remainder_reconciled=true;
 out.entry.independently_verified=true;out.independently_verified=true;
 return true;
}

RecoveryManualPendingFillResult RecoveryManualPendingFillStop(RecoveryManualPendingFill &p) {
 p.state=RMPF_UNKNOWN;
 return RMPF_UNRESOLVED;
}

RecoveryManualPendingFillResult RecoveryManualPendingFillStep(RecoveryManualPendingFill &p,
    const RecoveryManualPendingOwner &o,const ulong generation) {
 if(p.state==RMPF_DONE)return RMPF_WAIT;
 if(p.state==RMPF_UNUSED || p.state==RMPF_UNKNOWN)return RMPF_UNRESOLVED;
 if(!RecoveryManualPendingFillCurrent(p,o) || generation!=(ulong)p.inventory.generation)
    return RecoveryManualPendingFillStop(p);
 switch(p.state) {
    case RMPF_TERMINAL:
       if(!RecoveryManualPendingFillLocal(p))return RMPF_WAIT;
       p.state=RMPF_HISTORY_REQUEST;return RMPF_PROGRESS;
    case RMPF_HISTORY_REQUEST:
       p.state=RMPF_HISTORY_REPLY;
       if(!RecoveryHistoryRequest(p.history,p.tag))return RecoveryManualPendingFillStop(p);
       return RMPF_PROGRESS;
    case RMPF_HISTORY_REPLY:
       if(!RecoveryHistoryRead(p.history,p.facts))return RMPF_WAIT;
       if(!p.facts.usable || !p.facts.observed || p.facts.reason!="ok")
          return RecoveryManualPendingFillStop(p);
       p.state=RMPF_INVENTORY_REQUEST;return RMPF_PROGRESS;
    case RMPF_INVENTORY_REQUEST: {
       const RecoveryInventoryScope a=p.inventory;
       p.state=RMPF_INVENTORY_REPLY;
       if(!RecoveryRequestInventory(a.nonce,a.executor_epoch,a.verifier_epoch,a.generation,
                                    a.login,a.server,a.symbol,a.magic))return RecoveryManualPendingFillStop(p);
       return RMPF_PROGRESS;
    }
    case RMPF_INVENTORY_REPLY: {
       RecoveryInventorySnapshot a;
       if(!RecoveryInvRead(p.inventory,a))return RMPF_WAIT;
       if(!RecoveryManualPendingFillFold(p,generation,p.facts,a,p.proof))
          return RecoveryManualPendingFillStop(p);
       p.verified_sequence=(ulong)a.sequence;p.state=RMPF_DONE;return RMPF_PROOF;
    }
    default:
       return RecoveryManualPendingFillStop(p);
 }
}

// RAM-only stage. An existing live SL/TP is never rewritten. A verified fast
// fill without prior or live SL stages initial protection from actual entry;
// independent opening confirmation still owns whether that SL took effect.
bool RecoveryManualPendingOpening(const RecoveryManualPendingFill &p,const RRSettings &cfg,
    RRRangeState &range,RROpeningPair &opening,RRAlternationLedger &ledger) {
 const RRFilled f=p.proof;
 if(p.state!=RMPF_DONE || range.scope!=0 || opening.reserved || ledger.scope!=0 ||
    f.sl_ticks<0 || f.sl_ticks>RC_MAX_UNITS ||
    !f.independently_verified || f.position_ticket==0 || f.working_other_ticket!=0 ||
    f.entry.expected_order!=p.original.ticket || f.entry.executed_order!=p.original.ticket ||
    f.entry.position_id!=p.history.position_identifier || f.entry.side!=p.original.side ||
    f.entry.planned_steps!=p.original.volume_steps || f.tp_ticks!=p.original.tp_ticks)return false;
 RRRangeState next;
 ZeroMemory(next);
 if(!RRNewRange(next,cfg,1,p.original.side,p.original.entry_ticks) ||
    !RCApplyEntry(next.cycle,f.entry))return false;
 const bool needs_stop=f.sl_ticks==0;
 if(needs_stop && (p.original.sl_ticks!=0 || p.observed_sl!=0))return false;
 next.position_ticket=f.position_ticket;
 next.initial_sl=p.observed_sl>0 ? p.observed_sl :
    (needs_stop ? f.entry.actual_entry_ticks-f.entry.side*cfg.initial_sl_ticks : f.sl_ticks);
 next.live_sl=f.sl_ticks;next.live_tp=f.tp_ticks;
 RROpeningPair pair;
 ZeroMemory(pair);
 RRAlternationLedger chain;
 ZeroMemory(chain);
 if(needs_stop) next.phase=RR_PROTECT;
 else if(!RRAfterActualEntryNoSL(next,cfg))return false;
 if(!RRStageOpeningPair(next,cfg,pair) || pair.sl_required!=needs_stop ||
    !RRStartAlternation(next,0,chain))return false;
 range=next;opening=pair;ledger=chain;
 return true;
}

// Private ordinary owner projection. Public panel mode and actual dispatch
// remain caller-owned. Call once, before dispatching opening.reverse.
bool RecoveryProjectManualPendingOpening(RecoveryManualHandoff &out,const RecoveryManualPendingFill &p,
    const RecoveryManualPendingOwner &origin,const RecoveryPanelControl &public_panel,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RRSettings &cfg) {
 if(out.seeded || public_panel.configured[0] || public_panel.configured[1] ||
    !RecoveryManualPendingFillCurrent(p,origin) ||
    !RecoveryManualScope(life,live,scope,p.inventory) ||
    origin.scope!=scope.scope || origin.generation!=scope.generation ||
    origin.executor_epoch!=scope.executor_epoch || cfg.scope!=scope.scope ||
    cfg.generation!=scope.generation)return false;
 RecoveryManualHandoff next;
 ZeroMemory(next);
 if(!RecoveryManualPendingOpening(p,cfg,next.backing.ranges[0],next.ordinary.opening,next.ordinary.ledger))
    return false;
 next.first_position_id=p.proof.entry.position_id;next.first_position_ticket=p.proof.position_ticket;
 next.first_position_magic=p.original.order_magic;
 next.backing.mode=REC_CONTROL_RANGES;next.backing.settings=cfg;next.backing.configured[0]=true;
 next.ordinary.phase=RRO_ORDINARY;next.ordinary.range_id=1;next.ordinary.revision=1;
 next.ordinary.scope=scope.scope;next.ordinary.generation=scope.generation;next.ordinary.executor_epoch=scope.executor_epoch;
 if(!RecoveryRangeOwnerBound(next.ordinary,next.backing,life,live,scope))return false;
 next.seeded=true;out=next;
 return true;
}

// A single fresh independent inventory can verify both legs of the staged
// fast-fill protection pair. It cannot prove a fast execution by absence.
bool RecoveryManualPendingConfirmPair(RecoveryManualHandoff &handoff,
    const RecoveryManualPendingFill &p,const RecoverySessionLifecycle &life,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &request,const RecoveryInventorySnapshot &snapshot) {
 if(!handoff.seeded || p.state!=RMPF_DONE || p.verified_sequence==0 ||
    request.nonce==p.original.request.nonce || request.nonce==p.history.binding.nonce ||
    request.nonce==p.inventory.nonce || !RecoveryManualScope(life,live,scope,request) ||
    !RecoveryAlternatingSameSession(request,p.inventory) ||
    !RecoveryRangeOwnerBound(handoff.ordinary,handoff.backing,life,live,scope) ||
    snapshot.position_count!=1 || snapshot.reason!="ok" ||
    !RecoveryManualPendingInventoryOK(snapshot,p.verified_sequence))return false;
 const ulong exact=handoff.ordinary.next_exact_ticket;
 RRRangeState range=handoff.backing.ranges[0];
 RROpeningPair pair=handoff.ordinary.opening;
 if(exact==0 || !pair.sl_required || !RROpeningBound(range,pair) ||
    !RRAltBound(range,handoff.ordinary.ledger) ||
    range.cycle.active_order!=p.original.ticket || range.cycle.position_id!=p.proof.entry.position_id ||
    range.position_ticket!=p.proof.position_ticket || range.live_tp!=p.proof.tp_ticks ||
    handoff.first_position_magic!=p.original.order_magic || handoff.ordinary.revision==ULONG_MAX)return false;
 RecoveryIntentKey key;
 string recorded;
 if(!RecoveryBuildSessionIntentKey(scope,pair.reverse,p.tick,p.step,key) ||
    RecoveryIntentLookup(key,recorded)!=RI_TICKET || recorded!=(string)exact)return false;
 string f[];
 ulong magic;
 long units;
 if(StringSplit(snapshot.rows[0],9,f)!=9 || !RecoveryInvPosition(f) ||
    f[1]!=(string)range.cycle.position_id || f[2]!=(string)range.position_ticket ||
    !RecoveryAdoptionUnsigned(f[3],magic) || magic!=p.original.order_magic ||
    f[4]!=(range.cycle.side==1?"BUY":"SELL") ||
    !RecoveryAdoptionUnits(f[5],p.step,false,units) || units!=range.cycle.filled_steps ||
    !RecoveryAdoptionUnits(f[6],p.tick,false,units) || units!=range.cycle.actual_entry_ticks ||
    !RecoveryAdoptionUnits(f[7],p.tick,false,units) || units!=pair.sl.sl_ticks ||
    !RecoveryAdoptionUnits(f[8],p.tick,true,units) || units!=pair.sl.tp_ticks)return false;
 bool found=false;
 for(int j=1;j<ArraySize(snapshot.rows);j++) {
    if(StringSplit(snapshot.rows[j],9,f)!=12 || !RecoveryInvOrder(f))return false;
    if(f[1]!=(string)exact)continue;
    if(found || f[2]!=(string)request.magic || f[3]!="0" ||
       f[4]!=(string)(pair.reverse.side==1?ORDER_TYPE_BUY_STOP:ORDER_TYPE_SELL_STOP) ||
       f[5]!=(string)ORDER_STATE_PLACED ||
       !RecoveryAdoptionUnits(f[6],p.step,false,units) || units!=pair.reverse.volume_steps ||
       !RecoveryAdoptionUnits(f[7],p.step,false,units) || units!=pair.reverse.volume_steps ||
       !RecoveryAdoptionUnits(f[8],p.tick,false,units) || units!=pair.reverse.entry_ticks ||
       !RecoveryAdoptionUnits(f[9],p.tick,false,units) || units!=pair.reverse.sl_ticks ||
       !RecoveryAdoptionUnits(f[10],p.tick,true,units) || units!=pair.reverse.tp_ticks ||
       !RecoveryAdoptionUnits(f[11],p.tick,true,units) || units!=0)return false;
    found=true;
 }
 if(!found)return false;
 RREffect sl;
 sl.action=pair.sl;sl.terminal_verified=true;sl.created_order=0;
 RREffect reverse;
 reverse.action=pair.reverse;reverse.terminal_verified=true;reverse.created_order=exact;
 if(RRConfirmOpeningPair(range,pair,sl)!=RR_PAIR_WAITING ||
    RRConfirmOpeningPair(range,pair,reverse)!=RR_PAIR_CONFIRMED)return false;
 int last=handoff.ordinary.ledger.position_count-1;
 handoff.ordinary.ledger.positions[last].last_confirmed_sl=range.live_sl;
 handoff.ordinary.ledger.positions[last].confirmed_initial_sl=range.live_sl;
 handoff.backing.ranges[0]=range;handoff.ordinary.opening=pair;handoff.ordinary.revision++;
 return true;
}
#endif
