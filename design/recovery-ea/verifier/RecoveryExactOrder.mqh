#ifndef RECOVERY_EXACT_ORDER_MQH
#define RECOVERY_EXACT_ORDER_MQH

// Read-only exact-ticket observation. HistoryOrderSelect resets its local
// history order list; do not call while enumerating a position's history.
enum RecoveryExactOrderResult {
   RECOVERY_EXACT_ORDER_INVALID_SCOPE,
   RECOVERY_EXACT_ORDER_SESSION,
   RECOVERY_EXACT_ORDER_UNKNOWN,
   RECOVERY_EXACT_ORDER_MISMATCH,
   RECOVERY_EXACT_ORDER_READ_ERROR,
   RECOVERY_EXACT_ORDER_CHANGED,
   RECOVERY_EXACT_ORDER_OBSERVED
};
enum RecoveryExactOrderSource {
   RECOVERY_EXACT_ORDER_NONE,
   RECOVERY_EXACT_ORDER_CURRENT,
   RECOVERY_EXACT_ORDER_HISTORY
};
struct RecoveryExactOrderScope {
   long login;
   string server;
   string symbol;
   ulong magic;
   ulong ticket;
};
struct RecoveryExactOrderObservation {
   RecoveryExactOrderResult result;
   RecoveryExactOrderSource source;
   ulong ticket;
   ulong magic;
   ulong position_identifier; // 0 is valid: some orders never link to a position.
   string symbol;
   long type;
   long state;
   double initial_volume;
   double remaining_volume;
   double price;
   double sl;
   double tp;
   double stop_limit;
};

bool RecoveryExactOrderBound(const RecoveryExactOrderScope &scope) {
   return TerminalInfoInteger(TERMINAL_CONNECTED)!=0 &&
          AccountInfoInteger(ACCOUNT_LOGIN)==scope.login &&
          AccountInfoString(ACCOUNT_SERVER)==scope.server;
}
bool RecoveryExactOrderNonnegative(const double value) {
   return MathIsValidNumber(value) && value>=0.0;
}
void RecoveryExactOrderClear(RecoveryExactOrderObservation &out,const RecoveryExactOrderResult result) {
   out.result=result; out.source=RECOVERY_EXACT_ORDER_NONE;
   out.ticket=0; out.magic=0; out.position_identifier=0; out.symbol="";
   out.type=0; out.state=0; out.initial_volume=0.0; out.remaining_volume=0.0;
   out.price=0.0; out.sl=0.0; out.tp=0.0; out.stop_limit=0.0;
}
bool RecoveryExactInteger(const bool historical,const ulong ticket,
                          const ENUM_ORDER_PROPERTY_INTEGER key,long &value) {
   return historical ? HistoryOrderGetInteger(ticket,key,value) : OrderGetInteger(key,value);
}
bool RecoveryExactDouble(const bool historical,const ulong ticket,
                         const ENUM_ORDER_PROPERTY_DOUBLE key,double &value) {
   return historical ? HistoryOrderGetDouble(ticket,key,value) : OrderGetDouble(key,value);
}

