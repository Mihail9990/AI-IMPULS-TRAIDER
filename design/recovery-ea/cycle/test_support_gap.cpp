#include <cassert>
#include <cstdint>
#include <limits>
#include <initializer_list>
using ulong=std::uint64_t;
constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();
#include "RecoverySupportGap.mqh"

static RRSettings config() {
 RRSettings c{};c.scope=1;c.generation=7;c.initial_sl_ticks=80;c.support_first_ticks=25;c.support_second_ticks=40;c.support_offset_ticks=25;
 for(int n=1;n<=24;n++)c.lot_steps[n-1]=RCDefaultLotHundredths(n);
 return c;
}
static RRFilled fill(const RRRangeState &s,const RRKnownOrder &o,ulong id,long actual) {
 return {{s.cycle.generation,s.cycle.range_id,o.ticket,o.ticket,id,o.side,
          o.volume_steps,o.volume_steps,o.volume_steps,actual,true,true,true},
          id+100,0,o.sl_ticks,0,true};
}
static RRRangeState supported(const RRSettings &cfg,int side) {
 RRRangeState s{};RRAction action{};
 assert(RRNewRange(s,cfg,1,side,400000));
 assert(RRStage(s,cfg,0,0,action)&&RRConfirm(s,{action,true,50}));
 s.cycle.full_sl_count=4;
 assert(RRObserveFill(s,cfg,fill(s,s.entry,601,400000)));
 RROpeningPair opening{};
 assert(RRStageOpeningPair(s,cfg,opening));
 assert(RRConfirmOpeningPair(s,opening,{opening.reverse,true,51})==RR_PAIR_CONFIRMED);
 const long bid=side==1?400060:399910,ask=side==1?400090:399940;
 assert(RRStage(s,cfg,bid,ask,action)&&action.kind==RR_SUPPORT_SL);
 assert(RRConfirm(s,{action,true,0})&&s.support_stage==2&&s.live_sl==400000);
 return s;
}
static RCCloseCheckpoint close(const RRRangeState &s) {
 const auto &r=s.cycle;
 RCCloseCheckpoint c{};
 c.key={s.scope,99,r.generation,r.range_id,r.position_id,r.active_order};
 c.before=r;c.reentry_distance=80;
 c.proof={r.generation,r.range_id,r.active_order,r.position_id,r.filled_steps,r.filled_steps,
          0,true,true,RC_SL,s.live_sl,RC_SUPPORT_LEVEL,true};
 return c;
}
int main() {
 const auto cfg=config();
 for(int side : {1,-1}) for(bool first_close : {false,true}) {
   auto s=supported(cfg,side);
   const auto fixed=s.opposite;
   assert(fixed.volume_steps==2&&fixed.entry_ticks==400000-side*80);
   auto f=fill(s,fixed,602,fixed.entry_ticks+fixed.side*7);
   auto c=close(s);
   RRSupportGap gap{};
   RROpeningPair opening{};RRAction only{};
   assert(RRStartSupportGap(s,gap));
   assert(!RRStartSupportGap(s,gap));
   auto false_c=c;false_c.proof.exit_stop_ticks=s.initial_sl;
   assert(!RRStageSupportGapClose(s,gap,false_c));
   false_c=c;false_c.proof.confirmed_stop_mask=RC_INITIAL_LEVEL;
   assert(!RRStageSupportGapClose(s,gap,false_c));
   false_c=c;false_c.proof.remaining_steps=1;
   assert(!RRStageSupportGapClose(s,gap,false_c));
   auto false_f=f;false_f.entry.expected_order++;
   assert(!RRStageSupportGapFill(s,gap,fixed,false_f));
   false_f=f;false_f.entry.remainder_reconciled=false;
   assert(!RRStageSupportGapFill(s,gap,fixed,false_f));
   auto substituted=fixed;substituted.volume_steps++;
   assert(!RRStageSupportGapFill(s,gap,substituted,f));
   if(first_close) {
      assert(RRStageSupportGapClose(s,gap,c));
      assert(RRStageSupportGapClose(s,gap,c));
      assert(!RRReleaseSupportGapToReentry(s,gap,fixed,false));
   } else {
      assert(RRStageSupportGapFill(s,gap,fixed,f));
      assert(!RRReleaseSupportGapToReentry(s,gap,fixed,true));
   }
   assert(RRAdvanceSupportGap(s,cfg,gap,opening,only)==RR_GAP_UNRESOLVED);
   assert(s.cycle.position_id==601&&s.cycle.full_sl_count==4&&s.pending.kind==RR_NONE);
   if(first_close)assert(RRStageSupportGapFill(s,gap,fixed,f));
   else assert(RRStageSupportGapClose(s,gap,c));
   assert(RRStageSupportGapFill(s,gap,fixed,f));
   assert(!RRReleaseSupportGapToReentry(s,gap,fixed,true));
   assert(RRAdvanceSupportGap(s,cfg,gap,opening,only)==RR_GAP_NEXT_PAIR);
   assert(!gap.reserved&&s.cycle.position_id==602&&s.cycle.full_sl_count==4&&s.cycle.last_closed_position==601);
   assert(s.entry.ticket==0&&s.reentry_slot==0&&s.retire.ticket==0);
   assert(opening.sl_required&&opening.reverse.kind==RR_REVERSE&&opening.reverse.side==side);
   assert(opening.sl.sl_ticks==f.entry.actual_entry_ticks+side*80);
   assert(opening.reverse.entry_ticks==opening.sl.sl_ticks&&opening.reverse.sl_ticks==f.entry.actual_entry_ticks);
   assert(opening.reverse.volume_steps==cfg.lot_steps[5]);
   assert(RRAdvanceSupportGap(s,cfg,gap,opening,only)==RR_GAP_UNRESOLVED);
 }
 auto s=supported(cfg,1); RRSupportGap gap{}; RRAction action{};
 auto working=s.opposite;
 assert(RRStartSupportGap(s,gap)&&RRStageSupportGapClose(s,gap,close(s)));
 ++working.entry_ticks;
 assert(!RRReleaseSupportGapToReentry(s,gap,working,true));
 --working.entry_ticks;
 assert(RRReleaseSupportGapToReentry(s,gap,working,true));
 assert(s.phase==RR_ARM_REENTRY&&s.cycle.full_sl_count==4&&RRKnownEqual(s.opposite,working));
 assert(RRStage(s,cfg,0,0,action)&&action.kind==RR_REENTRY&&action.entry_ticks==400080);
}
