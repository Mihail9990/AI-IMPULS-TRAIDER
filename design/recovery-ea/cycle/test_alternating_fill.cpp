#include <cassert>
#include <cstdint>
#include <limits>
#include <initializer_list>
using ulong=std::uint64_t;
constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();
#include "RecoveryAlternatingFill.mqh"

static RRSettings config() {
 RRSettings c{};c.scope=1;c.generation=7;
 c.initial_sl_ticks=80;c.support_first_ticks=25;c.support_second_ticks=40;c.support_offset_ticks=25;
 for(int n=1;n<=24;n++)c.lot_steps[n-1]=RCDefaultLotHundredths(n);
 return c;
}
static RRRangeState first(const RRSettings &cfg,int side,ulong initial_id) {
 RRRangeState s{};RRAction a{};
 assert(RRNewRange(s,cfg,1,side,400000));
 assert(RRStage(s,cfg,0,0,a)&&RRConfirm(s,{a,true,50}));
 RRFilled f{{cfg.generation,1,50,50,initial_id,side,1,1,1,
          400000+side*5,true,true,true},initial_id+100,0,s.entry.sl_ticks,0,true};
 assert(RRObserveFill(s,cfg,f));
 return s;
}
static RRAlternatingObservation evidence(const RRRangeState &s,const RROpeningPair &p,
                                        ulong ticket,ulong id,long actual) {
 RRAlternatingObservation e{};
 e.scope=s.scope;
 if(p.reserved) {
    e.paired_intent=p.reverse;e.paired_order_ticket=ticket;
    e.executed_order={ticket,p.reverse.side,p.reverse.entry_ticks,p.reverse.sl_ticks,p.reverse.volume_steps};
 } else e.executed_order=s.opposite;
 auto volume=e.executed_order.volume_steps;
 e.fill.entry={s.cycle.generation,s.cycle.range_id,ticket,ticket,id,e.executed_order.side,
           volume,volume,volume,actual,true,true,true};
 e.fill.position_ticket=id+100;e.fill.sl_ticks=e.executed_order.sl_ticks;
 e.fill.independently_verified=true;
 return e;
}
static RCCloseCheckpoint closure(const RRAlternationLedger &a,int index,int reason=RC_SL) {
 const auto &pos=a.positions[index];
 const auto &r=pos.bound_entry;
 RCCloseCheckpoint c{};
 c.key={a.scope,99+(ulong)index,r.generation,r.range_id,r.position_id,r.active_order};
 c.before=r;c.reentry_distance=80;
 c.proof={r.generation,r.range_id,r.active_order,r.position_id,r.filled_steps,r.filled_steps,
          0,true,true,reason,reason==RC_SL?pos.planned_sl:0,reason==RC_SL?RC_INITIAL_LEVEL:0,true};
 return c;
}
int main() {
 const auto cfg=config();
 for(int start_side : {-1,1}) {
  auto s=first(cfg,start_side,9007199254741000ULL);
  RRAlternationLedger ledger{};
  assert(RRStartAlternation(s,0,ledger));
  RROpeningPair opening{};
  assert(RRStageOpeningPair(s,cfg,opening)&&opening.sl_required);
  auto f=evidence(s,opening,51,9007199254741001ULL,opening.reverse.entry_ticks+19);
  f.predecessor_inventory_complete=true;f.predecessor_present=true;
  f.predecessor_position_id=s.cycle.position_id;f.predecessor_remaining_steps=1;
  auto bad=f;bad.paired_order_ticket=0;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,bad)==RR_ALT_UNRESOLVED);
  bad=f;bad.scope++;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,bad)==RR_ALT_UNRESOLVED);
  bad=f;bad.executed_order.entry_ticks++;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,bad)==RR_ALT_UNRESOLVED);
  bad=f;bad.paired_intent.serial++;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,bad)==RR_ALT_UNRESOLVED);
  bad=f;bad.fill.entry.filled_steps--;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,bad)==RR_ALT_UNRESOLVED);
  bad=f;bad.predecessor_position_id++;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,bad)==RR_ALT_UNRESOLVED);
  bad=f;bad.fill.sl_ticks=-1;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,bad)==RR_ALT_UNRESOLVED);
  f.fill.sl_ticks=0; // A missing/non-confirmed broker SL does not move the next trigger.
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,f)==RR_ALT_NEXT_OPPOSITE);
  assert(ledger.strategy_slot==2&&ledger.confirmed_full_sl_count==0);
  assert(ledger.positions[0].detached&&!ledger.positions[0].exit_recorded);
  assert(ledger.positions[0].pending_at_handoff.kind==RR_INITIAL_SL&&ledger.positions[0].opening_at_handoff.reserved);
  assert(ledger.positions[0].observed_open_at_handoff&&ledger.positions[0].observed_remaining_at_handoff==1);
  assert(!opening.sl_required&&opening.reverse.side==start_side);
  assert(s.initial_sl==f.executed_order.sl_ticks && s.live_sl==0);
  assert(opening.reverse.entry_ticks==f.fill.entry.actual_entry_ticks+start_side*80);
  assert(opening.reverse.sl_ticks==f.fill.entry.actual_entry_ticks);
  assert(s.opposite_anchor_ticks==opening.reverse.entry_ticks && RROppositeAnchor(s)==opening.reverse.entry_ticks);
  assert(opening.reverse.entry_ticks!=f.executed_order.sl_ticks);
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,f)==RR_ALT_UNRESOLVED);
  auto closed=closure(ledger,0);
  auto wrong=closed;wrong.proof.exit_stop_ticks++;
  assert(RRRecordAlternatingClose(ledger,wrong)==RR_ALT_CLOSE_UNRESOLVED);
  wrong=closed;wrong.proof.remaining_steps=1;
  assert(RRRecordAlternatingClose(ledger,wrong)==RR_ALT_CLOSE_UNRESOLVED);
  assert(RRRecordAlternatingClose(ledger,closed)==RR_ALT_CLOSE_APPLIED);
  assert(RRRecordAlternatingClose(ledger,closed)==RR_ALT_CLOSE_DUPLICATE);
  assert(RRRecordAlternatingClose(ledger,closure(ledger,0,RC_MANUAL))==RR_ALT_CLOSE_UNRESOLVED);
  assert(ledger.strategy_slot==2&&ledger.confirmed_full_sl_count==1);
  // Distinct current book route with exact positive effect and fixed anchor.
  assert(RRConfirmOpeningPair(s,opening,{opening.reverse,true,52})==RR_PAIR_CONFIRMED);
  f=evidence(s,opening,52,9007199254741002ULL,s.opposite.entry_ticks+5);
  {
    // A independently verified closure can arrive before the new filled
    // reverse. Updating its accounting must not tear down range ownership.
    auto already_closed=s;
    auto next_opening=opening;
    auto on_close_first=ledger;
    assert(RRRecordAlternatingClose(on_close_first,closure(on_close_first,1))==RR_ALT_CLOSE_APPLIED);
    assert(on_close_first.confirmed_full_sl_count==2);
    assert(already_closed.cycle.position_id==s.cycle.position_id);
    auto prove_fill=f;
    prove_fill.predecessor_inventory_complete=true;
    prove_fill.predecessor_present=false;
    assert(RRAdvanceAlternatingFill(already_closed,cfg,next_opening,on_close_first,prove_fill)==RR_ALT_NEXT_OPPOSITE);
    assert(on_close_first.confirmed_full_sl_count==2);
    assert(next_opening.reverse.entry_ticks==prove_fill.fill.entry.actual_entry_ticks-prove_fill.fill.entry.side*80);
  }
  s.support_stage=1;
  s.live_sl=s.cycle.actual_entry_ticks;
  s.pending.kind=RR_SUPPORT_SL; // In-flight unrelated modification must not gate ordinary fill.
  s.reentry_slot=2;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,f)==RR_ALT_UNRESOLVED);
  s.reentry_slot=0;
  assert(RRAdvanceAlternatingFill(s,cfg,opening,ledger,f)==RR_ALT_NEXT_OPPOSITE);
  assert(ledger.strategy_slot==3&&ledger.confirmed_full_sl_count==1);
  assert(ledger.positions[1].pending_at_handoff.kind==RR_SUPPORT_SL);
  assert(ledger.positions[1].last_confirmed_sl!=ledger.positions[1].planned_sl);
  for(int n=4;n<=24;n++) {
    assert(opening.reverse.volume_steps==cfg.lot_steps[n-1]);
    f=evidence(s,opening,(ulong)(50+n),(ulong)(1000+n),opening.reverse.entry_ticks+3);
    auto result=RRAdvanceAlternatingFill(s,cfg,opening,ledger,f);
    assert(result==(n==24?RR_ALT_FINAL_NO_TRIGGER:RR_ALT_NEXT_OPPOSITE));
    assert(ledger.strategy_slot==n&&ledger.confirmed_full_sl_count==1);
    if(n<24) {
       assert(opening.reverse.entry_ticks==f.fill.entry.actual_entry_ticks-f.fill.entry.side*80);
       assert(opening.reverse.sl_ticks==f.fill.entry.actual_entry_ticks);
    }
  }
  assert(!opening.reserved&&s.phase==RR_ACTIVE&&RCReverseSlot(s.cycle)==0);
  assert(!RRStageOpeningPair(s,cfg,opening));
  // Closures can arrive out of order, without changing the terminal strategy slot.
  assert(RRRecordAlternatingClose(ledger,closure(ledger,23,RC_MANUAL))==RR_ALT_CLOSE_APPLIED);
  assert(RRRecordAlternatingClose(ledger,closure(ledger,3))==RR_ALT_CLOSE_APPLIED);
  assert(RRRecordAlternatingClose(ledger,closure(ledger,2))==RR_ALT_CLOSE_APPLIED);
  assert(ledger.strategy_slot==24&&ledger.confirmed_full_sl_count==3);
 }
}
