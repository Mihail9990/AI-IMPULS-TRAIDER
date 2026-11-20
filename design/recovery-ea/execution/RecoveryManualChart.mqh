#ifndef RECOVERY_MANUAL_CHART_MQH
#define RECOVERY_MANUAL_CHART_MQH
#include "RecoveryManualOwner.mqh"
#include "RecoveryOwnerEvidence.mqh"
#include "RecoveryInventoryRequest.mqh"

enum RecoveryManualChartPhase {
 RMC_IDLE=0,RMC_INVENTORY,RMC_HISTORY,RMC_READY,RMC_EFFECT_VISIBLE,RMC_EFFECT_REPORT,RMC_ADOPTED,RMC_UNKNOWN
};
enum RecoveryManualChartResult { RMC_WAIT=0,RMC_PROGRESS,RMC_ATTEMPTED,RMC_PROOF,RMC_NO_ACTION };
struct RecoveryManualHint { ulong position_id,ticket;long change; };
struct RecoveryManualChart {
 RecoveryManualChartPhase phase;
 RecoveryManualHint hint;
 RecoveryInventoryScope initial,confirm;
 RecoveryInventorySnapshot inventory;
 RecoveryHistoryScope history;
 RecoveryManualOwner owner;
 double tick,step;
};

// Cancel WAIT before any attempt, never forget an uncertain broker action.
bool RecoveryManualChartMayCancel(const RecoveryManualChart &task) {
 return task.owner.phase==RMO_UNUSED || task.owner.phase==RMO_PLANNED;
}
bool RecoveryManualChartClearWaiting(RecoveryManualChart &task) {
 if(!RecoveryManualChartMayCancel(task))return false;
 ZeroMemory(task);
 return true;
}
bool RecoveryManualHintEqual(const RecoveryManualHint &a,const RecoveryManualHint &b) {
 return a.position_id==b.position_id && a.ticket==b.ticket && a.change==b.change;
}

// Local hint avoids opening read requests with zero or multiple positions.
// Timestamp is a mutation marker only, never compared to the current time.
bool RecoveryManualChartHint(const string symbol,RecoveryManualHint &hint) {
 ZeroMemory(hint);
 const int n=PositionsTotal();
 if(n<1)return false;
 RecoveryManualHint found;
 ZeroMemory(found);
 for(int i=0;i<n;i++) {
    const ulong ticket=PositionGetTicket(i);
    string s;
    if(ticket==0 || !PositionGetString(POSITION_SYMBOL,s))return false;
    if(s!=symbol)continue;
    long id,change;
    if(found.ticket!=0 || !PositionGetInteger(POSITION_IDENTIFIER,id) || id==0 ||
       !PositionGetInteger(POSITION_TIME_UPDATE_MSC,change))return false;
    found.position_id=(ulong)id;found.ticket=ticket;found.change=change;
 }
 if(PositionsTotal()!=n || found.ticket==0)return false;
 hint=found;
 return true;
}

bool RecoveryManualChartScope(const RecoveryEvidenceOwner &owner,const RecoverySessionLifecycle &life,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &v,const RRSettings &cfg,const double tick,const double step) {
 return RecoveryManualScope(life,live,scope,v) && owner.executor_epoch==life.epoch &&
        RecoverySessionScopeEqual(owner.context,live) && owner.verifier_epoch==v.verifier_epoch &&
        owner.verifier_chart==v.verifier_chart && cfg.scope==scope.scope && cfg.generation==scope.generation &&
        RRSettingsValid(cfg) && MathIsValidNumber(tick) && tick>0 && MathIsValidNumber(step) && step>0;
}

bool RecoveryManualPublishInventory(const RecoveryInventoryScope &q) {
 return RecoveryRequestInventory(q.nonce,q.executor_epoch,q.verifier_epoch,q.generation,
                                  q.login,q.server,q.symbol,q.magic);
}
string RecoveryManualChartIdentity(const RecoveryInventoryScope &v,const ulong serial,
                             const RecoveryManualHint &hint) {
 return v.executor_epoch+"\t"+v.verifier_epoch+"\t"+(string)v.verifier_chart+"\t"+
        (string)v.login+"\t"+v.server+"\t"+v.symbol+"\t"+(string)v.magic+"\t"+
        (string)v.generation+"\t"+(string)serial+"\t"+(string)hint.position_id+"\t"+(string)hint.ticket;
}
RecoveryManualChartResult RecoveryManualChartRetryOnChange(RecoveryManualChart &task) {
 const RecoveryManualHint prior=task.hint;
 if(!RecoveryManualChartClearWaiting(task))return RMC_NO_ACTION;
 task.hint=prior;
 return RMC_WAIT;
}

bool RecoveryManualChartEffectVisible(const RecoveryManualOwner &owner) {
 if(owner.phase!=RMO_ATTEMPTED || owner.intent.opposite_ticket==0 ||
    !RecoveryOpeningFillStillThere(owner.range,owner.opening,owner.reverse_key,
                                  owner.check.price_tick,owner.check.volume_step))return false;
 double sl,tp;
 return PositionGetDouble(POSITION_SL,sl) && PositionGetDouble(POSITION_TP,tp) &&
    RecoveryDispatchUnits(sl,owner.check.price_tick,true,owner.intent.expected_sl_ticks) &&
    RecoveryDispatchUnits(tp,owner.check.price_tick,true,owner.intent.original_tp_ticks) &&
    OrderSelect(owner.intent.opposite_ticket);
}

