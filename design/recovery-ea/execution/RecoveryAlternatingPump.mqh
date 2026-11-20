#ifndef RECOVERY_ALTERNATING_PUMP_MQH
#define RECOVERY_ALTERNATING_PUMP_MQH
#include "RecoveryAlternatingEvidence.mqh"
#include "RecoveryInventoryRequest.mqh"

enum RecoveryAlternatingPumpState {
 RAP_UNUSED=0, RAP_REQUEST_EXACT, RAP_WAIT_EXACT, RAP_REQUEST_HISTORY, RAP_WAIT_HISTORY,
 RAP_REQUEST_INVENTORY, RAP_WAIT_INVENTORY, RAP_DONE, RAP_UNRESOLVED
};
enum RecoveryAlternatingPumpResult { RAP_WAIT=0, RAP_PROGRESS, RAP_PROOF, RAP_STOPPED };

struct RecoveryAlternatingPump {
 RecoveryAlternatingPumpState state;
 RecoveryAlternatingEvidenceBinding binding;
 RRKnownOrder wanted;
 ulong prior_order,prior_position,prior_ticket;
 int range_id;
 string publisher_tag;
 RecoveryExactReport exact;
 RecoveryHistorySnapshot history;
};

// Caller owns globally unique nonces (including across stages and prior
// pumps), current boot state and exact ticket/intent association.
bool RecoveryAlternatingPumpBegin(RecoveryAlternatingPump &p,const RRRangeState &s,
    const RROpeningPair &opening,const RecoveryAlternatingEvidenceBinding &evidence_binding,
    const string publisher_tag) {
 if(p.state!=RAP_UNUSED || evidence_binding.history.position_identifier!=0 ||
    !RecoveryInvHex(publisher_tag) || evidence_binding.scope!=s.scope ||
    evidence_binding.exact.order_ticket==0 || evidence_binding.exact.order_ticket!=evidence_binding.history.anchor_order ||
    !MathIsValidNumber(evidence_binding.price_tick) || evidence_binding.price_tick<=0 ||
    !MathIsValidNumber(evidence_binding.volume_step) || evidence_binding.volume_step<=0 ||
    !RecoveryAlternatingSameSession(evidence_binding.exact.binding,evidence_binding.history.binding) ||
    !RecoveryAlternatingSameSession(evidence_binding.exact.binding,evidence_binding.inventory) ||
    evidence_binding.exact.binding.nonce==evidence_binding.history.binding.nonce ||
    evidence_binding.exact.binding.nonce==evidence_binding.inventory.nonce ||
    evidence_binding.history.binding.nonce==evidence_binding.inventory.nonce) return false;
 // RecoveryAlternatingWanted also requires a distinct successor identifier.
 // This symbolic value is for binding validation only; never put it on wire.
 RecoveryAlternatingEvidenceBinding check=evidence_binding;
 check.history.position_identifier=s.cycle.position_id==1 ? 2 : 1;
 RRKnownOrder wanted;
 if(!RecoveryAlternatingWanted(s,opening,check,wanted)) return false;
 p.binding=evidence_binding;p.wanted=wanted;p.publisher_tag=publisher_tag;
 p.prior_order=s.cycle.active_order;p.prior_position=s.cycle.position_id;
 p.prior_ticket=s.position_ticket;p.range_id=s.cycle.range_id;
 RecoveryExactReportClear(p.exact);RecoveryHistoryClear(p.history);
 p.state=RAP_REQUEST_EXACT;
 return true;
}

bool RecoveryAlternatingPumpBound(const RecoveryAlternatingPump &p,const RRRangeState &s,
                                   const RROpeningPair &opening) {
 if(p.prior_order!=s.cycle.active_order || p.prior_position!=s.cycle.position_id ||
    p.prior_ticket!=s.position_ticket || p.range_id!=s.cycle.range_id) return false;
 RecoveryAlternatingEvidenceBinding check=p.binding;
 if(check.history.position_identifier==0)
    check.history.position_identifier=p.prior_position==1 ? 2 : 1;
 RRKnownOrder now;
 return RecoveryAlternatingWanted(s,opening,check,now) && RRKnownEqual(now,p.wanted);
}

