enum RecoveryRangeOwnerPhase {RRO_UNUSED,RRO_ORDINARY};
struct RecoveryRangeOwner {
 RecoveryRangeOwnerPhase phase;int range_id;string executor_epoch;
 ulong scope,generation,revision,next_exact_ticket;
 RROpeningPair opening;RRAlternationLedger ledger;
};
enum RecoveryAlternatingServiceResult {RAS_WAITING,RAS_NO_EFFECT,RAS_ADVANCED};
struct RecoverySessionAlternatingResult {int transition;};
struct MqlTradeTransaction {ulong ticket;};
const int RI_TICKET=2;
RecoveryIntentKey recorded{};
string broker_ticket;
int RecoveryIntentLookup(const RecoveryIntentKey& k,string& ticket) {
 if(k.id!=recorded.id||broker_ticket.empty())return 0;
 ticket=broker_ticket;return RI_TICKET;
}
bool RecoveryIntentKeysMatch(const RecoveryIntentKey& a,const RecoveryIntentKey& b) {return a.id==b.id;}
bool RecoveryRangeOwnerBound(const RecoveryRangeOwner& o,const RecoveryPanelControl& p,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope){
 return RecoveryPrimarySession(life,live,scope)&&o.phase==RRO_ORDINARY&&o.range_id==1&&
    o.executor_epoch==scope.executor_epoch&&o.scope==scope.scope&&o.generation==scope.generation&&
    p.mode==REC_CONTROL_RANGES&&p.configured[0]&&!p.configured[1]&&RRValid(p.ranges[0],p.settings);
}
int arms=0,advances=0;
RRAlternatingObservation proof{};
bool available=false;
bool RecoveryRangeOwnerArmOrdinary(RecoveryRangeOwner& o,const RecoveryPanelControl& p,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope,
 RecoveryEvidenceOwner&,ulong after,double,double) {
 assert(RecoveryRangeOwnerBound(o,p,life,live,scope));
 assert(o.next_exact_ticket==44&&RRAltBound(p.ranges[0],o.ledger)&&after==8);
 ++arms;return true;
}
bool RecoveryRangeOwnerOrdinaryTrade(RecoveryRangeOwner& o,const RecoveryPanelControl& p,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope,
 const RecoveryInventoryScope&,const MqlTradeTransaction& event) {
 return RecoveryRangeOwnerBound(o,p,life,live,scope)&&event.ticket==o.next_exact_ticket;
}
RecoveryAlternatingServiceResult RecoveryRangeOwnerOrdinaryTimer(RecoveryRangeOwner& o,RecoveryPanelControl& p,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RecoverySessionIntentScope& scope,
 const RecoveryInventoryScope&,RecoverySessionAlternatingResult& out) {
 assert(RecoveryRangeOwnerBound(o,p,life,live,scope));
 if(!available)return RAS_WAITING;
 out.transition=RRAdvanceAlternatingFill(p.ranges[0],p.settings,o.opening,o.ledger,proof);
 if(out.transition==RR_ALT_UNRESOLVED)return RAS_NO_EFFECT;
 ++o.revision;o.next_exact_ticket=77;++advances;
 return RAS_ADVANCED;
}

