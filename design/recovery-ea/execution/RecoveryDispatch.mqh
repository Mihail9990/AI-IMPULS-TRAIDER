#ifndef RECOVERY_DISPATCH_MQH
#define RECOVERY_DISPATCH_MQH
#include "RecoveryActionRequest.mqh"
#include "RecoveryIntentJournal.mqh"
#include "RecoveryAdoptionInventory.mqh"
#include "../verifier/RecoveryExactOrder.mqh"

// Control layer must retain the full immutable action in this session and enforce a single
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
   bool synchronous_sl_ack; // Diagnostic only; never proves observed SL.
   bool async_sl_submitted;  // Local accepted send, not broker acceptance.
   bool synchronous_move_rejected; // Exact matched response only, never a timeout.
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
   if(action.kind==RR_CANCEL || action.kind==RR_MOVE_SUPPORT_OPPOSITE)
     {
      long expected_entry=action.entry_ticks,expected_sl=action.sl_ticks,expected_volume=action.volume_steps;
      if(action.kind==RR_MOVE_SUPPORT_OPPOSITE)
        {
         const RRKnownOrder old=range.retire;
         if(old.ticket!=action.order_ticket || old.side!=action.side || old.volume_steps!=action.volume_steps)
            return false;
         expected_entry=old.entry_ticks;expected_sl=old.sl_ticks;expected_volume=old.volume_steps;
        }
      RecoveryExactOrderScope scope;
      scope.login=key.login;scope.server=key.server;scope.symbol=key.symbol;
      scope.magic=(ulong)key.magic;scope.ticket=action.order_ticket;
      RecoveryExactOrderObservation row;
      if(RecoveryObserveExactOrder(scope,row)!=RECOVERY_EXACT_ORDER_OBSERVED ||
         row.source!=RECOVERY_EXACT_ORDER_CURRENT ||
         row.state!=ORDER_STATE_PLACED || row.position_identifier!=0 ||
         row.type!=(action.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
         row.tp!=0.0 || row.stop_limit!=0.0 ||
         !RecoveryDispatchUnits(row.price,tick,false,expected_entry) ||
         !RecoveryDispatchUnits(row.sl,tick,false,expected_sl) ||
         !RecoveryDispatchUnits(row.initial_volume,step,false,expected_volume) ||
         !RecoveryDispatchUnits(row.remaining_volume,step,false,expected_volume)) return false;
      return RecoveryIntentContextOK(key);
     }
   // For two-sided support reentry, the old fixed opposite must still
   // be that exact untouched STOP. RecoveryDispatchImpl checks both before
   // OrderCheck and again after reservation, immediately before OrderSend.
   if(action.kind==RR_REENTRY && range.opposite.ticket!=0)
     {
      const RRKnownOrder keep=range.opposite;
      if(keep.side!=-action.side || keep.volume_steps<=0)return false;
      RecoveryExactOrderScope target;
      target.login=key.login;target.server=key.server;target.symbol=key.symbol;
      target.magic=(ulong)key.magic;target.ticket=keep.ticket;
      RecoveryExactOrderObservation row;
      if(RecoveryObserveExactOrder(target,row)!=RECOVERY_EXACT_ORDER_OBSERVED ||
         row.source!=RECOVERY_EXACT_ORDER_CURRENT || row.state!=ORDER_STATE_PLACED ||
         row.position_identifier!=0 ||
         row.type!=(keep.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP) ||
         row.tp!=0.0 || row.stop_limit!=0.0 ||
         !RecoveryDispatchUnits(row.price,tick,false,keep.entry_ticks) ||
         !RecoveryDispatchUnits(row.sl,tick,false,keep.sl_ticks) ||
         !RecoveryDispatchUnits(row.initial_volume,step,false,keep.volume_steps) ||
         !RecoveryDispatchUnits(row.remaining_volume,step,false,keep.volume_steps))return false;
      return RecoveryIntentContextOK(key);
     }
   return action.kind==RR_PRIMARY || action.kind==RR_REVERSE || action.kind==RR_REENTRY;
  }

enum RecoveryDispatchMode { RDM_SYNC=0,RDM_ASYNC_SL,RDM_ASYNC_CANCEL };

RecoveryDispatchState RecoveryDispatchImpl(const RRRangeState &range,const RRAction &action,
                                           const RecoveryIntentKey &key,const double tick,
                                           const double step,const RecoveryDispatchMode mode,RecoveryDispatchResult &out)
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
   if(mode!=RDM_SYNC)
     {
      // These narrow async paths check the exact target locally, so neither
      // waits for a server-side OrderCheck before the next independent leg.
      const bool sl=mode==RDM_ASYNC_SL && request.action==TRADE_ACTION_SLTP &&
                    (action.kind==RR_INITIAL_SL || action.kind==RR_SUPPORT_SL);
      const bool cancel=mode==RDM_ASYNC_CANCEL && request.action==TRADE_ACTION_REMOVE &&
                        action.kind==RR_CANCEL;
      if(!sl && !cancel)return out.state;
     }
   else
     {
      MqlTradeCheckResult check;
      ZeroMemory(check);
      const bool checked=OrderCheck(request,check);
      out.check_retcode=check.retcode;
      if(!checked || (check.retcode!=0 && check.retcode!=TRADE_RETCODE_DONE))
         return out.state;
     }
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
   if(mode!=RDM_SYNC)
     {
      const bool submitted_async=OrderSendAsync(request,result);
      out.async_sl_submitted=(mode==RDM_ASYNC_SL && submitted_async);
      out.send_retcode=result.retcode;
      if(!submitted_async) out.state=RD_RESERVED_NOT_SENT;
      return out.state;
     }
   const bool submitted=OrderSend(request,result);
   out.send_retcode=result.retcode;
   out.synchronous_sl_ack=(submitted && request.action==TRADE_ACTION_SLTP &&
                           result.retcode==TRADE_RETCODE_DONE);
   out.synchronous_move_rejected=(submitted && action.kind==RR_MOVE_SUPPORT_OPPOSITE &&
      request.action==TRADE_ACTION_MODIFY &&
      (result.retcode==TRADE_RETCODE_REJECT || result.retcode==TRADE_RETCODE_INVALID_PRICE ||
       result.retcode==TRADE_RETCODE_INVALID_STOPS || result.retcode==TRADE_RETCODE_FROZEN));
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

RecoveryDispatchState RecoveryDispatchOnce(const RRRangeState &range,const RRAction &action,
                                           const RecoveryIntentKey &key,const double tick,
                                           const double step,RecoveryDispatchResult &out)
  {
   return RecoveryDispatchImpl(range,action,key,tick,step,RDM_SYNC,out);
  }

RecoveryDispatchState RecoveryDispatchOpeningSLAsync(const RRRangeState &range,const RRAction &action,
                                                      const RecoveryIntentKey &key,const double tick,
                                                      const double step,RecoveryDispatchResult &out)
  {
   return RecoveryDispatchImpl(range,action,key,tick,step,RDM_ASYNC_SL,out);
  }
// Exact OCO cancel already identifies the ticket; async response cannot
// prove cancellation. The independent exact history check still owns that.
RecoveryDispatchState RecoveryDispatchExactCancelAsync(const RRRangeState &range,const RRAction &action,
    const RecoveryIntentKey &key,const double tick,const double step,RecoveryDispatchResult &out)
  {
   return RecoveryDispatchImpl(range,action,key,tick,step,RDM_ASYNC_CANCEL,out);
  }
#endif
