#ifndef RECOVERY_CLOSED_MONEY_MQH
#define RECOVERY_CLOSED_MONEY_MQH
// Independent close-deal economics. This file cannot mutate strategy state,
// approve another order or infer closure from a broker receipt.
const int RECOVERY_CLOSE_MONEY_LIMIT=128;
struct RecoveryCloseMoneyScope {
   long login;
   string server,symbol,currency;
   ulong position_identifier;
   int position_side;  // +1 BUY, -1 SELL
   long total_exit_steps;
   double volume_step;
};
struct RecoveryCloseMoneyExit {
   ulong ticket,position_identifier,closing_order;
   int exit_side;
   long steps;
};
struct RecoveryCloseMoneyRow {
   RecoveryCloseMoneyExit key;
   double profit,commission,swap,fee; // signed, denominated in account currency
};
struct RecoveryCloseMoneyTotal {
   bool observed;
   int deal_count;
   long closed_steps;
   double profit,commission,swap,fee,net,loss;
};
void RecoveryCloseMoneyClear(RecoveryCloseMoneyTotal &out) {
   out.observed=false; out.deal_count=0; out.closed_steps=0;
   out.profit=0; out.commission=0; out.swap=0; out.fee=0; out.net=0; out.loss=0;
}
bool RecoveryCloseMoneyScopeOK(const RecoveryCloseMoneyScope &s) {
   return s.login>0 && s.server!="" && s.symbol!="" && s.currency!="" && s.position_identifier>0 &&
          (s.position_side==1 || s.position_side==-1) && s.total_exit_steps>0 &&
          s.total_exit_steps<=1000000000000 && MathIsValidNumber(s.volume_step) && s.volume_step>0;
}
bool RecoveryCloseMoneyEqual(const RecoveryCloseMoneyExit &a,const RecoveryCloseMoneyExit &b) {
   return a.ticket==b.ticket && a.position_identifier==b.position_identifier &&
          a.closing_order==b.closing_order && a.exit_side==b.exit_side && a.steps==b.steps;
}
bool RecoveryCloseMoneyExpected(const RecoveryCloseMoneyScope &s,const RecoveryCloseMoneyExit &exits[]) {
   const int n=ArraySize(exits);
   if(!RecoveryCloseMoneyScopeOK(s) || n<1 || n>RECOVERY_CLOSE_MONEY_LIMIT) return false;
   long steps=0;
   for(int i=0;i<n;i++) {
      if(exits[i].ticket==0 || exits[i].closing_order==0 ||
         exits[i].position_identifier!=s.position_identifier || exits[i].exit_side!=-s.position_side ||
         exits[i].steps<=0 || exits[i].steps>s.total_exit_steps-steps) return false;
      for(int j=0;j<i;j++) if(exits[i].ticket==exits[j].ticket) return false;
      steps+=exits[i].steps;
   }
   return steps==s.total_exit_steps;
}
bool RecoveryCloseMoneyNumber(const double value) {
   return MathIsValidNumber(value) && MathAbs(value)<=1000000000000.0;
}
void RecoveryCloseMoneyAdd(const double value,double &sum,double &lost) {
   const double delta=value-lost;
   const double next=sum+delta;
   lost=(next-sum)-delta; sum=next;
}