bool RecoveryAlternatingPumpExact(const RecoveryAlternatingPump &p,const RecoveryExactReport &e,
                                   ulong &successor) {
 successor=0;
 if(!e.usable || !e.observed || e.reason!="ok" || e.source!="history" ||
    e.sequence<=0 || (ulong)e.sequence<=p.binding.verify_after ||
    !RecoveryAdoptionUnsigned(e.position_identifier,successor) ||
    successor==0 || successor==p.prior_position) return false;
 string f[];
 long stop_limit;
 return StringSplit(e.row,9,f)==12 && f[0]=="E" && f[1]=="history" &&
    f[2]==e.position_identifier && f[3]==(string)p.binding.exact.binding.magic &&
    RecoveryAlternatingOrderTerms(f[4],f[5],f[6],f[7],f[8],f[9],f[10],p.wanted,p.binding) &&
    RecoveryAdoptionUnits(f[11],p.binding.price_tick,true,stop_limit) && stop_limit==0;
}

RecoveryAlternatingPumpResult RecoveryAlternatingPumpStop(RecoveryAlternatingPump &p) {
 p.state=RAP_UNRESOLVED;
 return RAP_STOPPED;
}

bool RecoveryAlternatingPumpSession(const RecoveryInventoryScope &s) {
 return TerminalInfoInteger(TERMINAL_CONNECTED)!=0 &&
        AccountInfoInteger(ACCOUNT_LOGIN)==s.login && AccountInfoString(ACCOUNT_SERVER)==s.server &&
        _Symbol==s.symbol;
}

// One bounded action per tick. A request stage is consumed before publishing
// the verifier probe, including on an ambiguous file publication failure.
// A waiting stage only reads its exact correlated response. It never resends.
RecoveryAlternatingPumpResult RecoveryAlternatingPumpStep(RecoveryAlternatingPump &p,
    const RRRangeState &s,const RROpeningPair &opening,RRAlternatingObservation &out) {
 ZeroMemory(out);
 if(p.state==RAP_UNUSED || p.state==RAP_DONE || p.state==RAP_UNRESOLVED) return RAP_STOPPED;
 if(!RecoveryAlternatingPumpBound(p,s,opening) || !RecoveryAlternatingPumpSession(p.binding.inventory))
    return RecoveryAlternatingPumpStop(p);

 switch(p.state) {
   case RAP_REQUEST_EXACT:
      p.state=RAP_WAIT_EXACT;
      if(!RecoveryExactReportRequest(p.binding.exact,p.publisher_tag))
         return RecoveryAlternatingPumpStop(p);
      return RAP_PROGRESS;
   case RAP_WAIT_EXACT: {
      if(!RecoveryExactReportRead(p.binding.exact,p.exact))return RAP_WAIT;
      ulong position;
      if(!RecoveryAlternatingPumpExact(p,p.exact,position))return RecoveryAlternatingPumpStop(p);
      p.binding.history.position_identifier=position;
      p.state=RAP_REQUEST_HISTORY;
      return RAP_PROGRESS;
   }
   case RAP_REQUEST_HISTORY:
      p.state=RAP_WAIT_HISTORY;
      if(!RecoveryHistoryRequest(p.binding.history,p.publisher_tag))
         return RecoveryAlternatingPumpStop(p);
      return RAP_PROGRESS;
   case RAP_WAIT_HISTORY:
      if(!RecoveryHistoryRead(p.binding.history,p.history))return RAP_WAIT;
      if(!p.history.usable || !p.history.observed || p.history.reason!="ok" ||
         p.history.sequence<=p.exact.sequence || p.history.order_count!=1 ||
         p.history.deal_count<1 || p.history.deal_count>128)
         return RecoveryAlternatingPumpStop(p);
      p.state=RAP_REQUEST_INVENTORY;
      return RAP_PROGRESS;
   case RAP_REQUEST_INVENTORY: {
      const RecoveryInventoryScope i=p.binding.inventory;
      p.state=RAP_WAIT_INVENTORY;
      if(!RecoveryRequestInventory(i.nonce,i.executor_epoch,i.verifier_epoch,i.generation,
                                   i.login,i.server,i.symbol,i.magic))
         return RecoveryAlternatingPumpStop(p);
      return RAP_PROGRESS;
   }
   case RAP_WAIT_INVENTORY: {
      RecoveryInventorySnapshot inventory;
      if(!RecoveryInvRead(p.binding.inventory,inventory))return RAP_WAIT;
      if(!RecoveryAlternatingPumpSession(p.binding.inventory) ||
         !RecoveryAlternatingEvidenceFold(s,opening,p.binding,p.exact,p.history,inventory,out))
         return RecoveryAlternatingPumpStop(p);
      p.state=RAP_DONE;
      return RAP_PROOF;
   }
   default:
      return RecoveryAlternatingPumpStop(p);
 }
}
#endif
