#ifndef RECOVERY_PYTHON_ORDER_MQH
#define RECOVERY_PYTHON_ORDER_MQH
long OrderCursor=INVALID_HANDLE;string OrderName="";
void EndOrderScan(){if(OrderCursor!=INVALID_HANDLE)FileFindClose(OrderCursor);OrderCursor=INVALID_HANDLE;OrderName="";}
bool HistoricalOrder(const ulong id,const double tick,const double step,string &row){
 if(!PySession() || OrderSelect(id) || !HistoryOrderSelect(id))return false;
 long actual,pos,magic,type,state,reason,setup,done,vol,rem,price,sl,tp,limit;
 double v,r,p,s,t,l;string symbol;
 if(!HistoryOrderGetInteger(id,ORDER_TICKET,actual) || (ulong)actual!=id ||
    !HistoryOrderGetString(id,ORDER_SYMBOL,symbol) || symbol!=ExpectedSymbol ||
    !HistoryOrderGetInteger(id,ORDER_POSITION_ID,pos) || pos<0 || !HistoryOrderGetInteger(id,ORDER_MAGIC,magic) ||
    !HistoryOrderGetInteger(id,ORDER_TYPE,type) || !HistoryOrderGetInteger(id,ORDER_STATE,state) ||
    !HistoryOrderGetInteger(id,ORDER_REASON,reason) || !HistoryOrderGetInteger(id,ORDER_TIME_SETUP_MSC,setup) || setup<=0 ||
    !HistoryOrderGetInteger(id,ORDER_TIME_DONE_MSC,done) || done<=0 ||
    !HistoryOrderGetDouble(id,ORDER_VOLUME_INITIAL,v) || !PyUnits(v,step,vol) || vol<=0 ||
    !HistoryOrderGetDouble(id,ORDER_VOLUME_CURRENT,r) || !PyUnits(r,step,rem) || rem>vol ||
    !HistoryOrderGetDouble(id,ORDER_PRICE_OPEN,p) || !PyUnits(p,tick,price) ||
    !HistoryOrderGetDouble(id,ORDER_SL,s) || !PyUnits(s,tick,sl) ||
    !HistoryOrderGetDouble(id,ORDER_TP,t) || !PyUnits(t,tick,tp) ||
    !HistoryOrderGetDouble(id,ORDER_PRICE_STOPLIMIT,l) || !PyUnits(l,tick,limit))return false;
 row="H\t"+(string)id+"\t"+(string)(ulong)pos+"\t"+(string)(ulong)magic+"\t"+(string)type+"\t"+(string)state+
    "\t"+(string)reason+"\t"+(string)setup+"\t"+(string)done+"\t"+(string)vol+"\t"+(string)rem+
    "\t"+(string)price+"\t"+(string)sl+"\t"+(string)tp+"\t"+(string)limit+"\n";
 return PySession();
}
bool OrderRequest(const string name){
 string req,f[];ulong id,stamp;
 if(!PyRead(Box+name,req,1024) || StringSplit(req,9,f)!=10 || f[0]!="PYOQ1" || f[1]!=PythonScope ||
    f[2]!=(string)ExpectedLogin || f[3]!=ExpectedServer || f[4]!=ExpectedSymbol || f[5]!=Instance ||
    !PyNat(f[6],id) || !id || !PyHex(f[7]) || !PyNat(f[8],stamp) || f[9]!="END\n" ||
    stamp>GetTickCount64() || GetTickCount64()-stamp>10000 || name!="order-"+(string)id+".req")return false;
 const string path=Box+"order-"+(string)id+".tsv";string prior;
 if(PyRead(path,prior,2048)){
    int end=StringFind(prior,"\n");string h[];
    if(end>0 && StringSplit(StringSubstr(prior,0,end),9,h)==13 && h[5]==Instance && h[10]==f[7])return false;
 }
 double tick=0,step=0;string a="",b="";
 const bool ok=PyGrid(tick,step) && HistoricalOrder(id,tick,step,a) && HistoricalOrder(id,tick,step,b) && a==b;
 return PyWrite(path,PyHeader("PYORD1")+"\t"+(ok?"ok":"unavailable")+"\t"+(string)id+"\t"+f[7]+"\t"+
    PyNumber(tick)+"\t"+PyNumber(step)+"\n"+(ok?a:"")+"END\n");
}
void OrderScan(){
 if(!PySession()){EndOrderScan();return;}
 if(OrderCursor==INVALID_HANDLE){OrderCursor=FileFindFirst(Box+"order-*.req",OrderName);if(OrderCursor==INVALID_HANDLE)return;}
 for(int i=0;i<32;i++){
    const bool processed=OrderRequest(OrderName);
    if(!FileFindNext(OrderCursor,OrderName)){EndOrderScan();return;}
    if(processed)return;
 }
}
#endif
