#ifndef RECOVERY_OPENING_PAIR_MQH
#define RECOVERY_OPENING_PAIR_MQH
#include "RecoveryRangeCoordinator.mqh"

// A pair reserves two intents up front, never dispatches them. The broker
// writer journals and sends the SL modification followed by the opposite
// stop; full independent effect verification is outside the send sequence.
enum RROpeningResult { RR_PAIR_WAITING=0,RR_PAIR_CONFIRMED=1,RR_PAIR_CONFLICT=2 };
struct RROpeningPair {
 bool reserved,sl_required,sl_verified,reverse_verified;
 ulong active_order,position_id,position_ticket,reverse_ticket;
 RRAction sl,reverse;
};

bool RRStageOpeningPair(RRRangeState &s,const RRSettings &cfg,RROpeningPair &pair) {
 if(pair.reserved || !RRValid(s,cfg) || s.pending.kind!=RR_NONE ||
    s.retire.ticket!=0 || s.entry.ticket!=0 || s.opposite.ticket!=0 ||
    s.cycle.active_order==0 || s.cycle.position_id==0 || s.position_ticket==0 ||
    !RCValidSide(s.cycle.side) || !RCBoundedPositive(s.initial_sl))return false;
 const bool sl_required=s.phase==RR_PROTECT;
 if(sl_required ? s.live_sl==s.initial_sl :
    (s.phase!=RR_ARM_REVERSE || s.live_sl!=s.initial_sl)) return false;
 const int slot=RCReverseSlot(s.cycle);
 if(slot==0 || s.action_serial>ULONG_MAX-(sl_required?2:1))return false;

 RROpeningPair p;
 p.reserved=true;p.sl_required=sl_required;p.sl_verified=!sl_required;p.reverse_verified=false;
 p.active_order=s.cycle.active_order;p.position_id=s.cycle.position_id;
 p.position_ticket=s.position_ticket;p.reverse_ticket=0;
 RRClearAction(p.sl);RRClearAction(p.reverse);
 ulong serial=s.action_serial;
 if(sl_required) {
    if(!RRPositionAction(s,s.initial_sl,0,p.sl))return false;
    p.sl.kind=RR_INITIAL_SL;
    p.sl.scope=s.scope;p.sl.generation=s.cycle.generation;p.sl.range_id=s.cycle.range_id;
    p.sl.serial=++serial;
 }
 p.reverse.kind=RR_REVERSE;
 if(!RRMakeEntry(cfg,-s.cycle.side,s.initial_sl,slot,p.reverse))return false;
 p.reverse.scope=s.scope;p.reverse.generation=s.cycle.generation;p.reverse.range_id=s.cycle.range_id;
 p.reverse.serial=++serial;
 // Block all regular actions/closures while the opening pair is outstanding.
 s.action_serial=serial;
 if(sl_required)s.pending=p.sl; else s.pending=p.reverse;
 pair=p;
 return true;
}

