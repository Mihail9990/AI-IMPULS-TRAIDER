#ifndef RECOVERY_CHART_RUNTIME_MQH
#define RECOVERY_CHART_RUNTIME_MQH
#include "RecoveryLiveSession.mqh"
#include "RecoveryRangeOwner.mqh"
#include "RecoveryWorkingReverse.mqh"
#include "RecoveryOcoCancel.mqh"
#include "RecoverySupportRuntime.mqh"
#include "RecoveryRangeClosePoll.mqh"
#include "RecoverySupportCloseOwner.mqh"

struct RecoveryChartRuntime {
 RecoveryLiveSession live_session;
 RecoveryPanelControl panel;
 RecoveryEvidenceOwner verifier;
 RecoveryRangeOco oco;
 RecoveryOcoCancel losing_stop;
 RecoveryOcoCancelResult cancel_result;
 bool cancel_started,cancel_skipped;
 RecoveryRangeOwner ranges[2];
 RecoverySupportRuntime support[2];
 RecoveryRangeClosePoll close_poll[2];
 RCCloseCheckpoint close_proof[2];
 RecoverySupportCloseOwner support_exit[2];
 ulong close_position[2];
 string watcher_epoch;
 long watcher_chart;
 RecoveryWorkingReverse reverse_probe[2];
 ulong reverse_probe_position[2];
 RRSettings settings;
 double tick,step;
 bool publish_needed;
};

// In-session trading chart controller, not a release entrypoint. Remaining
// owner paths (manual adoption, support exit, close/cancel) are wired apart.
void RecoveryChartStop(RecoveryChartRuntime &chart)
  {
   RecoveryLiveSessionStop(chart.live_session);
   ZeroMemory(chart.panel);ZeroMemory(chart.verifier);
   ZeroMemory(chart.oco);ZeroMemory(chart.losing_stop);
   chart.cancel_started=false;chart.cancel_skipped=false;chart.cancel_result=ROC_WAIT;
   for(int i=0;i<2;i++) {
    ZeroMemory(chart.ranges[i]);ZeroMemory(chart.reverse_probe[i]);
    ZeroMemory(chart.support[i]);ZeroMemory(chart.close_poll[i]);ZeroMemory(chart.close_proof[i]);
    ZeroMemory(chart.support_exit[i]);
    chart.close_position[i]=0;
    chart.reverse_probe_position[i]=0;
 }
   ZeroMemory(chart.settings);
   chart.tick=0;chart.step=0;chart.publish_needed=false;chart.watcher_epoch="";chart.watcher_chart=0;
  }

bool RecoveryChartStart(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
   const string profile_scope,const string instance_seed,const string verifier_epoch,
   const long verifier_chart,const RRSettings &settings,const double tick,const double step,
   const string watcher_epoch="",const long watcher_chart=0)
  {
   if(chart.live_session.owns_mail || !RRSettingsValid(settings) ||
      settings.support_first_ticks!=0 || settings.support_offset_ticks!=0 ||
      settings.generation>(ulong)LONG_MAX ||
      ((watcher_epoch!="" || watcher_chart!=0) && (!RecoveryInvHex(watcher_epoch) ||
          watcher_chart<=0 || watcher_chart==live.chart || watcher_chart==verifier_chart ||
          watcher_epoch==verifier_epoch)) ||
      !MathIsValidNumber(tick) || tick<=0 || !MathIsValidNumber(step) || step<=0) return false;
   RecoveryChartStop(chart);
   if(!RecoveryLiveSessionStart(chart.live_session,live,profile_scope,instance_seed))return false;
   if(!RecoveryBeginEvidenceOwner(chart.verifier,chart.live_session.lifecycle,live,
                                  verifier_epoch,verifier_chart) ||
      !RecoveryLiveSessionPublish(chart.live_session,live,REC_CONTROL_IDLE))
     {
      RecoveryChartStop(chart);return false;
     }
   chart.settings=settings;chart.tick=tick;chart.step=step;
   chart.watcher_epoch=watcher_epoch;chart.watcher_chart=watcher_chart;
   return true;
  }

