#property strict
#property version "1.01"
#property description "Local TradFi command executor; strategy stays in Python. Trading disabled by default."
input string GatewayFolder = "AI_IMPULS_TRADFI";
input string TradeSymbol = "XAUUSD.s";
input ulong Magic = 25101101;
input bool AllowTrading = false;
input uint MaxDeviationPoints = 0;
int lock_handle = INVALID_HANDLE;
string keys[], vals[];

string Q(string s) {
   string out="\"",hex="0123456789abcdef";
   for(int i=0;i<StringLen(s);i++) {
      ushort c=StringGetCharacter(s,i);
      if(c==34) out+="\\\"";
      else if(c==92) out+="\\\\";
      else if(c<32) out+="\\u00"+StringSubstr(hex,c/16,1)+StringSubstr(hex,c%16,1);
      else out+=StringSubstr(s,i,1);
   }
   return out+"\"";
}
string N(double n) { return Q(DoubleToString(n,8)); }
string U(ulong n) { return Q((string)n); }
string Bool(bool b) { return b ? "true" : "false"; }
string Path(string p) { return GatewayFolder+"\\"+p; }
string Get(string k) {
   for(int i=0;i<ArraySize(keys);i++) if(keys[i]==k) return vals[i];
   return "";
}
bool ValidId(string s) {
   if(StringLen(s)!=24) return false;
   for(int i=0;i<24;i++) if(StringFind("0123456789abcdef",StringSubstr(s,i,1))<0) return false;
   return true;
}
bool WriteAtomic(string name,string body) {
   string tmp=name+".tmp";
   int h=FileOpen(Path(tmp),FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON,0,CP_UTF8);
   if(h==INVALID_HANDLE) return false;
   uint written=FileWriteString(h,body); FileFlush(h); FileClose(h);
   if(written==0) return false;
   return FileMove(Path(tmp),FILE_COMMON,Path(name),FILE_COMMON|FILE_REWRITE);
}
void Ack(string id,string status,string reason="",string extra="") {
   WriteAtomic("acks\\"+id+".json","{\"id\":"+Q(id)+",\"status\":"+Q(status)+
               ",\"reason\":"+Q(reason)+extra+"}");
}
bool Load(string name) {
   ArrayResize(keys,0); ArrayResize(vals,0);
   int h=FileOpen(Path("commands\\"+name),FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON,0,CP_UTF8);
   if(h==INVALID_HANDLE) return false;
   bool valid=true;
   while(!FileIsEnding(h)) {
      string line=FileReadString(h); int split=StringFind(line,"=");
      if(StringLen(line)==0) continue;
      if(split<=0 || ArraySize(keys)>=32) { valid=false; break; }
      string key=StringSubstr(line,0,split), value=StringSubstr(line,split+1);
      for(int j=0;j<ArraySize(keys);j++) if(keys[j]==key) valid=false;
      int n=ArraySize(keys); ArrayResize(keys,n+1); ArrayResize(vals,n+1);
      keys[n]=key; vals[n]=value;
   }
   FileClose(h); return valid;
}
bool Exact(double n,double step) {
   return MathIsValidNumber(n) && n>0 && step>0 && MathAbs(n/step-MathRound(n/step))<0.000001;
}
string StateName(long state) {
   if(state==ORDER_STATE_FILLED) return "FILLED";
   if(state==ORDER_STATE_CANCELED || state==ORDER_STATE_EXPIRED) return "CANCELLED";
   if(state==ORDER_STATE_REJECTED) return "REJECTED";
   return "PENDING";
}
string Side(long type) { return (type==ORDER_TYPE_BUY || type==ORDER_TYPE_BUY_STOP) ? "BUY" : "SELL"; }
void Add(string &array,string item) { if(StringLen(array)>1) array+=","; array+=item; }
string OrderJSON(ulong ticket,bool history) {
   long type=history ? HistoryOrderGetInteger(ticket,ORDER_TYPE) : OrderGetInteger(ORDER_TYPE);
   long magic=history ? HistoryOrderGetInteger(ticket,ORDER_MAGIC) : OrderGetInteger(ORDER_MAGIC);
   long state=history ? HistoryOrderGetInteger(ticket,ORDER_STATE) : OrderGetInteger(ORDER_STATE);
   string comment=history ? HistoryOrderGetString(ticket,ORDER_COMMENT) : OrderGetString(ORDER_COMMENT);
   return "{\"id\":"+U(ticket)+",\"command_id\":"+Q(comment)+",\"side\":"+Q(Side(type))+
          ",\"status\":"+Q(StateName(state))+",\"owned\":"+Bool((ulong)magic==Magic)+"}";
}
void Snapshot() {
   MqlTick tick; MqlRates bar[];
   if(!SymbolInfoTick(TradeSymbol,tick) || CopyRates(TradeSymbol,PERIOD_M1,1,1,bar)!=1) return;
   bool history_ok=HistorySelect(0,TimeCurrent());
   string orders="[", positions="[", history="[", deals="[";
   for(int i=0;i<OrdersTotal();i++) {
      ulong ticket=OrderGetTicket(i);
      if(ticket>0 && OrderGetString(ORDER_SYMBOL)==TradeSymbol) Add(orders,OrderJSON(ticket,false));
   }
   for(int i=0;i<PositionsTotal();i++) {
      ulong ticket=PositionGetTicket(i);
      if(ticket==0 || PositionGetString(POSITION_SYMBOL)!=TradeSymbol) continue;
      Add(positions,"{\"id\":"+U((ulong)PositionGetInteger(POSITION_IDENTIFIER))+
          ",\"ticket\":"+U(ticket)+",\"side\":"+Q(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? "BUY" : "SELL")+
          ",\"entry\":"+N(PositionGetDouble(POSITION_PRICE_OPEN))+",\"size\":"+N(PositionGetDouble(POSITION_VOLUME))+
          ",\"sl\":"+N(PositionGetDouble(POSITION_SL))+",\"tp\":"+N(PositionGetDouble(POSITION_TP))+
          ",\"owned\":"+Bool((ulong)PositionGetInteger(POSITION_MAGIC)==Magic)+"}");
   }
   if(history_ok) {
      for(int i=0;i<HistoryOrdersTotal();i++) {
         ulong ticket=HistoryOrderGetTicket(i);
         if(ticket>0 && HistoryOrderGetString(ticket,ORDER_SYMBOL)==TradeSymbol) Add(history,OrderJSON(ticket,true));
      }
      for(int i=0;i<HistoryDealsTotal();i++) {
         ulong ticket=HistoryDealGetTicket(i);
         if(ticket==0 || HistoryDealGetString(ticket,DEAL_SYMBOL)!=TradeSymbol) continue;
         long entry=HistoryDealGetInteger(ticket,DEAL_ENTRY), reason=HistoryDealGetInteger(ticket,DEAL_REASON);
         ulong order=(ulong)HistoryDealGetInteger(ticket,DEAL_ORDER);
         string entry_name=entry==DEAL_ENTRY_IN ? "IN" : (entry==DEAL_ENTRY_OUT ? "OUT" : "OTHER");
         string why=reason==DEAL_REASON_SL ? "SL" : (reason==DEAL_REASON_TP ? "TP" : "OTHER");
         Add(deals,"{\"id\":"+U(ticket)+",\"position_id\":"+U((ulong)HistoryDealGetInteger(ticket,DEAL_POSITION_ID))+
             ",\"order_id\":"+U(order)+",\"command_id\":"+Q(HistoryOrderGetString(order,ORDER_COMMENT))+
             ",\"entry\":"+Q(entry_name)+",\"reason\":"+Q(why)+",\"side\":"+
             Q(HistoryDealGetInteger(ticket,DEAL_TYPE)==DEAL_TYPE_BUY ? "BUY" : "SELL")+
             ",\"volume\":"+N(HistoryDealGetDouble(ticket,DEAL_VOLUME))+",\"price\":"+N(HistoryDealGetDouble(ticket,DEAL_PRICE))+
             ",\"profit\":"+N(HistoryDealGetDouble(ticket,DEAL_PROFIT))+",\"sl\":"+N(HistoryDealGetDouble(ticket,DEAL_SL))+
             ",\"tp\":"+N(HistoryDealGetDouble(ticket,DEAL_TP))+
             ",\"commission\":"+N(HistoryDealGetDouble(ticket,DEAL_COMMISSION))+",\"swap\":"+N(HistoryDealGetDouble(ticket,DEAL_SWAP))+
             ",\"time\":"+(string)HistoryDealGetInteger(ticket,DEAL_TIME_MSC)+"}");
      }
   }
   string spec="{\"symbol\":"+Q(TradeSymbol)+",\"currency\":"+Q(AccountInfoString(ACCOUNT_CURRENCY))+
      ",\"profit_currency\":"+Q(SymbolInfoString(TradeSymbol,SYMBOL_CURRENCY_PROFIT))+
      ",\"calc_mode\":"+Q((string)SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_CALC_MODE))+
      ",\"contract_size\":"+N(SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_CONTRACT_SIZE))+
      ",\"tick_size\":"+N(SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE))+
      ",\"tick_value_profit\":"+N(SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_VALUE_PROFIT))+
      ",\"tick_value_loss\":"+N(SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_VALUE_LOSS))+
      ",\"volume_min\":"+N(SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_MIN))+
      ",\"volume_step\":"+N(SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_STEP))+
      ",\"volume_max\":"+N(SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_MAX))+
      ",\"point\":"+N(SymbolInfoDouble(TradeSymbol,SYMBOL_POINT))+
      ",\"stops_level\":"+N((double)SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_STOPS_LEVEL))+
      ",\"freeze_level\":"+N((double)SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_FREEZE_LEVEL))+"}";
   long age=(long)TimeTradeServer()-(long)tick.time;
   if(age<0) age=0;
   string body="{\"protocol\":1,\"connected\":"+Bool((bool)TerminalInfoInteger(TERMINAL_CONNECTED))+
      ",\"account\":"+Q((string)AccountInfoInteger(ACCOUNT_LOGIN)+"@"+AccountInfoString(ACCOUNT_SERVER))+
      ",\"hedging\":"+Bool(AccountInfoInteger(ACCOUNT_MARGIN_MODE)==ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)+
      // datetime-to-string formats a calendar date. JSON timestamps must be integer seconds.
      ",\"history_complete\":"+Bool(history_ok)+",\"now\":"+IntegerToString((long)TimeGMT())+",\"spec\":"+spec+
      ",\"quote\":{\"bid\":"+N(tick.bid)+",\"ask\":"+N(tick.ask)+",\"utc_time\":"+IntegerToString((long)TimeGMT()-age)+"}"+
      ",\"candle\":{\"id\":"+IntegerToString((long)bar[0].time)+",\"close\":"+N(bar[0].close)+"}"+
      ",\"orders\":"+orders+"],\"positions\":"+positions+"],\"order_history\":"+history+"],\"deals\":"+deals+"]}";
   WriteAtomic("snapshot.json",body);
}
bool SelectIdentifier(ulong id) {
   for(int i=0;i<PositionsTotal();i++) {
      if(PositionGetTicket(i)>0 && (ulong)PositionGetInteger(POSITION_IDENTIFIER)==id &&
         PositionGetString(POSITION_SYMBOL)==TradeSymbol && (ulong)PositionGetInteger(POSITION_MAGIC)==Magic) return true;
   }
   return false;
}
void Execute(string file) {
   if(!Load(file)) return;
   string id=Get("id"),kind=Get("kind");
   if(!ValidId(id) || file!=id+".cmd") return;
   if(FileIsExist(Path("acks\\"+id+".json"),FILE_COMMON)) return;
   if(FileIsExist(Path("ledger\\"+id+".intent"),FILE_COMMON)) { Ack(id,"UNKNOWN","Durable intent exists; reconcile, never resend"); return; }
   if(Get("protocol")!="1" || Get("account")!=(string)AccountInfoInteger(ACCOUNT_LOGIN)+"@"+AccountInfoString(ACCOUNT_SERVER)) {
      Ack(id,"REJECTED","Protocol/account mismatch"); return;
   }
   if((long)TimeGMT()>StringToInteger(Get("expires"))) { Ack(id,"REJECTED","Expired unsent command"); return; }
   bool buy=Get("side")=="BUY";
   double size=StringToDouble(Get("size")),price=StringToDouble(Get("price")),sl=StringToDouble(Get("sl")),tp=StringToDouble(Get("tp"));
   if(kind=="PROFIT") {
      double profit=0;
      if((Get("side")!="BUY" && Get("side")!="SELL") || !OrderCalcProfit(buy?ORDER_TYPE_BUY:ORDER_TYPE_SELL,TradeSymbol,size,price,StringToDouble(Get("close")),profit))
         Ack(id,"REJECTED","OrderCalcProfit failed");
      else Ack(id,"DONE","",",\"profit\":"+N(profit));
      return;
   }
   if(!AllowTrading || !MQLInfoInteger(MQL_TRADE_ALLOWED) || !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !TerminalInfoInteger(TERMINAL_CONNECTED) || AccountInfoInteger(ACCOUNT_MARGIN_MODE)!=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING ||
      AccountInfoString(ACCOUNT_CURRENCY)!="USD") { Ack(id,"REJECTED","Trading disabled / disconnected / wrong account mode"); return; }
   MqlTradeRequest req={}; MqlTradeResult result={}; MqlTradeCheckResult check={}; MqlTick tick;
   if(!SymbolInfoTick(TradeSymbol,tick) || TimeTradeServer()-tick.time>5) { Ack(id,"REJECTED","Stale quote"); return; }
   req.symbol=TradeSymbol; req.magic=Magic; req.comment=id; req.deviation=MaxDeviationPoints;
   double step=SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE);
   double minstop=SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_STOPS_LEVEL)*SymbolInfoDouble(TradeSymbol,SYMBOL_POINT);
   if(kind=="STOP" || kind=="MARKET") {
      if((Get("side")!="BUY" && Get("side")!="SELL") || !Exact(size,SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_STEP)) ||
         size<SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_MIN) || size>SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_MAX) ||
         !Exact(price,step) || !Exact(sl,step) || !Exact(tp,step)) { Ack(id,"REJECTED","Invalid exact price/volume"); return; }
      double anchor=kind=="MARKET" ? (buy?tick.bid:tick.ask) : price;
      if((buy && (sl>=anchor || tp<=anchor || anchor-sl<minstop || tp-anchor<minstop)) ||
         (!buy && (sl<=anchor || tp>=anchor || sl-anchor<minstop || anchor-tp<minstop))) { Ack(id,"REJECTED","Invalid built-in protection"); return; }
      if(kind=="STOP" && ((buy && tick.ask>=price) || (!buy && tick.bid<=price))) { Ack(id,"REJECTED","CROSSED"); return; }
      double trigger=StringToDouble(Get("trigger"));
      if(kind=="MARKET" && (!Exact(trigger,step) || (buy && tick.ask<trigger) || (!buy && tick.bid>trigger))) {
         Ack(id,"REJECTED","STOP no longer crossed; MARKET not sent"); return;
      }
      req.action=kind=="STOP" ? TRADE_ACTION_PENDING : TRADE_ACTION_DEAL;
      req.type=kind=="STOP" ? (buy?ORDER_TYPE_BUY_STOP:ORDER_TYPE_SELL_STOP) : (buy?ORDER_TYPE_BUY:ORDER_TYPE_SELL);
      req.volume=size; req.price=kind=="STOP" ? price : (buy?tick.ask:tick.bid); req.sl=sl; req.tp=tp;
      req.type_time=ORDER_TIME_GTC;
      long filling=SymbolInfoInteger(TradeSymbol,SYMBOL_FILLING_MODE);
      if(kind=="STOP") req.type_filling=ORDER_FILLING_RETURN;
      else if((filling & SYMBOL_FILLING_FOK)!=0) req.type_filling=ORDER_FILLING_FOK;
      else { Ack(id,"REJECTED","FOK required; partial MARKET execution is not supported"); return; }
   } else if(kind=="CANCEL") {
      ulong ticket=(ulong)StringToInteger(Get("order_id"));
      if(!OrderSelect(ticket) || OrderGetString(ORDER_SYMBOL)!=TradeSymbol || (ulong)OrderGetInteger(ORDER_MAGIC)!=Magic) {
         Ack(id,"UNKNOWN","Order absent; inspect history for cancel/fill race"); return;
      }
      req.action=TRADE_ACTION_REMOVE; req.order=ticket;
   } else if(kind=="MODIFY") {
      if(!SelectIdentifier((ulong)StringToInteger(Get("position_id")))) { Ack(id,"UNKNOWN","Position absent; inspect deals"); return; }
      buy=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY;
      double oldsl=PositionGetDouble(POSITION_SL);
      double anchor=buy?tick.bid:tick.ask;
      double freeze=SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_FREEZE_LEVEL)*SymbolInfoDouble(TradeSymbol,SYMBOL_POINT);
      double distance=MathMax(freeze,minstop);
      bool stepping=Get("purpose")=="STEP";
      if(Get("purpose")!="ANCHOR" && !stepping) { Ack(id,"REJECTED","Unknown modification purpose"); return; }
      if(!Exact(sl,step) || !Exact(tp,step) || (buy && ((stepping && sl<oldsl) || anchor-sl<distance || tp-anchor<distance)) ||
         (!buy && ((stepping && sl>oldsl) || sl-anchor<distance || anchor-tp<distance))) { Ack(id,"REJECTED","SL cannot worsen on STEP; stop/freeze constraint"); return; }
      req.action=TRADE_ACTION_SLTP; req.position=(ulong)PositionGetInteger(POSITION_TICKET); req.sl=sl; req.tp=tp;
   } else { Ack(id,"REJECTED","Unsupported command"); return; }
   if(!OrderCheck(req,check)) { Ack(id,"REJECTED","OrderCheck: "+check.comment); return; }
   // Durable may-send marker MUST be flushed before OrderSend. Failure means no mutation.
   if(!WriteAtomic("ledger\\"+id+".intent",id)) return;
   bool accepted=OrderSend(req,result);
   string extra=",\"order_id\":"+U(result.order)+",\"deal_id\":"+U(result.deal)+",\"retcode\":"+(string)result.retcode;
   if(accepted && (result.retcode==TRADE_RETCODE_DONE || result.retcode==TRADE_RETCODE_PLACED))
      Ack(id,(kind=="MODIFY" || kind=="CANCEL") && result.retcode==TRADE_RETCODE_DONE ? "DONE" : "ACCEPTED",result.comment,extra);
   else {
      // Only explicit terminal rejection codes are safe. Timeout/connection/partial/unknown stay UNKNOWN.
      bool rejected=result.retcode==TRADE_RETCODE_REJECT || result.retcode==TRADE_RETCODE_INVALID ||
         result.retcode==TRADE_RETCODE_INVALID_VOLUME || result.retcode==TRADE_RETCODE_INVALID_PRICE ||
         result.retcode==TRADE_RETCODE_INVALID_STOPS || result.retcode==TRADE_RETCODE_NO_MONEY ||
         result.retcode==TRADE_RETCODE_MARKET_CLOSED || result.retcode==TRADE_RETCODE_TRADE_DISABLED;
      Ack(id,rejected?"REJECTED":"UNKNOWN",result.comment,extra);
   }
}
int OnInit() {
   if(TradeSymbol!="XAUUSD.s" || StringFind(GatewayFolder,"..")>=0 || StringFind(GatewayFolder,"\\")>=0 || StringFind(GatewayFolder,"/")>=0) return INIT_PARAMETERS_INCORRECT;
   FolderCreate(GatewayFolder,FILE_COMMON); FolderCreate(Path("commands"),FILE_COMMON);
   FolderCreate(Path("acks"),FILE_COMMON); FolderCreate(Path("ledger"),FILE_COMMON);
   lock_handle=FileOpen(Path("gateway.lock"),FILE_READ|FILE_WRITE|FILE_BIN|FILE_COMMON);
   if(lock_handle==INVALID_HANDLE) return INIT_FAILED;
   if(!SymbolSelect(TradeSymbol,true) || SymbolInfoInteger(TradeSymbol,SYMBOL_DIGITS)>8) return INIT_FAILED;
   EventSetMillisecondTimer(100); return INIT_SUCCEEDED;
}
void OnDeinit(const int reason) { EventKillTimer(); if(lock_handle!=INVALID_HANDLE) FileClose(lock_handle); }
void OnTimer() {
   Snapshot();
   string name; long search=FileFindFirst(Path("commands\\*.cmd"),name,FILE_COMMON);
   if(search!=INVALID_HANDLE) {
      do {
         Execute(name);
         string id=StringSubstr(name,0,24);
         if(ValidId(id) && FileIsExist(Path("acks\\"+id+".json"),FILE_COMMON))
            FileDelete(Path("commands\\"+name),FILE_COMMON);
      } while(FileFindNext(search,name));
      FileFindClose(search);
   }
   Snapshot();
}
