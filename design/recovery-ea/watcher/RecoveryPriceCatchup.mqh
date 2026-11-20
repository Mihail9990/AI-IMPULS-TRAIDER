#ifndef RECOVERY_PRICE_CATCHUP_MQH
#define RECOVERY_PRICE_CATCHUP_MQH
#include "RecoveryPriceStage.mqh"

const int RECOVERY_PRICE_TICK_CAP=256;

bool RecoveryPriceAnchor(const long opened,const long latest,long &anchor,ulong &cursor) {
   if(opened<=0 || latest<=0) return false;
   anchor=opened>latest ? opened : latest;
   // Subscription activation and position creation share broker milliseconds,
   // not a globally ordered tick ID. Exclude the whole boundary millisecond.
   cursor=(ulong)anchor+1;
   return true;
}

bool RecoveryPriceTickRecord(const long tick_msc,const long anchor,const long upper) {
   return tick_msc>anchor && tick_msc<=upper;
}

// Repeat the last timestamp inclusively. Max/min folding is idempotent and can
// safely consume a newly exposed tick with that same timestamp. A page saturated
// at one millisecond can never force movement past that millisecond.
bool RecoveryPriceCatchupFold(const MqlTick &ticks[],const int n,const bool synchronized,
                             const int side,const double entry,const double first,const double second,
                             const long anchor,const long upper,ulong &cursor,RecoveryPricePeak &peak) {
   if(n<1 || n>RECOVERY_PRICE_TICK_CAP || ArraySize(ticks)<n || anchor<=0 ||
      upper<=anchor || cursor<=(ulong)anchor || cursor>(ulong)upper) return false;
   long prior=0;
   for(int i=0;i<n;i++) {
      if(ticks[i].time_msc<=anchor || (ulong)ticks[i].time_msc<cursor || ticks[i].time_msc<prior) return false;
      prior=ticks[i].time_msc;
   }
   ulong latest=cursor;
   bool observed=false;
   for(int i=0;i<n;i++) {
      if(!RecoveryPriceTickRecord(ticks[i].time_msc,anchor,upper)) break;
      const int stage=RecoveryPriceStage(side,entry,ticks[i].bid,ticks[i].ask,first,second);
      if(stage>=0) {
         observed=true;
         RecoveryPriceAccumulate(peak,side,stage,side==1 ? ticks[i].bid : ticks[i].ask);
      }
      latest=(ulong)ticks[i].time_msc;
   }
   // Partial synchronization can contribute positive facts but never discards a
   // range that might be filled by a later sync. A live fallback uses no cursor.
   if(synchronized && latest>cursor) cursor=latest;
   return observed;
}
#endif
