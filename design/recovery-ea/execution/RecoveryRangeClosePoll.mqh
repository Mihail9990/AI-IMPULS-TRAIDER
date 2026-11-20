#ifndef RECOVERY_RANGE_CLOSE_POLL_MQH
#define RECOVERY_RANGE_CLOSE_POLL_MQH
#include "RecoveryProofRetry.mqh"
#include "../cycle/RecoveryCloseCheckpoint.mqh"
#include "RecoveryOwnerEvidence.mqh"
#include "RecoveryInventoryRequest.mqh"
#include "RecoveryMoneyJournal.mqh"

enum RecoveryClosePollState { RCP_IDLE=0,RCP_HISTORY_REQUEST,RCP_HISTORY_READ,
                              RCP_INVENTORY_REQUEST,RCP_INVENTORY_READ,RCP_DONE,RCP_UNRESOLVED };
enum RecoveryClosePollResult { RCP_WAIT=0,RCP_PROGRESS,RCP_PROOF,RCP_NOT_CONFIRMED };
struct RecoveryRangeClosePoll {
 RecoveryClosePollState state;
 RecoveryProofRetry retry;
 RCCloseWireBinding binding;
 RCRange before;
 ulong live_ticket;
 ulong verified_sequence;
 string publisher_tag;
 RecoveryHistorySnapshot history;
};

void RecoveryRearmCloseRead(RecoveryRangeClosePoll &p) {
 if(p.state!=RCP_UNRESOLVED || !RecoveryProofRetryDue(p.retry,GetTickCount64()))return;
 const RecoveryProofRetry retry=p.retry;
 ZeroMemory(p);p.retry=retry;
}

// For an exact known position from this session only. Absence merely
// starts observation; it cannot classify a close or submit a broker action.
bool RecoveryBeginRangeClosePoll(RecoveryRangeClosePoll &task,RecoveryEvidenceOwner &owner,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RecoveryInventoryScope &verifier,const RRRangeState &range,const RRSettings &cfg,
    const double tick,const double step) {
 if(task.state!=RCP_IDLE || !RecoverySessionCurrent(session,live) ||
    session.epoch!=owner.executor_epoch || !RecoverySessionScopeEqual(owner.context,live) ||
    !RecoveryInvScopeOK(verifier) || verifier.executor_epoch!=owner.executor_epoch ||
    verifier.verifier_epoch!=owner.verifier_epoch || verifier.verifier_chart!=owner.verifier_chart ||
    verifier.login!=live.login || verifier.server!=live.server || verifier.symbol!=live.symbol ||
    verifier.magic!=live.magic || verifier.generation<=0 ||
    (ulong)verifier.generation!=range.cycle.generation ||
    owner.serial==ULONG_MAX || !RRValid(range,cfg) || range.position_ticket==0 ||
    range.cycle.position_id==0 || range.cycle.active_order==0 ||
    PositionSelectByTicket(range.position_ticket))return false;
 RCCloseWireBinding b;
 ZeroMemory(b);
 const ulong serial=owner.serial+1;
 b.key.scope=range.scope;b.key.observation=serial;b.key.generation=range.cycle.generation;
 b.key.range_id=range.cycle.range_id;b.key.position_id=range.cycle.position_id;b.key.anchor_order=range.cycle.active_order;
 const string identity=owner.executor_epoch+":"+(string)b.key.generation+":"+
    (string)b.key.position_id+":"+(string)b.key.anchor_order+":"+(string)serial;
 b.history.binding=verifier;b.inventory=verifier;
 b.history.binding.nonce=RecoveryOwnerNonce("close-history",identity);
 b.inventory.nonce=RecoveryOwnerNonce("close-inventory",identity);
 const string tag=RecoveryOwnerNonce("close-publisher",identity);
 if(!RecoveryInvHex(tag) || tag==b.inventory.nonce || tag==b.history.binding.nonce)return false;
 b.history.position_identifier=b.key.position_id;b.history.anchor_order=b.key.anchor_order;
 b.tick=tick;b.step=step;
 if(!RCCloseWireScope(b))return false;
 task.before=range.cycle;task.live_ticket=range.position_ticket;
 task.binding=b;task.publisher_tag=tag;
 owner.serial=serial;task.state=RCP_HISTORY_REQUEST;
 return true;
}

