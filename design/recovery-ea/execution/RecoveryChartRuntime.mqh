#ifndef RECOVERY_CHART_RUNTIME_MQH
#define RECOVERY_CHART_RUNTIME_MQH
#include "RecoveryLiveSession.mqh"
#include "RecoveryRangeOwner.mqh"
#include "RecoveryWorkingReverse.mqh"

struct RecoveryChartRuntime {
 RecoveryLiveSession live_session;
 RecoveryPanelControl panel;
 RecoveryEvidenceOwner verifier;
 RecoveryRangeOwner ranges[2];
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
   for(int i=0;i<2;i++) {
    ZeroMemory(chart.ranges[i]);ZeroMemory(chart.reverse_probe[i]);
    chart.reverse_probe_position[i]=0;
 }
   ZeroMemory(chart.settings);
   chart.tick=0;chart.step=0;chart.publish_needed=false;
  }

bool RecoveryChartStart(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
   const string profile_scope,const string instance_seed,const string verifier_epoch,
   const long verifier_chart,const RRSettings &settings,const double tick,const double step)
  {
   if(chart.live_session.owns_mail || !RRSettingsValid(settings) ||
      settings.support_first_ticks!=0 || settings.support_offset_ticks!=0 ||
      settings.generation>(ulong)LONG_MAX ||
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

void RecoveryChartRangeTimer(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RecoveryInventoryScope &verifier,const int range_id)
  {
   const int i=range_id-1;
   if(i<0 || i>1 || !chart.panel.configured[i])return;
   if(chart.ranges[i].phase==RRO_UNUSED)
      RecoveryRangeOwnerSubmit(chart.ranges[i],chart.panel,range_id,chart.live_session.lifecycle,
                               live,scope,chart.tick,chart.step);
   if(chart.ranges[i].phase==RRO_PRIMARY_PENDING)
     {
      RecoveryRangeOwnerBindPrimary(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                       live,scope,chart.verifier,0,chart.tick,chart.step);
      RecoveryRangeOwnerPrimaryTimer(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                                     live,scope,verifier);
     }
   if(chart.ranges[i].phase==RRO_ORDINARY)
     {
      RecoveryChartWorkingReverse(chart,live,scope,verifier,i);
      RecoveryRangeOwnerArmOrdinary(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                        live,scope,chart.verifier,0,chart.tick,chart.step);
      RecoverySessionAlternatingResult event_result;
      RecoveryRangeOwnerOrdinaryTimer(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                                      live,scope,verifier,event_result);
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
   return true;
  }

void RecoveryChartOnTrade(RecoveryChartRuntime &chart,const RecoverySessionContext &live,
                         const MqlTradeTransaction &event)
  {
   if(!RecoveryLiveSessionCurrent(chart.live_session,live) || chart.panel.mode!=REC_CONTROL_RANGES)return;
   RecoverySessionIntentScope scope;RecoveryInventoryScope verifier;
   RecoveryChartBindings(chart,live,scope,verifier);
   for(int i=0;i<2;i++)
      RecoveryRangeOwnerOrdinaryTrade(chart.ranges[i],chart.panel,chart.live_session.lifecycle,
                                     live,scope,verifier,event);
  }
#endif
