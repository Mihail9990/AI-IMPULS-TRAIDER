#ifndef RECOVERY_CLOSE_EVIDENCE_MQH
#define RECOVERY_CLOSE_EVIDENCE_MQH
#include "RecoveryCycle.mqh"

// Exact normalized evidence only; no live reads, trade APIs or clock gating.
// Scope/observation bind evidence that consumers have already validated. They
// are durable local correlation IDs, not adequate proof on their own.
struct RCCloseEvidenceKey {
   ulong scope, observation, generation;
   int range_id;
   ulong position_id, anchor_order;
};

struct RCExitHistory {
   RCCloseEvidenceKey key;
   bool complete, anchor_final, anchor_matches_position;
   long observed_entry_steps;
};

struct RCCloseInventory {
   RCCloseEvidenceKey key;
   bool complete, present;
   ulong observed_position_id;
   long remaining_steps;
};

struct RCExitDeal {
   ulong ticket, position_id, closing_order;
   int exit_side, reason;
   long volume_steps, execution_ticks, deal_sl_ticks;
   // If normalized reason says manual, consumer has proved a client/mobile/web
   // manual exit, or exactly matched an independently confirmed own close.
};

struct RCProtectionFact {
   ulong scope, generation, position_id, anchor_order;
   int range_id, stop_class;
   long stop_ticks;
   bool confirmed;
};

enum RCEvidenceResult {
   RCE_UNRESOLVED=0, RCE_OPEN_OR_PARTIAL=1, RCE_FULLY_CLASSIFIED=2
};

bool RCCloseKeysEqual(const RCCloseEvidenceKey &a,const RCCloseEvidenceKey &b) {
   return a.scope>0 && a.scope==b.scope && a.observation>0 && a.observation==b.observation &&
          a.generation>0 && a.generation==b.generation && a.range_id==b.range_id &&
          a.position_id>0 && a.position_id==b.position_id &&
          a.anchor_order>0 && a.anchor_order==b.anchor_order;
}

bool RCExitSame(const RCExitDeal &a,const RCExitDeal &b) {
   return a.ticket==b.ticket && a.position_id==b.position_id &&
          a.closing_order==b.closing_order && a.exit_side==b.exit_side &&
          a.reason==b.reason && a.volume_steps==b.volume_steps &&
          a.execution_ticks==b.execution_ticks && a.deal_sl_ticks==b.deal_sl_ticks;
}

int RCProtectionMask(const RCCloseEvidenceKey &key,const long actual_deal_sl,
                     const RCProtectionFact &protection[]) {
   int mask=0;
   for(int i=0;i<ArraySize(protection);i++) {
      if(!protection[i].confirmed || protection[i].scope!=key.scope ||
         protection[i].generation!=key.generation || protection[i].range_id!=key.range_id ||
         protection[i].position_id!=key.position_id || protection[i].anchor_order!=key.anchor_order ||
         protection[i].stop_ticks!=actual_deal_sl) continue;
      if(protection[i].stop_class!=RC_INITIAL_LEVEL &&
         protection[i].stop_class!=RC_SUPPORT_LEVEL) return 0;
      mask|=protection[i].stop_class;
   }
   return mask;
}

void RCClearCloseProof(RCCloseProof &out) {
   out.generation=0;out.range_id=0;out.order=0;out.position_id=0;out.filled_steps=0;
   out.exited_steps=0;out.remaining_steps=0;out.independently_verified=false;
   out.history_complete=false;out.reason=0;out.exit_stop_ticks=0;
   out.confirmed_stop_mask=0;out.uniform_exit_reason_and_stop=false;
}

