#include <cassert>
#include <cstdint>
using ulong = std::uint64_t;
#include "RecoveryAdoptionGate.mqh"

int main() {
    const RecoveryAdoptionIntent buy{7,40,100,101,200,RA_SIDE_BUY,400070,400500,400140,1};
    const RecoveryAdoptionProof snapshot{7,41,true,true,100,101,RA_SIDE_BUY,400070,400500,
        true,200,RA_SIDE_SELL,RA_ORDER_WORKING_STOP,400070,400140,1};
    auto b = buy;
    auto p = snapshot;
    assert(RecoveryCheckAdoption(b,p)==RA_CONFIRMED);
    p.sequence=40; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    p=snapshot; p.scope=8; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    p=snapshot; p.coherent=false; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    p=snapshot; p.position_present=false; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    p=snapshot; p.position_id++; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    p=snapshot; p.position_ticket++; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    p=snapshot; p.position_tp_ticks=0; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    p=snapshot; p.position_sl_ticks=0; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    p=snapshot; p.order_ticket++; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    p=snapshot; p.order_present=false; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    p=snapshot; p.order_state=RA_ORDER_TERMINAL; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    p=snapshot; p.order_state=RA_ORDER_OTHER; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    p=snapshot; p.order_entry_ticks--; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    p=snapshot; p.order_sl_ticks--; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    p=snapshot; p.order_side=RA_SIDE_BUY; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    p=snapshot; p.order_volume_steps++; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    p=snapshot; b.opposite_ticket=0; assert(RecoveryCheckAdoption(b,p)==RA_UNRESOLVED);
    b=buy; b.opposite_sl_ticks=b.expected_sl_ticks; assert(RecoveryCheckAdoption(b,p)==RA_CONFLICT);
    b=buy; b.position_side=RA_SIDE_SELL; p.position_side=RA_SIDE_SELL;
    b.expected_sl_ticks=399930; p.position_sl_ticks=399930; p.order_entry_ticks=399930;
    b.opposite_sl_ticks=399860; p.order_sl_ticks=399860; p.order_side=RA_SIDE_BUY;
    b.original_tp_ticks=0; p.position_tp_ticks=0;
    assert(RecoveryCheckAdoption(b,p)==RA_CONFIRMED);
}
