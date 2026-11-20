#ifndef RECOVERY_OCO_CANCEL_MQH
#define RECOVERY_OCO_CANCEL_MQH
#include "RecoveryPrimaryService.mqh"
#include "RecoveryExactOrderEffect.mqh"

enum RecoveryOcoCancelState {ROC_IDLE=0,ROC_DISPATCH,ROC_PROBE,ROC_READ,ROC_DONE,ROC_UNRESOLVED};
enum RecoveryOcoCancelResult {ROC_WAIT=0,ROC_DISPATCH_CHECKED,ROC_REQUESTED,ROC_CANCELED,ROC_UNKNOWN};
struct RecoveryOcoCancel {
 RecoveryOcoCancelState state;
 int winner_id,loser_id;
 ulong winner_order,winner_position;
 RecoveryPrimarySubmitResult losing;
 RecoveryExactEffectBinding verify;
 RecoveryIntentKey cancel_key;
 string publisher_tag;
 RecoveryDispatchResult receipt;
};

bool RecoveryOcoCancelContext(const RecoveryOcoCancel &c,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryPanelControl &panel) {
 const RecoveryInventoryScope q=c.verify.request.binding;
 return RecoveryPrimarySession(session,live,scope) && RecoveryInvScopeOK(q) &&
    q.executor_epoch==session.epoch && q.verifier_chart!=live.chart &&
    q.login==live.login && q.server==live.server && q.symbol==live.symbol && q.magic==live.magic &&
    q.generation>0 && (ulong)q.generation==scope.generation &&
    panel.mode==REC_CONTROL_RANGES && c.winner_id>=1 && c.winner_id<=2 &&
    c.loser_id>=1 && c.loser_id<=2 && c.loser_id!=c.winner_id &&
    panel.configured[c.winner_id-1] && panel.configured[c.loser_id-1] &&
    panel.settings.scope==scope.scope && panel.settings.generation==scope.generation &&
    c.losing.ticket!=0 && c.losing.ticket==c.verify.request.order_ticket &&
    c.losing.ticket!=c.winner_order && c.winner_order!=0 && c.winner_position!=0;
}

bool RecoveryOcoWinner(const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RRRangeState &winner,const RRSettings &cfg,
    const RecoveryPrimaryFillBinding &b,const RRFilled &f) {
 if(!RecoveryPrimaryOpenBound(b,session,live,scope,winner) ||
    !RecoveryAlternatingSameSession(b.exact.binding,b.history.binding) ||
    !RecoveryAlternatingSameSession(b.history.binding,b.inventory) ||
    !f.independently_verified || !f.entry.independently_verified ||
    !f.entry.history_complete || !f.entry.remainder_reconciled ||
    f.entry.generation!=scope.generation || f.entry.range_id!=winner.cycle.range_id ||
    f.entry.expected_order==0 || f.entry.expected_order!=b.exact.order_ticket ||
    f.entry.executed_order!=b.exact.order_ticket || f.entry.executed_order!=b.history.anchor_order ||
    f.entry.position_id==0 || f.entry.position_id!=b.history.position_identifier ||
    f.entry.side!=winner.primary_side || f.entry.actual_entry_ticks<=0 ||
    f.entry.filled_steps<=0 || f.entry.active_steps!=f.entry.filled_steps ||
    f.entry.filled_steps>f.entry.planned_steps || f.position_ticket==0 ||
    f.working_other_ticket!=0 || f.tp_ticks!=0 || winner.scope!=scope.scope ||
    winner.cycle.generation!=scope.generation || !RRValid(winner,cfg))return false;
 if(winner.cycle.active_order==f.entry.executed_order &&
    winner.cycle.position_id==f.entry.position_id && winner.position_ticket==f.position_ticket &&
    winner.cycle.actual_entry_ticks==f.entry.actual_entry_ticks &&
    winner.cycle.filled_steps==f.entry.filled_steps && winner.cycle.side==f.entry.side &&
    RCCurrentSlot(winner.cycle)==1) return true;
 // Arbitration may hand us the verified winner immediately before promoting
 // it. Prove the identical first transition on a scratch copy only.
 RRRangeState test=winner;
 RROpeningPair pair;
 RRAlternationLedger ledger;
 ZeroMemory(pair);ZeroMemory(ledger);
 return RecoveryStagePrimaryOpening(test,cfg,b,f,pair,ledger);
}

