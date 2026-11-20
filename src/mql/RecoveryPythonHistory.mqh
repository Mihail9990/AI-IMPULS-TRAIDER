#ifndef RECOVERY_PYTHON_HISTORY_MQH
#define RECOVERY_PYTHON_HISTORY_MQH
long HistCursor=INVALID_HANDLE;string HistName="";
void EndHistoryScan(){if(HistCursor!=INVALID_HANDLE)FileFindClose(HistCursor);HistCursor=INVALID_HANDLE;HistName="";}
bool DealMoney(const ulong deal,const ulong position,const long type,string &line){
 double profit,commission,swap,fee;long order;
 if(!HistoryDealGetInteger(deal,DEAL_ORDER,order) || order<0 ||
    !HistoryDealGetDouble(deal,DEAL_PROFIT,profit) || !MathIsValidNumber(profit) ||
    !HistoryDealGetDouble(deal,DEAL_COMMISSION,commission) || !MathIsValidNumber(commission) ||
    !HistoryDealGetDouble(deal,DEAL_SWAP,swap) || !MathIsValidNumber(swap) ||
    !HistoryDealGetDouble(deal,DEAL_FEE,fee) || !MathIsValidNumber(fee))return false;
 line="M\t"+(string)deal+"\t"+(string)position+"\t"+(string)(ulong)order+"\t"+(string)type+
      "\t"+PyNumber(profit)+"\t"+PyNumber(commission)+"\t"+PyNumber(swap)+"\t"+PyNumber(fee)+"\n";
 return true;
}
bool ReadPositionDeals(const ulong position,const double tick,const double step,string &rows,int &count,
                       string &money,int &money_count,bool &money_ok){
 rows="";count=0;money="";money_count=0;money_ok=true;
 if(!PySession() || !HistorySelectByPosition(position))return false;
 const int n=HistoryDealsTotal();if(n<0 || n>1024)return false;
 for(int i=0;i<n;i++){
    const ulong id=HistoryDealGetTicket(i);long pos,type,order,magic,entry,reason,msc;string sym;
    if(id==0 || !HistoryDealGetInteger(id,DEAL_POSITION_ID,pos) || (ulong)pos!=position ||
       !HistoryDealGetInteger(id,DEAL_TYPE,type))return false;
    string amount;
    if(!DealMoney(id,position,type,amount) || money_count>=256)money_ok=false;
    else {money+=amount;money_count++;}
    if(type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL)continue;
    double v,p,s,t;long vol,price,sl,tp;
    if(++count>256 || !HistoryDealGetString(id,DEAL_SYMBOL,sym) || sym!=ExpectedSymbol ||
       !HistoryDealGetInteger(id,DEAL_ORDER,order) || order<=0 ||
       !HistoryDealGetInteger(id,DEAL_MAGIC,magic) || !HistoryDealGetInteger(id,DEAL_ENTRY,entry) ||
       !HistoryDealGetInteger(id,DEAL_REASON,reason) || !HistoryDealGetInteger(id,DEAL_TIME_MSC,msc) || msc<=0 ||
       !HistoryDealGetDouble(id,DEAL_VOLUME,v) || !PyUnits(v,step,vol) || vol<=0 ||
       !HistoryDealGetDouble(id,DEAL_PRICE,p) || !PyUnits(p,tick,price) || price<=0 ||
       !HistoryDealGetDouble(id,DEAL_SL,s) || !PyUnits(s,tick,sl) ||
       !HistoryDealGetDouble(id,DEAL_TP,t) || !PyUnits(t,tick,tp))return false;
    rows+="D\t"+(string)id+"\t"+(string)(ulong)order+"\t"+(string)position+"\t"+(string)(ulong)magic+"\t"+
       (string)type+"\t"+(string)entry+"\t"+(string)reason+"\t"+(string)msc+"\t"+(string)vol+"\t"+
       (string)price+"\t"+(string)sl+"\t"+(string)tp+"\n";
 }
 return PySession() && n==HistoryDealsTotal();
}
bool HistoryRequest(const string name){
 string req,f[];ulong id,stamp;
 if(StringFind(name,"history-")!=0 || !PyRead(Box+name,req,1024) || StringSplit(req,9,f)!=10 ||
    f[0]!="PYHREQ1" || f[1]!=PythonScope || f[2]!=(string)ExpectedLogin || f[3]!=ExpectedServer ||
    f[4]!=ExpectedSymbol || f[5]!=Instance || !PyNat(f[6],id) || id==0 || !PyHex(f[7]) ||
    !PyNat(f[8],stamp) || f[9]!="END\n" || stamp>GetTickCount64() || GetTickCount64()-stamp>10000 ||
    name!="history-"+(string)id+".req")return false;
 const string response=Box+"history-"+(string)id+".tsv";string old;
 if(PyRead(response,old,131072)){
    int e=StringFind(old,"\n");string parts[];
    if(e>0 && StringSplit(StringSubstr(old,0,e),9,parts)==17 && parts[5]==Instance && parts[10]==f[7])return false;
 }
 double tick=0,step=0;int n=0,m=0,nm=0,mm=0;string a="",b="",am="",bm="";
 const string currency=AccountInfoString(ACCOUNT_CURRENCY);bool ma=false,mb=false;
 const bool ok=PyGrid(tick,step) && ReadPositionDeals(id,tick,step,a,n,am,nm,ma) &&
    ReadPositionDeals(id,tick,step,b,m,bm,mm,mb) && a==b && n==m;
 const bool cash=ok && ma && mb && nm==mm && am==bm && PyField(currency) &&
                 AccountInfoString(ACCOUNT_CURRENCY)==currency;
 const string wire=PyHeader("PYHIST2")+"\t"+(ok?"ok":"unavailable")+"\t"+(string)id+"\t"+f[7]+"\t"+
    PyNumber(tick)+"\t"+PyNumber(step)+"\t"+(string)(ok?n:0)+"\t"+(PyField(currency)?currency:"-")+"\t"+
    (cash?"ok":"unavailable")+"\t"+(string)(cash?nm:0)+"\n"+(ok?a:"")+(cash?am:"")+"END\n";
 return PyWrite(response,wire);
}
void HistoryScan(){
 if(!PySession()){EndHistoryScan();return;}
 if(HistCursor==INVALID_HANDLE){HistCursor=FileFindFirst(Box+"history-*.req",HistName);if(HistCursor==INVALID_HANDLE)return;}
 for(int i=0;i<32;i++){
    bool produced=HistoryRequest(HistName);
    if(!FileFindNext(HistCursor,HistName)){EndHistoryScan();return;}
    if(produced)return;
 }
}
#endif
