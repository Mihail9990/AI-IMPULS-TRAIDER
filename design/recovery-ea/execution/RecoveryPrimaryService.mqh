#ifndef RECOVERY_PRIMARY_SERVICE_MQH
#define RECOVERY_PRIMARY_SERVICE_MQH
#include "RecoveryPrimarySubmit.mqh"
#include "RecoveryPrimaryFill.mqh"
#include "RecoveryOpeningDispatch.mqh"

struct RecoveryPrimaryOpenResult {
 RecoveryPairSendState disposition;
 RecoveryPairSendResult receipts;
 RecoveryIntentKey sl_key,reverse_key;
};

// Called after the independent verifier reports have arrived. Read exact
// evidence, commit the opening pair in the current session, then submit its
// opposite STOP at the configured distance from the actual fill. The live
// position's SL is observed, never changed merely to align the new trigger.
bool RecoveryOpenVerifiedPrimary(
    const RecoveryPrimaryFillBinding &binding,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    RRRangeState &range,const RRSettings &cfg,RROpeningPair &opening,
    RRAlternationLedger &ledger,RecoveryPrimaryOpenResult &out)
  {
   ZeroMemory(out);
   if(!RecoveryPrimarySession(session,live,scope) || binding.scope!=scope.scope ||
      binding.inventory.executor_epoch!=scope.executor_epoch ||
      binding.inventory.login!=scope.login || binding.inventory.server!=scope.server ||
      binding.inventory.symbol!=scope.symbol || binding.inventory.magic!=scope.magic ||
      binding.inventory.generation<=0 || (ulong)binding.inventory.generation!=scope.generation)
      return false;
   if(range.phase==RR_NEW)
     {
      RecoveryIntentKey primary;
      string ticket;
      if(!RecoveryBuildSessionIntentKey(scope,range.pending,binding.price_tick,
           binding.volume_step,primary) || RecoveryIntentLookup(primary,ticket)!=RI_TICKET ||
           ticket!=(string)binding.exact.order_ticket) return false;
     }
   RRFilled fill;
   if(!RecoveryReadPrimaryFill(range,cfg,binding,fill))return false;
   RRRangeState next=range;
   RROpeningPair pair=opening;
   RRAlternationLedger chain=ledger;
   if(!RecoveryStagePrimaryOpening(next,cfg,binding,fill,pair,chain))return false;
   if(pair.sl_required || pair.sl.kind!=RR_NONE ||
      !RecoveryBuildSessionIntentKey(scope,pair.reverse,binding.price_tick,binding.volume_step,out.reverse_key) ||
      !RecoveryIntentContextOK(out.reverse_key)) return false;
   range=next;opening=pair;ledger=chain;
   out.disposition=RecoveryDispatchOpeningPair(range,opening,out.sl_key,out.reverse_key,
                           binding.price_tick,binding.volume_step,out.receipts);
   return true; // even if unresolved: the staged pair must not be submitted again
  }
#endif
