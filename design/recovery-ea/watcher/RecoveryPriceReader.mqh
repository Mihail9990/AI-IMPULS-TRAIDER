#ifndef RECOVERY_PRICE_READER_MQH
#define RECOVERY_PRICE_READER_MQH
#include "../verifier/RecoveryInventoryReader.mqh"
#include "RecoveryPriceStage.mqh"
struct RecoveryPriceScope {
   string nonce, executor_epoch, watcher_epoch;
   ulong generation;
   long login, watcher_chart;
   string server, symbol;
   ulong position_identifier, live_ticket;
   int side;
   string actual_entry, first_advance, second_advance;
};
struct RecoveryPriceSignal {
   bool usable, observed;
   long sequence;
   int stage;
   string selected_quote;
};
void RecoveryPriceClear(RecoveryPriceSignal &out) {
   out.usable=false; out.observed=false; out.sequence=0; out.stage=-1; out.selected_quote="";
}
bool RecoveryPriceCanonical(const string value) {
   const double d=StringToDouble(value);
   return RecoveryInvFixed(value,true) && RecoveryPricePositive(d) && DoubleToString(d,16)==value;
}
bool RecoveryPriceScopeOK(const RecoveryPriceScope &s) {
   return RecoveryInvHex(s.nonce) && RecoveryInvHex(s.executor_epoch) && RecoveryInvHex(s.watcher_epoch) &&
          s.executor_epoch!=s.watcher_epoch && s.generation>0 && s.login>0 && s.watcher_chart>0 &&
          RecoveryInvContext(s.server) && RecoveryInvContext(s.symbol) && s.position_identifier>0 &&
          s.live_ticket>0 && (s.side==1 || s.side==-1) && RecoveryPriceCanonical(s.actual_entry) &&
          (s.first_advance=="0.0000000000000000" || RecoveryPriceCanonical(s.first_advance)) &&
          RecoveryPriceCanonical(s.second_advance) &&
          RecoveryPriceThresholds(StringToDouble(s.first_advance),StringToDouble(s.second_advance));
}
string RecoveryPriceRequestText(const RecoveryPriceScope &s) {
   if(!RecoveryPriceScopeOK(s)) return "";
   return "RECOVERY_PRICE_REQUEST_V1\t"+s.nonce+"\t"+s.executor_epoch+"\t"+s.watcher_epoch+"\t"+
          (string)s.generation+"\t"+(string)s.login+"\t"+s.server+"\t"+s.symbol+"\t"+
          (string)s.position_identifier+"\t"+(string)s.live_ticket+"\t"+(s.side==1 ? "BUY" : "SELL")+"\t"+
          s.actual_entry+"\t"+s.first_advance+"\t"+s.second_advance+"\t"+(string)s.watcher_chart+"\tEND\n";
}
string RecoveryPriceFollowText(const RecoveryPriceScope &s,const int desired_stage) {
   if(!RecoveryPriceScopeOK(s) || (desired_stage!=1 && desired_stage!=2) ||
      (desired_stage==1 && s.first_advance=="0.0000000000000000")) return "";
   return "RECOVERY_PRICE_FOLLOW_V2\t"+s.nonce+"\t"+s.executor_epoch+"\t"+s.watcher_epoch+"\t"+
          (string)s.generation+"\t"+(string)s.login+"\t"+s.server+"\t"+s.symbol+"\t"+
          (string)s.position_identifier+"\t"+(string)s.live_ticket+"\t"+(s.side==1 ? "BUY" : "SELL")+"\t"+
          s.actual_entry+"\t"+s.first_advance+"\t"+s.second_advance+"\t"+(string)desired_stage+"\t"+
          (string)s.watcher_chart+"\tEND\n";
}
bool RecoveryPriceParseVersion(const string wire,const RecoveryPriceScope &s,const ulong after_sequence,
                               const int desired_stage,RecoveryPriceSignal &out) {
   RecoveryPriceClear(out);
   const int n=StringLen(wire);
   if(desired_stage<0 || desired_stage>2) return false;
   const bool follow=desired_stage>0;
   const int shift=follow ? 1 : 0;
   if(n<2 || n>2048 || !RecoveryPriceScopeOK(s) || StringFind(wire,"\n")!=n-1 ||
      (desired_stage==1 && s.first_advance=="0.0000000000000000")) return false;
   string f[];
   if(StringSplit(StringSubstr(wire,0,n-1),9,f)!=20+shift ||
      f[0]!=(follow ? "RECOVERY_PRICE_PEAK_V2" : "RECOVERY_PRICE_V1") || f[1]!=s.nonce || f[2]!=s.executor_epoch || f[3]!=s.watcher_epoch ||
      f[4]!=(string)s.generation || f[5]!=(string)s.login || f[6]!=s.server || f[7]!=s.symbol ||
      f[8]!=(string)s.watcher_chart || !RecoveryInvNatural(f[9],"9223372036854775807",true) ||
      f[10]!=(string)s.position_identifier || f[11]!=(string)s.live_ticket || f[12]!=(s.side==1 ? "BUY" : "SELL") ||
      f[13]!=s.actual_entry || f[14]!=s.first_advance || f[15]!=s.second_advance || f[19+shift]!="END" ||
      (follow && f[16]!=(string)desired_stage)) return false;
   const long sequence=StringToInteger(f[9]);
   if((ulong)sequence<=after_sequence) return false;
   const bool observed=f[16+shift]=="observed";
   int stage=-1;
   if(observed) {
      if(!RecoveryInvNatural(f[17+shift],"2") || !RecoveryPriceCanonical(f[18+shift])) return false;
      stage=(int)StringToInteger(f[17+shift]);
      if(stage<desired_stage) return false;
      const double quote=StringToDouble(f[18+shift]);
      if(stage!=RecoveryPriceStage(s.side,StringToDouble(s.actual_entry),quote,quote,
                                  StringToDouble(s.first_advance),StringToDouble(s.second_advance))) return false;
   } else if(follow || f[16+shift]!="unavailable" || f[17+shift]!="-1" || f[18+shift]!="0.0000000000000000") return false;
   out.usable=true; out.observed=observed; out.sequence=sequence; out.stage=stage;
   if(observed) out.selected_quote=f[18+shift];
   return true;
}
bool RecoveryPriceParse(const string wire,const RecoveryPriceScope &s,const ulong after_sequence,
                        RecoveryPriceSignal &out) {
   return RecoveryPriceParseVersion(wire,s,after_sequence,0,out);
}
bool RecoveryPricePeakParse(const string wire,const RecoveryPriceScope &s,const ulong after_sequence,
                            const int desired_stage,RecoveryPriceSignal &out) {
   RecoveryPriceClear(out);
   if(desired_stage!=1 && desired_stage!=2) return false;
   return RecoveryPriceParseVersion(wire,s,after_sequence,desired_stage,out);
}
bool RecoveryPricePublishLocal(const RecoveryPriceScope &s,const string publisher_tag,
                                const string text,const string suffix,const string response) {
   if(text=="" || !RecoveryInvHex(publisher_tag)) return false;
   const string p="RECOVERY_PRICE\\"+s.nonce;
   const string request_path=p+suffix, temp=p+"."+publisher_tag+suffix+".tmp";
   if(FileIsExist(request_path) || FileIsExist(p+response) || FileIsExist(temp)) return false;
   uchar payload[];
   const int length=StringToCharArray(text,payload,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(length<1 || length>2048) return false;
   const int h=FileOpen(temp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const uint written=FileWriteArray(h,payload,0,length);
   FileFlush(h); FileClose(h);
   if(written==(uint)length && FileMove(temp,0,request_path,0)) return true;
   FileDelete(temp);
   return false;
}
bool RecoveryPriceRequest(const RecoveryPriceScope &s,const string publisher_tag) {
   return RecoveryPricePublishLocal(s,publisher_tag,RecoveryPriceRequestText(s),".watch",".quote");
}
bool RecoveryPriceFollow(const RecoveryPriceScope &s,const int desired_stage,const string publisher_tag) {
   return RecoveryPricePublishLocal(s,publisher_tag,RecoveryPriceFollowText(s,desired_stage),".follow",".peak");
}
bool RecoveryPriceDrop(const RecoveryPriceScope &s,const int desired_stage,const string publisher_tag) {
   return RecoveryPricePublishLocal(s,publisher_tag,RecoveryPriceFollowText(s,desired_stage),".drop",".peak");
}
bool RecoveryPriceReadVersion(const RecoveryPriceScope &s,const ulong after_sequence,
                             const int desired_stage,RecoveryPriceSignal &out) {
   RecoveryPriceClear(out);
   if(!RecoveryPriceScopeOK(s) || desired_stage<0 || desired_stage>2 ||
      (desired_stage==1 && s.first_advance=="0.0000000000000000")) return false;
   const int h=FileOpen("RECOVERY_PRICE\\"+s.nonce+(desired_stage>0 ? ".peak" : ".quote"),FILE_READ|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const ulong size=FileSize(h);
   if(size<1 || size>2048) { FileClose(h); return false; }
   uchar bytes[];
   const uint copied=FileReadArray(h,bytes,0,(int)size);
   FileClose(h);
   if(copied!=(uint)size) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]<32 && bytes[i]!=9 && bytes[i]!=10) return false;
   const string wire=CharArrayToString(bytes,0,(int)size,CP_UTF8);
   uchar canonical[];
   if(StringToCharArray(wire,canonical,0,WHOLE_ARRAY,CP_UTF8)!=(int)size+1) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]!=canonical[i]) return false;
   return RecoveryPriceParseVersion(wire,s,after_sequence,desired_stage,out);
}
bool RecoveryPriceRead(const RecoveryPriceScope &s,const ulong after_sequence,RecoveryPriceSignal &out) {
   return RecoveryPriceReadVersion(s,after_sequence,0,out);
}
bool RecoveryPricePeakRead(const RecoveryPriceScope &s,const ulong after_sequence,
                           const int desired_stage,RecoveryPriceSignal &out) {
   RecoveryPriceClear(out);
   if(desired_stage!=1 && desired_stage!=2) return false;
   return RecoveryPriceReadVersion(s,after_sequence,desired_stage,out);
}
#endif
