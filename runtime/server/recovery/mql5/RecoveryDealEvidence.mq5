#property strict
#property version "0.10"
// READ ONLY: exports one explicitly selected historical exit; no trade API.
input long ExpectedLogin=0;
input string ExpectedServer="";
input string ExpectedSymbol="XAUUSD.s";
input ulong DealTicket=0;
input ulong PositionIdentifier=0;
input string RequestId=""; // 32 lowercase hexadecimal characters from caller
bool SafeText(string s) {
   return StringLen(s)>0 && StringFind(s,"\t")<0 && StringFind(s,"\r")<0 && StringFind(s,"\n")<0;
}
bool SessionOK() {
   return TerminalInfoInteger(TERMINAL_CONNECTED) && AccountInfoInteger(ACCOUNT_LOGIN)==ExpectedLogin && AccountInfoString(ACCOUNT_SERVER)==ExpectedServer;
}
int OnInit() {
   if(ExpectedLogin<=0 || DealTicket==0 || PositionIdentifier==0 || !SafeText(ExpectedServer) || !SafeText(ExpectedSymbol) || StringLen(RequestId)!=32) return INIT_PARAMETERS_INCORRECT;
   for(int i=0;i<32;i++) if(StringFind("0123456789abcdef",StringSubstr(RequestId,i,1))<0) return INIT_PARAMETERS_INCORRECT;
   EventSetTimer(1);
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer() {
   if(!SessionOK() || !HistoryDealSelect(DealTicket)) return;
   long order,position,entry,reason,type,time_msc;
   double sl,volume; string symbol;
   if(!HistoryDealGetInteger(DealTicket,DEAL_ORDER,order) || !HistoryDealGetInteger(DealTicket,DEAL_POSITION_ID,position) || !HistoryDealGetInteger(DealTicket,DEAL_ENTRY,entry) || !HistoryDealGetInteger(DealTicket,DEAL_REASON,reason) || !HistoryDealGetInteger(DealTicket,DEAL_TYPE,type) || !HistoryDealGetInteger(DealTicket,DEAL_TIME_MSC,time_msc) || !HistoryDealGetDouble(DealTicket,DEAL_SL,sl) || !HistoryDealGetDouble(DealTicket,DEAL_VOLUME,volume) || !HistoryDealGetString(DealTicket,DEAL_SYMBOL,symbol)) return;
   if((ulong)position!=PositionIdentifier || symbol!=ExpectedSymbol || entry!=DEAL_ENTRY_OUT || reason!=DEAL_REASON_SL || (type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL) || !MathIsValidNumber(sl) || sl<=0 || !MathIsValidNumber(volume) || volume<=0 || !SessionOK()) return;
   string name="recovery-evidence-"+RequestId;
   string payload="RECOVERY_DEAL_SL_V1\t"+RequestId+"\t"+(string)ExpectedLogin+"\t"+ExpectedServer+"\t"+(string)DealTicket+"\t"+(string)order+"\t"+(string)position+"\t"+symbol+"\t"+(type==DEAL_TYPE_BUY ? "BUY" : "SELL")+"\t"+(string)time_msc+"\t"+DoubleToString(volume,16)+"\t"+DoubleToString(sl,16)+"\tEND\n";
   int handle=FileOpen(name+".tmp",FILE_WRITE|FILE_TXT|FILE_ANSI,0,CP_UTF8);
   if(handle==INVALID_HANDLE) return;
   uint written=FileWriteString(handle,payload);
   FileFlush(handle); FileClose(handle);
   if(written==0 || !SessionOK()) { FileDelete(name+".tmp"); return; }
   // Consumer rejects truncation/identity mismatch. Rename atomicity is NOT assumed.
   if(FileMove(name+".tmp",0,name+".tsv",FILE_REWRITE)) EventKillTimer();
}
