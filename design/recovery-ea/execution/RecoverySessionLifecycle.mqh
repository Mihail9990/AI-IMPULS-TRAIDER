#ifndef RECOVERY_SESSION_LIFECYCLE_MQH
#define RECOVERY_SESSION_LIFECYCLE_MQH

// Identity values and the immutable binding description are supplied by the
// caller. Period is a reinit discriminator, NOT included in the trading scope.
struct RecoverySessionContext {
 long chart,login,magic;
 string server,symbol,binding;
 int period;
};
struct RecoverySessionLifecycle {
 RecoverySessionContext context;
 string epoch;
 bool active,period_reinit;
};
enum RecoverySessionInitResult {
 RSL_INACTIVE=0, RSL_NEW=1, RSL_RESUMED=2
};

bool RecoverySessionContextValid(const RecoverySessionContext &c) {
 return c.chart!=0 && c.login>0 && c.magic>0 && c.server!="" &&
        c.symbol!="" && c.binding!="" && c.period>0;
}
bool RecoverySessionScopeEqual(const RecoverySessionContext &a,const RecoverySessionContext &b) {
 return a.chart==b.chart && a.login==b.login && a.magic==b.magic &&
        a.server==b.server && a.symbol==b.symbol && a.binding==b.binding;
}
void RecoverySessionReset(RecoverySessionLifecycle &s) {
 s.active=false;s.period_reinit=false;s.epoch="";
 s.context.chart=0;s.context.login=0;s.context.magic=0;s.context.period=0;
 s.context.server="";s.context.symbol="";s.context.binding="";
}
void RecoverySessionDeinit(RecoverySessionLifecycle &s,const bool chart_change) {
 if(chart_change && s.active && s.epoch!="") {
    s.active=false;s.period_reinit=true;return;
 }
 RecoverySessionReset(s);
}

// 'setup_ok' means setup (including the newly armed timer) actually succeeded.
// Caller keeps payload only on RESUMED; both other outcomes must clear payload.
// A fresh opaque epoch is caller generated and must never be reused.
RecoverySessionInitResult RecoverySessionInit(RecoverySessionLifecycle &s,
    const RecoverySessionContext &observed,const bool setup_ok,const string fresh_epoch) {
 if(!setup_ok || !RecoverySessionContextValid(observed)) {
    RecoverySessionReset(s);return RSL_INACTIVE;
 }
 if(!s.active && s.period_reinit && s.epoch!="" &&
    RecoverySessionScopeEqual(s.context,observed) && s.context.period!=observed.period) {
    s.context=observed;s.active=true;s.period_reinit=false;return RSL_RESUMED;
 }
 const bool epoch_ok=fresh_epoch!="" && fresh_epoch!=s.epoch;
 RecoverySessionReset(s);
 if(!epoch_ok)return RSL_INACTIVE;
 s.context=observed;s.epoch=fresh_epoch;s.active=true;return RSL_NEW;
}

// Use before consuming messages / executing effects; no clock/freshness gate.
bool RecoverySessionCurrent(const RecoverySessionLifecycle &s,const RecoverySessionContext &live) {
 return s.active && !s.period_reinit && s.epoch!="" &&
        RecoverySessionContextValid(live) && RecoverySessionScopeEqual(s.context,live) &&
        s.context.period==live.period;
}
#endif
