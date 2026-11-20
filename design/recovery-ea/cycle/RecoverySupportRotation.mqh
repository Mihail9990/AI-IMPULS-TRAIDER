#ifndef RECOVERY_SUPPORT_ROTATION_MQH
#define RECOVERY_SUPPORT_ROTATION_MQH
#include "RecoveryOpeningPair.mqh"

#define RR_MOVE_SUPPORT_OPPOSITE 7
struct RRSupportRotation {
 bool reserved,sl_required,sl_verified,move_required,move_verified;
 bool actual_entry_only; // single break-even mode modifies the survivor only
 ulong active_order,position_id,position_ticket;
 RRKnownOrder previous; // exact unchanged old order before modification
 RRAction sl,move;
};

// Runtime must route these two alternatives here, never to the older
// cancel-and-replace path merely because this verification did not succeed.
bool RRStageSupportRotation(RRRangeState &s,const RRSettings &cfg,const RRFilled &fill,
                            const RRKnownOrder &observed_remaining,RRSupportRotation &rotation) {
 if(rotation.reserved || !RRValid(s,cfg) || !RRHasSupportAlternatives(s) || s.pending.kind!=RR_NONE ||
    s.retire.ticket!=0 ||
    s.entry.ticket==s.opposite.ticket || !RCValidSide(s.reentry_side) ||
    s.entry.side!=s.reentry_side || s.opposite.side!=-s.reentry_side ||
    s.entry.entry_ticks!=s.reentry_ticks || s.reentry_slot<1 || s.reentry_slot>24 ||
    s.entry.volume_steps!=cfg.lot_steps[s.reentry_slot-1] ||
    s.cycle.position_id!=0 || RCReverseSlot(s.cycle)==0)return false;
 RRKnownOrder other;
 if(fill.entry.executed_order==s.entry.ticket)other=s.opposite;
 else if(fill.entry.executed_order==s.opposite.ticket)other=s.entry;
 else return false;
 if(!RRKnownEqual(other,observed_remaining) || other.side!=-fill.entry.side ||
    fill.working_other_ticket!=other.ticket)return false;

 RRRangeState n=s;
 if(!RRObserveFillCore(n,cfg,fill) || !RRKnownEqual(n.retire,other))return false;
 RRSupportRotation p;
 p.active_order=n.cycle.active_order;p.position_id=n.cycle.position_id;p.position_ticket=n.position_ticket;
 p.previous=other;
 const long new_entry=n.initial_sl;
 const long new_order_sl=new_entry-other.side*cfg.initial_sl_ticks;
 if(!RCBoundedPositive(new_entry) || !RCBoundedPositive(new_order_sl))return false;
 p.actual_entry_only=cfg.support_first_ticks==0;
 if(p.actual_entry_only) {
    // Keep the filled order's confirmed attached stop exactly as observed.
    // Only the still-working ticket is recentered around the actual fill.
    n.initial_sl=fill.sl_ticks;n.opposite_anchor_ticks=new_entry;n.phase=RR_ARM_REVERSE;
 }
 p.sl_required=!p.actual_entry_only && n.live_sl!=new_entry;p.sl_verified=!p.sl_required;
 p.move_required=(other.entry_ticks!=new_entry || other.sl_ticks!=new_order_sl);
 p.move_verified=!p.move_required;
 p.reserved=p.sl_required||p.move_required;
 RRClearAction(p.sl);RRClearAction(p.move);
 const int requests=(p.sl_required?1:0)+(p.move_required?1:0);
 if(n.action_serial>ULONG_MAX-(ulong)requests)return false;

 if(p.sl_required) {
    if(!RRPositionAction(n,new_entry,0,p.sl))return false;
    p.sl.kind=RR_INITIAL_SL;p.sl.scope=n.scope;p.sl.generation=n.cycle.generation;
    p.sl.range_id=n.cycle.range_id;p.sl.serial=++n.action_serial;
 }
 if(p.move_required) {
    p.move.kind=RR_MOVE_SUPPORT_OPPOSITE;p.move.scope=n.scope;p.move.generation=n.cycle.generation;
    p.move.range_id=n.cycle.range_id;p.move.serial=++n.action_serial;
    p.move.order_ticket=other.ticket;p.move.side=other.side;
    p.move.entry_ticks=new_entry;p.move.sl_ticks=new_order_sl;p.move.volume_steps=other.volume_steps;
 }
 if(p.sl_required)n.pending=p.sl;
 else if(p.move_required)n.pending=p.move;
 else {
    // Exact stop and working order already verified at the intended geometry.
    // No modification just for an identical value; ticket/lot stay unchanged.
    n.opposite=other;RRClearKnown(n.retire);n.phase=RR_ACTIVE;
 }
 s=n;rotation=p;
 return true;
}

