#ifndef RECOVERY_INTENT_JOURNAL_MQH
#define RECOVERY_INTENT_JOURNAL_MQH
#include "../verifier/RecoveryInventoryReader.mqh"

enum RecoveryIntentState
  {
   RI_NO_RECORD=0,   // Missing is not broker evidence or authorization to send.
   RI_UNRESOLVED=1,  // This identity was reserved; never send it again.
   RI_TICKET=2,      // Order ticket recorded; verify broker effect separately.
   RI_CONFLICT=3,   // Conflicting identity or payload in this EA session.
   RI_UNAVAILABLE=4 // Terminal disconnected; not a broker outcome.
  };

struct RecoveryIntentKey
  {
   string id;
   long login;
   string server;
   string symbol;
   long magic;
   long generation;
   string fingerprint; // Lowercase SHA-256 of the canonical intent saved by the caller.
  };

bool RecoveryIntentKeyOK(const RecoveryIntentKey &key)
  {
   return RecoveryInvHex(key.id) && StringLen(key.fingerprint)==64 &&
          RecoveryInvHex(StringSubstr(key.fingerprint,0,32)) &&
          RecoveryInvHex(StringSubstr(key.fingerprint,32,32)) && key.login>0 && key.magic>0 &&
          key.generation>=0 && RecoveryInvContext(key.server) && RecoveryInvContext(key.symbol);
  }

bool RecoveryIntentContextOK(const RecoveryIntentKey &key)
  {
   return RecoveryIntentKeyOK(key) && TerminalInfoInteger(TERMINAL_CONNECTED)!=0 &&
          AccountInfoInteger(ACCOUNT_LOGIN)==key.login &&
          AccountInfoString(ACCOUNT_SERVER)==key.server && _Symbol==key.symbol;
  }

// EA-local RAM only. No startup load and no cross-session trade suppression.
struct RecoverySessionIntent {
   RecoveryIntentKey key;
   ulong order;
};
RecoverySessionIntent RecoverySessionIntents[];

int RecoveryIntentIndex(const string id)
  {
   for(int i=ArraySize(RecoverySessionIntents)-1;i>=0;i--)
      if(RecoverySessionIntents[i].key.id==id) return i;
   return -1;
  }

bool RecoveryIntentKeysMatch(const RecoveryIntentKey &a,const RecoveryIntentKey &b)
  {
   return a.id==b.id && a.login==b.login && a.server==b.server &&
          a.symbol==b.symbol && a.magic==b.magic && a.generation==b.generation &&
          a.fingerprint==b.fingerprint;
  }

RecoveryIntentState RecoveryIntentLookup(const RecoveryIntentKey &key,string &order_ticket)
  {
   order_ticket="";
   if(!RecoveryIntentKeyOK(key)) return RI_CONFLICT;
   if(TerminalInfoInteger(TERMINAL_CONNECTED)==0) return RI_UNAVAILABLE;
   if(!RecoveryIntentContextOK(key)) return RI_CONFLICT;
   const int at=RecoveryIntentIndex(key.id);
   if(at<0) return RI_NO_RECORD;
   if(!RecoveryIntentKeysMatch(key,RecoverySessionIntents[at].key)) return RI_CONFLICT;
   if(RecoverySessionIntents[at].order==0) return RI_UNRESOLVED;
   order_ticket=(string)RecoverySessionIntents[at].order;
   return RI_TICKET;
  }

bool RecoveryIntentReserve(const RecoveryIntentKey &key)
  {
   string previous;
   if(RecoveryIntentLookup(key,previous)!=RI_NO_RECORD) return false;
   const int size=ArraySize(RecoverySessionIntents);
   if(ArrayResize(RecoverySessionIntents,size+1,64)!=size+1) return false;
   RecoverySessionIntents[size].key=key;
   RecoverySessionIntents[size].order=0;
   return true;
  }

bool RecoveryIntentRecordTicket(const RecoveryIntentKey &key,const ulong ticket)
  {
   string previous;
   if(ticket==0 || RecoveryIntentLookup(key,previous)!=RI_UNRESOLVED) return false;
   const int at=RecoveryIntentIndex(key.id);
   if(at<0) return false;
   RecoverySessionIntents[at].order=ticket;
   return true;
  }
#endif
