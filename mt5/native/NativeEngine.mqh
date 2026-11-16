#ifndef IMPULS_NATIVE_ENGINE
#define IMPULS_NATIVE_ENGINE
#include "NativeWire.mqh"
#include "RecoveryPolicy.mqh"
// One record per broker request. UNKNOWN is persisted BEFORE OrderSend.
// No timeout changes UNKNOWN into permission to send again.
struct NativeCommand {
 string id,kind,status,control;
 ulong basket,position,order,ticket;
 int stage,side; // side +1 BUY / -1 SELL
 double volume,price,sl,tp,oldsl,oldtp,originalsl,net,filled;
 bool support,closed;
 int exit_kind; // 1 original SL, 2 support SL, 3 TP, 4 mixed/manual
};
NativeCommand commands[];
bool native_tick_released[];
int native_dispatch_cursor=0;
string native_saved_rows[];
string native_reason="",native_profiles[11];
bool native_split_two=false;
bool native_paused=true,native_healthy=true,native_reverse=false;
ulong native_serial=0,native_revision=0,native_basket=1,native_applied=0;
ulong native_fixed_source=0,native_fixed_target=0;
int native_fixed_steps=0,native_fixed_side=0;double native_fixed_price=0;
int native_steps=2,native_cursor=0;
bool NativeInitial(){return native_steps==2 && !native_split_two;}
datetime native_bar=0,native_accounting_start=0;
double native_carry=0,native_sl=0,native_tp=0,native_offset=0,native_recovery_sl=0,native_recovery_offset=0;
double native_sl_first=.25,native_sl_second=.40,native_sl_first_offset=-.25,native_sl_second_offset=0,native_credit=0;
int native_next_steps=0;
string native_control="",native_close_all="",native_cancel_control="";

