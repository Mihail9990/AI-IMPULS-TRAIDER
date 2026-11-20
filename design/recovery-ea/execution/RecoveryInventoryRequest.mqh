#ifndef RECOVERY_INVENTORY_REQUEST_MQH
#define RECOVERY_INVENTORY_REQUEST_MQH

// Request-only terminal-local side of verifier/RecoveryVerifier.mq5.
// Caller generates a fresh nonce and maintains command/account ownership.
bool RecoveryInventoryHex(const string value)
  {
   if(StringLen(value)!=32) return false;
   for(int i=0;i<32;i++)
      if(StringFind("0123456789abcdef",StringSubstr(value,i,1))<0) return false;
   return true;
  }

bool RecoveryInventorySafeField(const string value)
  {
   if(StringLen(value)<1 || StringLen(value)>100) return false;
   for(int i=0;i<StringLen(value);i++)
     {
      ushort c=StringGetCharacter(value,i);
      if(c<32 || c==127 || c==0xfffd) return false;
     }
   return true;
  }

bool RecoveryRequestInventory(const string nonce,const string executor_epoch,
                              const string verifier_epoch,const long generation,
                              const long login,const string server,
                              const string symbol,const long magic)
  {
   if(!RecoveryInventoryHex(nonce) || !RecoveryInventoryHex(executor_epoch) ||
      !RecoveryInventoryHex(verifier_epoch) || executor_epoch==verifier_epoch ||
      generation<0 || login<=0 || magic<=0 || !RecoveryInventorySafeField(server) ||
      !RecoveryInventorySafeField(symbol) || _Symbol!=symbol)
      return false;
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) ||
      AccountInfoInteger(ACCOUNT_LOGIN)!=login || AccountInfoString(ACCOUNT_SERVER)!=server)
      return false;

   const string base="RECOVERY_VERIFY\\"+nonce;
   const string request_file=base+".inv";
   if(FileIsExist(request_file) || FileIsExist(base+".snapshot")) return false;
   FolderCreate("RECOVERY_VERIFY");

   const string temporary=base+"."+executor_epoch+"."+(string)ChartID()+".writing";
   if(FileIsExist(temporary)) return false;
   const string line="RECOVERY_INVENTORY_REQUEST_V1\t"+nonce+"\t"+executor_epoch+"\t"+
      verifier_epoch+"\t"+(string)generation+"\t"+(string)login+"\t"+
      server+"\t"+symbol+"\t"+(string)magic+"\tEND\n";
   uchar bytes[];
   int count=StringToCharArray(line,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(count<=0 || count>2048) return false;
   int handle=FileOpen(temporary,FILE_WRITE|FILE_BIN);
   if(handle==INVALID_HANDLE) return false;
   uint written=FileWriteArray(handle,bytes,0,count);
   FileFlush(handle);
   FileClose(handle);

   if(written!=(uint)count || !TerminalInfoInteger(TERMINAL_CONNECTED) ||
      AccountInfoInteger(ACCOUNT_LOGIN)!=login || AccountInfoString(ACCOUNT_SERVER)!=server ||
      !FileMove(temporary,0,request_file,0))
     {
      FileDelete(temporary);
      return false;
     }
   return true;
  }
#endif
