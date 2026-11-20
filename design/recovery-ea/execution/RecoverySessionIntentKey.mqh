#ifndef RECOVERY_SESSION_INTENT_KEY_MQH
#define RECOVERY_SESSION_INTENT_KEY_MQH
#include "RecoveryIntentJournal.mqh"
#include "../cycle/RecoveryRangeCoordinator.mqh"

struct RecoverySessionIntentScope {
 string executor_epoch;
 long login,magic;
 string server,symbol;
 ulong scope,generation;
};

string RecoverySessionSha256(const string wire)
  {
   uchar bytes[],unused[],digest[];
   const int n=StringToCharArray(wire,bytes,0,WHOLE_ARRAY,CP_UTF8);
   if(n<2 || ArrayResize(bytes,n-1)!=n-1 ||
      CryptEncode(CRYPT_HASH_SHA256,bytes,unused,digest)!=32 || ArraySize(digest)!=32) return "";
   const string alphabet="0123456789abcdef";
   string hex="";
   for(int i=0;i<32;i++)
      hex+=StringSubstr(alphabet,digest[i]>>4,1)+StringSubstr(alphabet,digest[i]&15,1);
   return hex;
  }

// ID identifies the assigned action slot, not its price: a conflicting
// payload in the same slot must NOT create another sendable intent ID.
bool RecoveryBuildSessionIntentKey(const RecoverySessionIntentScope &scope,
      const RRAction &action,const double tick,const double step,RecoveryIntentKey &out)
  {
   ZeroMemory(out);
   if(!RecoveryInvHex(scope.executor_epoch) || scope.login<=0 || scope.magic<=0 ||
      !RecoveryInvContext(scope.server) || !RecoveryInvContext(scope.symbol) ||
      action.scope==0 || action.scope!=scope.scope || action.generation==0 ||
      action.generation!=scope.generation || action.generation>(ulong)LONG_MAX ||
      action.serial==0 || (action.range_id!=1 && action.range_id!=2) ||
      action.kind==RR_NONE || !RCValidSide(action.side) ||
      !MathIsValidNumber(tick) || !MathIsValidNumber(step) || tick<=0 || step<=0) return false;
   const string identity=scope.executor_epoch+"\t"+(string)scope.login+"\t"+scope.server+
      "\t"+scope.symbol+"\t"+(string)scope.magic+"\t"+(string)scope.scope+
      "\t"+(string)scope.generation+"\t"+(string)action.range_id+"\t"+(string)action.serial;
   const string terms=(string)action.kind+"\t"+(string)action.side+"\t"+(string)action.stage+
      "\t"+(string)action.order_ticket+"\t"+(string)action.position_id+"\t"+(string)action.position_ticket+
      "\t"+(string)action.entry_ticks+"\t"+(string)action.sl_ticks+"\t"+(string)action.tp_ticks+
      "\t"+(string)action.volume_steps+"\t"+DoubleToString(tick,-16)+"\t"+DoubleToString(step,-16);
   const string id_hash=RecoverySessionSha256("RECOVERY_SESSION_ID_V1\t"+identity);
   const string fingerprint=RecoverySessionSha256("RECOVERY_SESSION_PAYLOAD_V1\t"+identity+"\t"+terms);
   if(StringLen(id_hash)!=64 || StringLen(fingerprint)!=64) return false;
   out.id=StringSubstr(id_hash,0,32);out.fingerprint=fingerprint;
   out.login=scope.login;out.server=scope.server;out.symbol=scope.symbol;
   out.magic=scope.magic;out.generation=(long)scope.generation;
   return RecoveryIntentKeyOK(out);
  }
#endif
