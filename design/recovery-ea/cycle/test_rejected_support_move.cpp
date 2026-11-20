#include <cassert>
#include <cstdint>
#include <limits>
#include <initializer_list>
using ulong=std::uint64_t;
constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();
#include "RecoveryRejectedSupportMove.mqh"

static RRSettings config() {
 RRSettings c{};c.scope=1;c.generation=7;
 c.initial_sl_ticks=80;c.support_first_ticks=25;c.support_second_ticks=40;c.support_offset_ticks=25;
 for(int n=1;n<=24;n++)c.lot_steps[n-1]=RCDefaultLotHundredths(n);
 return c;
}
static RRFilled fill(const RRRangeState &s,const RRKnownOrder &o,ulong id,long actual,ulong other) {
 return {{s.cycle.generation,s.cycle.range_id,o.ticket,o.ticket,id,o.side,
          o.volume_steps,o.volume_steps,o.volume_steps,actual,true,true,true},
          id+100,other,o.sl_ticks,0,true};
}
static RRRangeState staged(const RRSettings &cfg,int side,RRSupportRotation &p,int count=4) {
 RRRangeState s{};
 assert(RRNewRange(s,cfg,1,side,400000));
 s.phase=RR_WAIT;s.action_serial=31;s.cycle.full_sl_count=count;
 s.cycle.last_closed_order=200;s.cycle.last_closed_position=500;
 s.reentry_side=side;s.reentry_slot=count+1;s.reentry_ticks=400000+side*80;
 s.entry={201,side,s.reentry_ticks,400000,cfg.lot_steps[count]};
 s.opposite={202,-side,400000-side*80,400000,cfg.lot_steps[count+1]};
 auto original=s.opposite;
 assert(RRStageSupportRotation(s,cfg,fill(s,s.entry,600,s.entry.entry_ticks+side*5,original.ticket),original,p));
 assert(p.sl_required==(cfg.support_first_ticks>0)&&p.move_required&&p.reserved);
 return s;
}
static RCCloseCheckpoint closed(const RRRangeState &s,int reason=RC_SL) {
 const auto &r=s.cycle;
 RCCloseCheckpoint c{};
 c.key={s.scope,99,r.generation,r.range_id,r.position_id,r.active_order};
 c.before=r;c.reentry_distance=80;
 c.proof={r.generation,r.range_id,r.active_order,r.position_id,r.filled_steps,r.filled_steps,
          0,true,true,reason,reason==RC_SL?s.initial_sl:0,reason==RC_SL?RC_INITIAL_LEVEL:0,true};
 return c;
}
int main() {
 const auto cfg=config();
 for(int side : {1,-1}) {
    RRSupportRotation p{};
    auto s=staged(cfg,side,p);
    RROpeningPair next{};
    const RRKnownOrder original=p.previous;
    const long actual=original.entry_ticks+original.side*15;
    assert(actual-original.side*80!=original.sl_ticks);
    RRRejectedSupportMove e{p.move,true,original,closed(s),fill(s,original,601,actual,0)};
    auto bad=e;bad.definite_broker_rejection=false;
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,bad,next)==RR_REJECTED_MOVE_UNRESOLVED);
    bad=e;bad.rejected_action.serial++;
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,bad,next)==RR_REJECTED_MOVE_UNRESOLVED);
    bad=e;bad.executed_original.entry_ticks++;
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,bad,next)==RR_REJECTED_MOVE_UNRESOLVED);
    bad=e;bad.predecessor_close.proof.remaining_steps=1;
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,bad,next)==RR_REJECTED_MOVE_UNRESOLVED);
    bad=e;bad.predecessor_close.key.scope++;
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,bad,next)==RR_REJECTED_MOVE_UNRESOLVED);
    bad=e;bad.successor.sl_ticks=actual-original.side*80;
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,bad,next)==RR_REJECTED_MOVE_UNRESOLVED);
    bad=e;bad.successor.entry.remainder_reconciled=false;
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,bad,next)==RR_REJECTED_MOVE_UNRESOLVED);
    bad=e;bad.successor.entry.position_id=s.cycle.position_id;
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,bad,next)==RR_REJECTED_MOVE_UNRESOLVED);
    assert(p.reserved&&s.cycle.active_order==201&&s.cycle.full_sl_count==4);
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,e,next)==RR_REJECTED_MOVE_NEW_OPPOSITE);
    assert(!p.reserved&&next.reserved&&!next.sl_required&&next.sl.kind==RR_NONE);
    assert(s.cycle.full_sl_count==5&&s.cycle.active_order==202&&s.cycle.actual_entry_ticks==actual);
    assert(s.initial_sl==original.sl_ticks&&s.live_sl==original.sl_ticks);
    assert(next.reverse.side==side&&next.reverse.entry_ticks==original.sl_ticks);
    assert(next.reverse.sl_ticks==original.sl_ticks-side*80&&next.reverse.order_ticket==0);
    assert(RRAdvanceRejectedSupportMove(s,cfg,p,e,next)==RR_REJECTED_MOVE_UNRESOLVED);
 }
 RRSupportRotation p{};
 auto s=staged(cfg,1,p,22);
 RROpeningPair next{};
 const auto orig=p.previous;
 RRRejectedSupportMove e{p.move,true,orig,closed(s),fill(s,orig,601,orig.entry_ticks-15,0)};
 assert(RRAdvanceRejectedSupportMove(s,cfg,p,e,next)==RR_REJECTED_MOVE_FINAL_PROTECTED);
 assert(s.cycle.full_sl_count==23&&s.initial_sl==orig.sl_ticks&&s.phase==RR_ACTIVE&&!next.reserved);

 p={};s=staged(cfg,1,p);
 e={p.move,true,p.previous,closed(s,RC_MANUAL),fill(s,p.previous,601,p.previous.entry_ticks-15,0)};
 assert(RRAdvanceRejectedSupportMove(s,cfg,p,e,next)==RR_REJECTED_MOVE_NEW_OPPOSITE);
 assert(s.cycle.full_sl_count==4&&!next.sl_required);
 auto single=cfg;single.support_first_ticks=0;single.support_offset_ticks=0;
 p={};s=staged(single,-1,p);next={};
 assert(p.actual_entry_only&&!p.sl_required);
 const auto unedited=p.previous;
 const long actual=unedited.entry_ticks+unedited.side*13;
 e={p.move,true,unedited,closed(s),fill(s,unedited,602,actual,0)};
 assert(RRAdvanceRejectedSupportMove(s,single,p,e,next)==RR_REJECTED_MOVE_NEW_OPPOSITE);
 assert(!p.reserved&&!next.sl_required&&s.cycle.full_sl_count==5);
 assert(s.live_sl==unedited.sl_ticks&&s.initial_sl==unedited.sl_ticks);
 assert(next.reverse.entry_ticks==actual-unedited.side*80&&next.reverse.sl_ticks==actual);

}
