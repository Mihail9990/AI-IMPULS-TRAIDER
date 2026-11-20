enum RecoveryRangeOwnerPhase {RRO_UNUSED,RRO_ORDINARY};
struct RecoveryRangeOwner {
 RecoveryRangeOwnerPhase phase;int range_id;string executor_epoch;
 ulong scope,generation,revision,next_exact_ticket;
 RROpeningPair opening;RRAlternationLedger ledger;
};
enum RecoveryAlternatingServiceResult {RAS_NO_EFFECT,RAS_ADVANCED};
struct RecoverySessionAlternatingResult {int transition;};
struct MqlTradeTransaction {ulong ticket;};
int armed=0,events=0,ordinary_timers=0;
bool RecoveryRangeOwnerBound(const RecoveryRangeOwner& o,const RecoveryPanelControl& p,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope){
 return RecoveryPrimarySession(life,live,scope)&&o.phase==RRO_ORDINARY&&o.range_id==1&&
    o.executor_epoch==scope.executor_epoch&&o.scope==scope.scope&&o.generation==scope.generation&&
    p.mode==REC_CONTROL_RANGES&&p.configured[0]&&!p.configured[1]&&RRValid(p.ranges[0],p.settings);
}
bool RecoveryRangeOwnerArmOrdinary(RecoveryRangeOwner& o,const RecoveryPanelControl& p,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope,
 RecoveryEvidenceOwner&,ulong,double,double) {
 assert(RecoveryRangeOwnerBound(o,p,life,live,scope));
 assert(o.next_exact_ticket==p.ranges[0].opposite.ticket&&RRAltBound(p.ranges[0],o.ledger));
 ++armed;return true;
}
bool RecoveryRangeOwnerOrdinaryTrade(RecoveryRangeOwner& o,const RecoveryPanelControl& p,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope,
 const RecoveryInventoryScope&,const MqlTradeTransaction& event){
 if(!RecoveryRangeOwnerBound(o,p,life,live,scope)||event.ticket!=o.next_exact_ticket)return false;
 ++events;return true;
}
RecoveryAlternatingServiceResult RecoveryRangeOwnerOrdinaryTimer(RecoveryRangeOwner& o,RecoveryPanelControl& p,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope,
 const RecoveryInventoryScope&,RecoverySessionAlternatingResult& result){
 assert(RecoveryRangeOwnerBound(o,p,life,live,scope));
 ++ordinary_timers;result.transition=1;return RAS_ADVANCED;
}

// TEST AFTER HANDOFF
int main(){
 const RecoverySessionContext live{11,5,700,"Test","XAUUSD.s","fixed"};
 RecoverySessionLifecycle life{};
 const string epoch(32,'2'),verifier_epoch(32,'3');
 assert(RecoverySessionInit(life,live,true,epoch)==RSL_NEW);
 const RecoverySessionIntentScope scope{epoch,5,700,"Test","XAUUSD.s",1,7};
 const RecoveryInventoryScope verifier{string(32,'1'),epoch,verifier_epoch,7,5,"Test","XAUUSD.s",700,99};
 RecoveryEvidenceOwner proof{live,epoch,verifier_epoch,99,0};
 RRSettings cfg{};cfg.scope=1;cfg.generation=7;cfg.initial_sl_ticks=80;cfg.support_second_ticks=40;
 for(auto& n:cfg.lot_steps)n=1;
 for(int side : {-1,1}){
  RecoveryManualChart manual{};
  RecoveryPanelControl panel{};
  panel.mode=REC_CONTROL_MANUAL_WAIT;
  count=1;readable_inv=false;readable_hist=false;selected_order=true;
  const long prior=side==1?399965:0,tp=side==1?400150:399850;
  const long target=prior!=0?prior:400000-side*80;
  live_sl=prior*.01;live_tp=tp*.01;
  auto step=[&](){return RecoveryManualChartTimer(manual,panel,false,proof,life,live,scope,verifier,cfg,.01,.01);};
  assert(step()==RMC_PROGRESS);
  const ulong original_magic=side==1?456:0;
  auto original=pos(side,prior,tp);
  auto m=original.find("\t0\t");assert(m!=string::npos);
  original.replace(m+1,1,std::to_string(original_magic));
  readable_inv=true;inv_text=inv(manual.initial,7,1,original);
  assert(step()==RMC_PROGRESS);
  readable_hist=true;history_text=manual_hist(manual.history,8,side);
  assert(step()==RMC_PROGRESS&&step()==RMC_ATTEMPTED);
  live_sl=target*.01;
  assert(step()==RMC_PROGRESS&&manual.phase==RMC_EFFECT_REPORT);
  RecoveryManualHandoff h{};
  assert(!RecoverySeedManualHandoff(h,manual,panel,life,live,scope,cfg));
  auto confirmed=pos(side,target,tp);
  m=confirmed.find("\t0\t");assert(m!=string::npos);
  confirmed.replace(m+1,1,std::to_string(original_magic));
  inv_text=inv(manual.confirm,9,1,confirmed,opposite(side,target,target+side*80));
  assert(step()==RMC_PROOF&&manual.phase==RMC_ADOPTED);
  auto tampered=manual;tampered.owner.range.live_tp++;
  assert(!RecoverySeedManualHandoff(h,tampered,panel,life,live,scope,cfg));
  tampered=manual;tampered.owner.range.opposite.ticket++;
  assert(!RecoverySeedManualHandoff(h,tampered,panel,life,live,scope,cfg));
  auto other=scope;other.generation++;
  assert(!RecoverySeedManualHandoff(h,manual,panel,life,live,other,cfg));
  const int first_sends=sends;
  assert(RecoverySeedManualHandoff(h,manual,panel,life,live,scope,cfg));
  assert(!RecoverySeedManualHandoff(h,manual,panel,life,live,scope,cfg)&&sends==first_sends);
  assert(panel.mode==REC_CONTROL_ADOPTED&&!panel.configured[0]&&!panel.configured[1]);
  assert(h.backing.configured[0]&&!h.backing.configured[1]&&h.backing.ranges[0].live_tp==tp);
  assert(h.backing.ranges[0].live_sl==target&&h.ordinary.next_exact_ticket==44);
  ulong mag=999;
  assert(RecoveryManualHandoffOrigin(h,h.backing.ranges[0],mag)&&mag==original_magic);
  auto successor=h.backing.ranges[0];successor.cycle.position_id++;successor.position_ticket++;
  assert(!RecoveryManualHandoffOrigin(h,successor,mag));
  assert(!RecoveryManualHandoffTrade(h,panel,life,live,scope,verifier,{45}));
  assert(RecoveryManualHandoffTrade(h,panel,life,live,scope,verifier,{44}));
  RecoverySessionAlternatingResult route{};
  assert(RecoveryManualHandoffTimer(h,panel,life,live,scope,proof,verifier,.01,.01,route)==RAS_ADVANCED);
  assert(route.transition==1&&sends==first_sends);
  panel.mode=REC_CONTROL_IDLE;
  assert(!RecoveryManualHandoffTrade(h,panel,life,live,scope,verifier,{44}));
  assert(RecoveryManualHandoffTimer(h,panel,life,live,scope,proof,verifier,.01,.01,route)==RAS_NO_EFFECT);
 }
 assert(armed==2&&events==2&&ordinary_timers==2);
}
