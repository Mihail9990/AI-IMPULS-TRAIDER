#include <cmath>
#include <cassert>
#include <limits>
bool MathIsValidNumber(double v){return std::isfinite(v);}
double MathAbs(double v){return std::abs(v);}
double MathMax(double a,double b){return std::fmax(a,b);}
#include "../../mt5/native/RecoveryPolicy.mqh"
int main(){
 double v=0;bool buy=false;
 assert(DecideBasket(2,true,true,1,0,0,0,1,false)==BASKET_INCREASE);
 assert(NextSteps(2,BASKET_INCREASE)==3);
 for(int n=3;n<10;n++){
   assert(DecideBasket(n,true,true,n,0,0,0,n,false)==BASKET_INCREASE);
   assert(NextSteps(n,BASKET_INCREASE)==n+1);
 }
 assert(DecideBasket(10,true,true,10,0,0,0,10,false)==BASKET_MANUAL_LIMIT);
 assert(NextSteps(10,BASKET_MANUAL_LIMIT)==0);
 assert(DecideBasket(5,true,true,0,5,0,0,5,false)==BASKET_KEEP);
 assert(NextSteps(5,BASKET_KEEP)==5);
 assert(DecideBasket(5,true,true,2,0,3,0,5,false)==BASKET_KEEP);
 assert(DecideBasket(5,true,true,0,2,3,0,5,false)==BASKET_KEEP);
 assert(DecideBasket(5,true,true,5,0,0,0,5,true)==BASKET_NEEDS_DECISION);
 assert(DecideBasket(5,false,true,5,0,0,0,5,false)==BASKET_WAIT);
 assert(DecideBasket(5,true,false,1,0,0,0,5,false)==BASKET_WAIT);
 assert(DecideBasket(5,true,true,0,0,5,0,5,false)==BASKET_FINISHED);
 assert(DecideBasket(5,true,true,0,0,4,1,5,false)==BASKET_NEEDS_DECISION);
 assert(ApprovedTP(3,0)==.30 && ApprovedTP(7,6)==4.90);
 for(int n=3;n<=7;n++)for(int i=0;i<n;i++)assert(ApprovedTP(n,i)>0);
 for(int n=2;n<=10;n++)for(int i=0;i<n;i++)assert(ApprovedTP(n,i)>0);
 assert(ApprovedTP(8,7)==5.60 && ApprovedTP(9,8)==6.90 && ApprovedTP(10,9)==8.20);
 assert(SupportCandidate(true,100,100.25,98.5,v) && v==99.75);
 assert(SupportCandidate(true,100,100.40,99.75,v) && v==100);
 assert(!SupportCandidate(true,100,100.24,98.5,v));
 assert(!SupportCandidate(true,100,100.40,100.1,v));
 assert(SupportCandidate(false,100,99.75,101.5,v) && v==100.25);
 assert(SupportCandidate(false,100,99.60,100.25,v) && v==100);
 assert(!SupportCandidate(false,100,99.76,101.5,v));
 assert(!SupportCandidate(false,100,99.60,99.9,v));
 assert(SupportCandidate(true,100,101,98,v,.5,.8,-.4,.2) && v==100.2);
 assert(SupportCandidate(false,100,99,102,v,.5,.8,-.4,.2) && v==99.8);
 assert(!SupportCandidate(true,100,100.4,98,v,.5,.8,-.4,.2));
 assert(!SupportCandidate(true,100,101,98,v,.8,.5,-.4,.2));
 assert(SupportCandidate(true,100,101,98,v) && v==100); // skipped first tick -> second level
 assert(!SupportConfirmed(98.5,99.75,false,.01));
 assert(SupportConfirmed(98.5,99.75,true,.01));
 assert(!SupportConfirmed(98.5,98.5,true,.01));
 assert(RecoveryBalance(10,3,v)&&v==7);
 assert(RecoveryBalance(10,-3,v)&&v==13);
 assert(RecoveryBalance(10,12,v)&&v==0);
 assert(EffectiveRecovery(10,-.2,-5,v)&&std::abs(v-15.2)<1e-9);
 assert(RecoveryBalance(10,-5.5,v)&&v==15.5); // actual replaces estimate
 assert(!RequiredGross(0,0,v));
 assert(!RequiredGross(-1,0,v));
 assert(RequiredGross(5,.2,v)&&v==5.2);
 assert(ProfitableTarget(true,100,100.1)); // earlier than TP1 is valid
 assert(!ProfitableTarget(true,100,100));
 assert(!ProfitableTarget(false,100,101));
 assert(CandleTrigger(100,101,.7,false,v,buy)&&buy&&v==101.7);
 assert(CandleTrigger(101,100,.7,false,v,buy)&&!buy&&v==99.3);
 assert(!CandleTrigger(100,100,.7,false,v,buy));
 assert(!CandleTrigger(100,101,.7,true,v,buy));
 // Durable reached-stage replay: retrace does not lose the crossing;
 // current quote and a more favorable manual SL still gate the proposal.
 assert(SupportAtStage(true,100,100.10,98,2,v) && v==100);
 assert(!SupportAtStage(true,100,99.90,98,2,v));
 assert(!SupportAtStage(true,100,100.10,100.05,2,v));
 assert(SupportAtStage(false,100,99.90,102,2,v) && v==100);
 assert(!SupportAtStage(false,100,100.10,102,2,v));
 assert(!SupportAtStage(true,100,101,98,0,v));
 double nan=std::numeric_limits<double>::quiet_NaN();
 assert(!SupportCandidate(true,100,nan,98,v));
 assert(!RecoveryBalance(1,nan,v));
 assert(!RequiredGross(nan,0,v));
 assert(!ProfitableTarget(true,100,nan));
}
