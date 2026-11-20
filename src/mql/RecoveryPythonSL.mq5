#property strict
#property version "1.00"
#include "RecoveryPythonShared.mqh"
#include "../../src/watcher/RecoveryPriceStage.mqh"

struct Owner {ulong id,ticket,magic;long opened,entry,advance;int side;string row;};
struct Saved {string phase;long anchor,confirmed_sl,confirmed_tp;ulong cursor,retcode,cooldown;};
struct View {double entry,sl,tp;long sl_ticks,tp_ticks;};
Owner Active[];
string Run="";
ulong Lease=0,LastStatus=0;
long LatestTick=0;
ulong LastQuoteAt=0;
string Mark(const Owner &o){return Box+"position-"+(string)o.id+".state";}
string Fingerprint(const Owner &o){return PySha(PyBinding()+"\t"+o.row);}
bool Same(const Owner &a,const Owner &b){return a.row==b.row;}

bool LoadOwners(Owner &list[],string &run,ulong &until){
 run="";until=0;ArrayResize(list,0);
 string wire;
 if(!PyRead(Box+"sl-register.tsv",wire,16384))return false;
 const int size=StringLen(wire);if(size<5 || StringSubstr(wire,size-5)!="\nEND\n")return false;
 string lines[],h[];const int n=StringSplit(StringSubstr(wire,0,size-1),10,lines);
 if(n<2 || lines[n-1]!="END" || StringSplit(lines[0],9,h)!=10 ||
    h[0]!="PYREG1" || h[1]!=PythonScope || h[2]!=(string)ExpectedLogin ||
    h[3]!=ExpectedServer || h[4]!=ExpectedSymbol || h[5]!=Instance || !PyHex(h[6]))return false;
 ulong stamp,limit,count;const ulong now=GetTickCount64();
 if(!PyNat(h[7],stamp) || !PyNat(h[8],limit) || !PyNat(h[9],count) || count>24 ||
    stamp>now || limit<=now || limit<=stamp || limit-stamp>6000 || n!=2+(int)count)return false;
 for(int i=0;i<(int)count;i++){
    string f[];Owner o;ulong opened,entry,advance;
    if(StringSplit(lines[i+1],9,f)!=8 || f[0]!="R" || !PyNat(f[1],o.id) || o.id==0 ||
       !PyNat(f[2],o.ticket) || !o.ticket || !PyNat(f[3],o.magic) || !PyNat(f[4],opened) ||
       opened==0 || opened>(ulong)LONG_MAX || (f[5]!="BUY" && f[5]!="SELL") ||
       !PyNat(f[6],entry) || !entry || entry>1000000000000 || !PyNat(f[7],advance) ||
       !advance || advance>1000000000000)return false;
    o.opened=(long)opened;o.entry=(long)entry;o.advance=(long)advance;o.side=f[5]=="BUY"?1:-1;o.row=lines[i+1];
    for(int j=0;j<i;j++)if(list[j].id==o.id || list[j].ticket==o.ticket)return false;
    if(ArrayResize(list,i+1)!=i+1)return false;list[i]=o;
 }
 run=h[6];until=limit;return true;
}
bool ReadView(const Owner &o,const double tick,View &v) {
 long t,id,side,opened,magic,entry;string symbol;
 if(!PySession() || !PositionSelectByTicket(o.ticket) || !PositionGetInteger(POSITION_TICKET,t) ||
    (ulong)t!=o.ticket || !PositionGetInteger(POSITION_IDENTIFIER,id) || (ulong)id!=o.id ||
    !PositionGetInteger(POSITION_MAGIC,magic) || (ulong)magic!=o.magic ||
    !PositionGetString(POSITION_SYMBOL,symbol) || symbol!=ExpectedSymbol ||
    !PositionGetInteger(POSITION_TYPE,side) || side!=(o.side==1?POSITION_TYPE_BUY:POSITION_TYPE_SELL) ||
    !PositionGetInteger(POSITION_TIME_MSC,opened) || opened!=o.opened ||
    !PositionGetDouble(POSITION_PRICE_OPEN,v.entry) || !PyUnits(v.entry,tick,entry) || entry!=o.entry ||
    !PositionGetDouble(POSITION_SL,v.sl) || !PyUnits(v.sl,tick,v.sl_ticks) ||
    !PositionGetDouble(POSITION_TP,v.tp) || !PyUnits(v.tp,tick,v.tp_ticks))return false;
 return true;
}
bool Save(const Owner &o,const Saved &s,const bool replace=true){
 return PyWrite(Mark(o),Fingerprint(o)+"\t"+s.phase+"\t"+(string)s.anchor+"\t"+(string)s.cursor+
       "\t"+(string)s.retcode+"\t"+(string)s.cooldown+"\t"+(string)s.confirmed_sl+"\t"+(string)s.confirmed_tp,replace);
}
bool ReadSaved(const Owner &o,Saved &s){
 string wire,f[];ulong anchor,proof_sl,proof_tp;
 if(!PyRead(Mark(o),wire,256) || StringSplit(wire,9,f)!=8 || f[0]!=Fingerprint(o) ||
    (f[1]!="watch" && f[1]!="hit" && f[1]!="submitting" && f[1]!="rejected" && f[1]!="done") ||
    !PyNat(f[2],anchor) || anchor==0 || anchor>(ulong)LONG_MAX || !PyNat(f[3],s.cursor) ||
    !PyNat(f[4],s.retcode) || !PyNat(f[5],s.cooldown) || !PyNat(f[6],proof_sl) ||
    !PyNat(f[7],proof_tp) || proof_sl>1000000000000 || proof_tp>1000000000000)return false;
 s.phase=f[1];s.anchor=(long)anchor;s.confirmed_sl=(long)proof_sl;s.confirmed_tp=(long)proof_tp;return true;
}
int Protection(const int side,const long entry,const long sl){
 if(sl==0)return 0;if(sl==entry)return 1;return (sl-entry)*side>0?2:0;
}
bool Permissions(){return PySession() && MQLInfoInteger(MQL_TRADE_ALLOWED)!=0 &&
  TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)!=0 && AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)!=0 &&
  AccountInfoInteger(ACCOUNT_TRADE_EXPERT)!=0;}
