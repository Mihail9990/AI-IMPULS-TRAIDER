#include <cassert>
#include <cstdint>
#include <limits>
using ulong=std::uint64_t;
constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();
#include "RecoveryCompletedReverse.mqh"

static RRSettings config() {
 RRSettings c{};c.scope=7;c.generation=11;
 c.initial_sl_ticks=80;c.support_first_ticks=25;c.support_second_ticks=40;c.support_offset_ticks=25;
 for(int n=1;n<=24;n++)c.lot_steps[n-1]=RCDefaultLotHundredths(n);
 return c;
}
static RRRangeState fill(const RRSettings &c,int side,long fill_price,int count=0) {
 RRRangeState s{};RRAction a{};
 assert(RRNewRange(s,c,1,side,400000));
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_PRIMARY);
 assert(RRConfirm(s,{a,true,50}));
 s.cycle.full_sl_count=count;
 RRFilled f{{c.generation,1,50,50,9007199254740993ULL,side,1,1,1,fill_price,true,true,true},
            100,0,s.entry.sl_ticks,0,true};
 assert(RRObserveFill(s,c,f));
 return s;
}
static RRCompletedReverse completed(const RRRangeState &s,const RROpeningPair &p,
                                      long actual,ulong pos=9007199254740994ULL) {
 RRCompletedReverse e{};
 e.routed_action=p.reverse;e.routed_order=51;
 auto &r=s.cycle;
 auto &cl=e.predecessor_close;
 cl.key={s.scope,731,r.generation,r.range_id,r.position_id,r.active_order};
 cl.before=r;
 cl.proof={r.generation,r.range_id,r.active_order,r.position_id,r.filled_steps,r.filled_steps,
           0,true,true,RC_SL,p.reverse.entry_ticks,RC_INITIAL_LEVEL,true};
 auto &f=e.successor;
 auto volume=p.reverse.volume_steps;
 f.entry={r.generation,r.range_id,51,51,pos,p.reverse.side,volume,volume,volume,actual,true,true,true};
 f.position_ticket=101;f.sl_ticks=p.reverse.sl_ticks;f.tp_ticks=0;f.independently_verified=true;
 return e;
}

int main() {
 const RRSettings c=config();
 auto buy=fill(c,1,400005);
 RROpeningPair pair{};RRAction protect{};
 assert(RRStageOpeningPair(buy,c,pair));
 assert(pair.sl.sl_ticks==399925&&pair.reverse.entry_ticks==399925&&pair.reverse.sl_ticks==400005);
 auto sold=completed(buy,pair,399921); // SELL slippage -0.04 from original trigger
 auto changed=sold;changed.routed_order++;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 changed=sold;changed.successor.entry.filled_steps--;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 changed=sold;changed.successor.entry.remainder_reconciled=false;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 changed=sold;changed.successor.working_other_ticket=93;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 changed=sold;changed.predecessor_close.proof.remaining_steps=1;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 changed=sold;changed.predecessor_close.proof.exit_stop_ticks--;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 changed=sold;changed.predecessor_close.key.scope++;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 changed=sold;changed.predecessor_close.proof.reason=RC_MANUAL;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 changed=sold;changed.routed_action.serial++;
 assert(RRAdvanceCompletedReverse(buy,c,pair,changed,protect)==RR_COMPLETED_UNRESOLVED);
 assert(buy.cycle.full_sl_count==0&&pair.reserved&&!pair.sl_verified&&!pair.reverse_verified);
 assert(RRAdvanceCompletedReverse(buy,c,pair,sold,protect)==RR_COMPLETED_NEXT_PAIR);
 assert(buy.cycle.full_sl_count==1&&buy.cycle.active_order==51&&buy.cycle.side==-1);
 assert(buy.cycle.actual_entry_ticks==399921&&pair.sl.sl_ticks==400001);
 assert(pair.reverse.side==1&&pair.reverse.entry_ticks==400001&&pair.reverse.sl_ticks==399921);
 assert(RRAdvanceCompletedReverse(buy,c,pair,sold,protect)==RR_COMPLETED_UNRESOLVED);
 assert(buy.cycle.full_sl_count==1);

 auto sell=fill(c,-1,399995);
 pair={};assert(RRStageOpeningPair(sell,c,pair));
 assert(pair.reverse.entry_ticks==400075&&pair.reverse.sl_ticks==399995);
 auto bought=completed(sell,pair,400088);
 assert(RRConfirmOpeningPair(sell,pair,{pair.reverse,true,51})==RR_PAIR_WAITING);
 assert(RRAdvanceCompletedReverse(sell,c,pair,bought,protect)==RR_COMPLETED_NEXT_PAIR);
 assert(sell.cycle.side==1&&sell.cycle.actual_entry_ticks==400088);
 assert(pair.reverse.side==-1&&pair.reverse.entry_ticks==400008&&pair.reverse.sl_ticks==400088);

 sell=fill(c,-1,399995,22);
 pair={};assert(RRStageOpeningPair(sell,c,pair));
 bought=completed(sell,pair,400088);
 assert(RRAdvanceCompletedReverse(sell,c,pair,bought,protect)==RR_COMPLETED_PROTECTION_ONLY);
 assert(sell.cycle.full_sl_count==23&&sell.phase==RR_PROTECT&&protect.kind==RR_INITIAL_SL&&!pair.reserved);
 assert(protect.sl_ticks==400008);
 assert(RRConfirm(sell,{protect,true,0})&&sell.phase==RR_ACTIVE);

 sell=fill(c,-1,400000,22);
 pair={};assert(RRStageOpeningPair(sell,c,pair)&&!pair.sl_required);
 bought=completed(sell,pair,400080);
 assert(RRAdvanceCompletedReverse(sell,c,pair,bought,protect)==RR_COMPLETED_FINAL_PROTECTED);
 assert(sell.cycle.full_sl_count==23&&sell.phase==RR_ACTIVE&&sell.pending.kind==RR_NONE&&!pair.reserved);
}
