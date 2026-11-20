#property strict
#property version "1.01"
#include "RecoveryPythonShared.mqh"
#include "RecoveryPythonHistory.mqh"
#include "RecoveryPythonOrder.mqh"

bool Inventory(const double tick,const double step,string &rows,int &pc,int &oc) {
 rows="";pc=0;oc=0;
 const int pn=PositionsTotal(),on=OrdersTotal();
 if(pn<0 || on<0 || pn>2048 || on>2048 || !PySession())return false;
 for(int i=0;i<pn;i++){
    const ulong id=PositionGetTicket(i);string sym;
    if(id==0 || !PositionSelectByTicket(id) || !PositionGetString(POSITION_SYMBOL,sym))return false;
    if(sym!=ExpectedSymbol)continue;
    long ticket,position,magic,side,reason,opened,vol,price,sl,tp;double v,e,s,t;
    if(++pc>128 || !PositionGetInteger(POSITION_TICKET,ticket) || (ulong)ticket!=id ||
       !PositionGetInteger(POSITION_IDENTIFIER,position) || position<=0 || !PositionGetInteger(POSITION_MAGIC,magic) ||
       !PositionGetInteger(POSITION_TYPE,side) || (side!=POSITION_TYPE_BUY && side!=POSITION_TYPE_SELL) ||
       !PositionGetInteger(POSITION_REASON,reason) || !PositionGetInteger(POSITION_TIME_MSC,opened) || opened<=0 ||
       !PositionGetDouble(POSITION_VOLUME,v) || !PyUnits(v,step,vol) || vol<=0 ||
       !PositionGetDouble(POSITION_PRICE_OPEN,e) || !PyUnits(e,tick,price) || price<=0 ||
       !PositionGetDouble(POSITION_SL,s) || !PyUnits(s,tick,sl) ||
       !PositionGetDouble(POSITION_TP,t) || !PyUnits(t,tick,tp))return false;
    rows+="P\t"+(string)position+"\t"+(string)id+"\t"+(string)(ulong)magic+"\t"+(string)reason+"\t"+
       (side==POSITION_TYPE_BUY?"BUY":"SELL")+"\t"+(string)opened+"\t"+(string)vol+"\t"+
       (string)price+"\t"+(string)sl+"\t"+(string)tp+"\n";
 }
 for(int i=0;i<on;i++){
    const ulong id=OrderGetTicket(i);string sym;
    if(id==0 || !OrderSelect(id) || !OrderGetString(ORDER_SYMBOL,sym))return false;
    if(sym!=ExpectedSymbol)continue;
    long ticket,pos,magic,type,state,reason,opened,vol,rem,price,sl,tp,limit;
    double v,r,e,s,t,l;
    if(++oc>128 || !OrderGetInteger(ORDER_TICKET,ticket) || (ulong)ticket!=id ||
       !OrderGetInteger(ORDER_POSITION_ID,pos) || pos<0 || !OrderGetInteger(ORDER_MAGIC,magic) ||
       !OrderGetInteger(ORDER_TYPE,type) || !OrderGetInteger(ORDER_STATE,state) || !OrderGetInteger(ORDER_REASON,reason) ||
       !OrderGetInteger(ORDER_TIME_SETUP_MSC,opened) || opened<=0 ||
       !OrderGetDouble(ORDER_VOLUME_INITIAL,v) || !PyUnits(v,step,vol) || vol<=0 ||
       !OrderGetDouble(ORDER_VOLUME_CURRENT,r) || !PyUnits(r,step,rem) || rem>vol ||
       !OrderGetDouble(ORDER_PRICE_OPEN,e) || !PyUnits(e,tick,price) ||
       !OrderGetDouble(ORDER_SL,s) || !PyUnits(s,tick,sl) || !OrderGetDouble(ORDER_TP,t) || !PyUnits(t,tick,tp) ||
       !OrderGetDouble(ORDER_PRICE_STOPLIMIT,l) || !PyUnits(l,tick,limit))return false;
    rows+="O\t"+(string)id+"\t"+(string)pos+"\t"+(string)(ulong)magic+"\t"+(string)type+"\t"+(string)state+
       "\t"+(string)reason+"\t"+(string)opened+"\t"+(string)vol+"\t"+(string)rem+"\t"+(string)price+
       "\t"+(string)sl+"\t"+(string)tp+"\t"+(string)limit+"\n";
 }
 return pn==PositionsTotal() && on==OrdersTotal() && PySession();
}
string LastInventory="";
bool PublishInventory(bool &changed){
 double tick=0,step=0;string first="",second="";int pc=0,oc=0,pc2=0,oc2=0;
 const bool ok=PySession() && PyGrid(tick,step) && Inventory(tick,step,first,pc,oc) &&
      Inventory(tick,step,second,pc2,oc2) && pc==pc2 && oc==oc2 && first==second;
 const string state=ok?"ok":"unavailable";
 const string body="\t"+state+"\t"+PyNumber(tick)+"\t"+PyNumber(step)+"\t"+
    (string)(ok?pc:0)+"\t"+(string)(ok?oc:0)+"\t"+(MQLInfoInteger(MQL_TRADE_ALLOWED)?"1":"0")+"\n"+
    (ok?first:"")+"END\n";
 if(!PyWrite(Box+"verifier.tsv",PyHeader("PYINV1")+body))return false;
 changed=(body!=LastInventory);LastInventory=body;return true;
}

