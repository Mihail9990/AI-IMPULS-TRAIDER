#ifndef RECOVERY_INTENT_JOURNAL_MQH
#define RECOVERY_INTENT_JOURNAL_MQH
#include "../verifier/RecoveryInventoryReader.mqh"

enum RecoveryIntentState
  {
   RI_NO_RECORD=0,   // Missing is not broker evidence or authorization to send.
   RI_UNRESOLVED=1,  // This identity was reserved; never send it again.
   RI_TICKET=2,      // Order ticket recorded; verify broker effect separately.
   RI_CONFLICT=3,   // Bad/conflicting files; preserve and investigate identity.
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

string RecoveryIntentLine(const RecoveryIntentKey &key)
  {
   return key.id+"\t"+(string)key.login+"\t"+key.server+"\t"+key.symbol+"\t"+
          (string)key.magic+"\t"+(string)key.generation+"\t"+key.fingerprint;
  }

string RecoveryIntentPath(const RecoveryIntentKey &key)
  {
   return "RECOVERY_INTENTS\\"+key.id;
  }

bool RecoveryIntentRead(const string path,string &wire)
  {
   wire="";
   int h=FileOpen(path,FILE_READ|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   ulong size=FileSize(h);
   if(size<1 || size>1024) { FileClose(h);return false; }
   uchar bytes[];
   uint count=FileReadArray(h,bytes,0,(int)size);
   FileClose(h);
   if(count!=(uint)size) return false;
   for(int i=0;i<(int)size;i++)
      if(bytes[i]<32 && bytes[i]!=9 && bytes[i]!=10) return false;
   wire=CharArrayToString(bytes,0,(int)size,CP_UTF8);
   uchar normalized[];
   if(StringToCharArray(wire,normalized,0,WHOLE_ARRAY,CP_UTF8)!=(int)size+1) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]!=normalized[i]) return false;
   return true;
  }

bool RecoveryIntentCreate(const RecoveryIntentKey &key,const string path,const string wire)
  {
   if(!RecoveryIntentContextOK(key) || FileIsExist(path)) return false;
   FolderCreate("RECOVERY_INTENTS");
   const string temporary=path+"."+(string)ChartID()+".writing";
   if(FileIsExist(temporary)) return false;
   uchar bytes[];
   int count=StringToCharArray(wire,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(count<1 || count>1024) return false;
   int h=FileOpen(temporary,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   uint written=FileWriteArray(h,bytes,0,count);
   FileFlush(h);
   FileClose(h);
   if(written!=(uint)count || !RecoveryIntentContextOK(key) || !FileMove(temporary,0,path,0))
     {
      FileDelete(temporary);
      return false;
     }
   string actual;
   return RecoveryIntentRead(path,actual) && actual==wire;
  }

// Reserve each request before the first possible dispatch. Returning true is
// not sufficient authorization by itself. On restart read/reconcile, not reserve.
bool RecoveryIntentReserve(const RecoveryIntentKey &key)
  {
   if(!RecoveryIntentContextOK(key)) return false;
   const string base=RecoveryIntentPath(key);
   if(FileIsExist(base+".ticket")) return false;
   return RecoveryIntentCreate(key,base+".intent","RECOVERY_INTENT_V1\t"+
                               RecoveryIntentLine(key)+"\tEND\n");
  }

RecoveryIntentState RecoveryIntentLookup(const RecoveryIntentKey &key,string &order_ticket)
  {
   order_ticket="";
   if(!RecoveryIntentKeyOK(key)) return RI_CONFLICT;
   if(TerminalInfoInteger(TERMINAL_CONNECTED)==0) return RI_UNAVAILABLE;
   if(!RecoveryIntentContextOK(key)) return RI_CONFLICT;
   const string base=RecoveryIntentPath(key);
   const bool has_reservation=FileIsExist(base+".intent");
   const bool has_ticket=FileIsExist(base+".ticket");
   if(!has_reservation) return has_ticket ? RI_CONFLICT : RI_NO_RECORD;
   string wire;
   if(!RecoveryIntentRead(base+".intent",wire) ||
      wire!="RECOVERY_INTENT_V1\t"+RecoveryIntentLine(key)+"\tEND\n") return RI_CONFLICT;
   if(!has_ticket) return RI_UNRESOLVED;
   if(!RecoveryIntentRead(base+".ticket",wire)) return RI_CONFLICT;
   const string prefix="RECOVERY_INTENT_TICKET_V1\t"+RecoveryIntentLine(key)+"\t";
   int start=StringLen(prefix), length=StringLen(wire)-start-5;
   if(length<1 || StringSubstr(wire,0,start)!=prefix ||
      StringSubstr(wire,start+length)!="\tEND\n") return RI_CONFLICT;
   string candidate=StringSubstr(wire,start,length);
   if(!RecoveryInvNatural(candidate,"18446744073709551615",true)) return RI_CONFLICT;
   order_ticket=candidate;
   return RI_TICKET;
  }

// Persist the exact ticket already associated with this request, never a match
// selected by price/magic. Missing ticket is unresolved, not a failed send.
bool RecoveryIntentRecordTicket(const RecoveryIntentKey &key,const ulong ticket)
  {
   string current;
   if(ticket==0 || RecoveryIntentLookup(key,current)!=RI_UNRESOLVED) return false;
   return RecoveryIntentCreate(key,RecoveryIntentPath(key)+".ticket","RECOVERY_INTENT_TICKET_V1\t"+
                     RecoveryIntentLine(key)+"\t"+(string)ticket+"\tEND\n");
  }
#endif
