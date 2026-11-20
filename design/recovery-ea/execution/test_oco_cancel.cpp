#include "RecoveryPanelControl.mqh"
#include "RecoverySessionLifecycle.mqh"
#include "RecoverySessionIntentKey.mqh"
enum {RI_TICKET=4,RI_NO_RECORD=8};
enum RecoveryDispatchState {RD_NOT_SENT,RD_OUTCOME_UNRESOLVED,RD_TICKET_RECORDED};
struct RecoveryDispatchResult {RecoveryDispatchState state;ulong order;};
struct RecoveryPrimarySubmitResult {RRAction action;RecoveryIntentKey key;RecoveryDispatchResult receipt;ulong ticket;};
struct RecoveryInventoryScope {string nonce,executor_epoch,verifier_epoch;long verifier_chart,login,generation,magic;string server,symbol;};
struct RecoveryExactReportScope {RecoveryInventoryScope binding;ulong order_ticket;};
struct RecoveryHistoryScope {RecoveryInventoryScope binding;ulong anchor_order,position_identifier;};
struct RecoveryPrimaryFillBinding {RecoveryExactReportScope exact;RecoveryHistoryScope history;RecoveryInventoryScope inventory;};
struct RecoveryExactEffectBinding {RecoveryExactReportScope request;RRAction action;ulong verify_after;double price_tick,volume_step;};
struct RecoveryExactReport {bool canceled;long sequence;};
const ulong winner_ticket=9007199254740997ULL,loser_ticket=9007199254740999ULL;
bool RecoveryInvScopeOK(const RecoveryInventoryScope &q){return RecoveryInvHex(q.nonce);}
bool RecoveryAlternatingSameSession(const RecoveryInventoryScope &a,const RecoveryInventoryScope &b) {
 return a.executor_epoch==b.executor_epoch && a.verifier_epoch==b.verifier_epoch &&
        a.verifier_chart==b.verifier_chart && a.login==b.login && a.magic==b.magic &&
        a.generation==b.generation && a.server==b.server && a.symbol==b.symbol;
}
bool RecoveryPrimarySession(const RecoverySessionLifecycle &s,const RecoverySessionContext &l,const RecoverySessionIntentScope &q){
 return RecoverySessionCurrent(s,l)&&s.epoch==q.executor_epoch&&l.login==q.login&&l.magic==q.magic&&
        l.server==q.server&&l.symbol==q.symbol;
}
bool RecoveryPrimaryOpenBound(const RecoveryPrimaryFillBinding &,const RecoverySessionLifecycle &s,
   const RecoverySessionContext &l,const RecoverySessionIntentScope &q,const RRRangeState &){
 return RecoveryPrimarySession(s,l,q);
}
bool RecoveryStagePrimaryOpening(RRRangeState &,const RRSettings &,const RecoveryPrimaryFillBinding &,
    const RRFilled &,RROpeningPair &,RRAlternationLedger &){return false;}
