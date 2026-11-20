#ifndef RECOVERY_ACTION_REQUEST_MQH
#define RECOVERY_ACTION_REQUEST_MQH
#include "../cycle/RecoveryRangeCoordinator.mqh"
#include "RecoveryOrderRequest.mqh"

// Convert only an already-reserved, matching coordinator intent. Retains the
// action's complete identity in the caller; MT5 requests don't carry it all.
bool RecoveryBuildStagedRequest(const RRRangeState &range,const RRAction &action,
                                const string symbol,const ulong magic,
                                const double tick,const double step,
                                MqlTradeRequest &request)
  {
   ZeroMemory(request);
   if(action.serial==0 || action.scope==0 || action.generation==0 ||
      action.scope!=range.scope || action.generation!=range.cycle.generation ||
      action.range_id!=range.cycle.range_id ||
      !RRActionEqual(range.pending,action)) return false;
   RecoveryOrderSpec s;
   ZeroMemory(s);
   s.symbol=symbol;s.magic=magic;s.tick=tick;s.step=step;
   switch(action.kind)
     {
      case RR_PRIMARY:
      case RR_REVERSE:
      case RR_REENTRY:
         if(action.order_ticket!=0 || action.position_ticket!=0 || action.position_id!=0) return false;
         s.kind=RRQ_STOP_ENTRY;s.side=action.side;
         s.entry_ticks=action.entry_ticks;s.stop_ticks=action.sl_ticks;
         s.take_ticks=action.tp_ticks;s.volume_steps=action.volume_steps;
         break;
      case RR_INITIAL_SL:
      case RR_SUPPORT_SL:
         // Order ticket here is the confirmed entry, not the modify target.
         if(action.position_id==0 || action.position_id!=range.cycle.position_id ||
            action.position_ticket==0 || action.position_ticket!=range.position_ticket ||
            action.order_ticket==0 || action.order_ticket!=range.cycle.active_order ||
            action.side!=range.cycle.side || action.tp_ticks!=range.live_tp ||
            action.entry_ticks!=0 || action.volume_steps!=0) return false;
         s.kind=RRQ_POSITION_SL;s.position_ticket=action.position_ticket;
         s.stop_ticks=action.sl_ticks;s.take_ticks=action.tp_ticks;
         break;
      case RR_CANCEL:
        {
         if(action.order_ticket==0 || action.position_ticket!=0 || action.position_id!=0 ||
            action.tp_ticks!=0) return false;
         RRKnownOrder owned;
         if(range.retire.ticket==action.order_ticket)
            owned=range.retire;
         else if(range.phase==RR_DRAIN && range.entry.ticket==action.order_ticket)
            owned=range.entry;
         else if(range.phase==RR_DRAIN && range.opposite.ticket==action.order_ticket)
            owned=range.opposite;
         else return false;
         if(owned.side!=action.side || owned.entry_ticks!=action.entry_ticks ||
            owned.sl_ticks!=action.sl_ticks || owned.volume_steps!=action.volume_steps)
            return false;
         s.kind=RRQ_CANCEL_STOP_ENTRY;s.order_ticket=owned.ticket;s.side=owned.side;
         break;
        }
      default: return false;
     }
   return RecoveryOrderBuild(s,request);
  }
#endif
