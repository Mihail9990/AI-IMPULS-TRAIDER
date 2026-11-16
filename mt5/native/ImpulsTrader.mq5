#property strict
#property version "0.30"
#property description "Native strategy owner. Trading opt-in plus explicit migration required."
#include "NativeWire.mqh"
#include "RecoveryPolicy.mqh"
input string MailboxFolder="AI_IMPULS_NATIVE_V1";
input string InstallationId=""; // UUID from native config; never reuse for a different journal
input long ExpectedLogin=0;
input string ExpectedServer="";
input string TradeSymbol="XAUUSD.s";
input ulong Magic=25101103;
input bool AllowTrading=false;
input bool LegacyOwnerStopped=false;
input bool MigrationConfirmed=false;
input bool NewAccountingStart=false; // New installation only: zero carry, no historical import; never resets an existing journal.
input bool RequireObserver=true; // Production native path consumes the second EA evidence, no heavy history scan here.
input double CandleOffset=0;
input double RecoveryCandleOffset=0;
input int StartingSteps=2;
input bool CarryStageConfirmed=false;
input double InitialSLDistance=0;
input double InitialTPDistance=0;
input double RecoverySLDistance=0;
input double StartingRecovery=0;
input double ExitCostReserve=0; // Positive estimate only, excludes already booked costs
input uint MaxDeviationPoints=0;
#include "NativeEngine.mqh"
#include "NativeControl.mqh"
int owner=INVALID_HANDLE;
bool dirty=true;int closed_cursor=0,native_pass=0;

