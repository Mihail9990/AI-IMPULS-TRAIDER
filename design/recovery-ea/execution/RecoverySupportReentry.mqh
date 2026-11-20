#ifndef RECOVERY_SUPPORT_REENTRY_MQH
#define RECOVERY_SUPPORT_REENTRY_MQH
#include "RecoverySupportCloseOwner.mqh"
#include "RecoveryDispatch.mqh"

enum RecoverySupportReentryState {
 RSRE_IDLE=0,RSRE_RECEIPT,RSRE_REQUEST,RSRE_READ,RSRE_WORKING,RSRE_UNRESOLVED
};
enum RecoverySupportReentryResult {RSRE_WAIT=0,RSRE_PUBLISHED,RSRE_CONFIRMED,RSRE_STOPPED};

struct RecoverySupportReentry {
 RecoverySupportReentryState state;
 ulong revision,ticket;
 RecoveryDispatchResult receipt;
 RecoveryExactEffectBinding prove;
 string publisher_tag;
};

bool RecoverySupportReentryBound(const RecoverySupportCloseOwner &close,
    const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier) {
 if((close.phase!=RSCO_STAGED && close.phase!=RSCO_DISPATCHED) ||
    owner.phase!=RRO_ORDINARY || owner.opening.reserved ||
    close.owner_revision==0 || close.owner_revision>=ULONG_MAX-1 ||
    owner.revision!=close.owner_revision+1 ||
    !RecoveryRangeOwnerBound(owner,panel,session,live,scope) ||
    !RecoveryAlternatingSameSession(close.working.request.binding,verifier) ||
    close.working_sequence<=close.working.verify_after ||
    close.working_sequence>=(ulong)LONG_MAX || close.gap.reserved ||
    !close.gap.have_close || close.gap.have_fill || close.gap.support_stage!=2 ||
    close.gap.before.actual_entry_ticks!=close.gap.support_level ||
    close.working.request.order_ticket!=close.gap.opposite.ticket ||
    close.gap.close.proof.exit_stop_ticks!=close.gap.support_level ||
    close.gap.close.reentry_distance!=panel.settings.initial_sl_ticks ||
    panel.settings.support_first_ticks!=0 || panel.settings.support_offset_ticks!=0)
    return false;
 const RRRangeState range=panel.ranges[owner.range_id-1];
 const RRAction action=close.reentry;
 if(range.phase!=RR_ARM_REENTRY || range.cycle.position_id!=0 || range.position_ticket!=0 ||
    range.entry.ticket!=0 || range.retire.ticket!=0 ||
    range.cycle.last_closed_position!=close.gap.before.position_id ||
    range.cycle.last_closed_order!=close.gap.before.active_order ||
    !RRKnownEqual(range.opposite,close.gap.opposite) || range.opposite.ticket==0 ||
    range.reentry_slot<1 || range.reentry_slot>24 || range.reentry_side!=close.gap.before.side ||
    !RRActionEqual(range.pending,action) || action.kind!=RR_REENTRY || action.side!=range.reentry_side ||
    action.serial!=range.action_serial || action.entry_ticks!=range.reentry_ticks ||
    action.entry_ticks!=close.gap.support_level+action.side*panel.settings.initial_sl_ticks ||
    action.sl_ticks!=close.gap.support_level || action.volume_steps!=panel.settings.lot_steps[range.reentry_slot-1])
    return false;
 RecoveryIntentKey expected;
 return RecoveryBuildSessionIntentKey(scope,action,close.working.price_tick,close.working.volume_step,expected) &&
    RecoveryIntentKeysMatch(expected,close.key) && RecoveryIntentContextOK(close.key);
}

bool RecoverySupportReentryReceiptBound(const RecoverySupportReentry &task,
    const RecoverySupportCloseOwner &close,const RecoveryRangeOwner &owner) {
 if(close.phase!=RSCO_DISPATCHED || task.revision!=owner.revision ||
    task.ticket==0 || task.ticket==close.gap.opposite.ticket ||
    task.ticket==close.gap.before.active_order || task.receipt.state!=RD_TICKET_RECORDED ||
    task.receipt.order!=task.ticket)return false;
 string recorded;
 return RecoveryIntentLookup(close.key,recorded)==RI_TICKET && recorded==(string)task.ticket;
}

