struct Fixture {
 RecoveryPanelControl panel;
 RecoveryRangeOwner owner;
 RecoverySessionContext live;
 RecoverySessionLifecycle session;
 RecoverySessionIntentScope scope;
 RecoveryEvidenceOwner verifier;
 RCCloseCheckpoint close;
};

static Fixture prepared(int side) {
 Fixture f{};
 auto cfg=config();
 cfg.support_first_ticks=0;cfg.support_offset_ticks=0;
 f.panel.mode=REC_CONTROL_RANGES;f.panel.settings=cfg;f.panel.configured[0]=true;
 RRRangeState s=first(cfg,side,9007199254740993ULL);
 s.initial_sl=s.live_sl; // native primary keeps the broker attachment
 assert(RRAfterActualEntryNoSL(s,cfg));
 RRAction a{};
 assert(RRStage(s,cfg,0,0,a)&&a.kind==RR_REVERSE);
 assert(RRConfirm(s,{a,true,18446744073709551595ULL})&&s.phase==RR_ACTIVE);
 assert(RRStage(s,cfg,s.cycle.actual_entry_ticks+side*45,s.cycle.actual_entry_ticks+side*45,a));
 assert(a.kind==RR_SUPPORT_SL&&a.stage==2&&RRConfirm(s,{a,true,0}));
 s.cycle.full_sl_count=4;
 f.panel.ranges[0]=s;
 const string epoch(32,'a');
 f.live={10,12,18,"server","XAUUSD.s","profile"};
 assert(RecoverySessionInit(f.session,f.live,true,epoch)==RSL_NEW);
 f.scope={epoch,12,18,"server","XAUUSD.s",cfg.scope,cfg.generation};
 f.verifier={f.live,epoch,string(32,'b'),11,8};
 f.owner.phase=RRO_ORDINARY;f.owner.range_id=1;f.owner.executor_epoch=epoch;
 f.owner.scope=cfg.scope;f.owner.generation=cfg.generation;f.owner.revision=6;
 assert(RRStartAlternation(s,2,f.owner.ledger));
 const auto &r=s.cycle;
 f.close.key={s.scope,99,r.generation,r.range_id,r.position_id,r.active_order};
 f.close.before=r;f.close.reentry_distance=cfg.initial_sl_ticks;
 f.close.proof={r.generation,r.range_id,r.active_order,r.position_id,r.filled_steps,r.filled_steps,
                0,true,true,RC_SL,s.live_sl,RC_SUPPORT_LEVEL,true};
 published=false;has_answer=false;allow_publish=true;
 return f;
}

static bool begin(RecoverySupportCloseOwner &t,Fixture& f) {
 return RecoveryBeginSupportCloseOwner(t,f.owner,f.panel,f.session,f.live,f.scope,f.verifier,f.close,52,.01,.01);
}

static RecoverySupportCloseResult step(RecoverySupportCloseOwner &t,Fixture& f) {
 return RecoveryStepSupportCloseOwner(t,f.owner,f.panel,f.session,f.live,f.scope,t.working.request.binding);
}

static string ticks(long t) {
 return std::to_string(static_cast<double>(t)*.01);
}
static RecoveryExactReport current(const Fixture &f) {
 const auto &o=f.panel.ranges[0].opposite;
 return {true,true,"ok","current",53,"0",
    string("E\tcurrent\t0\t18\t")+(o.side==1?"4":"5")+"\t1\t"+ticks(o.volume_steps)+"\t"+
    ticks(o.volume_steps)+"\t"+ticks(o.entry_ticks)+"\t"+ticks(o.sl_ticks)+"\t0\t0.0000000000000000"};
}

