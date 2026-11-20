#ifndef RECOVERY_DUAL_SUPPORT_PUMP_MQH
#define RECOVERY_DUAL_SUPPORT_PUMP_MQH
#include "RecoveryAlternatingPump.mqh"
#include "RecoverySessionIntentKey.mqh"
#include "../verifier/RecoveryExactOrder.mqh"

enum RecoveryDualSupportState {
 RDS_IDLE=0,RDS_LOCAL,RDS_REQ_ENTRY,RDS_READ_ENTRY,RDS_REQ_OPPOSITE,RDS_READ_OPPOSITE,
 RDS_REQ_HISTORY,RDS_READ_HISTORY,RDS_REQ_INVENTORY,RDS_READ_INVENTORY,RDS_DONE,RDS_STOP
};
enum RecoveryDualSupportResult {RDS_WAIT=0,RDS_PROGRESS,RDS_PROOF,RDS_COLLISION,RDS_UNKNOWN};
struct RecoveryDualSupportBinding {
 RecoveryExactReportScope entry,opposite;
 RecoveryHistoryScope history;
 RecoveryInventoryScope inventory;
 string publisher_tag;
 ulong verify_after;
 double price_tick,volume_step;
};
struct RecoveryDualSupportPump {
 RecoveryDualSupportState state;
 RecoveryDualSupportBinding bind;
 RRKnownOrder entry,opposite,winner,survivor;
 ulong scope,generation,action_serial;
 int range_id;
 long exact_sequence,verified_sequence;
 RecoveryHistorySnapshot hist;
};

bool RecoveryDualSupportRanges(const RRRangeState &s,const RRSettings &cfg) {
 return RRValid(s,cfg) && RRHasSupportAlternatives(s) && s.pending.kind==RR_NONE &&
        s.retire.ticket==0 && s.entry.ticket!=s.opposite.ticket && RCValidSide(s.reentry_side) &&
        s.cycle.position_id==0 && s.cycle.active_order==0 && s.position_ticket==0 &&
        s.entry.side==s.reentry_side && s.opposite.side==-s.reentry_side &&
        s.entry.entry_ticks==s.reentry_ticks && s.reentry_slot>=1 && s.reentry_slot<=24 &&
        s.entry.volume_steps==cfg.lot_steps[s.reentry_slot-1] && RCReverseSlot(s.cycle)!=0;
}
// Transport-only view when reentry filled before a PLACED report. It
// validates the exact unchanged staged intent and its RAM-journal ticket. No
// RRConfirm is invoked here and caller must not publish this as range state.
bool RecoveryDualPendingReentryView(const RRRangeState &range,const RRSettings &cfg,
    const RecoverySessionIntentScope &scope,const RRAction &action,const RecoveryIntentKey &recorded_key,
    const ulong ticket,const double tick,const double step,RRRangeState &view) {
 if(!RRValid(range,cfg) || range.phase!=RR_ARM_REENTRY || range.entry.ticket!=0 ||
    range.opposite.ticket==0 || range.opposite.ticket==ticket || ticket==0 ||
    scope.scope!=range.scope || scope.generation!=range.cycle.generation ||
    action.kind!=RR_REENTRY || !RRActionEqual(action,range.pending))return false;
 RecoveryIntentKey key;
 string stored_ticket;
 if(!RecoveryBuildSessionIntentKey(scope,action,tick,step,key) ||
    !RecoveryIntentKeysMatch(key,recorded_key) ||
    RecoveryIntentLookup(key,stored_ticket)!=RI_TICKET || stored_ticket!=(string)ticket)
    return false;
 RRRangeState transport=range;
 RRSetKnown(transport.entry,ticket,action);
 RRClearAction(transport.pending);
 transport.phase=RR_WAIT;
 if(!RecoveryDualSupportRanges(transport,cfg))return false;
 view=transport;
 return true;
}

