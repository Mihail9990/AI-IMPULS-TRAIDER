#ifndef RECOVERY_EXACT_ORDER_WIRE_MQH
#define RECOVERY_EXACT_ORDER_WIRE_MQH
#include "RecoveryExactOrder.mqh"

string RecoveryExactOrderReason(const RecoveryExactOrderResult result) {
   if(result==RECOVERY_EXACT_ORDER_OBSERVED) return "ok";
   if(result==RECOVERY_EXACT_ORDER_UNKNOWN) return "unknown";
   if(result==RECOVERY_EXACT_ORDER_MISMATCH) return "mismatch";
   if(result==RECOVERY_EXACT_ORDER_CHANGED) return "changed";
   if(result==RECOVERY_EXACT_ORDER_SESSION) return "session";
   if(result==RECOVERY_EXACT_ORDER_READ_ERROR) return "read";
   return "";
}
string RecoveryExactOrderWireNumber(const double value) {
   return DoubleToString(value==0 ? 0.0 : value,16);
}
string RecoveryExactOrderResponseBody(const RecoveryExactOrderScope &scope,
                                      const RecoveryExactOrderObservation &value) {
   string reason=RecoveryExactOrderReason(value.result);
   if(reason=="" || scope.ticket==0) return "";
   if(value.result!=RECOVERY_EXACT_ORDER_OBSERVED)
      return (string)scope.ticket+"\tunavailable\t"+reason+"\nEND\n";
   if(value.ticket!=scope.ticket || value.magic!=scope.magic || value.symbol!=scope.symbol ||
      (value.source!=RECOVERY_EXACT_ORDER_CURRENT && value.source!=RECOVERY_EXACT_ORDER_HISTORY)) return "";
   return (string)scope.ticket+"\tobserved\tok\nE\t"+
          (value.source==RECOVERY_EXACT_ORDER_CURRENT ? "current" : "history")+"\t"+
          (string)value.position_identifier+"\t"+(string)value.magic+"\t"+
          (string)value.type+"\t"+(string)value.state+"\t"+
          RecoveryExactOrderWireNumber(value.initial_volume)+"\t"+
          RecoveryExactOrderWireNumber(value.remaining_volume)+"\t"+
          RecoveryExactOrderWireNumber(value.price)+"\t"+RecoveryExactOrderWireNumber(value.sl)+"\t"+
          RecoveryExactOrderWireNumber(value.tp)+"\t"+RecoveryExactOrderWireNumber(value.stop_limit)+"\nEND\n";
}
#endif
