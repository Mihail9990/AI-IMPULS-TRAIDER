#ifndef RECOVERY_OPENING_DISPATCH_MQH
#define RECOVERY_OPENING_DISPATCH_MQH
#include "../cycle/RecoveryOpeningPair.mqh"
#include "RecoveryDispatch.mqh"

enum RecoveryPairSendState {
   RPS_NOT_STARTED=0,
   RPS_WAIT_SL_PROOF=1,
   RPS_REVERSE_NOT_SENT=2,
   RPS_AWAIT_VERIFIER=3
};
struct RecoveryPairSendResult {
   RecoveryPairSendState state;
   RecoveryDispatchResult sl,reverse;
};

// Recheck the already-observed SL on the exact same position; no new broker
// operation or independent wait is introduced by this direct local read.
bool RecoveryOpeningSLStillThere(const RRRangeState &range,const RROpeningPair &pair,
                                const RecoveryIntentKey &key,const double tick,const double step)
  {
   RRRangeState observed=range;
   observed.live_sl=pair.reverse.entry_ticks;
   RRAction check=pair.reverse;
   check.kind=RR_INITIAL_SL;check.side=range.cycle.side;
   check.position_id=pair.position_id;check.position_ticket=pair.position_ticket;
   check.tp_ticks=range.live_tp;
   return RecoveryDispatchTarget(key,observed,check,tick,step);
  }

// Fresh opening only. Runtime first durably records the pair payload, the two
// independent immutable IDs, and evidence for any already-attached SL. This
// helper neither changes the pair state nor treats broker ACK as verifier proof.
RecoveryPairSendState RecoveryDispatchOpeningPair(const RRRangeState &range,
                                                   const RROpeningPair &pair,
                                                   const RecoveryIntentKey &sl_key,
                                                   const RecoveryIntentKey &reverse_key,
                                                   const double tick,const double step,
                                                   RecoveryPairSendResult &out)
  {
   ZeroMemory(out);
   out.state=RPS_NOT_STARTED;
   RRRangeState reverse_state;
   if(pair.reverse.kind!=RR_REVERSE ||
      !RROpeningRequestState(range,pair,pair.reverse,reverse_state) ||
      !RecoveryIntentContextOK(reverse_key) ||
      (ulong)reverse_key.generation!=pair.reverse.generation)
      return out.state;
   string stored;
   if(RecoveryIntentLookup(reverse_key,stored)!=RI_NO_RECORD)
      return out.state;
   if(pair.sl.kind!=RR_NONE)
     {
      RRRangeState sl_state;
      if(pair.sl.kind!=RR_INITIAL_SL || pair.sl.sl_ticks!=pair.reverse.entry_ticks ||
         !RROpeningRequestState(range,pair,pair.sl,sl_state) ||
         !RecoveryIntentContextOK(sl_key) || sl_key.id==reverse_key.id ||
         sl_key.login!=reverse_key.login || sl_key.server!=reverse_key.server ||
         sl_key.symbol!=reverse_key.symbol || sl_key.magic!=reverse_key.magic ||
         sl_key.generation!=reverse_key.generation)
         return out.state;
      RecoveryDispatchOnce(sl_state,pair.sl,sl_key,tick,step,out.sl);
      if(!out.sl.synchronous_sl_ack)
        {
         out.state=RPS_WAIT_SL_PROOF;
         return out.state;
        }
     }
   else if(pair.reverse.entry_ticks!=range.live_sl || range.live_sl<=0 ||
           !RecoveryOpeningSLStillThere(range,pair,reverse_key,tick,step))
      return out.state; // attached stop: known from scoped fill evidence

   // No independent verifier exchange between the successful SL response
   // and opposite request. Both outcomes still require verifier evidence.
   const RecoveryDispatchState sent=RecoveryDispatchOnce(reverse_state,pair.reverse,reverse_key,
                                                          tick,step,out.reverse);
   out.state=(sent==RD_OUTCOME_UNRESOLVED || sent==RD_TICKET_RECORDED)?
              RPS_AWAIT_VERIFIER:RPS_REVERSE_NOT_SENT;
   return out.state;
  }

// Recovered SL proof was independently verified and durably restored. This
// can send only the previously unreserved second intent, never a repeat.
RecoveryPairSendState RecoveryContinueOpeningPair(const RRRangeState &range,
                                                   const RROpeningPair &pair,
                                                   const RecoveryIntentKey &reverse_key,
                                                   const double tick,const double step,
                                                   RecoveryDispatchResult &out)
  {
   ZeroMemory(out);
   RRRangeState shadow;
   if(!pair.sl_verified || pair.reverse_verified ||
      !RROpeningRequestState(range,pair,pair.reverse,shadow) ||
      !RecoveryOpeningSLStillThere(range,pair,reverse_key,tick,step))
      return RPS_WAIT_SL_PROOF;
   const RecoveryDispatchState sent=RecoveryDispatchOnce(shadow,pair.reverse,reverse_key,
                                                          tick,step,out);
   return (sent==RD_OUTCOME_UNRESOLVED || sent==RD_TICKET_RECORDED)?
          RPS_AWAIT_VERIFIER:RPS_REVERSE_NOT_SENT;
  }
#endif
