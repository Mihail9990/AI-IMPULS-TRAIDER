#ifndef RECOVERY_PRIMARY_FILL_PUMP_MQH
#define RECOVERY_PRIMARY_FILL_PUMP_MQH
#include "RecoveryPrimaryFill.mqh"
#include "RecoveryAlternatingPump.mqh"
#include "../verifier/RecoveryExactOrder.mqh"

enum RecoveryPrimaryPumpState {
 RPP_UNUSED=0,RPP_WAIT_TERMINAL,RPP_REQUEST_EXACT,RPP_WAIT_EXACT,
 RPP_REQUEST_HISTORY,RPP_WAIT_HISTORY,RPP_REQUEST_INVENTORY,RPP_WAIT_INVENTORY,
 RPP_DONE,RPP_UNRESOLVED
};
enum RecoveryPrimaryPumpResult {RPP_WAIT=0,RPP_PROGRESS,RPP_PROOF,RPP_STOPPED};
struct RecoveryPrimaryFillPump {
 RecoveryPrimaryPumpState state;
 RecoveryPrimaryFillBinding binding;
 RRKnownOrder wanted;
 int range_id;
 string publisher_tag;
 ulong local_position;
 RecoveryExactReport exact;
 RecoveryHistorySnapshot history;
};

// One RAM object for the exact broker ticket learned by the current primary
// submission or already verified in RR_WAIT. Caller allocates unique nonces.
bool RecoveryPrimaryPumpBegin(RecoveryPrimaryFillPump &p,const RRRangeState &range,
    const RRSettings &cfg,const RecoveryPrimaryFillBinding &binding,const string publisher_tag) {
 if(p.state!=RPP_UNUSED || binding.history.position_identifier!=0 ||
    !RecoveryInvHex(publisher_tag) || !MathIsValidNumber(binding.price_tick) ||
    !MathIsValidNumber(binding.volume_step) || binding.price_tick<=0.0 || binding.volume_step<=0.0)
    return false;
 RecoveryPrimaryFillBinding check=binding;
 check.history.position_identifier=1; // shape validation only; never a wire request
 RRKnownOrder wanted;
 if(!RecoveryPrimaryFillWanted(range,cfg,check,wanted))return false;
 p.binding=binding;p.wanted=wanted;p.range_id=range.cycle.range_id;
 p.publisher_tag=publisher_tag;p.local_position=0;
 RecoveryExactReportClear(p.exact);RecoveryHistoryClear(p.history);
 p.state=RPP_WAIT_TERMINAL;
 return true;
}

bool RecoveryPrimaryPumpCurrent(const RecoveryPrimaryFillPump &p,const RRRangeState &range,
                                const RRSettings &cfg) {
 if(p.range_id!=range.cycle.range_id || !RecoveryAlternatingPumpSession(p.binding.inventory))
    return false;
 RecoveryPrimaryFillBinding check=p.binding;
 if(check.history.position_identifier==0)check.history.position_identifier=1;
 RRKnownOrder current;
 return RecoveryPrimaryFillWanted(range,cfg,check,current) && RRKnownEqual(current,p.wanted);
}

// Does not accept a fill. A stable exact terminal observation only schedules
// the independent verifier. No polling by price, no cancellation by the pump.
bool RecoveryPrimaryPumpObserveLocal(const RecoveryPrimaryFillPump &p,ulong &candidate) {
 candidate=0;
 RecoveryExactOrderScope request;
 const RecoveryInventoryScope scope=p.binding.exact.binding;
 request.login=scope.login;request.server=scope.server;request.symbol=scope.symbol;
 request.magic=(ulong)scope.magic;request.ticket=p.wanted.ticket;
 RecoveryExactOrderObservation e;
 if(RecoveryObserveExactOrder(request,e)!=RECOVERY_EXACT_ORDER_OBSERVED ||
    e.source!=RECOVERY_EXACT_ORDER_HISTORY || e.ticket!=request.ticket ||
    e.symbol!=request.symbol || e.magic!=request.magic || e.position_identifier==0 ||
    e.stop_limit!=0.0 || !RecoveryAlternatingPumpSession(scope))return false;
 ulong terminal_state;
 long remainder;
 if(!RecoveryPrimaryFillTerms((string)e.type,(string)e.state,DoubleToString(e.initial_volume,16),
    DoubleToString(e.remaining_volume,16),DoubleToString(e.price,16),DoubleToString(e.sl,16),
    DoubleToString(e.tp,16),p.wanted,p.binding,terminal_state,remainder))return false;
 candidate=e.position_identifier;
 return true;
}

