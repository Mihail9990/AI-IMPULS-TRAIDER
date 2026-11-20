#ifndef RECOVERY_EXACT_ORDER_READER_MQH
#define RECOVERY_EXACT_ORDER_READER_MQH
#include "RecoveryInventoryReader.mqh"

struct RecoveryExactReportScope {
   RecoveryInventoryScope binding;
   ulong order_ticket;
};
struct RecoveryExactReport {
   bool usable;
   bool observed;
   string reason;
   string source;
   long sequence;
   string position_identifier; // Canonical unsigned decimal; zero is possible.
   string row;                 // E row; numeric fields remain exact wire text.
};
void RecoveryExactReportClear(RecoveryExactReport &out) {
   out.usable=false; out.observed=false; out.reason=""; out.source="";
   out.sequence=0; out.position_identifier=""; out.row="";
}

string RecoveryExactReportRequestText(const RecoveryExactReportScope &scope) {
   if(!RecoveryInvScopeOK(scope.binding) || scope.order_ticket==0) return "";
   return "RECOVERY_EXACT_ORDER_REQUEST_V1\t"+scope.binding.nonce+"\t"+
          scope.binding.executor_epoch+"\t"+scope.binding.verifier_epoch+"\t"+
          (string)scope.binding.generation+"\t"+(string)scope.binding.login+"\t"+
          scope.binding.server+"\t"+scope.binding.symbol+"\t"+(string)scope.binding.magic+"\t"+
          (string)scope.order_ticket+"\tEND\n";
}
bool RecoveryExactReportParse(const string wire,const RecoveryExactReportScope &scope,RecoveryExactReport &out) {
   RecoveryExactReportClear(out);
   if(!RecoveryInvScopeOK(scope.binding) || scope.order_ticket==0 || StringLen(wire)>4096) return false;
   int line=StringFind(wire,"\n");
   if(line<1) return false;
   string fields[];
   if(StringSplit(StringSubstr(wire,0,line),9,fields)!=14 ||
      fields[0]!="RECOVERY_EXACT_ORDER_V1" ||
      fields[1]!=scope.binding.nonce || fields[2]!=scope.binding.executor_epoch ||
      fields[3]!=scope.binding.verifier_epoch || fields[4]!=(string)scope.binding.generation ||
      fields[5]!=(string)scope.binding.login || fields[6]!=scope.binding.server ||
      fields[7]!=scope.binding.symbol || fields[8]!=(string)scope.binding.magic ||
      fields[9]!=(string)scope.binding.verifier_chart ||
      !RecoveryInvNatural(fields[10],"9223372036854775807",true) || fields[11]!=(string)scope.order_ticket) return false;
   const bool observed=fields[12]=="observed";
   const string reason=fields[13];
   const long sequence=StringToInteger(fields[10]);
   string source="",position="",row="";
   if(!observed) {
      if(fields[12]!="unavailable" ||
         (reason!="unknown" && reason!="mismatch" && reason!="read" &&
          reason!="changed" && reason!="session") ||
         StringSubstr(wire,line+1)!="END\n") return false;
   } else {
      int next=StringFind(wire,"\n",line+1);
      if(reason!="ok" || next<=line+1 || StringSubstr(wire,next+1)!="END\n") return false;
      row=StringSubstr(wire,line+1,next-line-1);
      const string max="18446744073709551615";
      if(StringSplit(row,9,fields)!=12 || fields[0]!="E" ||
         (fields[1]!="current" && fields[1]!="history") ||
         !RecoveryInvNatural(fields[2],max) || fields[3]!=(string)scope.binding.magic ||
         !RecoveryInvNatural(fields[4],max) || !RecoveryInvNatural(fields[5],max) ||
         !RecoveryInvFixed(fields[6],true)) return false;
      for(int i=7;i<12;i++) if(!RecoveryInvFixed(fields[i])) return false;
      int remaining=StringLen(fields[7]), initial=StringLen(fields[6]);
      if(remaining>initial || (remaining==initial && StringCompare(fields[7],fields[6])>0)) return false;
      source=fields[1]; position=fields[2];
   }
   out.usable=true; out.observed=observed; out.reason=reason; out.sequence=sequence;
   out.source=source; out.position_identifier=position; out.row=row;
   return true;
}
// Publisher tag is unique per attempt; one designated requester owns each nonce.
// A false result does not authorize reuse of the nonce or a broker retry.
bool RecoveryExactReportRequest(const RecoveryExactReportScope &scope,const string publisher_tag) {
   const string text=RecoveryExactReportRequestText(scope);
   if(text=="" || !RecoveryInvHex(publisher_tag)) return false;
   const string prefix="RECOVERY_VERIFY\\"+scope.binding.nonce;
   const string final=prefix+".ord", temp=prefix+"."+publisher_tag+".ord.tmp";
   if(FileIsExist(final) || FileIsExist(prefix+".order") || FileIsExist(temp)) return false;
   uchar payload[];
   const int length=StringToCharArray(text,payload,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(length<1 || length>2048) return false;
   const int h=FileOpen(temp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const uint written=FileWriteArray(h,payload,0,length);
   FileFlush(h);
   FileClose(h);
   if(written==(uint)length && FileMove(temp,0,final,0)) return true;
   FileDelete(temp);
   return false;
}

bool RecoveryExactReportRead(const RecoveryExactReportScope &scope,RecoveryExactReport &snapshot) {
   RecoveryExactReportClear(snapshot);
   if(!RecoveryInvScopeOK(scope.binding) || scope.order_ticket==0) return false;
   const int h=FileOpen("RECOVERY_VERIFY\\"+scope.binding.nonce+".order",FILE_READ|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const ulong size=FileSize(h);
   if(size<1 || size>4096) { FileClose(h); return false; }
   uchar bytes[];
   const uint copied=FileReadArray(h,bytes,0,(int)size);
   FileClose(h);
   if(copied!=(uint)size) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]<32 && bytes[i]!=9 && bytes[i]!=10) return false;
   const string wire=CharArrayToString(bytes,0,(int)size,CP_UTF8);
   uchar canonical[];
   if(StringToCharArray(wire,canonical,0,WHOLE_ARRAY,CP_UTF8)!=(int)size+1) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]!=canonical[i]) return false;
   return RecoveryExactReportParse(wire,scope,snapshot);
}
#endif
