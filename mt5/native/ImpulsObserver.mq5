#property strict
#property version "0.10"
#property description "Native migration candidate: read-only position-linked history observer."
#include "NativeWire.mqh"
input string MailboxFolder="AI_IMPULS_NATIVE_V1";
input long ExpectedLogin=0;
input string ExpectedServer="";
input string TradeSymbol="XAUUSD.s";
input string PositionIdentifiers=""; // explicit comma-separated POSITION_IDENTIFIERs; no automatic ownership
ulong watched[];
int cursor=0,owner=INVALID_HANDLE;
ulong sequence=0;
bool dirty=true;
string observer_cycle="";

string OpenSignature(ulong id) {
   string signature="";
   for(int i=0;i<PositionsTotal();i++) {
      ulong ticket=PositionGetTicket(i);
      if(ticket==0 || (ulong)PositionGetInteger(POSITION_IDENTIFIER)!=id) continue;
      if(PositionGetString(POSITION_SYMBOL)!=TradeSymbol) return "MISMATCH";
      signature=(string)ticket+":"+DoubleToString(PositionGetDouble(POSITION_VOLUME),8)+":"+
                DoubleToString(PositionGetDouble(POSITION_SL),10)+":"+
                DoubleToString(PositionGetDouble(POSITION_TP),10);
   }
   return signature;
}
void Observe(ulong id) {
   sequence++;
   string prefix="{\"protocol\":\"native_observation_v1\",\"login\":"+NativeU((ulong)ExpectedLogin)+
      ",\"server\":"+NativeQ(ExpectedServer)+",\"symbol\":"+NativeQ(TradeSymbol)+
      ",\"position_id\":"+NativeU(id)+",\"sequence\":"+NativeU(sequence)+
      ",\"terminal_local_time\":"+(string)TimeLocal()+",\"broker_time\":"+(string)TimeCurrent();
   string status="OBSERVED",rows="[",proof_rows="",before=OpenSignature(id);
   int proof_count=0;
   double entered=0,exited=0,net=0;
   bool connected=(bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   if(!NativeIdentity(ExpectedLogin,ExpectedServer)) status="IDENTITY_CHANGED";
   else if(!connected) status="DISCONNECTED";
   else if(!HistorySelectByPosition(id)) status="HISTORY_UNAVAILABLE";
   else {
      int total=HistoryDealsTotal();
      if(total>10000) status="HISTORY_TOO_LARGE"; // explicit bounded record; no truncation labelled complete
      else for(int i=0;i<total;i++) {
         ulong deal=HistoryDealGetTicket(i);
         if(deal==0 || (ulong)HistoryDealGetInteger(deal,DEAL_POSITION_ID)!=id ||
            HistoryDealGetString(deal,DEAL_SYMBOL)!=TradeSymbol) { status="HISTORY_IDENTITY_MISMATCH";break; }
         long entry=HistoryDealGetInteger(deal,DEAL_ENTRY),type=HistoryDealGetInteger(deal,DEAL_TYPE);
         double volume=HistoryDealGetDouble(deal,DEAL_VOLUME);
         double profit=HistoryDealGetDouble(deal,DEAL_PROFIT),commission=HistoryDealGetDouble(deal,DEAL_COMMISSION);
         double swap=HistoryDealGetDouble(deal,DEAL_SWAP),fee=HistoryDealGetDouble(deal,DEAL_FEE);
         if(!MathIsValidNumber(profit+commission+swap+fee+volume)) { status="INVALID_HISTORY";break; }
         net+=profit+commission+swap+fee;
         if(type==DEAL_TYPE_BUY || type==DEAL_TYPE_SELL) {
            if(entry==DEAL_ENTRY_IN) entered+=volume;
            else if(entry==DEAL_ENTRY_OUT) exited+=volume;
            else { status="UNSUPPORTED_REVERSAL_OR_CLOSE_BY";break; }
         }
         proof_rows+=(string)deal+"|"+(string)HistoryDealGetInteger(deal,DEAL_ORDER)+"|"+(string)entry+"|"+(string)type+"|"+(string)HistoryDealGetInteger(deal,DEAL_REASON)+"|"+(string)HistoryDealGetInteger(deal,DEAL_MAGIC)+"|"+DoubleToString(volume,10)+"|"+DoubleToString(profit,10)+"|"+DoubleToString(commission,10)+"|"+DoubleToString(swap,10)+"|"+DoubleToString(fee,10)+"|"+DoubleToString(HistoryDealGetDouble(deal,DEAL_SL),10)+"\n";proof_count++;
         if(i>0)rows+=",";
         rows+="{\"ticket\":"+NativeU(deal)+",\"order\":"+NativeU((ulong)HistoryDealGetInteger(deal,DEAL_ORDER))+
            ",\"position_id\":"+NativeU(id)+",\"entry\":"+(string)entry+",\"type\":"+(string)type+
            ",\"reason\":"+(string)HistoryDealGetInteger(deal,DEAL_REASON)+
            ",\"magic\":"+NativeU((ulong)HistoryDealGetInteger(deal,DEAL_MAGIC))+
            ",\"time_msc\":"+NativeU((ulong)HistoryDealGetInteger(deal,DEAL_TIME_MSC))+
            ",\"volume\":"+NativeN(volume)+",\"price\":"+NativeN(HistoryDealGetDouble(deal,DEAL_PRICE))+
            ",\"profit\":"+NativeN(profit)+",\"commission\":"+NativeN(commission)+
            ",\"swap\":"+NativeN(swap)+",\"fee\":"+NativeN(fee)+"}";
      }
   }
   string after=OpenSignature(id);
   if(status=="OBSERVED" && (before!=after || before=="MISMATCH"))status="CHANGED_DURING_SCAN";
   // Balanced volume is evidence, NOT a completeness/ownership claim.
   // The trader must independently match its entry order and command journal.
   bool balanced=status=="OBSERVED" && entered>0 && MathAbs(entered-exited)<1e-8 && after=="";
   if(status!="OBSERVED") {rows="[";proof_rows="";proof_count=0;entered=0;exited=0;net=0;}
   string body=prefix+",\"status\":"+NativeQ(status)+",\"open_signature\":"+NativeQ(after)+
      ",\"balanced_closed_volume\":"+(balanced?"true":"false")+
      ",\"history_complete\":false,\"ownership_verified\":false,\"entered\":"+NativeN(entered)+
      ",\"exited\":"+NativeN(exited)+",\"net\":"+NativeN(net)+",\"deals\":"+rows+"]}";
   string proof="NATIVE_OBS1|"+(string)ExpectedLogin+"|"+ExpectedServer+"|"+TradeSymbol+"|"+(string)id+"|"+status+"|"+(string)TimeLocal()+"|"+(string)proof_count+"\n"+proof_rows+"END";
   NativePublish(MailboxFolder,"position-"+(string)id+".proof",proof);
   if(!NativePublish(MailboxFolder,"position-"+(string)id+".json",body)) Print("Observer publication failed; old file is not fresh evidence");
}
int OnInit() {
   if(!NativeFolder(MailboxFolder) || !NativeIdentity(ExpectedLogin,ExpectedServer) || TradeSymbol=="")return INIT_PARAMETERS_INCORRECT;
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE)!=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) return INIT_PARAMETERS_INCORRECT;
   string ids[];int n=PositionIdentifiers==""?0:StringSplit(PositionIdentifiers,',',ids);
   if(n>128)return INIT_PARAMETERS_INCORRECT;
   ArrayResize(watched,n);
   for(int i=0;i<n;i++) {
      if(!NativeId(ids[i]))return INIT_PARAMETERS_INCORRECT;
      watched[i]=(ulong)StringToInteger(ids[i]);
      for(int j=0;j<i;j++)if(watched[i]==watched[j])return INIT_PARAMETERS_INCORRECT;
   }
   FolderCreate(MailboxFolder,FILE_COMMON);
   owner=FileOpen(MailboxFolder+"\\observer.lock",FILE_READ|FILE_WRITE|FILE_BIN|FILE_COMMON);
   if(owner==INVALID_HANDLE)return INIT_FAILED;
   if(!EventSetMillisecondTimer(250)) {FileClose(owner);owner=INVALID_HANDLE;return INIT_FAILED;}
   return INIT_SUCCEEDED;
}
void OnTradeTransaction(const MqlTradeTransaction &trans,const MqlTradeRequest &request,const MqlTradeResult &result) {
   dirty=true; // No history scans, IO or trading here. Event order/count is not a ledger.
}
void OnTimer() {
   // Periodic sweep is unconditional: lost/reordered/coalesced events cannot
   // permanently suppress reconciliation. One position per timer callback.
   if(PositionIdentifiers=="" && !NativeManifest(MailboxFolder,ExpectedLogin,ExpectedServer,TradeSymbol,watched,observer_cycle))return;
   if(ArraySize(watched)==0)return;
   cursor=cursor%ArraySize(watched);
   Observe(watched[cursor]);cursor=(cursor+1)%ArraySize(watched);
   if(cursor==0)dirty=false;
}
void OnDeinit(const int reason) { EventKillTimer();if(owner!=INVALID_HANDLE)FileClose(owner); }
