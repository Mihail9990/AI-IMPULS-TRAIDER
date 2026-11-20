#ifndef RECOVERY_CLOSE_CHECKPOINT_MQH
#define RECOVERY_CLOSE_CHECKPOINT_MQH
#include "RecoveryCloseWire.mqh"

// One immutable record commits the confirmed close and its derived follow-up
// plan together. It is not an outbox submission receipt or send permission.
struct RCCloseCheckpoint {
   RCCloseEvidenceKey key;
   RCRange before;
   RCCloseProof proof;
   long reentry_distance;
};
enum RCCheckpointResult { RCCP_CONFLICT=0,RCCP_APPLIED=1,RCCP_ALREADY_APPLIED=2 };

bool RCCPSameRange(const RCRange &a,const RCRange &b) {
   return a.generation==b.generation && a.range_id==b.range_id &&
          a.full_sl_count==b.full_sl_count && a.requires_manual_ranges==b.requires_manual_ranges &&
          a.active_order==b.active_order && a.position_id==b.position_id &&
          a.side==b.side && a.filled_steps==b.filled_steps && a.actual_entry_ticks==b.actual_entry_ticks &&
          a.last_closed_order==b.last_closed_order && a.last_closed_position==b.last_closed_position;
}

bool RCCPDerive(const RCCloseCheckpoint &record,RCRange &after,RCClosePlan &plan) {
   if(!RCCloseKeysEqual(record.key,record.key) ||
      !RCBound(record.before,record.key.generation,record.key.range_id) ||
      record.before.position_id!=record.key.position_id ||
      record.before.active_order!=record.key.anchor_order ||
      record.before.requires_manual_ranges || !RCBoundedPositive(record.before.actual_entry_ticks) ||
      record.before.last_closed_order==record.before.active_order ||
      record.before.last_closed_position==record.before.position_id ||
      record.reentry_distance<0 || record.reentry_distance>RC_MAX_UNITS) return false;
   after=record.before;
   return RCApplyClose(after,record.proof,record.reentry_distance,plan);
}

bool RCCPStageFromWire(const RCRange &before,const RCCloseWireBinding &binding,
                     const string history_wire,const string inventory_wire,
                     const RCProtectionFact &protection[],const long reentry_distance,
                     RCCloseCheckpoint &out) {
   out.key.scope=0; // invalid sentinel even if caller reused an output variable
   RCCloseCheckpoint candidate;
   if(RCBuildCloseFromWire(before,binding,history_wire,inventory_wire,protection,candidate.proof)
      !=RCE_FULLY_CLASSIFIED)return false;
   candidate.key=binding.key;candidate.before=before;candidate.reentry_distance=reentry_distance;
   RCRange after;
   RCClosePlan plan;
   if(!RCCPDerive(candidate,after,plan)) return false;
   out=candidate;
   return true;
}

void RCCPClearPlan(RCClosePlan &plan) {
   plan.classification=RC_UNRESOLVED;plan.current_slot=0;plan.reentry_slot=0;
   plan.reentry_side=0;plan.reentry_ticks=0;plan.keep_existing_opposite=false;
   plan.cancel_remaining_range_orders=false;
}

RCCheckpointResult RCCPApply(const RCCloseCheckpoint &record,RCRange &current,RCClosePlan &plan) {
   RCCPClearPlan(plan);
   RCRange after;
   RCClosePlan derived;
   if(!RCCPDerive(record,after,derived))return RCCP_CONFLICT;
   if(RCCPSameRange(current,after))return RCCP_ALREADY_APPLIED;
   if(!RCCPSameRange(current,record.before))return RCCP_CONFLICT;
   current=after;plan=derived;
   return RCCP_APPLIED;
}

// Canonical snapshot for a trusted terminal-local journal. The compact record
// stores only a fully classified close; result/counters/plans are rederived.
string RCCPEncode(const RCCloseCheckpoint &r) {
   RCRange after;
   RCClosePlan plan;
   if(!RCCPDerive(r,after,plan))return "";
   return "RECOVERY_CLOSE_CHECKPOINT_V1\t"+(string)r.key.scope+"\t"+(string)r.key.observation+"\t"+
      (string)r.key.generation+"\t"+(string)r.key.range_id+"\t"+(string)r.key.position_id+"\t"+
      (string)r.key.anchor_order+"\t"+(string)r.before.full_sl_count+"\t"+(string)(r.before.side==1?1:2)+"\t"+
      (string)r.before.filled_steps+"\t"+(string)r.before.actual_entry_ticks+"\t"+
      (string)r.before.last_closed_order+"\t"+(string)r.before.last_closed_position+"\t"+
      (string)r.proof.reason+"\t"+(string)r.proof.exit_stop_ticks+"\t"+(string)r.proof.confirmed_stop_mask+
      "\t"+(string)r.reentry_distance+"\tEND\n";
}

bool RCCPField(const string token,const ulong maximum,ulong &number) {
   return RecoveryAdoptionUnsigned(token,number) && number<=maximum;
}

