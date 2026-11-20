#ifndef RECOVERY_CLOSE_WIRE_MQH
#define RECOVERY_CLOSE_WIRE_MQH
#include "RecoveryCloseEvidence.mqh"
#include "../verifier/RecoveryHistoryReader.mqh"
#include "../execution/RecoveryAdoptionInventory.mqh"

struct RCCloseWireBinding {
   RCCloseEvidenceKey key;
   RecoveryHistoryScope history;
   RecoveryInventoryScope inventory;
   long history_after, inventory_after; // verifier sequence checkpoints, not clocks
   double tick, step;
};

bool RCCloseWireScope(const RCCloseWireBinding &b) {
   return RCCloseKeysEqual(b.key,b.key) && (b.key.range_id==1 || b.key.range_id==2) &&
          RecoveryHistoryScopeOK(b.history) && RecoveryInvScopeOK(b.inventory) &&
          b.history.position_identifier==b.key.position_id && b.history.anchor_order==b.key.anchor_order &&
          b.history.binding.nonce!=b.inventory.nonce &&
          b.history.binding.executor_epoch==b.inventory.executor_epoch &&
          b.history.binding.verifier_epoch==b.inventory.verifier_epoch &&
          b.history.binding.generation==b.inventory.generation &&
          (ulong)b.inventory.generation==b.key.generation &&
          b.history.binding.login==b.inventory.login && b.history.binding.server==b.inventory.server &&
          b.history.binding.symbol==b.inventory.symbol && b.history.binding.magic==b.inventory.magic &&
          b.history.binding.verifier_chart==b.inventory.verifier_chart &&
          b.history_after>=0 && b.inventory_after>=0 &&
          MathIsValidNumber(b.tick) && b.tick>0 && MathIsValidNumber(b.step) && b.step>0;
}

int RCExitReason(const ulong reason) {
   if(reason==DEAL_REASON_SL)return RC_SL;
   if(reason==DEAL_REASON_TP)return RC_TP;
   if(reason==DEAL_REASON_CLIENT || reason==DEAL_REASON_MOBILE || reason==DEAL_REASON_WEB)
      return RC_MANUAL;
   return 0; // Expert/stop-out/etc. must not masquerade as manual.
}

bool RCAnchorType(const ulong type,const int side) {
   if(side==1)
      return type==ORDER_TYPE_BUY || type==ORDER_TYPE_BUY_LIMIT ||
             type==ORDER_TYPE_BUY_STOP || type==ORDER_TYPE_BUY_STOP_LIMIT;
   if(side==-1)
      return type==ORDER_TYPE_SELL || type==ORDER_TYPE_SELL_LIMIT ||
             type==ORDER_TYPE_SELL_STOP || type==ORDER_TYPE_SELL_STOP_LIMIT;
   return false;
}

