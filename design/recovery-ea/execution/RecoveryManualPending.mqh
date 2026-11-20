#ifndef RECOVERY_MANUAL_PENDING_MQH
#define RECOVERY_MANUAL_PENDING_MQH
#include "RecoveryPrimarySubmit.mqh"
#include "RecoveryAlternatingEvidence.mqh"
#include "RecoveryDispatch.mqh"

// Separate key domain; this action never enters the RR dispatcher.
#define RR_MANUAL_PENDING_SL 8

enum RecoveryManualPendingPhase {
 RMP_IDLE=0,RMP_PREPARED,RMP_ATTEMPTED,RMP_READY,RMP_UNRESOLVED
};
struct RecoveryManualPendingCandidate {
 RecoveryInventoryScope request;
 ulong sequence,ticket,order_magic;
 int side;
 long entry_ticks,sl_ticks,tp_ticks,volume_steps; // initial == remaining
};
struct RecoveryManualPendingOwner {
 RecoveryManualPendingPhase phase;
 bool local_filled_before_prepare;
 RecoveryManualPendingCandidate original;
 ulong scope,generation,confirmed_sequence;
 string executor_epoch;
 long target_sl,time_type,expiration;
 double price_tick,volume_step;
 RRAction action;
 RecoveryIntentKey key;
 RecoveryDispatchResult receipt;
};

bool RecoveryManualPendingScope(const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &request) {
 return RecoveryPrimarySession(session,live,scope) && RecoveryInvScopeOK(request) &&
    request.executor_epoch==scope.executor_epoch && request.login==live.login && request.server==live.server &&
    request.symbol==live.symbol && request.magic==live.magic && request.verifier_chart!=live.chart &&
    request.generation>0 && (ulong)request.generation==scope.generation;
}

bool RecoveryManualPendingRow(const string &row,const double tick,const double step,
    RecoveryManualPendingCandidate &out) {
 ulong type,state,position;
 long remaining,stoplimit;
 string fields[];
 if(StringSplit(row,9,fields)!=12 || !RecoveryInvOrder(fields) ||
    !RecoveryAdoptionUnsigned(fields[1],out.ticket) || out.ticket==0 ||
    !RecoveryAdoptionUnsigned(fields[2],out.order_magic) || out.order_magic!=0 ||
    !RecoveryAdoptionUnsigned(fields[3],position) || position!=0 ||
    !RecoveryAdoptionUnsigned(fields[4],type) ||
    (type!=(ulong)ORDER_TYPE_BUY_STOP && type!=(ulong)ORDER_TYPE_SELL_STOP) ||
    !RecoveryAdoptionUnsigned(fields[5],state) || state!=(ulong)ORDER_STATE_PLACED ||
    !RecoveryAdoptionUnits(fields[6],step,false,out.volume_steps) ||
    !RecoveryAdoptionUnits(fields[7],step,false,remaining) || remaining!=out.volume_steps ||
    !RecoveryAdoptionUnits(fields[8],tick,false,out.entry_ticks) ||
    !RecoveryAdoptionUnits(fields[9],tick,true,out.sl_ticks) ||
    !RecoveryAdoptionUnits(fields[10],tick,true,out.tp_ticks) ||
    !RecoveryAdoptionUnits(fields[11],tick,true,stoplimit) || stoplimit!=0)
    return false;
 out.side=(type==(ulong)ORDER_TYPE_BUY_STOP ? 1 : -1);
 return true;
}

bool RecoveryManualPendingInventoryOK(const RecoveryInventorySnapshot &inventory,
                                       const ulong floor) {
 return inventory.usable && inventory.complete && inventory.sequence>0 &&
    (ulong)inventory.sequence>floor && inventory.position_count>=0 && inventory.position_count<=128 &&
    inventory.order_count>=0 && inventory.order_count<=128 &&
    ArraySize(inventory.rows)==inventory.position_count+inventory.order_count;
}

