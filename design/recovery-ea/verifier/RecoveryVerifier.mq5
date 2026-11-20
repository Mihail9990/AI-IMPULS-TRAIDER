#property strict
#property version "0.10"
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
   return EventSetTimer(1) ? INIT_SUCCEEDED : INIT_FAILED;
}
void OnDeinit(const int reason) { EventKillTimer(); CloseSearch(); }

bool LoadRequest(const string filename,string &nonce,string &generation) {
   if(StringLen(filename)!=36) return false;
   nonce=StringSubstr(filename,0,32);
   if(!HexToken(nonce) || filename!=nonce+".inv") return false;
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
   if(StringSplit(line,9,f)!=10) return false;
   if(f[0]!="RECOVERY_INVENTORY_REQUEST_V1" || f[1]!=nonce || f[2]!=ExecutorEpoch ||
      f[3]!=VerifierEpoch || f[5]!=(string)ExpectedLogin || f[6]!=ExpectedServer ||
      f[7]!=ExpectedSymbol || f[8]!=(string)StrategyMagic || f[9]!="END") return false;
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
   const string prefix=Mailbox+"\\"+nonce;
   const string temp=prefix+"."+VerifierEpoch+"."+(string)ChartID()+".tmp";
   string payload="RECOVERY_INVENTORY_V1\t"+nonce+"\t"+ExecutorEpoch+"\t"+VerifierEpoch+"\t"+
      generation+"\t"+(string)ExpectedLogin+"\t"+ExpectedServer+"\t"+ExpectedSymbol+"\t"+
      (string)StrategyMagic+"\t"+(string)ChartID()+"\t"+(string)(++Sequence)+"\t"+
      (result=="ok" ? "complete" : "unavailable")+"\t"+result+"\t"+
      (string)positions+"\t"+(string)orders+"\n"+first+"END\n";
   uchar bytes[];
   int count=StringToCharArray(payload,bytes,0,WHOLE_ARRAY,CP_UTF8)-1; // exclude terminating NUL
   if(count<=0 || (ulong)count>MaxResponse) return;
   int h=FileOpen(temp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return;
   uint written=FileWriteArray(h,bytes,0,count);
   FileFlush(h);
   FileClose(h);
   // Neither replacement nor atomicity is assumed. Verifying reader checks END and row counts.
   if(written!=(uint)count || !SessionOK() || !FileMove(temp,0,prefix+".snapshot",0)) FileDelete(temp);
}
void OnTimer() {
   if(!SessionOK()) { CloseSearch(); return; }
   if(SearchHandle==INVALID_HANDLE) {
      SearchHandle=FileFindFirst(Mailbox+"\\*.inv",SearchFilename);
      if(SearchHandle==INVALID_HANDLE) return;
   }
   for(int visited=0;visited<32;visited++) {
      string nonce,generation;
      bool new_work=LoadRequest(SearchFilename,nonce,generation) && !FileIsExist(Mailbox+"\\"+nonce+".snapshot");
      if(new_work) Publish(nonce,generation);
      if(!FileFindNext(SearchHandle,SearchFilename)) { CloseSearch(); return; }
      if(new_work) return; // One full snapshot max per timer event.
   }
}