// Consumes the close route before attempting dispatch. A rejection, timeout or
// missing ticket can never authorize this task to send again.
bool RecoverySubmitSupportReentry(RecoverySupportReentry &task,RecoverySupportCloseOwner &close,
    const RecoveryRangeOwner &owner,const RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier) {
 if(task.state!=RSRE_IDLE || close.phase!=RSCO_STAGED ||
    !RecoverySupportReentryBound(close,owner,panel,session,live,scope,verifier))return false;
 close.phase=RSCO_DISPATCHED;task.state=RSRE_UNRESOLVED;task.revision=owner.revision;
 const RecoveryDispatchState result=RecoveryDispatchOnce(panel.ranges[owner.range_id-1],close.reentry,
                       close.key,close.working.price_tick,close.working.volume_step,task.receipt);
 if(result==RD_TICKET_RECORDED && task.receipt.state==RD_TICKET_RECORDED) {
    task.ticket=task.receipt.order;
    if(RecoverySupportReentryReceiptBound(task,close,owner))task.state=RSRE_RECEIPT;
 }
 return true;
}

bool RecoveryBeginSupportReentryVerification(RecoverySupportReentry &task,
    const RecoverySupportCloseOwner &close,const RecoveryRangeOwner &owner,
    const RecoveryPanelControl &panel,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryInventoryScope &live_verifier,RecoveryEvidenceOwner &verifier) {
 if(task.state!=RSRE_RECEIPT || !RecoverySupportReentryReceiptBound(task,close,owner) ||
    !RecoverySupportReentryBound(close,owner,panel,session,live,scope,live_verifier) ||
    verifier.executor_epoch!=session.epoch || verifier.verifier_epoch!=live_verifier.verifier_epoch ||
    verifier.verifier_chart!=live_verifier.verifier_chart ||
    !RecoverySessionScopeEqual(verifier.context,live) || verifier.serial==ULONG_MAX)
    return false;
 const ulong serial=verifier.serial+1;
 const string identity=scope.executor_epoch+":"+verifier.verifier_epoch+":"+close.key.id+":"+
         (string)close.gap.opposite.ticket+":"+(string)task.ticket+":"+(string)serial;
 RecoveryExactEffectBinding effect;
 effect.request.binding=live_verifier;
 effect.request.binding.nonce=RecoveryOwnerNonce("support-reentry-exact",identity);
 effect.request.order_ticket=task.ticket;effect.action=close.reentry;
 effect.verify_after=close.working_sequence;
 effect.price_tick=close.working.price_tick;effect.volume_step=close.working.volume_step;
 const string publisher=RecoveryOwnerNonce("support-reentry-publisher",identity);
 if(!RecoveryInvHex(publisher) || publisher==effect.request.binding.nonce ||
    effect.request.binding.nonce==close.working.request.binding.nonce ||
    !RecoveryExactEffectBound(panel.ranges[owner.range_id-1],effect))return false;
 task.prove=effect;task.publisher_tag=publisher;task.state=RSRE_REQUEST;
 verifier.serial=serial;
 return true;
}

RecoverySupportReentryResult RecoveryStopSupportReentry(RecoverySupportReentry &task) {
 task.state=RSRE_UNRESOLVED;
 return RSRE_STOPPED;
}

RecoverySupportReentryResult RecoveryStepSupportReentry(RecoverySupportReentry &task,
    const RecoverySupportCloseOwner &close,RecoveryRangeOwner &owner,RecoveryPanelControl &panel,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier) {
 if(task.state!=RSRE_REQUEST && task.state!=RSRE_READ)return RSRE_STOPPED;
 if(!RecoverySupportReentryBound(close,owner,panel,session,live,scope,verifier) ||
    !RecoverySupportReentryReceiptBound(task,close,owner) ||
    !RecoveryAlternatingSameSession(task.prove.request.binding,verifier) ||
    task.prove.request.order_ticket!=task.ticket ||
    !RecoveryExactEffectBound(panel.ranges[owner.range_id-1],task.prove))
    return RecoveryStopSupportReentry(task);
 if(task.state==RSRE_REQUEST) {
    task.state=RSRE_READ;
    if(!RecoveryExactReportRequest(task.prove.request,task.publisher_tag))
       return RecoveryStopSupportReentry(task);
    return RSRE_PUBLISHED;
 }
 RecoveryExactReport report;
 if(!RecoveryExactReportRead(task.prove.request,report))return RSRE_WAIT;
 RREffect confirmed;
 if(!RecoveryExactEffectFold(report,task.prove,confirmed) || !confirmed.terminal_verified ||
    confirmed.created_order!=task.ticket ||
    !RecoverySupportReentryBound(close,owner,panel,session,live,scope,verifier) ||
    !RecoverySupportReentryReceiptBound(task,close,owner))
    return RecoveryStopSupportReentry(task);

 RRRangeState next=panel.ranges[owner.range_id-1];
 if(!RRConfirm(next,confirmed) || !RRHasSupportAlternatives(next) ||
    next.entry.ticket!=task.ticket || !RRKnownEqual(next.opposite,close.gap.opposite))
    return RecoveryStopSupportReentry(task);
 panel.ranges[owner.range_id-1]=next;owner.revision++;
 task.state=RSRE_WORKING;
 return RSRE_CONFIRMED;
}
#endif
