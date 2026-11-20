#ifndef RECOVERY_PRICE_STAGE_MQH
#define RECOVERY_PRICE_STAGE_MQH
// A diagnostic price hint only. No quote-time gates or position mutations.
bool RecoveryPricePositive(const double value) {
   return MathIsValidNumber(value) && value>0.0 && value<10000000.0;
}
bool RecoveryPriceThresholds(const double first,const double second) {
   return RecoveryPricePositive(second) &&
          (first==0.0 || (RecoveryPricePositive(first) && first<second));
}
int RecoveryPriceStage(const int side,const double entry,const double bid,const double ask,
                       const double first_advance,const double second_advance) {
   if((side!=1 && side!=-1) || !RecoveryPricePositive(entry) ||
      !RecoveryPricePositive(bid) || !RecoveryPricePositive(ask) || bid>ask ||
      !RecoveryPriceThresholds(first_advance,second_advance)) return -1;
   const double quote=side==1 ? bid : ask;
   const double change=side*(quote-entry);
   // Limit the allowance to the binary64 subtraction error near the threshold.
   const double smallest=first_advance>0.0 ? first_advance : second_advance;
   const double tolerance=MathMin(smallest*1e-6,2.2204460492503131e-16*(entry+quote));
   if(change>=second_advance || second_advance-change<=tolerance) return 2;
   if(first_advance>0.0 && (change>=first_advance || first_advance-change<=tolerance)) return 1;
   return 0;
}
struct RecoveryPricePeak {
   int stage;
   double selected;
};
void RecoveryPriceReset(RecoveryPricePeak &peak) {
   peak.stage=-1; peak.selected=0.0;
}
bool RecoveryPriceAccumulate(RecoveryPricePeak &peak,const int side,const int stage,const double selected) {
   if((side!=1 && side!=-1) || stage<0 || stage>2 || !RecoveryPricePositive(selected)) return false;
   if(stage<peak.stage || (stage==peak.stage && side*(selected-peak.selected)<=0)) return false;
   peak.stage=stage; peak.selected=selected;
   return true;
}
bool RecoveryPriceReached(const RecoveryPricePeak &peak,const int next_stage) {
   return (next_stage==1 || next_stage==2) && peak.stage>=next_stage && RecoveryPricePositive(peak.selected);
}
#endif
