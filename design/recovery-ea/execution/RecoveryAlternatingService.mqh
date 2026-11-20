#ifndef RECOVERY_ALTERNATING_SERVICE_MQH
#define RECOVERY_ALTERNATING_SERVICE_MQH
#include "RecoveryAlternatingPump.mqh"
#include "RecoverySessionAlternating.mqh"
#include "RecoverySessionLifecycle.mqh"

enum RecoveryAlternatingServiceResult {
 RAS_WAITING=0, RAS_VERIFYING, RAS_ADVANCED, RAS_NO_EFFECT
};

// Intended for the one trading EA's timer handler. Earlier fill hints only
// start the read-only pump. Only its normalized full proof can stage an action.
// The caller owns range initialization, session epoch and single writer.
RecoveryAlternatingServiceResult RecoveryServiceOrdinaryAlternating(
    const RecoverySessionLifecycle &lifecycle,const RecoverySessionContext &live,
    RecoveryAlternatingPump &pump,RecoveryAlternatingState &state,
    RecoveryAlternatingOutbox &outbox,const RRSettings &cfg,
    RecoverySessionAlternatingResult &result)
  {
   ZeroMemory(result);
   const RecoveryInventoryScope scope=pump.binding.inventory;
   if(!RecoverySessionCurrent(lifecycle,live) ||
      lifecycle.epoch!=scope.executor_epoch || live.login!=scope.login ||
      live.server!=scope.server || live.symbol!=scope.symbol || live.magic!=scope.magic)
     {
      RecoveryAlternatingPumpStop(pump);
      return RAS_NO_EFFECT;
     }
   RRAlternatingObservation proof;
   const RecoveryAlternatingPumpResult status=RecoveryAlternatingPumpStep(pump,state.range,
                                                                          state.opening,proof);
   if(status==RAP_WAIT)return RAS_WAITING;
   if(status==RAP_PROGRESS)return RAS_VERIFYING;
   if(status!=RAP_PROOF)return RAS_NO_EFFECT;
   RecoverySessionIntentScope key_scope;
   key_scope.executor_epoch=scope.executor_epoch;
   key_scope.login=scope.login;key_scope.server=scope.server;key_scope.symbol=scope.symbol;
   key_scope.magic=scope.magic;key_scope.scope=pump.binding.scope;
   key_scope.generation=(ulong)scope.generation;
   if(!RecoveryApplyVerifiedAlternating(key_scope,state,outbox,cfg,proof,
                          pump.binding.price_tick,pump.binding.volume_step,result))return RAS_NO_EFFECT;
   // State advanced; result.dispatch is still only an attempted send, not a
   // verified broker effect. Do not retry from this service.
   return RAS_ADVANCED;
  }
#endif
