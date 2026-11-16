#property strict
#property version "0.10"
#property description "Native candidate: durable SL recommendations only; never trades."
#include "NativeWire.mqh"
#include "RecoveryPolicy.mqh"
input string MailboxFolder="AI_IMPULS_NATIVE_V1";
input long ExpectedLogin=0;
input string ExpectedServer="";
input string TradeSymbol="XAUUSD.s";
input string CycleId="";
input string PositionIdentifiers=""; // explicit manifest candidate, NOT proof of cycle ownership
input double FirstThreshold=0.25;
input double SecondThreshold=0.40;
input double FirstOffset=-0.25; // signed offset in profitable direction from entry
input double SecondOffset=0.0;
// No wall-clock quote-age cutoff; reconnect still requires a new observation.
ulong watched[],revisions[],tick_cursors[];
string fingerprints[],bindings[];
string active_cycle="";
int reached[];
int owner=INVALID_HANDLE,cursor=0;
long quote_time=0;
double quote_bid=0,quote_ask=0;
ulong quote_seen=0;
bool quote_changed_since_start=false,healthy=true;

double monitor_first=.25,monitor_second=.40,monitor_first_offset=-.25,monitor_second_offset=0;
void ReadMonitorSettings(){
 int h=FileOpen(MailboxFolder+"\\sl-monitor-settings.txt",FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON,0,CP_UTF8);if(h==INVALID_HANDLE)return;
 string value=FileReadString(h);FileClose(h);string parts[];if(StringSplit(value,'|',parts)!=7 || parts[0]!=(string)ExpectedLogin || parts[1]!=ExpectedServer || parts[2]!=TradeSymbol)return;
 double a=StringToDouble(parts[3]),b=StringToDouble(parts[4]),c=StringToDouble(parts[5]),d=StringToDouble(parts[6]);
 if(!PolicyPositive(a)||!PolicyPositive(b)||b<=a||!MathIsValidNumber(c)||!MathIsValidNumber(d)||d<c)return;
 monitor_first=a;monitor_second=b;monitor_first_offset=c;monitor_second_offset=d;
}
string PositionCycle(ulong pid) {
 if(CycleId!="")return CycleId;
 int h=FileOpen(MailboxFolder+"\\position-cycle-"+(string)pid+".txt",FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON,0,CP_UTF8);
 if(h==INVALID_HANDLE)return "";string value=FileReadString(h);FileClose(h);string parts[];
 if(StringSplit(value,'|',parts)!=5 || parts[0]!=(string)ExpectedLogin || parts[1]!=ExpectedServer || parts[2]!=TradeSymbol || parts[3]!=(string)pid || !NativeId(parts[4]))return "";
 return parts[4];
}
string RevisionFile(ulong pid) {return "sl-state-"+PositionCycle(pid)+"-"+(string)pid+".txt";}
bool LoadRevision(int i) {
   string file=MailboxFolder+"\\"+RevisionFile(watched[i]);
   if(!FileIsExist(file,FILE_COMMON)) {revisions[i]=0;reached[i]=0;bindings[i]="";tick_cursors[i]=0;return true;}
   int h=FileOpen(file,FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON,0,CP_UTF8);
   if(h==INVALID_HANDLE)return false;
   string value=FileReadString(h);bool end=FileIsEnding(h);FileClose(h);
   string fields[];
   if(!end || StringSplit(value,'|',fields)!=4 || !NativeId(fields[0]) ||
      (fields[1]!="0" && fields[1]!="1" && fields[1]!="2"))return false;
   revisions[i]=(ulong)StringToInteger(fields[0]);reached[i]=(int)StringToInteger(fields[1]);
   bindings[i]=fields[2];tick_cursors[i]=(ulong)StringToInteger(fields[3]);return true;
}
bool SelectIdentifier(ulong id) {
   for(int i=0;i<PositionsTotal();i++)
      if(PositionGetTicket(i)>0 && (ulong)PositionGetInteger(POSITION_IDENTIFIER)==id)
         return PositionGetString(POSITION_SYMBOL)==TradeSymbol;
   return false;
}
void Recommend(int i,const MqlTick &tick) {
   ulong pid=watched[i];string position_cycle=PositionCycle(pid);if(position_cycle=="")return;
   if(!SelectIdentifier(pid))return; // absence is not a close, never synthesize acknowledgement
   bool buy=PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY;
   double entry=PositionGetDouble(POSITION_PRICE_OPEN),sl=PositionGetDouble(POSITION_SL),tp=PositionGetDouble(POSITION_TP);
   double volume=PositionGetDouble(POSITION_VOLUME),price=buy?tick.bid:tick.ask,candidate=0;
   string binding=(string)ExpectedLogin+":"+ExpectedServer+":"+TradeSymbol+":"+
      NativeN(entry)+":"+(buy?"BUY":"SELL")+":"+NativeN(monitor_first)+":"+
      NativeN(monitor_second)+":"+NativeN(monitor_first_offset)+":"+NativeN(monitor_second_offset);
   if(bindings[i]!="" && bindings[i]!=binding) {
      string prefix=(string)ExpectedLogin+":"+ExpectedServer+":"+TradeSymbol+":"+NativeN(entry)+":"+(buy?"BUY":"SELL")+":";
      if(StringFind(bindings[i],prefix)!=0){healthy=false;return;}
      reached[i]=0;tick_cursors[i]=0;fingerprints[i]="";
   }
   bindings[i]=binding;
   double advance=buy?price-entry:entry-price;
   int stage=reached[i];
   ulong next_cursor=tick_cursors[i];string tick_history="CURRENT_QUOTE_ONLY";
   ulong entry_stamp=(ulong)PositionGetInteger(POSITION_TIME_MSC);
   if(entry_stamp>0 && (ulong)tick.time_msc>entry_stamp) {
      ulong from=next_cursor>entry_stamp?next_cursor:entry_stamp+1;
      ulong through=from+5000;if(through>(ulong)tick.time_msc)through=(ulong)tick.time_msc;
      if(from<=through) {
         MqlTick history[2048];ResetLastError();
         int count=CopyTicksRange(TradeSymbol,history,COPY_TICKS_INFO,from,through);
         int history_error=GetLastError();
         if(count>=0)for(int j=0;j<count && j<2048;j++) {
            double observed=buy?history[j].bid:history[j].ask;
            if(history[j].time_msc<=(long)entry_stamp || !PolicyPositive(observed))continue;
            double move=buy?observed-entry:entry-observed;
            if(move+1e-10>=monitor_second)stage=2;
            else if(move+1e-10>=monitor_first && stage<1)stage=1;
         }
         if(count>=0 && history_error==0){next_cursor=through;tick_history="OBSERVED_WINDOW";}
         else tick_history="INCOMPLETE_RETRY_WINDOW"; // never advance across truncated/error data
      }
   }
   if(advance+1e-10>=monitor_second)stage=2;
   else if(advance+1e-10>=monitor_first && stage<1)stage=1;
   bool actionable=SupportAtStage(buy,entry,price,sl,stage,candidate,monitor_first_offset,monitor_second_offset);
   // Retain observed crossings; invalidate unexecutable proposals on CURRENT
   // quote/SL/TP. The retained stage may produce a valid proposal later.
   string status=actionable?"RECOMMENDED":"NO_ACTION";
   if(actionable && tp>0 && (buy?candidate>=tp:candidate<=tp))status="TP_CONFLICT";
   if(status!="RECOMMENDED")candidate=0;
   string fingerprint=NativeN(sl)+":"+NativeN(tp)+":"+NativeN(candidate)+":"+NativeN(volume)+":"+status+":"+(string)stage;
   if(fingerprints[i]!=fingerprint || next_cursor!=tick_cursors[i]) {
      ulong next=revisions[i]+(fingerprints[i]!=fingerprint?1:0);
      if(next==0 || next>9007199254740991)return;
      // Reserve a revision before publication. A crash may leave a gap, never
      // a reused ID. Missing publication is not evidence of broker execution.
      if(!NativePublish(MailboxFolder,RevisionFile(pid),(string)next+"|"+(string)stage+"|"+binding+"|"+(string)next_cursor)) {healthy=false;return;}
      revisions[i]=next;tick_cursors[i]=next_cursor;reached[i]=stage;fingerprints[i]=fingerprint;
   }
   string body="{\"protocol\":\"native_sl_proposal_v1\",\"login\":"+NativeU((ulong)ExpectedLogin)+
      ",\"server\":"+NativeQ(ExpectedServer)+",\"symbol\":"+NativeQ(TradeSymbol)+
      ",\"cycle_id\":"+NativeQ(position_cycle)+",\"position_id\":"+NativeU(pid)+
      ",\"first_threshold\":"+NativeN(monitor_first)+",\"second_threshold\":"+NativeN(monitor_second)+",\"first_offset\":"+NativeN(monitor_first_offset)+",\"second_offset\":"+NativeN(monitor_second_offset)+",\"revision\":"+NativeU(revisions[i])+",\"status\":"+NativeQ(status)+
      ",\"ownership_verified\":false,\"broker_confirmed\":false,\"side\":"+NativeQ(buy?"BUY":"SELL")+
      ",\"entry\":"+NativeN(entry)+",\"volume\":"+NativeN(volume)+
      ",\"expected_sl\":"+NativeN(sl)+",\"expected_tp\":"+NativeN(tp)+
      ",\"recommended_sl\":"+NativeN(candidate)+",\"quote\":"+NativeN(price)+
      ",\"tick_history\":"+NativeQ(tick_history)+",\"quote_time_msc\":"+NativeU((ulong)tick.time_msc)+",\"terminal_local_time\":"+(string)TimeLocal()+"}";
   if(!NativePublish(MailboxFolder,"sl-"+position_cycle+"-"+(string)pid+".json",body))healthy=false;
}
bool RefreshSLWatch() {
 if(CycleId!="")return true;
 ulong ids[];string cycle;
 if(!NativeManifest(MailboxFolder,ExpectedLogin,ExpectedServer,TradeSymbol,ids,cycle))return false;
 bool changed=cycle!=active_cycle || ArraySize(ids)!=ArraySize(watched);
 if(!changed)for(int i=0;i<ArraySize(ids);i++)if(ids[i]!=watched[i])changed=true;
 if(!changed)return true;
 int n=ArraySize(ids);active_cycle=cycle;cursor=0;
 ArrayResize(watched,n);ArrayResize(revisions,n);ArrayResize(fingerprints,n);ArrayResize(bindings,n);ArrayResize(reached,n);ArrayResize(tick_cursors,n);
 for(int i=0;i<n;i++){watched[i]=ids[i];fingerprints[i]="";if(!LoadRevision(i)){healthy=false;return false;}}
 return true;
}
void Scan() {
   ReadMonitorSettings();
   if(!healthy || !NativeIdentity(ExpectedLogin,ExpectedServer) || !RefreshSLWatch())return;
   if(!TerminalInfoInteger(TERMINAL_CONNECTED)) {quote_changed_since_start=false;return;}
   MqlTick tick;if(!SymbolInfoTick(TradeSymbol,tick) || tick.time_msc<=0 ||
      !PolicyPositive(tick.bid) || !PolicyPositive(tick.ask) || tick.ask<tick.bid)return;
   ulong now=GetTickCount64();
   if(tick.time_msc!=quote_time || tick.bid!=quote_bid || tick.ask!=quote_ask) {
      if(quote_time!=0)quote_changed_since_start=true;
      quote_time=tick.time_msc;quote_bid=tick.bid;quote_ask=tick.ask;quote_seen=now;
   }
   if(!quote_changed_since_start)return;
   // At most 10 positions per callback; bounded tick-history windows. A timer repeats
   // observation even if ticks/events are coalesced. Broker validation is still
   // required at dispatch; publication does not reserve the quoted price.
   for(int k=0;k<10 && k<ArraySize(watched);k++) {
      Recommend(cursor,tick);cursor=(cursor+1)%ArraySize(watched);
      if(!healthy)break;
   }
}
int OnInit() {
   monitor_first=FirstThreshold;monitor_second=SecondThreshold;monitor_first_offset=FirstOffset;monitor_second_offset=SecondOffset;ReadMonitorSettings();
   if(!NativeFolder(MailboxFolder) || (CycleId!="" && !NativeFolder(CycleId)) || StringFind(ExpectedServer,"|")>=0 ||
      StringFind(ExpectedServer,"\n")>=0 || StringFind(ExpectedServer,"\r")>=0 || StringFind(TradeSymbol,"|")>=0 ||
      !NativeIdentity(ExpectedLogin,ExpectedServer) || _Symbol!=TradeSymbol ||
      !PolicyPositive(FirstThreshold) || SecondThreshold<=FirstThreshold || !PolicyPositive(SecondThreshold) ||
      !MathIsValidNumber(FirstOffset) || !MathIsValidNumber(SecondOffset) || SecondOffset<FirstOffset) return INIT_PARAMETERS_INCORRECT;
   string ids[];int n=PositionIdentifiers==""?0:StringSplit(PositionIdentifiers,',',ids);
   if(n>128 || (CycleId!="" && n==0))return INIT_PARAMETERS_INCORRECT;
   active_cycle=CycleId;
   FolderCreate(MailboxFolder,FILE_COMMON);
   owner=FileOpen(MailboxFolder+"\\sl-monitor.lock",FILE_READ|FILE_WRITE|FILE_BIN|FILE_COMMON);
   if(owner==INVALID_HANDLE)return INIT_FAILED;
   ArrayResize(watched,n);ArrayResize(revisions,n);ArrayResize(fingerprints,n);ArrayResize(reached,n);ArrayResize(tick_cursors,n);ArrayResize(bindings,n);
   for(int i=0;i<n;i++) {
      if(!NativeId(ids[i])) {FileClose(owner);owner=INVALID_HANDLE;return INIT_PARAMETERS_INCORRECT;}
      watched[i]=(ulong)StringToInteger(ids[i]);
      for(int j=0;j<i;j++)if(watched[j]==watched[i]) {FileClose(owner);owner=INVALID_HANDLE;return INIT_PARAMETERS_INCORRECT;}
      if(!LoadRevision(i)) {FileClose(owner);owner=INVALID_HANDLE;return INIT_FAILED;}
   }
   if(!EventSetMillisecondTimer(100)) {FileClose(owner);owner=INVALID_HANDLE;return INIT_FAILED;}
   return INIT_SUCCEEDED;
}
void OnTick(){quote_changed_since_start=true;Scan();}
void OnTimer(){Scan();}
void OnDeinit(const int reason){EventKillTimer();if(owner!=INVALID_HANDLE)FileClose(owner);}
