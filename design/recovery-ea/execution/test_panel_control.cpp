#include "RecoveryPanelSessionApply.mqh"
int main() {
 const auto cfg=config();
 RecoveryPanelControl state{};
 RecoveryControlCommand wait{},cancel{},ranges{};
 wait.op=REC_CONTROL_WAIT;
 cancel.op=REC_CONTROL_CANCEL;
 ranges.op=REC_CONTROL_CONFIGURE;ranges.count=2;
 ranges.ranges[0]={2,-1,395000};
 ranges.ranges[1]={1,1,400000};
 assert(RecoveryApplyPanelControl(state,wait,cfg)==REC_CONTROL_APPLIED);
 assert(state.mode==REC_CONTROL_MANUAL_WAIT);
 assert(RecoveryApplyPanelControl(state,ranges,cfg)==REC_CONTROL_REJECTED);
 assert(RecoveryApplyPanelControl(state,wait,cfg)==REC_CONTROL_REJECTED);
 assert(RecoveryApplyPanelControl(state,cancel,cfg)==REC_CONTROL_APPLIED);
 state.mode=REC_CONTROL_ADOPTED;
 assert(RecoveryApplyPanelControl(state,cancel,cfg)==REC_CONTROL_REJECTED);
 assert(state.mode==REC_CONTROL_ADOPTED);
 state.mode=REC_CONTROL_IDLE;
 auto duplicate=ranges;duplicate.ranges[1].id=2;
 assert(RecoveryApplyPanelControl(state,duplicate,cfg)==REC_CONTROL_REJECTED);
 assert(state.mode==REC_CONTROL_IDLE&&!state.configured[0]&&!state.configured[1]);
 auto invalid=ranges;invalid.ranges[1].price_ticks=0;
 assert(RecoveryApplyPanelControl(state,invalid,cfg)==REC_CONTROL_REJECTED);
 assert(state.mode==REC_CONTROL_IDLE&&!state.configured[0]&&!state.configured[1]);
 assert(RecoveryApplyPanelControl(state,ranges,cfg)==REC_CONTROL_APPLIED);
 assert(state.mode==REC_CONTROL_RANGES&&state.configured[0]&&state.configured[1]);
 assert(RecoveryApplyPanelControl(state,wait,cfg)==REC_CONTROL_REJECTED);
 assert(RecoveryApplyPanelControl(state,ranges,cfg)==REC_CONTROL_REJECTED);
 for(int i=0;i<2;i++) {
   const int expected_side=i==0?1:-1;const long expected_entry=i==0?400000:395000;
   RRAction action{};
   assert(RRStage(state.ranges[i],state.settings,0,0,action));
   assert(action.kind==RR_PRIMARY&&action.side==expected_side&&action.entry_ticks==expected_entry);
   assert(action.sl_ticks==expected_entry-expected_side*cfg.initial_sl_ticks);
 }
 state={};ranges.count=1;ranges.ranges[0]={2,1,400200};
 assert(RecoveryApplyPanelControl(state,ranges,cfg)==REC_CONTROL_APPLIED);
 assert(!state.configured[0]&&state.configured[1]&&state.ranges[1].primary_side==1);

 state={};
 const RecoverySessionContext live{10,20,30,"server","XAUUSD.s","active"};
 RecoverySessionLifecycle life{};
 const string epoch(32,'a');
 assert(RecoverySessionInit(life,live,true,epoch)==RSL_NEW);
 RecoveryPanelCommandScope mail{"scope","aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","context","generation"};
 RecoveryPanelApplySession ram;
 RecoveryPanelApplyResult r;
 assert(RecoveryDecodeAndStagePanel("wait","id1",mail,life,live,cfg,.01,ram,state,r)==RECP_STAGED);
 assert(state.mode==REC_CONTROL_MANUAL_WAIT);
 assert(RecoveryDecodeAndStagePanel("cancel","id2",mail,life,live,cfg,.01,ram,state,r)==RECP_STAGED);
 assert(state.mode==REC_CONTROL_IDLE);
 assert(RecoveryDecodeAndStagePanel("wait","id1",mail,life,live,cfg,.01,ram,state,r)==RECP_DUPLICATE);
 assert(r.original==RECP_STAGED&&state.mode==REC_CONTROL_IDLE);
 assert(RecoveryDecodeAndStagePanel("cancel","id1",mail,life,live,cfg,.01,ram,state,r)==RECP_CONFLICT);
 assert(RecoveryDecodeAndStagePanel("ranges","id3",mail,life,live,cfg,.01,ram,state,r)==RECP_STAGED);
 assert(state.mode==REC_CONTROL_RANGES&&state.ranges[0].primary_ticks==400000);
 assert(RecoveryDecodeAndStagePanel("wait","id4",mail,life,live,cfg,.01,ram,state,r)==RECP_REJECTED);
 assert(RecoveryDecodeAndStagePanel("wait","id4",mail,life,live,cfg,.01,ram,state,r)==RECP_DUPLICATE);
 assert(r.original==RECP_REJECTED&&ArraySize(ram.seen)==4);
 const auto original=mail;mail.boot_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
 assert(RecoveryDecodeAndStagePanel("wait","id5",mail,life,live,cfg,.01,ram,state,r)==RECP_INVALID);
 assert(RecoveryDecodeAndStagePanel("bad","id5",original,life,live,cfg,.01,ram,state,r)==RECP_INVALID);
 RecoveryClearPanelApply(ram);RecoverySessionReset(life);state={};
 assert(RecoverySessionInit(life,live,true,string(32,'b'))==RSL_NEW);
 assert(RecoveryDecodeAndStagePanel("wait","id1",original,life,live,cfg,.01,ram,state,r)==RECP_INVALID);
 assert(RecoveryDecodeAndStagePanel("wait","id1",mail,life,live,cfg,.01,ram,state,r)==RECP_STAGED);
}