bool RCCPDecode(const string text,const RCCloseEvidenceKey &expected,RCCloseCheckpoint &out) {
   out.key.scope=0;
   if(StringLen(text)<1 || StringLen(text)>1024 || StringFind(text,"\n")!=StringLen(text)-1)return false;
   string f[];
   if(StringSplit(StringSubstr(text,0,StringLen(text)-1),9,f)!=18 ||
      f[0]!="RECOVERY_CLOSE_CHECKPOINT_V1" || f[17]!="END")return false;
   RCCloseCheckpoint c;
   ulong range_id,count,side,steps,entry,reason,stop,mask,reentry;
   if(!RecoveryAdoptionUnsigned(f[1],c.key.scope) || !RecoveryAdoptionUnsigned(f[2],c.key.observation) ||
      !RecoveryAdoptionUnsigned(f[3],c.key.generation) || !RCCPField(f[4],2,range_id) ||
      !RecoveryAdoptionUnsigned(f[5],c.key.position_id) || !RecoveryAdoptionUnsigned(f[6],c.key.anchor_order) ||
      !RCCPField(f[7],RC_LIMIT-1,count) || !RCCPField(f[8],2,side) || side==0 ||
      !RCCPField(f[9],RC_MAX_UNITS,steps) || !RCCPField(f[10],RC_MAX_UNITS,entry) ||
      !RecoveryAdoptionUnsigned(f[11],c.before.last_closed_order) ||
      !RecoveryAdoptionUnsigned(f[12],c.before.last_closed_position) ||
      !RCCPField(f[13],RC_TP,reason) || !RCCPField(f[14],RC_MAX_UNITS,stop) ||
      !RCCPField(f[15],RC_SUPPORT_LEVEL,mask) || !RCCPField(f[16],RC_MAX_UNITS,reentry))return false;
   c.key.range_id=(int)range_id;
   if(!RCCloseKeysEqual(c.key,expected))return false;
   c.before.generation=c.key.generation;c.before.range_id=c.key.range_id;
   c.before.full_sl_count=(int)count;c.before.requires_manual_ranges=false;
   c.before.side=side==1?1:-1;c.before.filled_steps=(long)steps;c.before.actual_entry_ticks=(long)entry;
   c.before.position_id=c.key.position_id;c.before.active_order=c.key.anchor_order;
   c.proof.generation=c.key.generation;c.proof.range_id=c.key.range_id;
   c.proof.position_id=c.key.position_id;c.proof.order=c.key.anchor_order;
   c.proof.filled_steps=(long)steps;c.proof.exited_steps=(long)steps;c.proof.remaining_steps=0;
   c.proof.independently_verified=true;c.proof.history_complete=true;
   c.proof.reason=(int)reason;c.proof.exit_stop_ticks=(long)stop;
   c.proof.confirmed_stop_mask=(int)mask;c.proof.uniform_exit_reason_and_stop=true;
   c.reentry_distance=(long)reentry;
   if(RCCPEncode(c)!=text)return false;
   out=c;
   return true;
}

// Caller owns naming, one writer, ACL, and proof that the candidate came from
// RCCPStageFromWire. Create-only; never replaces prior evidence.
bool RCCPPublish(const RCCloseCheckpoint &record,const string record_id,const string publisher_tag) {
   const string text=RCCPEncode(record);
   if(text=="" || !RecoveryInvHex(record_id) || !RecoveryInvHex(publisher_tag))return false;
   const string prefix="RECOVERY_VERIFY\\"+record_id;
   const string final=prefix+".close",tmp=prefix+"."+publisher_tag+".close.tmp";
   if(FileIsExist(final) || FileIsExist(tmp))return false;
   uchar bytes[];
   const int length=StringToCharArray(text,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(length<1 || length>1024)return false;
   const int handle=FileOpen(tmp,FILE_WRITE|FILE_BIN);
   if(handle==INVALID_HANDLE)return false;
   const uint written=FileWriteArray(handle,bytes,0,length);
   FileFlush(handle);FileClose(handle);
   if(written==(uint)length && FileMove(tmp,0,final,0))return true;
   FileDelete(tmp);
   return false;
}

bool RCCPRead(const string record_id,const RCCloseEvidenceKey &expected,RCCloseCheckpoint &out) {
   out.key.scope=0;
   if(!RecoveryInvHex(record_id))return false;
   const int handle=FileOpen("RECOVERY_VERIFY\\"+record_id+".close",FILE_READ|FILE_BIN);
   if(handle==INVALID_HANDLE)return false;
   const ulong n=FileSize(handle);
   if(n<1 || n>1024){FileClose(handle);return false;}
   uchar bytes[];
   const uint count=FileReadArray(handle,bytes,0,(int)n);
   FileClose(handle);
   if(count!=(uint)n)return false;
   for(int i=0;i<(int)n;i++)
      if((bytes[i]<32 && bytes[i]!=9 && bytes[i]!=10) || bytes[i]>126)return false;
   return RCCPDecode(CharArrayToString(bytes,0,(int)n,CP_UTF8),expected,out);
}
#endif
