#ifndef RECOVERY_WORKING_REVERSE_MQH
#define RECOVERY_WORKING_REVERSE_MQH
#include "RecoveryExactOrderEffect.mqh"
#include "RecoverySessionIntentKey.mqh"
#include "RecoveryIntentJournal.mqh"
#include "RecoverySessionLifecycle.mqh"
#include "../cycle/RecoveryOpeningPair.mqh"

enum RecoveryWorkingReverseState {RWR_IDLE=0,RWR_REQUEST,RWR_READ,RWR_DONE,RWR_UNRESOLVED};
enum RecoveryWorkingReverseResult {RWR_WAIT=0,RWR_PUBLISHED,RWR_ACTIVE,RWR_STOPPED};
struct RecoveryWorkingReverse {
 RecoveryWorkingReverseState state;
 RecoveryExactEffectBinding bind;
 RecoveryIntentKey key;
 string publisher_tag;
 ulong position_id,position_ticket,entry_order;
};

bool RecoveryWorkingReverseContext(const RecoverySessionLifecycle &session,
    const RecoverySessionContext &live,const RecoverySessionIntentScope &scope,
    const RecoveryExactReportScope &request) {
 const RecoveryInventoryScope q=request.binding;
 return RecoverySessionCurrent(session,live) && RecoveryInvScopeOK(q) &&
        session.epoch==scope.executor_epoch && q.executor_epoch==session.epoch &&
        q.verifier_chart!=live.chart && q.login==live.login && q.server==live.server &&
        q.symbol==live.symbol && q.magic==live.magic &&
        q.login==scope.login && q.server==scope.server && q.symbol==scope.symbol &&
        q.magic==scope.magic && q.generation>0 && (ulong)q.generation==scope.generation &&
        request.order_ticket>0;
}

bool RecoveryWorkingReverseRoute(const RecoveryWorkingReverse &probe,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RRRangeState &range,
    const RROpeningPair &pair,RRRangeState &view) {
 if(pair.sl_required || pair.sl.kind!=RR_NONE ||
    !RecoveryWorkingReverseContext(session,live,scope,probe.bind.request) ||
    probe.position_id!=range.cycle.position_id || probe.position_ticket!=range.position_ticket ||
    probe.entry_order!=range.cycle.active_order || scope.scope!=range.scope ||
    !RRActionEqual(probe.bind.action,pair.reverse) ||
    !RROpeningRequestState(range,pair,pair.reverse,view) ||
    !RecoveryExactEffectBound(view,probe.bind))return false;
 RecoveryIntentKey expected;
 string ticket;
 return RecoveryBuildSessionIntentKey(scope,pair.reverse,probe.bind.price_tick,probe.bind.volume_step,expected) &&
        RecoveryIntentKeysMatch(expected,probe.key) &&
        RecoveryIntentLookup(probe.key,ticket)==RI_TICKET &&
        ticket==(string)probe.bind.request.order_ticket;
}

// Begin with the exact key returned from dispatch and the independently
// allocated verifier challenge for its recorded ticket; never use a quote to
// find an order. Allocating this RAM object does not initiate verifier I/O.
bool RecoveryBeginWorkingReverse(RecoveryWorkingReverse &probe,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,const RRRangeState &range,
    const RROpeningPair &pair,const RecoveryIntentKey &dispatch_key,
    const RecoveryExactReportScope &request,const ulong verify_after,const string publisher_tag,
    const double price_tick,const double volume_step) {
 if(probe.state!=RWR_IDLE || !RecoveryInvHex(publisher_tag) ||
    publisher_tag==request.binding.nonce || verify_after>=(ulong)LONG_MAX)return false;
 RecoveryWorkingReverse next;
 next.state=RWR_REQUEST;next.bind.request=request;next.bind.action=pair.reverse;
 next.bind.verify_after=verify_after;next.bind.price_tick=price_tick;next.bind.volume_step=volume_step;
 next.key=dispatch_key;next.publisher_tag=publisher_tag;
 next.position_id=range.cycle.position_id;next.position_ticket=range.position_ticket;
 next.entry_order=range.cycle.active_order;
 RRRangeState view;
 if(!RecoveryWorkingReverseRoute(next,session,live,scope,range,pair,view))return false;
 probe=next;
 return true;
}

RecoveryWorkingReverseResult RecoveryStopWorkingReverse(RecoveryWorkingReverse &probe) {
 probe.state=RWR_UNRESOLVED;
 return RWR_STOPPED;
}

// One file publication and one verified state transition at most. The
// ordinary fill pump remains the independent path for an already-filled order.
RecoveryWorkingReverseResult RecoveryStepWorkingReverse(RecoveryWorkingReverse &probe,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoverySessionIntentScope &scope,RRRangeState &range,RROpeningPair &pair) {
 if(probe.state!=RWR_REQUEST && probe.state!=RWR_READ)return RWR_STOPPED;
 RRRangeState view;
 if(!RecoveryWorkingReverseRoute(probe,session,live,scope,range,pair,view))
    return RecoveryStopWorkingReverse(probe);
 if(probe.state==RWR_REQUEST) {
    probe.state=RWR_READ;
    if(!RecoveryExactReportRequest(probe.bind.request,probe.publisher_tag))
       return RecoveryStopWorkingReverse(probe);
    return RWR_PUBLISHED;
 }
 RecoveryExactReport report;
 if(!RecoveryExactReportRead(probe.bind.request,report))return RWR_WAIT;
 RREffect effect;
 if(!RecoveryExactEffectFold(report,probe.bind,effect) ||
    !RecoveryWorkingReverseRoute(probe,session,live,scope,range,pair,view))
    return RecoveryStopWorkingReverse(probe);
 RRRangeState next=range;
 RROpeningPair opened=pair;
 if(RRConfirmOpeningPair(next,opened,effect)!=RR_PAIR_CONFIRMED || next.phase!=RR_ACTIVE)
    return RecoveryStopWorkingReverse(probe);
 range=next;pair=opened;probe.state=RWR_DONE;
 return RWR_ACTIVE;
}
#endif
