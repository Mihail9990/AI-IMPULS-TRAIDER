#ifndef RECOVERY_MONEY_JOURNAL_MQH
#define RECOVERY_MONEY_JOURNAL_MQH
#include "../verifier/RecoveryClosedMoney.mqh"
#include "../cycle/RecoveryCloseCheckpoint.mqh"
#include "RecoverySessionIntentKey.mqh"

// Only the successful close-proof path enqueues work. A missing amount never
// changes the strategy state, and a new manual cycle does not clear the queue.
struct RecoveryMoneyJob {
   RecoveryCloseMoneyScope scope;
   RecoveryCloseMoneyExit exits[];
   string evidence,base;
   bool pending_written;
};
RecoveryMoneyJob RecoveryMoneyJobs[];
int RecoveryMoneyCursor=0;

bool RecoveryMoneyCapture(const RCCloseWireBinding &b,const RecoveryHistorySnapshot &history,
                         const RCCloseCheckpoint &closed,RecoveryMoneyJob &job) {
   if(!RCCloseKeysEqual(b.key,closed.key) || !RCCloseWireScope(b) ||
      !closed.proof.independently_verified || !closed.proof.history_complete ||
      closed.proof.position_id!=b.key.position_id || closed.proof.order!=b.key.anchor_order ||
      closed.before.position_id!=b.key.position_id || closed.before.active_order!=b.key.anchor_order ||
      closed.proof.remaining_steps!=0 || closed.proof.exited_steps!=closed.before.filled_steps ||
      !RCValidSide(closed.before.side) || !RCBoundedPositive(closed.proof.exited_steps) ||
      !history.usable || !history.observed || history.reason!="ok" || history.sequence<=b.history_after ||
      history.order_count<1 || history.order_count>128 || history.deal_count<1 || history.deal_count>128 ||
      ArraySize(history.rows)!=history.order_count+history.deal_count)return false;

   job.scope.login=b.inventory.login; job.scope.server=b.inventory.server; job.scope.symbol=b.inventory.symbol;
   job.scope.position_identifier=b.key.position_id; job.scope.position_side=closed.before.side;
   job.scope.total_exit_steps=closed.proof.exited_steps; job.scope.volume_step=b.step;
   job.scope.currency=""; job.pending_written=false;
   if(ArrayResize(job.exits,history.deal_count)!=history.deal_count)return false;
   string rows[];
   if(ArrayResize(rows,history.deal_count)!=history.deal_count)return false;
   int count=0;
   long sum=0;
   for(int i=history.order_count;i<ArraySize(history.rows);i++) {
      string fields[];
      ulong kind;
      if(StringSplit(history.rows[i],9,fields)!=12 || fields[0]!="D" ||
         !RecoveryAdoptionUnsigned(fields[5],kind))return false;
      if(kind==DEAL_ENTRY_IN)continue;
      RecoveryCloseMoneyExit exit;
      exit.position_identifier=b.key.position_id; exit.exit_side=-closed.before.side;
      if(kind!=DEAL_ENTRY_OUT || fields[4]!=(exit.exit_side==1 ? "BUY" : "SELL") ||
         !RecoveryAdoptionUnsigned(fields[1],exit.ticket) || exit.ticket==0 ||
         !RecoveryAdoptionUnsigned(fields[2],exit.closing_order) || exit.closing_order==0 ||
         !RecoveryAdoptionUnits(fields[8],b.step,false,exit.steps) ||
         exit.steps>job.scope.total_exit_steps-sum)return false;
      // A later proof may enumerate the same deals in a different order.
      int at=count;
      while(at>0 && job.exits[at-1].ticket>exit.ticket) {
         job.exits[at]=job.exits[at-1];rows[at]=rows[at-1];at--;
      }
      if(at>0 && job.exits[at-1].ticket==exit.ticket)return false;
      job.exits[at]=exit;
      rows[at]=fields[1]+"\t"+fields[2]+"\t"+fields[4]+"\t"+fields[8]+"\t"+fields[9]+"\t"+fields[10]+"\n";
      sum+=exit.steps;count++;
   }
   if(sum!=job.scope.total_exit_steps || count==0 || ArrayResize(job.exits,count)!=count)return false;
   const string account=RecoverySessionSha256("RECOVERY_MONEY_V1\t"+(string)job.scope.login+"\t"+job.scope.server);
   if(StringLen(account)!=64)return false;
   job.base="RECOVERY_PANEL\\closed-money\\"+account+"-"+(string)b.key.position_id;
   job.evidence="RECOVERY_CLOSED_MONEY_V1\nserver\taccount\tsymbol\tposition_id\tentry_order\tclosed_steps\tvolume_step\n"+
      job.scope.server+"\t"+(string)job.scope.login+"\t"+job.scope.symbol+"\t"+(string)b.key.position_id+"\t"+
      (string)b.key.anchor_order+"\t"+(string)sum+"\t"+DoubleToString(b.step,-16)+
      "\nexit_deal\tclosing_order\tside\tvolume\texecution_price\tdeal_sl\n";
   for(int i=0;i<count;i++)job.evidence+=rows[i];
   return true;
}

