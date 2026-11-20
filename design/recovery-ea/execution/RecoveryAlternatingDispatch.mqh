#ifndef RECOVERY_ALTERNATING_DISPATCH_MQH
#define RECOVERY_ALTERNATING_DISPATCH_MQH
#include "RecoveryAlternatingPlan.mqh"
#include "RecoveryOpeningDispatch.mqh"

// Immediate call is permitted only with the exact state and action+intent key
// read together from the single writer's current session state. Session retry is
// not authorization: existing dispatch checks the in-memory intent journal.
RecoveryPairSendState RecoverySendCommittedAlternating(const RecoveryAlternatingState &committed,
    const ulong outbox_revision,const RRAction &outbox_action,const RecoveryIntentKey &key,
    const double tick,const double step,RecoveryDispatchResult &out) {
 ZeroMemory(out);
 RRRangeState shadow;
 if(!RecoveryAlternatingDispatchView(committed,outbox_revision,outbox_action,shadow))
    return RPS_NOT_STARTED;
 return RecoveryContinueOpeningPair(committed.range,committed.opening,key,tick,step,out);
}
#endif