string NativeDecimal(double v){return DoubleToString(v,10);}
string NativeStatePath(){return MailboxFolder+"\\native-state.txt";}
string ReadNativeFile(string relative) {
 int h=FileOpen(MailboxFolder+"\\"+relative,FILE_READ|FILE_BIN|FILE_COMMON|FILE_SHARE_READ);
 if(h==INVALID_HANDLE)return "";
 ulong size=FileSize(h);if(size==0 || size>8000000){FileClose(h);return "";}
 uchar bytes[];ArrayResize(bytes,(int)size);
 uint n=FileReadArray(h,bytes,0,(int)size);FileClose(h);
 if(n!=size)return "";return CharArrayToString(bytes,0,(int)n,CP_UTF8);
}
bool SaveNative() {
 if(!native_healthy)return false;
 native_revision++;
 string out="NATIVE3|"+(string)ExpectedLogin+"|"+ExpectedServer+"|"+TradeSymbol+"|"+(string)Magic+"|"+InstallationId+"\n";
 out+=(string)native_serial+"|"+(string)native_revision+"|"+(string)native_basket+"|"+(string)native_applied+"|"+(string)native_steps+"|"+(string)(int)native_paused+"|"+(string)(int)native_reverse+"|"+NativeDecimal(native_carry)+"|"+NativeDecimal(native_sl)+"|"+NativeDecimal(native_tp)+"|"+NativeDecimal(native_offset)+"|"+NativeDecimal(native_recovery_sl)+"|"+(string)native_bar+"|"+native_reason+"|"+native_close_all+"|"+native_cancel_control+"|"+NativeDecimal(native_recovery_offset)+"|"+(string)(int)native_split_two+"|"+native_profiles[2]+"|"+(string)native_fixed_source+"|"+(string)native_fixed_target+"|"+(string)native_fixed_steps+"|"+NativeDecimal(native_fixed_price)+"|"+(string)native_fixed_side+"|"+(string)native_accounting_start+"|"+NativeDecimal(native_sl_first)+"|"+NativeDecimal(native_sl_second)+"|"+NativeDecimal(native_sl_first_offset)+"|"+NativeDecimal(native_sl_second_offset)+"|"+NativeDecimal(native_credit)+"|"+(string)native_next_steps+"\n";
 for(int n=3;n<=10;n++)out+=native_profiles[n]+"\n";
 string audit_prefix="NATIVE_AUDIT1\n"+out,changed_rows="";int changed_count=0;
 string next_rows[];ArrayResize(next_rows,ArraySize(commands));
 out+=(string)ArraySize(commands)+"\n";
 for(int i=0;i<ArraySize(commands);i++) {
  NativeCommand c=commands[i];
  string row=c.id+"|"+c.kind+"|"+c.status+"|"+c.control+"|"+(string)c.basket+"|"+(string)c.position+"|"+(string)c.order+"|"+(string)c.ticket+"|"+(string)c.stage+"|"+(string)c.side+"|"+NativeDecimal(c.volume)+"|"+NativeDecimal(c.price)+"|"+NativeDecimal(c.sl)+"|"+NativeDecimal(c.tp)+"|"+NativeDecimal(c.oldsl)+"|"+NativeDecimal(c.oldtp)+"|"+NativeDecimal(c.originalsl)+"|"+NativeDecimal(c.net)+"|"+NativeDecimal(c.filled)+"|"+(string)(int)c.support+"|"+(string)(int)c.closed+"|"+(string)c.exit_kind;
  next_rows[i]=row;out+=row+"\n";
  if(i>=ArraySize(native_saved_rows) || row!=native_saved_rows[i]){changed_rows+=row+"\n";changed_count++;}
 }
 out+="END|"+(string)native_revision;
 // Retain prior committed snapshots as an append-only local audit.
 string audit="native_audit\\"+(string)native_revision+".state";
 if(FileIsExist(MailboxFolder+"\\"+audit,FILE_COMMON) || !NativePublish(MailboxFolder,audit,audit_prefix+(string)changed_count+"\n"+changed_rows+"END|"+(string)native_revision)){native_healthy=false;native_paused=true;return false;}
 if(!NativePublish(MailboxFolder,"native-state.txt",out)){native_healthy=false;native_paused=true;native_reason="STATE_WRITE_FAILED";return false;}
 ArrayResize(native_saved_rows,ArraySize(next_rows));for(int i=0;i<ArraySize(next_rows);i++)native_saved_rows[i]=next_rows[i];
 return true;
}
bool LoadNative() {
 ArrayResize(native_tick_released,0);
 string body=ReadNativeFile("native-state.txt"),lines[],v[];
 int count=StringSplit(body,'\n',lines);if(count<12)return false;
 if(lines[0]!="NATIVE3|"+(string)ExpectedLogin+"|"+ExpectedServer+"|"+TradeSymbol+"|"+(string)Magic+"|"+InstallationId)return false;
 int fields=StringSplit(lines[1],'|',v);if(fields!=17 && fields!=19 && fields!=24 && fields!=25 && fields!=31)return false;
 native_split_two=fields>=19 && StringToInteger(v[17])==1;native_profiles[2]=fields>=19?v[18]:"0.30,0.70";
 native_fixed_source=fields>=24?(ulong)StringToInteger(v[19]):0;native_fixed_target=fields>=24?(ulong)StringToInteger(v[20]):0;native_fixed_steps=fields>=24?(int)StringToInteger(v[21]):0;native_fixed_price=fields>=24?StringToDouble(v[22]):0;native_fixed_side=fields>=24?(int)StringToInteger(v[23]):0;
 native_accounting_start=fields>=25?(datetime)StringToInteger(v[24]):0;
 native_sl_first=fields==31?StringToDouble(v[25]):.25;native_sl_second=fields==31?StringToDouble(v[26]):.40;native_sl_first_offset=fields==31?StringToDouble(v[27]):-0.25;native_sl_second_offset=fields==31?StringToDouble(v[28]):0;native_credit=fields==31?StringToDouble(v[29]):0;native_next_steps=fields==31?(int)StringToInteger(v[30]):0;
 native_serial=(ulong)StringToInteger(v[0]);native_revision=(ulong)StringToInteger(v[1]);native_basket=(ulong)StringToInteger(v[2]);native_applied=(ulong)StringToInteger(v[3]);native_steps=(int)StringToInteger(v[4]);
 native_paused=(bool)StringToInteger(v[5]);native_reverse=(bool)StringToInteger(v[6]);native_carry=StringToDouble(v[7]);native_sl=StringToDouble(v[8]);native_tp=StringToDouble(v[9]);native_offset=StringToDouble(v[10]);native_recovery_sl=StringToDouble(v[11]);native_bar=(datetime)StringToInteger(v[12]);native_reason=v[13];native_close_all=v[14];native_cancel_control=v[15];native_recovery_offset=StringToDouble(v[16]);
 if(!PolicyPositive(native_sl_first) || !PolicyPositive(native_sl_second) || native_sl_second<=native_sl_first || !MathIsValidNumber(native_sl_first_offset) || !MathIsValidNumber(native_sl_second_offset) || native_sl_second_offset<native_sl_first_offset || !PolicyNonnegative(native_credit) || (native_next_steps!=0 && (native_next_steps<2 || native_next_steps>10)))return false;
 if(native_fixed_target>0 && (native_fixed_source==0 || native_fixed_target!=native_fixed_source+1 || (native_basket!=native_fixed_source && native_basket!=native_fixed_target) || native_fixed_steps<2 || native_fixed_steps>10 || !PolicyPositive(native_fixed_price) || (native_fixed_side!=1 && native_fixed_side!=-1)))return false;
 if(native_steps<2 || native_steps>10 || !PolicyNonnegative(native_carry) || !PolicyPositive(native_sl) || !PolicyPositive(native_tp) || !PolicyPositive(native_offset) || !PolicyPositive(native_recovery_sl) || !PolicyPositive(native_recovery_offset))return false;
 for(int n=3;n<=10;n++)native_profiles[n]=lines[n-1];
 int size=(int)StringToInteger(lines[10]);
 if(size<0 || size>10000 || count!=size+12 || lines[count-1]!="END|"+(string)native_revision)return false;
 ArrayResize(commands,size);ArrayResize(native_saved_rows,size);
 for(int i=0;i<size;i++) {
  if(StringSplit(lines[i+11],'|',v)!=22)return false;native_saved_rows[i]=lines[i+11];
  commands[i].id=v[0];commands[i].kind=v[1];commands[i].status=v[2];commands[i].control=v[3];
  commands[i].basket=(ulong)StringToInteger(v[4]);commands[i].position=(ulong)StringToInteger(v[5]);commands[i].order=(ulong)StringToInteger(v[6]);commands[i].ticket=(ulong)StringToInteger(v[7]);commands[i].stage=(int)StringToInteger(v[8]);commands[i].side=(int)StringToInteger(v[9]);
  commands[i].volume=StringToDouble(v[10]);commands[i].price=StringToDouble(v[11]);commands[i].sl=StringToDouble(v[12]);commands[i].tp=StringToDouble(v[13]);commands[i].oldsl=StringToDouble(v[14]);commands[i].oldtp=StringToDouble(v[15]);commands[i].originalsl=StringToDouble(v[16]);commands[i].net=StringToDouble(v[17]);commands[i].filled=StringToDouble(v[18]);commands[i].support=(bool)StringToInteger(v[19]);commands[i].closed=(bool)StringToInteger(v[20]);commands[i].exit_kind=(int)StringToInteger(v[21]);
  if(commands[i].id=="" || (commands[i].side!=1 && commands[i].side!=-1) || !MathIsValidNumber(commands[i].net))return false;
  for(int j=0;j<i;j++)if(commands[j].id==commands[i].id)return false;
 }
 // No automatic resumed entry on restart. Existing obligations are reconciled.
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].status=="READY")commands[i].status="REJECTED";
 native_paused=true;return true;
}
bool SelectNativePosition(ulong pid) {
 for(int i=0;i<PositionsTotal();i++)
  if(PositionGetTicket(i)>0 && (ulong)PositionGetInteger(POSITION_IDENTIFIER)==pid)
   return PositionGetString(POSITION_SYMBOL)==TradeSymbol && (ulong)PositionGetInteger(POSITION_MAGIC)==Magic;
 return false;
}
bool UnknownResource(ulong pid,ulong order=0) {
 for(int i=0;i<ArraySize(commands);i++)if((commands[i].status=="UNKNOWN" || commands[i].status=="RECONCILING" || commands[i].status=="WAIT_TICK" || commands[i].status=="READY") &&
   ((pid>0 && commands[i].position==pid) || (order>0 && commands[i].order==order)))return true;
 return false;
}
bool AnyUnknown() {for(int i=0;i<ArraySize(commands);i++)if(commands[i].status=="UNKNOWN" || commands[i].status=="RECONCILING" || commands[i].status=="PARTIAL" || commands[i].status=="WAIT_TICK" || commands[i].status=="READY")return true;return false;}
long native_tick_stamp=0;double native_tick_bid=0,native_tick_ask=0;ulong native_tick_seen=0;bool native_tick_changed=false,native_tick_event=false;
void RefreshNativeQuote() {
 bool observed_event=native_tick_event;native_tick_event=false;native_tick_changed=false;
 if(!TerminalInfoInteger(TERMINAL_CONNECTED)){native_tick_changed=false;ArrayResize(native_tick_released,0);return;}
 MqlTick q;if(!SymbolInfoTick(TradeSymbol,q) || q.time_msc<=0 || !PolicyPositive(q.bid) || !PolicyPositive(q.ask) || q.ask<q.bid)return;
 if(q.time_msc!=native_tick_stamp || q.bid!=native_tick_bid || q.ask!=native_tick_ask) {
  if(native_tick_stamp>0 || observed_event)native_tick_changed=true;
  native_tick_stamp=q.time_msc;native_tick_bid=q.bid;native_tick_ask=q.ask;native_tick_seen=GetTickCount64();
 } else if(observed_event)native_tick_changed=true;
}
// Price operations use an observed tick event, never a wall-clock age cutoff.
bool NativeTickObserved(){return native_tick_changed;}
void NativePause(string why){if(native_paused && native_reason==why)return;native_paused=true;native_reason=why;SaveNative();}
bool NativeContext() {
 return native_healthy && NativeIdentity(ExpectedLogin,ExpectedServer) &&
  TerminalInfoInteger(TERMINAL_CONNECTED) && AccountInfoInteger(ACCOUNT_MARGIN_MODE)==ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;
}
bool NativePrice(double p) {
 double tick=SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE);
 return PolicyPositive(p) && PolicyPositive(tick) && MathAbs(p/tick-MathRound(p/tick))<1e-6;
}
bool NativeStops(int side,double sl,double tp,const MqlTick &q) {
 double point=SymbolInfoDouble(TradeSymbol,SYMBOL_POINT);
 double limit=MathMax((double)SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_STOPS_LEVEL),(double)SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_FREEZE_LEVEL))*point;
 if(!NativePrice(sl) || !NativePrice(tp))return false;
 return side==1 ? (sl<q.bid && q.bid-sl>=limit && tp>q.bid && tp-q.bid>=limit) : (sl>q.ask && sl-q.ask>=limit && tp<q.ask && q.ask-tp>=limit);
}
bool NativeVolume(double v) {
 double step=SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_STEP);
 return PolicyPositive(step) && v>=SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_MIN) && v<=SymbolInfoDouble(TradeSymbol,SYMBOL_VOLUME_MAX) && MathAbs(v/step-MathRound(v/step))<1e-7;
}
int AddNative(string kind,int side,double volume,double price,double sl,double tp,ulong pid=0,ulong order=0,int stage=0,ulong basket_override=0) {
 if(!NativeContext() || !AllowTrading || !LegacyOwnerStopped || !MigrationConfirmed || ArraySize(commands)>=10000)return -1;
 if(UnknownResource(pid,order))return -1;
 if(kind=="CANCEL" && (!OrderSelect(order) || OrderGetString(ORDER_SYMBOL)!=TradeSymbol || (ulong)OrderGetInteger(ORDER_MAGIC)!=Magic))return -1;
 NativeCommand c={};native_serial++;c.id="N"+StringSubstr(InstallationId,0,8)+StringSubstr(InstallationId,9,4)+StringSubstr(InstallationId,14,4)+StringSubstr(InstallationId,19,4)+(string)native_serial;c.kind=kind;c.status="READY";c.control=native_control;c.side=side;c.volume=volume;c.price=price;c.sl=sl;c.tp=tp;c.originalsl=sl;c.position=pid;c.order=order;c.stage=stage;c.basket=basket_override>0?basket_override:native_basket;
 if(pid>0) {
  if(!SelectNativePosition(pid))return -1;
  c.ticket=(ulong)PositionGetInteger(POSITION_TICKET);c.oldsl=PositionGetDouble(POSITION_SL);c.oldtp=PositionGetDouble(POSITION_TP);
 }
 int i=ArraySize(commands);ArrayResize(commands,i+1);commands[i]=c;
 if(!SaveNative())return -1;return i;
}
void SendNative(int i,bool released_tick=false) {
 if(i<0 || i>=ArraySize(commands) || (commands[i].status!="READY" && commands[i].status!="WAIT_TICK") || !AllowTrading)return;
 if(commands[i].kind!="CANCEL" && (!NativeContext() || (!NativeTickObserved() && !released_tick))){if(commands[i].status!="WAIT_TICK"){commands[i].status="WAIT_TICK";SaveNative();}return;}
 if(!NativeContext()){commands[i].status="WAIT_TICK";SaveNative();return;}
 NativeCommand c=commands[i];MqlTick q;MqlTradeRequest req={};MqlTradeResult result={};MqlTradeCheckResult check={};
 if(c.kind!="CANCEL" && (!SymbolInfoTick(TradeSymbol,q) || !PolicyPositive(q.bid) || !PolicyPositive(q.ask) || q.ask<q.bid)){if(commands[i].status!="WAIT_TICK"){commands[i].status="WAIT_TICK";SaveNative();}return;}
 req.symbol=TradeSymbol;req.magic=Magic;req.comment=c.id;req.deviation=MaxDeviationPoints;
 if(c.kind=="STOP") {
  if((native_paused && c.basket!=native_fixed_target) || !NativeVolume(c.volume) || !NativePrice(c.price) || !NativePrice(c.sl) || !NativePrice(c.tp)){commands[i].status="REJECTED";SaveNative();return;}
  double d=SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_STOPS_LEVEL)*SymbolInfoDouble(TradeSymbol,SYMBOL_POINT);
  if(c.side==1 ? (c.price<=q.ask || c.price-q.ask<d || c.sl>=c.price || c.tp<=c.price) : (c.price>=q.bid || q.bid-c.price<d || c.sl<=c.price || c.tp>=c.price)){commands[i].status="REJECTED";SaveNative();return;}
  req.action=TRADE_ACTION_PENDING;req.type=c.side==1?ORDER_TYPE_BUY_STOP:ORDER_TYPE_SELL_STOP;req.volume=c.volume;req.price=c.price;req.sl=c.sl;req.tp=c.tp;req.type_filling=ORDER_FILLING_RETURN;req.type_time=ORDER_TIME_GTC;
 } else if(c.kind=="CANCEL") {
  if(!OrderSelect(c.order)){commands[i].status="RECONCILING";SaveNative();return;}
  if(OrderGetString(ORDER_SYMBOL)!=TradeSymbol || (ulong)OrderGetInteger(ORDER_MAGIC)!=Magic){commands[i].status="REJECTED";native_reason="CANCEL_ORDER_IDENTITY_MISMATCH";SaveNative();return;}
  req.action=TRADE_ACTION_REMOVE;req.order=c.order;
 } else {
  if(!SelectNativePosition(c.position) || (ulong)PositionGetInteger(POSITION_TICKET)!=c.ticket ||
     PositionGetDouble(POSITION_SL)!=c.oldsl || PositionGetDouble(POSITION_TP)!=c.oldtp || PositionGetDouble(POSITION_VOLUME)!=c.volume) {
   commands[i].status="REJECTED";SaveNative();return;
  }
  req.position=c.ticket;
  if(c.kind=="MODIFY") {
   if(!NativeStops(c.side,c.sl,c.tp,q) || (c.oldsl>0 && (c.side==1?c.sl<c.oldsl:c.sl>c.oldsl))) {commands[i].status="REJECTED";SaveNative();return;}
   req.action=TRADE_ACTION_SLTP;req.sl=c.sl;req.tp=c.tp;
  } else if(c.kind=="CLOSE") {
   req.action=TRADE_ACTION_DEAL;req.type=c.side==1?ORDER_TYPE_SELL:ORDER_TYPE_BUY;req.volume=c.volume;req.price=c.side==1?q.bid:q.ask;
   long filling=SymbolInfoInteger(TradeSymbol,SYMBOL_FILLING_MODE);
   if((filling&SYMBOL_FILLING_FOK)!=0)req.type_filling=ORDER_FILLING_FOK;
   else if((filling&SYMBOL_FILLING_IOC)!=0)req.type_filling=ORDER_FILLING_IOC;
   else {commands[i].status="REJECTED";SaveNative();return;}
  } else {commands[i].status="REJECTED";SaveNative();return;}
 }
 if(!OrderCheck(req,check) || (check.retcode!=0 && check.retcode!=TRADE_RETCODE_DONE)) {commands[i].status=c.kind=="CANCEL" && !OrderSelect(c.order)?"RECONCILING":"REJECTED";SaveNative();return;}
 commands[i].status="UNKNOWN";
 if(!SaveNative())return; // Broker not contacted until uncertainty is durably recorded.
 // Last local readback AFTER the durable write narrows the manual-edit race.
 // MT5 has no conditional/CAS SL API: this cannot be atomic with the server.
 if(!NativeContext() || (c.kind!="CANCEL" && !NativeTickObserved() && !released_tick)) {commands[i].status="WAIT_TICK";SaveNative();return;}
 if(c.position>0 && (!SelectNativePosition(c.position) || (ulong)PositionGetInteger(POSITION_TICKET)!=c.ticket || PositionGetDouble(POSITION_SL)!=c.oldsl || PositionGetDouble(POSITION_TP)!=c.oldtp || PositionGetDouble(POSITION_VOLUME)!=c.volume)) {commands[i].status="REJECTED";SaveNative();return;}
 if(c.kind=="CANCEL" && !OrderSelect(c.order)){commands[i].status="RECONCILING";SaveNative();return;}
 bool sent=OrderSend(req,result); // THE ONLY TRADE SENDER IN THE NATIVE BACKEND.
 if(result.order>0 && c.kind=="STOP")commands[i].order=result.order;
 // Timeout/connection/ambiguous failures stay UNKNOWN. No automatic repeat.
 if(result.retcode==TRADE_RETCODE_REJECT || result.retcode==TRADE_RETCODE_INVALID ||
    result.retcode==TRADE_RETCODE_INVALID_VOLUME || result.retcode==TRADE_RETCODE_INVALID_PRICE ||
    result.retcode==TRADE_RETCODE_INVALID_STOPS || result.retcode==TRADE_RETCODE_NO_MONEY ||
    result.retcode==TRADE_RETCODE_TRADE_DISABLED || result.retcode==TRADE_RETCODE_MARKET_CLOSED)
  commands[i].status="REJECTED";
 SaveNative(); // Even DONE is not final until reconciliation/readback.
}
void DispatchNativeTickQueue() {
 if(!NativeContext()){ArrayResize(native_tick_released,0);return;}
 int prior_size=ArraySize(native_tick_released);ArrayResize(native_tick_released,ArraySize(commands));for(int i=prior_size;i<ArraySize(native_tick_released);i++)native_tick_released[i]=false;
 if(NativeTickObserved())for(int i=0;i<ArraySize(commands);i++)if(commands[i].status=="WAIT_TICK")native_tick_released[i]=true;
 int attempted=0;
 int count=ArraySize(commands);if(count==0)return;
 for(int scanned=0;scanned<count && attempted<4;scanned++){int i=native_dispatch_cursor%count;native_dispatch_cursor=(i+1)%count;
  if(commands[i].status=="WAIT_TICK" && (commands[i].kind=="CANCEL" || native_tick_released[i])){attempted++;SendNative(i,native_tick_released[i]);}}

}
bool NativeObserverEvidence(NativeCommand &c,double &entered,double &exited,double &net,int &reason,bool &proved) {
 string body=ReadNativeFile("position-"+(string)c.position+".proof"),lines[],head[],row[];
 int n=StringSplit(body,'\n',lines);
 if(n<2 || StringSplit(lines[0],'|',head)!=8 || head[0]!="NATIVE_OBS1" || head[1]!=(string)ExpectedLogin || head[2]!=ExpectedServer || head[3]!=TradeSymbol || head[4]!=(string)c.position || head[5]!="OBSERVED" || lines[n-1]!="END")return false;
 long stamp=StringToInteger(head[6]);int count=(int)StringToInteger(head[7]);
 if(count<0 || count>10000 || n!=count+2 || TimeLocal()-stamp<0 || TimeLocal()-stamp>10)return false;
 ulong ids[];ArrayResize(ids,count);double entry_sl=0;
 for(int i=0;i<count;i++) {
  if(StringSplit(lines[i+1],'|',row)!=12 || !NativeId(row[0]))return false;
  ids[i]=(ulong)StringToInteger(row[0]);long entry=StringToInteger(row[2]),type=StringToInteger(row[3]),why=StringToInteger(row[4]);
  double volume=StringToDouble(row[6]),value=StringToDouble(row[7])+StringToDouble(row[8])+StringToDouble(row[9])+StringToDouble(row[10]);
  if(!PolicyNonnegative(volume) || !MathIsValidNumber(value))return false;net+=value;
  if(type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL)continue;
  if(entry==DEAL_ENTRY_IN){if((ulong)StringToInteger(row[1])!=c.order || (ulong)StringToInteger(row[5])!=Magic)return false;double source_sl=StringToDouble(row[11]);if(source_sl<=0 || (entry_sl>0 && MathAbs(source_sl-entry_sl)>1e-9))return false;entry_sl=source_sl;c.originalsl=source_sl;entered+=volume;proved=true;}
  else if(entry==DEAL_ENTRY_OUT){
   exited+=volume;if(why==DEAL_REASON_SL && SupportConfirmed(c.originalsl,StringToDouble(row[11]),true,SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE)))c.support=true;
   int kind=why==DEAL_REASON_SL?(c.support?2:1):(why==DEAL_REASON_TP?3:4);if(reason==0)reason=kind;else if(reason!=kind)reason=4;
  }else return false;
 }
 ArraySort(ids);for(int i=1;i<count;i++)if(ids[i]==ids[i-1])return false;
 return true;
}
void ReconcileNative(NativeCommand &c) {
 if(c.kind!="STOP") {
  if(c.status!="UNKNOWN" && c.status!="RECONCILING")return;
  if(c.kind=="MODIFY" && SelectNativePosition(c.position) && PositionGetDouble(POSITION_SL)==c.sl && PositionGetDouble(POSITION_TP)==c.tp)c.status="DONE";
  if(c.kind=="CANCEL" && !OrderSelect(c.order) && HistoryOrderSelect(c.order)) {
   int source=-1;for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="STOP" && commands[j].order==c.order){source=j;break;}
   if(source<0 || HistoryOrderGetString(c.order,ORDER_SYMBOL)!=TradeSymbol || (ulong)HistoryOrderGetInteger(c.order,ORDER_MAGIC)!=Magic || HistoryOrderGetString(c.order,ORDER_COMMENT)!=commands[source].id){native_reason="CANCEL_HISTORY_IDENTITY_UNPROVEN";return;}
   long state=HistoryOrderGetInteger(c.order,ORDER_STATE);ulong pid=(ulong)HistoryOrderGetInteger(c.order,ORDER_POSITION_ID);
   bool terminal=state==ORDER_STATE_CANCELED || state==ORDER_STATE_EXPIRED || state==ORDER_STATE_REJECTED || state==ORDER_STATE_FILLED;
   if(!terminal)return;
   if(state==ORDER_STATE_FILLED || pid>0 || commands[source].filled>0){
    // An executed part must first be adopted by the position/deal reconciler.
    if(pid==0 || commands[source].position!=pid || (commands[source].status!="OPEN" && commands[source].status!="CLOSED"))return;
    bool live=SelectNativePosition(pid);if((commands[source].status=="OPEN" && !live) || (commands[source].status=="CLOSED" && live))return;
   }
   c.status="DONE";c.exit_kind=state==ORDER_STATE_FILLED?5:6;
  }
  if(c.kind=="CLOSE")for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="STOP" && commands[j].position==c.position && commands[j].closed)c.status="DONE";
  return;
 }
 if(c.status=="WAIT_TICK" || c.status=="READY" || c.status=="REJECTED" || c.status=="CANCELLED")return;
 // Recover missing OrderSend response by the unique saved broker comment.
 if(c.order==0) {
  for(int j=0;j<OrdersTotal();j++) {
   ulong t=OrderGetTicket(j);
   if(t>0 && OrderGetString(ORDER_COMMENT)==c.id && OrderGetString(ORDER_SYMBOL)==TradeSymbol && (ulong)OrderGetInteger(ORDER_MAGIC)==Magic) {if(c.order!=0 && c.order!=t){native_reason="ORDER_IDENTITY_AMBIGUOUS";return;}c.order=t;}
  }
  if(c.order==0 && HistorySelect(0,TimeCurrent()))for(int j=0;j<HistoryOrdersTotal();j++) {
   ulong t=HistoryOrderGetTicket(j);
   if(t>0 && HistoryOrderGetString(t,ORDER_COMMENT)==c.id && HistoryOrderGetString(t,ORDER_SYMBOL)==TradeSymbol && (ulong)HistoryOrderGetInteger(t,ORDER_MAGIC)==Magic) {if(c.order!=0 && c.order!=t){native_reason="ORDER_IDENTITY_AMBIGUOUS";return;}c.order=t;}
  }
 }
 if(c.order==0)return;
 bool active_partial=false;ulong pid=0;
 if(OrderSelect(c.order)) {
  if(OrderGetString(ORDER_COMMENT)!=c.id || OrderGetString(ORDER_SYMBOL)!=TradeSymbol || (ulong)OrderGetInteger(ORDER_MAGIC)!=Magic){native_reason="ORDER_IDENTITY_MISMATCH";return;}
  if(OrderGetDouble(ORDER_VOLUME_CURRENT)>=c.volume-1e-8){c.status="PENDING";return;}
  active_partial=true;c.status="PARTIAL";pid=(ulong)OrderGetInteger(ORDER_POSITION_ID);
 } else {
  if(!HistoryOrderSelect(c.order))return;
  if(HistoryOrderGetString(c.order,ORDER_COMMENT)!=c.id || HistoryOrderGetString(c.order,ORDER_SYMBOL)!=TradeSymbol || (ulong)HistoryOrderGetInteger(c.order,ORDER_MAGIC)!=Magic){native_reason="ORDER_IDENTITY_MISMATCH";return;}
  long state=HistoryOrderGetInteger(c.order,ORDER_STATE);pid=(ulong)HistoryOrderGetInteger(c.order,ORDER_POSITION_ID);
  if(pid==0 && (state==ORDER_STATE_CANCELED || state==ORDER_STATE_EXPIRED || state==ORDER_STATE_REJECTED)){c.status="CANCELLED";return;}
 }
 if(pid==0)return;
 if(c.position>0 && c.position!=pid){native_reason="POSITION_IDENTITY_CHANGED";return;}
 c.position=pid;
 bool open=SelectNativePosition(pid);double live_volume=0,live_sl=0,live_tp=0;
 if(open){c.ticket=(ulong)PositionGetInteger(POSITION_TICKET);live_volume=PositionGetDouble(POSITION_VOLUME);live_sl=PositionGetDouble(POSITION_SL);live_tp=PositionGetDouble(POSITION_TP);}
 double entered=0,exited=0,net=0,entry_sl=0;int reason=0;bool proved=false;NativeCommand evidence=c;
 if(RequireObserver) {
  if(!NativeObserverEvidence(evidence,entered,exited,net,reason,proved))return;
 } else {
 if(!HistorySelectByPosition(pid))return;
 for(int j=0;j<HistoryDealsTotal();j++) {
  ulong d=HistoryDealGetTicket(j);
  if(d==0 || (ulong)HistoryDealGetInteger(d,DEAL_POSITION_ID)!=pid || HistoryDealGetString(d,DEAL_SYMBOL)!=TradeSymbol)return;
  long type=HistoryDealGetInteger(d,DEAL_TYPE),entry=HistoryDealGetInteger(d,DEAL_ENTRY);
  net+=HistoryDealGetDouble(d,DEAL_PROFIT)+HistoryDealGetDouble(d,DEAL_COMMISSION)+HistoryDealGetDouble(d,DEAL_SWAP)+HistoryDealGetDouble(d,DEAL_FEE);
  if(type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL)continue;
  double volume=HistoryDealGetDouble(d,DEAL_VOLUME);
  if(entry==DEAL_ENTRY_IN) {
   if((ulong)HistoryDealGetInteger(d,DEAL_ORDER)!=c.order || (ulong)HistoryDealGetInteger(d,DEAL_MAGIC)!=Magic)return;
   double source_sl=HistoryDealGetDouble(d,DEAL_SL);if(source_sl<=0 || (entry_sl>0 && MathAbs(source_sl-entry_sl)>1e-9))return;entry_sl=source_sl;evidence.originalsl=source_sl;entered+=volume;proved=true;
  } else if(entry==DEAL_ENTRY_OUT) {
   exited+=volume;long r=HistoryDealGetInteger(d,DEAL_REASON);
   // DEAL_SL preserves the stop attached to the exit, including a manual
   // move not observed while disconnected. Never infer support from profit.
   double exit_sl=HistoryDealGetDouble(d,DEAL_SL);
   if(r==DEAL_REASON_SL && SupportConfirmed(evidence.originalsl,exit_sl,true,SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE)))evidence.support=true;
   int k=r==DEAL_REASON_SL?(evidence.support?2:1):(r==DEAL_REASON_TP?3:4);
   if(reason==0)reason=k;else if(reason!=k)reason=4;
  } else {native_reason="CLOSE_BY_OR_REVERSAL_NEEDS_REVIEW";return;}
 }
 }
 if(!proved || !MathIsValidNumber(net) || entered<=0 || exited>entered+1e-8 || MathAbs(entered-exited-live_volume)>1e-8)return;
 c.filled=entered;c.net=net;c.support=evidence.support;c.originalsl=evidence.originalsl;
 if(MathAbs(entered-c.volume)>1e-8){native_paused=true;native_reason="PARTIAL_ENTRY_NEEDS_REVIEW";}
 if(open) {
  c.closed=false;c.status=active_partial?"PARTIAL":"OPEN";
  if(c.oldtp>0 && live_tp!=c.oldtp) {
   bool ours=false;for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="MODIFY" && commands[j].position==pid && commands[j].tp==live_tp && (commands[j].status=="UNKNOWN" || commands[j].status=="DONE"))ours=true;
   if(!ours)NativePublish(MailboxFolder,"manual-tp-"+(string)pid+".txt",NativeDecimal(live_tp));
  }
  c.oldtp=live_tp;
  if(SupportConfirmed(c.originalsl,live_sl,true,SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE)))c.support=true;
 } else if(active_partial){c.closed=false;c.status="PARTIAL";}
 else if(MathAbs(entered-exited)<1e-8) {c.closed=true;c.status="CLOSED";c.exit_kind=reason;}
}
double NativeNet(){double n=0;for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP")n+=commands[i].net;return n;}
double NativeDebt(){double d=0;RecoveryBalance(native_carry,NativeNet()+native_credit,d);return d;}
bool NativeOwnedOnly() {
 for(int i=0;i<PositionsTotal();i++) {
  if(PositionGetTicket(i)==0 || PositionGetString(POSITION_SYMBOL)!=TradeSymbol)continue;
  ulong pid=(ulong)PositionGetInteger(POSITION_IDENTIFIER);bool found=false;
  for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="STOP" && commands[j].position==pid && !commands[j].closed)found=true;
  if(!found)return false;
 }
 for(int i=0;i<OrdersTotal();i++) {
  ulong t=OrderGetTicket(i);if(t==0 || OrderGetString(ORDER_SYMBOL)!=TradeSymbol)continue;
  bool found=false;for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="STOP" && commands[j].order==t)found=true;
  if(!found)return false;
 }
 return true;
}
bool ProfileNative(int count,double &distances[]) {
 if(count<2 || count>10)return false;
 string values[];int n=StringSplit(native_profiles[count],',',values);if(n!=count)return false;
 ArrayResize(distances,n);
 for(int i=0;i<n;i++){distances[i]=StringToDouble(values[i]);if(!PolicyPositive(distances[i]) || !NativePrice(distances[i]) || (i>0 && distances[i]<=distances[i-1]))return false;}
 return true;
}
void CancelNativeEntries() {
 bool changed=false;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && (commands[i].status=="READY" || commands[i].status=="WAIT_TICK")){commands[i].status="CANCELLED";changed=true;}
 if(changed && !SaveNative())return;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && (commands[i].status=="PENDING" || commands[i].status=="PARTIAL") && !UnknownResource(0,commands[i].order)) {
  bool refused=false;for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="CANCEL" && commands[j].order==commands[i].order && commands[j].control==native_control && commands[j].status=="REJECTED")refused=true;
  if(refused){native_reason="CANCEL_REJECTED_REQUIRES_DECISION";continue;}
  int k=AddNative("CANCEL",commands[i].side,0,0,0,0,0,commands[i].order);SendNative(k);
 }
}
bool FixedNativeDebt(double &debt) {
 double estimated=0;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_fixed_source && !commands[i].closed && commands[i].status!="CANCELLED" && commands[i].status!="REJECTED") {
  NativeCommand c=commands[i];if(c.status!="OPEN" || !SelectNativePosition(c.position))return false;
  double value=0,sl=PositionGetDouble(POSITION_SL),volume=PositionGetDouble(POSITION_VOLUME),entry=PositionGetDouble(POSITION_PRICE_OPEN);
  if(RequireObserver){double entered=0,exited=0,net=0;int why=0;bool proved=false;NativeCommand evidence=c;
   if(!NativeObserverEvidence(evidence,entered,exited,net,why,proved) || !proved || MathAbs(entered-exited-volume)>1e-8 || MathAbs(net-c.net)>1e-8)return false;}

  if(!PolicyPositive(sl) || !OrderCalcProfit(c.side==1?ORDER_TYPE_BUY:ORDER_TYPE_SELL,TradeSymbol,volume,entry,sl,value))return false;
  estimated+=value-ExitCostReserve; // only unbooked exit estimate; entry costs already in NativeNet
 }
 return EffectiveRecovery(native_carry,NativeNet()+native_credit,estimated,debt);
}
void AdvanceNativeBasket() {
 if(native_applied==native_basket || AnyUnknown() || native_close_all!="")return;
 int total=0,original=0,support=0,tp=0,other=0;bool partial=false;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_basket) {
  if(commands[i].status=="REJECTED"){NativePause("ENTRY_REJECTED_NEEDS_REVIEW");return;}
  if(commands[i].status=="CANCELLED")continue;
  if(!commands[i].closed)return;total++;
  if(MathAbs(commands[i].filled-commands[i].volume)>1e-8)partial=true;
  if(commands[i].exit_kind==1)original++;else if(commands[i].exit_kind==2)support++;else if(commands[i].exit_kind==3)tp++;else other++;
 }
 if(total==0)return;
 if(total!=(NativeInitial()?1:native_steps))partial=true;
 BasketOutcome result=DecideBasket(native_steps,true,true,original,support,tp,other,total,partial);
 if(native_fixed_source==native_basket && native_fixed_target>0) {
  bool live=false;for(int i=0;i<ArraySize(commands);i++)if(commands[i].basket==native_fixed_target && commands[i].kind=="STOP" && commands[i].status!="CANCELLED" && commands[i].status!="REJECTED")live=true;
  if(live){int expected=NextSteps(native_steps,result);native_applied=native_basket;native_basket=native_fixed_target;native_steps=native_fixed_steps;native_split_two=native_steps==2;native_reverse=false;native_bar=0;
   if(expected!=native_steps){native_paused=true;native_reason="FIXED_VOLUME_OUTCOME_REQUIRES_REVIEW";}SaveNative();return;}
  native_fixed_source=0;native_fixed_target=0; // cancelled/refused plan: ordinary source transition
 }
 if(native_fixed_target==native_basket){native_fixed_source=0;native_fixed_target=0;}
 if(result==BASKET_KEEP && NativeInitial() && support>0)native_split_two=true;
 if(result==BASKET_INCREASE || result==BASKET_KEEP) {native_applied=native_basket;native_steps=NextSteps(native_steps,result);native_basket++;native_reverse=false;native_bar=0;SaveNative();}
 else if(result==BASKET_FINISHED && NativeDebt()<=0) {native_applied=native_basket;native_steps=2;native_split_two=false;native_carry=NativeNet()+native_credit;native_basket++;native_reverse=false;native_bar=0;SaveNative();}
 else if(result==BASKET_FINISHED) {native_applied=native_basket;native_basket++;native_reverse=false;native_bar=0;SaveNative();} // debt carried; no volume increase
 else NativePause(result==BASKET_MANUAL_LIMIT?"FULL_SL_010_MANUAL": "MIXED_EXIT_NEEDS_DECISION");
}
void FollowNativeCandle() {
 if(ArraySize(commands)>=9000){NativePause("JOURNAL_ROLLOVER_REQUIRES_REVIEW");return;}
 if(native_fixed_target>0 || native_paused || native_reason!="" || AnyUnknown() || !NativeOwnedOnly())return;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && !commands[i].closed && commands[i].position>0)return;
 MqlRates bars[];MqlTick q;if(CopyRates(TradeSymbol,PERIOD_M1,1,1,bars)!=1 || !SymbolInfoTick(TradeSymbol,q) || q.time<bars[0].time+60 || bars[0].time<=native_bar)return;
 string config_name="basket-config-"+(string)native_basket+".txt",saved=ReadNativeFile(config_name),parts[];
 if(saved=="")for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_basket){NativePause("ACTIVE_BASKET_SETTINGS_MISSING");return;}
 if(saved=="" && native_next_steps>0){native_steps=native_next_steps;native_split_two=native_steps==2;native_next_steps=0;if(!SaveNative())return;}
 double offset=NativeInitial()?native_offset:native_recovery_offset,stop_distance=NativeInitial()?native_sl:native_recovery_sl;
 double distances[];int count=NativeInitial()?1:native_steps;
 if(saved!="") {if(StringSplit(saved,'|',parts)!=3){NativePause("ACTIVE_BASKET_SETTINGS_CORRUPT");return;}offset=StringToDouble(parts[0]);stop_distance=StringToDouble(parts[1]);string values[];if(StringSplit(parts[2],',',values)!=count){NativePause("ACTIVE_BASKET_PROFILE_CORRUPT");return;}ArrayResize(distances,count);for(int i=0;i<count;i++)distances[i]=StringToDouble(values[i]);}
 else {if(NativeInitial()){ArrayResize(distances,1);distances[0]=native_tp;}else if(!ProfileNative(native_steps,distances)){NativePause("MISSING_OR_INVALID_PROFILE_"+(string)native_steps);return;}}
 double price=0;bool buy=false;if(!CandleTrigger(bars[0].open,bars[0].close,offset,false,price,buy))return;
 // Cancel then verify before replacing. A fill during cancel wins over re-entry.
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].status=="PENDING"){CancelNativeEntries();return;}
 if(saved==""){string profile="";for(int i=0;i<count;i++){if(i)profile+=",";profile+=NativeDecimal(distances[i]);}if(!NativePublish(MailboxFolder,config_name,NativeDecimal(offset)+"|"+NativeDecimal(stop_distance)+"|"+profile)){NativePause("ACTIVE_SETTINGS_WRITE_FAILED");return;}}
 int side=buy?1:-1;double sl=price-side*stop_distance;
 if(!NativePrice(price) || !NativePrice(sl)){NativePause("ENTRY_PRICE_GRID_INVALID");return;}
 // Prepare ALL siblings before any send. Interrupted preparation does not infer fills.
 native_bar=bars[0].time;int first=ArraySize(commands);
 for(int i=0;i<count;i++)if(AddNative("STOP",side,NativeInitial()?0.02:0.01,price,sl,price+side*distances[i],0,0,i)<0){NativePause("ENTRY_PREPARATION_FAILED");return;}
 for(int i=first;i<ArraySize(commands);i++)SendNative(i);
}
// Invert broker OrderCalcProfit for the LAST STILL OPEN original stage.
// Uses actual net only; no expected profits of other open tickets are booked.
bool RecoveryTarget(int side,double volume,double entry,double required,double &target) {
 if(!PolicyPositive(required) || !NativeVolume(volume) || !PolicyPositive(entry))return false;
 ENUM_ORDER_TYPE type=side==1?ORDER_TYPE_BUY:ORDER_TYPE_SELL;
 double tick=SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE),low=0,high=tick,profit=0;
 if(!PolicyPositive(tick))return false;
 for(int i=0;i<60;i++) {
  double price=entry+side*high;if(price<=0 || !OrderCalcProfit(type,TradeSymbol,volume,entry,price,profit))return false;
  if(profit>=required)break;high*=2;if(i==59)return false;
 }
 for(int i=0;i<70;i++){double mid=(low+high)/2;if(!OrderCalcProfit(type,TradeSymbol,volume,entry,entry+side*mid,profit))return false;if(profit>=required)high=mid;else low=mid;}
 double raw=entry+side*high;
 target=(side==1?MathCeil(raw/tick):MathFloor(raw/tick))*tick;
 return ProfitableTarget(side==1,entry,target) && OrderCalcProfit(type,TradeSymbol,volume,entry,target,profit) && profit+1e-8>=required;
}
bool ArmNativeSLTriggers(string &reason) {
 reason="FIXED_SL_SOURCE_NOT_READY";
 if(native_fixed_target>0 || AnyUnknown() || !NativeOwnedOnly() || native_close_all!="" || native_cancel_control!="" || native_reason=="FULL_SL_010_MANUAL")return false;
 int total=0,original=0,support=0,tp=0,other=0,side=0,open=0;double price=0;bool partial=false;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_basket && commands[i].status!="CANCELLED") {
  NativeCommand c=commands[i];total++;if(c.status=="REJECTED" || MathAbs(c.filled-c.volume)>1e-8)partial=true;
  if(c.closed){if(c.exit_kind==3)tp++;else if(c.exit_kind==1)original++;else if(c.exit_kind==2)support++;else other++;continue;}
  if(c.status!="OPEN" || !SelectNativePosition(c.position))return false;
  double sl=PositionGetDouble(POSITION_SL);if(!NativePrice(sl))return false;
  if(open>0 && (c.side!=side || MathAbs(sl-price)>1e-8)){reason="SOURCE_SL_LEVELS_DIFFER_REQUIRE_SELECTION";return false;}
  side=c.side;price=sl;open++;
  if(c.support || SupportConfirmed(c.originalsl,sl,true,SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE)))support++;else original++;
 }
 if(open==0 || total!=(NativeInitial()?1:native_steps))return false;
 BasketOutcome outcome=DecideBasket(native_steps,true,true,original,support,tp,other,total,partial);
 int steps=NextSteps(native_steps,outcome);if(steps<2){reason="FIXED_SL_VOLUME_REQUIRES_REVIEW";return false;}
 double distances[];if(!ProfileNative(steps,distances)){reason="MISSING_OR_INVALID_PROFILE_"+(string)steps;return false;}
 MqlTick q;if(!SymbolInfoTick(TradeSymbol,q))return false;int target_side=-side;
 double gap=SymbolInfoInteger(TradeSymbol,SYMBOL_TRADE_STOPS_LEVEL)*SymbolInfoDouble(TradeSymbol,SYMBOL_POINT);
 if(target_side==1?(price<=q.ask || price-q.ask<gap):(price>=q.bid || q.bid-price<gap)){reason="FIXED_SL_PRICE_ALREADY_CROSSED_OR_TOO_CLOSE";return false;}
 native_fixed_source=native_basket;double debt=0;
 if(!FixedNativeDebt(debt)){native_fixed_source=0;return false;}
 double last=0,required=0;
 double projected=0;for(int i=0;i<steps-1;i++){double profit=0;if(!OrderCalcProfit(target_side==1?ORDER_TYPE_BUY:ORDER_TYPE_SELL,TradeSymbol,.01,price,price+target_side*distances[i],profit)){native_fixed_source=0;reason="PROVISIONAL_SIBLING_TP_CALCULATION_FAILED";return false;}projected+=profit-ExitCostReserve;}
 required=debt-projected+ExitCostReserve;
 if(!PolicyPositive(required) || !RecoveryTarget(target_side,.01,price,required,last)){native_fixed_source=0;reason="NONPOSITIVE_OR_INVALID_PROVISIONAL_TP";return false;}
 double sl=price-target_side*native_recovery_sl;
 if(!NativePrice(sl) || !NativePrice(last)){native_fixed_source=0;reason="FIXED_SL_INVALID_PRICE_GRID";return false;}
 native_fixed_target=native_basket+1;native_fixed_steps=steps;native_fixed_side=target_side;native_fixed_price=price;
 if(!SaveNative()){reason="FIXED_SL_JOURNAL_FAILED";return false;}
 int first=ArraySize(commands);
 for(int i=0;i<steps;i++)if(AddNative("STOP",target_side,.01,price,sl,i==steps-1?last:price+target_side*distances[i],0,0,i,native_fixed_target)<0){NativePause("FIXED_SL_PREPARATION_INTERRUPTED");reason=native_reason;return false;}
 for(int i=first;i<ArraySize(commands);i++)SendNative(i);
 reason="FIXED_SL_ORDERS_SUBMITTED_AWAITING_BROKER";return true;
}
// Projected sibling profits allocate a TP budget only. They NEVER enter NativeNet.
// Closed siblings are already in the actual ledger and are not projected again.
bool NativeRemainingGross(ulong basket,int last,double debt,double &required) {
 double expected=0;
 for(int i=0;i<ArraySize(commands);i++)if(i!=last && commands[i].kind=="STOP" && commands[i].basket==basket && !commands[i].closed && commands[i].status=="OPEN") {
  NativeCommand c=commands[i];if(!SelectNativePosition(c.position))return false;
  double entry=PositionGetDouble(POSITION_PRICE_OPEN),tp=PositionGetDouble(POSITION_TP),volume=PositionGetDouble(POSITION_VOLUME),profit=0;
  if(!ProfitableTarget(c.side==1,entry,tp) || !OrderCalcProfit(c.side==1?ORDER_TYPE_BUY:ORDER_TYPE_SELL,TradeSymbol,volume,entry,tp,profit) || !PolicyPositive(profit))return false;
  expected+=profit-ExitCostReserve;
 }
 required=debt-expected+ExitCostReserve;
 return PolicyPositive(required);
}
void AdjustNativeRecoveryTP() {
 if((NativeInitial() && native_fixed_target==0) || AnyUnknown())return;
 ulong target_basket=native_fixed_target>0?native_fixed_target:native_basket;
 int last=-1;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==target_basket && commands[i].status=="OPEN" && !commands[i].closed && (last<0 || commands[i].stage>commands[last].stage))last=i;
 if(last<0)return;
 NativeCommand c=commands[last];if(!SelectNativePosition(c.position))return;
 string manual_target=ReadNativeFile("manual-tp-"+(string)c.position+".txt");
 if(manual_target!="" && StringToDouble(manual_target)==PositionGetDouble(POSITION_TP))return;
 double debt=NativeDebt();if(native_fixed_target>0 && !FixedNativeDebt(debt)){native_reason="WAITING_SOURCE_SL_RECONCILIATION";return;}
 double required=0;if(!NativeRemainingGross(target_basket,last,debt,required)){native_reason="NONPOSITIVE_OR_UNAVAILABLE_RECOVERY_TP_NEEDS_DECISION";return;}
 if(!SelectNativePosition(c.position))return;
 double target=0,volume=PositionGetDouble(POSITION_VOLUME),entry=PositionGetDouble(POSITION_PRICE_OPEN),sl=PositionGetDouble(POSITION_SL),oldtp=PositionGetDouble(POSITION_TP);
 if(!RecoveryTarget(c.side,volume,entry,required,target)){native_reason="RECOVERY_TP_CALCULATION_FAILED";return;}
 if(MathAbs(target-oldtp)<SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE)/2)return;
 MqlTick q;if(!SymbolInfoTick(TradeSymbol,q) || !NativeStops(c.side,sl,target,q)){native_reason="RECOVERY_TP_PRICE_REQUIRES_DECISION";return;}
 for(int i=ArraySize(commands)-1;i>=0;i--)if(commands[i].kind=="MODIFY" && commands[i].position==c.position && commands[i].sl==sl && commands[i].tp==target && commands[i].status=="REJECTED") {native_reason="RECOVERY_TP_REJECTED_REQUIRES_DECISION";return;}
 int k=AddNative("MODIFY",c.side,volume,0,sl,target,c.position);SendNative(k);
}
#endif