bool RROpeningBound(const RRRangeState &s,const RROpeningPair &p) {
 if(!p.reserved || s.scope==0 || p.active_order==0 || p.position_id==0 || p.position_ticket==0 ||
    p.active_order!=s.cycle.active_order || p.position_id!=s.cycle.position_id ||
    p.position_ticket!=s.position_ticket || s.retire.ticket!=0 || s.entry.ticket!=0 ||
    s.opposite.ticket!=0 || RCReverseSlot(s.cycle)==0 || !RCValidSide(s.cycle.side) ||
    p.reverse.kind!=RR_REVERSE || p.reverse.scope!=s.scope ||
    p.reverse.generation!=s.cycle.generation || p.reverse.range_id!=s.cycle.range_id ||
    p.reverse.serial==0 || p.reverse.serial!=s.action_serial ||
    p.reverse.side!=-s.cycle.side || p.reverse.entry_ticks!=s.initial_sl ||
    !RCBoundedPositive(p.reverse.entry_ticks) || !RCBoundedPositive(p.reverse.sl_ticks) ||
    !RCBoundedPositive(p.reverse.volume_steps) || p.reverse.stage!=0 ||
    p.reverse.order_ticket!=0 || p.reverse.position_id!=0 || p.reverse.position_ticket!=0 ||
    p.reverse.tp_ticks!=0 || (p.reverse_verified!=(p.reverse_ticket!=0)))return false;
 if(p.sl_required) {
    return s.phase==RR_PROTECT && s.live_sl!=s.initial_sl &&
           p.sl.kind==RR_INITIAL_SL && RRActionEqual(s.pending,p.sl) &&
           p.sl.scope==s.scope && p.sl.generation==s.cycle.generation &&
           p.sl.range_id==s.cycle.range_id && p.sl.serial>0 &&
           p.sl.serial==p.reverse.serial-1 &&
           p.sl.order_ticket==p.active_order && p.sl.position_id==p.position_id &&
           p.sl.position_ticket==p.position_ticket && p.sl.side==s.cycle.side &&
           p.sl.sl_ticks==s.initial_sl && p.sl.tp_ticks==s.live_tp &&
           p.sl.stage==0 && p.sl.entry_ticks==0 && p.sl.volume_steps==0;
 }
 return s.phase==RR_ARM_REVERSE && s.live_sl==s.initial_sl &&
        p.sl.kind==RR_NONE && p.sl_verified && RRActionEqual(s.pending,p.reverse);
}

// Produces an immutable transport-planning view; not a new reservation, not
// permission to resend, and not proof that the preceding broker call succeeded.
bool RROpeningRequestState(const RRRangeState &s,const RROpeningPair &p,const RRAction &a,
                           RRRangeState &request_state) {
 if(!RROpeningBound(s,p))return false;
 if(a.kind==RR_INITIAL_SL && p.sl_required && !p.sl_verified && RRActionEqual(a,p.sl)) {
    request_state=s;request_state.pending=a;return true;
 }
 if(a.kind==RR_REVERSE && !p.reverse_verified && RRActionEqual(a,p.reverse)) {
    request_state=s;request_state.pending=a;return true;
 }
 return false;
}

RROpeningResult RRConfirmOpeningPair(RRRangeState &s,RROpeningPair &p,const RREffect &e) {
 if(!RROpeningBound(s,p))return RR_PAIR_CONFLICT;
 if(!e.terminal_verified)return RR_PAIR_WAITING;
 RROpeningPair next=p;
 if(p.sl_required && RRActionEqual(p.sl,e.action)) {
    if(e.created_order!=0)return RR_PAIR_CONFLICT;
    next.sl_verified=true;
 } else if(RRActionEqual(p.reverse,e.action)) {
    if(e.created_order==0 || e.created_order==s.cycle.active_order ||
       e.created_order==s.cycle.last_closed_order ||
       (p.reverse_verified && p.reverse_ticket!=e.created_order)) return RR_PAIR_CONFLICT;
    next.reverse_verified=true;next.reverse_ticket=e.created_order;
 } else return RR_PAIR_CONFLICT;
 if(!next.sl_verified || !next.reverse_verified) {
    p=next;return RR_PAIR_WAITING;
 }

 // Publication of state, cleared opening pair, and retained audit receipts
 // belongs to caller. Calculate the whole transition into a scratch copy.
 RRRangeState after=s;
 if(next.sl_required) {
    RREffect sl;sl.action=next.sl;sl.terminal_verified=true;sl.created_order=0;
    if(!RRConfirm(after,sl))return RR_PAIR_CONFLICT;
 }
 after.pending=next.reverse;
 RREffect reverse;reverse.action=next.reverse;reverse.terminal_verified=true;
 reverse.created_order=next.reverse_ticket;
 if(!RRConfirm(after,reverse))return RR_PAIR_CONFLICT;
 next.reserved=false;
 s=after;p=next;
 return RR_PAIR_CONFIRMED;
}
#endif
