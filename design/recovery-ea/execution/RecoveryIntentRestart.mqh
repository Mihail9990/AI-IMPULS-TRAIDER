#ifndef RECOVERY_INTENT_RESTART_MQH
#define RECOVERY_INTENT_RESTART_MQH
#include "RecoveryIntentJournal.mqh"

// One action at a time. The restart recovery API has no send/arm outcome.
enum RecoveryRestartRoute
  {
   RR_NOT_RECORDED=0,
   RR_TICKET_UNKNOWN=1,
   RR_READ_EXACT_ORDER=2,
   RR_READ_EXACT_HISTORY=3,
   RR_JOURNAL_CONFLICT=4,
   RR_TERMINAL_UNAVAILABLE=5
  };

struct RecoveryRestartDecision
  {
   RecoveryRestartRoute route;
   string order_ticket;
   string position_id;
  };

// Called only after fresh broker evidence proves the saved ticket-to-position link.
bool RecoveryIntentRecordPositionLink(const RecoveryIntentKey &key,const string order_ticket,
                                      const ulong verified_position_id)
  {
   string saved;
   if(verified_position_id==0 || RecoveryIntentLookup(key,saved)!=RI_TICKET ||
      order_ticket!=saved) return false;
   return RecoveryIntentCreate(key,RecoveryIntentPath(key)+".position",
      "RECOVERY_INTENT_POSITION_V1\t"+RecoveryIntentLine(key)+"\t"+saved+
      "\t"+(string)verified_position_id+"\tEND\n");
  }

void RecoveryIntentRestart(const RecoveryIntentKey &key,RecoveryRestartDecision &decision)
  {
   decision.route=RR_JOURNAL_CONFLICT;
   decision.order_ticket="";
   decision.position_id="";
   string exact_order;
   RecoveryIntentState stored=RecoveryIntentLookup(key,exact_order);
   switch(stored)
     {
      case RI_NO_RECORD: decision.route=RR_NOT_RECORDED; return;
      case RI_UNRESOLVED: decision.route=RR_TICKET_UNKNOWN; return;
      case RI_UNAVAILABLE: decision.route=RR_TERMINAL_UNAVAILABLE; return;
      case RI_TICKET:
        {
         string path=RecoveryIntentPath(key)+".position";
         if(!FileIsExist(path))
           {
            decision.order_ticket=exact_order;
            decision.route=RR_READ_EXACT_ORDER;
            return;
           }
         string wire;
         if(!RecoveryIntentRead(path,wire)) return;
         string prefix="RECOVERY_INTENT_POSITION_V1\t"+RecoveryIntentLine(key)+"\t"+exact_order+"\t";
         int start=StringLen(prefix), length=StringLen(wire)-start-5;
         if(length<1 || StringSubstr(wire,0,start)!=prefix ||
            StringSubstr(wire,start+length)!="\tEND\n") return;
         string id=StringSubstr(wire,start,length);
         if(!RecoveryInvNatural(id,"18446744073709551615",true)) return;
         decision.order_ticket=exact_order;
         decision.position_id=id;
         decision.route=RR_READ_EXACT_HISTORY;
         return;
        }
      default: return;
     }
  }

// Stable diagnostic keys for the existing journal UI; never log account details.
string RecoveryRestartLogReason(const RecoveryRestartRoute route)
  {
   switch(route)
     {
      case RR_NOT_RECORDED: return "intent_not_recorded";
      case RR_TICKET_UNKNOWN: return "broker_ticket_unknown";
      case RR_READ_EXACT_ORDER: return "check_exact_order";
      case RR_READ_EXACT_HISTORY: return "check_exact_history";
      case RR_TERMINAL_UNAVAILABLE: return "terminal_unavailable";
      default: return "intent_record_conflict";
     }
  }
#endif
