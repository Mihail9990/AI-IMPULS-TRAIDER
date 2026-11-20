#include <cassert>
#include <cstdint>
#include <limits>
#include <initializer_list>
using ulong=std::uint64_t;
constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();
#include "RecoverySupportRotationLedger.mqh"

static RRSettings config() {
 RRSettings cfg{};cfg.scope=1;cfg.generation=7;
 cfg.initial_sl_ticks=80;cfg.support_first_ticks=25;cfg.support_second_ticks=40;cfg.support_offset_ticks=25;
 for(int n=1;n<=24;n++)cfg.lot_steps[n-1]=RCDefaultLotHundredths(n);
 return cfg;
}
static RRRangeState wait(const RRSettings &cfg,int side) {
 RRRangeState s{};
 assert(RRNewRange(s,cfg,1,side,400000));
 s.phase=RR_WAIT;s.action_serial=31;
 s.cycle.full_sl_count=4;
 s.cycle.last_closed_order=200;s.cycle.last_closed_position=500;
 // At lot schedule boundary, roles have intentionally different lot sizes.
 s.reentry_side=side;s.reentry_slot=5;s.reentry_ticks=400000+side*80;
 s.entry={201,side,s.reentry_ticks,400000,cfg.lot_steps[4]};
 s.opposite={202,-side,400000-side*80,400000,cfg.lot_steps[5]};
 return s;
}
static RRFilled fill(const RRRangeState &s,const RRKnownOrder &order,long actual,ulong other) {
 return {{s.cycle.generation,s.cycle.range_id,order.ticket,order.ticket,9007199254740993ULL,
          order.side,order.volume_steps,order.volume_steps,order.volume_steps,actual,true,true,true},
          700,other,order.sl_ticks,0,true};
}

