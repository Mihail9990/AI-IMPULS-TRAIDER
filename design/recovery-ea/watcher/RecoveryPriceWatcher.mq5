#property strict
#property version "0.12"
#include "RecoveryPriceCatchup.mqh"
// Third EA, reads one observed position and the quote, then writes a hint to
// the main EA's local mailbox. No orders, web commands or position mutations.
input long ExpectedLogin=0;
input string ExpectedServer="";
input string ExpectedSymbol="";
input string ExecutorEpoch="";
input string WatcherEpoch="";

const string PriceBox="RECOVERY_PRICE";
long ScanHandle=INVALID_HANDLE, FollowHandle=INVALID_HANDLE, Sequence=0;
string ScanName="", FollowName="";
struct PriceQuery {
   string nonce,generation,actual_entry,first,second;
   ulong position,ticket;
   int side,next_stage;
};
struct PriceSubscription {
   bool active;
   PriceQuery query;
   RecoveryPricePeak peak;
   long anchor, opened;
   ulong cursor;
   double volume;
};
PriceSubscription Subs[4];
int CatchupSlot=0;
bool PriceHex(const string token) {
   if(StringLen(token)!=32) return false;
   for(int i=0;i<32;i++)
      if(StringFind("0123456789abcdef",StringSubstr(token,i,1))<0) return false;
   return true;
}
bool PriceText(const string value) {
   if(StringLen(value)==0 || StringLen(value)>100) return false;
   for(int i=0;i<StringLen(value);i++) {
      const ushort c=StringGetCharacter(value,i);
      if(c<32 || c==127 || c==0xfffd) return false;
   }
   return true;
}
bool PriceSession() {
   return TerminalInfoInteger(TERMINAL_CONNECTED)!=0 && AccountInfoInteger(ACCOUNT_LOGIN)==ExpectedLogin &&
          AccountInfoString(ACCOUNT_SERVER)==ExpectedServer;
}
bool PriceUnsigned(const string token,ulong &number) {
   number=0;
   const int n=StringLen(token);
   if(n<1 || n>20 || (n>1 && StringGetCharacter(token,0)==48) ||
      (n==20 && StringCompare(token,"18446744073709551615")>0)) return false;
   for(int i=0;i<n;i++) {
      const ushort c=StringGetCharacter(token,i);
      if(c<48 || c>57) return false;
      number=number*10+(ulong)(c-48);
   }
   return true;
}
string PriceWire(const double value) { return DoubleToString(value,16); }
bool PriceNumber(const string value) {
   const double number=StringToDouble(value);
   return RecoveryPricePositive(number) && PriceWire(number)==value;
}
void PriceCloseSearch() {
   if(ScanHandle!=INVALID_HANDLE) FileFindClose(ScanHandle);
   ScanHandle=INVALID_HANDLE; ScanName="";
   if(FollowHandle!=INVALID_HANDLE) FileFindClose(FollowHandle);
   FollowHandle=INVALID_HANDLE; FollowName="";
}
int OnInit() {
   if(ExpectedLogin<=0 || !PriceText(ExpectedServer) || !PriceText(ExpectedSymbol) || ExpectedSymbol!=_Symbol ||
      !PriceHex(ExecutorEpoch) || !PriceHex(WatcherEpoch) || ExecutorEpoch==WatcherEpoch || ChartID()<=0)
      return INIT_PARAMETERS_INCORRECT;
   FolderCreate(PriceBox);
   if(!EventSetTimer(1)) return INIT_FAILED;
   PrintFormat("RECOVERY price watcher ready to observe; chart ID %I64d",ChartID());
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason) { EventKillTimer(); PriceCloseSearch(); }

