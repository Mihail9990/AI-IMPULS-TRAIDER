static RecoverySupportCloseOwner staged(Fixture &f) {
 RecoverySupportCloseOwner close{};
 assert(begin(close,f)&&step(close,f)==RSCO_PUBLISHED);
 answer=current(f);has_answer=true;
 assert(step(close,f)==RSCO_REENTRY_READY&&close.working_sequence==53);
 return close;
}

static RecoveryExactReport reentry_report(const RecoverySupportCloseOwner &close) {
 const auto &a=close.reentry;
 return {true,true,"ok","current",54,"0",string("E\tcurrent\t0\t18\t")+
    (a.side==1?"4":"5")+"\t1\t"+ticks(a.volume_steps)+"\t"+ticks(a.volume_steps)+"\t"+
    ticks(a.entry_ticks)+"\t"+ticks(a.sl_ticks)+"\t0\t0.0000000000000000"};
}

int main(){
 for(int side:{-1,1}) {
   reset_reentry();
   auto f=prepared(side);
   auto close=staged(f);
   auto &range=f.panel.ranges[0];
   const auto old=range.opposite;
   const auto held=range.pending;
   RecoverySupportReentry task{};
   const auto verifier=close.working.request.binding;
   auto submit=[&]{return RecoverySubmitSupportReentry(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier);};
   auto bind=[&]{return RecoveryBeginSupportReentryVerification(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier,f.verifier);};
   auto advance=[&]{return RecoveryStepSupportReentry(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier);};
   range.opposite.volume_steps++;
   assert(!submit()&&dispatch_calls==0&&close.phase==RSCO_STAGED);
   range.opposite=old;close.key.fingerprint[0]=close.key.fingerprint[0]=='a'?'b':'a';
   assert(!submit()&&dispatch_calls==0);
   assert(RecoveryBuildSessionIntentKey(f.scope,close.reentry,.01,.01,close.key));
   assert(!bind());
   assert(submit()&&task.state==RSRE_RECEIPT&&dispatch_calls==1&&close.phase==RSCO_DISPATCHED);
   assert(task.ticket==18446744073709551601ULL&&range.entry.ticket==0&&range.phase==RR_ARM_REENTRY);
   assert(!submit()&&dispatch_calls==1&&RRActionEqual(held,range.pending));
   assert(!RecoverySubmitSupportReentry(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier));
   auto altered=task;
   altered.ticket++;
   assert(!RecoverySupportReentryReceiptBound(altered,close,f.owner));
   const auto original_receipt=journal_ticket;
   journal_ticket="18446744073709551600";
   assert(!bind());
   journal_ticket=original_receipt;
   const int requests=publications;
   assert(bind()&&!bind()&&task.state==RSRE_REQUEST&&task.prove.verify_after==53);
   assert(task.prove.request.binding.nonce!=close.working.request.binding.nonce);
   assert(advance()==RSRE_PUBLISHED&&publications==requests+1);
   has_answer=false;
   assert(advance()==RSRE_WAIT&&publications==requests+1&&f.owner.revision==7);
   answer=reentry_report(close);has_answer=true;
   assert(advance()==RSRE_CONFIRMED&&task.state==RSRE_WORKING&&f.owner.revision==8);
   assert(range.phase==RR_WAIT&&range.entry.ticket==task.ticket&&RRKnownEqual(range.opposite,old));
   assert(advance()==RSRE_STOPPED&&dispatch_calls==1&&publications==requests+1);
 }
 for(int outcome=0;outcome<5;outcome++) {
   reset_reentry();
   auto f=prepared(1);
   auto close=staged(f);
   const auto verifier=close.working.request.binding;
   RecoverySupportReentry task{};
   if(outcome==0)dispatch_disposition=RD_OUTCOME_UNRESOLVED;
   if(outcome==1)unbound_receipt=true;
   assert(RecoverySubmitSupportReentry(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier));
   assert(!RecoverySubmitSupportReentry(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier));
   const bool bound=RecoveryBeginSupportReentryVerification(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier,f.verifier);
   if(outcome<2)assert(!bound&&task.state==RSRE_UNRESOLVED);
   else {
      assert(bound&&RecoveryStepSupportReentry(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier)==RSRE_PUBLISHED);
      answer=reentry_report(close);has_answer=true;
      if(outcome==2)answer.source="history"; // already filled: defer to dual proof; don't mark working
      if(outcome==3)answer.sequence=53; // recycled sequence
      if(outcome==4)++f.owner.revision;
      assert(RecoveryStepSupportReentry(task,close,f.owner,f.panel,f.session,f.live,f.scope,verifier)==RSRE_STOPPED);
      assert(task.state==RSRE_UNRESOLVED&&task.ticket>0);
   }
   assert(f.panel.ranges[0].phase==RR_ARM_REENTRY&&f.panel.ranges[0].entry.ticket==0&&dispatch_calls==1);
 }
}
