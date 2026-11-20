#ifndef RECOVERY_MANUAL_PENDING_CHART_MQH
#define RECOVERY_MANUAL_PENDING_CHART_MQH
#include "RecoveryManualPendingFill.mqh"
#include "RecoveryManualPendingEarly.mqh"
#include "RecoveryInventoryRequest.mqh"

enum RecoveryPendingChartState {
 RMPCH_IDLE=0,RMPCH_INVENTORY,RMPCH_BOUND,RMPCH_ACTIVE
};
struct RecoveryManualPendingChart {
 RecoveryPendingChartState state;
 RecoveryManualPendingOwner origin;
 RecoveryManualPendingFill fill;
 RecoveryManualPendingEarly early;
 RecoveryInventoryScope initial, confirm;
 RecoveryInventoryScope fill_history,fill_inventory,opening_confirm;
 string tag;
 ulong hint;
 bool confirm_requested,opening_requested;
 RecoveryPairSendResult opening_receipts;
};

bool RecoveryPendingChartManualOrder(const string symbol,ulong &ticket) {
 ticket=0;
 const int n=OrdersTotal();
 for(int i=0;i<n;i++) {
    const ulong id=OrderGetTicket(i);
    long magic,type,state,origin;
    string actual;
    if(id==0 || !OrderGetString(ORDER_SYMBOL,actual))return false;
    if(actual!=symbol)continue;
    if(!OrderGetInteger(ORDER_MAGIC,magic) || !OrderGetInteger(ORDER_TYPE,type) ||
       !OrderGetInteger(ORDER_STATE,state) || !OrderGetInteger(ORDER_REASON,origin))return false;
    if(magic!=0 || state!=ORDER_STATE_PLACED || !RecoveryManualPendingReason(origin) ||
       (type!=ORDER_TYPE_BUY_STOP && type!=ORDER_TYPE_SELL_STOP))continue;
    if(ticket!=0)return false;
    ticket=id;
 }
 return OrdersTotal()==n && ticket!=0;
}

bool RecoveryPendingChartSLVisible(const RecoveryManualPendingOwner &o) {
 double sl;
 return OrderSelect(o.original.ticket) && OrderGetDouble(ORDER_SL,sl) &&
        RecoveryDispatchUnits(sl,o.price_tick,false,o.target_sl);
}

// Accepting "cancel wait" drops only checks owned by this chart. No broker
// request, removal, SL undo, or history replay is possible from reset.
void RecoveryManualPendingChartReset(RecoveryManualPendingChart &task) {ZeroMemory(task);}