bool RecoveryBeginDualSupport(RecoveryDualSupportPump &p,const RRRangeState &s,
    const RRSettings &cfg,const RecoveryDualSupportBinding &b) {
 if(p.state!=RDS_IDLE || !RecoveryDualSupportRanges(s,cfg) ||
    !RecoveryAlternatingSameSession(b.entry.binding,b.opposite.binding) ||
    !RecoveryAlternatingSameSession(b.entry.binding,b.history.binding) ||
    !RecoveryAlternatingSameSession(b.entry.binding,b.inventory) ||
    (ulong)b.entry.binding.generation!=s.cycle.generation ||
    b.entry.order_ticket!=s.entry.ticket || b.opposite.order_ticket!=s.opposite.ticket ||
    b.history.anchor_order!=0 || b.history.position_identifier!=0 ||
    !RecoveryInvHex(b.publisher_tag) || b.verify_after>(ulong)LONG_MAX-4 ||
    !MathIsValidNumber(b.price_tick) || !MathIsValidNumber(b.volume_step) ||
    b.price_tick<=0 || b.volume_step<=0) return false;
 string ids[5];
 ids[0]=b.entry.binding.nonce;ids[1]=b.opposite.binding.nonce;
 ids[2]=b.history.binding.nonce;ids[3]=b.inventory.nonce;ids[4]=b.publisher_tag;
 for(int i=0;i<5;i++)for(int j=0;j<i;j++)if(ids[i]==ids[j])return false;
 p.bind=b;p.entry=s.entry;p.opposite=s.opposite;p.scope=s.scope;
 p.generation=s.cycle.generation;p.range_id=s.cycle.range_id;p.action_serial=s.action_serial;
 p.state=RDS_LOCAL;return true;
}
bool RecoveryDualSupportCurrent(const RecoveryDualSupportPump &p,const RRRangeState &s,const RRSettings &cfg) {
 return RecoveryAlternatingPumpSession(p.bind.inventory) && RecoveryDualSupportRanges(s,cfg) &&
        s.scope==p.scope && s.cycle.generation==p.generation && s.cycle.range_id==p.range_id &&
        s.action_serial==p.action_serial && RRKnownEqual(s.entry,p.entry) && RRKnownEqual(s.opposite,p.opposite);
}
bool RecoveryDualLocalCandidate(const RecoveryExactReportScope &scope,const RRKnownOrder &order) {
 const RecoveryInventoryScope q=scope.binding;
 RecoveryExactOrderScope local;
 local.login=q.login;local.server=q.server;local.symbol=q.symbol;local.magic=(ulong)q.magic;local.ticket=order.ticket;
 RecoveryExactOrderObservation e;
 return RecoveryObserveExactOrder(local,e)==RECOVERY_EXACT_ORDER_OBSERVED &&
        e.source==RECOVERY_EXACT_ORDER_HISTORY && e.state==ORDER_STATE_FILLED &&
        e.ticket==order.ticket && e.position_identifier!=0;
}

// Common strict order terms for both the exact report and the inventory row.
// Historical fill requires zero remainder; survivor requires full remainder.
bool RecoveryDualTerms(const string type,const string state,const string volume,const string rest,
    const string price,const string sl,const string tp,const RRKnownOrder &order,
    const RecoveryDualSupportBinding &b,const bool filled) {
 ulong k,x;
 long n,r,v,l,t;
 return RecoveryAdoptionUnsigned(type,k) &&
        k==(ulong)(order.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) &&
        RecoveryAdoptionUnsigned(state,x) && x==(ulong)(filled ? ORDER_STATE_FILLED : ORDER_STATE_PLACED) &&
        RecoveryAdoptionUnits(volume,b.volume_step,false,n) && n==order.volume_steps &&
        RecoveryAdoptionUnits(rest,b.volume_step,filled,r) && r==(filled ? 0 : n) &&
        RecoveryAdoptionUnits(price,b.price_tick,false,v) && v==order.entry_ticks &&
        RecoveryAdoptionUnits(sl,b.price_tick,false,l) && l==order.sl_ticks &&
        RecoveryAdoptionUnits(tp,b.price_tick,true,t) && t==0;
}
enum RecoveryDualExactKind {RDE_UNKNOWN=0,RDE_WORKING,RDE_FILLED};
RecoveryDualExactKind RecoveryDualExact(const RecoveryExactReport &r,const RRKnownOrder &o,
    const long floor,const RecoveryDualSupportBinding &b,ulong &position) {
 position=0;
 if(!r.usable || !r.observed || r.reason!="ok" || r.sequence<=floor ||
    (r.source!="history" && r.source!="current"))return RDE_UNKNOWN;
 const bool filled=r.source=="history";
 string fields[];long stoplimit;
 ulong linked;
 if(StringSplit(r.row,9,fields)!=12 || fields[0]!="E" || fields[1]!=r.source ||
    fields[2]!=r.position_identifier || fields[3]!=(string)b.inventory.magic ||
    !RecoveryAdoptionUnsigned(fields[2],linked) || (filled ? linked==0 : linked!=0) ||
    !RecoveryDualTerms(fields[4],fields[5],fields[6],fields[7],fields[8],fields[9],fields[10],o,b,filled) ||
    !RecoveryAdoptionUnits(fields[11],b.price_tick,true,stoplimit) || stoplimit!=0) return RDE_UNKNOWN;
 position=linked;
 return filled ? RDE_FILLED : RDE_WORKING;
}

