#ifndef RECOVERY_PYTHON_HISTORY_MQH
#define RECOVERY_PYTHON_HISTORY_MQH
long HistCursor=INVALID_HANDLE;string HistName="";
void EndHistoryScan(){if(HistCursor!=INVALID_HANDLE)FileFindClose(HistCursor);HistCursor=INVALID_HANDLE;HistName="";}
bool ReadPositionDeals(const ulong position,const double tick,const double step,string &rows,int &count){
 rows="";count=0;
 if(!PySession() || !HistorySelectByPosition(position))return false;
 const int n=HistoryDealsTotal();if(n<0 || n>1024)return false;
 for(int i=0;i<n;i++){
    const ulong id=HistoryDealGetTicket(i);long pos,type,order,magic,entry,reason,msc;string sym;
    if(id==0 || !HistoryDealGetInteger(id,DEAL_POSITION_ID,pos) || (ulong)pos!=position ||
       !HistoryDealGetInteger(id,DEAL_TYPE,type))return false;
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
bool HistoryRequest(const string name,const bool refresh=false){
 string req,f[];ulong id,stamp;
 if(StringFind(name,"history-")!=0 || !PyRead(Box+name,req,1024) || StringSplit(req,9,f)!=10 ||
    f[0]!="PYHREQ1" || f[1]!=PythonScope || f[2]!=(string)ExpectedLogin || f[3]!=ExpectedServer ||
    f[4]!=ExpectedSymbol || f[5]!=Instance || !PyNat(f[6],id) || id==0 || !PyHex(f[7]) ||
    !PyNat(f[8],stamp) || f[9]!="END\n" || stamp>GetTickCount64() || GetTickCount64()-stamp>10000 ||
    name!="history-"+(string)id+".req")return false;
 const string response=Box+"history-"+(string)id+".tsv";string old,prior="";
 if(PyRead(response,old,131072)){
    int e=StringFind(old,"\n");string parts[];
    if(e>0 && StringSplit(StringSubstr(old,0,e),9,parts)==17 && parts[5]==Instance && parts[10]==f[7]){
       if(parts[8]=="ok" && !refresh)return false;
       int stateAt=StringFind(old,"\t"+parts[8]+"\t"+f[6]+"\t"+f[7]+"\t");
       if(stateAt>0)prior=StringSubstr(old,stateAt);
    }
 }
 double tick=0,step=0;int n=0,m=0;string a="",b="";
 const bool ok=PyGrid(tick,step) && ReadPositionDeals(id,tick,step,a,n) &&
    ReadPositionDeals(id,tick,step,b,m) && a==b && n==m;
 const string body="\t"+(ok?"ok":"unavailable")+"\t"+(string)id+"\t"+f[7]+"\t"+
    PyNumber(tick)+"\t"+PyNumber(step)+"\t"+(string)(ok?n:0)+"\t-\tunavailable\t0\n"+(ok?a:"")+"END\n";
 return body!=prior && PyWrite(response,PyHeader("PYHIST2")+body);
}
bool HistoryScan(const bool refresh=false){
 if(!PySession()){EndHistoryScan();return false;}
 if(HistCursor==INVALID_HANDLE){HistCursor=FileFindFirst(Box+"history-*.req",HistName);if(HistCursor==INVALID_HANDLE)return false;}
 for(int i=0;i<32;i++){
    bool produced=HistoryRequest(HistName,refresh);
    if(!FileFindNext(HistCursor,HistName)){EndHistoryScan();return produced;}
    if(produced)return true;
 }
 return false;
}
#endif