void NativeSnapshot() {
 MqlTick q={};SymbolInfoTick(TradeSymbol,q);string positions="[",orders="[";bool first=true;
 string manifest=(string)ExpectedLogin+"|"+ExpectedServer+"|"+TradeSymbol+"|"+(string)native_basket+"\n";
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].position>0 && !commands[i].closed && SelectNativePosition(commands[i].position)) {
  if(!first)positions+=",";first=false;
  positions+="{\"id\":"+NativeU(commands[i].position)+",\"stage\":"+(string)commands[i].stage+",\"side\":"+NativeQ(commands[i].side==1?"BUY":"SELL")+",\"entry\":"+NativeN(PositionGetDouble(POSITION_PRICE_OPEN))+",\"volume\":"+NativeN(PositionGetDouble(POSITION_VOLUME))+",\"sl\":"+NativeN(PositionGetDouble(POSITION_SL))+",\"tp\":"+NativeN(PositionGetDouble(POSITION_TP))+",\"support_confirmed\":"+(commands[i].support?"true":"false")+"}";

 }
 for(int i=0;i<PositionsTotal();i++) {
  ulong ticket=PositionGetTicket(i);if(ticket==0 || PositionGetString(POSITION_SYMBOL)!=TradeSymbol)continue;
  ulong pid=(ulong)PositionGetInteger(POSITION_IDENTIFIER);bool owned=false;for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="STOP" && commands[j].position==pid)owned=true;
  if(owned)continue;if(!first)positions+=",";first=false;
  positions+="{\"id\":"+NativeU(pid)+",\"owned\":false,\"side\":"+NativeQ(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?"BUY":"SELL")+",\"entry\":"+NativeN(PositionGetDouble(POSITION_PRICE_OPEN))+",\"volume\":"+NativeN(PositionGetDouble(POSITION_VOLUME))+",\"sl\":"+NativeN(PositionGetDouble(POSITION_SL))+",\"tp\":"+NativeN(PositionGetDouble(POSITION_TP))+"}";
 }
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].position>0 && commands[i].basket+1>=native_basket){NativePublish(MailboxFolder,"position-cycle-"+(string)commands[i].position+".txt",(string)ExpectedLogin+"|"+ExpectedServer+"|"+TradeSymbol+"|"+(string)commands[i].position+"|"+(string)commands[i].basket);manifest+=(string)commands[i].position+"\n";}
 first=true;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && (commands[i].status=="PENDING" || commands[i].status=="PARTIAL")) {
  if(!first)orders+=",";first=false;orders+="{\"id\":"+NativeU(commands[i].order)+",\"side\":"+NativeQ(commands[i].side==1?"BUY":"SELL")+",\"price\":"+NativeN(commands[i].price)+",\"volume\":"+NativeN(commands[i].volume)+"}";
 }
 for(int i=0;i<OrdersTotal();i++) {
  ulong ticket=OrderGetTicket(i);if(ticket==0 || OrderGetString(ORDER_SYMBOL)!=TradeSymbol)continue;
  bool owned=false;for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="STOP" && commands[j].order==ticket)owned=true;
  if(owned)continue;if(!first)orders+=",";first=false;orders+="{\"id\":"+NativeU(ticket)+",\"owned\":false,\"side\":\"UNMANAGED\",\"volume\":"+NativeN(OrderGetDouble(ORDER_VOLUME_CURRENT))+"}";
 }
 string candles="[",history="[";MqlRates bars[];int copied=CopyRates(TradeSymbol,PERIOD_M1,0,60,bars);bool first_bar=true;
 for(int i=0;i<copied;i++){if(bars[i].time>q.time)continue;if(!first_bar)candles+=",";first_bar=false;candles+="{\"time\":"+(string)bars[i].time+",\"open\":"+NativeN(bars[i].open)+",\"high\":"+NativeN(bars[i].high)+",\"low\":"+NativeN(bars[i].low)+",\"close\":"+NativeN(bars[i].close)+"}";}
 int kept=0;for(int i=ArraySize(commands)-1;i>=0 && kept<100;i--)if(commands[i].kind=="STOP" && commands[i].closed){if(kept++)history+=",";history+="{\"position_id\":"+NativeU(commands[i].position)+",\"side\":"+NativeQ(commands[i].side==1?"BUY":"SELL")+",\"net\":"+NativeN(commands[i].net)+",\"reason\":"+NativeQ(commands[i].exit_kind==1?"INITIAL_SL":(commands[i].exit_kind==2?"SUPPORT_SL":(commands[i].exit_kind==3?"TP":"MIXED_OR_MANUAL")))+"}";}
 string profiles="{";for(int n=2;n<=10;n++){if(n>2)profiles+=",";profiles+=NativeQ((string)n)+":"+NativeQ(native_profiles[n]);}profiles+="}";
 string phase=!NativeContext()?"DISCONNECTED":(AnyUnknown()?"AWAITING_BROKER":(native_paused?"PAUSED":"RUNNING"));
 bool tick_wait=false,broker_wait=false,recon_wait=false;for(int i=0;i<ArraySize(commands);i++){if(commands[i].status=="RECONCILING")recon_wait=true;if(commands[i].status=="WAIT_TICK")tick_wait=true;if(commands[i].status=="UNKNOWN")broker_wait=true;}
 if(NativeContext() && tick_wait && !broker_wait)phase="WAITING_TICK";
 if(NativeContext() && recon_wait && !broker_wait)phase="WAITING_RECONCILIATION";
 string body="{\"protocol\":\"native_state_v1\",\"installation\":"+NativeQ(InstallationId)+",\"login\":"+NativeU((ulong)ExpectedLogin)+",\"server\":"+NativeQ(ExpectedServer)+",\"symbol\":"+NativeQ(TradeSymbol)+",\"currency\":"+NativeQ(AccountInfoString(ACCOUNT_CURRENCY))+",\"revision\":"+(string)native_revision+",\"accounting_start\":"+(string)native_accounting_start+",\"cycle_id\":"+NativeU(native_basket)+",\"phase\":"+NativeQ(phase)+",\"reason\":"+NativeQ(native_reason)+",\"read_only\":"+(AllowTrading?"false":"true")+",\"split_two\":"+(native_split_two?"true":"false")+",\"steps\":"+(string)native_steps+",\"recovery\":"+NativeN(NativeDebt())+",\"realized\":"+NativeN(NativeNet())+",\"bid\":"+NativeN(q.bid)+",\"ask\":"+NativeN(q.ask)+",\"tick_size\":"+NativeN(SymbolInfoDouble(TradeSymbol,SYMBOL_TRADE_TICK_SIZE))+",\"broker_time\":"+(string)q.time+",\"positions\":"+positions+"],\"orders\":"+orders+"],\"candles\":"+candles+"],\"history\":"+history+"],\"settings\":{\"sl_first\":"+NativeN(native_sl_first)+",\"sl_second\":"+NativeN(native_sl_second)+",\"sl_first_offset\":"+NativeN(native_sl_first_offset)+",\"sl_second_offset\":"+NativeN(native_sl_second_offset)+",\"next_steps\":"+(string)native_next_steps+",\"initial_sl\":"+NativeN(native_sl)+",\"initial_tp\":"+NativeN(native_tp)+",\"offset\":"+NativeN(native_offset)+",\"recovery_sl\":"+NativeN(native_recovery_sl)+",\"recovery_offset\":"+NativeN(native_recovery_offset)+",\"profiles\":"+profiles+"}}";
 NativePublish(MailboxFolder,"native-view.json",body);
 NativePublish(MailboxFolder,"sl-monitor-settings.txt",(string)ExpectedLogin+"|"+ExpectedServer+"|"+TradeSymbol+"|"+NativeDecimal(native_sl_first)+"|"+NativeDecimal(native_sl_second)+"|"+NativeDecimal(native_sl_first_offset)+"|"+NativeDecimal(native_sl_second_offset));
 NativePublish(MailboxFolder,"native-watch.txt",manifest+"END");
}
void NativeTP1Protection() {
 if(NativeInitial())return;
 bool tp1=false;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_basket && commands[i].stage==0 && commands[i].closed && commands[i].exit_kind==3)tp1=true;
 if(!tp1)return;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_basket && !commands[i].closed && commands[i].position>0 && !UnknownResource(commands[i].position) && SelectNativePosition(commands[i].position)) {
  NativeCommand c=commands[i];string plan="tp1-sl-"+(string)c.basket+"-"+(string)c.position+".txt";
  if(FileIsExist(MailboxFolder+"\\"+plan,FILE_COMMON))continue;
  double entry=PositionGetDouble(POSITION_PRICE_OPEN),sl=PositionGetDouble(POSITION_SL),tp=PositionGetDouble(POSITION_TP),volume=PositionGetDouble(POSITION_VOLUME);
  if(sl>0 && (c.side==1?sl>=entry:sl<=entry))continue;
  int k=AddNative("MODIFY",c.side,volume,0,entry,tp,c.position);if(k<0)continue;
  if(!NativePublish(MailboxFolder,plan,commands[k].id))return;SendNative(k);
 }
}
void NativeReverseLadder() {
 if(!native_reverse)return;
 int indices[];double targets[];int count=0;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_basket && !commands[i].closed && commands[i].position>0){ArrayResize(indices,count+1);indices[count++]=i;}
 if(count==0)return;
 // Stable original stage rank; remaining count and zero-based rank reproduce
 // the existing reverse_sl_prices policy, anchored at each actual entry.
 for(int i=0;i<count;i++)for(int j=i+1;j<count;j++)if(commands[indices[j]].stage<commands[indices[i]].stage){int x=indices[i];indices[i]=indices[j];indices[j]=x;}
 MqlTick q;if(!SymbolInfoTick(TradeSymbol,q))return;ArrayResize(targets,count);
 for(int rank=0;rank<count;rank++) {
  NativeCommand c=commands[indices[rank]];if(UnknownResource(c.position) || !SelectNativePosition(c.position))return;
  double entry=PositionGetDouble(POSITION_PRICE_OPEN),close=c.side==1?q.bid:q.ask,sl=PositionGetDouble(POSITION_SL),tp=PositionGetDouble(POSITION_TP);
  targets[rank]=entry+(close-entry)*rank/count;
  if(c.side*(close-entry)<=0 || !NativePrice(targets[rank]) || !NativeStops(c.side,targets[rank],tp,q)){native_reason="REVERSE_LADDER_GRID_REQUIRES_DECISION";return;}
  if(rank>0 && c.side*(targets[rank]-targets[rank-1])<=0){native_reason="REVERSE_LADDER_ORDER_REQUIRES_DECISION";return;}
  if(sl>0 && (c.side==1?targets[rank]<sl:targets[rank]>sl))targets[rank]=sl;
 }
 for(int rank=0;rank<count;rank++) {
  NativeCommand c=commands[indices[rank]];string plan="reverse-"+(string)c.basket+"-"+(string)c.position+".txt";
  if(FileIsExist(MailboxFolder+"\\"+plan,FILE_COMMON) || !SelectNativePosition(c.position))continue;
  int k=AddNative("MODIFY",c.side,PositionGetDouble(POSITION_VOLUME),0,targets[rank],PositionGetDouble(POSITION_TP),c.position);
  if(k<0)continue;if(!NativePublish(MailboxFolder,plan,commands[k].id))return;SendNative(k);
 }
}
void NativeStep() {
 if(!native_healthy)return;
 RefreshNativeQuote();
 PollNativeControls();
 if(NativeContext()) {
  // Bounded direct reconciliation; background observer is corroboration, not
  // authority to repeat a command. UI actions are processed before history scans.
  DispatchNativeTickQueue();
  bool changed=false;int total=ArraySize(commands),checked=0;
  for(int scan=0;scan<total && checked<4;scan++) {
   int index=native_cursor%total;native_cursor=(index+1)%total;
   if(commands[index].status!="UNKNOWN" && commands[index].status!="RECONCILING" && commands[index].status!="PARTIAL" && commands[index].status!="PENDING" && commands[index].status!="OPEN")continue;
   checked++;string before=commands[index].status;double oldtp=commands[index].oldtp,net=commands[index].net;bool support=commands[index].support;ulong pid=commands[index].position,order=commands[index].order;
   ReconcileNative(commands[index]);
   if(before!=commands[index].status || oldtp!=commands[index].oldtp || net!=commands[index].net || support!=commands[index].support || pid!=commands[index].position || order!=commands[index].order)changed=true;
  }
  // Late broker costs/corrections are refreshed separately; active commands
  // do not wait behind thousands of already completed journal records.
  native_pass++;
  if(native_pass%10==0 && total>0)for(int scan=0;scan<total;scan++) {
   int index=closed_cursor%total;closed_cursor=(index+1)%total;
   if(commands[index].kind!="STOP" || !commands[index].closed)continue;
   double net=commands[index].net;ReconcileNative(commands[index]);if(net!=commands[index].net)changed=true;break;
  }
  if(changed)SaveNative();
  ContinueNativeCancellation();ContinueNativeCloseAll();RefreshNativeSLAcks();ApplyNativeSLRecommendations();NativeTP1Protection();NativeReverseLadder();
  AdvanceNativeBasket();AdjustNativeRecoveryTP();FollowNativeCandle();
 }
 NativeSnapshot();dirty=false;
}
int OnInit() {
 if(!NativeFolder(MailboxFolder) || !NativeFolder(InstallationId) || StringLen(InstallationId)!=36 ||
  !NativeIdentity(ExpectedLogin,ExpectedServer) || _Symbol!=TradeSymbol || StringFind(ExpectedServer,"|")>=0 || StringFind(ExpectedServer,"\n")>=0 || StringFind(TradeSymbol,"|")>=0 ||
  AccountInfoInteger(ACCOUNT_MARGIN_MODE)!=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING || !PolicyNonnegative(ExitCostReserve))return INIT_PARAMETERS_INCORRECT;
 if(AllowTrading && (!LegacyOwnerStopped || !MigrationConfirmed))return INIT_PARAMETERS_INCORRECT;
 FolderCreate(MailboxFolder,FILE_COMMON);FolderCreate(MailboxFolder+"\\native_audit",FILE_COMMON);FolderCreate(MailboxFolder+"\\native_receipts",FILE_COMMON);FolderCreate(MailboxFolder+"\\native_requests",FILE_COMMON);FolderCreate(MailboxFolder+"\\native_accepted",FILE_COMMON);
 // Account+symbol owner lock is shared across native instances, independent of folder.
 string lock="AI_IMPULS_NATIVE_"+(string)ExpectedLogin+"_"+TradeSymbol+".lock";
 owner=FileOpen(lock,FILE_READ|FILE_WRITE|FILE_BIN|FILE_COMMON);
 if(owner==INVALID_HANDLE)return INIT_FAILED;
 bool existing=FileIsExist(NativeStatePath(),FILE_COMMON);
 FolderCreate("AI_IMPULS_NATIVE_BINDINGS",FILE_COMMON);
 string binding_name=InstallationId+".binding",binding_path="AI_IMPULS_NATIVE_BINDINGS\\"+binding_name;
 string binding=(string)ExpectedLogin+"|"+ExpectedServer+"|"+TradeSymbol+"|"+(string)Magic+"|"+MailboxFolder;
 if(FileIsExist(binding_path,FILE_COMMON)) {
  int h=FileOpen(binding_path,FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON,0,CP_UTF8);
  if(h==INVALID_HANDLE){FileClose(owner);owner=INVALID_HANDLE;return INIT_FAILED;}
  string prior=FileReadString(h);FileClose(h);
  if(prior!=binding || !existing){FileClose(owner);owner=INVALID_HANDLE;return INIT_FAILED;}
 }
 if(existing) {if(!LoadNative()){FileClose(owner);owner=INVALID_HANDLE;return INIT_FAILED;}}
 else {
  if(!NativeOwnedOnly()){native_paused=true;native_reason="NEW_ACCOUNTING_BLOCKED_EXISTING_POSITIONS_OR_ORDERS";NativeSnapshot();Print("New accounting blocked: inspect native-view.json for existing positions and orders; no history deleted.");FileClose(owner);owner=INVALID_HANDLE;return INIT_PARAMETERS_INCORRECT;}
  if(!MigrationConfirmed || !PolicyPositive(CandleOffset) || !PolicyPositive(InitialSLDistance) || !PolicyPositive(InitialTPDistance) || !PolicyPositive(RecoverySLDistance) || !PolicyPositive(RecoveryCandleOffset) || StartingSteps<2 || StartingSteps>10 || (StartingRecovery>0 && !CarryStageConfirmed) || !PolicyNonnegative(StartingRecovery) || !NativeOwnedOnly()){FileClose(owner);owner=INVALID_HANDLE;return INIT_PARAMETERS_INCORRECT;}
  if(NewAccountingStart && (StartingRecovery!=0 || StartingSteps!=2)){FileClose(owner);owner=INVALID_HANDLE;return INIT_PARAMETERS_INCORRECT;}
  native_accounting_start=TimeCurrent();native_paused=true;native_reason="";
  native_sl=InitialSLDistance;native_tp=InitialTPDistance;native_offset=CandleOffset;native_recovery_sl=RecoverySLDistance;native_carry=StartingRecovery;native_steps=StartingSteps;native_split_two=StartingSteps==2 && StartingRecovery>0;native_recovery_offset=RecoveryCandleOffset;
  for(int n=2;n<=10;n++){native_profiles[n]="";for(int j=0;j<n;j++){if(j>0)native_profiles[n]+=",";native_profiles[n]+=DoubleToString(ApprovedTP(n,j),2);}}
 }
 if(!FileIsExist(binding_path,FILE_COMMON) && !NativePublish("AI_IMPULS_NATIVE_BINDINGS",binding_name,binding)){FileClose(owner);owner=INVALID_HANDLE;return INIT_FAILED;}
 if(!SaveNative() || !EventSetMillisecondTimer(100)){FileClose(owner);owner=INVALID_HANDLE;return INIT_FAILED;}
 NativeSnapshot();return INIT_SUCCEEDED;
}
void OnTick(){native_tick_event=true;dirty=true;} // Strategy work stays bounded on timer; never replay assumed ticks.
void OnTradeTransaction(const MqlTradeTransaction &trans,const MqlTradeRequest &request,const MqlTradeResult &result){dirty=true;}
void OnTimer(){NativeStep();}
void OnDeinit(const int reason){EventKillTimer();if(owner!=INVALID_HANDLE)FileClose(owner);}
