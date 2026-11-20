#ifndef RECOVERY_PROOF_RETRY_MQH
#define RECOVERY_PROOF_RETRY_MQH
// Back off only after an immutable verifier response was unusable or a
// request failed. A normal outstanding response keeps its original nonce.
struct RecoveryProofRetry {ulong delay_ms,after_ms;};
bool RecoveryProofRetryDue(RecoveryProofRetry &retry,const ulong now) {
 if(retry.after_ms==0) {
    if(retry.delay_ms==0)retry.delay_ms=1000;
    else if(retry.delay_ms<256000)retry.delay_ms*=2;
    retry.after_ms=now+retry.delay_ms;
    return false;
 }
 if(now<retry.after_ms)return false;
 retry.after_ms=0;return true;
}

#endif