bool RecoveryPrimaryPumpExact(const RecoveryPrimaryFillPump &p,const RecoveryExactReport &e,
                              ulong &identifier) {
 identifier=0;
 if(!e.usable || !e.observed || e.reason!="ok" || e.source!="history" || e.sequence<=0 ||
    (ulong)e.sequence<=p.binding.verify_after ||
    !RecoveryAdoptionUnsigned(e.position_identifier,identifier) || identifier==0 ||
    identifier!=p.local_position) return false;
 string parts[];
 ulong terminal_state;
 long remainder,stop_limit;
 return StringSplit(e.row,9,parts)==12 && parts[0]=="E" && parts[1]=="history" &&
    parts[2]==e.position_identifier && parts[3]==(string)p.binding.exact.binding.magic &&
    RecoveryPrimaryFillTerms(parts[4],parts[5],parts[6],parts[7],parts[8],parts[9],parts[10],
                             p.wanted,p.binding,terminal_state,remainder) &&
    RecoveryAdoptionUnits(parts[11],p.binding.price_tick,true,stop_limit) && stop_limit==0;
}

RecoveryPrimaryPumpResult RecoveryPrimaryPumpStop(RecoveryPrimaryFillPump &p) {
 p.state=RPP_UNRESOLVED;
 return RPP_STOPPED;
}

// Process one stage per timer. Publisher state is consumed before each local
// request; failures never cause an automatic re-publication or broker retry.
RecoveryPrimaryPumpResult RecoveryPrimaryPumpStep(RecoveryPrimaryFillPump &p,const RRRangeState &range,
                                                  const RRSettings &cfg,RRFilled &out) {
 ZeroMemory(out);
 if(p.state==RPP_UNUSED || p.state==RPP_DONE || p.state==RPP_UNRESOLVED)return RPP_STOPPED;
 if(!RecoveryPrimaryPumpCurrent(p,range,cfg))return RecoveryPrimaryPumpStop(p);

 switch(p.state) {
   case RPP_WAIT_TERMINAL: {
      ulong candidate;
      if(!RecoveryPrimaryPumpObserveLocal(p,candidate))return RPP_WAIT;
      p.local_position=candidate;p.state=RPP_REQUEST_EXACT;
      return RPP_PROGRESS;
   }
   case RPP_REQUEST_EXACT:
      p.state=RPP_WAIT_EXACT;
      if(!RecoveryExactReportRequest(p.binding.exact,p.publisher_tag))return RecoveryPrimaryPumpStop(p);
      return RPP_PROGRESS;
   case RPP_WAIT_EXACT: {
      if(!RecoveryExactReportRead(p.binding.exact,p.exact))return RPP_WAIT;
      ulong identifier;
      if(!RecoveryPrimaryPumpExact(p,p.exact,identifier))return RecoveryPrimaryPumpStop(p);
      p.binding.history.position_identifier=identifier;
      p.state=RPP_REQUEST_HISTORY;
      return RPP_PROGRESS;
   }
   case RPP_REQUEST_HISTORY:
      p.state=RPP_WAIT_HISTORY;
      if(!RecoveryHistoryRequest(p.binding.history,p.publisher_tag))return RecoveryPrimaryPumpStop(p);
      return RPP_PROGRESS;
   case RPP_WAIT_HISTORY:
      if(!RecoveryHistoryRead(p.binding.history,p.history))return RPP_WAIT;
      if(!p.history.usable || !p.history.observed || p.history.reason!="ok" ||
         p.history.sequence<=p.exact.sequence || p.history.order_count!=1 ||
         p.history.deal_count<1 || p.history.deal_count>128)
         return RecoveryPrimaryPumpStop(p);
      p.state=RPP_REQUEST_INVENTORY;
      return RPP_PROGRESS;
   case RPP_REQUEST_INVENTORY: {
      const RecoveryInventoryScope i=p.binding.inventory;
      p.state=RPP_WAIT_INVENTORY;
      if(!RecoveryRequestInventory(i.nonce,i.executor_epoch,i.verifier_epoch,i.generation,
                                   i.login,i.server,i.symbol,i.magic))return RecoveryPrimaryPumpStop(p);
      return RPP_PROGRESS;
   }
   case RPP_WAIT_INVENTORY: {
      RecoveryInventorySnapshot inventory;
      if(!RecoveryInvRead(p.binding.inventory,inventory))return RPP_WAIT;
      if(!RecoveryPrimaryPumpCurrent(p,range,cfg) ||
         !RecoveryPrimaryFillFold(range,cfg,p.binding,p.exact,p.history,inventory,out))
         return RecoveryPrimaryPumpStop(p);
      p.state=RPP_DONE;
      return RPP_PROOF;
   }
   default:
      return RecoveryPrimaryPumpStop(p);
 }
}
#endif