RecoveryClosePollResult RecoveryClosePollStop(RecoveryRangeClosePoll &task) {
 task.state=RCP_UNRESOLVED;return RCP_NOT_CONFIRMED;
}

// Protection facts are assembled separately from the exact in-session SL
// observations. This reader cannot infer a protective stop from a quote.
RecoveryClosePollResult RecoveryRangeClosePollStep(RecoveryRangeClosePoll &task,
    const RecoverySessionLifecycle &session,const RecoverySessionContext &live,
    const RRRangeState &range,const RRSettings &cfg,const RCProtectionFact &facts[],
    RCCloseCheckpoint &out) {
 out.key.scope=0;
 if(task.state==RCP_IDLE || task.state==RCP_DONE || task.state==RCP_UNRESOLVED)
    return RCP_NOT_CONFIRMED;
 const RecoveryInventoryScope v=task.binding.inventory;
 if(!RecoverySessionCurrent(session,live) || session.epoch!=v.executor_epoch ||
    live.login!=v.login || live.server!=v.server || live.symbol!=v.symbol || live.magic!=v.magic ||
    !RecoveryAlternatingPumpSession(v) || !RRValid(range,cfg) ||
    range.scope!=task.binding.key.scope || range.position_ticket!=task.live_ticket ||
    !RCCPSameRange(range.cycle,task.before))return RecoveryClosePollStop(task);
 switch(task.state) {
   case RCP_HISTORY_REQUEST:
      task.state=RCP_HISTORY_READ;
      if(!RecoveryHistoryRequest(task.binding.history,task.publisher_tag))return RecoveryClosePollStop(task);
      return RCP_PROGRESS;
   case RCP_HISTORY_READ:
      if(!RecoveryHistoryRead(task.binding.history,task.history))
         return FileIsExist("RECOVERY_VERIFY\\"+task.binding.history.binding.nonce+".history") ?
                RecoveryClosePollStop(task) : RCP_WAIT;
      if(!task.history.usable || !task.history.observed ||
         task.history.sequence<=task.binding.history_after)return RecoveryClosePollStop(task);
      task.state=RCP_INVENTORY_REQUEST;
      return RCP_PROGRESS;
   case RCP_INVENTORY_REQUEST:
      task.state=RCP_INVENTORY_READ;
      if(!RecoveryRequestInventory(v.nonce,v.executor_epoch,v.verifier_epoch,v.generation,
                                   v.login,v.server,v.symbol,v.magic))return RecoveryClosePollStop(task);
      return RCP_PROGRESS;
   case RCP_INVENTORY_READ: {
      RecoveryInventorySnapshot inv;
      if(!RecoveryInvRead(v,inv))
         return FileIsExist("RECOVERY_VERIFY\\"+v.nonce+".snapshot") ? RecoveryClosePollStop(task) : RCP_WAIT;
      if(inv.sequence<=task.history.sequence || !RecoveryAlternatingPumpSession(v))
         return RecoveryClosePollStop(task);
      RCCloseCheckpoint observed;
      const RCEvidenceResult status=RCCPObserveFromReports(task.before,task.binding,task.history,inv,facts,
                                                           cfg.initial_sl_ticks,observed,true);
      if(status!=RCE_FULLY_CLASSIFIED && status!=RCE_CLOSED_UNCLASSIFIED)return RecoveryClosePollStop(task);
      if(!RecoveryMoneyEnqueue(task.binding,task.history,observed))
         Print("RECOVERY: confirmed close has no monetary journal entry; amount unresolved.");
      RCRange after;RCClosePlan plan;
      if(status!=RCE_FULLY_CLASSIFIED || !RCCPDerive(observed,after,plan))return RecoveryClosePollStop(task);
      out=observed;
      task.verified_sequence=(ulong)inv.sequence;
      task.state=RCP_DONE;
      return RCP_PROOF;
   }
   default:
      return RecoveryClosePollStop(task);
 }
}
#endif
