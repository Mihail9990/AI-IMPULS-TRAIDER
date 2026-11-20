struct RecoveryEvidenceOwner {
 RecoverySessionContext context;
 string executor_epoch,verifier_epoch;
 long verifier_chart;
 ulong serial;
};
enum {POSITION_SYMBOL,POSITION_IDENTIFIER,POSITION_TIME_UPDATE_MSC};
const ulong position_id=9007199254740993ULL;
long edit=82;
int count=1,pending_inv=0,pending_hist=0;
bool readable_inv=false,readable_hist=false,selected_order=false;
string last_inv,inv_text,history_text;
RecoveryHistoryScope history_request{};
int PositionsTotal(){return count;}
ulong PositionGetTicket(int n){return live_ticket+n;}
bool PositionGetString(int field,string& s){assert(field==POSITION_SYMBOL);s="XAUUSD.s";return true;}
bool PositionGetInteger(int field,long& out){
 if(field==POSITION_IDENTIFIER)out=static_cast<long>(position_id);
 else {assert(field==POSITION_TIME_UPDATE_MSC);out=edit;}
 return true;
}
bool OrderSelect(ulong o){return selected_order&&o==44;}
bool RecoveryOpeningFillStillThere(const RRRangeState& r,const RROpeningPair& pair,
    const RecoveryIntentKey& key,double,double){
 return r.position_ticket==live_ticket&&r.cycle.position_id==position_id&&RROpeningBound(r,pair)&&
        RecoveryIntentContextOK(key);
}
string RecoveryOwnerNonce(const string& lane,const string& identity){
 string out;
 const char* hex="0123456789abcdef";
 const auto n=std::hash<string>{}(lane+"\t"+identity);
 for(int i=0;i<32;++i)out+=hex[(n>>((i%16)*4))&15];
 return out;
}
bool RecoveryRequestInventory(const string& nonce,const string&,const string&,long,long,
    const string&,const string&,long){
 ++pending_inv;last_inv=nonce;return true;
}
bool RecoveryInvRead(const RecoveryInventoryScope& q,RecoveryInventorySnapshot& v){
 return readable_inv&&q.nonce==last_inv&&RecoveryInvParse(inv_text,q,v);
}
bool RecoveryHistoryRequest(const RecoveryHistoryScope& h,const string& tag){
 assert(tag!=h.binding.nonce);
 ++pending_hist;history_request=h;return true;
}
bool RecoveryHistoryRead(const RecoveryHistoryScope& q,RecoveryHistorySnapshot& d){
 return readable_hist&&q.binding.nonce==history_request.binding.nonce&&RecoveryHistoryParse(history_text,q,d);
}
string manual_hist(const RecoveryHistoryScope& q,int seq,int side) {
 string h=hist(q,seq,side);
 const string id=std::to_string(q.anchor_order);
 auto p=h.find("\t17");assert(p!=string::npos);h.replace(p+1,2,id);
 p=h.find("\t17",p+id.size());assert(p!=string::npos);h.replace(p+1,2,id);
 p=h.find("\t17",p+id.size());assert(p!=string::npos);h.replace(p+1,2,id);
 return h;
}