int main(){
 for(int side : {-1,1}){
  auto f=prepared(side);
  RecoverySupportCloseOwner t{};
  auto &s=f.panel.ranges[0];
  const RRKnownOrder old=s.opposite;
  assert(old.entry_ticks!=s.initial_sl&&old.entry_ticks==RROppositeAnchor(s));
  const long stop=s.live_sl;
  const auto checkpoint=f.close;
  const int requests=publications;
  f.close.proof.remaining_steps=1;
  assert(!begin(t,f)&&t.phase==RSCO_IDLE&&f.verifier.serial==8);
  f.close=checkpoint;f.close.proof.confirmed_stop_mask=RC_INITIAL_LEVEL;
  assert(!begin(t,f)&&t.phase==RSCO_IDLE);
  f.close=checkpoint;f.close.proof.exit_stop_ticks-=side;
  assert(!begin(t,f));
  f.close=checkpoint;f.close.reentry_distance++;
  assert(!begin(t,f));
  f.close=checkpoint;f.close.key.observation=0;
  assert(!begin(t,f));
  f.close=checkpoint;
  assert(begin(t,f)&&f.verifier.serial==9&&publications==requests);
  assert(!begin(t,f)&&f.verifier.serial==9);
  assert(t.working.request.order_ticket==old.ticket&&t.working.verify_after==52);
  assert(step(t,f)==RSCO_PUBLISHED&&publications==requests+1);
  assert(step(t,f)==RSCO_WAIT&&s.pending.kind==RR_NONE&&s.cycle.position_id!=0);
  assert(f.owner.ledger.confirmed_full_sl_count==2&&!f.owner.ledger.positions[0].exit_recorded);
  answer=current(f);has_answer=true;
  assert(step(t,f)==RSCO_REENTRY_READY&&t.phase==RSCO_STAGED&&f.owner.revision==7);
  assert(s.phase==RR_ARM_REENTRY&&s.cycle.position_id==0&&s.cycle.full_sl_count==4);
  assert(RRKnownEqual(s.opposite,old)&&s.entry.ticket==0&&RRActionEqual(s.pending,t.reentry));
  assert(t.reentry.kind==RR_REENTRY&&t.reentry.side==side);
  assert(t.reentry.entry_ticks==stop+side*80&&t.reentry.sl_ticks==stop);
  assert(t.reentry.volume_steps==f.panel.settings.lot_steps[4]&&!t.key.id.empty());
  assert(f.owner.ledger.confirmed_full_sl_count==2&&f.owner.ledger.strategy_slot==5);
  assert(f.owner.ledger.positions[0].exit_recorded&&f.owner.ledger.positions[0].exit_class==RC_SUPPORT_SL);
  assert(step(t,f)==RSCO_STOPPED&&publications==requests+1&&f.owner.revision==7);
  RecoverySupportCloseOwner duplicate{};
  assert(!begin(duplicate,f));
 }
 for(int i=0;i<9;++i){
  auto f=prepared(1);
  RecoverySupportCloseOwner t{};
  assert(begin(t,f));
  if(i==8)allow_publish=false;
  const auto p=step(t,f);
  if(i==8){assert(p==RSCO_STOPPED&&f.panel.ranges[0].pending.kind==RR_NONE);continue;}
  assert(p==RSCO_PUBLISHED);
  answer=current(f);has_answer=true;
  if(i==0)answer.sequence=52;                // not observed after close
  if(i==1)answer.source="history";          // previously filled/canceled, not current
  if(i==2)answer.position_identifier="35"; // not an untouched pending order
  if(i==3)answer.observed=false;
  if(i==4)answer.row.replace(answer.row.find("\t1\t")+1,1,"3");  // wrong state
  if(i==5)++f.owner.revision;
  if(i==6)++f.panel.ranges[0].opposite.volume_steps;
  if(i==7)usable_hash=false;                 // no state mutation if key cannot be built
  assert(step(t,f)==RSCO_STOPPED&&t.phase==RSCO_UNRESOLVED);
  usable_hash=true;
  assert(f.panel.ranges[0].phase==RR_ACTIVE&&f.panel.ranges[0].pending.kind==RR_NONE);
  assert(!f.owner.ledger.positions[0].exit_recorded&&f.owner.ledger.confirmed_full_sl_count==2);
 }
 auto f=prepared(-1);
 RecoverySupportCloseOwner t{};
 assert(begin(t,f));
 auto alien=t.working.request.binding;alien.verifier_chart++;
 assert(RecoveryStepSupportCloseOwner(t,f.owner,f.panel,f.session,f.live,f.scope,alien)==RSCO_STOPPED);
}