bool RecoveryDualSupportFold(const RecoveryDualSupportPump &p,const RecoveryInventorySnapshot &v,RRFilled &out) {
 ZeroMemory(out);
 if(!p.hist.usable || !p.hist.observed || p.hist.reason!="ok" || p.hist.sequence<=p.exact_sequence ||
    p.hist.order_count!=1 || p.hist.deal_count<1 || p.hist.deal_count>128 || ArraySize(p.hist.rows)!=1+p.hist.deal_count ||
    !v.usable || !v.complete || v.reason!="ok" || v.sequence<=p.hist.sequence ||
    v.position_count<1 || v.position_count>128 || v.order_count<1 || v.order_count>128 ||
    ArraySize(v.rows)!=v.position_count+v.order_count) return false;
 const string anchor=(string)p.winner.ticket, other=(string)p.survivor.ticket;
 const string id=(string)p.bind.history.position_identifier,magic=(string)p.bind.inventory.magic;
 const string side=p.winner.side==1?"BUY":"SELL";
 string f[];
 ulong executed_at;
 if(StringSplit(p.hist.rows[0],9,f)!=11 || f[0]!="H" || f[1]!=anchor || f[2]!=magic ||
    !RecoveryAdoptionUnsigned(f[5],executed_at) || executed_at==0 ||
    !RecoveryDualTerms(f[3],f[4],f[6],f[7],f[8],f[9],f[10],p.winner,p.bind,true))return false;
 ulong ticket=0;long entry=0;
 for(int i=0;i<v.position_count;i++) {
    if(StringSplit(v.rows[i],9,f)!=9 || f[0]!="P")return false;
    if(f[1]!=id)continue;
    long volume,sl,tp;
    if(ticket!=0 || !RecoveryAdoptionUnsigned(f[2],ticket) || ticket==0 ||
       f[3]!=magic || f[4]!=side ||
       !RecoveryAdoptionUnits(f[5],p.bind.volume_step,false,volume) || volume!=p.winner.volume_steps ||
       !RecoveryAdoptionUnits(f[6],p.bind.price_tick,false,entry) ||
       !RecoveryAdoptionUnits(f[7],p.bind.price_tick,false,sl) || sl!=p.winner.sl_ticks ||
       !RecoveryAdoptionUnits(f[8],p.bind.price_tick,true,tp) || tp!=0)return false;
 }
 if(ticket==0)return false;
 bool still_working=false;
 for(int i=v.position_count;i<ArraySize(v.rows);i++) {
    if(StringSplit(v.rows[i],9,f)!=12 || f[0]!="O" || f[1]==anchor || f[3]==id)return false;
    if(f[1]!=other)continue;
    long stoplimit;
    if(still_working || f[2]!=magic || f[3]!="0" ||
       !RecoveryDualTerms(f[4],f[5],f[6],f[7],f[8],f[9],f[10],p.survivor,p.bind,false) ||
       !RecoveryAdoptionUnits(f[11],p.bind.price_tick,true,stoplimit) || stoplimit!=0) return false;
    still_working=true;
 }
 if(!still_working)return false;
 long filled=0,weighted=0;
 for(int i=1;i<ArraySize(p.hist.rows);i++) {
    ulong kind;long steps,price;
    if(StringSplit(p.hist.rows[i],9,f)!=12 || f[0]!="D" || f[2]!=anchor || f[3]!=magic || f[4]!=side ||
       !RecoveryAdoptionUnsigned(f[5],kind) || kind!=(ulong)DEAL_ENTRY_IN ||
       !RecoveryAdoptionUnits(f[8],p.bind.volume_step,false,steps) || steps>p.winner.volume_steps-filled ||
       !RecoveryAdoptionUnits(f[9],p.bind.price_tick,false,price)) return false;
    filled+=steps;
    const long delta=price-entry;
    if(delta>LONG_MAX/steps || delta<(-LONG_MAX)/steps)return false;
    const long part=delta*steps;
    if((part>0 && weighted>LONG_MAX-part) || (part<0 && weighted<(-LONG_MAX)-part))return false;
    weighted+=part;
 }
 if(filled!=p.winner.volume_steps || weighted!=0)return false;
 out.position_ticket=ticket;out.working_other_ticket=p.survivor.ticket;out.sl_ticks=p.winner.sl_ticks;
 out.entry.generation=p.generation;out.entry.range_id=p.range_id;
 out.entry.expected_order=p.winner.ticket;out.entry.executed_order=p.winner.ticket;
 out.entry.position_id=p.bind.history.position_identifier;out.entry.side=p.winner.side;
 out.entry.planned_steps=filled;out.entry.filled_steps=filled;out.entry.active_steps=filled;
 out.entry.actual_entry_ticks=entry;out.entry.history_complete=true;
 out.entry.remainder_reconciled=true;out.entry.independently_verified=true;out.independently_verified=true;
 return true;
}
RecoveryDualSupportResult RecoveryDualSupportStop(RecoveryDualSupportPump &p) {
 p.state=RDS_STOP;return RDS_UNKNOWN;
}

