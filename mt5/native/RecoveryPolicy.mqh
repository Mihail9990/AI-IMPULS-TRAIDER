#ifndef AI_IMPULS_RECOVERY_POLICY
#define AI_IMPULS_RECOVERY_POLICY
// Pure policy: no terminal, file, clock, account or trading calls.
// Also compiled verbatim by tests/native/policy.cpp (math compatibility shim).
enum BasketOutcome {
   BASKET_WAIT=0, BASKET_INCREASE=1, BASKET_KEEP=2,
   BASKET_MANUAL_LIMIT=3, BASKET_NEEDS_DECISION=4, BASKET_FINISHED=5
};

bool PolicyPositive(const double v) { return MathIsValidNumber(v) && v>0; }
bool PolicyNonnegative(const double v) { return MathIsValidNumber(v) && v>=0; }

// steps=2 is total 0.02: initial single entry or two recovery tickets.
// The caller tracks that distinction and supplies expected_positions.
// Full initial SL is a basket event; the caller must persist the basket ID
// together with this transition. A partial fill is NOT a complete basket.
BasketOutcome DecideBasket(const int steps,const bool all_entries_terminal,
                          const bool all_positions_closed,const int initial_sl,
                          const int support_sl,const int tp,const int other,
                          const int expected_positions,const bool partial_entry) {
   if(steps<2 || steps>10 || expected_positions<1 || initial_sl<0 ||
      support_sl<0 || tp<0 || other<0) return BASKET_NEEDS_DECISION;
   if(!all_entries_terminal || !all_positions_closed) return BASKET_WAIT;
   if(partial_entry || initial_sl+support_sl+tp+other!=expected_positions || other>0)
      return BASKET_NEEDS_DECISION;
   if(initial_sl==expected_positions)
      return steps==10 ? BASKET_MANUAL_LIMIT : BASKET_INCREASE;
   // Owner-approved: partial TP then original SL keeps the basket volume.
   if(initial_sl>0 && tp>0) return BASKET_KEEP;
   if(initial_sl>0 && support_sl>0) return BASKET_KEEP;
   if(support_sl>0) return BASKET_KEEP;
   return tp==expected_positions ? BASKET_FINISHED : BASKET_NEEDS_DECISION;
}
int NextSteps(const int steps,const BasketOutcome outcome) {
   if(steps<2 || steps>10) return 0;
   if(outcome==BASKET_INCREASE && steps<10) return steps+1;
   if(outcome==BASKET_KEEP) return steps;
   return 0; // No implicit reset, resume or profile for unresolved cases.
}

// Only the approved defaults. User settings are validated separately and
// are not overwritten by these defaults. Profiles 0.02--0.10 are owner-approved.
double ApprovedTP(const int steps,const int index) {
   if(index<0 || index>=steps) return 0;
   if(steps==2) { double p[2]={0.30,0.70}; return p[index]; }
   if(steps==3) { double p[3]={0.30,0.70,1.00}; return p[index]; }
   if(steps==4) { double p[4]={0.50,1.00,1.50,2.00}; return p[index]; }
   if(steps==5) { double p[5]={0.70,1.30,1.90,2.40,2.70}; return p[index]; }
   if(steps==6) { double p[6]={0.70,1.30,2.00,2.80,3.30,3.90}; return p[index]; }
   if(steps==7) { double p[7]={0.70,1.30,2.30,3.00,3.50,4.30,4.90}; return p[index]; }
   if(steps==8) { double p[8]={0.70,1.30,2.30,3.30,4.00,4.70,5.10,5.60}; return p[index]; }
   if(steps==9) { double p[9]={0.70,1.30,2.30,3.30,4.00,4.70,5.50,6.30,6.90}; return p[index]; }
   if(steps==10) { double p[10]={0.70,1.30,2.30,3.30,4.00,4.70,5.50,6.50,7.50,8.20}; return p[index]; }
   return 0;
}

