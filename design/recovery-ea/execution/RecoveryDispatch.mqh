#ifndef RECOVERY_DISPATCH_MQH
#define RECOVERY_DISPATCH_MQH
#include "RecoveryActionRequest.mqh"
#include "RecoveryIntentJournal.mqh"
#include "RecoveryAdoptionInventory.mqh"
#include "../verifier/RecoveryExactOrder.mqh"

// Control layer must persist the full immutable action and enforce a single
// writer before calling. This is not an auto-start EA. No automatic retries.
enum RecoveryDispatchState
  {
   RD_NOT_SENT=0,
   RD_EXISTING_INTENT=1,
   RD_RESERVED_NOT_SENT=2,
   RD_OUTCOME_UNRESOLVED=3,
   RD_TICKET_RECORDED=4
  };
struct RecoveryDispatchResult
  {
   RecoveryDispatchState state;
   uint check_retcode;
   uint send_retcode;
   ulong order;
  };

bool RecoveryDispatchUnits(const double value,const double unit,const bool allow_zero,
                           const long expected)
  {
   long actual;
   return MathIsValidNumber(value) &&
          RecoveryAdoptionUnits(DoubleToString(value,16),unit,allow_zero,actual) &&
          actual==expected;
  }

bool RecoveryDispatchTarget(const RecoveryIntentKey &key,const RRRangeState &range,const RRAction &action,
                            const double tick,const double step)
  {
   if(!RecoveryIntentContextOK(key)) return false;
   if(action.kind==RR_INITIAL_SL || action.kind==RR_SUPPORT_SL)
     {
      long identifier,ticket,side;
      string symbol;
      double sl,tp;
      if(!PositionSelectByTicket(action.position_ticket) ||
         !PositionGetInteger(POSITION_IDENTIFIER,identifier) || (ulong)identifier!=action.position_id ||
         !PositionGetInteger(POSITION_TICKET,ticket) || (ulong)ticket!=action.position_ticket ||
         !PositionGetInteger(POSITION_TYPE,side) ||
         side!=(action.side==1 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL) ||
         !PositionGetString(POSITION_SYMBOL,symbol) || symbol!=key.symbol ||
         !PositionGetDouble(POSITION_SL,sl) ||
         !RecoveryDispatchUnits(sl,tick,true,range.live_sl) ||
         !PositionGetDouble(POSITION_TP,tp) ||
         !RecoveryDispatchUnits(tp,tick,true,action.tp_ticks)) return false;
      return RecoveryIntentContextOK(key);
     }
   if(action.kind==RR_CANCEL)
     {
      RecoveryExactOrderScope scope;
      scope.login=key.login;scope.server=key.server;scope.symbol=key.symbol;
      scope.magic=(ulong)key.magic;scope.ticket=action.order_ticket;
      RecoveryExactOrderObservation row;
      if(RecoveryObserveExactOrder(scope,row)!=RECOVERY_EXACT_ORDER_OBSERVED ||
         row.source!=RECOVERY_EXACT_ORDER_CURRENT ||
         row.state!=ORDER_STATE_PLACED || row.position_identifier!=0 ||
         row.type!=(action.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
         row.tp!=0.0 || row.stop_limit!=0.0 ||
         !RecoveryDispatchUnits(row.price,tick,false,action.entry_ticks) ||
         !RecoveryDispatchUnits(row.sl,tick,false,action.sl_ticks) ||
         !RecoveryDispatchUnits(row.initial_volume,step,false,action.volume_steps) ||
         !RecoveryDispatchUnits(row.remaining_volume,step,false,action.volume_steps)) return false;
      return RecoveryIntentContextOK(key);
     }
   return action.kind==RR_PRIMARY || action.kind==RR_REVERSE || action.kind==RR_REENTRY;
  }

RecoveryDispatchState RecoveryDispatchOnce(const RRRangeState &range,const RRAction &action,
                                           const RecoveryIntentKey &key,const double tick,
                                           const double step,RecoveryDispatchResult &out)
  {
   ZeroMemory(out);
   out.state=RD_NOT_SENT;
   if(!RecoveryIntentContextOK(key) || (ulong)key.generation!=action.generation)
      return out.state;
   string prior;
   if(RecoveryIntentLookup(key,prior)!=RI_NO_RECORD)
     {
      out.state=RD_EXISTING_INTENT;
      return out.state;
     }
   MqlTradeRequest request;
   if(!RecoveryBuildStagedRequest(range,action,key.symbol,(ulong)key.magic,tick,step,request) ||
      !RecoveryDispatchTarget(key,range,action,tick,step))
      return out.state;
   MqlTradeCheckResult check;
   ZeroMemory(check);
   const bool checked=OrderCheck(request,check);
   out.check_retcode=check.retcode;
   // Common success is zero; do not send after a broker-side rejection.
   if(!checked || (check.retcode!=0 && check.retcode!=TRADE_RETCODE_DONE))
      return out.state;
   if(!RecoveryIntentReserve(key))
     {
      out.state=RD_EXISTING_INTENT;
      return out.state;
     }
   out.state=RD_RESERVED_NOT_SENT;
   if(!RecoveryDispatchTarget(key,range,action,tick,step)) return out.state;

   MqlTradeResult result;
   ZeroMemory(result);
   out.state=RD_OUTCOME_UNRESOLVED;
   const bool submitted=OrderSend(request,result);
   out.send_retcode=result.retcode;
   // This ACK binds a ticket only; the independent verifier still proves the
   // effect. No return code here advances strategy state or authorizes retry.
   if(submitted && request.action==TRADE_ACTION_PENDING && result.order!=0 &&
      (result.retcode==TRADE_RETCODE_PLACED || result.retcode==TRADE_RETCODE_DONE ||
       result.retcode==TRADE_RETCODE_DONE_PARTIAL) &&
      RecoveryIntentRecordTicket(key,result.order))
     {
      out.order=result.order;
      out.state=RD_TICKET_RECORDED;
     }
   return out.state;
  }
#endif
