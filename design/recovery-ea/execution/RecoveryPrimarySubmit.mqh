#ifndef RECOVERY_PRIMARY_SUBMIT_MQH
#define RECOVERY_PRIMARY_SUBMIT_MQH
#include "RecoveryPanelControl.mqh"
#include "RecoverySessionLifecycle.mqh"
#include "RecoverySessionIntentKey.mqh"
#include "RecoveryDispatch.mqh"

struct RecoveryPrimarySubmitResult {
 RRAction action;
 RecoveryIntentKey key;
 RecoveryDispatchResult receipt;
 ulong ticket; // exact same-session receipt, not independent fill evidence
};

bool RecoveryPrimarySession(const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope) {
 return RecoverySessionCurrent(session,live) && session.epoch==scope.executor_epoch &&
    live.login==scope.login && live.server==scope.server && live.symbol==scope.symbol &&
    live.magic==scope.magic && scope.scope>0 && scope.generation>0;
}

// One writer only. On true the intent was staged before the sole dispatch
// attempt; pending remains for all outcomes, including unresolved and not sent.
// On false nothing changes, including any previous receipt retained by caller.
bool RecoverySubmitPanelPrimary(RecoveryPanelControl &panel,const int range_id,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const double tick,const double step,
    RecoveryPrimarySubmitResult &out) {
 if(!RecoveryPrimarySession(session,live,scope) ||
    panel.mode!=REC_CONTROL_RANGES || range_id<1 || range_id>2 ||
    !panel.configured[range_id-1] || panel.settings.scope!=scope.scope ||
    panel.settings.generation!=scope.generation) return false;

 RRRangeState staged=panel.ranges[range_id-1];
 if(staged.scope!=scope.scope || staged.cycle.generation!=scope.generation ||
    staged.cycle.range_id!=range_id || staged.phase!=RR_NEW || staged.action_serial!=0 ||
    staged.pending.kind!=RR_NONE || staged.cycle.active_order!=0 || staged.cycle.position_id!=0 ||
    staged.position_ticket!=0 || staged.entry.ticket!=0 || staged.opposite.ticket!=0 ||
    staged.retire.ticket!=0)
    return false;
 RecoveryPrimarySubmitResult result;
 ZeroMemory(result);
 if(!RRStage(staged,panel.settings,0,0,result.action) ||
    result.action.kind!=RR_PRIMARY || !RRActionEqual(staged.pending,result.action) ||
    !RecoveryBuildSessionIntentKey(scope,result.action,tick,step,result.key) ||
    !RecoveryIntentContextOK(result.key))
    return false;

 panel.ranges[range_id-1]=staged;
 const RecoveryDispatchState disposition=RecoveryDispatchOnce(panel.ranges[range_id-1],
                                      result.action,result.key,tick,step,result.receipt);
 if(disposition==RD_TICKET_RECORDED && result.receipt.state==RD_TICKET_RECORDED)
    result.ticket=result.receipt.order;
 out=result;
 return true;
}
#endif
