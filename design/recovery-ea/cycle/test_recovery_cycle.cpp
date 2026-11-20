// Host checks of deterministic policy; not an MQL5/terminal compilation.
#include <cassert>
#include <cstdint>
using ulong = std::uint64_t;
#include "RecoveryCycle.mqh"

static RCRange range(int id=1,int count=0) {
 RCRange r{};
 r.generation=5;r.range_id=id;r.full_sl_count=count;
 return r;
}
static RCEntryProof entry(int id=1,ulong order=30,ulong position=50,int side=1) {
 return {5,id,order,order,position,side,10,7,7,400030,true,true,true};
}
static RCCloseProof close(const RCRange &r,int reason=RC_SL,long level=399960,int mask=RC_INITIAL_LEVEL) {
 return {r.generation,r.range_id,r.active_order,r.position_id,
         r.filled_steps,r.filled_steps,0,true,true,reason,level,mask,true};
}

int main() {
 const int defaults[24]={1,1,1,1,1,2,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10};
 assert(RCDefaultLotHundredths(0)==0 && RCDefaultLotHundredths(25)==0);
 for(int n=1;n<=24;n++) assert(RCDefaultLotHundredths(n)==defaults[n-1]);
 auto a=range(1,4), b=range(2,0);
 auto e=entry();
 assert(RCCurrentSlot(a)==5 && RCReverseSlot(a)==6 && RCReverseSlot(b)==2);
 e.remainder_reconciled=false;assert(!RCApplyEntry(a,e));
 e.remainder_reconciled=true;e.executed_order++;assert(!RCApplyEntry(a,e));
 e=entry();e.active_steps--;assert(!RCApplyEntry(a,e));
 e=entry();e.generation++;assert(!RCApplyEntry(a,e));
 e=entry();e.independently_verified=false;assert(!RCApplyEntry(a,e));
 assert(RCApplyEntry(a,entry()));
 assert(a.actual_entry_ticks==400030 && a.filled_steps==7 && a.full_sl_count==4);
 assert(!RCApplyEntry(a,entry(1,31,51)));
 assert(RCApplyEntry(b,entry(2,40,60,-1)));
 RCStopProposal stop{};
 assert(!RCProposeSupport(a,399960,0,400054,400075,25,40,25,stop)); // wrong side only
 assert(RCProposeSupport(a,399960,0,400055,400076,25,40,25,stop));
 assert(stop.stage==1 && stop.target_ticks==400005 && a.full_sl_count==4);
 assert(!RCProposeSupport(a,400005,0,400055,400076,25,40,25,stop)); // no weakening or reclassification
 assert(RCProposeSupport(a,400005,1,400070,400090,25,40,25,stop) && stop.stage==2 && stop.target_ticks==400030);
 assert(!RCProposeSupport(b,400100,0,399994,400006,25,40,25,stop));
 assert(RCProposeSupport(b,400100,0,399994,400005,25,40,25,stop) && stop.target_ticks==400055);
 assert(RCProposeSupport(b,400055,1,399965,399990,25,40,25,stop) && stop.target_ticks==400030);
 assert(!RCProposeSupport(a,400005,1,400069,400090,25,40,25,stop));
 assert(!RCProposeSupport(b,400055,1,399965,399991,25,40,25,stop));
 assert(RCProposeSupport(a,399960,0,400060,400080,30,45,25,stop) && stop.stage==1 && stop.target_ticks==400005);
 assert(RCProposeSupport(a,399960,0,400075,400090,30,45,25,stop) && stop.stage==2 && stop.target_ticks==400030);
 assert(!RCProposeSupport(a,399960,0,400075,400090,45,45,25,stop));

 RCClosePlan out{};
 auto fact=close(a);
 fact.exited_steps=3;fact.remaining_steps=4;
 assert(!RCApplyClose(a,fact,70,out) && a.active_order==30 && a.full_sl_count==4);
 fact=close(a);fact.uniform_exit_reason_and_stop=false;
 assert(!RCApplyClose(a,fact,70,out));
 fact=close(a);fact.confirmed_stop_mask=RC_INITIAL_LEVEL|RC_SUPPORT_LEVEL;
 assert(!RCApplyClose(a,fact,70,out));
 fact=close(a);fact.confirmed_stop_mask=0;
 assert(!RCApplyClose(a,fact,70,out));
 fact=close(a);fact.range_id=2;assert(!RCApplyClose(a,fact,70,out));
 fact=close(a);fact.history_complete=false;assert(!RCApplyClose(a,fact,70,out));
 fact=close(a);
 assert(RCApplyClose(a,fact,70,out) && out.classification==RC_FULL_SL && out.current_slot==6);
 assert(out.keep_existing_opposite && !out.reentry_slot && !out.cancel_remaining_range_orders);
 assert(a.full_sl_count==5 && b.full_sl_count==0);
 assert(!RCApplyClose(a,fact,70,out) && a.full_sl_count==5);
 assert(!RCApplyEntry(a,entry()));
 assert(RCApplyEntry(a,entry(1,31,51)));
 fact=close(a,RC_SL,400005,RC_SUPPORT_LEVEL);
 assert(!RCApplyClose(a,fact,0,out));
 assert(RCApplyClose(a,fact,70,out) && out.classification==RC_SUPPORT_SL);
 assert(out.keep_existing_opposite && out.reentry_slot==6 && out.reentry_side==1 && out.reentry_ticks==400075);
 assert(a.full_sl_count==5);
 fact=close(b,RC_SL,400055,RC_SUPPORT_LEVEL);
 assert(RCApplyClose(b,fact,70,out) && out.reentry_side==-1 && out.reentry_ticks==399985);
 assert(RCApplyEntry(b,entry(2,41,61,-1)));
 fact=close(b,RC_TP,0,0);
 assert(RCClassifyClose(b,fact)==RC_TP_EXIT);
 assert(!RCApplyClose(b,fact,70,out) && b.position_id==61 && b.full_sl_count==0);
 fact=close(b,RC_MANUAL,0,0);
 assert(RCApplyClose(b,fact,70,out) && out.keep_existing_opposite && out.current_slot==1);
 auto limit=range(1,22);
 assert(RCCurrentSlot(limit)==23 && RCReverseSlot(limit)==24);
 assert(RCApplyEntry(limit,entry()));
 assert(RCApplyClose(limit,close(limit),70,out) && out.current_slot==24);
 assert(RCReverseSlot(limit)==0);
 assert(RCApplyEntry(limit,entry(1,31,51)));
 assert(RCApplyClose(limit,close(limit),70,out) && limit.full_sl_count==24);
 assert(limit.requires_manual_ranges && out.cancel_remaining_range_orders && !out.keep_existing_opposite);
 assert(RCCurrentSlot(limit)==0 && RCReverseSlot(limit)==0 && !RCApplyEntry(limit,entry(1,32,52)));

 auto sequence=range();
 for(int n=1;n<=24;n++) {
  assert(RCCurrentSlot(sequence)==n);
  assert(RCReverseSlot(sequence)==(n==24?0:n+1));
  assert(RCApplyEntry(sequence,entry(1,100+n,200+n)));
  auto sl=close(sequence);
  assert(RCApplyClose(sequence,sl,70,out));
  assert(!RCApplyClose(sequence,sl,70,out));
  assert(sequence.full_sl_count==n && sequence.requires_manual_ranges==(n==24));
 }
}
