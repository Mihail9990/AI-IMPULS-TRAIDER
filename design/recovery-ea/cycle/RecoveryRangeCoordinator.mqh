#ifndef RECOVERY_RANGE_COORDINATOR_MQH
#define RECOVERY_RANGE_COORDINATOR_MQH
#include "RecoveryCloseCheckpoint.mqh"

// Per-range broker-neutral action coordinator. Its state and action identity
// must be persisted before an external transport does anything with an intent.
enum RRPhase { RR_NEW=1, RR_WAIT=2, RR_PROTECT=3, RR_ARM_REVERSE=4,
               RR_ACTIVE=5, RR_ARM_REENTRY=6, RR_DRAIN=7, RR_DONE=8 };
enum RRActionKind { RR_NONE=0, RR_PRIMARY=1, RR_INITIAL_SL=2, RR_REVERSE=3,
                    RR_SUPPORT_SL=4, RR_REENTRY=5, RR_CANCEL=6 };

struct RRSettings {
 ulong scope,generation;
 long initial_sl_ticks,support_first_ticks,support_second_ticks,support_offset_ticks;
 long lot_steps[24];          // validated, immutable strategy snapshot for generation
};
struct RRKnownOrder {
 ulong ticket;
 int side;
 long entry_ticks,sl_ticks,volume_steps;
};
struct RRAction {
 ulong scope,generation,serial;
 int range_id,kind,side,stage;
 ulong order_ticket,position_id,position_ticket;
 long entry_ticks,sl_ticks,tp_ticks,volume_steps;
};
struct RREffect {
 RRAction action;
 bool terminal_verified;      // exact observed effect, not a successful send/retcode
 ulong created_order;
};
struct RRFilled {
 RCEntryProof entry;
 ulong position_ticket,working_other_ticket;
 long sl_ticks,tp_ticks;
 bool independently_verified; // complete post-fill inventory for position + unchanged other order
};
struct RRRangeState {
 RCRange cycle;
 ulong scope,action_serial;
 int phase,primary_side,support_stage;
 long primary_ticks,initial_sl,live_sl,live_tp,reentry_ticks;
 int reentry_side,reentry_slot;
 ulong position_ticket;
 RRKnownOrder entry,opposite,retire;
 RRAction pending;
};

bool RRKnownEqual(const RRKnownOrder &a,const RRKnownOrder &b) {
 return a.ticket==b.ticket && a.side==b.side && a.entry_ticks==b.entry_ticks &&
        a.sl_ticks==b.sl_ticks && a.volume_steps==b.volume_steps;
}
void RRClearKnown(RRKnownOrder &o) {
 o.ticket=0;o.side=0;o.entry_ticks=0;o.sl_ticks=0;o.volume_steps=0;
}
void RRClearAction(RRAction &a) {
 a.scope=0;a.generation=0;a.serial=0;a.range_id=0;a.kind=RR_NONE;a.side=0;a.stage=0;
 a.order_ticket=0;a.position_id=0;a.position_ticket=0;
 a.entry_ticks=0;a.sl_ticks=0;a.tp_ticks=0;a.volume_steps=0;
}
bool RRSettingsValid(const RRSettings &cfg) {
 if(cfg.scope==0 || cfg.generation==0 || !RCBoundedPositive(cfg.initial_sl_ticks) ||
    (cfg.support_first_ticks<0 || cfg.support_first_ticks>RC_MAX_UNITS) || !RCBoundedPositive(cfg.support_second_ticks) ||
    (cfg.support_offset_ticks<0 || cfg.support_offset_ticks>RC_MAX_UNITS || (cfg.support_first_ticks>0 && cfg.support_offset_ticks==0)) || cfg.support_second_ticks<=cfg.support_first_ticks) return false;
 for(int i=0;i<24;i++) if(!RCBoundedPositive(cfg.lot_steps[i]))return false;
 return true;
}
bool RRNewRange(RRRangeState &s,const RRSettings &cfg,const int rid,const int side,const long price) {
 if(s.scope!=0 || !RRSettingsValid(cfg) || (rid!=1 && rid!=2) ||
    !RCValidSide(side) || !RCBoundedPositive(price) ||
    !RCBoundedPositive(price-side*cfg.initial_sl_ticks))return false;
 s.scope=cfg.scope;s.action_serial=0;s.phase=RR_NEW;s.primary_side=side;s.primary_ticks=price;
 s.cycle.generation=cfg.generation;s.cycle.range_id=rid;s.cycle.full_sl_count=0;
 s.cycle.requires_manual_ranges=false;s.cycle.active_order=0;s.cycle.position_id=0;s.cycle.side=0;
 s.cycle.filled_steps=0;s.cycle.actual_entry_ticks=0;
 s.cycle.last_closed_order=0;s.cycle.last_closed_position=0;
 s.support_stage=0;s.initial_sl=0;s.live_sl=0;s.live_tp=0;s.position_ticket=0;
 s.reentry_ticks=0;s.reentry_side=0;s.reentry_slot=0;
 RRClearKnown(s.entry);RRClearKnown(s.opposite);RRClearKnown(s.retire);RRClearAction(s.pending);
 return true;
}
bool RRValid(const RRRangeState &s,const RRSettings &cfg) {
 return RRSettingsValid(cfg) && s.scope==cfg.scope && RCBound(s.cycle,cfg.generation,s.cycle.range_id);
}

