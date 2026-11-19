#property strict
#property version "0.10"
// READ ONLY: serves bounded local requests for historical exits; no trade API.
input long ExpectedLogin=0;
input string ExpectedServer="";
input string ExpectedSymbol="XAUUSD.s";
ulong DealTicket=0;
ulong PositionIdentifier=0;
string RequestId=""; // 32 lowercase hexadecimal characters from caller
string ExpectedOrder="", ExpectedTime="", ExpectedVolume="", ExpectedSide="";
bool SafeText(string s) {
   return StringLen(s)>0 && StringFind(s,"\t")<0 && StringFind(s,"\r")<0 && StringFind(s,"\n")<0;
}
bool SessionOK() {
   return TerminalInfoInteger(TERMINAL_CONNECTED) && AccountInfoInteger(ACCOUNT_LOGIN)==ExpectedLogin && AccountInfoString(ACCOUNT_SERVER)==ExpectedServer;
}
int OnInit() {
   if(ExpectedLogin<=0 || !SafeText(ExpectedServer) || !SafeText(ExpectedSymbol)) return INIT_PARAMETERS_INCORRECT;
   FolderCreate("RECOVERY");
   EventSetTimer(1); return INIT_SUCCEEDED;
}
bool LoadRequest(string filename) {
   int h=FileOpen("RECOVERY\\"+filename,FILE_READ|FILE_TXT|FILE_ANSI,0,CP_UTF8);
   if(h==INVALID_HANDLE) return false;
   if(FileSize(h)>4096) { FileClose(h); return false; }
   string line=FileReadString(h); bool done=FileIsEnding(h); FileClose(h);
   string f[]; if(!done || StringSplit(line,9,f)!=12) return false;
   if(f[0]!="RECOVERY_REQUEST_V1" || f[11]!="END" || f[2]!=(string)ExpectedLogin || f[3]!=ExpectedServer || f[7]!=ExpectedSymbol) return false;
   if(StringLen(f[1])!=32 || filename!=f[1]+".request") return false;
   for(int i=0;i<32;i++) if(StringFind("0123456789abcdef",StringSubstr(f[1],i,1))<0) return false;
   long deal=StringToInteger(f[4]), position=StringToInteger(f[6]);
   if(deal<=0 || position<=0 || f[4]!=(string)deal || f[6]!=(string)position) return false;
   RequestId=f[1]; DealTicket=(ulong)deal; PositionIdentifier=(ulong)position;
   ExpectedOrder=f[5]; ExpectedTime=f[9]; ExpectedVolume=f[10]; ExpectedSide=f[8];
   return true;
}
// Persist the enumeration cursor across timer events. Every filename consumes budget,
// including invalid and completed records; unresolved records cannot monopolize it.
long SearchHandle=INVALID_HANDLE;
string SearchFilename="";
void CloseSearch() {
   if(SearchHandle!=INVALID_HANDLE) FileFindClose(SearchHandle);
   SearchHandle=INVALID_HANDLE;
   SearchFilename="";
}
void OnTimer() {
   if(!SessionOK()) { CloseSearch(); return; }
   if(SearchHandle==INVALID_HANDLE) {
      SearchHandle=FileFindFirst("RECOVERY\\*.request",SearchFilename);
      if(SearchHandle==INVALID_HANDLE) return;
   }
   for(int visited=0;visited<128;visited++) {
      if(LoadRequest(SearchFilename) && !FileIsExist("RECOVERY\\"+RequestId+".response")) ExportOne();
      if(!FileFindNext(SearchHandle,SearchFilename)) { CloseSearch(); return; }
   }
   // Next event starts with the already fetched next filename, not the first one.
}
void OnDeinit(const int reason) { EventKillTimer(); CloseSearch(); }
void ExportOne() {
   if(!SessionOK() || !HistoryDealSelect(DealTicket)) return;
   long order,position,entry,reason,type,time_msc;
   double sl,volume; string symbol;
   if(!HistoryDealGetInteger(DealTicket,DEAL_ORDER,order) || !HistoryDealGetInteger(DealTicket,DEAL_POSITION_ID,position) || !HistoryDealGetInteger(DealTicket,DEAL_ENTRY,entry) || !HistoryDealGetInteger(DealTicket,DEAL_REASON,reason) || !HistoryDealGetInteger(DealTicket,DEAL_TYPE,type) || !HistoryDealGetInteger(DealTicket,DEAL_TIME_MSC,time_msc) || !HistoryDealGetDouble(DealTicket,DEAL_SL,sl) || !HistoryDealGetDouble(DealTicket,DEAL_VOLUME,volume) || !HistoryDealGetString(DealTicket,DEAL_SYMBOL,symbol)) return;
   if((ulong)position!=PositionIdentifier || symbol!=ExpectedSymbol || entry!=DEAL_ENTRY_OUT || reason!=DEAL_REASON_SL || (type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL) || !MathIsValidNumber(sl) || sl<=0 || !MathIsValidNumber(volume) || volume<=0 || !SessionOK()) return;
   if((string)order!=ExpectedOrder || (string)time_msc!=ExpectedTime || DoubleToString(volume,16)!=ExpectedVolume || (type==DEAL_TYPE_BUY ? "BUY" : "SELL")!=ExpectedSide) return;
   string name="RECOVERY\\"+RequestId;
   string payload="RECOVERY_DEAL_SL_V1\t"+RequestId+"\t"+(string)ExpectedLogin+"\t"+ExpectedServer+"\t"+(string)DealTicket+"\t"+(string)order+"\t"+(string)position+"\t"+symbol+"\t"+(type==DEAL_TYPE_BUY ? "BUY" : "SELL")+"\t"+(string)time_msc+"\t"+DoubleToString(volume,16)+"\t"+DoubleToString(sl,16)+"\tEND\n";
   int handle=FileOpen(name+".tmp",FILE_WRITE|FILE_TXT|FILE_ANSI,0,CP_UTF8);
   if(handle==INVALID_HANDLE) return;
   uint written=FileWriteString(handle,payload);
   FileFlush(handle); FileClose(handle);
   if(written==0 || !SessionOK()) { FileDelete(name+".tmp"); return; }
   // Consumer rejects truncation/identity mismatch. Rename atomicity is NOT assumed.
   FileMove(name+".tmp",0,name+".response",FILE_REWRITE);
}
