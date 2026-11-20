#ifndef RECOVERY_ORDINARY_EVENT_LOOP_MQH
#define RECOVERY_ORDINARY_EVENT_LOOP_MQH
#include "RecoveryAlternatingFillFallback.mqh"
#include "RecoveryAlternatingService.mqh"

struct RecoveryOrdinaryEventLoop {
 RecoveryFillHint hint;
 RecoveryAlternatingPump pump;
};

// One event loop per range of the single trading EA. OnInit allocates
// a fresh session and zeroes these values; none of them are restored.
void RecoveryOrdinaryEventReset(RecoveryOrdinaryEventLoop &loop)
  {
   ZeroMemory(loop);
  }

bool RecoveryOrdinaryEventScope(const RecoverySessionLifecycle &life,
   const RecoverySessionContext &live,const RecoveryInventoryScope &scope)
  {
   return RecoverySessionCurrent(life,live) &&
      life.epoch==scope.executor_epoch &&
      live.login==scope.login && live.server==scope.server &&
      live.symbol==scope.symbol && live.magic==scope.magic;
  }

// A pending pair's precise ticket comes from the current journal entry
// for its immutable staged action, never from a similar price or magic.
bool RecoveryOrdinaryTicketBound(const RecoveryAlternatingState &state,
   const RecoveryAlternatingEvidenceBinding &b)
  {
   if(b.exact.order_ticket==0)return false;
   if(!state.opening.reserved || state.opening.reverse_verified)
      return true; // Known/verified ticket is checked by the evidence module.
   const RecoveryInventoryScope i=b.inventory;
   RecoverySessionIntentScope key_scope;
   key_scope.executor_epoch=i.executor_epoch;
   key_scope.login=i.login;key_scope.server=i.server;key_scope.symbol=i.symbol;
   key_scope.magic=i.magic;key_scope.scope=b.scope;key_scope.generation=(ulong)i.generation;
   RecoveryIntentKey key;
   string exact;
   return RecoveryBuildSessionIntentKey(key_scope,state.opening.reverse,b.price_tick,b.volume_step,key) &&
      RecoveryIntentLookup(key,exact)==RI_TICKET && exact==(string)b.exact.order_ticket;
  }

bool RecoveryOrdinaryEventBind(RecoveryOrdinaryEventLoop &loop,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoveryAlternatingState &state,const RecoveryAlternatingEvidenceBinding &b,
    const string publisher_tag)
  {
   if(!RecoveryOrdinaryEventScope(life,live,b.inventory) || !RecoveryOrdinaryTicketBound(state,b))
      return false;
   if(loop.hint.state!=RFH_UNUSED)
     {
      // Do not replace a running verification, or rearm the same position.
      // The owner can explicitly initialize another range independently.
      if(loop.pump.state!=RAP_DONE ||
         loop.hint.prior_position==state.range.cycle.position_id) return false;
     }
   RecoveryFillHint next;
   ZeroMemory(next);
   if(!RecoveryArmFillHint(next,state.range,state.opening,b,publisher_tag))return false;
   RecoveryOrdinaryEventReset(loop);
   loop.hint=next;
   return true;
  }

// Fast notification handler: no history or broker operations in callback.
bool RecoveryOrdinaryOnTrade(const MqlTradeTransaction &trans,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoveryInventoryScope &live_verifier,RecoveryOrdinaryEventLoop &loop)
  {
   return RecoveryOrdinaryEventScope(life,live,live_verifier) &&
      RecoveryQueueFillHint(trans,live_verifier,loop.hint);
  }

// Timer owns read-only verification and applies only an exact finished proof.
// The broker receipt remains separate from verified effect and exit accounting.
RecoveryAlternatingServiceResult RecoveryOrdinaryOnTimer(
   const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
   const RecoveryInventoryScope &live_verifier,RecoveryOrdinaryEventLoop &loop,
   RecoveryAlternatingState &state,RecoveryAlternatingOutbox &outbox,
   const RRSettings &cfg,RecoverySessionAlternatingResult &result)
  {
   ZeroMemory(result);
   if(loop.hint.state==RFH_UNUSED)return RAS_WAITING;
   if(!RecoveryOrdinaryEventScope(life,live,live_verifier) ||
      !RecoveryFillHintContext(loop.hint,live_verifier))
     {
      loop.hint.state=RFH_DISARMED;
      RecoveryAlternatingPumpStop(loop.pump);
      return RAS_NO_EFFECT;
     }
   if(loop.pump.state==RAP_UNUSED)
     {
      if(loop.hint.state==RFH_ARMED) {
         RecoveryPollExactFillHint(loop.hint,live_verifier,state.range,state.opening);
         if(loop.hint.state==RFH_ARMED)return RAS_WAITING;
      }
      if(!RecoveryStartHintPump(loop.hint,live_verifier,state.range,state.opening,loop.pump))
         return RAS_NO_EFFECT;
     }
   return RecoveryServiceOrdinaryAlternating(life,live,loop.pump,state,outbox,cfg,result);
  }
#endif