bool RRMakeEntry(const RRSettings &cfg,const int side,const long price,const int slot,RRAction &a) {
 if(slot<1 || slot>24 || !RCValidSide(side) || !RCBoundedPositive(price))return false;
 const long sl=price-side*cfg.initial_sl_ticks;
 if(!RCBoundedPositive(sl))return false;
 a.side=side;a.entry_ticks=price;a.sl_ticks=sl;a.volume_steps=cfg.lot_steps[slot-1];
 return true;
}
void RRExactCancel(const RRKnownOrder &order,RRAction &a) {
 a.kind=RR_CANCEL;a.order_ticket=order.ticket;a.side=order.side;
 a.entry_ticks=order.entry_ticks;a.sl_ticks=order.sl_ticks;a.volume_steps=order.volume_steps;
}
bool RRPositionAction(const RRRangeState &s,const long sl,const int stage,RRAction &a) {
 if(s.position_ticket==0 || !RCBoundedPositive(sl) || !RCValidSide(s.cycle.side) ||
    s.cycle.position_id==0 || s.cycle.active_order==0 || s.live_tp<0 || s.live_tp>RC_MAX_UNITS)return false;
 a.order_ticket=s.cycle.active_order;a.position_id=s.cycle.position_id;a.position_ticket=s.position_ticket;
 a.side=s.cycle.side;a.sl_ticks=sl;a.tp_ticks=s.live_tp;a.stage=stage;
 return true;
}

