#ifndef RECOVERY_HISTORY_READER_MQH
#define RECOVERY_HISTORY_READER_MQH
#include "RecoveryInventoryReader.mqh"
// Read-only result consumer; no inferred command outcome or trading calls.

struct RecoveryHistoryScope {
   RecoveryInventoryScope binding;
   ulong position_identifier;
   ulong anchor_order;
};

struct RecoveryHistorySnapshot {
   bool usable;
   bool observed;  // Exact evidence exists; not proof of complete execution.
   string reason;
   long sequence;
   int order_count;
   int deal_count;
   string rows[];  // Canonical H rows followed by D rows.
};

bool RecoveryHistoryScopeOK(const RecoveryHistoryScope &scope) {
   return RecoveryInvScopeOK(scope.binding) && scope.position_identifier>0 && scope.anchor_order>0;
}

void RecoveryHistoryClear(RecoveryHistorySnapshot &snapshot) {
   snapshot.usable=false; snapshot.observed=false; snapshot.reason="";
   snapshot.sequence=0; snapshot.order_count=0; snapshot.deal_count=0;
   ArrayFree(snapshot.rows);
}

bool RecoveryHistoryOrderFields(const string &p[]) {
   const string max="18446744073709551615";
   if(ArraySize(p)!=11 || p[0]!="H" ||
      !RecoveryInvNatural(p[1],max,true) || !RecoveryInvNatural(p[2],max) ||
      !RecoveryInvNatural(p[3],max) || !RecoveryInvNatural(p[4],max) ||
      !RecoveryInvNatural(p[5],max) || !RecoveryInvFixed(p[6],true)) return false;
   for(int i=7;i<11;i++) if(!RecoveryInvFixed(p[i])) return false;
   const int remainder=StringLen(p[7]), initial=StringLen(p[6]);
   return remainder<initial || (remainder==initial && StringCompare(p[7],p[6])<=0);
}

bool RecoveryHistoryDealFields(const string &p[]) {
   const string max="18446744073709551615";
   return ArraySize(p)==12 && p[0]=="D" &&
          RecoveryInvNatural(p[1],max,true) && RecoveryInvNatural(p[2],max,true) &&
          RecoveryInvNatural(p[3],max) && (p[4]=="BUY" || p[4]=="SELL") &&
          RecoveryInvNatural(p[5],max) && RecoveryInvNatural(p[6],max) &&
          RecoveryInvNatural(p[7],max,true) && RecoveryInvFixed(p[8],true) &&
          RecoveryInvFixed(p[9],true) && RecoveryInvFixed(p[10]) && RecoveryInvFixed(p[11]);
}

string RecoveryHistoryRequestText(const RecoveryHistoryScope &scope) {
   if(!RecoveryHistoryScopeOK(scope)) return "";
   return "RECOVERY_HISTORY_REQUEST_V1\t"+scope.binding.nonce+"\t"+
          scope.binding.executor_epoch+"\t"+scope.binding.verifier_epoch+"\t"+
          (string)scope.binding.generation+"\t"+(string)scope.binding.login+"\t"+
          scope.binding.server+"\t"+scope.binding.symbol+"\t"+(string)scope.binding.magic+"\t"+
          (string)scope.position_identifier+"\t"+(string)scope.anchor_order+"\tEND\n";
}

