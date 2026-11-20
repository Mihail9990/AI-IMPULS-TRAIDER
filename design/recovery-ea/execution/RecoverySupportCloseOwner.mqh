#ifndef RECOVERY_SUPPORT_CLOSE_OWNER_MQH
#define RECOVERY_SUPPORT_CLOSE_OWNER_MQH
#include "RecoveryRangeOwner.mqh"
#include "RecoveryExactOrderEffect.mqh"
#include "../cycle/RecoverySupportGap.mqh"

enum RecoverySupportClosePhase { RSCO_IDLE=0, RSCO_REQUEST, RSCO_READ, RSCO_STAGED, RSCO_UNRESOLVED, RSCO_DISPATCHED };
enum RecoverySupportCloseResult { RSCO_WAIT=0, RSCO_PUBLISHED, RSCO_REENTRY_READY, RSCO_STOPPED };

// Owned only by the running chart. No persistence and no trade IO here.
struct RecoverySupportCloseOwner {
 RecoverySupportClosePhase phase;
 RRSupportGap gap;
 RecoveryExactEffectBinding working;
 string publisher_tag;
 ulong owner_revision,working_sequence;
 RRAction reentry;
 RecoveryIntentKey key;
};

bool RecoverySupportCloseOwnerBound(const RecoverySupportCloseOwner &task,
   const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
   const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
   const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier) {
 if(owner.phase!=RRO_ORDINARY || owner.opening.reserved || task.owner_revision==0 ||
    owner.revision!=task.owner_revision || owner.revision==ULONG_MAX ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    !RecoveryAlternatingSameSession(task.working.request.binding,verifier) ||
    panel.settings.support_first_ticks!=0 || panel.settings.support_offset_ticks!=0)
    return false;
 const RRRangeState range=panel.ranges[owner.range_id-1];
 return RRAltBound(range,owner.ledger) && RRSupportGapBound(range,task.gap) &&
        task.gap.have_close && !task.gap.have_fill && task.gap.support_stage==2 &&
        task.gap.support_level==range.cycle.actual_entry_ticks &&
        task.gap.close.reentry_distance==panel.settings.initial_sl_ticks &&
        task.working.request.order_ticket==task.gap.opposite.ticket;
}

// close is the already verified close from RCCPStageFromWire; close_sequence
// is the higher sequence of those two verifier responses. The owner's serial
// allocates a new exact ticket challenge AFTER that proof. No order search.
bool RecoveryBeginSupportCloseOwner(RecoverySupportCloseOwner &task,
   const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
   const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
   const RecoverySessionIntentScope &scope,RecoveryEvidenceOwner &verifier,
   const RCCloseCheckpoint &close,const ulong close_sequence,const double tick,const double step) {
 if(task.phase!=RSCO_IDLE || owner.phase!=RRO_ORDINARY || owner.opening.reserved ||
    owner.revision==0 || owner.revision==ULONG_MAX ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    verifier.executor_epoch!=session.epoch || !RecoverySessionScopeEqual(verifier.context,live) ||
    verifier.serial==ULONG_MAX || close_sequence==0 || close_sequence>=(ulong)LONG_MAX ||
    !MathIsValidNumber(tick) || tick<=0 || !MathIsValidNumber(step) || step<=0 ||
    panel.settings.support_first_ticks!=0 || panel.settings.support_offset_ticks!=0 ||
    close.reentry_distance!=panel.settings.initial_sl_ticks)
    return false;
 const RRRangeState range=panel.ranges[owner.range_id-1];
 if(!RRAltBound(range,owner.ledger) || range.support_stage!=2 ||
    range.live_sl!=range.cycle.actual_entry_ticks)return false;

 RecoverySupportCloseOwner next;
 ZeroMemory(next);
 if(!RRStartSupportGap(range,next.gap)||!RRStageSupportGapClose(range,next.gap,close))return false;
 const ulong serial=verifier.serial+1;
 const string identity=scope.executor_epoch+"\t"+verifier.verifier_epoch+"\t"+
     (string)verifier.verifier_chart+"\t"+(string)live.login+"\t"+live.server+"\t"+live.symbol+"\t"+
     (string)live.magic+"\t"+(string)scope.scope+"\t"+(string)scope.generation+"\t"+
     (string)owner.range_id+"\t"+(string)close.key.position_id+"\t"+(string)close.key.anchor_order+"\t"+
     (string)close.key.observation+"\t"+(string)next.gap.opposite.ticket+"\t"+(string)serial;
 RecoveryInventoryScope req;
 req.nonce=RecoveryOwnerNonce("support-close-exact",identity);
 req.executor_epoch=scope.executor_epoch;req.verifier_epoch=verifier.verifier_epoch;
 req.generation=(long)scope.generation;req.login=live.login;req.server=live.server;
 req.symbol=live.symbol;req.magic=live.magic;req.verifier_chart=verifier.verifier_chart;
 next.publisher_tag=RecoveryOwnerNonce("support-close-publisher",identity);
 if(!RecoveryInvScopeOK(req) || req.verifier_chart==live.chart ||
    !RecoveryInvHex(next.publisher_tag) || req.nonce==next.publisher_tag)return false;
 next.working.request.binding=req;next.working.request.order_ticket=next.gap.opposite.ticket;
 next.working.verify_after=close_sequence;next.working.price_tick=tick;next.working.volume_step=step;
 // Reuse only the exact *working row* decoder. This descriptor is not an
 // order attempt, is never passed to dispatch and has no action serial.
 RRAction shape;
 RRClearAction(shape);
 shape.kind=RR_REVERSE;shape.side=next.gap.opposite.side;
 shape.entry_ticks=next.gap.opposite.entry_ticks;shape.sl_ticks=next.gap.opposite.sl_ticks;
 shape.volume_steps=next.gap.opposite.volume_steps;
 next.working.action=shape;
 next.owner_revision=owner.revision;next.phase=RSCO_REQUEST;
 task=next;verifier.serial=serial;
 return true;
}