// Once staged, an UNKNOWN result leaves pending intact. Repeated polling does
// not return the staged action as a fresh send instruction.
bool RRStage(RRRangeState &s,const RRSettings &cfg,const long bid,const long ask,RRAction &out) {
 RRClearAction(out);
 if(!RRValid(s,cfg) || s.pending.kind!=RR_NONE || s.action_serial==ULONG_MAX)return false;
 RRAction a;RRClearAction(a);
 if(s.phase==RR_NEW) {
    a.kind=RR_PRIMARY;
    if(!RRMakeEntry(cfg,s.primary_side,s.primary_ticks,1,a))return false;
 } else if(s.phase==RR_PROTECT) {
    a.kind=RR_INITIAL_SL;
    if(!RRPositionAction(s,s.initial_sl,0,a))return false;
 } else if(s.phase==RR_ARM_REVERSE) {
    if(s.retire.ticket>0) RRExactCancel(s.retire,a);
    else {
       const int slot=RCReverseSlot(s.cycle);
       if(slot==0 || s.opposite.ticket!=0 || s.live_sl!=s.initial_sl)return false;
       a.kind=RR_REVERSE;
       if(!RRMakeEntry(cfg,-s.cycle.side,s.initial_sl,slot,a))return false;
    }
 } else if(s.phase==RR_ACTIVE) {
    RCStopProposal p;
    if(!RCProposeSupport(s.cycle,s.live_sl,s.support_stage,bid,ask,cfg.support_first_ticks,cfg.support_second_ticks,
                         cfg.support_offset_ticks,p))
       return false;
    a.kind=RR_SUPPORT_SL;
    if(!RRPositionAction(s,p.target_ticks,p.stage,a))return false;
 } else if(s.phase==RR_ARM_REENTRY) {
    a.kind=RR_REENTRY;
    if(s.entry.ticket!=0 || !RRMakeEntry(cfg,s.reentry_side,s.reentry_ticks,s.reentry_slot,a))
       return false;
 } else if(s.phase==RR_DRAIN) {
    if(s.retire.ticket!=0) RRExactCancel(s.retire,a);
    else if(s.entry.ticket!=0) RRExactCancel(s.entry,a);
    else if(s.opposite.ticket!=0) RRExactCancel(s.opposite,a);
    else return false;
 } else return false;
 a.scope=s.scope;a.generation=s.cycle.generation;a.range_id=s.cycle.range_id;a.serial=++s.action_serial;
 s.pending=a;out=a;
 return true;
}
bool RRActionEqual(const RRAction &a,const RRAction &b) {
 return a.scope==b.scope && a.generation==b.generation && a.serial==b.serial &&
        a.range_id==b.range_id && a.kind==b.kind && a.side==b.side && a.stage==b.stage &&
        a.order_ticket==b.order_ticket && a.position_id==b.position_id &&
        a.position_ticket==b.position_ticket && a.entry_ticks==b.entry_ticks &&
        a.sl_ticks==b.sl_ticks && a.tp_ticks==b.tp_ticks && a.volume_steps==b.volume_steps;
}
void RRSetKnown(RRKnownOrder &o,const ulong ticket,const RRAction &a) {
 o.ticket=ticket;o.side=a.side;o.entry_ticks=a.entry_ticks;o.sl_ticks=a.sl_ticks;o.volume_steps=a.volume_steps;
}
void RRAfterProtection(RRRangeState &s) {
 s.phase=(s.retire.ticket!=0 || RCReverseSlot(s.cycle)!=0)?RR_ARM_REVERSE:RR_ACTIVE;
}

// Input is normalized by a separate verified outcome adapter. No positive
// report may be synthesized from a send acknowledgement, missing order, or
// a browser retry. Keep all state if effect is missing or inconsistent.
bool RRConfirm(RRRangeState &s,const RREffect &e) {
 if(!e.terminal_verified || s.pending.kind==RR_NONE || !RRActionEqual(s.pending,e.action))return false;
 const int kind=e.action.kind;
 const bool create=(kind==RR_PRIMARY || kind==RR_REVERSE || kind==RR_REENTRY);
 if(create) {
    if(e.created_order==0 || e.created_order==s.entry.ticket || e.created_order==s.opposite.ticket ||
       e.created_order==s.retire.ticket || e.created_order==s.cycle.active_order ||
       e.created_order==s.cycle.last_closed_order)return false;
 } else if(e.created_order!=0)return false;
 if(kind==RR_PRIMARY && s.phase==RR_NEW && s.entry.ticket==0) {
    RRSetKnown(s.entry,e.created_order,e.action);s.phase=RR_WAIT;
 } else if(kind==RR_INITIAL_SL && s.phase==RR_PROTECT) {
    s.live_sl=e.action.sl_ticks;RRAfterProtection(s);
 } else if(kind==RR_REVERSE && s.phase==RR_ARM_REVERSE && s.opposite.ticket==0 && s.retire.ticket==0) {
    RRSetKnown(s.opposite,e.created_order,e.action);s.phase=RR_ACTIVE;
 } else if(kind==RR_SUPPORT_SL && s.phase==RR_ACTIVE && e.action.stage>s.support_stage) {
    s.support_stage=e.action.stage;s.live_sl=e.action.sl_ticks;
 } else if(kind==RR_REENTRY && s.phase==RR_ARM_REENTRY && s.entry.ticket==0) {
    RRSetKnown(s.entry,e.created_order,e.action);s.phase=RR_WAIT;
 } else if(kind==RR_CANCEL && (s.phase==RR_ARM_REVERSE || s.phase==RR_DRAIN)) {
    if(e.action.order_ticket==s.retire.ticket && s.retire.ticket!=0)RRClearKnown(s.retire);
    else if(s.phase==RR_DRAIN && e.action.order_ticket==s.entry.ticket && s.entry.ticket!=0)RRClearKnown(s.entry);
    else if(s.phase==RR_DRAIN && e.action.order_ticket==s.opposite.ticket && s.opposite.ticket!=0)RRClearKnown(s.opposite);
    else return false;
    if(s.phase==RR_DRAIN && s.retire.ticket==0 && s.entry.ticket==0 && s.opposite.ticket==0)s.phase=RR_DONE;
    else if(s.phase==RR_ARM_REVERSE && RCReverseSlot(s.cycle)==0)s.phase=RR_ACTIVE;
 } else return false;
 RRClearAction(s.pending);
 return true;
}