// Returns true once on transfer to regular RECOVERY. The order belonging to
// the user is never created or removed. Only the independently proved fill
// may start a new RECOVERY order.
bool RecoveryManualPendingChartTimer(RecoveryManualPendingChart &task,RecoveryManualHandoff &adopted,
    RecoveryPanelControl &panel,RecoveryEvidenceOwner &verifier,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &q,
    const RRSettings &cfg,const double tick,const double step) {
 if(panel.mode!=REC_CONTROL_PENDING_WAIT || adopted.seeded || task.state==RMPCH_ACTIVE ||
    !RecoveryManualPendingScope(life,live,scope,q) ||
    verifier.executor_epoch!=life.epoch || verifier.verifier_epoch!=q.verifier_epoch ||
    verifier.verifier_chart!=q.verifier_chart || !RecoverySessionScopeEqual(verifier.context,live))
    return false;
 if(task.state==RMPCH_IDLE) {
    ulong hint;
    if(!RecoveryPendingChartManualOrder(live.symbol,hint) ||
       !RecoveryCaptureManualPendingEarly(hint,live,tick,step,task.early))return false;
    if(verifier.serial==ULONG_MAX)return false;
    const ulong serial=verifier.serial+1;
    const string id=scope.executor_epoch+":"+(string)hint+":"+(string)serial;
    task.initial=q;task.confirm=q;task.fill_history=q;task.fill_inventory=q;task.opening_confirm=q;
    task.initial.nonce=RecoveryOwnerNonce("manual-pending-initial",id);
    task.confirm.nonce=RecoveryOwnerNonce("manual-pending-sl",id);
    task.fill_history.nonce=RecoveryOwnerNonce("manual-pending-fill-history",id);
    task.fill_inventory.nonce=RecoveryOwnerNonce("manual-pending-fill-inventory",id);
    task.opening_confirm.nonce=RecoveryOwnerNonce("manual-pending-opening",id);
    task.tag=RecoveryOwnerNonce("manual-pending-fill-publisher",id);
    if(!RecoveryInvScopeOK(task.initial) || !RecoveryInvScopeOK(task.confirm) ||
       !RecoveryInvScopeOK(task.fill_history) || !RecoveryInvScopeOK(task.fill_inventory) ||
       !RecoveryInvScopeOK(task.opening_confirm) ||
       !RecoveryInvHex(task.tag)) return false;
    verifier.serial=serial;task.hint=hint;
    if(RecoveryManualPublishInventory(task.initial))task.state=RMPCH_INVENTORY;
    return false;
 }
 if(!RecoveryAlternatingSameSession(task.initial,q))return false;
 if(task.state==RMPCH_INVENTORY) {
    RecoveryInventorySnapshot initial;
    if(!RecoveryInvRead(task.initial,initial))return false;
    RecoveryManualPendingCandidate candidate;
    if(RecoveryFindManualPending(initial,task.initial,0,0,tick,step,candidate)) {
       if(candidate.ticket!=task.hint || !RecoveryManualPendingEarlyTerms(candidate,task.early.original) ||
          !RecoveryPrepareManualPending(task.origin,true,life,live,scope,cfg,task.initial,initial,0,
                                       candidate.ticket,tick,step) ||
          task.origin.time_type!=task.early.time_type || task.origin.expiration!=task.early.expiration) {
          RecoveryManualPendingChartReset(task);return false;
       }
    } else if(!RecoveryPrepareManualPendingEarly(task.origin,true,life,live,scope,cfg,task.initial,
                                                   initial,task.early,tick,step))return false;
    task.state=RMPCH_BOUND;
 }
 if(task.state!=RMPCH_BOUND)return false;
 if(task.origin.phase==RMP_PREPARED)RecoveryAttemptManualPendingSL(task.origin,true,life,live,scope);
 // A ticket can fill before proof of the SL; binding the exact fill is not
 // conditional on a separate PLACED report.
 if(task.fill.state==RMPF_UNUSED)
    RecoveryManualPendingFillBegin(task.fill,task.origin,task.fill_history,task.fill_inventory,task.tag);
 if(task.origin.phase==RMP_ATTEMPTED) {
    if(!task.confirm_requested && RecoveryPendingChartSLVisible(task.origin))
       task.confirm_requested=RecoveryManualPublishInventory(task.confirm);
    if(task.confirm_requested) {
       RecoveryInventorySnapshot seen;
       if(RecoveryInvRead(task.confirm,seen))
          RecoveryConfirmManualPendingSL(task.origin,true,life,live,scope,task.confirm,seen);
    }
 }
 if(RecoveryManualPendingFillStep(task.fill,task.origin,scope.generation)!=RMPF_PROOF)return false;
 RecoveryManualHandoff prepared;
 ZeroMemory(prepared);
 if(!RecoveryProjectManualPendingOpening(prepared,task.fill,task.origin,panel,life,live,scope,cfg))return false;
 const RROpeningPair opening=prepared.ordinary.opening;
 RecoveryIntentKey reverse,sl;
 ZeroMemory(sl);
 if(!RecoveryBuildSessionIntentKey(scope,opening.reverse,tick,step,reverse) ||
    (opening.sl_required && !RecoveryBuildSessionIntentKey(scope,opening.sl,tick,step,sl)))return false;
 task.state=RMPCH_ACTIVE;
 adopted=prepared;
 panel.mode=REC_CONTROL_ADOPTED;
 RecoveryDispatchOpeningPair(adopted.backing.ranges[0],opening,sl,reverse,tick,step,task.opening_receipts);
 adopted.ordinary.next_exact_ticket=RecoveryRangeOwnerReceiptTicket(task.opening_receipts.reverse);
 return true;
}
// Fast reverse execution has priority; an opening snapshot must not replace
// the bound state under the exact ordinary fill pump.
void RecoveryManualPendingChartConfirmOpening(RecoveryManualPendingChart &task,
    RecoveryManualHandoff &handoff,const RecoverySessionLifecycle &life,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope) {
 if(task.state!=RMPCH_ACTIVE || !handoff.seeded || !handoff.ordinary.opening.sl_required ||
    !handoff.ordinary.opening.reserved || handoff.ordinary.next_exact_ticket==0 ||
    handoff.ordinary.ordinary.hint.state==RFH_QUEUED ||
    handoff.ordinary.ordinary.pump.state!=RAP_UNUSED)return;
 const RRRangeState p=handoff.backing.ranges[0];
 if(p.cycle.position_id!=task.fill.proof.entry.position_id || p.position_ticket!=task.fill.proof.position_ticket)
    return;
 if(!task.opening_requested) {
    double sl;
    if(!PositionSelectByTicket(p.position_ticket) || !PositionGetDouble(POSITION_SL,sl) ||
       !RecoveryDispatchUnits(sl,task.fill.tick,false,handoff.ordinary.opening.sl.sl_ticks) ||
       !OrderSelect(handoff.ordinary.next_exact_ticket))return;
    task.opening_requested=RecoveryManualPublishInventory(task.opening_confirm);
    return;
 }
 RecoveryInventorySnapshot a;
 if(RecoveryInvRead(task.opening_confirm,a) &&
    RecoveryManualPendingConfirmPair(handoff,task.fill,life,live,scope,task.opening_confirm,a)) {
    RecoveryOrdinaryEventReset(handoff.ordinary.ordinary);
    handoff.ordinary.armed_position=0;
 }
}
#endif