int main() {
 const auto cfg=config();
 RRRangeState shadow{};
 for(int original_side : {1,-1}) {
   for(bool reentry_wins : {false,true}) {
     auto s=wait(cfg,original_side);
     assert(RRHasSupportAlternatives(s));
     const RRKnownOrder winner=reentry_wins?s.entry:s.opposite;
     const RRKnownOrder other=reentry_wins?s.opposite:s.entry;
     const long price=winner.entry_ticks+winner.side*5;
     auto f=fill(s,winner,price,other.ticket);
     auto wrong=other;wrong.volume_steps--;
     RRSupportRotation p{};
     assert(!RRStageSupportRotation(s,cfg,f,wrong,p));
     wrong=other;wrong.ticket++;
     assert(!RRStageSupportRotation(s,cfg,f,wrong,p));
     wrong=other;wrong.entry_ticks++;
     assert(!RRStageSupportRotation(s,cfg,f,wrong,p));
     f.working_other_ticket++;
     assert(!RRStageSupportRotation(s,cfg,f,other,p));
     f.working_other_ticket=other.ticket;
     f.entry.remainder_reconciled=false;
     assert(!RRStageSupportRotation(s,cfg,f,other,p));
     f.entry.remainder_reconciled=true;
     s.pending.kind=RR_CANCEL;
     assert(!RRStageSupportRotation(s,cfg,f,other,p));
     s.pending.kind=RR_NONE;
     assert(RRStageSupportRotation(s,cfg,f,other,p));
     assert(RRSupportRotationBound(s,p)&&p.reserved&&p.sl_required&&p.move_required);
     assert(!RRHasSupportAlternatives(s));
     assert(!RRStageSupportRotation(s,cfg,f,other,p));
     assert(p.sl.sl_ticks==price-winner.side*80);
     assert(p.move.kind==RR_MOVE_SUPPORT_OPPOSITE&&p.move.order_ticket==other.ticket);
     assert(p.move.entry_ticks==p.sl.sl_ticks&&p.move.sl_ticks==price&&p.move.volume_steps==other.volume_steps);
     assert(p.move.position_id==0&&p.move.position_ticket==0&&p.move.serial==p.sl.serial+1);
     assert(p.position_id==f.entry.position_id&&RRKnownEqual(p.previous,other));
     assert(RRSupportRotationRequestState(s,p,p.move,shadow)&&RRKnownEqual(shadow.retire,other));
     assert(RRActionEqual(shadow.pending,p.move)&&RRActionEqual(s.pending,p.sl));
     assert(RRConfirmSupportRotation(s,p,{p.move,true,other.ticket})==RR_PAIR_CONFLICT); // not a create
     auto wrong_action=p.move;wrong_action.entry_ticks++;
     assert(RRConfirmSupportRotation(s,p,{wrong_action,true,0})==RR_PAIR_CONFLICT);
     assert(RRConfirmSupportRotation(s,p,{p.move,false,0})==RR_PAIR_WAITING);
     assert(!p.move_verified);
     assert(RRConfirmSupportRotation(s,p,{p.move,true,0})==RR_PAIR_WAITING);
     assert(!RRSupportRotationRequestState(s,p,p.move,shadow));
     assert(RRConfirmSupportRotation(s,p,{p.move,true,0})==RR_PAIR_WAITING);
     assert(s.opposite.ticket==0&&s.pending.kind==RR_INITIAL_SL);
     assert(RRConfirmSupportRotation(s,p,{p.sl,true,0})==RR_PAIR_CONFIRMED);
     assert(s.phase==RR_ACTIVE&&!p.reserved&&s.pending.kind==RR_NONE&&s.retire.ticket==0);
     assert(s.opposite.ticket==other.ticket&&s.opposite.volume_steps==other.volume_steps);
     assert(s.opposite.entry_ticks==price-winner.side*80&&s.opposite.sl_ticks==price);
     assert(s.cycle.full_sl_count==4);
   }
 }
 auto s=wait(cfg,1);
 RRSupportRotation p{};
 auto f=fill(s,s.entry,s.entry.entry_ticks,s.opposite.ticket);
 const auto other=s.opposite;
 assert(RRStageSupportRotation(s,cfg,f,other,p)&&!p.sl_required&&p.move_required);
 assert(RRConfirmSupportRotation(s,p,{p.move,true,0})==RR_PAIR_CONFIRMED);
 assert(s.opposite.ticket==other.ticket&&s.opposite.entry_ticks==400000);

 s=wait(cfg,1);
 // Already-correct geometry of pending and attached stops: no redundant write.
 s.opposite.entry_ticks=400000;s.opposite.sl_ticks=400080;
 f=fill(s,s.entry,s.entry.entry_ticks,s.opposite.ticket);p={};
 const auto stationary=s.opposite;
 assert(RRStageSupportRotation(s,cfg,f,stationary,p)&&!p.reserved&&!p.sl_required&&!p.move_required);
 assert(s.phase==RR_ACTIVE&&RRKnownEqual(s.opposite,stationary));

 s=wait(cfg,1);p={};
 s.opposite.entry_ticks=400005;s.opposite.sl_ticks=400085;
 f=fill(s,s.entry,400085,s.opposite.ticket);
 const auto aligned=s.opposite;
 assert(RRStageSupportRotation(s,cfg,f,aligned,p)&&p.reserved&&p.sl_required&&!p.move_required);
 assert(RRConfirmSupportRotation(s,p,{p.sl,true,0})==RR_PAIR_CONFIRMED);
 assert(RRKnownEqual(s.opposite,aligned));

 s=wait(cfg,-1);p={};s.cycle.full_sl_count=23;s.reentry_slot=24;
 s.entry.volume_steps=cfg.lot_steps[23];
 f=fill(s,s.entry,s.entry.entry_ticks,s.opposite.ticket);
 assert(!RRStageSupportRotation(s,cfg,f,s.opposite,p)&&s.phase==RR_WAIT&&!p.reserved);
 // Single break-even mode: one in-place MODIFY, no SL write even if gapped.
 auto single=cfg;single.support_first_ticks=0;single.support_offset_ticks=0;
 for(int side : {-1,1}) for(bool reentry_wins : {false,true}) {
    auto live=wait(single,side);
    const auto winner=reentry_wins?live.entry:live.opposite;
    const auto survivor=reentry_wins?live.opposite:live.entry;
    const auto evidence=fill(live,winner,winner.entry_ticks+winner.side*7,survivor.ticket);
    const long opened=evidence.entry.actual_entry_ticks;
    RRSupportRotation rotation{};
    assert(RRStageSupportRotation(live,single,evidence,survivor,rotation));
    assert(rotation.actual_entry_only&&rotation.reserved&&rotation.move_required&&!rotation.sl_required);
    assert(rotation.sl.kind==RR_NONE&&live.pending.kind==RR_MOVE_SUPPORT_OPPOSITE);
    assert(live.initial_sl==winner.sl_ticks&&live.live_sl==winner.sl_ticks);
    assert(RROppositeAnchor(live)==opened-winner.side*80);
    assert(rotation.move.entry_ticks==opened-winner.side*80&&rotation.move.sl_ticks==opened);
    assert(rotation.move.order_ticket==survivor.ticket&&rotation.move.volume_steps==survivor.volume_steps);
    assert(RRSupportRotationRequestState(live,rotation,rotation.move,shadow));
    assert(!RRSupportRotationRequestState(live,rotation,rotation.sl,shadow));
    auto fake=rotation.move;fake.volume_steps++;
    assert(RRConfirmSupportRotation(live,rotation,{fake,true,0})==RR_PAIR_CONFLICT);
    assert(RRConfirmSupportRotation(live,rotation,{rotation.move,true,survivor.ticket})==RR_PAIR_CONFLICT);
    assert(RRConfirmSupportRotation(live,rotation,{rotation.move,true,0})==RR_PAIR_CONFIRMED);
    assert(!rotation.reserved&&live.phase==RR_ACTIVE&&live.opposite.ticket==survivor.ticket);
    assert(live.opposite.sl_ticks==opened&&live.live_sl==winner.sl_ticks&&live.cycle.full_sl_count==4);
    assert(RRConfirmSupportRotation(live,rotation,{rotation.move,true,0})==RR_PAIR_CONFLICT);
    const auto &r=live.cycle;
    RCCloseCheckpoint stop{};
    stop.key={live.scope,77,r.generation,r.range_id,r.position_id,r.active_order};
    stop.before=r;stop.reentry_distance=80;
    stop.proof={r.generation,r.range_id,r.active_order,r.position_id,r.filled_steps,r.filled_steps,
                0,true,true,RC_SL,winner.sl_ticks,RC_INITIAL_LEVEL,true};
    assert(RRApplySessionClose(live,stop)&&live.phase==RR_WAIT&&live.cycle.full_sl_count==5);
    assert(live.opposite.ticket==survivor.ticket&&live.entry.ticket==0);
 }

 s=wait(single,1);p={};
 s.opposite.entry_ticks=400005;s.opposite.sl_ticks=400085;
 f=fill(s,s.entry,400085,s.opposite.ticket);
 const auto aligned_single=s.opposite;
 assert(RRStageSupportRotation(s,single,f,aligned_single,p)&&p.actual_entry_only);
 assert(!p.reserved&&!p.move_required&&!p.sl_required&&s.pending.kind==RR_NONE);
 assert(s.phase==RR_ACTIVE&&s.live_sl==f.sl_ticks&&s.initial_sl==f.sl_ticks);
 assert(RRKnownEqual(s.opposite,aligned_single)&&RROppositeAnchor(s)==400005);

 // The verified support exit is replaced, not charged; older unconfirmed
 // predecessors remain separately accountable and duplicate fills do not replay.
 for(int side : {-1,1}) for(bool reentry_wins : {false,true}) {
   auto range=wait(single,side);
   RRAlternationLedger ledger{};
   ledger.scope=range.scope;ledger.generation=range.cycle.generation;ledger.range_id=range.cycle.range_id;
   ledger.position_count=2;ledger.strategy_slot=5;ledger.confirmed_full_sl_count=1;
   ledger.positions[0].bound_entry.position_id=401;ledger.positions[0].bound_entry.active_order=101;
   ledger.positions[0].detached=true;
   RRAlternatingPosition &old=ledger.positions[1];
   old.bound_entry=range.cycle;old.bound_entry.position_id=500;old.bound_entry.active_order=200;
   old.bound_entry.side=side;old.bound_entry.actual_entry_ticks=400000;old.exit_class=RC_SUPPORT_SL;
   old.reported_stop=400000;
   auto won=reentry_wins?range.entry:range.opposite;
   auto remains=reentry_wins?range.opposite:range.entry;
   auto e=fill(range,won,won.entry_ticks+won.side*6,remains.ticket);
   RRSupportRotation r{};
   assert(!RRStageSupportRotationLedger(range,single,ledger,e,remains,r)); // no confirmed exit
   old.exit_recorded=true;
   e.entry.position_id=401;
   assert(!RRStageSupportRotationLedger(range,single,ledger,e,remains,r)); // replay/owned predecessor
   e.entry.position_id=501;
   assert(RRStageSupportRotationLedger(range,single,ledger,e,remains,r));
   assert(r.move.order_ticket==remains.ticket&&r.move.volume_steps==remains.volume_steps);
   assert(r.move.entry_ticks==e.entry.actual_entry_ticks-won.side*80);
   assert(range.live_sl==won.sl_ticks&&RRAltBound(range,ledger));
   assert(ledger.positions[0].bound_entry.position_id==401&&ledger.positions[0].detached);
   assert(ledger.positions[1].bound_entry.position_id==501&&!ledger.positions[1].exit_recorded);
   assert(ledger.position_count==2&&ledger.strategy_slot==5&&ledger.confirmed_full_sl_count==1);
   assert(!RRStageSupportRotationLedger(range,single,ledger,e,remains,r));
 }

}