// Accept one owned fill only after confirmed remainder, exact position/proof,
// and the preceding close transition. Concurrent/racing fills are not guessed.
// Internal normalized application; public legacy path must not consume two
// support alternatives. They need an independently observed surviving ticket.
bool RRObserveFillCore(RRRangeState &s,const RRSettings &cfg,const RRFilled &f) {
 if(!RRValid(s,cfg) || s.pending.kind!=RR_NONE ||
    (s.phase!=RR_WAIT && s.phase!=RR_ARM_REENTRY) || s.retire.ticket!=0 ||
    s.cycle.position_id!=0 || !f.independently_verified ||
    f.position_ticket==0 || !RCBoundedPositive(f.sl_ticks) || f.tp_ticks!=0 ||
    !RCValidSide(f.entry.side) || !RCBoundedPositive(f.entry.actual_entry_ticks))
    return false;
 RRKnownOrder owned,other;
 if(f.entry.executed_order==s.entry.ticket && s.entry.ticket!=0) {
    owned=s.entry;other=s.opposite;
 } else if(f.entry.executed_order==s.opposite.ticket && s.opposite.ticket!=0) {
    owned=s.opposite;other=s.entry;
 } else return false;
 if(f.entry.expected_order!=owned.ticket || f.entry.planned_steps!=owned.volume_steps ||
    f.entry.side!=owned.side || f.sl_ticks!=owned.sl_ticks || f.working_other_ticket!=other.ticket)return false;
 const long sl=f.entry.actual_entry_ticks-f.entry.side*cfg.initial_sl_ticks;
 if(!RCBoundedPositive(sl) || !RCApplyEntry(s.cycle,f.entry))return false;
 s.position_ticket=f.position_ticket;s.initial_sl=sl;s.live_sl=f.sl_ticks;s.live_tp=f.tp_ticks;
 s.support_stage=0;s.retire=other;RRClearKnown(s.entry);RRClearKnown(s.opposite);
 s.reentry_side=0;s.reentry_slot=0;s.reentry_ticks=0;
 if(sl==s.live_sl)RRAfterProtection(s);
 else s.phase=RR_PROTECT;
 return true;
}

bool RRHasSupportAlternatives(const RRRangeState &s) {
 return s.phase==RR_WAIT && s.reentry_slot>0 && s.entry.ticket!=0 && s.opposite.ticket!=0;
}

bool RRObserveFill(RRRangeState &s,const RRSettings &cfg,const RRFilled &f) {
 if(RRHasSupportAlternatives(s))return false;
 return RRObserveFillCore(s,cfg,f);
}

bool RRApplyDurableClose(RRRangeState &s,const RCCloseCheckpoint &close) {
 if(s.pending.kind!=RR_NONE || s.scope!=close.key.scope || close.key.scope==0 ||
    (s.phase!=RR_ACTIVE && s.phase!=RR_ARM_REVERSE && s.phase!=RR_PROTECT))return false;
 // No mixed transition after a racing fill/old cancel: preserve exact pending
 // objects for reconciliation rather than treating an incomplete rotation as closed.
 if(s.retire.ticket!=0 || s.entry.ticket!=0)return false;
 RCClosePlan next;
 if(RCCPApply(close,s.cycle,next)!=RCCP_APPLIED)return false;
 s.position_ticket=0;s.support_stage=0;s.initial_sl=0;s.live_sl=0;s.live_tp=0;
 if(next.cancel_remaining_range_orders)
    s.phase=(s.opposite.ticket!=0)?RR_DRAIN:RR_DONE;
 else if(next.reentry_slot!=0) {
    s.phase=RR_ARM_REENTRY;s.reentry_side=next.reentry_side;
    s.reentry_slot=next.reentry_slot;s.reentry_ticks=next.reentry_ticks;
 } else s.phase=(s.opposite.ticket!=0)?RR_WAIT:RR_DONE;
 return true;
}
#endif