bool RecoveryHistoryParse(const string wire,const RecoveryHistoryScope &scope,
                          RecoveryHistorySnapshot &snapshot) {
   RecoveryHistoryClear(snapshot);
   if(StringLen(wire)>65536 || !RecoveryHistoryScopeOK(scope)) return false;
   int start=StringFind(wire,"\n");
   if(start<1) return false;
   string fields[];
   if(StringSplit(StringSubstr(wire,0,start),9,fields)!=17 ||
      fields[0]!="RECOVERY_HISTORY_V1" ||
      fields[1]!=scope.binding.nonce || fields[2]!=scope.binding.executor_epoch ||
      fields[3]!=scope.binding.verifier_epoch || fields[4]!=(string)scope.binding.generation ||
      fields[5]!=(string)scope.binding.login || fields[6]!=scope.binding.server ||
      fields[7]!=scope.binding.symbol || fields[8]!=(string)scope.binding.magic ||
      fields[9]!=(string)scope.binding.verifier_chart ||
      !RecoveryInvNatural(fields[10],"9223372036854775807",true) ||
      fields[11]!=(string)scope.position_identifier || fields[12]!=(string)scope.anchor_order ||
      !RecoveryInvNatural(fields[15],"128") || !RecoveryInvNatural(fields[16],"128")) return false;
   const int orders=(int)StringToInteger(fields[15]), deals=(int)StringToInteger(fields[16]);
   const long sequence=StringToInteger(fields[10]);
   const string reason=fields[14], anchor=(string)scope.anchor_order;
   const bool observed=fields[13]=="observed";
   if(observed ? reason!="ok" :
      (fields[13]!="unavailable" || orders!=0 || deals!=0 ||
       (reason!="read" && reason!="changed" && reason!="limit" && reason!="session" &&
        reason!="anchor_missing"))) return false;
   bool matched=false;
   string order_tickets[128], deal_tickets[128], rows[];
   if(ArrayResize(rows,orders+deals)!=orders+deals) return false;
   start++;
   for(int i=0;i<orders+deals;i++) {
      const int next=StringFind(wire,"\n",start);
      if(next<=start) return false;
      const string row=StringSubstr(wire,start,next-start);
      start=next+1;
      if(StringSplit(row,9,fields)<1) return false;
      if(i<orders) {
         if(!RecoveryHistoryOrderFields(fields)) return false;
         for(int j=0;j<i;j++) if(order_tickets[j]==fields[1]) return false;
         order_tickets[i]=fields[1];
         if(fields[1]==anchor) matched=true;
      } else {
         if(!RecoveryHistoryDealFields(fields)) return false;
         const int index=i-orders;
         for(int j=0;j<index;j++) if(deal_tickets[j]==fields[1]) return false;
         deal_tickets[index]=fields[1];
         if(fields[2]==anchor) matched=true;
      }
      rows[i]=row;
   }
   if(StringSubstr(wire,start)!="END\n" || (observed && !matched)) return false;
   if(orders+deals>0 && ArrayCopy(snapshot.rows,rows)!=orders+deals) {
      RecoveryHistoryClear(snapshot);
      return false;
   }
   snapshot.usable=true; snapshot.observed=observed; snapshot.reason=reason;
   snapshot.sequence=sequence; snapshot.order_count=orders; snapshot.deal_count=deals;
   return true;
}

// Publisher tag is unique per attempt; one designated requester owns each nonce.
// A false result does not authorize reuse of the nonce or a broker retry.
bool RecoveryHistoryRequest(const RecoveryHistoryScope &scope,const string publisher_tag) {
   const string text=RecoveryHistoryRequestText(scope);
   if(text=="" || !RecoveryInvHex(publisher_tag)) return false;
   const string prefix="RECOVERY_VERIFY\\"+scope.binding.nonce;
   const string request_path=prefix+".hist", temp=prefix+"."+publisher_tag+".hist.tmp";
   if(FileIsExist(request_path) || FileIsExist(prefix+".history") || FileIsExist(temp)) return false;
   uchar payload[];
   const int length=StringToCharArray(text,payload,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(length<1 || length>2048) return false;
   const int h=FileOpen(temp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const uint written=FileWriteArray(h,payload,0,length);
   FileFlush(h);
   FileClose(h);
   if(written==(uint)length && FileMove(temp,0,request_path,0)) return true;
   FileDelete(temp);
   return false;
}

bool RecoveryHistoryRead(const RecoveryHistoryScope &scope,RecoveryHistorySnapshot &snapshot) {
   RecoveryHistoryClear(snapshot);
   if(!RecoveryHistoryScopeOK(scope)) return false;
   const int h=FileOpen("RECOVERY_VERIFY\\"+scope.binding.nonce+".history",FILE_READ|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const ulong size=FileSize(h);
   if(size<1 || size>65536) { FileClose(h); return false; }
   uchar bytes[];
   const uint copied=FileReadArray(h,bytes,0,(int)size);
   FileClose(h);
   if(copied!=(uint)size) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]<32 && bytes[i]!=9 && bytes[i]!=10) return false;
   const string wire=CharArrayToString(bytes,0,(int)size,CP_UTF8);
   uchar canonical[];
   if(StringToCharArray(wire,canonical,0,WHOLE_ARRAY,CP_UTF8)!=(int)size+1) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]!=canonical[i]) return false;
   return RecoveryHistoryParse(wire,scope,snapshot);
}
#endif
