#ifndef RECOVERY_SESSION_ALTERNATING_MQH
#define RECOVERY_SESSION_ALTERNATING_MQH
#include "RecoveryAlternatingDispatch.mqh"
#include "RecoverySessionIntentKey.mqh"

struct RecoverySessionAlternatingResult {
 RRAlternatingResult transition;
 RecoveryPairSendState dispatch;
 RecoveryIntentKey key;
 RecoveryDispatchResult receipt;
};

// The single writer invokes this only on fully normalized, independent
// evidence for a specific current cycle. No timers, replays, or strategy
// close-accounting are triggered here. A send receipt is not effect evidence.
bool RecoveryApplyVerifiedAlternating(const RecoverySessionIntentScope &scope,
     RecoveryAlternatingState &state,RecoveryAlternatingOutbox &outbox,
     const RRSettings &cfg,const RRAlternatingObservation &proof,
     const double tick,const double step,RecoverySessionAlternatingResult &result)
  {
   ZeroMemory(result);
   if(scope.scope!=state.range.scope || scope.generation!=state.range.cycle.generation ||
      scope.login<=0 || AccountInfoInteger(ACCOUNT_LOGIN)!=scope.login ||
      AccountInfoString(ACCOUNT_SERVER)!=scope.server || _Symbol!=scope.symbol ||
      !RecoveryInvHex(scope.executor_epoch) || scope.magic<=0) return false;
   RecoveryAlternatingPlan plan;
   const RRAlternatingResult staged=RecoveryPreviewAlternating(state,cfg,proof,plan);
   if(staged==RR_ALT_UNRESOLVED) return false;
   RecoveryIntentKey key;
   if(staged==RR_ALT_NEXT_OPPOSITE &&
      (!RecoveryBuildSessionIntentKey(scope,plan.next.opening.reverse,tick,step,key) ||
       !RecoveryIntentContextOK(key))) return false;
   if(!RecoveryCommitSessionAlternating(state,plan,outbox)) return false;
   result.transition=staged;
   result.dispatch=RPS_NOT_STARTED;
   if(staged==RR_ALT_NEXT_OPPOSITE)
     {
      result.key=key;
      result.dispatch=RecoverySendCommittedAlternating(state,outbox.revision,outbox.action,
                                              key,tick,step,result.receipt);
     }
   return true;
  }
#endif