// TEST AFTER FAST FILL
int main(){
 const RecoverySessionContext live{11,5,700,"Test","XAUUSD.s","fixed"};
 RecoverySessionLifecycle life{};
 const string epoch(32,'2'),verifier_epoch(32,'3');
 assert(RecoverySessionInit(life,live,true,epoch)==RSL_NEW);
 const RecoverySessionIntentScope scope{epoch,5,700,"Test","XAUUSD.s",1,7};
 const RecoveryInventoryScope verifier{string(32,'1'),epoch,verifier_epoch,7,5,"Test","XAUUSD.s",700,99};
 RecoveryEvidenceOwner source{live,epoch,verifier_epoch,99,0};
 RRSettings cfg{};cfg.scope=1;cfg.generation=7;cfg.initial_sl_ticks=80;cfg.support_second_ticks=40;
 for(auto& n:cfg.lot_steps)n=1;
 for(int side : {-1,1}){
  RecoveryManualChart manual{};
  RecoveryPanelControl panel{};
  panel.mode=REC_CONTROL_MANUAL_WAIT;
  count=1;readable_inv=false;readable_hist=false;selected_order=false;available=false;
  const long prior=side==1?399965:0,tp=side==1?400150:399850;
  live_sl=prior*.01;live_tp=tp*.01;
  auto step=[&](){return RecoveryManualChartTimer(manual,panel,false,source,life,live,scope,verifier,cfg,.01,.01);};
  assert(step()==RMC_PROGRESS);
  readable_inv=true;inv_text=inv(manual.initial,7,1,pos(side,prior,tp));
  assert(step()==RMC_PROGRESS);
  readable_hist=true;history_text=manual_hist(manual.history,8,side);
  assert(step()==RMC_PROGRESS&&step()==RMC_ATTEMPTED);
  assert(manual.phase==RMC_EFFECT_VISIBLE&&manual.owner.phase==RMO_ATTEMPTED);
  if(side==1)manual.phase=RMC_UNKNOWN; // normal confirmation failed, exact ticket is still known
  const int attempts=sends;
  RecoveryManualFastFill fast{};
  RecoveryManualHandoff adopted{};
  RecoverySessionAlternatingResult result{};
  auto run=[&](){return RecoveryManualFastFillTimer(fast,manual,adopted,panel,life,live,scope,
                                          source,verifier,cfg,.01,.01,result);};
  recorded=manual.owner.reverse_key;broker_ticket="";
  assert(run()==RAS_NO_EFFECT&&!fast.projected.seeded);
  broker_ticket="43";
  assert(run()==RAS_NO_EFFECT&&!fast.projected.seeded);
  broker_ticket="44";
  assert(run()==RAS_WAITING&&fast.projected.seeded&&!adopted.seeded);
  assert(panel.mode==REC_CONTROL_MANUAL_WAIT&&sends==attempts);
  assert(!RecoveryManualFastFillTrade(fast,manual,panel,life,live,scope,verifier,{45}));
  assert(RecoveryManualFastFillTrade(fast,manual,panel,life,live,scope,verifier,{44}));

  const auto reverse=manual.owner.opening.reverse;
  const long actual=reverse.entry_ticks+reverse.side*11;
  proof={};proof.scope=cfg.scope;proof.paired_intent=reverse;proof.paired_order_ticket=44;
  proof.executed_order={44,reverse.side,reverse.entry_ticks,reverse.sl_ticks,reverse.volume_steps};
  proof.fill.entry={cfg.generation,1,44,44,9007199254742001ULL,reverse.side,
       reverse.volume_steps,reverse.volume_steps,reverse.volume_steps,actual,true,true,true};
  proof.fill.position_ticket=9007199254742002ULL;proof.fill.independently_verified=true;
  proof.fill.sl_ticks=0; // ordinary geometry does not wait for the new live SL
  proof.predecessor_inventory_complete=true;proof.predecessor_present=true;
  proof.predecessor_position_id=manual.owner.candidate.position_id;
  proof.predecessor_remaining_steps=manual.owner.candidate.volume_steps;

  available=true;proof.fill.entry.history_complete=false;
  assert(run()==RAS_NO_EFFECT&&!adopted.seeded&&panel.mode==REC_CONTROL_MANUAL_WAIT);
  proof.fill.entry.history_complete=true;proof.paired_order_ticket=47;
  assert(run()==RAS_NO_EFFECT&&!adopted.seeded);
  proof.paired_order_ticket=44;
  assert(run()==RAS_ADVANCED&&result.transition==RR_ALT_NEXT_OPPOSITE&&adopted.seeded);
  assert(panel.mode==REC_CONTROL_ADOPTED&&!panel.configured[0]&&!panel.configured[1]);
  assert(adopted.ordinary.revision==2&&adopted.ordinary.next_exact_ticket==77);
  assert(adopted.ordinary.opening.reverse.entry_ticks==actual+side*80);
  assert(adopted.ordinary.opening.reverse.sl_ticks==actual&&!adopted.ordinary.opening.sl_required);
  assert(adopted.ordinary.ledger.positions[0].detached&&!adopted.ordinary.ledger.positions[0].exit_recorded);
  assert(adopted.ordinary.ledger.positions[0].observed_open_at_handoff);
  assert(adopted.ordinary.ledger.confirmed_full_sl_count==0);
  assert(manual.owner.range.live_sl==prior&&manual.owner.range.live_tp==tp&&manual.owner.phase==RMO_ATTEMPTED);
  assert(run()==RAS_NO_EFFECT&&sends==attempts);
 }
 assert(advances==2&&arms>=2);
}