bool Rejected(const uint code){
 return code==TRADE_RETCODE_REQUOTE || code==TRADE_RETCODE_REJECT || code==TRADE_RETCODE_INVALID_STOPS ||
    code==TRADE_RETCODE_MARKET_CLOSED || code==TRADE_RETCODE_PRICE_CHANGED || code==TRADE_RETCODE_PRICE_OFF ||
    code==TRADE_RETCODE_FROZEN || code==TRADE_RETCODE_TOO_MANY_REQUESTS ||
    code==TRADE_RETCODE_TRADE_DISABLED || code==TRADE_RETCODE_CLIENT_DISABLES_AT || code==TRADE_RETCODE_SERVER_DISABLES_AT;
}
bool EligibleQuote(const Owner &o,const View &v,const double tick,const MqlTick &q){
 return q.time_msc>=o.opened && RecoveryPriceStage(o.side,v.entry,q.bid,q.ask,0.0,o.advance*tick)==2;
}
bool TicksHit(const Owner &o,const View &v,const double tick,const MqlTick &q,Saved &s){
 if(EligibleQuote(o,v,tick,q))return true;
 if(s.cursor>(ulong)q.time_msc)return false;
 MqlTick t[];ResetLastError();
 const int n=CopyTicks(ExpectedSymbol,t,COPY_TICKS_INFO,s.cursor,512);
 if(n<=0)return false;
 const bool synchronized=GetLastError()==0;
 for(int i=0;i<n;i++)if(t[i].time_msc>s.anchor && EligibleQuote(o,v,tick,t[i]))return true;
 const ulong last=(ulong)t[n-1].time_msc;
 // Preserve the inclusive boundary. Re-reading a timestamp is harmless.
 if(synchronized && last>s.cursor && (n==512 || last-s.cursor>=3000)){s.cursor=last;Save(o,s);}
 return false;
}
bool CurrentOwner(const Owner &o,const string run){
 Owner rows[];string actual;ulong until;
 if(!LoadOwners(rows,actual,until) || actual!=run)return false;
 for(int i=0;i<ArraySize(rows);i++)if(Same(o,rows[i]))return true;
 return false;
}
string Submit(const Owner &o,const string run,const double tick,View &v,Saved &s){
 const int lock=FileOpen(Box+"register.lock",FILE_READ|FILE_WRITE|FILE_BIN);
 if(lock==INVALID_HANDLE)return "hit";
 string status=SubmitLocked(o,run,tick,v,s);
 FileClose(lock);return status;
}
string SubmitLocked(const Owner &o,const string run,const double tick,View &v,Saved &s){
 if(!Permissions())return "blocked";
 if(!CurrentOwner(o,run) || !ReadView(o,tick,v))return "inactive";
 if(Protection(o.side,o.entry,v.sl_ticks)>0)return "hit";
 MqlTick quote;long min_stops,freeze;double point;
 if(!SymbolInfoTick(ExpectedSymbol,quote) || quote.time_msc<o.opened || GetTickCount64()-LastQuoteAt>5000 ||
    !SymbolInfoInteger(ExpectedSymbol,SYMBOL_TRADE_STOPS_LEVEL,min_stops) ||
    !SymbolInfoInteger(ExpectedSymbol,SYMBOL_TRADE_FREEZE_LEVEL,freeze) ||
    !SymbolInfoDouble(ExpectedSymbol,SYMBOL_POINT,point) || point<=0)return "hit";
 const double gap=o.side==1?quote.bid-v.entry:v.entry-quote.ask;
 if(gap<=0 || gap<min_stops*point || (freeze>0 && gap<=freeze*point))return "hit";
 MqlTradeRequest req={}; MqlTradeCheckResult check={};MqlTradeResult result={};
 req.action=TRADE_ACTION_SLTP;req.symbol=ExpectedSymbol;req.position=o.ticket;req.sl=v.entry;req.tp=v.tp;
 if(!OrderCheck(req,check)){s.retcode=check.retcode;return "rejected";}
 View fresh;
 if(!ReadView(o,tick,fresh) || fresh.sl!=v.sl || fresh.tp!=v.tp || fresh.entry!=v.entry || !CurrentOwner(o,run))return "hit";
 s.phase="submitting";s.retcode=0;
 if(!Save(o,s))return "unknown";
 // Last read after the durable reservation. No real submission if the
 // last known TP or stop changed during reservation.
 if(!Permissions() || !ReadView(o,tick,fresh) || fresh.sl!=v.sl || fresh.tp!=v.tp ||
    fresh.entry!=v.entry || !CurrentOwner(o,run)){
    s.phase="hit";Save(o,s);return "hit";
 }
 const bool submitted=OrderSend(req,result);
 s.retcode=result.retcode;
 if(Rejected(result.retcode)){s.phase="rejected";s.cooldown=GetTickCount64()+3000;Save(o,s);return "rejected";}
 // Accepted, timeouts, empty retcodes and connection errors all require
 // broker readback. A process restart cannot issue a second uncertain write.
 if(!submitted && result.retcode==0)return "pending"; // durable reservation already records unknown outcome
 Save(o,s);
 return "pending";
}
string Observe(const Owner &o,const string run,const double tick,View &v,Saved &s,const bool submit) {
 if(!ReadView(o,tick,v)){
    if(ReadSaved(o,s) && s.phase=="done" && Protection(o.side,o.entry,s.confirmed_sl)>0){
       v.sl_ticks=s.confirmed_sl;v.tp_ticks=s.confirmed_tp;
       return s.confirmed_sl==o.entry?"last_confirmed":"last_protected";
    }
    return "missing";
 }
 MqlTick q;
 if(!SymbolInfoTick(ExpectedSymbol,q) || q.time_msc<=0)return "unavailable";
 if(q.time_msc!=LatestTick){LatestTick=q.time_msc;LastQuoteAt=GetTickCount64();}
 if(!ReadSaved(o,s)){
    if(FileIsExist(Mark(o)))return "unknown";
    s.phase="watch";s.anchor=q.time_msc>o.opened?q.time_msc:o.opened;s.cursor=(ulong)s.anchor+1;s.retcode=0;s.cooldown=0;
    if(!Save(o,s,false))return "unknown";
 }
 const int level=Protection(o.side,o.entry,v.sl_ticks);
 if(level>0){if(s.phase!="done" || v.sl_ticks!=s.confirmed_sl || v.tp_ticks!=s.confirmed_tp){
                s.phase="done";s.confirmed_sl=v.sl_ticks;s.confirmed_tp=v.tp_ticks;if(!Save(o,s))return "unknown";}
             return level==1?"confirmed":"protected";}
 if(s.phase=="done")return "changed";
 if(s.phase=="submitting")return "pending";
 if(s.phase=="watch"){
    if(!(submit?TicksHit(o,v,tick,q,s):EligibleQuote(o,v,tick,q)))return "watching";
    s.phase="hit";if(!Save(o,s))return "unknown";
 }
 if(s.phase=="rejected" && s.cooldown>GetTickCount64() && s.cooldown-GetTickCount64()<=3000)return "rejected";
 if(!submit)return "hit";
 return Submit(o,run,tick,v,s);
}
void OnTick(){
 if(!PySession() || GetTickCount64()>=Lease)return;
 double tick,step;if(!PyGrid(tick,step))return;
 // The quote callback latches a crossing but never submits an order.
 for(int i=0;i<ArraySize(Active);i++){View v={};Saved s;Observe(Active[i],Run,tick,v,s,false);}
}
void OnTimer(){
 Owner list[];string run;ulong until=0;double tick,step;
 const bool online=PySession() && PyGrid(tick,step);
 if(!online || !LoadOwners(list,run,until)){ArrayResize(list,0);run="";until=0;}
 if(ArrayResize(Active,ArraySize(list))!=ArraySize(list)){ArrayResize(Active,0);return;}
 for(int i=0;i<ArraySize(list);i++)Active[i]=list[i];
 Run=run;Lease=until;
 string rows="";
 for(int i=0;i<ArraySize(list);i++){
    const Owner o=list[i];View v={};Saved s;ZeroMemory(s);
    string status=Observe(o,run,tick,v,s,true);
    rows+="W\t"+(string)o.id+"\t"+(string)o.ticket+"\t"+(string)o.magic+"\t"+(string)o.opened+"\t"+
          (o.side==1?"BUY":"SELL")+"\t"+(string)o.entry+"\t"+(string)o.advance+"\t"+
          (string)v.sl_ticks+"\t"+(string)v.tp_ticks+"\t"+(string)LatestTick+"\t"+status+"\t"+(string)s.retcode+"\n";
 }
 const string wire=PyHeader("PYSL1")+"\t"+(online?"ok":"unavailable")+"\t"+(run==""?"-":run)+"\t"+
     (string)until+"\t"+(string)ArraySize(list)+"\t"+(Permissions()?"1":"0")+"\n"+rows+"END\n";
 PyWrite(Box+"sl-status.tsv",wire);
}
int OnInit(){if(!PyInit("sl")){PyClose();return INIT_FAILED;}if(!EventSetTimer(1)){PyClose();return INIT_FAILED;}
 OnTimer();return INIT_SUCCEEDED;}
void OnDeinit(const int reason){EventKillTimer();PyClose();}
