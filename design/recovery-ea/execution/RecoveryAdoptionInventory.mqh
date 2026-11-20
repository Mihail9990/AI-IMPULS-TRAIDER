#ifndef RECOVERY_ADOPTION_INVENTORY_MQH
#define RECOVERY_ADOPTION_INVENTORY_MQH
#include "../verifier/RecoveryInventoryReader.mqh"
#include "RecoveryAdoptionGate.mqh"

struct RecoveryAdoptionBinding {
   ulong intent_scope;           // independently restored local runtime binding
   RecoveryInventoryScope inventory_scope; // stable scope; nonce is not part of identity
   double price_tick;
   double volume_step;
   long position_volume_steps;  // exact original volume captured with adoption intent
   string position_entry_wire;  // exact 16-digit record from the original observation
};

// IDs never pass through a double or signed parse. Reader allows uint64 tickets.
bool RecoveryAdoptionUnsigned(const string token,ulong &value) {
   value=0;
   if(!RecoveryInvNatural(token,"18446744073709551615")) return false;
   for(int i=0;i<StringLen(token);i++)
      value=value*10+(ulong)(StringGetCharacter(token,i)-48);
   return true;
}

// Tolerate bounded binary64 noise from terminal serialization/step arithmetic,
// plus half a unit at 16 decimal places. Never use a relative epsilon in units
// of tick count, a decimal-digit rounding rule, or half-tick acceptance.
bool RecoveryAdoptionUnits(const string token,const double quantum,const bool allow_zero,long &out) {
   out=0;
   if(!RecoveryInvFixed(token,!allow_zero) || !MathIsValidNumber(quantum) || quantum<=0) return false;
   if(token=="0.0000000000000000") return allow_zero;
   const double value=StringToDouble(token);
   const double ratio=value/quantum;
   if(!MathIsValidNumber(value) || !MathIsValidNumber(ratio) ||
      value<=0 || ratio<0.5 || ratio>1000000000000.0) return false;
   const double units=MathRound(ratio), center=units*quantum;
   const double tolerance=2.0*2.2204460492503131e-16*MathMax(value,center)+0.00000000000000005;
   // At poor magnitude/step ratios refuse to guess even if the double happens
   // to equal center. Current wire precision might be insufficient.
   if(!MathIsValidNumber(center) || !MathIsValidNumber(tolerance) ||
      tolerance>quantum*0.000001 || MathAbs(value-center)>tolerance) return false;
   out=(long)units;
   return out>0;
}

bool RecoveryAdoptionBound(const RecoveryAdoptionBinding &binding,const RecoveryInventoryScope &reply,
                          const RecoveryAdoptionIntent &intent) {
   return binding.intent_scope>0 && binding.intent_scope==intent.scope &&
          binding.position_volume_steps>0 &&
          RecoveryInvFixed(binding.position_entry_wire,true) &&
          binding.inventory_scope.executor_epoch==reply.executor_epoch &&
          binding.inventory_scope.verifier_epoch==reply.verifier_epoch &&
          binding.inventory_scope.generation==reply.generation &&
          binding.inventory_scope.login==reply.login &&
          binding.inventory_scope.server==reply.server &&
          binding.inventory_scope.symbol==reply.symbol &&
          binding.inventory_scope.magic==reply.magic &&
          binding.inventory_scope.verifier_chart==reply.verifier_chart &&
          MathIsValidNumber(binding.price_tick) && binding.price_tick>0 &&
          MathIsValidNumber(binding.volume_step) && binding.volume_step>0;
}