void RecoveryChartBindings(const RecoveryChartRuntime &chart,const RecoverySessionContext &live,
                             RecoverySessionIntentScope &scope,RecoveryInventoryScope &verifier)
  {
   scope.executor_epoch=chart.live_session.lifecycle.epoch;scope.login=live.login;
   scope.server=live.server;scope.symbol=live.symbol;scope.magic=live.magic;
   scope.scope=chart.settings.scope;scope.generation=chart.settings.generation;
   // This is context only; no query is published with this nonce.
   verifier.nonce=scope.executor_epoch;
   verifier.executor_epoch=scope.executor_epoch;verifier.verifier_epoch=chart.verifier.verifier_epoch;
   verifier.generation=(long)scope.generation;verifier.login=live.login;verifier.server=live.server;
   verifier.symbol=live.symbol;verifier.magic=live.magic;verifier.verifier_chart=chart.verifier.verifier_chart;
  }

// Verify the known reverse while it is resting. Fill proof has priority:
// after a fill hint is queued or is already being processed, do not change
// its binding underneath it. Missing callbacks are covered by exact polling.
void RecoveryChartWorkingReverse(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,const int i)
  {
   if(!chart.ranges[i].opening.reserved || chart.ranges[i].opening.sl_required ||
      chart.ranges[i].next_exact_ticket==0 ||
      chart.ranges[i].ordinary.hint.state==RFH_QUEUED ||
      chart.ranges[i].ordinary.pump.state!=RAP_UNUSED) return;
   const ulong position=chart.panel.ranges[i].cycle.position_id;
   if(position==0)return;
   if(chart.reverse_probe_position[i]!=position)
     {
      ZeroMemory(chart.reverse_probe[i]);chart.reverse_probe_position[i]=position;
     }
   if(chart.reverse_probe[i].state==RWR_IDLE)
     {
      RecoveryIntentKey key;
      if(!RecoveryBuildSessionIntentKey(scope,chart.ranges[i].opening.reverse,chart.tick,chart.step,key))
         return;
      RecoveryExactReportScope request;
      request.binding=verifier;request.order_ticket=chart.ranges[i].next_exact_ticket;
      const string identity=scope.executor_epoch+":"+key.id+":"+(string)request.order_ticket;
      request.binding.nonce=RecoveryOwnerNonce("working-exact",identity);
      const string tag=RecoveryOwnerNonce("working-publisher",identity);
      if(!RecoveryBeginWorkingReverse(chart.reverse_probe[i],chart.live_session.lifecycle,live,
           scope,chart.panel.ranges[i],chart.ranges[i].opening,key,request,0,tag,chart.tick,chart.step))
         return;
     }
   if(RecoveryStepWorkingReverse(chart.reverse_probe[i],chart.live_session.lifecycle,live,scope,
           chart.panel.ranges[i],chart.ranges[i].opening)==RWR_ACTIVE)
     {
      RecoveryOrdinaryEventReset(chart.ranges[i].ordinary);
      chart.ranges[i].armed_position=0;
     }
  }

// Cancel only the other range's recorded primary STOP. The winning
// range's opposite order is not a candidate for this action.
void RecoveryChartOcoTimer(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
   const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier)
  {
   const int winner=chart.oco.winner_range_id;
   if(winner<1 || winner>2 || chart.cancel_skipped)return;
   const int win=winner-1,other=2-winner;
   if(!chart.panel.configured[other] || chart.ranges[other].primary.ticket==0 ||
      chart.ranges[win].first_proof.entry.executed_order==0)
     {chart.cancel_skipped=true;return;}
   if(!chart.cancel_started)
     {
      RecoveryExactReportScope q;
      q.binding=verifier;q.order_ticket=chart.ranges[other].primary.ticket;
      const string identity=scope.executor_epoch+":"+chart.ranges[other].primary.key.id+":"+
         (string)q.order_ticket+":"+(string)chart.oco.winner_order;
      q.binding.nonce=RecoveryOwnerNonce("loser-cancel-exact",identity);
      const string tag=RecoveryOwnerNonce("loser-cancel-publisher",identity);
      if(!RecoveryBeginOcoCancel(chart.losing_stop,chart.live_session.lifecycle,live,scope,chart.panel,
           winner,chart.ranges[win].primary_pump.binding,chart.ranges[win].first_proof,
           other+1,chart.ranges[other].primary,q,0,tag,chart.tick,chart.step))
        {chart.cancel_skipped=true;return;}
      chart.cancel_started=true;
     }
   if(chart.cancel_result!=ROC_CANCELED && chart.cancel_result!=ROC_UNKNOWN)
      chart.cancel_result=RecoveryStepOcoCancel(chart.losing_stop,chart.live_session.lifecycle,live,
                                                scope,chart.panel);
   if(chart.cancel_result==ROC_CANCELED)chart.panel.configured[other]=false;
  }