bool PriceLoad(const string name,PriceQuery &query,const bool follow=false,const bool drop=false) {
   const string nonce=StringSubstr(name,0,32);
   const string suffix=drop ? ".drop" : (follow ? ".follow" : ".watch");
   if(!PriceHex(nonce) || name!=nonce+suffix) return false;
   const int h=FileOpen(PriceBox+"\\"+name,FILE_READ|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const ulong size=FileSize(h);
   if(size<2 || size>2048) { FileClose(h); return false; }
   uchar bytes[];
   const uint copied=FileReadArray(h,bytes,0,(int)size);
   FileClose(h);
   if(copied!=(uint)size || bytes[(int)size-1]!=10) return false;
   int end=(int)size-1;
   if(bytes[end-1]==13) end--;
   for(int i=0;i<end;i++) if(bytes[i]<32 && bytes[i]!=9) return false;
   const string wire=CharArrayToString(bytes,0,end,CP_UTF8);
   uchar canonical[];
   if(StringToCharArray(wire,canonical,0,WHOLE_ARRAY,CP_UTF8)!=end+1) return false;
   for(int i=0;i<end;i++) if(bytes[i]!=canonical[i]) return false;
   string f[];
   ulong generation,position,ticket;
   const int count=follow ? 17 : 16;
   const string schema=follow ? "RECOVERY_PRICE_FOLLOW_V2" : "RECOVERY_PRICE_REQUEST_V1";
   if(StringSplit(wire,9,f)!=count || f[0]!=schema || f[1]!=nonce ||
      f[2]!=ExecutorEpoch || f[3]!=WatcherEpoch || !PriceUnsigned(f[4],generation) || generation==0 ||
      f[5]!=(string)ExpectedLogin || f[6]!=ExpectedServer || f[7]!=ExpectedSymbol ||
      !PriceUnsigned(f[8],position) || !PriceUnsigned(f[9],ticket) || position==0 || ticket==0 ||
      (f[10]!="BUY" && f[10]!="SELL") || !PriceNumber(f[11]) ||
      !PriceNumber(f[12]) || !PriceNumber(f[13]) || StringToDouble(f[12])>=StringToDouble(f[13]) ||
      f[count-2]!=(string)ChartID() || f[count-1]!="END" ||
      (follow && f[14]!="1" && f[14]!="2")) return false;
   query.nonce=nonce; query.generation=f[4]; query.position=position; query.ticket=ticket;
   query.side=f[10]=="BUY" ? 1 : -1; query.actual_entry=f[11]; query.first=f[12]; query.second=f[13];
   query.next_stage=follow ? (int)StringToInteger(f[14]) : 0;
   return true;
}
bool PricePositionLineage(const PriceQuery &q,double &entry,long &opened,double &volume) {
   long id,ticket,side;
   string symbol;
   if(!PriceSession() || !PositionSelectByTicket(q.ticket) ||
      !PositionGetInteger(POSITION_TICKET,ticket) || (ulong)ticket!=q.ticket ||
      !PositionGetInteger(POSITION_IDENTIFIER,id) || (ulong)id!=q.position ||
      !PositionGetString(POSITION_SYMBOL,symbol) || symbol!=ExpectedSymbol ||
      !PositionGetInteger(POSITION_TYPE,side) ||
      side!=(q.side==1 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL) ||
      !PositionGetDouble(POSITION_PRICE_OPEN,entry) || !RecoveryPricePositive(entry) ||
      PriceWire(entry)!=q.actual_entry ||
      !PositionGetInteger(POSITION_TIME_MSC,opened) || opened<=0 ||
      !PositionGetDouble(POSITION_VOLUME,volume) || !RecoveryPricePositive(volume)) return false;
   return PriceSession();
}
int PriceSample(const PriceQuery &q,double &selected) {
   double entry,check,volume,check_volume; long opened,check_opened; MqlTick tick;
   selected=0.0;
   if(!PricePositionLineage(q,entry,opened,volume) || !SymbolInfoTick(ExpectedSymbol,tick) ||
      !PricePositionLineage(q,check,check_opened,check_volume) ||
      check!=entry || check_opened!=opened || check_volume!=volume) return -1;
   const int stage=RecoveryPriceStage(q.side,entry,tick.bid,tick.ask,StringToDouble(q.first),StringToDouble(q.second));
   if(stage>=0) selected=q.side==1 ? tick.bid : tick.ask;
   return stage;
}
bool PriceActivePosition(const int slot,double &entry) {
   long opened; double volume;
   return PricePositionLineage(Subs[slot].query,entry,opened,volume) &&
          opened==Subs[slot].opened && volume==Subs[slot].volume;
}
bool PriceInitFollow(const int slot,const PriceQuery &q) {
   MqlTick current; double entry,volume,check,check_volume; long opened,check_opened;
   if(!PricePositionLineage(q,entry,opened,volume) || !SymbolInfoTick(ExpectedSymbol,current) ||
      !PricePositionLineage(q,check,check_opened,check_volume) ||
      entry!=check || opened!=check_opened || volume!=check_volume ||
      !RecoveryPriceAnchor(opened,current.time_msc,Subs[slot].anchor,Subs[slot].cursor)) return false;
   Subs[slot].opened=opened; Subs[slot].volume=volume;
   return true;
}
bool PriceWrite(const PriceQuery &q,const int stage,const double selected) {
   if(!PriceSession()) return false;
   const bool follow=q.next_stage>0;
   const string suffix=follow ? ".peak" : ".quote";
   const string prefix=PriceBox+"\\"+q.nonce;
   const string temp=prefix+suffix+"."+WatcherEpoch+"."+(string)ChartID()+".tmp";
   const string observation=stage>=0 ?
      "observed\t"+(string)stage+"\t"+PriceWire(selected) : "unavailable\t-1\t0.0000000000000000";
   const string schema=follow ? "RECOVERY_PRICE_PEAK_V2" : "RECOVERY_PRICE_V1";
   const string requested=follow ? (string)q.next_stage+"\t" : "";
   const string response=schema+"\t"+q.nonce+"\t"+ExecutorEpoch+"\t"+WatcherEpoch+"\t"+
      q.generation+"\t"+(string)ExpectedLogin+"\t"+ExpectedServer+"\t"+ExpectedSymbol+"\t"+
      (string)ChartID()+"\t"+(string)(++Sequence)+"\t"+(string)q.position+"\t"+(string)q.ticket+"\t"+
      (q.side==1 ? "BUY" : "SELL")+"\t"+q.actual_entry+"\t"+q.first+"\t"+q.second+"\t"+
      requested+observation+"\tEND\n";
   uchar bytes[];
   const int count=StringToCharArray(response,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(count<=0 || count>2048) return false;
   const int h=FileOpen(temp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const uint written=FileWriteArray(h,bytes,0,count);
   FileFlush(h); FileClose(h);
   if(written==(uint)count && PriceSession() && FileMove(temp,0,prefix+suffix,0)) return true;
   FileDelete(temp);
   return false;
}
void PricePublish(const PriceQuery &q) {
   double selected;
   const int stage=PriceSample(q,selected);
   PriceWrite(q,stage,selected);
}
bool PriceSame(const PriceQuery &a,const PriceQuery &b) {
   return a.nonce==b.nonce && a.generation==b.generation && a.actual_entry==b.actual_entry &&
          a.first==b.first && a.second==b.second && a.position==b.position && a.ticket==b.ticket &&
          a.side==b.side && a.next_stage==b.next_stage;
}
void PriceCapture() {
   for(int i=0;i<4;i++) {
      if(!Subs[i].active) continue;
      double entry,check; MqlTick tick;
      if(!PriceActivePosition(i,entry) || !SymbolInfoTick(ExpectedSymbol,tick) ||
         tick.time_msc<=Subs[i].anchor || !PriceActivePosition(i,check) || check!=entry) continue;
      const int side=Subs[i].query.side;
      const int stage=RecoveryPriceStage(side,entry,tick.bid,tick.ask,
                                       StringToDouble(Subs[i].query.first),StringToDouble(Subs[i].query.second));
      RecoveryPriceAccumulate(Subs[i].peak,side,stage,side==1 ? tick.bid : tick.ask);
   }
}
void PriceCatchupOne() {
   // Bound terminal work to one history call per watcher event; the trading EA
   // never waits for CopyTicks or calls it on its fill/opening path.
   for(int attempt=0;attempt<4;attempt++) {
      const int i=CatchupSlot;
      CatchupSlot=(CatchupSlot+1)%4;
      if(!Subs[i].active) continue;
      double entry,check; MqlTick current;
      if(!PriceActivePosition(i,entry) || !SymbolInfoTick(ExpectedSymbol,current) ||
         current.time_msc<=Subs[i].anchor || (ulong)current.time_msc<Subs[i].cursor) continue;
      MqlTick ticks[];
      ResetLastError();
      const int n=CopyTicks(ExpectedSymbol,ticks,COPY_TICKS_ALL,Subs[i].cursor,(uint)RECOVERY_PRICE_TICK_CAP);
      const bool synchronized=GetLastError()==0;
      if(n>0 && PriceActivePosition(i,check) && check==entry)
         RecoveryPriceCatchupFold(ticks,n,synchronized,Subs[i].query.side,entry,
            StringToDouble(Subs[i].query.first),StringToDouble(Subs[i].query.second),
            Subs[i].anchor,current.time_msc,Subs[i].cursor,Subs[i].peak);
      return;
   }
}
void OnTick() {
   PriceCapture();
   PriceCatchupOne();
}
void PriceFollowService() {
   for(int i=0;i<4;i++) {
      if(!Subs[i].active) continue;
      PriceQuery drop;
      if(PriceLoad(Subs[i].query.nonce+".drop",drop,true,true) && PriceSame(Subs[i].query,drop)) {
         Subs[i].active=false;
         continue;
      }
      double exact_entry;
      if(RecoveryPriceReached(Subs[i].peak,Subs[i].query.next_stage) &&
         PriceActivePosition(i,exact_entry) &&
         PriceWrite(Subs[i].query,Subs[i].peak.stage,Subs[i].peak.selected))
         Subs[i].active=false;
   }
}
void PriceFollowScan() {
   int slot=-1;
   for(int i=0;i<4;i++) if(!Subs[i].active) { slot=i; break; }
   if(slot<0) return;
   if(FollowHandle==INVALID_HANDLE) {
      FollowHandle=FileFindFirst(PriceBox+"\\*.follow",FollowName);
      if(FollowHandle==INVALID_HANDLE) return;
   }
   for(int visited=0;visited<32;visited++) {
      PriceQuery q,drop;
      bool work=PriceLoad(FollowName,q,true) && !FileIsExist(PriceBox+"\\"+q.nonce+".peak");
      if(work) {
         for(int i=0;i<4;i++) if(Subs[i].active && Subs[i].query.nonce==q.nonce) work=false;
         if(work && PriceLoad(q.nonce+".drop",drop,true,true) && PriceSame(q,drop)) work=false;
      }
      if(work && !PriceInitFollow(slot,q)) work=false;
      if(work) {
         Subs[slot].query.nonce=q.nonce; Subs[slot].query.generation=q.generation;
         Subs[slot].query.actual_entry=q.actual_entry; Subs[slot].query.first=q.first; Subs[slot].query.second=q.second;
         Subs[slot].query.position=q.position; Subs[slot].query.ticket=q.ticket;
         Subs[slot].query.side=q.side; Subs[slot].query.next_stage=q.next_stage;
         Subs[slot].active=true;
         RecoveryPriceReset(Subs[slot].peak);
      }
      if(!FileFindNext(FollowHandle,FollowName)) {
         FileFindClose(FollowHandle); FollowHandle=INVALID_HANDLE; FollowName=""; return;
      }
      if(work) return;
   }
}
void OnTimer() {
   if(!PriceSession()) { PriceCloseSearch(); return; }
   PriceFollowScan();
   PriceCapture();
   PriceCatchupOne();
   PriceFollowService();
   if(ScanHandle==INVALID_HANDLE) {
      ScanHandle=FileFindFirst(PriceBox+"\\*.watch",ScanName);
      if(ScanHandle==INVALID_HANDLE) return;
   }
   for(int visited=0;visited<32;visited++) {
      PriceQuery query;
      const bool work=PriceLoad(ScanName,query) && !FileIsExist(PriceBox+"\\"+query.nonce+".quote");
      if(work) PricePublish(query);
      if(!FileFindNext(ScanHandle,ScanName)) { PriceCloseSearch(); return; }
      if(work) return;
   }
}