RecoveryAdoptionResult RecoveryCheckAdoptionInventory(const string wire,
         const RecoveryInventoryScope &request,const RecoveryAdoptionBinding &binding,
         const RecoveryAdoptionIntent &intent) {
   if(!RecoveryAdoptionBound(binding,request,intent)) return RA_UNRESOLVED;
   RecoveryInventorySnapshot snapshot;
   if(!RecoveryInvParse(wire,request,snapshot) || !snapshot.usable || !snapshot.complete ||
      snapshot.sequence<=0 || (ulong)snapshot.sequence<=intent.verify_after) return RA_UNRESOLVED;

   RecoveryAdoptionProof proof;
   proof.scope=binding.intent_scope; proof.sequence=(ulong)snapshot.sequence; proof.coherent=true;
   proof.position_present=false; proof.position_id=0; proof.position_ticket=0;
   proof.position_side=RA_SIDE_INVALID; proof.position_sl_ticks=0; proof.position_tp_ticks=0;
   proof.order_present=false; proof.order_ticket=0; proof.order_side=RA_SIDE_INVALID;
   proof.order_state=RA_ORDER_UNKNOWN; proof.order_entry_ticks=0; proof.order_sl_ticks=0;
   proof.order_volume_steps=0;

   string fields[];
   for(int i=0;i<snapshot.position_count;i++) {
      ulong position_id,position_ticket;
      if(StringSplit(snapshot.rows[i],9,fields)!=9 ||
         !RecoveryAdoptionUnsigned(fields[1],position_id) ||
         !RecoveryAdoptionUnsigned(fields[2],position_ticket)) return RA_UNRESOLVED;
      if(position_id!=intent.position_id) {
         // Conflicting permanent identifier for a purported exact ticket.
         if(position_ticket==intent.position_ticket) return RA_CONFLICT;
         continue;
      }
      long volume;
      if(!RecoveryAdoptionUnits(fields[5],binding.volume_step,false,volume) ||
         !RecoveryAdoptionUnits(fields[7],binding.price_tick,true,proof.position_sl_ticks) ||
         !RecoveryAdoptionUnits(fields[8],binding.price_tick,true,proof.position_tp_ticks))
         return RA_UNRESOLVED;
      if(volume!=binding.position_volume_steps || fields[6]!=binding.position_entry_wire) return RA_CONFLICT;
      // Manual positions may have any magic value. Their IDs were matched exactly.
      proof.position_present=true; proof.position_id=position_id; proof.position_ticket=position_ticket;
      proof.position_side=(fields[4]=="BUY" ? RA_SIDE_BUY : RA_SIDE_SELL);
   }
   for(int i=snapshot.position_count;i<snapshot.position_count+snapshot.order_count;i++) {
      ulong ticket,magic,position_id,type,state;
      if(StringSplit(snapshot.rows[i],9,fields)!=12 ||
         !RecoveryAdoptionUnsigned(fields[1],ticket)) return RA_UNRESOLVED;
      if(ticket!=intent.opposite_ticket) continue;
      if(!RecoveryAdoptionUnsigned(fields[2],magic) || !RecoveryAdoptionUnsigned(fields[3],position_id) ||
         !RecoveryAdoptionUnsigned(fields[4],type) || !RecoveryAdoptionUnsigned(fields[5],state))
         return RA_UNRESOLVED;
      if(magic!=(ulong)request.magic) return RA_CONFLICT;
      proof.order_present=true; proof.order_ticket=ticket;
      if(state==ORDER_STATE_CANCELED || state==ORDER_STATE_FILLED ||
         state==ORDER_STATE_REJECTED || state==ORDER_STATE_EXPIRED) {
         proof.order_state=RA_ORDER_TERMINAL; continue;
      }
      // Partial fills or requests still changing are not proof of the original
      // intact pair. Reconcile exact execution history; never infer a resend.
      if(state!=ORDER_STATE_PLACED) continue;
      if(type==ORDER_TYPE_BUY_STOP) proof.order_side=RA_SIDE_BUY;
      else if(type==ORDER_TYPE_SELL_STOP) proof.order_side=RA_SIDE_SELL;
      else { proof.order_state=RA_ORDER_OTHER; continue; }
      long initial,tp,stoplimit;
      if(!RecoveryAdoptionUnits(fields[6],binding.volume_step,false,initial) ||
         !RecoveryAdoptionUnits(fields[7],binding.volume_step,false,proof.order_volume_steps) ||
         !RecoveryAdoptionUnits(fields[8],binding.price_tick,false,proof.order_entry_ticks) ||
         !RecoveryAdoptionUnits(fields[9],binding.price_tick,true,proof.order_sl_ticks) ||
         !RecoveryAdoptionUnits(fields[10],binding.price_tick,true,tp) ||
         !RecoveryAdoptionUnits(fields[11],binding.price_tick,true,stoplimit))
         return RA_UNRESOLVED;
      if(position_id!=0 || tp!=0 || stoplimit!=0 || initial!=intent.opposite_volume_steps)
         return RA_CONFLICT;
      if(proof.order_volume_steps!=initial) return RA_UNRESOLVED;
      proof.order_state=RA_ORDER_WORKING_STOP;
   }
   return RecoveryCheckAdoption(intent,proof);
}

#endif