// Typed inventory must be from RecoveryInvRead/Parse with the supplied nonce.
// If the mode does not provide an explicit ticket, exactly one eligible
// pristine manual STOP must be visible. Never choose a price/volume match.
bool RecoveryFindManualPending(const RecoveryInventorySnapshot &inventory,
    const RecoveryInventoryScope &request,const ulong after,const ulong selected_ticket,
    const double tick,const double step,RecoveryManualPendingCandidate &out) {
 ZeroMemory(out);
 if(!RecoveryInvScopeOK(request) || !RecoveryManualPendingInventoryOK(inventory,after))return false;
 RecoveryManualPendingCandidate found;
 ZeroMemory(found);
 int count=0;
 for(int i=inventory.position_count;i<ArraySize(inventory.rows);i++) {
    RecoveryManualPendingCandidate item;
    ZeroMemory(item);
    if(!RecoveryManualPendingRow(inventory.rows[i],tick,step,item) ||
       (selected_ticket!=0 && item.ticket!=selected_ticket))continue;
    if(++count!=1)return false;
    found=item;
 }
 if(count!=1)return false;
 found.request=request;found.sequence=(ulong)inventory.sequence;
 out=found;return true;
}

bool RecoveryManualPendingReason(const long reason) {
 return reason==ORDER_REASON_CLIENT || reason==ORDER_REASON_MOBILE || reason==ORDER_REASON_WEB;
}