// TEST AFTER ADAPTER
int main(){
 const RecoverySessionContext live{11,5,700,"Test","XAUUSD.s","fixed"};
 RecoverySessionLifecycle life{};
 const string epoch(32,'2'),verifier_epoch(32,'3');
 assert(RecoverySessionInit(life,live,true,epoch)==RSL_NEW);
 const RecoverySessionIntentScope scope{epoch,5,700,"Test","XAUUSD.s",1,7};
 const RecoveryInventoryScope verifier{string(32,'1'),epoch,verifier_epoch,7,5,"Test","XAUUSD.s",700,99};
 RecoveryEvidenceOwner proof{live,epoch,verifier_epoch,99,0};
 RRSettings cfg{};cfg.scope=1;cfg.generation=7;cfg.initial_sl_ticks=80;
 cfg.support_second_ticks=40;
 for(auto& n:cfg.lot_steps)n=1;
 for(int side:{1,-1}) {
  RecoveryManualChart task{};
  RecoveryPanelControl panel{};
  count=1;readable_hist=false;readable_inv=false;selected_order=false;
  const int before=sends,requests=pending_inv,hreq=pending_hist;
  const auto step=[&](bool active=false) {
   return RecoveryManualChartTimer(task,panel,active,proof,life,live,scope,verifier,cfg,.01,.01);
  };
  RecoveryControlCommand cmd{REC_CONTROL_WAIT,0,{}};
  assert(step()==RMC_NO_ACTION&&pending_inv==requests);
  assert(RecoveryApplyPanelControl(panel,cmd,cfg)==REC_CONTROL_APPLIED);
  assert(RecoveryManualChartMayCancel(task)&&step(true)==RMC_NO_ACTION);
  count=0;assert(step()==RMC_WAIT);
  count=2;assert(step()==RMC_WAIT&&pending_inv==requests);
  count=1;assert(step()==RMC_PROGRESS&&pending_inv==requests+1&&task.phase==RMC_INVENTORY);
  assert(step()==RMC_WAIT&&pending_hist==hreq);
  const auto first_nonce=task.initial.nonce;
  // Cancel while waiting is honored before side effects and a fresh arm gets
  // a different exact challenge, even if the same position stays open.
  assert(RecoveryManualChartMayCancel(task));
  cmd.op=REC_CONTROL_CANCEL;
  assert(RecoveryApplyPanelControl(panel,cmd,cfg)==REC_CONTROL_APPLIED);
  assert(step()==RMC_NO_ACTION&&task.phase==RMC_IDLE);
  cmd.op=REC_CONTROL_WAIT;
  assert(RecoveryApplyPanelControl(panel,cmd,cfg)==REC_CONTROL_APPLIED);
  assert(step()==RMC_PROGRESS&&task.initial.nonce!=first_nonce&&pending_inv==requests+2);
  const long prior=side==1?399965:0,tp=side==1?400150:399850;
  const long target=prior==0?400000-side*80:prior;
  live_sl=prior*.01;live_tp=tp*.01;
  readable_inv=true;inv_text=inv(task.initial,7,1,pos(side,prior,tp));
  assert(step()==RMC_PROGRESS&&task.phase==RMC_HISTORY&&pending_hist==hreq+1);
  assert(history_request.position_identifier==position_id&&history_request.anchor_order==position_id);
  assert(history_request.anchor_order!=live_ticket);
  assert(step()==RMC_WAIT);
  readable_hist=true;history_text=manual_hist(task.history,8,side);
  assert(step()==RMC_PROGRESS&&task.phase==RMC_READY&&sends==before);
  assert(step()==RMC_ATTEMPTED&&task.phase==RMC_EFFECT_VISIBLE&&sends==before+1);
  assert(task.owner.opening.sl_required==(prior==0)&&task.owner.opening.reverse.entry_ticks==target);
  assert(task.owner.range.live_tp==tp&&!RecoveryManualChartMayCancel(task));
  assert(!RecoveryManualChartClearWaiting(task)&&step()==RMC_WAIT);
  selected_order=true;live_sl=target*.01;
  assert(step()==RMC_PROGRESS&&task.phase==RMC_EFFECT_REPORT&&pending_inv==requests+3);
  assert(step()==RMC_WAIT); // prior inventory has wrong nonce
  inv_text=inv(task.confirm,9,1,pos(side,target,tp),opposite(side,target,target+side*80));
  assert(step()==RMC_PROOF&&task.phase==RMC_ADOPTED&&panel.mode==REC_CONTROL_ADOPTED);
  assert(task.owner.range.live_sl==target&&task.owner.range.live_tp==tp&&task.owner.range.opposite.ticket==44);
  assert(step()==RMC_NO_ACTION&&sends==before+1);
 }
 assert(sl_arms==1&&sends==2);

 for(bool key_known : {false,true}) {
  RecoveryManualChart task{};
  RecoveryPanelControl panel{};
  panel.mode=REC_CONTROL_MANUAL_WAIT;
  count=1;readable_inv=false;readable_hist=false;selected_order=true;
  const auto step=[&]() {
   return RecoveryManualChartTimer(task,panel,false,proof,life,live,scope,verifier,cfg,.01,.01);
  };
  const long sl=399965,tp=400150;
  live_sl=sl*.01;live_tp=tp*.01;
  const int before=sends;
  assert(step()==RMC_PROGRESS);
  readable_inv=true;inv_text=inv(task.initial,7,1,pos(1,sl,tp));
  assert(step()==RMC_PROGRESS);
  readable_hist=true;history_text=manual_hist(task.history,8,1);
  assert(step()==RMC_PROGRESS&&task.phase==RMC_READY);
  receipt_known=key_known;
  if(key_known)live_tp+=.01; // last local recheck, before calling broker
  if(key_known) {
   assert(step()==RMC_WAIT&&RecoveryManualChartMayCancel(task)&&sends==before);
  } else {
   assert(step()==RMC_NO_ACTION&&task.phase==RMC_UNKNOWN&&sends==before+1);
   assert(!RecoveryManualChartMayCancel(task)&&!RecoveryManualChartClearWaiting(task));
   assert(step()==RMC_NO_ACTION&&sends==before+1);
  }
 }
}

