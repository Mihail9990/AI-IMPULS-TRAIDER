#ifndef RECOVERY_PRICE_STAGE_MQH
#define RECOVERY_PRICE_STAGE_MQH
// A diagnostic price hint only. No quote-time gates or position mutations.
bool RecoveryPricePositive(const double value) {
   return MathIsValidNumber(value) && value>0.0 && value<10000000.0;
}
int RecoveryPriceStage(const int side,const double entry,const double bid,const double ask,
                       const double first_advance,const double second_advance) {
   if((side!=1 && side!=-1) || !RecoveryPricePositive(entry) ||
      !RecoveryPricePositive(bid) || !RecoveryPricePositive(ask) || bid>ask ||
      !RecoveryPricePositive(first_advance) || !RecoveryPricePositive(second_advance) ||
      first_advance>=second_advance) return -1;
   const double quote=side==1 ? bid : ask;
   const double change=side*(quote-entry);
   // Limit the allowance to the binary64 subtraction error near the threshold.
   const double tolerance=MathMin(first_advance*1e-6,2.2204460492503131e-16*(entry+quote));
   if(change>=second_advance || second_advance-change<=tolerance) return 2;
   if(change>=first_advance || first_advance-change<=tolerance) return 1;
   return 0;
}
#endif