// A final terminal read is not independent confirmation. It prevents edits
// to an order that filled, was manually edited, expired, or changed origin
// since the separate verifier report. Read original expiry exactly.
bool RecoveryManualPendingLocal(const RecoveryManualPendingCandidate &c,const RecoveryIntentKey &key,
    const double tick,const double step,long &time_type,long &expiration,double &price,double &tp) {
 if(!RecoveryIntentContextOK(key) || !OrderSelect(c.ticket))return false;
 long id,magic,position,type,state,reason;
 double volume,remaining,sl,stoplimit;
 string symbol;
 if(!OrderGetInteger(ORDER_TICKET,id) || (ulong)id!=c.ticket ||
    !OrderGetInteger(ORDER_MAGIC,magic) || (ulong)magic!=c.order_magic || c.order_magic!=0 ||
    !OrderGetInteger(ORDER_POSITION_ID,position) || position!=0 ||
    !OrderGetInteger(ORDER_TYPE,type) || type!=(c.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
    !OrderGetInteger(ORDER_STATE,state) || state!=ORDER_STATE_PLACED ||
    !OrderGetInteger(ORDER_REASON,reason) || !RecoveryManualPendingReason(reason) ||
    !OrderGetInteger(ORDER_TYPE_TIME,time_type) ||
    !OrderGetInteger(ORDER_TIME_EXPIRATION,expiration) ||
    !OrderGetString(ORDER_SYMBOL,symbol) || symbol!=key.symbol ||
    !OrderGetDouble(ORDER_VOLUME_INITIAL,volume) ||
    !RecoveryDispatchUnits(volume,step,false,c.volume_steps) ||
    !OrderGetDouble(ORDER_VOLUME_CURRENT,remaining) ||
    !RecoveryDispatchUnits(remaining,step,false,c.volume_steps) ||
    !OrderGetDouble(ORDER_PRICE_OPEN,price) || !RecoveryDispatchUnits(price,tick,false,c.entry_ticks) ||
    !OrderGetDouble(ORDER_SL,sl) || !RecoveryDispatchUnits(sl,tick,true,c.sl_ticks) ||
    !OrderGetDouble(ORDER_TP,tp) || !RecoveryDispatchUnits(tp,tick,true,c.tp_ticks) ||
    !OrderGetDouble(ORDER_PRICE_STOPLIMIT,stoplimit) || stoplimit!=0.0) return false;
 return (time_type==ORDER_TIME_GTC || time_type==ORDER_TIME_DAY ||
         time_type==ORDER_TIME_SPECIFIED || time_type==ORDER_TIME_SPECIFIED_DAY) && expiration>=0 &&
    ((time_type!=ORDER_TIME_SPECIFIED && time_type!=ORDER_TIME_SPECIFIED_DAY) || expiration>0) &&
    RecoveryIntentContextOK(key);
}

// Read-only early-fill hint, only after the independent inventory already
// selected this exact pristine user ticket. A fast execution must never turn
// into a late MODIFY. The caller still needs full independent fill evidence.
bool RecoveryManualPendingLocalFilled(const RecoveryManualPendingCandidate &c,const RecoveryIntentKey &key,
    const double tick,const double step,long &time_type,long &expiration) {
 if(!RecoveryIntentContextOK(key) || c.ticket==0 || c.order_magic!=0)return false;
 RecoveryExactOrderScope exact;
 exact.login=key.login;exact.server=key.server;exact.symbol=key.symbol;
 exact.magic=c.order_magic;exact.ticket=c.ticket;
 RecoveryExactOrderObservation row;
 long reason;
 // The strategy-specific public exact reader rejects magic=0. This single
 // exact local sample never enters its independent verifier channel.
 if(RecoveryExactOrderSample(exact,row)!=RECOVERY_EXACT_ORDER_OBSERVED ||
    row.source!=RECOVERY_EXACT_ORDER_HISTORY || row.ticket!=c.ticket || row.magic!=0 ||
    row.position_identifier==0 || row.symbol!=key.symbol || row.state!=ORDER_STATE_FILLED ||
    row.type!=(c.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
    row.remaining_volume!=0.0 || row.stop_limit!=0.0 ||
    !RecoveryDispatchUnits(row.initial_volume,step,false,c.volume_steps) ||
    !RecoveryDispatchUnits(row.price,tick,false,c.entry_ticks) ||
    !RecoveryDispatchUnits(row.sl,tick,true,c.sl_ticks) ||
    !RecoveryDispatchUnits(row.tp,tick,true,c.tp_ticks) ||
    !HistoryOrderGetInteger(c.ticket,ORDER_REASON,reason) || !RecoveryManualPendingReason(reason) ||
    !HistoryOrderGetInteger(c.ticket,ORDER_TYPE_TIME,time_type) ||
    !HistoryOrderGetInteger(c.ticket,ORDER_TIME_EXPIRATION,expiration))
    return false;
 return (time_type==ORDER_TIME_GTC || time_type==ORDER_TIME_DAY ||
         time_type==ORDER_TIME_SPECIFIED || time_type==ORDER_TIME_SPECIFIED_DAY) && expiration>=0 &&
    ((time_type!=ORDER_TIME_SPECIFIED && time_type!=ORDER_TIME_SPECIFIED_DAY) || expiration>0) &&
    RecoveryIntentContextOK(key);
}

bool RecoveryManualPendingBuildKey(const RecoveryManualPendingOwner &owner,
    const RecoverySessionIntentScope &scope,RecoveryIntentKey &out) {
 RecoveryIntentKey base;
 if(!RecoveryBuildSessionIntentKey(scope,owner.action,owner.price_tick,owner.volume_step,base))return false;
 const string ordinal=base.id+"\t"+(string)owner.original.ticket;
 const string id=RecoverySessionSha256("RECOVERY_USER_STOP_SL_ID_V1\t"+ordinal);
 const string payload=RecoverySessionSha256("RECOVERY_USER_STOP_SL_PAYLOAD_V1\t"+base.fingerprint+
    "\t"+(string)owner.original.order_magic+"\t"+(string)owner.original.sl_ticks+"\t"+
    (string)owner.time_type+"\t"+(string)owner.expiration);
 if(StringLen(id)!=64 || StringLen(payload)!=64)return false;
 out=base;out.id=StringSubstr(id,0,32);out.fingerprint=payload;
 return RecoveryIntentKeyOK(out);
}

// arm_pending MUST reflect the currently authorized pending-wait command,
// not market wait or a local trade event. Preparation never sends.
bool RecoveryPrepareManualPending(RecoveryManualPendingOwner &owner,const bool arm_pending,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RRSettings &cfg,
    const RecoveryInventoryScope &request,const RecoveryInventorySnapshot &inventory,
    const ulong after,const ulong selected_ticket,const double tick,const double step) {
 if(owner.phase!=RMP_IDLE || !arm_pending || !RecoveryManualPendingScope(session,live,scope,request) ||
    cfg.scope!=scope.scope || cfg.generation!=scope.generation || !RRSettingsValid(cfg))return false;
 RecoveryManualPendingOwner next;
 ZeroMemory(next);
 if(!RecoveryFindManualPending(inventory,request,after,selected_ticket,tick,step,next.original))return false;
 next.scope=scope.scope;next.generation=scope.generation;next.executor_epoch=scope.executor_epoch;
 next.price_tick=tick;next.volume_step=step;
 next.target_sl=next.original.sl_ticks>0 ? next.original.sl_ticks :
                      next.original.entry_ticks-next.original.side*cfg.initial_sl_ticks;
 if(!RCBoundedPositive(next.target_sl))return false;
 RRAction a;
 RRClearAction(a);
 a.kind=RR_MANUAL_PENDING_SL;a.scope=scope.scope;a.generation=scope.generation;
 a.range_id=1;a.serial=1;a.side=next.original.side;a.order_ticket=next.original.ticket;
 a.entry_ticks=next.original.entry_ticks;a.sl_ticks=next.target_sl;a.tp_ticks=next.original.tp_ticks;
 a.volume_steps=next.original.volume_steps;
 next.action=a;
 RecoveryIntentKey local_key;
 double price,tp;
 if(!RecoveryBuildSessionIntentKey(scope,a,tick,step,local_key))return false;
 if(!RecoveryManualPendingLocal(next.original,local_key,tick,step,next.time_type,next.expiration,price,tp)) {
    if(!RecoveryManualPendingLocalFilled(next.original,local_key,tick,step,next.time_type,next.expiration))
       return false;
    next.local_filled_before_prepare=true;
 }
 if(!RecoveryManualPendingBuildKey(next,scope,next.key))return false;
 if(next.original.sl_ticks>0) {
    next.phase=RMP_READY;next.confirmed_sequence=next.original.sequence;
 } else if(next.local_filled_before_prepare) {
    next.phase=RMP_ATTEMPTED;next.receipt.state=RD_NOT_SENT;
 } else next.phase=RMP_PREPARED;
 owner=next;return true;
}

bool RecoveryManualPendingBound(const RecoveryManualPendingOwner &owner,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope) {
 RecoveryIntentKey expected;
 const RRAction a=owner.action;
 return owner.phase!=RMP_IDLE && owner.scope==scope.scope && owner.generation==scope.generation &&
    owner.executor_epoch==scope.executor_epoch &&
    RecoveryManualPendingScope(session,live,scope,owner.original.request) &&
    owner.original.ticket!=0 && owner.original.order_magic==0 && RCValidSide(owner.original.side) &&
    a.kind==RR_MANUAL_PENDING_SL && a.scope==owner.scope && a.generation==owner.generation &&
    a.serial==1 && a.range_id==1 && a.side==owner.original.side && a.order_ticket==owner.original.ticket &&
    a.entry_ticks==owner.original.entry_ticks && a.sl_ticks==owner.target_sl &&
    a.tp_ticks==owner.original.tp_ticks && a.volume_steps==owner.original.volume_steps &&
    RecoveryManualPendingBuildKey(owner,scope,expected) && RecoveryIntentKeysMatch(owner.key,expected);
}

// Consume the initial attempt before terminal preflight. Only MODIFY can be
// emitted, preserving price/TP AND lifetime from the exact still-unchanged
// order. No create, cancel, replacement, inferred success or repeat.
bool RecoveryAttemptManualPendingSL(RecoveryManualPendingOwner &owner,const bool arm_pending,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope) {
 if(owner.phase!=RMP_PREPARED || !arm_pending || owner.original.sl_ticks!=0 || owner.local_filled_before_prepare ||
    !RecoveryManualPendingBound(owner,session,live,scope))return false;
 owner.phase=RMP_ATTEMPTED;
 ZeroMemory(owner.receipt);
 owner.receipt.state=RD_NOT_SENT;
 string ignored;
 if(RecoveryIntentLookup(owner.key,ignored)!=RI_NO_RECORD) {
    owner.receipt.state=RD_EXISTING_INTENT;return false;
 }
 long time_type,expiration;
 double price,tp;
 if(!RecoveryManualPendingLocal(owner.original,owner.key,owner.price_tick,owner.volume_step,
           time_type,expiration,price,tp) || time_type!=owner.time_type || expiration!=owner.expiration)
    return false;
 const double stop=owner.target_sl*owner.price_tick;
 if(!MathIsValidNumber(stop) || stop<=0.0 ||
    (owner.original.entry_ticks-owner.target_sl)*owner.original.side<=0)return false;

 MqlTradeRequest cmd;
 MqlTradeCheckResult checked;
 ZeroMemory(cmd);ZeroMemory(checked);
 cmd.action=TRADE_ACTION_MODIFY;cmd.order=owner.original.ticket;cmd.symbol=owner.key.symbol;
 cmd.price=price;cmd.sl=stop;cmd.tp=tp;
 cmd.type_time=(ENUM_ORDER_TYPE_TIME)time_type;cmd.expiration=(datetime)expiration;
 const bool allowed=OrderCheck(cmd,checked);
 owner.receipt.check_retcode=checked.retcode;
 if(!allowed || (checked.retcode!=0 && checked.retcode!=TRADE_RETCODE_DONE))return false;
 if(!RecoveryIntentReserve(owner.key)) {
    owner.receipt.state=RD_EXISTING_INTENT;return false;
 }
 owner.receipt.state=RD_RESERVED_NOT_SENT;
 if(!RecoveryManualPendingLocal(owner.original,owner.key,owner.price_tick,owner.volume_step,
           time_type,expiration,price,tp) || time_type!=owner.time_type || expiration!=owner.expiration)
    return false;
 // Copy the freshly observed exact wire-equivalent prices; do not round
 // the requested entry or TP again or supply a new order size or type.
 cmd.price=price;cmd.tp=tp;
 MqlTradeResult answer;
 ZeroMemory(answer);
 owner.receipt.state=RD_OUTCOME_UNRESOLVED;
 OrderSend(cmd,answer);
 owner.receipt.send_retcode=answer.retcode;
 return true; // only attempted, never verified
}

bool RecoveryConfirmManualPendingSL(RecoveryManualPendingOwner &owner,const bool arm_pending,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &request,
    const RecoveryInventorySnapshot &inventory) {
 if(owner.phase!=RMP_ATTEMPTED || owner.receipt.state!=RD_OUTCOME_UNRESOLVED || !arm_pending ||
    request.nonce==owner.original.request.nonce ||
    !RecoveryManualPendingScope(session,live,scope,request) ||
    !RecoveryAlternatingSameSession(request,owner.original.request) ||
    !RecoveryManualPendingBound(owner,session,live,scope))return false;
 string ignored;
 if(RecoveryIntentLookup(owner.key,ignored)!=RI_UNRESOLVED)return false;
 RecoveryManualPendingCandidate row;
 if(!RecoveryFindManualPending(inventory,request,owner.original.sequence,owner.original.ticket,
       owner.price_tick,owner.volume_step,row) ||
    row.order_magic!=owner.original.order_magic || row.side!=owner.original.side ||
    row.entry_ticks!=owner.original.entry_ticks || row.sl_ticks!=owner.target_sl ||
    row.tp_ticks!=owner.original.tp_ticks || row.volume_steps!=owner.original.volume_steps)
    return false;
 owner.confirmed_sequence=row.sequence;owner.phase=RMP_READY;
 return true;
}
#endif
