#ifndef RECOVERY_SUPPORT_GAP_MQH
#define RECOVERY_SUPPORT_GAP_MQH
#include "RecoveryOpeningPair.mqh"

// Separate event latch for a fixed opposite filling after a confirmed support
// move and before any same-side reentry was reserved. No broker requests here.
struct RRSupportGap {
 bool reserved,have_close,have_fill;
 ulong scope, position_ticket;
 RCRange before;
 int support_stage;
 long support_level,tp,initial_level;
 RRKnownOrder opposite;
 RCCloseCheckpoint close;
 RRFilled fill;
};
enum RRSupportGapResult { RR_GAP_UNRESOLVED=0, RR_GAP_NEXT_PAIR=1,
                          RR_GAP_PROTECTION_ONLY=2, RR_GAP_FINAL_PROTECTED=3 };
bool RRSupportGapBound(const RRRangeState &s,const RRSupportGap &g) {
 return g.reserved && g.scope>0 && g.scope==s.scope && g.position_ticket==s.position_ticket &&
        RCCPSameRange(g.before,s.cycle) && s.phase==RR_ACTIVE &&
        s.pending.kind==RR_NONE && s.support_stage>=1 && s.support_stage<=2 &&
        s.support_stage==g.support_stage && s.live_sl==g.support_level && s.live_tp==g.tp &&
        s.initial_sl==g.initial_level && s.entry.ticket==0 && s.retire.ticket==0&&s.reentry_slot==0 &&
        RRKnownEqual(s.opposite,g.opposite);
}
bool RRStartSupportGap(const RRRangeState &s,RRSupportGap &out) {
 if(out.reserved || s.scope==0 || s.position_ticket==0 || s.cycle.position_id==0 ||
    s.cycle.active_order==0 || !RCValidSide(s.cycle.side) ||
    s.phase!=RR_ACTIVE || s.pending.kind!=RR_NONE || s.support_stage<1 || s.support_stage>2 ||
    s.entry.ticket!=0 || s.retire.ticket!=0 || s.reentry_slot!=0 ||
    !RCBoundedPositive(s.live_sl) || !RCBoundedPositive(s.initial_sl) ||
    (s.live_sl-s.initial_sl)*s.cycle.side<=0 || s.opposite.ticket==0 ||
    s.opposite.side!=-s.cycle.side || s.opposite.entry_ticks!=s.initial_sl ||
    !RCBoundedPositive(s.opposite.sl_ticks) || !RCBoundedPositive(s.opposite.volume_steps))return false;
 out.reserved=true;out.have_close=false;out.have_fill=false;out.scope=s.scope;
 out.before=s.cycle;out.support_stage=s.support_stage;out.support_level=s.live_sl;
 out.tp=s.live_tp;out.initial_level=s.initial_sl;out.position_ticket=s.position_ticket;out.opposite=s.opposite;
 return true;
}
bool RRStageSupportGapClose(const RRRangeState &s,RRSupportGap &g,const RCCloseCheckpoint &close) {
 if(!RRSupportGapBound(s,g) || close.key.scope!=g.scope || !RCCPSameRange(close.before,g.before) ||
    RCClassifyClose(g.before,close.proof)!=RC_SUPPORT_SL ||
    close.proof.exit_stop_ticks!=g.support_level)return false;
 RCRange ignored;RCClosePlan plan;
 if(!RCCPDerive(close,ignored,plan) || plan.classification!=RC_SUPPORT_SL)return false;
 if(g.have_close)return RCCloseKeysEqual(close.key,g.close.key) && close.reentry_distance==g.close.reentry_distance;
 g.close=close;g.have_close=true;
 return true;
}
bool RRStageSupportGapFill(const RRRangeState &s,RRSupportGap &g,const RRKnownOrder &historical,
                          const RRFilled &fill) {
 if(!RRSupportGapBound(s,g) || !RRKnownEqual(historical,g.opposite) ||
    !fill.independently_verified || !fill.entry.independently_verified ||
    !fill.entry.history_complete || !fill.entry.remainder_reconciled ||
    fill.entry.generation!=g.before.generation || fill.entry.range_id!=g.before.range_id ||
    fill.entry.expected_order!=g.opposite.ticket || fill.entry.executed_order!=g.opposite.ticket ||
    fill.entry.position_id==0 || fill.entry.position_id==g.before.position_id ||
    fill.position_ticket==0 || fill.position_ticket==g.position_ticket ||
    fill.entry.side!=g.opposite.side || !RCBoundedPositive(fill.entry.actual_entry_ticks) ||
    fill.entry.planned_steps!=g.opposite.volume_steps || fill.entry.filled_steps!=g.opposite.volume_steps ||
    fill.entry.active_steps!=fill.entry.filled_steps || fill.sl_ticks!=g.opposite.sl_ticks ||
    fill.tp_ticks!=0 || fill.working_other_ticket!=0)return false;
 if(g.have_fill) return g.fill.entry.position_id==fill.entry.position_id &&
   g.fill.position_ticket==fill.position_ticket && g.fill.entry.actual_entry_ticks==fill.entry.actual_entry_ticks;
 g.have_fill=true;g.fill=fill;
 return true;
}

// Close-first may follow the ordinary support route when a new complete,
// independently bound read proves the fixed order is STILL intact. No absence
// inference, no timer, and no acceptance of a pre-close inventory replay.
bool RRReleaseSupportGapToReentry(RRRangeState &s,RRSupportGap &g,const RRKnownOrder &working,
                                const bool independently_proven_after_close) {
 if(!RRSupportGapBound(s,g) || !g.have_close || g.have_fill ||
    !independently_proven_after_close || !RRKnownEqual(g.opposite,working))return false;
 RRRangeState n=s;
 if(!RRApplyDurableClose(n,g.close)||n.phase!=RR_ARM_REENTRY)return false;
 s=n;g.reserved=false;
 return true;
}

RRSupportGapResult RRAdvanceSupportGap(RRRangeState &s,const RRSettings &cfg,RRSupportGap &g,
                                     RROpeningPair &opening,RRAction &protection_only) {
 RRClearAction(protection_only);
 if(!RRSupportGapBound(s,g)||!RRValid(s,cfg)||!g.have_close||!g.have_fill||opening.reserved)
    return RR_GAP_UNRESOLVED;
 RRRangeState n=s;
 // Both facts retained until the whole advance succeeds. No call to RRStage
 // while RR_ARM_REENTRY: consume the already filled exact fixed opposite.
 if(!RRApplyDurableClose(n,g.close) || n.phase!=RR_ARM_REENTRY ||
    !RRObserveFill(n,cfg,g.fill) || n.retire.ticket!=0 || n.reentry_slot!=0)
    return RR_GAP_UNRESOLVED;
 const bool final=RCReverseSlot(n.cycle)==0;
 if(final) {
    if(n.phase==RR_PROTECT) {
       RRAction a;
       if(!RRStage(n,cfg,0,0,a)||a.kind!=RR_INITIAL_SL)return RR_GAP_UNRESOLVED;
       s=n;g.reserved=false;protection_only=a;
       return RR_GAP_PROTECTION_ONLY;
    }
    if(n.phase!=RR_ACTIVE)return RR_GAP_UNRESOLVED;
    s=n;g.reserved=false;
    return RR_GAP_FINAL_PROTECTED;
 }
 RROpeningPair next;
 next.reserved=false;
 if(!RRStageOpeningPair(n,cfg,next))return RR_GAP_UNRESOLVED;
 s=n;g.reserved=false;opening=next;
 return RR_GAP_NEXT_PAIR;
}
#endif