RCEvidenceResult RCBuildCloseProof(const RCRange &range,const RCCloseEvidenceKey &expected,
                                  const RCExitHistory &history,const RCCloseInventory &inventory,
                                  const RCExitDeal &deals[],const RCProtectionFact &protection[],
                                  RCCloseProof &out) {
   RCClearCloseProof(out);
   const int count=ArraySize(deals);
   if(count>128 || ArraySize(protection)>128 || !RCBound(range,expected.generation,expected.range_id) ||
      !RCValidSide(range.side) || !RCBoundedPositive(range.filled_steps) ||
      expected.position_id!=range.position_id || expected.anchor_order!=range.active_order ||
      !RCCloseKeysEqual(expected,history.key) || !RCCloseKeysEqual(expected,inventory.key) ||
      !history.complete || !history.anchor_final || !history.anchor_matches_position ||
      history.observed_entry_steps!=range.filled_steps || !inventory.complete) return RCE_UNRESOLVED;
   if(inventory.present) {
      if(inventory.observed_position_id!=expected.position_id ||
         !RCBoundedPositive(inventory.remaining_steps)) return RCE_UNRESOLVED;
   } else if(inventory.observed_position_id!=0 || inventory.remaining_steps!=0)
      return RCE_UNRESOLVED;

   long total=0,stop=0;
   int reason=0;
   bool uniform=true;
   for(int i=0;i<count;i++) {
      if(deals[i].ticket==0 || deals[i].position_id!=expected.position_id ||
         deals[i].closing_order==0 || deals[i].closing_order==expected.anchor_order ||
         deals[i].exit_side!=-range.side || !RCBoundedPositive(deals[i].volume_steps) ||
         !RCBoundedPositive(deals[i].execution_ticks) || deals[i].deal_sl_ticks<0 ||
         deals[i].deal_sl_ticks>RC_MAX_UNITS) return RCE_UNRESOLVED;
      bool duplicate=false;
      for(int j=0;j<i;j++) {
         if(deals[i].ticket!=deals[j].ticket) continue;
         if(!RCExitSame(deals[i],deals[j])) return RCE_UNRESOLVED;
         duplicate=true;break;
      }
      if(duplicate) continue;
      // A SL on a manual/TP deal may be a field inherited from history, not
      // its closure cause. Only a true SL reason authorizes use of DEAL_SL.
      const long level=(deals[i].reason==RC_SL ? deals[i].deal_sl_ticks : 0);
      if(i==0) { reason=deals[i].reason;stop=level; }
      else if(reason!=deals[i].reason || stop!=level) uniform=false;
      if(deals[i].volume_steps>range.filled_steps-total) return RCE_UNRESOLVED;
      total+=deals[i].volume_steps;
   }
   // Inventory absence and exited volume prove different things. A complete
   // history with some exits is not "closed" without complete inventory and
   // zero remaining; absent inventory without complete exit volume is unknown.
   if(!inventory.present && total!=range.filled_steps) return RCE_UNRESOLVED;
   if(inventory.present) {
      if(total>=range.filled_steps || inventory.remaining_steps!=range.filled_steps-total)
         return RCE_UNRESOLVED;
      return RCE_OPEN_OR_PARTIAL;
   }
   if(!uniform || (reason!=RC_SL && reason!=RC_MANUAL && reason!=RC_TP)) return RCE_UNRESOLVED;
   int stop_mask=0;
   if(reason==RC_SL) {
      if(!RCBoundedPositive(stop)) return RCE_UNRESOLVED;
      stop_mask=RCProtectionMask(expected,stop,protection);
      if(stop_mask!=RC_INITIAL_LEVEL && stop_mask!=RC_SUPPORT_LEVEL)
         return RCE_UNRESOLVED;
   }
   out.generation=expected.generation;out.range_id=expected.range_id;out.order=expected.anchor_order;
   out.position_id=expected.position_id;out.filled_steps=range.filled_steps;
   out.exited_steps=total;out.remaining_steps=0;out.independently_verified=true;
   out.history_complete=true;out.reason=reason;out.exit_stop_ticks=stop;
   out.confirmed_stop_mask=stop_mask;out.uniform_exit_reason_and_stop=true;
   return RCE_FULLY_CLASSIFIED;
}
#endif