// A panel WAIT is the only arm. No user time, clock window or persisted
// state. A duplicate/changing reply may never cause a second trade attempt.
RecoveryManualChartResult RecoveryManualChartTimer(RecoveryManualChart &task,
    RecoveryPanelControl &panel,const bool recovery_active,RecoveryEvidenceOwner &owner,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,
    const RRSettings &cfg,const double tick,const double step) {
 if(task.phase==RMC_ADOPTED)return RMC_NO_ACTION;
 if(recovery_active || !RecoveryManualMode(panel)) {
    RecoveryManualChartClearWaiting(task);
    return RMC_NO_ACTION;
 }
 if(task.phase==RMC_UNKNOWN || !RecoveryManualChartScope(owner,life,live,scope,verifier,cfg,tick,step))
    return RMC_NO_ACTION;
 if(task.phase!=RMC_IDLE && (!RecoveryAlternatingSameSession(task.initial,verifier) ||
                           tick!=task.tick || step!=task.step))return RMC_NO_ACTION;

 if(RecoveryManualChartMayCancel(task)) {
    RecoveryManualHint hint;
    if(!RecoveryManualChartHint(live.symbol,hint)) {
       RecoveryManualChartClearWaiting(task);
       return RMC_WAIT;
    }
    if(task.phase!=RMC_IDLE && !RecoveryManualHintEqual(hint,task.hint))
       RecoveryManualChartClearWaiting(task);
    if(task.phase==RMC_IDLE) {
       if(RecoveryManualHintEqual(hint,task.hint) || owner.serial==ULONG_MAX)return RMC_WAIT;
       const ulong serial=owner.serial+1;
       const string identity=RecoveryManualChartIdentity(verifier,serial,hint);
       task.initial=verifier;task.initial.nonce=RecoveryOwnerNonce("manual-inventory",identity);
       task.history.binding=verifier;task.history.binding.nonce=RecoveryOwnerNonce("manual-history",identity);
       task.confirm=verifier;task.confirm.nonce=RecoveryOwnerNonce("manual-adopt-confirm",identity);
       if(!RecoveryInvScopeOK(task.initial) || !RecoveryInvScopeOK(task.history.binding) ||
          !RecoveryInvScopeOK(task.confirm) || task.initial.nonce==task.history.binding.nonce ||
          task.confirm.nonce==task.history.binding.nonce || task.confirm.nonce==task.initial.nonce)
          return RMC_NO_ACTION;
       task.hint=hint;task.tick=tick;task.step=step;owner.serial=serial;task.phase=RMC_INVENTORY;
       if(!RecoveryManualPublishInventory(task.initial))return RecoveryManualChartRetryOnChange(task);
       return RMC_PROGRESS;
    }
 }

 if(task.phase==RMC_INVENTORY) {
    if(!RecoveryInvRead(task.initial,task.inventory))return RMC_WAIT;
    RecoveryManualCandidate f;
    if(!RecoveryManualCandidateFold(task.inventory,task.initial,0,tick,step,f) ||
       f.position_id!=task.hint.position_id || f.position_ticket!=task.hint.ticket)
       return RecoveryManualChartRetryOnChange(task);
    // MT5 POSITION_IDENTIFIER identifies the opening order, not necessarily
    // the current live position ticket. The matching ENTRY_IN still has to
    // be established independently before planning.
    task.history.position_identifier=f.position_id;task.history.anchor_order=f.position_id;
    const string tag=RecoveryOwnerNonce("manual-history-publisher",task.history.binding.nonce);
    task.phase=RMC_HISTORY;
    if(!RecoveryInvHex(tag) || tag==task.history.binding.nonce || !RecoveryHistoryRequest(task.history,tag))
       return RecoveryManualChartRetryOnChange(task);
    return RMC_PROGRESS;
 }
 if(task.phase==RMC_HISTORY) {
    RecoveryHistorySnapshot h;
    if(!RecoveryHistoryRead(task.history,h))return RMC_WAIT;
    if(!RecoveryPlanManualOpening(task.owner,panel,life,live,scope,cfg,task.initial,task.inventory,
                                  task.history,h,0,tick,step))
       return RecoveryManualChartRetryOnChange(task);
    task.phase=RMC_READY;
    return RMC_PROGRESS;
 }
 if(task.phase==RMC_READY) {
    const bool attempted=RecoveryAttemptManualOpening(task.owner,panel,life,live,scope);
    if(!attempted) {
       // The owner's false outcome is exclusively before broker dispatch.
       // An intervening SL/TP edit does not leave an in-flight attempt.
       ZeroMemory(task.owner);
       return RecoveryManualChartRetryOnChange(task);
    }
    if(task.owner.intent.opposite_ticket==0) {
       task.phase=RMC_UNKNOWN;return RMC_NO_ACTION;
    }
    task.phase=RMC_EFFECT_VISIBLE;return RMC_ATTEMPTED;
 }
 if(task.phase==RMC_EFFECT_VISIBLE) {
    if(!RecoveryManualChartEffectVisible(task.owner))return RMC_WAIT;
    task.phase=RMC_EFFECT_REPORT;
    if(!RecoveryManualPublishInventory(task.confirm)) {task.phase=RMC_UNKNOWN;return RMC_NO_ACTION;}
    return RMC_PROGRESS;
 }
 if(task.phase==RMC_EFFECT_REPORT) {
    RecoveryInventorySnapshot i;
    if(!RecoveryInvRead(task.confirm,i))return RMC_WAIT;
    if(RecoveryConfirmManualOpening(task.owner,panel,life,live,scope,task.confirm,i)!=RA_CONFIRMED) {
       task.phase=RMC_UNKNOWN;return RMC_NO_ACTION;
    }
    task.phase=RMC_ADOPTED;
    return RMC_PROOF;
 }
 return RMC_NO_ACTION;
}
#endif