bool RRSupportRotationBound(const RRRangeState &s,const RRSupportRotation &p) {
 if(!p.reserved || s.scope==0 || RCReverseSlot(s.cycle)==0 ||
    p.active_order==0 || p.position_id==0 || p.position_ticket==0 ||
    p.active_order!=s.cycle.active_order || p.position_id!=s.cycle.position_id ||
    p.position_ticket!=s.position_ticket || s.entry.ticket!=0 || s.opposite.ticket!=0 ||
    !RRKnownEqual(s.retire,p.previous) || p.previous.ticket==0 ||
    p.previous.side!=-s.cycle.side || !RCBoundedPositive(p.previous.volume_steps))return false;
 if(p.actual_entry_only) {
    if(p.sl_required || !RCBoundedPositive(s.opposite_anchor_ticks) || s.live_sl!=s.initial_sl)
       return false;
 } else if(s.opposite_anchor_ticks!=0)return false;
 if(p.sl_required) {
    if(s.phase!=RR_PROTECT || s.live_sl==s.initial_sl || !RRActionEqual(s.pending,p.sl) ||
       p.sl.kind!=RR_INITIAL_SL || p.sl.serial==0 || p.sl.scope!=s.scope ||
       p.sl.generation!=s.cycle.generation || p.sl.range_id!=s.cycle.range_id ||
       p.sl.order_ticket!=p.active_order || p.sl.position_id!=p.position_id ||
       p.sl.position_ticket!=p.position_ticket || p.sl.side!=s.cycle.side ||
       p.sl.sl_ticks!=s.initial_sl || p.sl.tp_ticks!=s.live_tp ||
       p.sl.stage!=0 || p.sl.entry_ticks!=0 || p.sl.volume_steps!=0)return false;
 } else if(s.phase!=RR_ARM_REVERSE || !p.sl_verified || p.sl.kind!=RR_NONE ||
           s.live_sl!=s.initial_sl || !RRActionEqual(s.pending,p.move))return false;
 if(p.move_required) {
    if(p.move.kind!=RR_MOVE_SUPPORT_OPPOSITE || p.move.scope!=s.scope ||
       p.move.generation!=s.cycle.generation || p.move.range_id!=s.cycle.range_id ||
       p.move.serial==0 || p.move.serial!=s.action_serial ||
       (p.sl_required && p.sl.serial!=p.move.serial-1) ||
       p.move.order_ticket!=p.previous.ticket || p.move.side!=p.previous.side ||
       p.move.volume_steps!=p.previous.volume_steps || p.move.entry_ticks!=RROppositeAnchor(s) ||
       p.move.sl_ticks!=s.cycle.actual_entry_ticks ||
       !RCBoundedPositive(p.move.entry_ticks) || !RCBoundedPositive(p.move.sl_ticks) ||
       (p.move.sl_ticks-p.move.entry_ticks)*p.move.side>=0 ||
       p.move.position_id!=0 || p.move.position_ticket!=0 || p.move.tp_ticks!=0 || p.move.stage!=0)
       return false;
 } else if(!p.move_verified || p.move.kind!=RR_NONE || !p.sl_required ||
           p.sl.serial!=s.action_serial || p.previous.entry_ticks!=RROppositeAnchor(s) ||
           p.previous.sl_ticks!=s.cycle.actual_entry_ticks)
    return false;
 return true;
}

bool RRSupportRotationRequestState(const RRRangeState &s,const RRSupportRotation &p,
                                  const RRAction &a,RRRangeState &shadow) {
 if(!RRSupportRotationBound(s,p))return false;
 if(p.sl_required && !p.sl_verified && RRActionEqual(a,p.sl)) {
    shadow=s;shadow.pending=a;return true;
 }
 if(p.move_required && !p.move_verified && RRActionEqual(a,p.move)) {
    shadow=s;shadow.pending=a;return true;
 }
 return false;
}

RROpeningResult RRConfirmSupportRotation(RRRangeState &s,RRSupportRotation &p,const RREffect &effect) {
 if(!RRSupportRotationBound(s,p))return RR_PAIR_CONFLICT;
 if(!effect.terminal_verified)return RR_PAIR_WAITING;
 if(effect.created_order!=0)return RR_PAIR_CONFLICT; // MODIFY, never a created order
 RRSupportRotation n=p;
 if(p.sl_required && RRActionEqual(effect.action,p.sl))n.sl_verified=true;
 else if(p.move_required && RRActionEqual(effect.action,p.move))n.move_verified=true;
 else return RR_PAIR_CONFLICT;
 if(!n.sl_verified || !n.move_verified) {
    p=n;return RR_PAIR_WAITING;
 }
 RRRangeState after=s;
 if(n.sl_required) {
    RREffect protection;protection.action=n.sl;protection.terminal_verified=true;
    protection.created_order=0;
    if(!RRConfirm(after,protection))return RR_PAIR_CONFLICT;
 }
 if(n.move_required)RRSetKnown(after.opposite,n.previous.ticket,n.move);
 else after.opposite=n.previous;
 RRClearKnown(after.retire);RRClearAction(after.pending);
 after.phase=RR_ACTIVE;n.reserved=false;
 s=after;p=n;
 return RR_PAIR_CONFIRMED;
}
#endif
