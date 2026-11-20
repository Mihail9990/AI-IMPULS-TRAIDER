#include <cassert>
#include <cstdint>
#include <limits>
using ulong=std::uint64_t;
constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();
#include "RecoveryRangeCoordinator.mqh"

static RRSettings config() {
 RRSettings c{};c.scope=7;c.generation=11;
 c.initial_sl_ticks=70;c.support_first_ticks=25;c.support_second_ticks=40;c.support_offset_ticks=25;
 for(int n=1;n<=24;n++)c.lot_steps[n-1]=RCDefaultLotHundredths(n);
 return c;
}
static RRFilled fill(const RRRangeState& s,const RRKnownOrder &o,ulong id,long price,ulong other=0) {
 return {{s.cycle.generation,s.cycle.range_id,o.ticket,o.ticket,id,o.side,
          o.volume_steps,o.volume_steps,o.volume_steps,price,true,true,true},
          id+100,other,o.sl_ticks,0,true};
}
static RCCloseCheckpoint close(const RRRangeState &s,int reason,int cls,long level) {
 const auto &r=s.cycle;
 RCCloseCheckpoint p{};
 p.key={s.scope,123,r.generation,r.range_id,r.position_id,r.active_order};
 p.before=r;p.reentry_distance=70;
 p.proof={r.generation,r.range_id,r.active_order,r.position_id,
          r.filled_steps,r.filled_steps,0,true,true,reason,level,cls,true};
 return p;
}
int main() {
 const RRSettings c=config();
 RRRangeState s{},second{};
 RRAction a{},again{};
 assert(RRNewRange(s,c,1,1,301000));
 assert(RRNewRange(second,c,2,-1,301050));
 assert(!RRNewRange(second,c,2,-1,301050));
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_PRIMARY&&a.entry_ticks==301000&&a.sl_ticks==300930);
 assert(!RRStage(s,c,0,0,again)&&again.kind==RR_NONE);
 assert(!RRConfirm(s,{a,false,50}));
 auto wrong=a;wrong.serial++;
 assert(!RRConfirm(s,{wrong,true,50}));
 assert(RRConfirm(s,{a,true,50})&&s.phase==RR_WAIT);
 assert(!RRConfirm(s,{a,true,50}));
 auto f=fill(s,s.entry,100,301005);f.entry.remainder_reconciled=false;
 assert(!RRObserveFill(s,c,f));f.entry.remainder_reconciled=true;
 assert(RRObserveFill(s,c,f)&&s.phase==RR_PROTECT&&s.live_sl==300930);
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_INITIAL_SL&&a.sl_ticks==300935&&a.position_ticket==200);
 assert(!RRApplyDurableClose(s,close(s,RC_SL,RC_INITIAL_LEVEL,300930))); // pending effect unresolved
 assert(RRConfirm(s,{a,true,0})&&s.phase==RR_ARM_REVERSE&&s.live_sl==300935);
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_REVERSE&&a.side==-1&&a.entry_ticks==300935&&a.sl_ticks==301005);
 assert(RRConfirm(s,{a,true,51})&&s.phase==RR_ACTIVE);
 assert(!RRStage(s,c,301029,301060,a));
 assert(RRStage(s,c,301030,301061,a)&&a.kind==RR_SUPPORT_SL&&a.stage==1&&a.sl_ticks==300980);
 assert(s.live_sl==300935);
 assert(RRConfirm(s,{a,true,0})&&s.live_sl==300980&&s.opposite.ticket==51);
 assert(RRStage(s,c,301055,301100,a)&&a.kind==RR_SUPPORT_SL&&a.stage==2&&a.sl_ticks==301005);
 assert(RRConfirm(s,{a,true,0}));
 auto proof=close(s,RC_SL,RC_SUPPORT_LEVEL,301005);
 assert(RRApplyDurableClose(s,proof)&&s.cycle.full_sl_count==0&&s.phase==RR_ARM_REENTRY);
 assert(!RRApplyDurableClose(s,proof)); // duplicate immediate close
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_REENTRY&&a.entry_ticks==301075&&a.side==1);
 assert(RRConfirm(s,{a,true,52})&&s.phase==RR_WAIT&&s.opposite.ticket==51);
 f=fill(s,s.entry,101,301080,99);
 assert(!RRObserveFill(s,c,f));f.working_other_ticket=51;
 assert(RRObserveFill(s,c,f)&&s.phase==RR_PROTECT&&s.retire.ticket==51);
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_INITIAL_SL);
 assert(RRConfirm(s,{a,true,0})&&s.retire.ticket==51);
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_CANCEL&&a.order_ticket==51);
 assert(RRConfirm(s,{a,true,0})&&s.retire.ticket==0);
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_REVERSE&&a.entry_ticks==301010);
 assert(RRConfirm(s,{a,true,53}));
 proof=close(s,RC_SL,RC_INITIAL_LEVEL,301010);
 assert(RRApplyDurableClose(s,proof)&&s.cycle.full_sl_count==1&&s.phase==RR_WAIT);
 assert(s.opposite.ticket==53&&second.cycle.full_sl_count==0);
 assert(RRObserveFill(s,c,fill(s,s.opposite,102,301010)));
 assert(s.cycle.side==-1);
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_REVERSE&&a.side==1);
 assert(RRConfirm(s,{a,true,54}));
 proof=close(s,RC_MANUAL,0,0);
 assert(RRApplyDurableClose(s,proof)&&s.cycle.full_sl_count==1&&s.opposite.ticket==54);
 // Finish full-stop 24 with no projected slot 25.
 s.cycle.full_sl_count=23;
 assert(RRObserveFill(s,c,fill(s,s.opposite,103,s.opposite.entry_ticks)));
 assert(s.phase==RR_ACTIVE&&RCReverseSlot(s.cycle)==0&&s.opposite.ticket==0);
 assert(!RRStage(s,c,0,0,a));
 proof=close(s,RC_SL,RC_INITIAL_LEVEL,s.initial_sl);
 assert(RRApplyDurableClose(s,proof)&&s.cycle.full_sl_count==24&&s.phase==RR_DONE);
 assert(!RRStage(s,c,0,0,a));
}
