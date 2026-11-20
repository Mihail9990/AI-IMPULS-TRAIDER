#ifndef RECOVERY_EXACT_ORDER_EFFECT_MQH
#define RECOVERY_EXACT_ORDER_EFFECT_MQH
#include "../cycle/RecoverySupportRotation.mqh"
#include "../verifier/RecoveryExactOrderReader.mqh"
#include "RecoveryAdoptionInventory.mqh" // shared non-lossy fixed-point and ticket decoders

// Restore this from the exact reserved intent, broker receipt, and new
// verification challenge. Do not populate it from the response itself.
struct RecoveryExactEffectBinding {
   RecoveryExactReportScope request;
   RRAction action;
   ulong verify_after;            // sequence from this verifier epoch, not clock
   double price_tick;
   double volume_step;
};

void RecoveryExactEffectClear(RREffect &effect) {
   RRClearAction(effect.action);
   effect.terminal_verified=false;
   effect.created_order=0;
}

bool RecoveryExactEffectBound(const RRRangeState &range,const RecoveryExactEffectBinding &binding) {
   const RRAction a=binding.action;
   const bool create=a.kind==RR_PRIMARY || a.kind==RR_REVERSE || a.kind==RR_REENTRY;
   const bool move=a.kind==RR_MOVE_SUPPORT_OPPOSITE;
   return (create || move || a.kind==RR_CANCEL) && a.serial>0 && a.scope>0 && a.scope==range.scope &&
          a.generation>0 && a.generation==range.cycle.generation &&
          a.generation==(ulong)binding.request.binding.generation &&
          a.range_id==range.cycle.range_id && RRActionEqual(range.pending,a) &&
          RecoveryInvScopeOK(binding.request.binding) && binding.request.order_ticket>0 &&
          (create ? a.order_ticket==0 : a.order_ticket==binding.request.order_ticket) &&
          (!move || (range.retire.ticket==a.order_ticket && range.retire.side==a.side &&
                     range.retire.volume_steps==a.volume_steps &&
                     (range.retire.entry_ticks!=a.entry_ticks || range.retire.sl_ticks!=a.sl_ticks))) &&
          a.position_id==0 && a.position_ticket==0 && (a.side==1 || a.side==-1) &&
          a.entry_ticks>0 && a.sl_ticks>0 && a.tp_ticks>=0 && a.volume_steps>0 &&
          MathIsValidNumber(binding.price_tick) && binding.price_tick>0 &&
          MathIsValidNumber(binding.volume_step) && binding.volume_step>0;
}

// Internal fold of a report already parsed and bound by one of the two input
// functions below. Missing, partial and filled evidence never becomes an effect.
bool RecoveryExactEffectFold(const RecoveryExactReport &report,const RecoveryExactEffectBinding &binding,
                             RREffect &effect) {
   RecoveryExactEffectClear(effect);
   if(!report.usable || !report.observed || report.reason!="ok" || report.sequence<=0 ||
      (ulong)report.sequence<=binding.verify_after || report.position_identifier!="0") return false;
   const RRAction a=binding.action;
   const bool cancel=a.kind==RR_CANCEL;
   const bool create=a.kind==RR_PRIMARY || a.kind==RR_REVERSE || a.kind==RR_REENTRY;
   if(report.source!=(cancel ? "history" : "current")) return false;
   string f[];
   ulong type,state;
   long initial,remaining,entry,sl,tp;
   if(StringSplit(report.row,9,f)!=12 || f[0]!="E" || f[1]!=report.source || f[2]!="0" ||
      f[3]!=(string)binding.request.binding.magic ||
      !RecoveryAdoptionUnsigned(f[4],type) ||
      type!=(ulong)(a.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
      !RecoveryAdoptionUnsigned(f[5],state) ||
      state!=(ulong)(cancel ? ORDER_STATE_CANCELED : ORDER_STATE_PLACED) ||
      !RecoveryAdoptionUnits(f[6],binding.volume_step,false,initial) ||
      !RecoveryAdoptionUnits(f[7],binding.volume_step,false,remaining) || initial!=remaining ||
      initial!=a.volume_steps || !RecoveryAdoptionUnits(f[8],binding.price_tick,false,entry) ||
      entry!=a.entry_ticks || !RecoveryAdoptionUnits(f[9],binding.price_tick,false,sl) || sl!=a.sl_ticks ||
      !RecoveryAdoptionUnits(f[10],binding.price_tick,true,tp) || tp!=a.tp_ticks ||
      f[11]!="0.0000000000000000") return false;
   effect.action=a;
   // A move only confirms an update to the already-owned ticket.
   effect.created_order=create ? binding.request.order_ticket : 0;
   effect.terminal_verified=true;
   return true;
}
bool RecoveryBuildExactOrderEffect(const string wire,const RRRangeState &range,
                                  const RecoveryExactEffectBinding &binding,RREffect &effect) {
   RecoveryExactEffectClear(effect);
   if(!RecoveryExactEffectBound(range,binding)) return false;
   RecoveryExactReport report;
   return RecoveryExactReportParse(wire,binding.request,report) && RecoveryExactEffectFold(report,binding,effect);
}
// Transport entry point; same checks, without exposing raw file records to Python.
bool RecoveryReadExactOrderEffect(const RRRangeState &range,const RecoveryExactEffectBinding &binding,
                                 RREffect &effect) {
   RecoveryExactEffectClear(effect);
   if(!RecoveryExactEffectBound(range,binding)) return false;
   RecoveryExactReport report;
   return RecoveryExactReportRead(binding.request,report) && RecoveryExactEffectFold(report,binding,effect);
}
#endif
