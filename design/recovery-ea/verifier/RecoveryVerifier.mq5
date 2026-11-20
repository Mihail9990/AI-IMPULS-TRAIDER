#property strict
#property version "0.12"
// Offline integration source. Terminal and broker reads only.
input long ExpectedLogin=0;
input string ExpectedServer="";
input string ExpectedSymbol="";
input long StrategyMagic=0;
input string VerifierEpoch=""; // Unique 32-hex launch token from installer/controller.
input string ExecutorEpoch=""; // Current allowed peer; rotate both in a new test session.

const string Mailbox="RECOVERY_VERIFY";
const int ScanLimit=128;
const int RequestLimit=2048;
const ulong MaxResponse=65536;
long SearchHandle=INVALID_HANDLE;
string SearchFilename="";
long Sequence=0;
long HistorySearchHandle=INVALID_HANDLE;
string HistorySearchFilename="";
long OrderSearchHandle=INVALID_HANDLE;
string OrderSearchFilename="";
enum VerifierChannel { VERIFIER_INVENTORY, VERIFIER_HISTORY, VERIFIER_EXACT_ORDER };
string RequestSuffix(const VerifierChannel channel) {
   if(channel==VERIFIER_HISTORY) return ".hist";
   if(channel==VERIFIER_EXACT_ORDER) return ".ord";
   return ".inv";
}
string ResponseSuffix(const VerifierChannel channel) {
   if(channel==VERIFIER_HISTORY) return ".history";
   if(channel==VERIFIER_EXACT_ORDER) return ".order";
   return ".snapshot";
}

bool SafeText(const string value) {
   if(StringLen(value)<1 || StringLen(value)>100) return false;
   for(int i=0;i<StringLen(value);i++) {
      ushort c=StringGetCharacter(value,i);
      if(c<32 || c==127 || c==0xfffd) return false;
   }
   return true;
}
bool HexToken(const string value) {
   if(StringLen(value)!=32) return false;
   for(int i=0;i<32;i++) if(StringFind("0123456789abcdef",StringSubstr(value,i,1))<0) return false;
   return true;
}
bool SessionOK() {
   return TerminalInfoInteger(TERMINAL_CONNECTED)!=0 &&
          AccountInfoInteger(ACCOUNT_LOGIN)==ExpectedLogin &&
          AccountInfoString(ACCOUNT_SERVER)==ExpectedServer;
}
bool NonNegative(const double value) {
   return MathIsValidNumber(value) && value>=0 && value<1e15;
}
string Number(const double value) { return DoubleToString(value==0 ? 0.0 : value,16); }
void CloseSearch() {
   if(SearchHandle!=INVALID_HANDLE) FileFindClose(SearchHandle);
   SearchHandle=INVALID_HANDLE;
   SearchFilename="";
}

int OnInit() {
   if(ExpectedLogin<=0 || StrategyMagic<=0 || !SafeText(ExpectedServer) || !SafeText(ExpectedSymbol) ||
      ExpectedSymbol!=_Symbol || !HexToken(VerifierEpoch) || !HexToken(ExecutorEpoch) ||
      VerifierEpoch==ExecutorEpoch || ChartID()<=0) return INIT_PARAMETERS_INCORRECT;
   FolderCreate(Mailbox);
   if(!EventSetTimer(1)) return INIT_FAILED;
   PrintFormat("RECOVERY verifier ready to observe; chart ID %I64d",ChartID());
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason) {
   EventKillTimer();
   CloseSearch();
   CloseCursor(HistorySearchHandle,HistorySearchFilename);
   CloseCursor(OrderSearchHandle,OrderSearchFilename);
}