bool RecoveryMoneySameFile(const string path,const uchar &bytes[],const int count) {
   const int h=FileOpen(path,FILE_READ|FILE_BIN|FILE_SHARE_READ);
   if(h==INVALID_HANDLE)return false;
   uchar observed[];
   bool ok=FileSize(h)==(ulong)count && FileReadArray(h,observed,0,count)==(uint)count;
   FileClose(h);
   if(ok)for(int i=0;i<count;i++)if(observed[i]!=bytes[i])return false;
   return ok;
}

// Never append or replace a completed result. A competing identical publication
// counts as success; a conflict or short write stays unresolved.
bool RecoveryMoneyWriteOnce(const string path,const string text) {
   uchar bytes[];
   const int n=StringToCharArray(text,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(n<=0 || n>65536)return false;
   if(FileIsExist(path))return RecoveryMoneySameFile(path,bytes,n);
   const string tmp=path+"."+(string)ChartID()+".writing";
   const int h=FileOpen(tmp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE)return false;
   const uint written=FileWriteArray(h,bytes,0,n);
   FileFlush(h);FileClose(h);
   if(written==(uint)n && RecoveryMoneySameFile(tmp,bytes,n) && FileMove(tmp,0,path,0))return true;
   FileDelete(tmp);
   return RecoveryMoneySameFile(path,bytes,n);
}

bool RecoveryMoneyMarkPending(RecoveryMoneyJob &job) {
   if(!job.pending_written) {
      FolderCreate("RECOVERY_PANEL\\closed-money");
      job.pending_written=RecoveryMoneyWriteOnce(job.base+".pending.tsv",job.evidence+"status\tpending\n");
   }
   return job.pending_written;
}

bool RecoveryMoneyEnqueue(const RCCloseWireBinding &binding,const RecoveryHistorySnapshot &history,
                          const RCCloseCheckpoint &closed) {
   RecoveryMoneyJob job;
   if(!RecoveryMoneyCapture(binding,history,closed,job))return false;
   const int n=ArraySize(RecoveryMoneyJobs);
   for(int i=0;i<n;i++)
      if(RecoveryMoneyJobs[i].base==job.base)return RecoveryMoneyJobs[i].evidence==job.evidence;
   if(ArrayResize(RecoveryMoneyJobs,n+1,8)!=n+1)return false;
   RecoveryMoneyJobs[n]=job;
   RecoveryMoneyMarkPending(RecoveryMoneyJobs[n]);
   return true; // A failed local marker write is buffered separately from the trading path.
}

void RecoveryMoneyFlushPending() {
   for(int i=0;i<ArraySize(RecoveryMoneyJobs);i++)RecoveryMoneyMarkPending(RecoveryMoneyJobs[i]);
}

bool RecoveryMoneyRecord(RecoveryMoneyJob &job,const long login,const string server,const string symbol) {
   if(login!=job.scope.login || server!=job.scope.server || symbol!=job.scope.symbol)return false;
   const string marker=job.base+".pending.tsv",result=job.base+".tsv";
   if(!RecoveryMoneyMarkPending(job))return false;
   if(job.scope.currency=="") {
      const string currency=AccountInfoString(ACCOUNT_CURRENCY);
      if(!RecoveryInvContext(currency) || AccountInfoInteger(ACCOUNT_LOGIN)!=login ||
         AccountInfoString(ACCOUNT_SERVER)!=server)return false;
      job.scope.currency=currency;
   }
   RecoveryCloseMoneyTotal amount;
   if(!RecoveryReadClosedMoney(job.scope,job.exits,amount) || !amount.observed)return false;
   const string output=job.evidence+"status\tcurrency\tprofit\tcommission\tswap\tfee\tnet\tloss\nobserved\t"+
      job.scope.currency+"\t"+DoubleToString(amount.profit,-16)+"\t"+DoubleToString(amount.commission,-16)+"\t"+
      DoubleToString(amount.swap,-16)+"\t"+DoubleToString(amount.fee,-16)+"\t"+
      DoubleToString(amount.net,-16)+"\t"+DoubleToString(amount.loss,-16)+"\n";
   if(!RecoveryMoneyWriteOnce(result,output))return false;
   if(FileIsExist(marker) && !FileDelete(marker))return false;
   return true;
}

// Run after the trading callback. Visit at most one proved position per timer
// and rotate even on missing history or I/O failure.
void RecoveryMoneyJournalTimer(const long login,const string server,const string symbol) {
   const int n=ArraySize(RecoveryMoneyJobs);
   if(n==0)return;
   if(RecoveryMoneyCursor<0 || RecoveryMoneyCursor>=n)RecoveryMoneyCursor=0;
   const int i=RecoveryMoneyCursor;
   if(!RecoveryMoneyRecord(RecoveryMoneyJobs[i],login,server,symbol)) {
      RecoveryMoneyCursor=(i+1)%n;
      return;
   }
   if(i!=n-1)RecoveryMoneyJobs[i]=RecoveryMoneyJobs[n-1];
   ArrayResize(RecoveryMoneyJobs,n-1);
}
#endif
