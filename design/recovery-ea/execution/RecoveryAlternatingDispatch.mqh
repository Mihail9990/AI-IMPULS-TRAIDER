#ifndef RECOVERY_ALTERNATING_DISPATCH_MQH
#define RECOVERY_ALTERNATING_DISPATCH_MQH
#include "RecoveryAlternatingPlan.mqh"
#include "RecoveryOpeningDispatch.mqh"

// Immediate call is permitted only with the exact state and action+intent key
// read back together from the single writer's durable store. Session retry is
// not authorization: existing dispatch checks the persistent intent journal.
RecoveryPairSendState RecoverySendCommittedAlternating(const RecoveryAlternatingState &persisted,
    const ulong outbox_revision,const RRAction &outbox_action,const RecoveryIntentKey &key,
    const double tick,const double step,RecoveryDispatchResult &out) {
 ZeroMemory(out);
 RRRangeState shadow;
 if(!RecoveryAlternatingDispatchView(persisted,outbox_revision,outbox_action,shadow))
    return RPS_NOT_STARTED;
 return RecoveryContinueOpeningPair(persisted.range,persisted.opening,key,tick,step,out);
}
#endif
