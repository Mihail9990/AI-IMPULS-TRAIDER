#include <cassert>
#include <cstdint>
#include <vector>
using ulong=std::uint64_t;
template<class T> int ArraySize(const std::vector<T>& a){return static_cast<int>(a.size());}
#include "RecoveryCloseEvidence.cpp.inc"

static RCRange range (int id=1,int side=1) {
 RCRange r{}; r.generation=5;r.range_id=id;r.full_sl_count=4;
 r.active_order=30;r.position_id=9007199254740993ULL;r.side=side;r.filled_steps=7;return r;
}
static RCCloseEvidenceKey key(const RCRange &r){return {1,2,r.generation,r.range_id,r.position_id,r.active_order};}
static RCProtectionFact level(const RCCloseEvidenceKey& k,int kind,long price,bool confirmed=true) {
 return {k.scope,k.generation,k.position_id,k.anchor_order,k.range_id,kind,price,confirmed};
}
static RCExitDeal deal(const RCRange& r,ulong t,long size,long exec,long sl,int reason=RC_SL) {
 return {t,r.position_id,55,-r.side,reason,size,exec,sl};
}
int main() {
 auto r=range();const auto k=key(r);
 RCExitHistory h{k,true,true,true,7};
 RCCloseInventory i{k,true,false,0,0};
 std::vector<RCProtectionFact> stops{
   level(k,RC_INITIAL_LEVEL,4000),level(k,RC_SUPPORT_LEVEL,4050)};
 std::vector<RCExitDeal> deals{
   deal(r,100,1,3987,4000),deal(r,101,6,3990,4000)};
 RCCloseProof proof{};
 auto build=[&]{return RCBuildCloseProof(r,k,h,i,deals,stops,proof);};
 assert(build()==RCE_FULLY_CLASSIFIED);
 assert(RCClassifyClose(r,proof)==RC_FULL_SL && proof.exit_stop_ticks==4000 && proof.exited_steps==7);
 std::swap(deals[0],deals[1]);
 assert(build()==RCE_FULLY_CLASSIFIED);
 deals.push_back(deals[0]);assert(build()==RCE_FULLY_CLASSIFIED && proof.exited_steps==7);
 deals.back().execution_ticks++;assert(build()==RCE_UNRESOLVED && !proof.independently_verified);
 deals.pop_back();
 deals.back().closing_order++;assert(build()==RCE_FULLY_CLASSIFIED); // two distinct closes are possible
 deals.back().volume_steps++;assert(build()==RCE_UNRESOLVED);
 deals.back().volume_steps--;
 deals.back().position_id++;assert(build()==RCE_UNRESOLVED);
 deals.back().position_id--;
 deals.back().exit_side=1;assert(build()==RCE_UNRESOLVED);deals.back().exit_side=-1;
 deals.back().closing_order=k.anchor_order;assert(build()==RCE_UNRESOLVED);deals.back().closing_order=55;
 h.complete=false;assert(build()==RCE_UNRESOLVED);h.complete=true;
 h.anchor_final=false;assert(build()==RCE_UNRESOLVED);h.anchor_final=true;
 h.anchor_matches_position=false;assert(build()==RCE_UNRESOLVED);h.anchor_matches_position=true;
 h.observed_entry_steps=8;assert(build()==RCE_UNRESOLVED);h.observed_entry_steps=7;
 i.complete=false;assert(build()==RCE_UNRESOLVED);i.complete=true;
 i.key.scope++;assert(build()==RCE_UNRESOLVED);i.key.scope--;
 i.key.observation++;assert(build()==RCE_UNRESOLVED);i.key.observation--;
 h.key.generation++;assert(build()==RCE_UNRESOLVED);h.key.generation--;
 h.key.range_id++;assert(build()==RCE_UNRESOLVED);h.key.range_id--;
 i.present=true;i.observed_position_id=r.position_id;i.remaining_steps=1;
 assert(build()==RCE_UNRESOLVED); // total history and remaining disagree
 auto one=deals.back();deals.pop_back();
 assert(build()==RCE_OPEN_OR_PARTIAL && !proof.independently_verified);
 i.observed_position_id++;assert(build()==RCE_UNRESOLVED);i.observed_position_id--;
 i.present=false;i.observed_position_id=0;i.remaining_steps=0;
 assert(build()==RCE_UNRESOLVED); // absence plus partial exits is still unknown
 auto saved=deals; deals.clear();assert(build()==RCE_UNRESOLVED); // absence alone
 deals=saved;deals.push_back(one);
 stops.push_back(level(k,RC_SUPPORT_LEVEL,4000,false));
 assert(build()==RCE_FULLY_CLASSIFIED); // an unconfirmed adjustment isn't protection
 stops.back().confirmed=true;assert(build()==RCE_UNRESOLVED);
 stops.back().position_id++;assert(build()==RCE_FULLY_CLASSIFIED);
 stops.pop_back();
 stops[0].confirmed=false;assert(build()==RCE_UNRESOLVED);stops[0].confirmed=true;
 deals[0].deal_sl_ticks=4050;assert(build()==RCE_UNRESOLVED); // mixed exact SL levels
 deals[1].deal_sl_ticks=4050;assert(build()==RCE_FULLY_CLASSIFIED);
 assert(RCClassifyClose(r,proof)==RC_SUPPORT_SL && proof.exit_stop_ticks==4050);
 RCClosePlan plan{};
 assert(RCApplyClose(r,proof,70,plan));
 assert(r.full_sl_count==4 && plan.reentry_ticks==4120 && plan.keep_existing_opposite);
 assert(!RCApplyClose(r,proof,70,plan)); // same closure, already applied
 assert(build()==RCE_UNRESOLVED); // stale scope can't construct a fresh transition

 r=range();
 deals={deal(r,100,1,3987,4000,RC_MANUAL),deal(r,101,6,3990,4050,RC_MANUAL)};
 assert(build()==RCE_FULLY_CLASSIFIED && RCClassifyClose(r,proof)==RC_MANUAL_EXIT);
 deals[0].reason=RC_SL;assert(build()==RCE_UNRESOLVED);
 deals[0].reason=0;deals[1].reason=0;assert(build()==RCE_UNRESOLVED);
 deals={deal(r,103,7,3990,4000)};
 assert(build()==RCE_FULLY_CLASSIFIED && RCApplyClose(r,proof,70,plan));
 assert(r.full_sl_count==5 && plan.current_slot==6);
 r=range(1,-1);
 deals={deal(r,103,7,4111,4050)};
 assert(build()==RCE_FULLY_CLASSIFIED && RCApplyClose(r,proof,70,plan));
 assert(plan.reentry_ticks==3980 && plan.reentry_side==-1);
}