// The caller separately verifies and modifies the still-owned survivor; no
// guessed winner, replacement, close or send occurs anywhere in this pump.
RecoveryDualSupportResult RecoveryStepDualSupport(RecoveryDualSupportPump &p,const RRRangeState &s,
    const RRSettings &cfg,RRFilled &filled,RRKnownOrder &survivor) {
 ZeroMemory(filled);RRClearKnown(survivor);
 if(p.state==RDS_IDLE || p.state==RDS_DONE || p.state==RDS_STOP)return RDS_UNKNOWN;
 if(!RecoveryDualSupportCurrent(p,s,cfg))return RecoveryDualSupportStop(p);
 RecoveryExactReport e;
 ulong id;
 switch(p.state) {
   case RDS_LOCAL:
      if(!RecoveryDualLocalCandidate(p.bind.entry,p.entry) &&
         !RecoveryDualLocalCandidate(p.bind.opposite,p.opposite))return RDS_WAIT;
      p.state=RDS_REQ_ENTRY;return RDS_PROGRESS;
   case RDS_REQ_ENTRY:
      p.state=RDS_READ_ENTRY;
      return RecoveryExactReportRequest(p.bind.entry,p.bind.publisher_tag)?RDS_PROGRESS:RecoveryDualSupportStop(p);
   case RDS_READ_ENTRY: {
      if(!RecoveryExactReportRead(p.bind.entry,e))return RDS_WAIT;
      const RecoveryDualExactKind k=RecoveryDualExact(e,p.entry,(long)p.bind.verify_after,p.bind,id);
      if(k==RDE_UNKNOWN)return RecoveryDualSupportStop(p);
      p.bind.history.position_identifier=id;p.exact_sequence=e.sequence;
      p.state=RDS_REQ_OPPOSITE;return RDS_PROGRESS;
   }
   case RDS_REQ_OPPOSITE:
      p.state=RDS_READ_OPPOSITE;
      return RecoveryExactReportRequest(p.bind.opposite,p.bind.publisher_tag)?RDS_PROGRESS:RecoveryDualSupportStop(p);
   case RDS_READ_OPPOSITE: {
      if(!RecoveryExactReportRead(p.bind.opposite,e))return RDS_WAIT;
      const RecoveryDualExactKind k=RecoveryDualExact(e,p.opposite,p.exact_sequence,p.bind,id);
      if(k==RDE_UNKNOWN)return RecoveryDualSupportStop(p);
      if(id!=0 && p.bind.history.position_identifier!=0){p.state=RDS_STOP;return RDS_COLLISION;}
      if(id==0 && p.bind.history.position_identifier==0)return RecoveryDualSupportStop(p);
      if(id!=0) {
         p.bind.history.position_identifier=id;
         p.winner=p.opposite;p.survivor=p.entry;
      } else {p.winner=p.entry;p.survivor=p.opposite;}
      p.bind.history.anchor_order=p.winner.ticket;p.exact_sequence=e.sequence;
      p.state=RDS_REQ_HISTORY;return RDS_PROGRESS;
   }
   case RDS_REQ_HISTORY:
      p.state=RDS_READ_HISTORY;
      return RecoveryHistoryRequest(p.bind.history,p.bind.publisher_tag)?RDS_PROGRESS:RecoveryDualSupportStop(p);
   case RDS_READ_HISTORY:
      if(!RecoveryHistoryRead(p.bind.history,p.hist))return RDS_WAIT;
      if(!p.hist.usable || !p.hist.observed || p.hist.sequence<=p.exact_sequence ||
         p.hist.order_count!=1 || p.hist.deal_count<1 || p.hist.deal_count>128)
         return RecoveryDualSupportStop(p);
      p.state=RDS_REQ_INVENTORY;return RDS_PROGRESS;
   case RDS_REQ_INVENTORY: {
      const RecoveryInventoryScope q=p.bind.inventory;
      p.state=RDS_READ_INVENTORY;
      return RecoveryRequestInventory(q.nonce,q.executor_epoch,q.verifier_epoch,q.generation,
                   q.login,q.server,q.symbol,q.magic)?RDS_PROGRESS:RecoveryDualSupportStop(p);
   }
   case RDS_READ_INVENTORY: {
      RecoveryInventorySnapshot v;
      if(!RecoveryInvRead(p.bind.inventory,v))return RDS_WAIT;
      if(!RecoveryDualSupportCurrent(p,s,cfg) || !RecoveryDualSupportFold(p,v,filled))
         return RecoveryDualSupportStop(p);
      survivor=p.survivor;p.verified_sequence=v.sequence;p.state=RDS_DONE;return RDS_PROOF;
   }
   default: return RecoveryDualSupportStop(p);
 }
}
// This effect is allowed only after RDS_PROOF, for the same allocated view.
// Owner still owns the atomic commit and rotation of the surviving ticket.
bool RecoveryDualPendingReentryEffect(const RecoveryDualSupportPump &p,const RRRangeState &range,
    const RRSettings &cfg,const RecoverySessionIntentScope &scope,const RRAction &action,
    const RecoveryIntentKey &recorded_key,const ulong ticket,RREffect &effect) {
 ZeroMemory(effect);
 if(p.state!=RDS_DONE || p.bind.entry.binding.executor_epoch!=scope.executor_epoch ||
    p.bind.entry.binding.login!=scope.login || p.bind.entry.binding.server!=scope.server ||
    p.bind.entry.binding.symbol!=scope.symbol || p.bind.entry.binding.magic!=scope.magic)
    return false;
 RRRangeState transport;
 if(!RecoveryDualPendingReentryView(range,cfg,scope,action,recorded_key,ticket,
      p.bind.price_tick,p.bind.volume_step,transport) || !RecoveryDualSupportCurrent(p,transport,cfg) ||
    (p.winner.ticket!=ticket && p.survivor.ticket!=ticket))return false;
 effect.action=action;effect.created_order=ticket;effect.terminal_verified=true;
 return true;
}
#endif
