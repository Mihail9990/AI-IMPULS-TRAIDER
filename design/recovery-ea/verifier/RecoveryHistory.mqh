#ifndef RECOVERY_EXACT_HISTORY_MQH
#define RECOVERY_EXACT_HISTORY_MQH
// Requires SessionOK(), Number() and NonNegative() from RecoveryVerifier.

bool RecoveryHistoryOrder(const int index,const ulong expected_position,
                          const string expected_symbol,ulong &ticket,string &row) {
   ticket=HistoryOrderGetTicket(index);
   long actual,position,magic,type,state,done;
   double volume,remaining,price,sl,tp;
   string symbol;
   if(ticket==0 || !HistoryOrderGetInteger(ticket,ORDER_TICKET,actual) || (ulong)actual!=ticket ||
      !HistoryOrderGetInteger(ticket,ORDER_POSITION_ID,position) || (ulong)position!=expected_position ||
      !HistoryOrderGetString(ticket,ORDER_SYMBOL,symbol) || symbol!=expected_symbol ||
      !HistoryOrderGetInteger(ticket,ORDER_MAGIC,magic) ||
      !HistoryOrderGetInteger(ticket,ORDER_TYPE,type) || type<0 ||
      !HistoryOrderGetInteger(ticket,ORDER_STATE,state) || state<0 ||
      !HistoryOrderGetInteger(ticket,ORDER_TIME_DONE_MSC,done) || done<0 ||
      !HistoryOrderGetDouble(ticket,ORDER_VOLUME_INITIAL,volume) || !NonNegative(volume) || volume<=0 ||
      !HistoryOrderGetDouble(ticket,ORDER_VOLUME_CURRENT,remaining) || !NonNegative(remaining) || remaining>volume ||
      !HistoryOrderGetDouble(ticket,ORDER_PRICE_OPEN,price) || !NonNegative(price) ||
      !HistoryOrderGetDouble(ticket,ORDER_SL,sl) || !NonNegative(sl) ||
      !HistoryOrderGetDouble(ticket,ORDER_TP,tp) || !NonNegative(tp)) return false;
   row="H\t"+(string)ticket+"\t"+(string)(ulong)magic+"\t"+(string)type+"\t"+(string)state+"\t"+
       (string)done+"\t"+Number(volume)+"\t"+Number(remaining)+"\t"+Number(price)+"\t"+
       Number(sl)+"\t"+Number(tp)+"\n";
   return true;
}

bool RecoveryHistoryDeal(const int index,const ulong expected_position,
                         const string expected_symbol,ulong &ticket,ulong &linked_order,string &row) {
   ticket=HistoryDealGetTicket(index);
   long actual,position,order,magic,type,entry,reason,time_msc;
   double volume,price,sl,tp;
   string symbol;
   if(ticket==0 || !HistoryDealGetInteger(ticket,DEAL_TICKET,actual) || (ulong)actual!=ticket ||
      !HistoryDealGetInteger(ticket,DEAL_POSITION_ID,position) || (ulong)position!=expected_position ||
      !HistoryDealGetString(ticket,DEAL_SYMBOL,symbol) || symbol!=expected_symbol ||
      !HistoryDealGetInteger(ticket,DEAL_ORDER,order) || order==0 ||
      !HistoryDealGetInteger(ticket,DEAL_MAGIC,magic) ||
      !HistoryDealGetInteger(ticket,DEAL_TYPE,type) || (type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL) ||
      !HistoryDealGetInteger(ticket,DEAL_ENTRY,entry) || entry<0 ||
      !HistoryDealGetInteger(ticket,DEAL_REASON,reason) || reason<0 ||
      !HistoryDealGetInteger(ticket,DEAL_TIME_MSC,time_msc) || time_msc<=0 ||
      !HistoryDealGetDouble(ticket,DEAL_VOLUME,volume) || !NonNegative(volume) || volume<=0 ||
      !HistoryDealGetDouble(ticket,DEAL_PRICE,price) || !NonNegative(price) || price<=0 ||
      !HistoryDealGetDouble(ticket,DEAL_SL,sl) || !NonNegative(sl) ||
      !HistoryDealGetDouble(ticket,DEAL_TP,tp) || !NonNegative(tp)) return false;
   linked_order=(ulong)order;
   row="D\t"+(string)ticket+"\t"+(string)linked_order+"\t"+(string)(ulong)magic+"\t"+
       (type==DEAL_TYPE_BUY ? "BUY" : "SELL")+"\t"+(string)entry+"\t"+(string)reason+"\t"+
       (string)time_msc+"\t"+Number(volume)+"\t"+Number(price)+"\t"+Number(sl)+"\t"+Number(tp)+"\n";
   return true;
}

string RecoveryHistorySample(const ulong position,const ulong anchor,const string symbol,
                             string &rows,int &order_count,int &deal_count) {
   rows=""; order_count=0; deal_count=0;
   if(!SessionOK()) return "session";
   // Select the list once and use indexed getters; selecting individual history
   // records would invalidate the list being enumerated.
   if(position==0 || anchor==0 || !HistorySelectByPosition(position)) return "read";
   const int no=HistoryOrdersTotal(), nd=HistoryDealsTotal();
   if(no<0 || nd<0) return "read";
   if(no>128 || nd>128) return "limit";
   ulong order_ids[128], deal_ids[128], ticket, linked_order;
   bool found=false;
   string row, sample="";
   for(int i=0;i<no;i++) {
      if(!RecoveryHistoryOrder(i,position,symbol,ticket,row)) return "read";
      for(int j=0;j<i;j++) if(order_ids[j]==ticket) return "changed";
      order_ids[i]=ticket;
      if(ticket==anchor) found=true;
      sample+=row;
   }
   for(int i=0;i<nd;i++) {
      if(!RecoveryHistoryDeal(i,position,symbol,ticket,linked_order,row)) return "read";
      for(int j=0;j<i;j++) if(deal_ids[j]==ticket) return "changed";
      deal_ids[i]=ticket;
      if(linked_order==anchor) found=true;
      sample+=row;
   }
   if(!SessionOK()) return "session";
   if(no!=HistoryOrdersTotal() || nd!=HistoryDealsTotal()) return "changed";
   if(!found) return "anchor_missing"; // Not proof of no order or no execution.
   rows=sample; order_count=no; deal_count=nd;
   return "ok";
}

string RecoveryHistoryObserve(const ulong position,const ulong anchor,const string symbol,
                              string &rows,int &orders,int &deals) {
   string first,second; int no1,nd1,no2,nd2;
   rows=""; orders=0; deals=0;
   string result=RecoveryHistorySample(position,anchor,symbol,first,no1,nd1);
   if(result!="ok") return result;
   result=RecoveryHistorySample(position,anchor,symbol,second,no2,nd2);
   if(result!="ok") return result;
   if(first!=second || no1!=no2 || nd1!=nd2) return "changed";
   rows=second; orders=no2; deals=nd2;
   return "ok";
}
#endif