RecoverySupportCloseResult RecoveryStopSupportCloseOwner(RecoverySupportCloseOwner &task) {
 task.phase=RSCO_UNRESOLVED;
 return RSCO_STOPPED;
}

// Read the newly allocated current-order report once. Historical/filled,
// partial, missing or mismatched responses never allow adding the old-side
// STOP. The in-memory reentry action is staged only; the caller owns dispatch,
// the final exact target recheck and downstream dual-fill handling.
RecoverySupportCloseResult RecoveryStepSupportCloseOwner(RecoverySupportCloseOwner &task,
   RecoveryRangeOwner &owner,RecoveryPanelControl &panel,
   const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
   const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier) {
 if(task.phase!=RSCO_REQUEST && task.phase!=RSCO_READ)return RSCO_STOPPED;
 if(!RecoverySupportCloseOwnerBound(task,owner,panel,session,live,scope,verifier))
    return RecoveryStopSupportCloseOwner(task);
 if(task.phase==RSCO_REQUEST) {
    task.phase=RSCO_READ;
    if(!RecoveryExactReportRequest(task.working.request,task.publisher_tag))
       return RecoveryStopSupportCloseOwner(task);
    return RSCO_PUBLISHED;
 }
 RecoveryExactReport report;
 if(!RecoveryExactReportRead(task.working.request,report))return RSCO_WAIT;
 RREffect decoded;
 if(!RecoveryExactEffectFold(report,task.working,decoded) || !decoded.terminal_verified ||
    decoded.created_order!=task.gap.opposite.ticket ||
    !RecoverySupportCloseOwnerBound(task,owner,panel,session,live,scope,verifier))
    return RecoveryStopSupportCloseOwner(task);

 RRKnownOrder working;
 RRSetKnown(working,decoded.created_order,decoded.action);
 RRRangeState next=panel.ranges[owner.range_id-1];
 RRSupportGap gap=task.gap;
 RRAlternationLedger ledger=owner.ledger;
 const int count=ledger.confirmed_full_sl_count;
 const RRAlternatingCloseResult recorded=RRRecordAlternatingClose(ledger,gap.close);
 if((recorded!=RR_ALT_CLOSE_APPLIED && recorded!=RR_ALT_CLOSE_DUPLICATE) ||
    ledger.confirmed_full_sl_count!=count ||
    !RRReleaseSupportGapToReentry(next,gap,working,true))
    return RecoveryStopSupportCloseOwner(task);

 RRAction action;
 RecoveryIntentKey key;
 if(!RRStage(next,panel.settings,0,0,action) || action.kind!=RR_REENTRY ||
    action.side!=task.gap.before.side ||
    action.entry_ticks!=task.gap.support_level+action.side*panel.settings.initial_sl_ticks ||
    action.sl_ticks!=task.gap.support_level || !RRKnownEqual(next.opposite,working) ||
    !RecoveryBuildSessionIntentKey(scope,action,task.working.price_tick,task.working.volume_step,key) ||
    !RecoveryIntentContextOK(key))
    return RecoveryStopSupportCloseOwner(task);

 panel.ranges[owner.range_id-1]=next;
 owner.ledger=ledger;owner.revision++;
 task.gap=gap;task.reentry=action;task.key=key;task.working_sequence=(ulong)report.sequence;
 task.phase=RSCO_STAGED;
 return RSCO_REENTRY_READY;
}
#endif
