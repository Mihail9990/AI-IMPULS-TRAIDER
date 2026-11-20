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

// Both input paths require the exact current session, and a prior journal
// receipt for the immutable primary action while it is still staged.
bool RecoveryPrimaryOpenBound(
    const RecoveryPrimaryFillBinding &binding,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RRRangeState &range)
  {
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
   return true;
}

// Input MUST be the normalized result of this binding's independent
// exact/history/inventory reader or its pump, never an ACK or local hint.
bool RecoveryOpenVerifiedPrimaryProof(
    const RecoveryPrimaryFillBinding &binding,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,const RRFilled &fill,
    RRRangeState &range,const RRSettings &cfg,RROpeningPair &opening,
    RRAlternationLedger &ledger,RecoveryPrimaryOpenResult &out)
  {
   ZeroMemory(out);
   if(!RecoveryPrimaryOpenBound(binding,session,live,scope,range))return false;
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
// Compatibility path when a caller already has all verified messages on
// disk. The primary pump should use the proof-fed path, avoiding a second read.
bool RecoveryOpenVerifiedPrimary(
    const RecoveryPrimaryFillBinding &binding,const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    RRRangeState &range,const RRSettings &cfg,RROpeningPair &opening,
    RRAlternationLedger &ledger,RecoveryPrimaryOpenResult &out)
  {
   ZeroMemory(out);
   if(!RecoveryPrimaryOpenBound(binding,session,live,scope,range))return false;
   RRFilled fill;
   if(!RecoveryReadPrimaryFill(range,cfg,binding,fill))return false;
   return RecoveryOpenVerifiedPrimaryProof(binding,session,live,scope,fill,range,cfg,opening,ledger,out);
  }
#endif
