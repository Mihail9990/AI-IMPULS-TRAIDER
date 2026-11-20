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
          RecoveryPriceCanonical(s.first_advance) && RecoveryPriceCanonical(s.second_advance) &&
          StringToDouble(s.first_advance)<StringToDouble(s.second_advance);
}
string RecoveryPriceRequestText(const RecoveryPriceScope &s) {
   if(!RecoveryPriceScopeOK(s)) return "";
   return "RECOVERY_PRICE_REQUEST_V1\t"+s.nonce+"\t"+s.executor_epoch+"\t"+s.watcher_epoch+"\t"+
          (string)s.generation+"\t"+(string)s.login+"\t"+s.server+"\t"+s.symbol+"\t"+
          (string)s.position_identifier+"\t"+(string)s.live_ticket+"\t"+(s.side==1 ? "BUY" : "SELL")+"\t"+
          s.actual_entry+"\t"+s.first_advance+"\t"+s.second_advance+"\t"+(string)s.watcher_chart+"\tEND\n";
}
bool RecoveryPriceParse(const string wire,const RecoveryPriceScope &s,const ulong after_sequence,
                        RecoveryPriceSignal &out) {
   RecoveryPriceClear(out);
   const int n=StringLen(wire);
   if(n<2 || n>2048 || !RecoveryPriceScopeOK(s) || StringFind(wire,"\n")!=n-1) return false;
   string f[];
   if(StringSplit(StringSubstr(wire,0,n-1),9,f)!=20 ||
      f[0]!="RECOVERY_PRICE_V1" || f[1]!=s.nonce || f[2]!=s.executor_epoch || f[3]!=s.watcher_epoch ||
      f[4]!=(string)s.generation || f[5]!=(string)s.login || f[6]!=s.server || f[7]!=s.symbol ||
      f[8]!=(string)s.watcher_chart || !RecoveryInvNatural(f[9],"9223372036854775807",true) ||
      f[10]!=(string)s.position_identifier || f[11]!=(string)s.live_ticket || f[12]!=(s.side==1 ? "BUY" : "SELL") ||
      f[13]!=s.actual_entry || f[14]!=s.first_advance || f[15]!=s.second_advance || f[19]!="END") return false;
   const long sequence=StringToInteger(f[9]);
   if((ulong)sequence<=after_sequence) return false;
   const bool observed=f[16]=="observed";
   int stage=-1;
   if(observed) {
      if(!RecoveryInvNatural(f[17],"2") || !RecoveryPriceCanonical(f[18])) return false;
      stage=(int)StringToInteger(f[17]);
      const double quote=StringToDouble(f[18]);
      if(stage!=RecoveryPriceStage(s.side,StringToDouble(s.actual_entry),quote,quote,
                                  StringToDouble(s.first_advance),StringToDouble(s.second_advance))) return false;
   } else if(f[16]!="unavailable" || f[17]!="-1" || f[18]!="0.0000000000000000") return false;
   out.usable=true; out.observed=observed; out.sequence=sequence; out.stage=stage;
   if(observed) out.selected_quote=f[18];
   return true;
}
bool RecoveryPriceRequest(const RecoveryPriceScope &s,const string publisher_tag) {
   const string text=RecoveryPriceRequestText(s);
   if(text=="" || !RecoveryInvHex(publisher_tag)) return false;
   const string p="RECOVERY_PRICE\\"+s.nonce;
   const string final=p+".watch", temp=p+"."+publisher_tag+".watch.tmp";
   if(FileIsExist(final) || FileIsExist(p+".quote") || FileIsExist(temp)) return false;
   uchar payload[];
   const int length=StringToCharArray(text,payload,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(length<1 || length>2048) return false;
   const int h=FileOpen(temp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   const uint written=FileWriteArray(h,payload,0,length);
   FileFlush(h); FileClose(h);
   if(written==(uint)length && FileMove(temp,0,final,0)) return true;
   FileDelete(temp);
   return false;
}
bool RecoveryPriceRead(const RecoveryPriceScope &s,const ulong after_sequence,RecoveryPriceSignal &out) {
   RecoveryPriceClear(out);
   if(!RecoveryPriceScopeOK(s)) return false;
   const int h=FileOpen("RECOVERY_PRICE\\"+s.nonce+".quote",FILE_READ|FILE_BIN);
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
   return RecoveryPriceParse(wire,s,after_sequence,out);
}
#endif