// Known positions only. Generated evidence cannot qualify an SL from a
// price quote or from the prior submit receipt.
void RecoveryChartCloseFact(const RRRangeState &range,const int stop_class,
                           const long stop,RCProtectionFact &fact)
  {
   fact.scope=range.scope;fact.generation=range.cycle.generation;
   fact.range_id=range.cycle.range_id;fact.position_id=range.cycle.position_id;
   fact.anchor_order=range.cycle.active_order;fact.stop_ticks=stop;
   fact.stop_class=stop_class;fact.confirmed=RCBoundedPositive(stop);
  }

void RecoveryChartSupportTimer(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,const int i)
  {
   if(chart.watcher_chart==0 || chart.watcher_epoch=="")return;
   RecoverySupportRuntimeTimer(chart.support[i],chart.verifier,chart.live_session.lifecycle,
        live,scope,verifier,chart.watcher_epoch,chart.watcher_chart,
        chart.panel.ranges[i],chart.panel.settings,chart.tick,chart.step);
   const RRRangeState range=chart.panel.ranges[i];
   if(range.cycle.position_id==0 || range.position_ticket==0)return;
   if(chart.close_position[i]!=range.cycle.position_id)
     {
      chart.close_position[i]=range.cycle.position_id;
      ZeroMemory(chart.close_poll[i]);ZeroMemory(chart.close_proof[i]);
    ZeroMemory(chart.support_exit[i]);
     }
   if(chart.close_poll[i].state==RCP_IDLE &&
      !RecoveryBeginRangeClosePoll(chart.close_poll[i],chart.verifier,chart.live_session.lifecycle,
           live,verifier,range,chart.panel.settings,chart.tick,chart.step))return;
   if(chart.close_poll[i].state==RCP_UNRESOLVED)return;
   if(chart.close_poll[i].state==RCP_DONE)
     {
      const RCCloseCheckpoint verified=chart.close_proof[i];
      if(RCClassifyClose(verified.before,verified.proof)==RC_SUPPORT_SL)
        {
         if(chart.support_exit[i].phase==RSCO_IDLE &&
            !RecoveryBeginSupportCloseOwner(chart.support_exit[i],chart.ranges[i],chart.panel,
                    chart.live_session.lifecycle,live,scope,chart.verifier,verified,
                    chart.close_poll[i].verified_sequence,chart.tick,chart.step))return;
         if(chart.support_exit[i].phase==RSCO_REQUEST || chart.support_exit[i].phase==RSCO_READ)
            RecoveryStepSupportCloseOwner(chart.support_exit[i],chart.ranges[i],chart.panel,
                            chart.live_session.lifecycle,live,scope,verifier);
        }
      return;
     }
   RCProtectionFact facts[2];
   RecoveryChartCloseFact(range,RC_INITIAL_LEVEL,range.initial_sl,facts[0]);
   RecoveryChartCloseFact(range,RC_SUPPORT_LEVEL,
      range.support_stage==2 && range.live_sl==range.cycle.actual_entry_ticks ? range.live_sl : 0,facts[1]);
   const RecoveryClosePollResult closed=RecoveryRangeClosePollStep(chart.close_poll[i],
      chart.live_session.lifecycle,live,range,chart.panel.settings,facts,chart.close_proof[i]);
   if(closed==RCP_PROOF && chart.ranges[i].revision<ULONG_MAX &&
      RCClassifyClose(chart.close_proof[i].before,chart.close_proof[i].proof)==RC_FULL_SL &&
      RRRecordAlternatingClose(chart.ranges[i].ledger,chart.close_proof[i])==RR_ALT_CLOSE_APPLIED)
      ++chart.ranges[i].revision;
  }