bool RecoveryHistoryId(const string value,ulong &number) {
   int n=StringLen(value);
   const string max="18446744073709551615";
   number=0;
   if(n<1 || n>20 || StringGetCharacter(value,0)==48 ||
      (n==20 && StringCompare(value,max)>0)) return false;
   for(int i=0;i<n;i++) {
      ushort c=StringGetCharacter(value,i);
      if(c<48 || c>57) return false;
      number=number*10+(ulong)(c-48);
   }
   return true;
}
bool LoadRequest(const string filename,string &nonce,string &generation,
                 const VerifierChannel channel,ulong &position,ulong &anchor) {
   nonce=StringSubstr(filename,0,32);
   if(!HexToken(nonce) || filename!=nonce+RequestSuffix(channel)) return false;
   position=0; anchor=0;
   int h=FileOpen(Mailbox+"\\"+filename,FILE_READ|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   ulong size=FileSize(h);
   if(size<2 || size>(ulong)RequestLimit) { FileClose(h); return false; }
   uchar bytes[];
   uint copied=FileReadArray(h,bytes,0,(int)size);
   FileClose(h);
   if(copied!=(uint)size || bytes[(int)size-1]!=10) return false;
   int end=(int)size-1;
   if(bytes[end-1]==13) end--;
   for(int i=0;i<end;i++) if(bytes[i]<32 && bytes[i]!=9) return false;
   string line=CharArrayToString(bytes,0,end,CP_UTF8);
   uchar canonical[];
   if(StringToCharArray(line,canonical,0,WHOLE_ARRAY,CP_UTF8)!=end+1) return false;
   for(int i=0;i<end;i++) if(canonical[i]!=bytes[i]) return false;
   string f[];
   const int count=channel==VERIFIER_HISTORY ? 12 : (channel==VERIFIER_EXACT_ORDER ? 11 : 10);
   string schema="RECOVERY_INVENTORY_REQUEST_V1";
   if(channel==VERIFIER_HISTORY) schema="RECOVERY_HISTORY_REQUEST_V1";
   if(channel==VERIFIER_EXACT_ORDER) schema="RECOVERY_EXACT_ORDER_REQUEST_V1";
   if(StringSplit(line,9,f)!=count) return false;
   if(f[0]!=schema || f[1]!=nonce || f[2]!=ExecutorEpoch ||
      f[3]!=VerifierEpoch || f[5]!=(string)ExpectedLogin || f[6]!=ExpectedServer ||
      f[7]!=ExpectedSymbol || f[8]!=(string)StrategyMagic || f[count-1]!="END") return false;
   if(channel==VERIFIER_HISTORY && (!RecoveryHistoryId(f[9],position) || !RecoveryHistoryId(f[10],anchor))) return false;
   if(channel==VERIFIER_EXACT_ORDER && !RecoveryHistoryId(f[9],anchor)) return false;
   long gen=StringToInteger(f[4]);
   if(gen<0 || f[4]!=(string)gen) return false;
   generation=f[4];
   return true;
}

bool PositionRow(const int index,ulong &ticket,long &identifier,string &row) {
   row="";
   ticket=PositionGetTicket(index);
   if(ticket==0 || !PositionSelectByTicket(ticket)) return false;
   string symbol;
   long actual,magic,side;
   double volume,price,sl,tp;
   if(!PositionGetString(POSITION_SYMBOL,symbol)) return false;
   if(symbol!=ExpectedSymbol) return true;
   if(!PositionGetInteger(POSITION_TICKET,actual) || (ulong)actual!=ticket ||
      !PositionGetInteger(POSITION_IDENTIFIER,identifier) || identifier<=0 ||
      !PositionGetInteger(POSITION_MAGIC,magic) ||
      !PositionGetInteger(POSITION_TYPE,side) || (side!=POSITION_TYPE_BUY && side!=POSITION_TYPE_SELL) ||
      !PositionGetDouble(POSITION_VOLUME,volume) || !NonNegative(volume) || volume<=0 ||
      !PositionGetDouble(POSITION_PRICE_OPEN,price) || !NonNegative(price) || price<=0 ||
      !PositionGetDouble(POSITION_SL,sl) || !NonNegative(sl) ||
      !PositionGetDouble(POSITION_TP,tp) || !NonNegative(tp)) return false;
   row="P\t"+(string)identifier+"\t"+(string)ticket+"\t"+(string)(ulong)magic+
       "\t"+(side==POSITION_TYPE_BUY ? "BUY" : "SELL")+"\t"+Number(volume)+"\t"+
       Number(price)+"\t"+Number(sl)+"\t"+Number(tp)+"\n";
   return true;
}

bool OrderRow(const int index,ulong &ticket,string &row) {
   row="";
   ticket=OrderGetTicket(index);
   if(ticket==0 || !OrderSelect(ticket)) return false;
   string symbol;
   long actual,magic,position,type,state;
   double initial,current,price,sl,tp,stoplimit;
   if(!OrderGetString(ORDER_SYMBOL,symbol)) return false;
   if(symbol!=ExpectedSymbol) return true;
   if(!OrderGetInteger(ORDER_TICKET,actual) || (ulong)actual!=ticket ||
      !OrderGetInteger(ORDER_MAGIC,magic) ||
      !OrderGetInteger(ORDER_POSITION_ID,position) || position<0 ||
      !OrderGetInteger(ORDER_TYPE,type) || type<0 ||
      !OrderGetInteger(ORDER_STATE,state) || state<0 ||
      !OrderGetDouble(ORDER_VOLUME_INITIAL,initial) || !NonNegative(initial) || initial<=0 ||
      !OrderGetDouble(ORDER_VOLUME_CURRENT,current) || !NonNegative(current) || current>initial ||
      !OrderGetDouble(ORDER_PRICE_OPEN,price) || !NonNegative(price) ||
      !OrderGetDouble(ORDER_SL,sl) || !NonNegative(sl) ||
      !OrderGetDouble(ORDER_TP,tp) || !NonNegative(tp) ||
      !OrderGetDouble(ORDER_PRICE_STOPLIMIT,stoplimit) || !NonNegative(stoplimit)) return false;
   row="O\t"+(string)ticket+"\t"+(string)(ulong)magic+"\t"+(string)position+"\t"+
       (string)type+"\t"+(string)state+"\t"+Number(initial)+"\t"+Number(current)+"\t"+
       Number(price)+"\t"+Number(sl)+"\t"+Number(tp)+"\t"+Number(stoplimit)+"\n";
   return true;
}

string Collect(string &rows,int &positions,int &orders) {
   rows="";
   positions=0;
   orders=0;
   if(!SessionOK()) return "session";
   int total_positions=PositionsTotal(), total_orders=OrdersTotal();
   if(total_positions<0 || total_orders<0) return "read";
   if(total_positions>ScanLimit || total_orders>ScanLimit) return "limit";
   ulong ticket,position_tickets[],order_tickets[];
   long identifier,position_ids[];
   if(ArrayResize(position_tickets,total_positions)!=total_positions ||
      ArrayResize(position_ids,total_positions)!=total_positions ||
      ArrayResize(order_tickets,total_orders)!=total_orders) return "read";
   string row;
   for(int i=0;i<total_positions;i++) {
      identifier=0;
      if(!PositionRow(i,ticket,identifier,row)) return "read";
      for(int j=0;j<i;j++)
         if(position_tickets[j]==ticket || (identifier>0 && position_ids[j]==identifier)) return "changed";
      position_tickets[i]=ticket;
      position_ids[i]=identifier;
      if(row!="") { rows+=row; positions++; }
   }
   for(int i=0;i<total_orders;i++) {
      if(!OrderRow(i,ticket,row)) return "read";
      for(int j=0;j<i;j++) if(order_tickets[j]==ticket) return "changed";
      order_tickets[i]=ticket;
      if(row!="") { rows+=row; orders++; }
   }
   if(!SessionOK()) return "session";
   if(PositionsTotal()!=total_positions || OrdersTotal()!=total_orders) return "changed";
   return "ok";
}

void Publish(const string nonce,const string generation) {
   string first,second;
   int positions,orders,check_positions,check_orders;
   string result=Collect(first,positions,orders);
   if(result=="ok") {
      result=Collect(second,check_positions,check_orders);
      if(result=="ok" && (first!=second || positions!=check_positions || orders!=check_orders)) result="changed";
   }
   if(!SessionOK()) return;
   if(result!="ok") { first=""; positions=0; orders=0; }
   string payload=ReportPrefix("RECOVERY_INVENTORY_V1",nonce,generation)+
      (result=="ok" ? "complete" : "unavailable")+"\t"+result+"\t"+
      (string)positions+"\t"+(string)orders+"\n"+first+"END\n";
   WriteReport(nonce,".snapshot",payload);
}
#include "RecoveryHistory.mqh"
#include "RecoveryExactOrderWire.mqh"
string ReportPrefix(const string schema,const string nonce,const string generation) {
   return schema+"\t"+nonce+"\t"+ExecutorEpoch+"\t"+VerifierEpoch+"\t"+
      generation+"\t"+(string)ExpectedLogin+"\t"+ExpectedServer+"\t"+ExpectedSymbol+"\t"+
      (string)StrategyMagic+"\t"+(string)ChartID()+"\t"+(string)(++Sequence)+"\t";
}
void WriteReport(const string nonce,const string suffix,const string payload) {
   const string prefix=Mailbox+"\\"+nonce;
   const string temp=prefix+suffix+"."+VerifierEpoch+"."+(string)ChartID()+".tmp";
   uchar bytes[];
   int count=StringToCharArray(payload,bytes,0,WHOLE_ARRAY,CP_UTF8)-1; // exclude terminating NUL
   if(count<=0 || (ulong)count>MaxResponse) return;
   int h=FileOpen(temp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return;
   uint written=FileWriteArray(h,bytes,0,count);
   FileFlush(h);
   FileClose(h);
   // Neither replacement nor atomicity is assumed. Verifying reader checks END and row counts.
   if(written!=(uint)count || !SessionOK() || !FileMove(temp,0,prefix+suffix,0)) FileDelete(temp);
}
void PublishHistory(const string nonce,const string generation,const ulong position,const ulong anchor) {
   string rows;
   int orders,deals;
   string result=RecoveryHistoryObserve(position,anchor,ExpectedSymbol,rows,orders,deals);
   if(!SessionOK()) return;
   string payload=ReportPrefix("RECOVERY_HISTORY_V1",nonce,generation)+(string)position+"\t"+(string)anchor+"\t"+
      (result=="ok" ? "observed" : "unavailable")+"\t"+result+"\t"+
      (string)orders+"\t"+(string)deals+"\n"+rows+"END\n";
   WriteReport(nonce,".history",payload);
}

void PublishExact(const string nonce,const string generation,const ulong ticket) {
   RecoveryExactOrderScope scope;
   scope.login=ExpectedLogin; scope.server=ExpectedServer; scope.symbol=ExpectedSymbol;
   scope.magic=(ulong)StrategyMagic; scope.ticket=ticket;
   RecoveryExactOrderObservation observation;
   RecoveryObserveExactOrder(scope,observation);
   if(!SessionOK()) return;
   string body=RecoveryExactOrderResponseBody(scope,observation);
   if(body=="") return;
   WriteReport(nonce,".order",ReportPrefix("RECOVERY_EXACT_ORDER_V1",nonce,generation)+body);
}

void CloseCursor(long &handle,string &filename) {
   if(handle!=INVALID_HANDLE) FileFindClose(handle);
   handle=INVALID_HANDLE;
   filename="";
}
void ScanChannel(long &handle,string &filename,const VerifierChannel channel) {
   if(handle==INVALID_HANDLE) {
      handle=FileFindFirst(Mailbox+"\\*"+RequestSuffix(channel),filename);
      if(handle==INVALID_HANDLE) return;
   }
   for(int visited=0;visited<32;visited++) {
      string nonce,generation;
      ulong position,anchor;
      bool new_work=LoadRequest(filename,nonce,generation,channel,position,anchor) &&
                    !FileIsExist(Mailbox+"\\"+nonce+ResponseSuffix(channel));
      if(new_work) {
         if(channel==VERIFIER_HISTORY) PublishHistory(nonce,generation,position,anchor);
         else if(channel==VERIFIER_EXACT_ORDER) PublishExact(nonce,generation,anchor);
         else Publish(nonce,generation);
      }
      if(!FileFindNext(handle,filename)) { CloseCursor(handle,filename); return; }
      if(new_work) return; // One full probe max per channel per timer event.
   }
}
void OnTimer() {
   if(!SessionOK()) {
      CloseSearch();
      CloseCursor(HistorySearchHandle,HistorySearchFilename);
      CloseCursor(OrderSearchHandle,OrderSearchFilename);
      return;
   }
   ScanChannel(OrderSearchHandle,OrderSearchFilename,VERIFIER_EXACT_ORDER);
   ScanChannel(HistorySearchHandle,HistorySearchFilename,VERIFIER_HISTORY);
   ScanChannel(SearchHandle,SearchFilename,VERIFIER_INVENTORY);
}
