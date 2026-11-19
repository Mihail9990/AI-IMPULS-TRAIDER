#include <cmath>
#include <string>
#include <cassert>
#include <iostream>
using string=std::string;
bool MathIsValidNumber(double x){return std::isfinite(x);}
double MathAbs(double x){return std::abs(x);}
double MathRound(double x){return std::round(x);}
#include "../RecoveryPlan.mqh"
int main(){
 RScope scope{12,"TEST","XAUUSD.s"};
 RMarket m{.01,.01,.01,100,4000,4000.2,.1,.05,true,true};
 RPosition p{scope,88,99,0,R_BUY,4000.3,.10,3999.5,4005,true,false};
 RPlan a{};int checks=0;
 #define CHECK(x) do{assert(x);++checks;}while(0)
 CHECK(RMakeManualPlan(p,scope,m,88,99,1,.7,.10,a));
 CHECK(!a.needs_sl&&a.required_sl==p.sl&&a.reverse_price==p.sl&&a.original_tp==p.tp);
 CHECK(RPlanStillMatches(a,p,scope,m));CHECK(RProtectionConfirmed(a,p,scope,m));
 auto changed=p;changed.sl-=.01;CHECK(!RPlanStillMatches(a,changed,scope,m));
 changed=p;changed.tp+=.01;CHECK(!RPlanStillMatches(a,changed,scope,m));
 changed=p;changed.volume=.09;CHECK(!RPlanStillMatches(a,changed,scope,m));
 auto other=scope;other.login=13;changed=p;changed.scope=other;CHECK(!RPlanStillMatches(a,changed,other,m));
 CHECK(!RMakeManualPlan(p,scope,m,88,99,2,.7,.1,a));CHECK(!a.valid);
 CHECK(!RMakeManualPlan(p,scope,m,87,99,1,.7,.1,a));
 changed=p;changed.magic=1;CHECK(RMakeManualPlan(changed,scope,m,88,99,1,.7,.1,a));
 changed=p;changed.adopted=true;CHECK(!RMakeManualPlan(changed,scope,m,88,99,1,.7,.1,a));
 changed=p;changed.sl=3999.505;CHECK(!RMakeManualPlan(changed,scope,m,88,99,1,.7,.1,a));
 changed=p;changed.sl=0;CHECK(RMakeManualPlan(changed,scope,m,88,99,1,.7,.1,a));
 CHECK(a.needs_sl&&a.required_sl==changed.entry-.7&&a.original_tp==changed.tp);
 CHECK(!RProtectionConfirmed(a,changed,scope,m));changed.sl=a.required_sl;CHECK(RProtectionConfirmed(a,changed,scope,m));
 auto before=p;before.sl=0;CHECK(!RMakeManualPlan(before,scope,m,88,99,1,.705,.1,a));
 // Existing SL is not recomputed from distance, even when a trailed profit SL.
 changed=p;changed.entry=3998;CHECK(RMakeManualPlan(changed,scope,m,88,99,1,99,.1,a));CHECK(a.required_sl==p.sl);
 changed=p;changed.side=R_SELL;changed.sl=4001;changed.tp=3990;CHECK(RMakeManualPlan(changed,scope,m,88,99,1,.7,.1,a));CHECK(a.reverse_side==R_BUY&&a.reverse_price==4001);
 RPending o{scope,123,R_BUY,4001,.1,a.reverse_sl,0,true,true,true};CHECK(RReverseConfirmed(a,o,scope,m));o.price+=.01;CHECK(!RReverseConfirmed(a,o,scope,m));o.price=4001;o.sl=0;CHECK(!RReverseConfirmed(a,o,scope,m));o.sl=a.reverse_sl;o.is_plain_stop=false;CHECK(!RReverseConfirmed(a,o,scope,m));
 CHECK(!RTriggerGeometry(R_SELL,m.bid,m));CHECK(!RTriggerGeometry(R_BUY,m.ask,m));
 CHECK(!RVolume(.015,m));CHECK(!RVolume(0,m));
 auto broken=m;broken.hedging=false;CHECK(!RMakeManualPlan(p,scope,broken,88,99,1,.7,.1,a));broken=m;broken.snapshot_complete=false;CHECK(!RMakeManualPlan(p,scope,broken,88,99,1,.7,.1,a));
 CHECK(RCanReserve(R_IDLE,true,true));CHECK(!RCanReserve(R_IDLE,true,false));
 for(int phase=R_RESERVED;phase<=R_CONFIRMED;++phase)CHECK(!RCanReserve(phase,true,true));
 CHECK(RAfterAttempt(false,false,true)==R_UNKNOWN);CHECK(RAfterAttempt(true,true,false)==R_REJECTED);CHECK(RAfterAttempt(true,false,true)==R_CONFIRMED);CHECK(RAfterAttempt(true,false,false)==R_UNKNOWN);
 std::cout<<checks<<" planner assertions PASS\n";
}