RecoveryExactOrderResult RecoveryExactOrderSample(const RecoveryExactOrderScope &scope,
                                                  RecoveryExactOrderObservation &observation) {
   if(!RecoveryExactOrderBound(scope)) return RECOVERY_EXACT_ORDER_SESSION;
   bool historical=false;
   if(!OrderSelect(scope.ticket)) {
      if(!RecoveryExactOrderBound(scope)) return RECOVERY_EXACT_ORDER_SESSION;
      if(!HistoryOrderSelect(scope.ticket))
         return RecoveryExactOrderBound(scope) ? RECOVERY_EXACT_ORDER_UNKNOWN : RECOVERY_EXACT_ORDER_SESSION;
      historical=true;
   }
   const ulong ticket=scope.ticket;
   long actual,magic,position,type,state;
   string symbol;
   double initial,remaining,price,sl,tp,stop_limit;
   if(!RecoveryExactInteger(historical,ticket,ORDER_TICKET,actual) ||
      !RecoveryExactInteger(historical,ticket,ORDER_MAGIC,magic) ||
      !(historical ? HistoryOrderGetString(ticket,ORDER_SYMBOL,symbol) : OrderGetString(ORDER_SYMBOL,symbol)))
      return RECOVERY_EXACT_ORDER_READ_ERROR;
   // Do not try another namespace when an existing record fails identity checks.
   if((ulong)actual!=ticket || (ulong)magic!=scope.magic || symbol!=scope.symbol)
      return RECOVERY_EXACT_ORDER_MISMATCH;
   if(!RecoveryExactInteger(historical,ticket,ORDER_POSITION_ID,position) ||
      !RecoveryExactInteger(historical,ticket,ORDER_TYPE,type) || type<0 ||
      !RecoveryExactInteger(historical,ticket,ORDER_STATE,state) || state<0 ||
      !RecoveryExactDouble(historical,ticket,ORDER_VOLUME_INITIAL,initial) ||
      !RecoveryExactOrderNonnegative(initial) || initial<=0.0 ||
      !RecoveryExactDouble(historical,ticket,ORDER_VOLUME_CURRENT,remaining) ||
      !RecoveryExactOrderNonnegative(remaining) || remaining>initial ||
      !RecoveryExactDouble(historical,ticket,ORDER_PRICE_OPEN,price) || !RecoveryExactOrderNonnegative(price) ||
      !RecoveryExactDouble(historical,ticket,ORDER_SL,sl) || !RecoveryExactOrderNonnegative(sl) ||
      !RecoveryExactDouble(historical,ticket,ORDER_TP,tp) || !RecoveryExactOrderNonnegative(tp) ||
      !RecoveryExactDouble(historical,ticket,ORDER_PRICE_STOPLIMIT,stop_limit) ||
      !RecoveryExactOrderNonnegative(stop_limit)) return RECOVERY_EXACT_ORDER_READ_ERROR;
   if(!RecoveryExactOrderBound(scope)) return RECOVERY_EXACT_ORDER_SESSION;
   observation.result=RECOVERY_EXACT_ORDER_OBSERVED;
   observation.source=historical ? RECOVERY_EXACT_ORDER_HISTORY : RECOVERY_EXACT_ORDER_CURRENT;
   observation.ticket=ticket; observation.magic=(ulong)magic; observation.position_identifier=(ulong)position;
   observation.symbol=symbol; observation.type=type; observation.state=state;
   observation.initial_volume=initial; observation.remaining_volume=remaining;
   observation.price=price; observation.sl=sl; observation.tp=tp; observation.stop_limit=stop_limit;
   return RECOVERY_EXACT_ORDER_OBSERVED;
}

bool RecoveryExactOrderSame(const RecoveryExactOrderObservation &a,const RecoveryExactOrderObservation &b) {
   return a.source==b.source && a.ticket==b.ticket && a.magic==b.magic &&
          a.position_identifier==b.position_identifier && a.symbol==b.symbol && a.type==b.type &&
          a.state==b.state && a.initial_volume==b.initial_volume && a.remaining_volume==b.remaining_volume &&
          a.price==b.price && a.sl==b.sl && a.tp==b.tp && a.stop_limit==b.stop_limit;
}

// Only OBSERVED contains a broker record. UNKNOWN is not negative evidence.
// Two matching observations are not an atomic snapshot or effect confirmation.
RecoveryExactOrderResult RecoveryObserveExactOrder(const RecoveryExactOrderScope &scope,
                                                   RecoveryExactOrderObservation &out) {
   RecoveryExactOrderClear(out,RECOVERY_EXACT_ORDER_INVALID_SCOPE);
   if(scope.login<=0 || scope.server=="" || scope.symbol=="" || scope.magic==0 || scope.ticket==0)
      return out.result;
   RecoveryExactOrderObservation first;
   RecoveryExactOrderResult result=RecoveryExactOrderSample(scope,first);
   if(result==RECOVERY_EXACT_ORDER_OBSERVED) {
      result=RecoveryExactOrderSample(scope,out);
      if(result==RECOVERY_EXACT_ORDER_UNKNOWN ||
         (result==RECOVERY_EXACT_ORDER_OBSERVED && !RecoveryExactOrderSame(first,out)))
         result=RECOVERY_EXACT_ORDER_CHANGED;
   }
   if(result!=RECOVERY_EXACT_ORDER_OBSERVED) RecoveryExactOrderClear(out,result);
   return result;
}
#endif
