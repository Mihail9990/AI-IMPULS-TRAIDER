#ifndef RECOVERY_SUPPORT_RUNTIME_MQH
#define RECOVERY_SUPPORT_RUNTIME_MQH
#include "RecoverySingleSupportService.mqh"
#include "RecoveryOwnerEvidence.mqh"
#include "RecoveryInventoryRequest.mqh"

enum RecoverySupportRuntimeState {
 RSS_IDLE=0,RSS_WATCH,RSS_WAIT_LOCAL,RSS_WAIT_VERIFIER,RSS_DONE,RSS_UNCERTAIN
};
enum RecoverySupportRuntimeResult {
 RSR_WAIT=0,RSR_WATCHING,RSR_SUBMITTED,RSR_REQUESTED,RSR_CONFIRMED,RSR_UNKNOWN
};
struct RecoverySupportRuntime {
 RecoverySupportRuntimeState state;
 RecoverySingleSupport service;
 RecoveryPositionEffectBinding proof;
 string publisher_tag;
 ulong scope,generation,entry_order,position_id,position_ticket;
 int range_id;
 RecoveryDispatchResult receipt;
};

bool RecoverySupportOwnerBound(const RecoveryEvidenceOwner &owner,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,
    const string watcher_epoch,const long watcher_chart) {
 return RecoverySessionCurrent(session,live) && RecoverySessionScopeEqual(owner.context,live) &&
       TerminalInfoInteger(TERMINAL_CONNECTED)!=0 && AccountInfoInteger(ACCOUNT_LOGIN)==live.login &&
       AccountInfoString(ACCOUNT_SERVER)==live.server && _Symbol==live.symbol &&
       owner.executor_epoch==session.epoch && scope.executor_epoch==session.epoch &&
       verifier.executor_epoch==session.epoch && verifier.verifier_epoch==owner.verifier_epoch &&
       verifier.verifier_chart==owner.verifier_chart && verifier.verifier_chart!=live.chart &&
       RecoveryInvScopeOK(verifier) && RecoveryInvHex(watcher_epoch) &&
       watcher_epoch!=session.epoch && watcher_epoch!=verifier.verifier_epoch &&
       watcher_chart>0 && watcher_chart!=live.chart && watcher_chart!=verifier.verifier_chart &&
       scope.login==live.login && scope.server==live.server && scope.symbol==live.symbol &&
       scope.magic==live.magic && scope.scope>0 && scope.generation>0 &&
       scope.generation<=(ulong)LONG_MAX &&
       verifier.generation==(long)scope.generation && verifier.login==live.login &&
       verifier.server==live.server && verifier.symbol==live.symbol && verifier.magic==live.magic;
}

bool RecoverySupportRuntimeTarget(const RecoverySupportRuntime &t,const RRRangeState &range) {
 return t.scope==range.scope && t.generation==range.cycle.generation &&
       t.range_id==range.cycle.range_id && t.entry_order==range.cycle.active_order &&
       t.position_id==range.cycle.position_id && t.position_ticket==range.position_ticket;
}

// Local exact read is only a scheduling precondition. No RR state is changed
// and no position modification is verified by this check.
bool RecoverySupportLocalPosition(const RRRangeState &range,const RecoverySessionContext &live,
    const double tick,const double step,const long expected_sl,ulong &magic,string &entry_wire) {
 magic=0;entry_wire="";
 long actual_id,actual_ticket,type,signed_magic;
 string symbol;
 double price,volume,sl,tp;
 if(TerminalInfoInteger(TERMINAL_CONNECTED)==0 ||
    AccountInfoInteger(ACCOUNT_LOGIN)!=live.login || AccountInfoString(ACCOUNT_SERVER)!=live.server ||
    _Symbol!=live.symbol || !PositionSelectByTicket(range.position_ticket) ||
    !PositionGetInteger(POSITION_IDENTIFIER,actual_id) || (ulong)actual_id!=range.cycle.position_id ||
    !PositionGetInteger(POSITION_TICKET,actual_ticket) || (ulong)actual_ticket!=range.position_ticket ||
    !PositionGetInteger(POSITION_TYPE,type) ||
    type!=(range.cycle.side==1 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL) ||
    !PositionGetInteger(POSITION_MAGIC,signed_magic) || (ulong)signed_magic!=(ulong)live.magic ||
    !PositionGetString(POSITION_SYMBOL,symbol) || symbol!=live.symbol ||
    !PositionGetDouble(POSITION_PRICE_OPEN,price) || !RecoveryDispatchUnits(price,tick,false,range.cycle.actual_entry_ticks) ||
    !PositionGetDouble(POSITION_VOLUME,volume) || !RecoveryDispatchUnits(volume,step,false,range.cycle.filled_steps) ||
    !PositionGetDouble(POSITION_SL,sl) || !RecoveryDispatchUnits(sl,tick,true,expected_sl) ||
    !PositionGetDouble(POSITION_TP,tp) || !RecoveryDispatchUnits(tp,tick,true,range.live_tp) ||
    TerminalInfoInteger(TERMINAL_CONNECTED)==0 ||
    AccountInfoInteger(ACCOUNT_LOGIN)!=live.login || AccountInfoString(ACCOUNT_SERVER)!=live.server ||
    _Symbol!=live.symbol) return false;
 magic=(ulong)signed_magic;entry_wire=DoubleToString(price,16);
 return true;
}

