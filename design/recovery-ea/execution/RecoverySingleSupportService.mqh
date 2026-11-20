#ifndef RECOVERY_SINGLE_SUPPORT_SERVICE_MQH
#define RECOVERY_SINGLE_SUPPORT_SERVICE_MQH
#include "RecoveryWatchedSupport.mqh"
#include "RecoverySessionIntentKey.mqh"
#include "RecoverySessionLifecycle.mqh"
#include "RecoveryDispatch.mqh"
#include "RecoveryPositionEffect.mqh"

struct RecoverySingleSupport {
 RecoveryPriceScope request;
 int state; // 0 unstarted, 1 observing, 2 submitted or uncertain
 RRAction action;
 RecoveryIntentKey key;
};
bool RecoverySupportSession(const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
                             const RecoveryPriceScope &watch)
  {
   return RecoverySessionCurrent(session,live) && session.epoch==watch.executor_epoch &&
          live.login==watch.login && live.server==watch.server && live.symbol==watch.symbol &&
          AccountInfoInteger(ACCOUNT_LOGIN)==live.login &&
          AccountInfoString(ACCOUNT_SERVER)==live.server && _Symbol==live.symbol;
  }

// Bound subscription supplied by the chart owner for this exact position.
// By design the only production watcher stage is 2: move directly to entry.
bool RecoveryStartSingleSupport(RecoverySingleSupport &task,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RRRangeState &range,const RRSettings &cfg,
    const RecoveryPriceScope &request,const string publisher_tag,const double tick)
  {
   if(task.state!=0 || cfg.support_first_ticks!=0 || cfg.support_offset_ticks!=0 ||
      range.support_stage!=0 || !RecoverySupportSession(session,live,request) ||
      !RecoveryWatchedScope(range,cfg,request,tick)) return false;
   task.request=request;task.state=1;
   return RecoveryPriceFollow(request,2,publisher_tag);
  }

bool RecoverySubmitSingleSupport(RecoverySingleSupport &task,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,RRRangeState &range,
    const RRSettings &cfg,const double tick,const double step,RecoveryDispatchResult &receipt)
  {
   ZeroMemory(receipt);
   if(task.state!=1 || cfg.support_first_ticks!=0 || cfg.support_offset_ticks!=0 ||
      !RecoverySupportSession(session,live,task.request) ||
      scope.executor_epoch!=session.epoch || scope.login!=live.login || scope.server!=live.server ||
      scope.symbol!=live.symbol || scope.magic!=live.magic || scope.scope!=range.scope ||
      scope.generation!=range.cycle.generation) return false;
   RRRangeState next=range;
   RRAction action;
   RecoveryIntentKey key;
   if(!RecoveryReadWatchedPeak(next,cfg,task.request,0,tick,action) ||
      action.kind!=RR_SUPPORT_SL || action.stage!=2 ||
      action.sl_ticks!=range.cycle.actual_entry_ticks ||
      !RecoveryBuildSessionIntentKey(scope,action,tick,step,key) || !RecoveryIntentContextOK(key))
      return false;
   range=next;task.action=action;task.key=key;task.state=2;
   RecoveryDispatchOpeningSLAsync(range,action,key,tick,step,receipt);
   return true; // staged once; broker confirmation comes separately
  }

// Only independent, exact position response can mark actual protection moved.
bool RecoveryConfirmSingleSupport(const RecoverySingleSupport &task,RRRangeState &range,
                                   const RecoveryPositionEffectBinding &proof)
  {
   if(task.state!=2 || !RRActionEqual(task.action,proof.action) ||
      proof.request.executor_epoch!=task.request.executor_epoch ||
      proof.request.login!=task.request.login || proof.request.server!=task.request.server ||
      proof.request.symbol!=task.request.symbol ||
      proof.request.generation!=(long)task.request.generation ||
      proof.request.magic!=task.key.magic) return false;
   RREffect effect;
   return RecoveryReadPositionEffect(range,proof,effect) && RRConfirm(range,effect);
  }
#endif