// Build a transport-only DRAIN view around the actual known primary ticket.
// Never apply it to panel state; existing typed dispatch checks that the
// ticket is an untouched working STOP at submit time, not another order.
bool RecoveryOcoCancelView(const RecoveryOcoCancel &c,const RecoverySessionIntentScope &scope,
    const RecoveryPanelControl &panel,RRRangeState &view,RRAction &action) {
 const RRRangeState losing=panel.ranges[c.loser_id-1];
 const RRAction primary=c.losing.action;
 if(!RRValid(losing,panel.settings) || primary.kind!=RR_PRIMARY ||
    primary.scope!=scope.scope || primary.generation!=scope.generation ||
    primary.range_id!=c.loser_id || losing.cycle.range_id!=c.loser_id ||
    primary.serial==0 || primary.serial!=losing.action_serial ||
    primary.order_ticket!=0 || primary.position_ticket!=0 || primary.position_id!=0 ||
    primary.side!=losing.primary_side || primary.entry_ticks!=losing.primary_ticks ||
    primary.tp_ticks!=0 || primary.stage!=0 || losing.cycle.active_order!=0 ||
    losing.cycle.position_id!=0 || losing.position_ticket!=0 || losing.opposite.ticket!=0 ||
    losing.retire.ticket!=0 || losing.reentry_slot!=0 || RCCurrentSlot(losing.cycle)!=1 ||
    c.losing.receipt.state!=RD_TICKET_RECORDED || c.losing.receipt.order!=c.losing.ticket)
    return false;
 RRAction expected;
 RRClearAction(expected);
 if(!RRMakeEntry(panel.settings,losing.primary_side,losing.primary_ticks,1,expected) ||
    primary.side!=expected.side || primary.entry_ticks!=expected.entry_ticks ||
    primary.sl_ticks!=expected.sl_ticks || primary.volume_steps!=expected.volume_steps)
    return false;
 RRKnownOrder known;
 RRSetKnown(known,c.losing.ticket,primary);
 if(losing.phase==RR_NEW) {
    if(losing.entry.ticket!=0 || !RRActionEqual(losing.pending,primary))return false;
 } else if(losing.phase==RR_WAIT) {
    if(losing.pending.kind!=RR_NONE || !RRKnownEqual(losing.entry,known))return false;
 } else return false;

 RecoveryIntentKey expected_key;
 string ticket;
 if(!RecoveryBuildSessionIntentKey(scope,primary,c.verify.price_tick,c.verify.volume_step,expected_key) ||
    !RecoveryIntentKeysMatch(expected_key,c.losing.key) ||
    RecoveryIntentLookup(c.losing.key,ticket)!=RI_TICKET || ticket!=(string)c.losing.ticket)
    return false;
 view=losing;
 view.phase=RR_DRAIN;
 view.entry=known;
 RRClearAction(view.pending);
 return RRStage(view,panel.settings,0,0,action) &&
        action.kind==RR_CANCEL && action.order_ticket==known.ticket;
}

