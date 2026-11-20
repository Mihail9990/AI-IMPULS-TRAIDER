#property strict
#property version "1.00"
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
void PublishInventory(){
 double tick=0,step=0;string first="",second="";int pc=0,oc=0,pc2=0,oc2=0;
 const bool ok=PySession() && PyGrid(tick,step) && Inventory(tick,step,first,pc,oc) &&
      Inventory(tick,step,second,pc2,oc2) && pc==pc2 && oc==oc2 && first==second;
 const string state=ok?"ok":"unavailable";
 const string wire=PyHeader("PYINV1")+"\t"+state+"\t"+PyNumber(tick)+"\t"+PyNumber(step)+"\t"+
    (string)(ok?pc:0)+"\t"+(string)(ok?oc:0)+"\t"+(MQLInfoInteger(MQL_TRADE_ALLOWED)?"1":"0")+"\n"+
    (ok?first:"")+"END\n";
 PyWrite(Box+"verifier.tsv",wire);
}
int OnInit(){if(!PyInit("verifier")){PyClose();return INIT_FAILED;}if(!EventSetTimer(1)){PyClose();return INIT_FAILED;}
 PublishInventory();return INIT_SUCCEEDED;}
void OnTimer(){PublishInventory();HistoryScan();OrderScan();}
void OnDeinit(const int reason){EventKillTimer();EndHistoryScan();EndOrderScan();PyClose();}
