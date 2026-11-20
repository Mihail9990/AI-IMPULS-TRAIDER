#ifndef RECOVERY_WATCHED_SUPPORT_MQH
#define RECOVERY_WATCHED_SUPPORT_MQH
#include "../cycle/RecoveryRangeCoordinator.mqh"
#include "../watcher/RecoveryPriceReader.mqh"
#include "RecoveryAdoptionInventory.mqh"

// No trade operations. A watcher response is a signal for one exact active
// position and one settings generation, never permission to modify an SL.
bool RecoveryWatchedScope(const RRRangeState &s,const RRSettings &cfg,
                          const RecoveryPriceScope &request,const double tick)
  {
   long entry,first,second;
   return RRValid(s,cfg) && s.phase==RR_ACTIVE && s.pending.kind==RR_NONE &&
          s.cycle.position_id>0 && request.position_identifier==s.cycle.position_id &&
          s.position_ticket>0 && request.live_ticket==s.position_ticket &&
          request.generation==s.cycle.generation && request.side==s.cycle.side &&
          RecoveryAdoptionUnits(request.actual_entry,tick,false,entry) &&
          entry==s.cycle.actual_entry_ticks &&
          RecoveryAdoptionUnits(request.first_advance,tick,true,first) &&
          first==cfg.support_first_ticks &&
          RecoveryAdoptionUnits(request.second_advance,tick,false,second) &&
          second==cfg.support_second_ticks;
  }

// Call only with a result parsed using the exact scoped challenge.
bool RecoveryWatchedStage(const RecoveryPriceSignal &report,RRRangeState &state,
                          const RRSettings &cfg,const double tick,RRAction &out)
  {
   RRClearAction(out);
   long q;
   if(!report.usable || !report.observed || report.stage<1 || report.stage>2 ||
      (report.stage==1 && cfg.support_first_ticks==0) ||
      !RecoveryAdoptionUnits(report.selected_quote,tick,false,q))return false;
   // For BUY the selected quote is Bid, for SELL it is Ask. The pure reducer
   // only sees that side; using q for both fields conveys no spread assumption.
   RCStopProposal p;
   if(!RCProposeSupport(state.cycle,state.live_sl,state.support_stage,q,q,
      cfg.support_first_ticks,cfg.support_second_ticks,cfg.support_offset_ticks,p) ||
      p.stage!=report.stage) return false;
   return RRStage(state,cfg,q,q,out);
  }

bool RecoveryStageWatchedSupport(const string wire,RRRangeState &state,const RRSettings &cfg,
                                 const RecoveryPriceScope &scope,const long after_sequence,
                                 const double tick,RRAction &out)
  {
   RRClearAction(out);
   if(!RecoveryWatchedScope(state,cfg,scope,tick)) return false;
   RecoveryPriceSignal signal;
   return RecoveryPriceParse(wire,scope,after_sequence,signal) &&
          RecoveryWatchedStage(signal,state,cfg,tick,out);
  }

bool RecoveryReadWatchedSupport(RRRangeState &state,const RRSettings &cfg,
                                const RecoveryPriceScope &scope,const long after_sequence,
                                const double tick,RRAction &out)
  {
   RRClearAction(out);
   if(!RecoveryWatchedScope(state,cfg,scope,tick)) return false;
   RecoveryPriceSignal signal;
   return RecoveryPriceRead(scope,after_sequence,signal) &&
          RecoveryWatchedStage(signal,state,cfg,tick,out);
  }

bool RecoveryReadWatchedPeak(RRRangeState &state,const RRSettings &cfg,
                             const RecoveryPriceScope &scope,const ulong after_sequence,
                             const double tick,RRAction &out)
  {
   RRClearAction(out);
   if(state.support_stage<0 || state.support_stage>=2 ||
      !RecoveryWatchedScope(state,cfg,scope,tick))return false;
   RecoveryPriceSignal signal;
   const int desired=cfg.support_first_ticks==0 ? 2 : state.support_stage+1;
   return RecoveryPricePeakRead(scope,after_sequence,desired,signal) &&
          RecoveryWatchedStage(signal,state,cfg,tick,out);
  }
#endif
