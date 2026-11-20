static void configure(Fixture &f,RecoverySupportRotationOwner &r,int side) {
 RRRangeState &s=f.panel.ranges[0];
 RRKnownOrder survivor{999,-side,400000-side*130,400000-side*50,1};
 s.phase=RR_ARM_REVERSE;s.support_stage=0;s.live_sl=s.initial_sl;
 s.retire=survivor;RRClearKnown(s.opposite);RRClearKnown(s.entry);RRClearAction(s.pending);
 s.opposite_anchor_ticks=s.cycle.actual_entry_ticks-side*80;
 RRAction a{};
 a.kind=RR_MOVE_SUPPORT_OPPOSITE;a.scope=s.scope;a.generation=s.cycle.generation;a.range_id=s.cycle.range_id;
 a.serial=++s.action_serial;a.side=survivor.side;a.order_ticket=survivor.ticket;a.entry_ticks=s.opposite_anchor_ticks;
 a.sl_ticks=s.cycle.actual_entry_ticks;a.volume_steps=survivor.volume_steps;s.pending=a;
 r.plan={};r.plan.reserved=true;r.plan.actual_entry_only=true;r.plan.sl_verified=true;r.plan.move_required=true;
 r.plan.active_order=s.cycle.active_order;r.plan.position_id=s.cycle.position_id;r.plan.position_ticket=s.position_ticket;
 r.plan.previous=survivor;r.plan.move=a;
 r.effect.request={{string(32,'1'),f.session.epoch,f.verifier.verifier_epoch,static_cast<long>(f.scope.generation),
                      f.live.login,f.live.server,f.live.symbol,f.live.magic,f.verifier.verifier_chart},survivor.ticket};
 r.effect.action=a;r.effect.verify_after=50;r.effect.price_tick=.01;r.effect.volume_step=.01;
 assert(RecoveryBuildSessionIntentKey(f.scope,a,.01,.01,r.key));
 r.dispatch.move.state=RD_OUTCOME_UNRESOLVED;r.revision=f.owner.revision;r.phase=RSRM_READ;
 assert(RRSupportRotationBound(s,r.plan)&&RRAltBound(s,f.owner.ledger));
}
static RecoveryExactReport filled_report(const RRKnownOrder &o) {
 return {true,true,"ok","history",53,"919",
    string("E\thistory\t919\t18\t")+(o.side==1?"4":"5")+"\t4\t"+ticks(o.volume_steps)+
     "\t0\t"+ticks(o.entry_ticks)+"\t"+ticks(o.sl_ticks)+"\t0\t0"};
}

int main(){
 for(int side:{-1,1})for(int path=0;path<7;path++){
   auto f=prepared(side);
   RecoverySupportRotationOwner rotation{};
   configure(f,rotation,side);
   RecoverySupportMoveFill task{};
   auto &s=f.panel.ranges[0];
   const auto before=s.cycle;
   const auto old=rotation.plan.previous;
   RRKnownOrder edited;
   RRSetKnown(edited,old.ticket,rotation.plan.move);
   assert(!RRKnownEqual(old,edited));
   move_receipt_known=true;local_history=false;proof_ready=false;next_sends=0;
   const auto v=rotation.effect.request.binding;
   auto begin_move=[&]{return RecoveryBeginSupportMoveFill(task,rotation,f.owner,f.panel,f.session,f.live,f.scope,v,f.verifier);};
   assert(!begin_move()&&f.verifier.serial==8);
   local_history=true;
   assert(begin_move()&&!begin_move()&&f.verifier.serial==9);
   RecoverySessionAlternatingResult result{};
   auto unfold=[&]{return RecoveryStepSupportMoveFill(task,rotation,f.owner,f.panel,f.session,f.live,f.scope,v,result);};
   assert(unfold()==RSMF_PROGRESS&&task.state==RSMF_READ&&next_sends==0);
   const auto chosen=(path==0 || path==5)?old:edited;
   answer=filled_report(chosen);has_answer=true;
   if(path==0)rotation.dispatch.move.synchronous_move_rejected=true;
   if(path==2)rotation.dispatch.move.synchronous_move_rejected=true; // conflicting edited terms
   if(path==3)answer.sequence=50; // stale verifier sequence
   if(path==4)move_receipt_known=false;
   if(path==6)answer=filled_report({edited.ticket,edited.side,old.entry_ticks,edited.sl_ticks,edited.volume_steps});
   if(path>=2 && path!=5) {
      assert(unfold()==RSMF_STOPPED&&task.state==RSMF_UNRESOLVED&&next_sends==0);
      assert(RCCPSameRange(s.cycle,before)&&s.pending.kind==RR_MOVE_SUPPORT_OPPOSITE);
      continue;
   }
   assert(unfold()==RSMF_PROGRESS&&task.state==RSMF_FOLD&&task.edited==(path==1));
   assert(RRKnownEqual(task.chosen,chosen)&&task.pump.binding.history.position_identifier==919);
   assert(s.phase==RR_ARM_REVERSE&&s.retire.ticket==old.ticket&&next_sends==0);
   assert(unfold()==RSMF_WAIT&&next_sends==0);
   proof_ready=true;
   assert(unfold()==RSMF_ADVANCED&&unfold()==RSMF_STOPPED&&next_sends==1);
   assert(task.state==RSMF_DONE&&rotation.phase==RSRM_DONE&&!rotation.plan.move_verified);
   assert(s.cycle.active_order==old.ticket&&s.cycle.position_id==919&&s.cycle.actual_entry_ticks==chosen.entry_ticks+7);
   assert(s.initial_sl==chosen.sl_ticks&&s.live_sl==0); // current read separate; no correction
   assert(f.owner.opening.reverse.entry_ticks==s.cycle.actual_entry_ticks-s.cycle.side*80);
   assert(f.owner.opening.reverse.sl_ticks==s.cycle.actual_entry_ticks&&f.owner.next_exact_ticket==1009);
   assert(f.owner.ledger.confirmed_full_sl_count==2);
 }
}