RecoveryIntentKey stored;
bool registration=true,allow_send=true,can_publish=true,read_ready=false;
int attempts=0,published=0;
RecoveryExactReport report{true,11};
bool RecoveryIntentKeysMatch(const RecoveryIntentKey &a,const RecoveryIntentKey &b){
 return a.id==b.id && a.fingerprint==b.fingerprint;
}
int RecoveryIntentLookup(const RecoveryIntentKey &key,string &t){
 t="";
 if(!registration || !RecoveryIntentKeysMatch(stored,key))return RI_NO_RECORD;
 t=std::to_string(loser_ticket);return RI_TICKET;
}
bool RecoveryExactEffectBound(const RRRangeState &v,const RecoveryExactEffectBinding &b){
 return b.action.kind==RR_CANCEL && RRActionEqual(v.pending,b.action) &&
        v.entry.ticket==b.request.order_ticket;
}
RecoveryDispatchState RecoveryDispatchOnce(const RRRangeState &v,const RRAction &a,const RecoveryIntentKey &key,
    double,double,RecoveryDispatchResult &result) {
 assert(v.phase==RR_DRAIN && a.kind==RR_CANCEL && a.order_ticket==loser_ticket &&
        v.entry.ticket==loser_ticket && !v.opposite.ticket && !v.position_ticket &&
        key.id!=stored.id && RRActionEqual(v.pending,a));
 ++attempts;result.state=allow_send ? RD_OUTCOME_UNRESOLVED : RD_NOT_SENT;
 return result.state;
}
bool RecoveryExactReportRequest(const RecoveryExactReportScope &r,const string &publisher){
 assert(r.order_ticket==loser_ticket&&publisher==string(32,'6'));
 ++published;return can_publish;
}
bool RecoveryExactReportRead(const RecoveryExactReportScope &,RecoveryExactReport &r){
 if(!read_ready)return false;
 r=report;return true;
}
bool RecoveryExactEffectFold(const RecoveryExactReport &r,const RecoveryExactEffectBinding &b,RREffect &e){
 if(!r.canceled || r.sequence<=0 || (ulong)r.sequence<=b.verify_after)return false;
 e={b.action,true,0};return true;
}
#include "OcoCancelUnderTest"
int main(){
 const auto cfg=config();
 RecoverySessionContext live{7,12,18,"server","XAUUSD.s","settings"};
 const string epoch(32,'a');
 RecoverySessionLifecycle s{};
 assert(RecoverySessionInit(s,live,true,epoch)==RSL_NEW);
 RecoverySessionIntentScope scope{epoch,12,18,"server","XAUUSD.s",cfg.scope,cfg.generation};
 RecoveryPanelControl panel{};
 RecoveryControlCommand cmd{REC_CONTROL_CONFIGURE,2,{{1,1,400000},{2,-1,399800}}};
 assert(RecoveryApplyPanelControl(panel,cmd,cfg)==REC_CONTROL_APPLIED);
 auto &winner=panel.ranges[0];auto &loser=panel.ranges[1];
 RRAction placed{};
 assert(RRStage(winner,cfg,0,0,placed) && RRConfirm(winner,{placed,true,winner_ticket}));
 RRFilled f{{cfg.generation,1,winner_ticket,winner_ticket,123,1,1,1,1,400002,true,true,true},
              456,0,399920,0,true};
 assert(RRObserveFill(winner,cfg,f));
 RecoveryPrimarySubmitResult primary{};
 assert(RRStage(loser,cfg,0,0,primary.action));
 assert(RecoveryBuildSessionIntentKey(scope,primary.action,.01,.01,primary.key));
 stored=primary.key;
 primary.ticket=loser_ticket;primary.receipt={RD_TICKET_RECORDED,loser_ticket};
 RecoveryInventoryScope q{string(32,'1'),epoch,string(32,'b'),99,12,7,18,"server","XAUUSD.s"};
 RecoveryPrimaryFillBinding b{{q,winner_ticket},{q,winner_ticket,123},q};
 b.history.binding.nonce=string(32,'2');b.inventory.nonce=string(32,'3');
 RecoveryExactReportScope request{q,loser_ticket};request.binding.nonce=string(32,'4');
 RecoveryOcoCancel c{};
 auto begin=[&](){return RecoveryBeginOcoCancel(c,s,live,scope,panel,1,b,f,2,primary,request,10,string(32,'6'),.01,.01);};
 auto step=[&](){return RecoveryStepOcoCancel(c,s,live,scope,panel);};
 f.entry.history_complete=false;assert(!begin());f.entry.history_complete=true;
 primary.ticket++;assert(!begin());primary.ticket--;
 request.binding.nonce=b.exact.binding.nonce;assert(!begin());request.binding.nonce=string(32,'4');
 registration=false;assert(!begin());registration=true;
 assert(begin() && !begin() && attempts==0 && loser.phase==RR_NEW);
 assert(step()==ROC_DISPATCH_CHECKED && attempts==1 && loser.phase==RR_NEW);
 assert(step()==ROC_REQUESTED && published==1 && attempts==1);
 assert(step()==ROC_WAIT && published==1 && attempts==1);
 read_ready=true;
 assert(step()==ROC_CANCELED && c.state==ROC_DONE && loser.phase==RR_NEW);
 assert(step()==ROC_UNKNOWN && attempts==1);
 // Already changed/unknown losing target: the typed writer declines, then no repeats.
 c={};assert(begin());allow_send=false;
 assert(step()==ROC_UNKNOWN && attempts==2 && published==1);allow_send=true;
 assert(step()==ROC_UNKNOWN && attempts==2);
 c={};assert(begin() && step()==ROC_DISPATCH_CHECKED && step()==ROC_REQUESTED);
 report.canceled=false;
 assert(step()==ROC_UNKNOWN && c.state==ROC_UNRESOLVED && attempts==3);report.canceled=true;
 c={};assert(begin() && step()==ROC_DISPATCH_CHECKED && step()==ROC_REQUESTED);
 report.sequence=10;
 assert(step()==ROC_UNKNOWN && attempts==4);report.sequence=11;
 c={};assert(begin());loser.pending.serial++;
 assert(step()==ROC_UNKNOWN && attempts==4);
}
