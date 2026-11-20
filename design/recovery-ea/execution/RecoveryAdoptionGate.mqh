#ifndef RECOVERY_ADOPTION_GATE_MQH
#define RECOVERY_ADOPTION_GATE_MQH

// Pure read-only reducer. A complete observation is assembled from scoped verifier
// data; there are no trade calls or stateful retries here. Numeric prices and
// volume use exact integer ticks/steps from the bound symbol contract.
enum RecoveryAdoptionResult
  {
   RA_UNRESOLVED=0,
   RA_CONFIRMED=1,
   RA_CONFLICT=2
  };
enum RecoveryObservedSide
  {
   RA_SIDE_INVALID=0,
   RA_SIDE_BUY=1,
   RA_SIDE_SELL=2
  };
enum RecoveryObservedOrder
  {
   RA_ORDER_UNKNOWN=0,
   RA_ORDER_WORKING_STOP=1,
   RA_ORDER_TERMINAL=2,
   RA_ORDER_OTHER=3
  };
struct RecoveryAdoptionIntent
  {
   ulong scope;              // runtime binding, validated by caller
   ulong verify_after;       // sequence in that scope, after staging submissions
   ulong position_id;        // permanent MT5 position identifier
   ulong position_ticket;    // exact position ticket used by the intent
   ulong opposite_ticket;    // exact order ticket bound to this intent; 0 if unknown
   RecoveryObservedSide position_side;
   long expected_sl_ticks;
   long original_tp_ticks;   // zero is "no TP"
   long opposite_sl_ticks;  // planned SL of the reverse order
   long opposite_volume_steps;
  };
struct RecoveryAdoptionProof
  {
   ulong scope;
   ulong sequence;
   bool coherent;            // verifier/caller attests that evidence is compatible
   bool position_present;
   ulong position_id;
   ulong position_ticket;
   RecoveryObservedSide position_side;
   long position_sl_ticks;
   long position_tp_ticks;
   bool order_present;
   ulong order_ticket;
   RecoveryObservedSide order_side;
   RecoveryObservedOrder order_state;
   long order_entry_ticks;
   long order_sl_ticks;
   long order_volume_steps;  // current remaining, not initial
  };

RecoveryAdoptionResult RecoveryCheckAdoption(const RecoveryAdoptionIntent &intent,
                                            const RecoveryAdoptionProof &proof)
  {
   if(intent.scope==0 || intent.position_id==0 || intent.position_ticket==0 ||
      intent.expected_sl_ticks<=0 || intent.original_tp_ticks<0 ||
      intent.opposite_sl_ticks<=0 || intent.opposite_volume_steps<=0 ||
      (intent.position_side!=RA_SIDE_BUY && intent.position_side!=RA_SIDE_SELL))
      return RA_CONFLICT;
   if((intent.position_side==RA_SIDE_BUY && intent.opposite_sl_ticks<=intent.expected_sl_ticks) ||
      (intent.position_side==RA_SIDE_SELL && intent.opposite_sl_ticks>=intent.expected_sl_ticks))
      return RA_CONFLICT;
   if(!proof.coherent || proof.scope!=intent.scope ||
      proof.sequence<=intent.verify_after || !proof.position_present)
      return RA_UNRESOLVED;
   // Absence or a foreign ticket is not evidence of failure. Never match by price.
   if(proof.position_id!=intent.position_id)
      return RA_UNRESOLVED;
   if(proof.position_ticket!=intent.position_ticket ||
      proof.position_side!=intent.position_side ||
      proof.position_sl_ticks!=intent.expected_sl_ticks ||
      proof.position_tp_ticks!=intent.original_tp_ticks)
      return RA_CONFLICT;
   if(intent.opposite_ticket==0 || !proof.order_present ||
      proof.order_ticket!=intent.opposite_ticket ||
      proof.order_state==RA_ORDER_UNKNOWN || proof.order_state==RA_ORDER_TERMINAL)
      return RA_UNRESOLVED;
   if(proof.order_state!=RA_ORDER_WORKING_STOP ||
      proof.order_side!=(intent.position_side==RA_SIDE_BUY ? RA_SIDE_SELL : RA_SIDE_BUY) ||
      proof.order_entry_ticks!=intent.expected_sl_ticks ||
      proof.order_sl_ticks!=intent.opposite_sl_ticks ||
      proof.order_volume_steps!=intent.opposite_volume_steps)
      return RA_CONFLICT;
   return RA_CONFIRMED;
  }
#endif