// The rolling index locates live requests directly. Only the original per-ID,
// scope/instance/nonce-checked request can authorize an independent reading.
bool PriorityScan(const string kind,int &cursor,const bool refresh,bool &indexed){
 string data,fields[];indexed=false;
 if(!PyRead(Box+kind+"-priority.tsv",data,8192))return false;
 int count=StringSplit(data,9,fields);
 ulong stamp;const ulong now=GetTickCount64();
 if(count<4 || count>259 || fields[0]!=Instance || !PyNat(fields[1],stamp) ||
    stamp>now || now-stamp>10000 || fields[count-1]!="END\n")return false;
 indexed=true;const int n=count-3, budget=(n<4?n:4);
 bool changed=false;
 for(int i=0;i<budget;i++){
    cursor%=n;ulong id;
    const string key=fields[2+cursor];cursor++;
    if(!PyNat(key,id) || !id)continue;
    const string path=kind+"-"+key+".req";
    const bool wrote=(kind=="history"?HistoryRequest(path,refresh):OrderRequest(path));
    if(wrote)changed=true;
 }
 return changed;
}

// This file is solely a Windows last-write notification. Write in place:
// replacing a temp file may produce just a rename notification for the target.
bool SignalWake(){
 const int h=FileOpen(Box+"trade-wakeup.tsv",FILE_READ|FILE_WRITE|FILE_BIN|FILE_SHARE_READ|FILE_SHARE_WRITE);
 if(h==INVALID_HANDLE)return false;
 const string text=StringFormat("%020I64u\n",GetTickCount64());
 uchar bytes[];const int n=StringToCharArray(text,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
 bool ok=false;
 if(n>0 && FileSeek(h,0,SEEK_SET))ok=FileWriteArray(h,bytes,0,n)==(uint)n;
 FileFlush(h);FileClose(h);return ok;
}

ulong BurstUntil=0, NextHeartbeat=0;
int HistoryIndex=0, OrderIndex=0;
bool WakePending=false;

void OnTradeTransaction(const MqlTradeTransaction &event,const MqlTradeRequest &request,const MqlTradeResult &result){
 string symbol=event.symbol;
 if(StringLen(symbol)==0 && event.type==TRADE_TRANSACTION_REQUEST)symbol=request.symbol;
 if(StringLen(symbol)>0 && symbol!=ExpectedSymbol)return;
 // Transaction order is not guaranteed. Reconcile the whole scope on the
 // timer and keep a short window open for a later Python proof request.
 WakePending=true;BurstUntil=GetTickCount64()+3000;
}

void OnTimer(){
 const ulong now=GetTickCount64();
 const bool burst=(now<BurstUntil), heartbeat=(now>=NextHeartbeat);
 if(!burst && !heartbeat && !WakePending)return;
 if(heartbeat)NextHeartbeat=now+1000;
 bool inventory_changed=false;
 const bool published=PublishInventory(inventory_changed);
 bool proof=false;
 if(PySession()){
    bool hi=false,oi=false;
    if(PriorityScan("history",HistoryIndex,burst,hi))proof=true;
    if(PriorityScan("order",OrderIndex,burst,oi))proof=true;
    // Older clients have only per-ID requests. A cursor bounds each pass;
    // fresh indexed requests take precedence over the accumulated archive.
    if((!hi || heartbeat) && HistoryScan(burst))proof=true;
    if((!oi || heartbeat) && OrderScan())proof=true;
 } else {EndHistoryScan();EndOrderScan();}
 if(proof)BurstUntil=now+3000;
 if(inventory_changed || proof)WakePending=true;
 if(WakePending && published && SignalWake())WakePending=false;
}

int OnInit(){
 if(!PyInit("verifier") || !EventSetMillisecondTimer(125)){PyClose();return INIT_FAILED;}
 WakePending=true;BurstUntil=GetTickCount64()+3000;
 OnTimer();return INIT_SUCCEEDED;
}
void OnDeinit(const int reason){EventKillTimer();EndHistoryScan();EndOrderScan();PyClose();}