void RecoveryChartRangeTimer(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,const int range_id)
  {
   const int i=range_id-1;
   if(i<0 || i>1 || !chart.panel.configured[i] ||
      (chart.oco.winner_range_id!=0 && chart.oco.winner_range_id!=range_id))return;
   if(chart.ranges[i].phase==RRO_UNUSED)
      RecoveryRangeOwnerSubmit(chart.ranges[i],chart.panel,range_id,chart.live_session.lifecycle,
                               live,scope,chart.tick,chart.step);
   if(chart.ranges[i].phase==RRO_PRIMARY_PENDING)
     {
      RecoveryRangeOwnerBindPrimary(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                       live,scope,chart.verifier,0,chart.tick,chart.step);
      RecoveryRangeOwnerPrimaryTimer(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                                     live,scope,verifier,chart.oco);
     }
   if(chart.ranges[i].phase==RRO_PRIMARY_CLAIMED)
     {
      RecoveryChartOcoTimer(chart,live,scope,verifier);
      // Do not await any broker or verifier reply to the sibling cancel.
      RecoveryRangeOwnerDispatchClaimedPrimary(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                                               live,scope,chart.oco);
     }
   if(chart.ranges[i].phase==RRO_ORDINARY)
     {
      RecoveryChartWorkingReverse(chart,live,scope,verifier,i);
      RecoveryRangeOwnerArmOrdinary(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                        live,scope,chart.verifier,0,chart.tick,chart.step);
      RecoverySessionAlternatingResult event_result;
      RecoveryRangeOwnerOrdinaryTimer(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                                      live,scope,verifier,event_result);
      RecoveryChartSupportTimer(chart,live,scope,verifier,i);
     }
  }

// Called by the trading chart's timer, only after explicit user start.
bool RecoveryChartOnTimer(RecoveryChartRuntime &chart,const RecoverySessionContext &live)
  {
   if(!RecoveryLiveSessionCurrent(chart.live_session,live))return false;
   RecoveryPanelApplyResult action;
   if(RecoveryPanelMailboxPoll(chart.live_session.mailbox,chart.live_session.mail,
          chart.live_session.lifecycle,live,chart.settings,chart.tick,
          chart.live_session.panel_seen,chart.panel,action))
      chart.publish_needed=true;
   if(chart.publish_needed &&
      RecoveryLiveSessionPublish(chart.live_session,live,chart.panel.mode))chart.publish_needed=false;
   if(chart.panel.mode!=REC_CONTROL_RANGES)return true;
   RecoverySessionIntentScope scope;RecoveryInventoryScope verifier;
   RecoveryChartBindings(chart,live,scope,verifier);
   for(int id=1;id<=2;id++)
      if(RecoveryLiveSessionCurrent(chart.live_session,live))
         RecoveryChartRangeTimer(chart,live,scope,verifier,id);
   if(RecoveryLiveSessionCurrent(chart.live_session,live))
      RecoveryChartOcoTimer(chart,live,scope,verifier);
   return true;
  }

void RecoveryChartOnTrade(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
                         const MqlTradeTransaction &event)
  {
   if(!RecoveryLiveSessionCurrent(chart.live_session,live) || chart.panel.mode!=REC_CONTROL_RANGES)return;
   RecoverySessionIntentScope scope;RecoveryInventoryScope verifier;
   RecoveryChartBindings(chart,live,scope,verifier);
   for(int i=0;i<2;i++)
      if(chart.oco.winner_range_id==i+1)
         RecoveryRangeOwnerOrdinaryTrade(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                                     live,scope,verifier,event);
  }
#endif