bool RecoveryBeginOcoCancel(RecoveryOcoCancel &c,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryPanelControl &panel,const int winner_id,const RecoveryPrimaryFillBinding &winner_binding,
    const RRFilled &winner_fill,const int loser_id,const RecoveryPrimarySubmitResult &loser,
    const RecoveryExactReportScope &request,const ulong verify_after,const string publisher_tag,
    const double tick,const double step) {
 if(c.state!=ROC_IDLE || !RecoveryInvHex(publisher_tag) || publisher_tag==request.binding.nonce ||
    verify_after>=(ulong)LONG_MAX || !MathIsValidNumber(tick) || tick<=0 ||
    !MathIsValidNumber(step) || step<=0) return false;
 RecoveryOcoCancel p;
 ZeroMemory(p);
 p.state=ROC_DISPATCH;p.winner_id=winner_id;p.loser_id=loser_id;
 p.winner_order=winner_fill.entry.executed_order;p.winner_position=winner_fill.entry.position_id;
 p.losing=loser;p.verify.request=request;p.verify.verify_after=verify_after;
 p.publisher_tag=publisher_tag;p.verify.price_tick=tick;p.verify.volume_step=step;
 const RecoveryInventoryScope q=request.binding;
 if(!RecoveryOcoCancelContext(p,session,live,scope,panel) ||
    !RecoveryAlternatingSameSession(q,winner_binding.inventory) ||
    q.nonce==winner_binding.exact.binding.nonce || q.nonce==winner_binding.history.binding.nonce ||
    q.nonce==winner_binding.inventory.nonce ||
    !RecoveryOcoWinner(session,live,scope,panel.ranges[winner_id-1],panel.settings,winner_binding,winner_fill))
    return false;
 RRRangeState view;
 if(!RecoveryOcoCancelView(p,scope,panel,view,p.verify.action) ||
    !RecoveryExactEffectBound(view,p.verify) ||
    !RecoveryBuildSessionIntentKey(scope,p.verify.action,tick,step,p.cancel_key))return false;
 c=p;return true;
}

RecoveryOcoCancelResult RecoveryStopOcoCancel(RecoveryOcoCancel &c) {
 c.state=ROC_UNRESOLVED;return ROC_UNKNOWN;
}

// Does not mutate owner state. Only a separately verified exact historical
// CANCELED may be consumed by the owner as cancellation. No loser fill path.
RecoveryOcoCancelResult RecoveryStepOcoCancel(RecoveryOcoCancel &c,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,const RecoveryPanelControl &panel) {
 if(c.state!=ROC_DISPATCH && c.state!=ROC_PROBE && c.state!=ROC_READ)return ROC_UNKNOWN;
 if(!RecoveryOcoCancelContext(c,session,live,scope,panel))
    return RecoveryStopOcoCancel(c);
 if(c.state==ROC_DISPATCH) {
    c.state=ROC_UNRESOLVED; // Consume before any attempt, even on ambiguity.
    RRRangeState view;RRAction action;
    if(!RecoveryOcoCancelView(c,scope,panel,view,action) ||
       !RRActionEqual(c.verify.action,action) || !RecoveryExactEffectBound(view,c.verify))
       return ROC_UNKNOWN;
    RecoveryIntentKey expected;
    if(!RecoveryBuildSessionIntentKey(scope,action,c.verify.price_tick,c.verify.volume_step,expected) ||
       !RecoveryIntentKeysMatch(expected,c.cancel_key))return ROC_UNKNOWN;
    if(RecoveryDispatchExactCancelAsync(view,action,c.cancel_key,c.verify.price_tick,c.verify.volume_step,c.receipt)
       !=RD_OUTCOME_UNRESOLVED)return ROC_UNKNOWN;
    c.state=ROC_PROBE;return ROC_DISPATCH_CHECKED;
 }
 if(c.state==ROC_PROBE) {
    c.state=ROC_READ;
    if(!RecoveryExactReportRequest(c.verify.request,c.publisher_tag))return RecoveryStopOcoCancel(c);
    return ROC_REQUESTED;
 }
 RecoveryExactReport report;
 if(!RecoveryExactReportRead(c.verify.request,report))return ROC_WAIT;
 RREffect effect;
 if(!RecoveryExactEffectFold(report,c.verify,effect) || !effect.terminal_verified ||
    effect.created_order!=0 || !RRActionEqual(c.verify.action,effect.action) ||
    !RecoveryOcoCancelContext(c,session,live,scope,panel))
    return RecoveryStopOcoCancel(c);
 c.state=ROC_DONE;return ROC_CANCELED;
}
#endif