// The typed snapshots must have been read for the exact challenges in b.
RCEvidenceResult RCBuildCloseFromReports(const RCRange &r,const RCCloseWireBinding &b,
                                      const RecoveryHistorySnapshot &hist,const RecoveryInventorySnapshot &inv,
                                      const RCProtectionFact &protection[],RCCloseProof &proof) {
   RCClearCloseProof(proof);
   if(!RCCloseWireScope(b) || !RCBound(r,b.key.generation,b.key.range_id) ||
      !RCValidSide(r.side) || !RCBoundedPositive(r.filled_steps) ||
      !hist.usable || !hist.observed || hist.sequence<=b.history_after ||
      !inv.usable || !inv.complete || inv.sequence<=b.inventory_after ||
      hist.order_count<0 || hist.order_count>128 ||
      hist.deal_count<0 || hist.deal_count>128 ||
      inv.position_count<0 || inv.position_count>128 || inv.order_count<0 || inv.order_count>128 ||
      ArraySize(hist.rows)!=hist.order_count+hist.deal_count ||
      ArraySize(inv.rows)!=inv.position_count+inv.order_count) return RCE_UNRESOLVED;
   RCExitHistory h;
   h.key=b.key; h.complete=true; h.anchor_final=false; h.anchor_matches_position=false; h.observed_entry_steps=0;
   RCCloseInventory i;
   i.key=b.key; i.complete=true; i.present=false; i.observed_position_id=0; i.remaining_steps=0;
   string fields[];
   for(int n=0;n<inv.position_count;n++) {
      ulong id;
      if(StringSplit(inv.rows[n],9,fields)!=9 || !RecoveryAdoptionUnsigned(fields[1],id))
         return RCE_UNRESOLVED;
      if(id!=b.key.position_id)continue;
      if((fields[4]=="BUY"?1:-1)!=r.side ||
         !RecoveryAdoptionUnits(fields[5],b.step,false,i.remaining_steps))return RCE_UNRESOLVED;
      i.present=true;i.observed_position_id=id;
   }
   long anchor_initial=0,anchor_remainder=0;
   for(int n=0;n<hist.order_count;n++) {
      ulong order,type,state;
      if(StringSplit(hist.rows[n],9,fields)!=11 || !RecoveryAdoptionUnsigned(fields[1],order))
         return RCE_UNRESOLVED;
      if(order!=b.key.anchor_order)continue;
      if(!RecoveryAdoptionUnsigned(fields[3],type) || !RCAnchorType(type,r.side) ||
         !RecoveryAdoptionUnsigned(fields[4],state) ||
         !RecoveryAdoptionUnits(fields[6],b.step,false,anchor_initial) ||
         !RecoveryAdoptionUnits(fields[7],b.step,true,anchor_remainder) ||
         anchor_remainder>anchor_initial) return RCE_UNRESOLVED;
      h.anchor_final=(state==ORDER_STATE_FILLED && anchor_remainder==0) ||
                     ((state==ORDER_STATE_CANCELED || state==ORDER_STATE_EXPIRED) && anchor_remainder>0);
   }
   if(!h.anchor_final)return RCE_UNRESOLVED;

   RCExitDeal exits[];
   for(int n=hist.order_count;n<hist.order_count+hist.deal_count;n++) {
      ulong ticket,order,entry,reason;
      long steps;
      if(StringSplit(hist.rows[n],9,fields)!=12 || !RecoveryAdoptionUnsigned(fields[1],ticket) ||
         !RecoveryAdoptionUnsigned(fields[2],order) || !RecoveryAdoptionUnsigned(fields[5],entry) ||
         !RecoveryAdoptionUnsigned(fields[6],reason) ||
         !RecoveryAdoptionUnits(fields[8],b.step,false,steps))return RCE_UNRESOLVED;
      const int side=(fields[4]=="BUY"?1:-1);
      if(entry==DEAL_ENTRY_IN) {
         if(order!=b.key.anchor_order || side!=r.side ||
            steps>r.filled_steps-h.observed_entry_steps)return RCE_UNRESOLVED;
         h.anchor_matches_position=true; h.observed_entry_steps+=steps;
      } else if(entry==DEAL_ENTRY_OUT) {
         long price,sl;
         if(!RecoveryAdoptionUnits(fields[9],b.tick,false,price) ||
            !RecoveryAdoptionUnits(fields[10],b.tick,true,sl))return RCE_UNRESOLVED;
         const int at=ArraySize(exits);
         if(ArrayResize(exits,at+1)!=at+1)return RCE_UNRESOLVED;
         exits[at].ticket=ticket; exits[at].position_id=b.key.position_id;
         exits[at].closing_order=order; exits[at].exit_side=side; exits[at].reason=RCExitReason(reason);
         exits[at].volume_steps=steps; exits[at].execution_ticks=price; exits[at].deal_sl_ticks=sl;
      } else return RCE_UNRESOLVED; // no close-by, netting reversal or unsupported entry semantics
   }
   if(h.observed_entry_steps!=anchor_initial-anchor_remainder)return RCE_UNRESOLVED;
   return RCBuildCloseProof(r,b.key,h,i,exits,protection,proof);
}

RCEvidenceResult RCBuildCloseFromWire(const RCRange &r,const RCCloseWireBinding &b,
                                      const string history_wire,const string inventory_wire,
                                      const RCProtectionFact &protection[],RCCloseProof &proof) {
   RCClearCloseProof(proof);
   RecoveryHistorySnapshot hist;
   RecoveryInventorySnapshot inv;
   if(!RCCloseWireScope(b) || !RecoveryHistoryParse(history_wire,b.history,hist) ||
      !RecoveryInvParse(inventory_wire,b.inventory,inv))return RCE_UNRESOLVED;
   return RCBuildCloseFromReports(r,b,hist,inv,protection,proof);
}

// Mutates only supplied in-memory range once classified. Trading EA must
// atomically journal transition and intents separately before actions are sent.
RCEvidenceResult RCApplyCloseFromWire(RCRange &r,const RCCloseWireBinding &binding,
                                      const string history_wire,const string inventory_wire,
                                      const RCProtectionFact &protection[],const long reentry_distance,
                                      RCClosePlan &plan) {
   plan.classification=RC_UNRESOLVED;plan.current_slot=0;plan.reentry_slot=0;
   plan.reentry_side=0;plan.reentry_ticks=0;plan.keep_existing_opposite=false;
   plan.cancel_remaining_range_orders=false;
   RCCloseProof proof;
   RCEvidenceResult status=RCBuildCloseFromWire(r,binding,history_wire,inventory_wire,protection,proof);
   if(status!=RCE_FULLY_CLASSIFIED)return status;
   if(!RCApplyClose(r,proof,reentry_distance,plan))return RCE_UNRESOLVED;
   return RCE_FULLY_CLASSIFIED;
}
#endif