// A latched threshold is not broker-confirmed protection. This helper lets
// the monitor retain an observed crossing through a retrace/restart while
// checking the CURRENT closing quote before recommending its saved level.
bool SupportAtStage(const bool buy,const double entry,const double close_quote,
                    const double old_sl,const int stage,double &candidate,
                    const double first_offset=-0.25,const double second_offset=0.0) {
   candidate=0;
   if(!PolicyPositive(entry) || !PolicyPositive(close_quote) || !PolicyNonnegative(old_sl) ||
      !MathIsValidNumber(first_offset) || !MathIsValidNumber(second_offset) ||
      second_offset<first_offset || stage<1 || stage>2) return false;
   double offset=stage==2 ? second_offset : first_offset;
   candidate=entry+(buy ? offset : -offset);
   if(!PolicyPositive(candidate)) return false;
   if(old_sl>0 && (buy ? candidate<=old_sl+1e-10 : candidate>=old_sl-1e-10)) return false;
   return buy ? candidate<close_quote : candidate>close_quote;
}

// Caller supplies BID for BUY, ASK for SELL. old_sl must be broker readback.
// false means no improvement/invalid data. Never records support state here.
bool SupportCandidate(const bool buy,const double entry,const double close_quote,
                      const double old_sl,double &candidate,
                      const double first_threshold=0.25,const double second_threshold=0.40,
                      const double first_offset=-0.25,const double second_offset=0.0) {
   candidate=0;
   if(!PolicyPositive(entry) || !PolicyPositive(close_quote) || !PolicyNonnegative(old_sl) ||
      !PolicyPositive(first_threshold) || !PolicyPositive(second_threshold) || second_threshold<=first_threshold ||
      !MathIsValidNumber(first_offset) || !MathIsValidNumber(second_offset) || second_offset<first_offset) return false;
   double advance=buy ? close_quote-entry : entry-close_quote;
   // Tolerance is for floating representation only, not price rounding.
   if(advance+1e-10<first_threshold) return false;
   return SupportAtStage(buy,entry,close_quote,old_sl,
      advance+1e-10>=second_threshold ? 2 : 1,candidate,first_offset,second_offset);
}

// Acknowledged send alone is insufficient; require actual broker readback.
// Once observed away from the original SL, support classification is sticky.
bool SupportConfirmed(const double original_sl,const double observed_sl,
                      const bool broker_readback,const double tick_size) {
   return broker_readback && PolicyPositive(original_sl) && PolicyPositive(observed_sl)
       && PolicyPositive(tick_size) && MathAbs(original_sl-observed_sl)>tick_size/2;
}

// Actual cash flow only: fees, commission, swap and profit from each deal
// must be included exactly once by the identity-keyed ledger, including IN.
// Do not clamp each deal: earlier profits must offset later losses.
bool RecoveryBalance(const double carry,const double sum_deal_net,double &debt) {
   debt=0;
   if(!PolicyNonnegative(carry) || !MathIsValidNumber(sum_deal_net)) return false;
   debt=MathMax(0,carry-sum_deal_net); return MathIsValidNumber(debt);
}

// Provisional exit P&L must exclude opening costs already in the ledger.
// Replacement by actual deals removes the estimate, it is never added again.
bool EffectiveRecovery(const double carry,const double sum_deal_net,
                       const double provisional_exit_net,double &debt) {
   if(!MathIsValidNumber(provisional_exit_net)) return false;
   return RecoveryBalance(carry,sum_deal_net+provisional_exit_net,debt);
}

// gross_needed is in account currency. Exit costs are positive reserves and
// must not include already booked costs. Conversion to price is broker-specific
// (OrderCalcProfit + bracketed solve); never assume XAUUSD contract/tick value.
bool RequiredGross(const double debt,const double exit_cost_reserve,double &gross_needed) {
   gross_needed=0;
   if(!PolicyNonnegative(debt) || !PolicyNonnegative(exit_cost_reserve)) return false;
   gross_needed=debt+exit_cost_reserve;
   return PolicyPositive(gross_needed); // zero/negative TP requires a decision
}

// May be closer than preceding targets. Caller chooses highest still-open
// ORIGINAL stage index and validates tick/stop/freeze levels before send.
bool ProfitableTarget(const bool buy,const double entry,const double target) {
   return PolicyPositive(entry) && PolicyPositive(target) && (buy ? target>entry : target<entry);
}

// Close of completed directional M1, configurable offset. No wall-clock
// extrapolation and no invented rule for owner-placed triggers at SL.
bool CandleTrigger(const double open,const double close,const double offset,
                   const bool manual_sl_trigger,double &price,bool &buy) {
   price=0;buy=false;
   if(manual_sl_trigger || !PolicyPositive(open) || !PolicyPositive(close) ||
      !PolicyPositive(offset) || open==close) return false;
   buy=close>open;price=close+(buy ? offset : -offset);
   return PolicyPositive(price);
}
#endif