RecoverySupportRuntimeResult RecoverySupportRuntimeUnknown(RecoverySupportRuntime &t) {
 t.state=RSS_UNCERTAIN;return RSR_UNKNOWN;
}

RecoverySupportRuntimeResult RecoverySupportRuntimeTimer(RecoverySupportRuntime &task,
    RecoveryEvidenceOwner &owner,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &verifier,const string watcher_epoch,const long watcher_chart,
    RRRangeState &range,const RRSettings &cfg,const double tick,const double step) {
 if(!RecoverySupportOwnerBound(owner,session,live,scope,verifier,watcher_epoch,watcher_chart) ||
    !RRValid(range,cfg) || scope.scope!=range.scope || scope.generation!=range.cycle.generation ||
    cfg.support_first_ticks!=0 || cfg.support_offset_ticks!=0 ||
    !MathIsValidNumber(tick) || !MathIsValidNumber(step) || tick<=0 || step<=0)
    return RSR_UNKNOWN;

 if(task.state!=RSS_IDLE && !RecoverySupportRuntimeTarget(task,range)) {
    // Drop only our own prior read-only subscription once, even if the
    // previous position still exists. A new actual entry gets distinct IDs.
    if(task.state==RSS_WATCH && task.service.request.executor_epoch==session.epoch &&
       task.service.request.watcher_epoch==watcher_epoch && task.service.request.watcher_chart==watcher_chart)
       RecoveryPriceDrop(task.service.request,2,task.publisher_tag);
    ZeroMemory(task);
 }
 if(task.state==RSS_UNCERTAIN)return RSR_UNKNOWN;
 if(task.state==RSS_DONE)return RSR_WAIT;

 if(task.state==RSS_IDLE) {
    if(range.phase!=RR_ACTIVE || range.pending.kind!=RR_NONE || range.support_stage!=0 ||
       !RCValidSide(range.cycle.side) || range.cycle.active_order==0 ||
       range.cycle.position_id==0 || range.position_ticket==0 || owner.serial==ULONG_MAX ||
       !RCBoundedPositive(range.live_sl) ||
       (range.cycle.actual_entry_ticks-range.live_sl)*range.cycle.side<=0)
       return RSR_WAIT;
    ulong magic;
    string entry;
    if(!RecoverySupportLocalPosition(range,live,tick,step,range.live_sl,magic,entry))return RSR_WAIT;
    const ulong seq=owner.serial+1;
    const string identity=owner.executor_epoch+"\t"+owner.verifier_epoch+"\t"+watcher_epoch+"\t"+
       (string)watcher_chart+"\t"+(string)range.scope+"\t"+(string)range.cycle.generation+"\t"+
       (string)range.cycle.range_id+"\t"+(string)range.cycle.active_order+"\t"+
       (string)range.cycle.position_id+"\t"+(string)range.position_ticket+"\t"+(string)seq;
    RecoveryPriceScope watch;
    watch.nonce=RecoveryOwnerNonce("support-price",identity);
    watch.executor_epoch=owner.executor_epoch;watch.watcher_epoch=watcher_epoch;
    watch.generation=range.cycle.generation;watch.login=live.login;watch.watcher_chart=watcher_chart;
    watch.server=live.server;watch.symbol=live.symbol;watch.position_identifier=range.cycle.position_id;
    watch.live_ticket=range.position_ticket;watch.side=range.cycle.side;
    watch.actual_entry=entry;watch.first_advance="0.0000000000000000";
    watch.second_advance=DoubleToString((double)cfg.support_second_ticks*tick,16);
    const string tag=RecoveryOwnerNonce("support-publisher",identity);
    const string proof_nonce=RecoveryOwnerNonce("support-inventory",identity);
    if(!RecoveryPriceScopeOK(watch) || !RecoveryInvHex(tag) || !RecoveryInvHex(proof_nonce) ||
       tag==watch.nonce || proof_nonce==watch.nonce || proof_nonce==tag) return RSR_UNKNOWN;

    owner.serial=seq;task.publisher_tag=tag;
    task.scope=range.scope;task.generation=range.cycle.generation;task.range_id=range.cycle.range_id;
    task.entry_order=range.cycle.active_order;task.position_id=range.cycle.position_id;task.position_ticket=range.position_ticket;
    task.proof.request=verifier;task.proof.request.nonce=proof_nonce;task.proof.verify_after=0;
    task.proof.prior_position_magic=magic;task.proof.prior_position_entry=entry;
    task.proof.price_tick=tick;task.proof.volume_step=step;
    task.state=RSS_WATCH;
    if(!RecoveryStartSingleSupport(task.service,session,live,range,cfg,watch,tag,tick))
       return RecoverySupportRuntimeUnknown(task);
    return RSR_WATCHING;
 }
 if(task.service.request.watcher_chart!=watcher_chart || task.service.request.watcher_epoch!=watcher_epoch ||
    task.proof.request.verifier_chart!=verifier.verifier_chart ||
    task.proof.request.verifier_epoch!=verifier.verifier_epoch ||
    tick!=task.proof.price_tick || step!=task.proof.volume_step)
    return RecoverySupportRuntimeUnknown(task);

 if(task.state==RSS_WATCH) {
    ulong before_magic;string before_entry;
    if(!RecoverySupportLocalPosition(range,live,tick,step,range.live_sl,before_magic,before_entry) ||
       before_magic!=task.proof.prior_position_magic || before_entry!=task.proof.prior_position_entry ||
       !RecoverySubmitSingleSupport(task.service,session,live,scope,range,cfg,tick,step,task.receipt))
       return RSR_WAIT;
    task.proof.action=task.service.action;task.state=RSS_WAIT_LOCAL;
    if(task.receipt.state!=RD_OUTCOME_UNRESOLVED)return RecoverySupportRuntimeUnknown(task);
    return RSR_SUBMITTED;
 }
 if(task.state!=RSS_WAIT_LOCAL && task.state!=RSS_WAIT_VERIFIER)return RecoverySupportRuntimeUnknown(task);
 if(!RecoveryPositionEffectBound(range,task.proof))return RecoverySupportRuntimeUnknown(task);

 if(task.state==RSS_WAIT_LOCAL) {
    // Do not ask the one-shot verifier before the asynchronously submitted
    // target is visible. This does not confirm it; only releases the query.
    ulong actual_magic;string actual_entry;
    if(!RecoverySupportLocalPosition(range,live,tick,step,task.service.action.sl_ticks,actual_magic,actual_entry) ||
       actual_magic!=task.proof.prior_position_magic || actual_entry!=task.proof.prior_position_entry)
       return RSR_WAIT;
    const RecoveryInventoryScope q=task.proof.request;
    task.state=RSS_WAIT_VERIFIER;
    if(!RecoveryRequestInventory(q.nonce,q.executor_epoch,q.verifier_epoch,q.generation,
                                 q.login,q.server,q.symbol,q.magic))
       return RecoverySupportRuntimeUnknown(task);
    return RSR_REQUESTED;
 }

 RecoveryInventorySnapshot report;
 if(!RecoveryInvRead(task.proof.request,report))return RSR_WAIT;
 if(!RecoverySupportOwnerBound(owner,session,live,scope,verifier,watcher_epoch,watcher_chart) ||
    !RecoveryConfirmSingleSupport(task.service,range,task.proof))
    return RecoverySupportRuntimeUnknown(task);
 task.state=RSS_DONE;
 return RSR_CONFIRMED;
}
#endif
