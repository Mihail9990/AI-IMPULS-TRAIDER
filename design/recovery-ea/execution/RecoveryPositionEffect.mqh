#ifndef RECOVERY_POSITION_EFFECT_MQH
#define RECOVERY_POSITION_EFFECT_MQH
#include "../cycle/RecoveryRangeCoordinator.mqh"
#include "RecoveryAdoptionInventory.mqh"

struct RecoveryPositionEffectBinding {
   RecoveryInventoryScope request; // Exact independently issued challenge, including nonce.
   RRAction action;                 // Durable reserved intent; never derived from report.
   ulong verify_after;             // Last known sequence of this verifier epoch, not a clock.
   ulong prior_position_magic;      // From independent pre-action position evidence; zero is valid.
   string prior_position_entry;    // Exact wire value from the original position observation.
   double price_tick;
   double volume_step;
};

bool RecoveryPositionEffectBound(const RRRangeState &range,const RecoveryPositionEffectBinding &binding) {
   const RRAction a=binding.action;
   const bool initial=a.kind==RR_INITIAL_SL && range.phase==RR_PROTECT && a.sl_ticks==range.initial_sl;
   const bool support=a.kind==RR_SUPPORT_SL && range.phase==RR_ACTIVE && a.stage>range.support_stage;
   return (initial || support) && a.serial>0 && a.scope>0 && a.scope==range.scope &&
          a.generation>0 && a.generation==range.cycle.generation &&
          a.generation==(ulong)binding.request.generation && a.range_id==range.cycle.range_id &&
          RRActionEqual(range.pending,a) && RecoveryInvScopeOK(binding.request) &&
          a.position_id>0 && a.position_id==range.cycle.position_id &&
          a.position_ticket>0 && a.position_ticket==range.position_ticket &&
          a.order_ticket>0 && a.order_ticket==range.cycle.active_order &&
          (a.side==1 || a.side==-1) && a.side==range.cycle.side &&
          a.sl_ticks>0 && a.tp_ticks>=0 && a.tp_ticks==range.live_tp &&
          a.entry_ticks==0 && a.volume_steps==0 && range.cycle.filled_steps>0 &&
          RecoveryInvFixed(binding.prior_position_entry,true) &&
          MathIsValidNumber(binding.price_tick) && binding.price_tick>0 &&
          MathIsValidNumber(binding.volume_step) && binding.volume_step>0;
}

void RecoveryPositionEffectClear(RREffect &effect) {
   RRClearAction(effect.action); effect.created_order=0; effect.terminal_verified=false;
}

// Internal fold: only call after parsing the complete inventory with the
// exact expected challenge. No order evidence is consumed by SL confirmation.
bool RecoveryPositionEffectFold(const RecoveryInventorySnapshot &report,const RRRangeState &range,
                                const RecoveryPositionEffectBinding &binding,RREffect &effect) {
   RecoveryPositionEffectClear(effect);
   if(!report.usable || !report.complete || report.reason!="ok" || report.sequence<=0 ||
      (ulong)report.sequence<=binding.verify_after) return false;
   const RRAction a=binding.action;
   bool found=false;
   string f[];
   for(int i=0;i<report.position_count;i++) {
      ulong id,ticket,magic;
      if(StringSplit(report.rows[i],9,f)!=9 || f[0]!="P" ||
         !RecoveryAdoptionUnsigned(f[1],id) || !RecoveryAdoptionUnsigned(f[2],ticket)) return false;
      if(id!=a.position_id && ticket!=a.position_ticket) continue;
      if(found || id!=a.position_id || ticket!=a.position_ticket) return false;
      long volume,sl,tp;
      if(!RecoveryAdoptionUnsigned(f[3],magic) || magic!=binding.prior_position_magic ||
         f[4]!=(a.side==1 ? "BUY" : "SELL") ||
         !RecoveryAdoptionUnits(f[5],binding.volume_step,false,volume) || volume!=range.cycle.filled_steps ||
         f[6]!=binding.prior_position_entry ||
         !RecoveryAdoptionUnits(f[7],binding.price_tick,false,sl) || sl!=a.sl_ticks ||
         !RecoveryAdoptionUnits(f[8],binding.price_tick,true,tp) || tp!=a.tp_ticks) return false;
      found=true;
   }
   if(!found) return false;
   effect.action=a; effect.terminal_verified=true; // created_order stays zero.
   return true;
}

bool RecoveryBuildPositionEffect(const string wire,const RRRangeState &range,
                                 const RecoveryPositionEffectBinding &binding,RREffect &effect) {
   RecoveryPositionEffectClear(effect);
   if(!RecoveryPositionEffectBound(range,binding)) return false;
   RecoveryInventorySnapshot report;
   return RecoveryInvParse(wire,binding.request,report) && RecoveryPositionEffectFold(report,range,binding,effect);
}
// Transport entry point; reuses the complete inventory reader in the EA sandbox.
bool RecoveryReadPositionEffect(const RRRangeState &range,
                                const RecoveryPositionEffectBinding &binding,RREffect &effect) {
   RecoveryPositionEffectClear(effect);
   if(!RecoveryPositionEffectBound(range,binding)) return false;
   RecoveryInventorySnapshot report;
   return RecoveryInvRead(binding.request,report) && RecoveryPositionEffectFold(report,range,binding,effect);
}
#endif
