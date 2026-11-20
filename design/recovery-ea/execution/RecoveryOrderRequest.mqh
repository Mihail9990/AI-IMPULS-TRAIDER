#ifndef RECOVERY_ORDER_REQUEST_MQH
#define RECOVERY_ORDER_REQUEST_MQH

// Pure request assembler. Never dispatches or treats a request as a fill.
enum RecoveryRequestKind
  {
   RRQ_STOP_ENTRY=1,
   RRQ_POSITION_SL=2,
   RRQ_MOVE_STOP_ENTRY=3,
   RRQ_CANCEL_STOP_ENTRY=4
  };
struct RecoveryOrderSpec
  {
   RecoveryRequestKind kind;
   string symbol;
   ulong magic;
   ulong position_ticket;
   ulong order_ticket;
   int side;               // +1 BUY, -1 SELL, 0 only for position protection
   long entry_ticks;
   long stop_ticks;
   long take_ticks;
   long volume_steps;
   double tick;
   double step;
  };
bool RecoveryRequestNumber(const double x) { return MathIsValidNumber(x) && x>0.0; }
bool RecoveryRequestUnits(const long n) { return n>=0 && n<=1000000000000; }

bool RecoveryOrderBuild(const RecoveryOrderSpec &s,MqlTradeRequest &request)
  {
   ZeroMemory(request);
   if(s.symbol=="" || s.magic==0 || !RecoveryRequestNumber(s.tick) ||
      !RecoveryRequestNumber(s.step) ||
      !RecoveryRequestUnits(s.entry_ticks) || !RecoveryRequestUnits(s.stop_ticks) ||
      !RecoveryRequestUnits(s.take_ticks) || !RecoveryRequestUnits(s.volume_steps))
      return false;
   const double entry=s.entry_ticks*s.tick, stop=s.stop_ticks*s.tick,
      take=s.take_ticks*s.tick, volume=s.volume_steps*s.step;
   if(s.entry_ticks>0 && !RecoveryRequestNumber(entry)) return false;
   if(s.stop_ticks>0 && !RecoveryRequestNumber(stop)) return false;
   if(s.take_ticks>0 && !RecoveryRequestNumber(take)) return false;
   if(s.volume_steps>0 && !RecoveryRequestNumber(volume)) return false;
   MqlTradeRequest built;
   ZeroMemory(built);
   built.symbol=s.symbol;
   built.magic=s.magic;
   if(s.kind==RRQ_POSITION_SL)
     {
      // Caller proves the exact position ticket AND identifier and supplies
      // the already-observed TP, including 0 when it genuinely has no TP.
      if(s.position_ticket==0 || s.order_ticket!=0 || s.side!=0 || s.entry_ticks!=0 ||
         s.stop_ticks==0 || s.volume_steps!=0) return false;
      built.action=TRADE_ACTION_SLTP;
      built.position=s.position_ticket;
      built.sl=stop;
      built.tp=take;
     }
   else if(s.kind==RRQ_CANCEL_STOP_ENTRY)
     {
      if(s.order_ticket==0 || s.position_ticket!=0 || (s.side!=1 && s.side!=-1) ||
         s.entry_ticks!=0 || s.stop_ticks!=0 || s.take_ticks!=0 || s.volume_steps!=0)
         return false;
      built.action=TRADE_ACTION_REMOVE;
      built.order=s.order_ticket;
     }
   else if(s.kind==RRQ_STOP_ENTRY || s.kind==RRQ_MOVE_STOP_ENTRY)
     {
      if(s.position_ticket!=0 || (s.side!=1 && s.side!=-1) || s.entry_ticks==0 ||
         s.stop_ticks==0 || (s.entry_ticks-s.stop_ticks)*s.side<=0 ||
         (s.take_ticks!=0 && (s.take_ticks-s.entry_ticks)*s.side<=0)) return false;
      // Only ordinary GTC buy/sell stop orders. A pending-order edit must not
      // silently change the active order volume or its intentional TP value.
      if(s.kind==RRQ_STOP_ENTRY)
        {
         if(s.order_ticket!=0 || s.volume_steps==0) return false;
         built.action=TRADE_ACTION_PENDING;
         built.type=(s.side==1 ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP);
         built.volume=volume;
         built.type_filling=ORDER_FILLING_RETURN;
        }
      else
        {
         if(s.order_ticket==0 || s.volume_steps!=0) return false;
         built.action=TRADE_ACTION_MODIFY;
         built.order=s.order_ticket;
        }
      built.price=entry;
      built.sl=stop;
      built.tp=take;
      built.type_time=ORDER_TIME_GTC;
     }
   else return false;
   request=built;
   return true;
  }
#endif