// The caller supplies a complete, already verified list of close deals. Order
// and position identifiers are not interchangeable; each ticket is used once.
bool RecoveryCloseMoneyAggregate(const RecoveryCloseMoneyScope &s,const RecoveryCloseMoneyExit &exits[],
                                 const RecoveryCloseMoneyRow &rows[],RecoveryCloseMoneyTotal &out) {
   RecoveryCloseMoneyClear(out);
   const int n=ArraySize(exits);
   if(!RecoveryCloseMoneyExpected(s,exits) || ArraySize(rows)!=n) return false;
   double p=0,c=0,w=0,f=0,pe=0,ce=0,we=0,fe=0;
   // Canonical accumulation order is that of the independently verified list.
   for(int i=0;i<n;i++) {
      int match=-1;
      for(int j=0;j<n;j++) if(rows[j].key.ticket==exits[i].ticket) {
         if(match!=-1) return false;
         match=j;
      }
      if(match<0 || !RecoveryCloseMoneyEqual(exits[i],rows[match].key) ||
         !RecoveryCloseMoneyNumber(rows[match].profit) || !RecoveryCloseMoneyNumber(rows[match].commission) ||
         !RecoveryCloseMoneyNumber(rows[match].swap) || !RecoveryCloseMoneyNumber(rows[match].fee)) return false;
      RecoveryCloseMoneyAdd(rows[match].profit,p,pe);
      RecoveryCloseMoneyAdd(rows[match].commission,c,ce);
      RecoveryCloseMoneyAdd(rows[match].swap,w,we);
      RecoveryCloseMoneyAdd(rows[match].fee,f,fe);
   }
   const double net=(p+c)+(w+f);
   if(!MathIsValidNumber(net)) return false;
   out.observed=true; out.deal_count=n; out.closed_steps=s.total_exit_steps;
   out.profit=p; out.commission=c; out.swap=w; out.fee=f; out.net=net; out.loss=net<0 ? -net : 0;
   return true;
}
bool RecoveryCloseMoneySession(const RecoveryCloseMoneyScope &s) {
   return TerminalInfoInteger(TERMINAL_CONNECTED)!=0 && AccountInfoInteger(ACCOUNT_LOGIN)==s.login &&
          AccountInfoString(ACCOUNT_SERVER)==s.server && AccountInfoString(ACCOUNT_CURRENCY)==s.currency;
}
bool RecoveryCloseMoneyVolume(const double value,const double step,const long steps) {
   const double expected=step*(double)steps;
   if(!MathIsValidNumber(value) || value<=0 || !MathIsValidNumber(expected) || expected<=0) return false;
   const double tolerance=4.4408920985006262e-16*MathMax(value,expected);
   return MathIsValidNumber(tolerance) && tolerance<=step*0.000001 && MathAbs(value-expected)<=tolerance;
}
bool RecoveryCloseMoneyReadOne(const RecoveryCloseMoneyScope &s,const RecoveryCloseMoneyExit &expect,
                               RecoveryCloseMoneyRow &row) {
   const ulong deal=expect.ticket;
   long actual,position,order,entry,type;
   string symbol;
   double volume,p,c,w,f;
   if(!RecoveryCloseMoneySession(s) || !HistoryDealSelect(deal) ||
      !HistoryDealGetInteger(deal,DEAL_TICKET,actual) || (ulong)actual!=deal ||
      !HistoryDealGetInteger(deal,DEAL_POSITION_ID,position) || (ulong)position!=expect.position_identifier ||
      !HistoryDealGetInteger(deal,DEAL_ORDER,order) || (ulong)order!=expect.closing_order ||
      !HistoryDealGetInteger(deal,DEAL_ENTRY,entry) || entry!=DEAL_ENTRY_OUT ||
      !HistoryDealGetInteger(deal,DEAL_TYPE,type) ||
      type!=(expect.exit_side==1 ? DEAL_TYPE_BUY : DEAL_TYPE_SELL) ||
      !HistoryDealGetString(deal,DEAL_SYMBOL,symbol) || symbol!=s.symbol ||
      !HistoryDealGetDouble(deal,DEAL_VOLUME,volume) || !RecoveryCloseMoneyVolume(volume,s.volume_step,expect.steps) ||
      !HistoryDealGetDouble(deal,DEAL_PROFIT,p) || !RecoveryCloseMoneyNumber(p) ||
      !HistoryDealGetDouble(deal,DEAL_COMMISSION,c) || !RecoveryCloseMoneyNumber(c) ||
      !HistoryDealGetDouble(deal,DEAL_SWAP,w) || !RecoveryCloseMoneyNumber(w) ||
      !HistoryDealGetDouble(deal,DEAL_FEE,f) || !RecoveryCloseMoneyNumber(f) ||
      !RecoveryCloseMoneySession(s)) return false;
   row.key=expect; row.profit=p; row.commission=c; row.swap=w; row.fee=f;
   return true;
}

// Two exact history passes; do not call inside another active history-list
// enumeration because HistoryDealSelect changes the selected deals list.
bool RecoveryReadClosedMoney(const RecoveryCloseMoneyScope &s,const RecoveryCloseMoneyExit &exits[],
                             RecoveryCloseMoneyTotal &out) {
   RecoveryCloseMoneyClear(out);
   if(!RecoveryCloseMoneyExpected(s,exits) || !RecoveryCloseMoneySession(s)) return false;
   const int n=ArraySize(exits);
   RecoveryCloseMoneyRow first[];
   if(ArrayResize(first,n)!=n) return false;
   for(int i=0;i<n;i++) if(!RecoveryCloseMoneyReadOne(s,exits[i],first[i])) return false;
   RecoveryCloseMoneyRow second;
   for(int i=0;i<n;i++) {
      if(!RecoveryCloseMoneyReadOne(s,exits[i],second) ||
         first[i].profit!=second.profit || first[i].commission!=second.commission ||
         first[i].swap!=second.swap || first[i].fee!=second.fee) return false;
   }
   return RecoveryCloseMoneySession(s) && RecoveryCloseMoneyAggregate(s,exits,first,out);
}
#endif
