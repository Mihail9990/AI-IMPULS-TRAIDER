#include "RecoveryAlternatingDispatch.mqh"

int main() {
 const auto cfg=config();
 for(int side : {-1,1}) {
  RecoveryAlternatingState current{};
  current.revision=41;current.range=first(cfg,side,1000);
  assert(RRStartAlternation(current.range,0,current.ledger));
  assert(RRStageOpeningPair(current.range,cfg,current.opening));
  const auto f=evidence(current.range,current.opening,51,1001,current.opening.reverse.entry_ticks+19);
  RecoveryAlternatingPlan plan{};
  RecoveryDispatchResult result{};
  RecoveryIntentKey key{cfg.generation,123};
  RRRangeState view{};
  auto partial=f;partial.fill.entry.history_complete=false;
  assert(RecoveryPreviewAlternating(current,cfg,partial,plan)==RR_ALT_UNRESOLVED);
  assert(!RecoveryAlternatingPlanCurrent(current,plan));
  assert(RecoveryPreviewAlternating(current,cfg,f,plan)==RR_ALT_NEXT_OPPOSITE);
  assert(RecoveryAlternatingPlanCurrent(current,plan)&&current.revision==41);
  assert(current.range.cycle.active_order==50&&current.ledger.strategy_slot==1);
  assert(plan.next.revision==42&&plan.next.range.cycle.active_order==51);
  assert(plan.next.ledger.strategy_slot==2&&plan.next.ledger.confirmed_full_sl_count==0);
  assert(!plan.next.ledger.positions[0].exit_recorded);
  const auto action=plan.next.opening.reverse;
  assert(action.side==side&&action.entry_ticks==f.fill.sl_ticks);
  assert(action.sl_ticks==action.entry_ticks-side*80);
  assert(action.entry_ticks!=f.fill.entry.actual_entry_ticks+side*80);
  assert(!RecoveryAlternatingDispatchView(current,42,action,view));
  auto stale=current;stale.revision++;
  assert(!RecoveryAlternatingPlanCurrent(stale,plan));
  RecoveryAlternatingOutbox outbox{};
  auto persisted=current;
  assert(!RecoveryCommitSessionAlternating(stale,plan,outbox)&&outbox.revision==0);
  assert(RecoveryCommitSessionAlternating(persisted,plan,outbox));
  assert(outbox.revision==42&&RRActionEqual(outbox.action,action));
  assert(!RecoveryCommitSessionAlternating(persisted,plan,outbox));
  assert(!RecoveryAlternatingPlanCurrent(persisted,plan));
  assert(RecoveryAlternatingDispatchView(persisted,42,action,view)&&RRActionEqual(view.pending,action));
  auto wrong=action;wrong.sl_ticks++;
  ResetDispatchFixture();
  assert(RecoverySendCommittedAlternating(current,42,action,key,.01,.01,result)==RPS_NOT_STARTED);
  assert(RecoverySendCommittedAlternating(persisted,41,action,key,.01,.01,result)==RPS_NOT_STARTED);
  assert(RecoverySendCommittedAlternating(persisted,42,wrong,key,.01,.01,result)==RPS_NOT_STARTED);
  assert(DispatchFixtureAttempts()==0);
  assert(RecoverySendCommittedAlternating(persisted,42,action,key,.01,.01,result)==RPS_AWAIT_VERIFIER);
  assert(RecoverySendCommittedAlternating(persisted,42,action,key,.01,.01,result)==RPS_REVERSE_NOT_SENT);
  assert(DispatchFixtureAttempts()==1);
  assert(RecoveryPreviewAlternating(persisted,cfg,f,plan)==RR_ALT_UNRESOLVED);
  assert(!RecoveryAlternatingPlanCurrent(persisted,plan)&&plan.result==RR_ALT_UNRESOLVED);
  for(int n=3;n<=24;n++) {
     auto next=evidence(persisted.range,persisted.opening,50+n,1000+n,persisted.opening.reverse.entry_ticks-11);
     assert(RecoveryPreviewAlternating(persisted,cfg,next,plan)==(n==24?RR_ALT_FINAL_PROTECTED:RR_ALT_NEXT_OPPOSITE));
     assert(RecoveryAlternatingPlanCurrent(persisted,plan));
     assert(RecoveryCommitSessionAlternating(persisted,plan,outbox));
     assert(outbox.revision==(n==24 ? 0 : persisted.revision));
  }
  assert(!persisted.opening.reserved);
  assert(!RecoveryAlternatingDispatchView(persisted,persisted.revision,action,view));
 }
}
