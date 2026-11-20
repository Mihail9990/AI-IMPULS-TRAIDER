#ifndef RECOVERY_SESSION_LIFECYCLE_MQH
#define RECOVERY_SESSION_LIFECYCLE_MQH

// Identity values and the immutable binding description are supplied by the
// caller. The working chart uses a fixed timeframe.
struct RecoverySessionContext {
 long chart,login,magic;
 string server,symbol,binding;
};
struct RecoverySessionLifecycle {
 RecoverySessionContext context;
 string epoch;
 bool active;
};
enum RecoverySessionInitResult {
 RSL_INACTIVE=0, RSL_NEW=1
};

bool RecoverySessionContextValid(const RecoverySessionContext &c) {
 return c.chart!=0 && c.login>0 && c.magic>0 && c.server!="" &&
        c.symbol!="" && c.binding!="";
}
bool RecoverySessionScopeEqual(const RecoverySessionContext &a,const RecoverySessionContext &b) {
 return a.chart==b.chart && a.login==b.login && a.magic==b.magic &&
        a.server==b.server && a.symbol==b.symbol && a.binding==b.binding;
}
void RecoverySessionReset(RecoverySessionLifecycle &s) {
 s.active=false;s.epoch="";
 s.context.chart=0;s.context.login=0;s.context.magic=0;
 s.context.server="";s.context.symbol="";s.context.binding="";
}

// The caller clears its trading payload and starts a new queue on each
// initialization. There is no continuation from a previous initialization.
RecoverySessionInitResult RecoverySessionInit(RecoverySessionLifecycle &s,
    const RecoverySessionContext &observed,const bool setup_ok,const string fresh_epoch) {
 RecoverySessionReset(s);
 if(!setup_ok || !RecoverySessionContextValid(observed) || fresh_epoch=="")
    return RSL_INACTIVE;
 s.context=observed;s.epoch=fresh_epoch;s.active=true;return RSL_NEW;
}

// Use before consuming messages / executing effects; no timeframe or clock gate.
bool RecoverySessionCurrent(const RecoverySessionLifecycle &s,const RecoverySessionContext &live) {
 return s.active && s.epoch!="" && RecoverySessionContextValid(live) &&
        RecoverySessionScopeEqual(s.context,live);
}
#endif
