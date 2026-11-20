#property strict
#property version "0.10"
#include "RecoveryPriceStage.mqh"
// Third EA, reads one observed position and the quote, then writes a hint to
// the main EA's local mailbox. No orders, web commands or position mutations.
input long ExpectedLogin=0;
input string ExpectedServer="";
input string ExpectedSymbol="";
input string ExecutorEpoch="";
input string WatcherEpoch="";

const string PriceBox="RECOVERY_PRICE";
long ScanHandle=INVALID_HANDLE, Sequence=0;
string ScanName="";
struct PriceQuery {
   string nonce,generation,actual_entry,first,second;
   ulong position,ticket;
   int side;
};
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

bool PriceLoad(const string name,PriceQuery &query) {
   const string nonce=StringSubstr(name,0,32);
   if(!PriceHex(nonce) || name!=nonce+".watch") return false;
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
   if(StringSplit(wire,9,f)!=16 || f[0]!="RECOVERY_PRICE_REQUEST_V1" || f[1]!=nonce ||
      f[2]!=ExecutorEpoch || f[3]!=WatcherEpoch || !PriceUnsigned(f[4],generation) || generation==0 ||
      f[5]!=(string)ExpectedLogin || f[6]!=ExpectedServer || f[7]!=ExpectedSymbol ||
      !PriceUnsigned(f[8],position) || !PriceUnsigned(f[9],ticket) || position==0 || ticket==0 ||
      (f[10]!="BUY" && f[10]!="SELL") || !PriceNumber(f[11]) ||
      !PriceNumber(f[12]) || !PriceNumber(f[13]) || StringToDouble(f[12])>=StringToDouble(f[13]) ||
      f[14]!=(string)ChartID() || f[15]!="END") return false;
   query.nonce=nonce; query.generation=f[4]; query.position=position; query.ticket=ticket;
   query.side=f[10]=="BUY" ? 1 : -1; query.actual_entry=f[11]; query.first=f[12]; query.second=f[13];
   return true;
}
bool PricePosition(const PriceQuery &q,double &entry) {
   long id,ticket,side;
   string symbol;
   if(!PriceSession() || !PositionSelectByTicket(q.ticket) ||
      !PositionGetInteger(POSITION_TICKET,ticket) || (ulong)ticket!=q.ticket ||
      !PositionGetInteger(POSITION_IDENTIFIER,id) || (ulong)id!=q.position ||
      !PositionGetString(POSITION_SYMBOL,symbol) || symbol!=ExpectedSymbol ||
      !PositionGetInteger(POSITION_TYPE,side) ||
      side!=(q.side==1 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL) ||
      !PositionGetDouble(POSITION_PRICE_OPEN,entry) || !RecoveryPricePositive(entry) ||
      PriceWire(entry)!=q.actual_entry) return false;
   return PriceSession();
}
void PricePublish(const PriceQuery &q) {
   double entry,check;
   MqlTick tick;
   int stage=-1;
   if(PricePosition(q,entry) && SymbolInfoTick(ExpectedSymbol,tick) &&
      PricePosition(q,check) && check==entry)
      stage=RecoveryPriceStage(q.side,entry,tick.bid,tick.ask,StringToDouble(q.first),StringToDouble(q.second));
   if(!PriceSession()) return;
   const string prefix=PriceBox+"\\"+q.nonce;
   const string temp=prefix+"."+WatcherEpoch+"."+(string)ChartID()+".tmp";
   const string observation=stage>=0 ?
      "observed\t"+(string)stage+"\t"+PriceWire(q.side==1 ? tick.bid : tick.ask) :
      "unavailable\t-1\t0.0000000000000000";
   const string response="RECOVERY_PRICE_V1\t"+q.nonce+"\t"+ExecutorEpoch+"\t"+WatcherEpoch+"\t"+
      q.generation+"\t"+(string)ExpectedLogin+"\t"+ExpectedServer+"\t"+ExpectedSymbol+"\t"+
      (string)ChartID()+"\t"+(string)(++Sequence)+"\t"+(string)q.position+"\t"+(string)q.ticket+"\t"+
      (q.side==1 ? "BUY" : "SELL")+"\t"+q.actual_entry+"\t"+q.first+"\t"+q.second+"\t"+observation+"\tEND\n";
   uchar bytes[];
   const int count=StringToCharArray(response,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(count<=0 || count>2048) return;
   const int h=FileOpen(temp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return;
   const uint written=FileWriteArray(h,bytes,0,count);
   FileFlush(h); FileClose(h);
   if(written!=(uint)count || !PriceSession() || !FileMove(temp,0,prefix+".quote",0)) FileDelete(temp);
}
void OnTimer() {
   if(!PriceSession()) { PriceCloseSearch(); return; }
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
