#ifndef RECOVERY_MANUAL_PENDING_EARLY_MQH
#define RECOVERY_MANUAL_PENDING_EARLY_MQH
#include "RecoveryManualPending.mqh"

struct RecoveryManualPendingEarly {
 RecoveryManualPendingCandidate original; // immutable locally observed working terms
 long time_type,expiration;
};

bool RecoveryManualPendingEarlyTerms(const RecoveryManualPendingCandidate &a,
    const RecoveryManualPendingCandidate &b) {
 return a.ticket==b.ticket && a.order_magic==b.order_magic && a.side==b.side &&
    a.entry_ticks==b.entry_ticks && a.sl_ticks==b.sl_ticks && a.tp_ticks==b.tp_ticks &&
    a.volume_steps==b.volume_steps;
}

// Freeze terms before publishing the initial independent inventory request.
// These are only an exact candidate identifier, never order/fill permission.
bool RecoveryCaptureManualPendingEarly(const ulong ticket,const RecoverySessionContext &live,
    const double tick,const double step,RecoveryManualPendingEarly &out) {
 ZeroMemory(out);
 if(ticket==0 || !MathIsValidNumber(tick) || tick<=0 || !MathIsValidNumber(step) || step<=0)
    return false;
 RecoveryExactOrderScope scope;
 scope.login=live.login;scope.server=live.server;scope.symbol=live.symbol;scope.magic=0;scope.ticket=ticket;
 RecoveryExactOrderObservation a,b;
 long reason,time_type,expiration,reason2,time_type2,expiration2;
 if(RecoveryExactOrderSample(scope,a)!=RECOVERY_EXACT_ORDER_OBSERVED ||
    a.source!=RECOVERY_EXACT_ORDER_CURRENT || a.state!=ORDER_STATE_PLACED || a.position_identifier!=0 ||
    (a.type!=ORDER_TYPE_BUY_STOP && a.type!=ORDER_TYPE_SELL_STOP) ||
    a.initial_volume!=a.remaining_volume || a.stop_limit!=0.0 ||
    !OrderGetInteger(ORDER_REASON,reason) || !RecoveryManualPendingReason(reason) ||
    !OrderGetInteger(ORDER_TYPE_TIME,time_type) || !OrderGetInteger(ORDER_TIME_EXPIRATION,expiration) ||
    RecoveryExactOrderSample(scope,b)!=RECOVERY_EXACT_ORDER_OBSERVED || !RecoveryExactOrderSame(a,b) ||
    !OrderGetInteger(ORDER_REASON,reason2) || reason!=reason2 ||
    !OrderGetInteger(ORDER_TYPE_TIME,time_type2) || time_type!=time_type2 ||
    !OrderGetInteger(ORDER_TIME_EXPIRATION,expiration2) || expiration!=expiration2 ||
    !(time_type==ORDER_TIME_GTC || time_type==ORDER_TIME_DAY ||
      time_type==ORDER_TIME_SPECIFIED || time_type==ORDER_TIME_SPECIFIED_DAY) || expiration<0 ||
    ((time_type==ORDER_TIME_SPECIFIED || time_type==ORDER_TIME_SPECIFIED_DAY) && expiration==0))return false;
 RecoveryManualPendingCandidate c;
 ZeroMemory(c);c.ticket=ticket;c.order_magic=0;c.side=a.type==ORDER_TYPE_BUY_STOP ? 1 : -1;
 if(!RecoveryAdoptionUnits(DoubleToString(a.initial_volume,16),step,false,c.volume_steps) ||
    !RecoveryAdoptionUnits(DoubleToString(a.price,16),tick,false,c.entry_ticks) ||
    !RecoveryAdoptionUnits(DoubleToString(a.sl,16),tick,true,c.sl_ticks) ||
    !RecoveryAdoptionUnits(DoubleToString(a.tp,16),tick,true,c.tp_ticks))return false;
 out.original=c;out.time_type=time_type;out.expiration=expiration;
 return true;
}

// Only an exact, locally confirmed positive historical FILLED row may start
// the independent proof pump. A valid initial response supplies the sequence
// floor and per-session nonce, NOT evidence that the old order executed.
bool RecoveryPrepareManualPendingEarly(RecoveryManualPendingOwner &owner,const bool armed,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RRSettings &cfg,
    const RecoveryInventoryScope &request,const RecoveryInventorySnapshot &snapshot,
    const RecoveryManualPendingEarly &frozen,const double tick,const double step) {
 if(owner.phase!=RMP_IDLE || !armed || frozen.original.ticket==0 || frozen.original.order_magic!=0 ||
    !RecoveryManualPendingScope(life,live,scope,request) ||
    !RecoveryManualPendingInventoryOK(snapshot,0) || snapshot.reason!="ok" ||
    cfg.scope!=scope.scope || cfg.generation!=scope.generation || !RRSettingsValid(cfg))return false;
 string fields[];
 for(int i=snapshot.position_count;i<ArraySize(snapshot.rows);i++) {
    if(StringSplit(snapshot.rows[i],9,fields)!=12 || !RecoveryInvOrder(fields) ||
       fields[1]==(string)frozen.original.ticket)return false;
 }
 RecoveryManualPendingOwner o;
 ZeroMemory(o);
 o.original=frozen.original;o.original.request=request;o.original.sequence=(ulong)snapshot.sequence;
 o.scope=scope.scope;o.generation=scope.generation;o.executor_epoch=scope.executor_epoch;
 o.price_tick=tick;o.volume_step=step;o.target_sl=o.original.sl_ticks>0 ? o.original.sl_ticks :
    o.original.entry_ticks-o.original.side*cfg.initial_sl_ticks;
 if(!RCBoundedPositive(o.target_sl))return false;
 RRAction a;
 RRClearAction(a);
 a.kind=RR_MANUAL_PENDING_SL;a.scope=scope.scope;a.generation=scope.generation;
 a.range_id=1;a.serial=1;a.side=o.original.side;a.order_ticket=o.original.ticket;
 a.entry_ticks=o.original.entry_ticks;a.sl_ticks=o.target_sl;a.tp_ticks=o.original.tp_ticks;
 a.volume_steps=o.original.volume_steps;o.action=a;
 RecoveryIntentKey local;
 if(!RecoveryBuildSessionIntentKey(scope,a,tick,step,local) ||
    !RecoveryManualPendingLocalFilled(o.original,local,tick,step,o.time_type,o.expiration) ||
    o.time_type!=frozen.time_type || o.expiration!=frozen.expiration ||
    !RecoveryManualPendingBuildKey(o,scope,o.key))return false;
 o.phase=RMP_ATTEMPTED;o.receipt.state=RD_NOT_SENT;o.local_filled_before_prepare=true;
 // Even an existing SL is not treated as independently confirmed here.
 // RecoveryManualPendingFill is the sole route to new authorization.
 owner=o;return true;
}
#endif
