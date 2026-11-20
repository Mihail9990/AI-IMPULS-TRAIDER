#ifndef RECOVERY_OPENING_DISPATCH_MQH
#define RECOVERY_OPENING_DISPATCH_MQH
#include "../cycle/RecoveryOpeningPair.mqh"
#include "RecoveryDispatch.mqh"

enum RecoveryPairSendState {
   RPS_NOT_STARTED=0,
   RPS_FILL_NOT_BOUND=1,
   RPS_REVERSE_NOT_SENT=2,
   RPS_AWAIT_VERIFIER=3
};
struct RecoveryPairSendResult {
   RecoveryPairSendState state;
   RecoveryDispatchResult sl,reverse;
};

// The independently identified fill, not the current SL, authorizes the
// opposite intent. Live local identity recheck guards stale/replaced positions.
bool RecoveryOpeningFillStillThere(const RRRangeState &range,const RROpeningPair &pair,
                                  const RecoveryIntentKey &key,const double tick,const double step)
  {
   if(!RecoveryIntentContextOK(key))return false;
   long identifier,ticket,side;
   double price,volume;
   string symbol;
   if(!PositionSelectByTicket(pair.position_ticket) ||
      !PositionGetInteger(POSITION_IDENTIFIER,identifier) || (ulong)identifier!=pair.position_id ||
      !PositionGetInteger(POSITION_TICKET,ticket) || (ulong)ticket!=pair.position_ticket ||
      !PositionGetInteger(POSITION_TYPE,side) ||
      side!=(range.cycle.side==1 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL) ||
      !PositionGetString(POSITION_SYMBOL,symbol) || symbol!=key.symbol ||
      !PositionGetDouble(POSITION_PRICE_OPEN,price) ||
      !RecoveryDispatchUnits(price,tick,false,range.cycle.actual_entry_ticks) ||
      !PositionGetDouble(POSITION_VOLUME,volume) ||
      !RecoveryDispatchUnits(volume,step,false,range.cycle.filled_steps))
      return false;
   return RecoveryIntentContextOK(key);
  }

// Fresh opening only. Runtime first retains in this session the pair payload, the two
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
   if(!RecoveryOpeningFillStillThere(range,pair,reverse_key,tick,step))
     {
      out.state=RPS_FILL_NOT_BOUND;
      return out.state;
     }
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
      // A failed or delayed SL remains visible as its own result but is not
      // used as the trigger gate. Never wait for the SL server response here.
      RecoveryDispatchOpeningSLAsync(sl_state,pair.sl,sl_key,tick,step,out.sl);
     }

   // The separately reserved opposite request carries its own SL.
   const RecoveryDispatchState sent=RecoveryDispatchOnce(reverse_state,pair.reverse,reverse_key,
                                                          tick,step,out.reverse);
   out.state=(sent==RD_OUTCOME_UNRESOLVED || sent==RD_TICKET_RECORDED)?
              RPS_AWAIT_VERIFIER:RPS_REVERSE_NOT_SENT;
   return out.state;
  }

// Resume only from the independently proved and still-matching fill.
// Can send only the previously unreserved second intent, never a repeat.
RecoveryPairSendState RecoveryContinueOpeningPair(const RRRangeState &range,
                                                   const RROpeningPair &pair,
                                                   const RecoveryIntentKey &reverse_key,
                                                   const double tick,const double step,
                                                   RecoveryDispatchResult &out)
  {
   ZeroMemory(out);
   RRRangeState shadow;
   if(pair.reverse_verified ||
      !RROpeningRequestState(range,pair,pair.reverse,shadow) ||
      !RecoveryOpeningFillStillThere(range,pair,reverse_key,tick,step))
      return RPS_FILL_NOT_BOUND;
   const RecoveryDispatchState sent=RecoveryDispatchOnce(shadow,pair.reverse,reverse_key,
                                                          tick,step,out);
   return (sent==RD_OUTCOME_UNRESOLVED || sent==RD_TICKET_RECORDED)?
          RPS_AWAIT_VERIFIER:RPS_REVERSE_NOT_SENT;
  }
#endif
