#ifndef RECOVERY_SUPPORT_ROTATION_DISPATCH_MQH
#define RECOVERY_SUPPORT_ROTATION_DISPATCH_MQH
#include "../cycle/RecoverySupportRotation.mqh"
#include "RecoveryOpeningDispatch.mqh"

struct RecoveryRotationDispatchResult
  {
   RecoveryDispatchResult sl,move;
  };

// The caller persists the exact winning fill, rotation plan and separately
// typed intent IDs before calling. This never replaces the remaining order.
bool RecoveryDispatchSupportRotation(const RRRangeState &range,const RRSupportRotation &rotation,
                                     const RecoveryIntentKey &sl_key,const RecoveryIntentKey &move_key,
                                     const double tick,const double step,RecoveryRotationDispatchResult &out)
  {
   ZeroMemory(out);
   RRRangeState sl_state,move_state;
   const bool need_sl=rotation.sl_required && !rotation.sl_verified;
   const bool need_move=rotation.move_required && !rotation.move_verified;
   if(!need_sl && !need_move) return false;
   if(need_sl && (!RecoveryIntentContextOK(sl_key) ||
                  !RRSupportRotationRequestState(range,rotation,rotation.sl,sl_state))) return false;
   if(need_move && (!RecoveryIntentContextOK(move_key) ||
                    !RRSupportRotationRequestState(range,rotation,rotation.move,move_state))) return false;
   if(need_move && need_sl && (move_key.id==sl_key.id || sl_key.login!=move_key.login ||
      sl_key.server!=move_key.server || sl_key.symbol!=move_key.symbol ||
      sl_key.magic!=move_key.magic || sl_key.generation!=move_key.generation))
      return false;
   RROpeningPair identity;
   identity.position_id=rotation.position_id;identity.position_ticket=rotation.position_ticket;
   if(need_move)
     {
      if(!RecoveryOpeningFillStillThere(move_state,identity,move_key,tick,step))return false;
     }
   else if(!RecoveryOpeningFillStillThere(sl_state,identity,sl_key,tick,step))return false;
   if(need_sl)
      RecoveryDispatchOpeningSLAsync(sl_state,rotation.sl,sl_key,tick,step,out.sl);
   if(need_move)
      RecoveryDispatchOnce(move_state,rotation.move,move_key,tick,step,out.move);
   return true; // A step was considered, not an assertion of either effect.
  }
#endif
