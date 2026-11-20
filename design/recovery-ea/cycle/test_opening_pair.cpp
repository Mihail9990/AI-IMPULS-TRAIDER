#include <cassert>
#include <cstdint>
#include <limits>
using ulong=std::uint64_t;
constexpr auto ULONG_MAX=std::numeric_limits<ulong>::max();
#include "RecoveryOpeningPair.mqh"

static RRSettings config() {
 RRSettings c{};c.scope=7;c.generation=11;
 c.initial_sl_ticks=70;c.quarter_ticks=25;c.half_ticks=50;
 for(int n=1;n<=24;n++)c.lot_steps[n-1]=RCDefaultLotHundredths(n);
 return c;
}
static RRRangeState fill(const RRSettings &c,int side,long fill_price,int count=0) {
 RRRangeState s{};RRAction a{};
 assert(RRNewRange(s,c,1,side,301000));
 assert(RRStage(s,c,0,0,a)&&a.kind==RR_PRIMARY);
 assert(RRConfirm(s,{a,true,50}));
 s.cycle.full_sl_count=count;
 RRFilled f{{c.generation,1,50,50,9007199254740993ULL,side,1,1,1,fill_price,true,true,true},
            100,0,s.entry.sl_ticks,0,true};
 assert(RRObserveFill(s,c,f));
 return s;
}

int main() {
 const RRSettings c=config();
 auto s=fill(c,1,301005);
 RROpeningPair pair{};RRRangeState shadow{};RRAction next{};
 assert(s.phase==RR_PROTECT&&s.live_sl==300930&&s.initial_sl==300935);
 assert(RRStageOpeningPair(s,c,pair));
 assert(pair.sl_required&&!pair.sl_verified&&!pair.reverse_verified);
 assert(pair.sl.kind==RR_INITIAL_SL&&pair.reverse.kind==RR_REVERSE);
 assert(pair.sl.sl_ticks==pair.reverse.entry_ticks&&pair.reverse.sl_ticks==301005);
 assert(pair.sl.serial+1==pair.reverse.serial&&s.action_serial==pair.reverse.serial);
 assert(!RRStageOpeningPair(s,c,pair));
 assert(!RRStage(s,c,400000,400100,next)); // strategy blocked pending both facts
 assert(RROpeningRequestState(s,pair,pair.sl,shadow)&&RRActionEqual(shadow.pending,pair.sl));
 assert(RROpeningRequestState(s,pair,pair.reverse,shadow)&&RRActionEqual(shadow.pending,pair.reverse));
 assert(RRActionEqual(s.pending,pair.sl)); // constructing second request is not transition
 auto altered=pair.reverse;altered.entry_ticks++;
 assert(!RROpeningRequestState(s,pair,altered,shadow));
 assert(RRConfirmOpeningPair(s,pair,{altered,true,51})==RR_PAIR_CONFLICT);
 assert(RRConfirmOpeningPair(s,pair,{pair.sl,true,51})==RR_PAIR_CONFLICT);
 assert(RRConfirmOpeningPair(s,pair,{pair.reverse,true,50})==RR_PAIR_CONFLICT);
 assert(RRConfirmOpeningPair(s,pair,{pair.sl,false,0})==RR_PAIR_WAITING);
 assert(!pair.sl_verified);
 assert(RRConfirmOpeningPair(s,pair,{pair.reverse,true,51})==RR_PAIR_WAITING);
 assert(RRConfirmOpeningPair(s,pair,{pair.reverse,true,51})==RR_PAIR_WAITING); // same fact is harmless
 assert(RRConfirmOpeningPair(s,pair,{pair.reverse,true,52})==RR_PAIR_CONFLICT);
 assert(s.opposite.ticket==0&&pair.reverse_ticket==51&&s.live_sl==300930);
 assert(!RROpeningRequestState(s,pair,pair.reverse,shadow));
 auto restarted=s;auto restored=pair;
 assert(RRConfirmOpeningPair(restarted,restored,{pair.sl,true,0})==RR_PAIR_CONFIRMED);
 assert(restarted.phase==RR_ACTIVE&&restarted.opposite.ticket==51&&restarted.live_sl==300935);
 assert(restarted.pending.kind==RR_NONE&&!restored.reserved);
 assert(!RROpeningRequestState(restarted,restored,restored.sl,shadow));

 s=fill(c,-1,300995); pair={};
 assert(RRStageOpeningPair(s,c,pair));
 assert(pair.sl.sl_ticks==301065&&pair.reverse.entry_ticks==301065);
 assert(pair.reverse.side==1&&pair.reverse.sl_ticks==300995&&pair.reverse.volume_steps==1);
 assert(RRConfirmOpeningPair(s,pair,{pair.sl,true,0})==RR_PAIR_WAITING);
 assert(!RROpeningRequestState(s,pair,pair.sl,shadow));
 assert(RROpeningRequestState(s,pair,pair.reverse,shadow));
 assert(RRConfirmOpeningPair(s,pair,{pair.reverse,true,52})==RR_PAIR_CONFIRMED);
 assert(s.phase==RR_ACTIVE&&s.opposite.ticket==52);
 assert(RRStage(s,c,300900,300940,next)&&next.kind==RR_SUPPORT_SL);
 assert(RRConfirm(s,{next,true,0})&&s.opposite.entry_ticks==301065);

 s=fill(c,1,301000);pair={};
 assert(s.phase==RR_ARM_REVERSE&&s.live_sl==s.initial_sl);
 assert(RRStageOpeningPair(s,c,pair));
 assert(!pair.sl_required&&pair.sl.kind==RR_NONE&&pair.sl_verified&&pair.reverse.serial==2);
 assert(!RROpeningRequestState(s,pair,pair.sl,shadow));
 assert(RRConfirmOpeningPair(s,pair,{pair.reverse,true,53})==RR_PAIR_CONFIRMED);
 assert(s.phase==RR_ACTIVE&&s.opposite.ticket==53);

 s=fill(c,1,301005,23);pair={};
 assert(!RRStageOpeningPair(s,c,pair));
 assert(RRStage(s,c,0,0,next)&&next.kind==RR_INITIAL_SL); // protection only, no slot 25
 assert(RRConfirm(s,{next,true,0})&&s.phase==RR_ACTIVE);

 s=fill(c,-1,300995);pair={};
 s.retire={222,1,301050,300980,1};
 assert(!RRStageOpeningPair(s,c,pair)); // existing ticket must be resolved first
 assert(RRStage(s,c,0,0,next)&&next.kind==RR_INITIAL_SL);
 assert(RRConfirm(s,{next,true,0}));
 assert(RRStage(s,c,0,0,next)&&next.kind==RR_CANCEL&&next.order_ticket==222);
 assert(RRConfirm(s,{next,true,0}));
 assert(RRStageOpeningPair(s,c,pair)&&!pair.sl_required);
}
