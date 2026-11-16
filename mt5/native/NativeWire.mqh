#ifndef AI_IMPULS_NATIVE_WIRE
#define AI_IMPULS_NATIVE_WIRE
string NativeQ(string s) {
   string out="\"",hex="0123456789abcdef";
   for(int i=0;i<StringLen(s);i++) {
      ushort c=StringGetCharacter(s,i);
      if(c==34) out+="\\\"";
      else if(c==92) out+="\\\\";
      else if(c<32) out+="\\u00"+StringSubstr(hex,c/16,1)+StringSubstr(hex,c%16,1);
      else out+=StringSubstr(s,i,1);
   }
   return out+"\"";
}
string NativeN(double n) { return NativeQ(DoubleToString(n,10)); }
string NativeU(ulong n) { return NativeQ((string)n); }
bool NativeId(string s) {
   if(StringLen(s)<1 || StringLen(s)>20) return false;
   if(StringSubstr(s,0,1)=="0") return false;
   for(int i=0;i<StringLen(s);i++)
      if(StringFind("0123456789",StringSubstr(s,i,1))<0) return false;
   // MQL StringToInteger is signed: reject unsupported > LONG_MAX IDs.
   return StringToInteger(s)>0 && (string)StringToInteger(s)==s;
}
bool NativeFolder(string s) {
   if(StringLen(s)<1 || StringLen(s)>60) return false;
   for(int i=0;i<StringLen(s);i++)
      if(StringFind("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",StringSubstr(s,i,1))<0) return false;
   return true;
}
// One writer per destination, enforced by the EA's exclusive open lock file.
// FileFlush+FileMove is publication, NOT a transaction with the broker.
// Failure leaves the previous observation; consumers must check freshness.
bool NativePublish(string folder,string name,string body) {
   uchar bytes[];
   int count=StringToCharArray(body,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
   if(count<=0) return false;
   int h=FileOpen(folder+"\\"+name+".tmp",FILE_WRITE|FILE_BIN|FILE_COMMON);
   if(h==INVALID_HANDLE) return false;
   uint written=FileWriteArray(h,bytes,0,count);
   FileFlush(h); FileClose(h);
   if(written!=(uint)count) return false;
   return FileMove(folder+"\\"+name+".tmp",FILE_COMMON,folder+"\\"+name,FILE_COMMON|FILE_REWRITE);
}
bool NativeIdentity(long login,string server) {
   return login>0 && server!="" && AccountInfoInteger(ACCOUNT_LOGIN)==login &&
          AccountInfoString(ACCOUNT_SERVER)==server;
}
bool NativeManifest(string folder,long login,string server,string symbol,ulong &ids[],string &cycle) {
 int h=FileOpen(folder+"\\native-watch.txt",FILE_READ|FILE_BIN|FILE_COMMON|FILE_SHARE_READ);
 if(h==INVALID_HANDLE)return false;
 ulong size=FileSize(h);if(size<1 || size>16000){FileClose(h);return false;}
 uchar bytes[];ArrayResize(bytes,(int)size);uint read=FileReadArray(h,bytes,0,(int)size);FileClose(h);
 if(read!=size)return false;
 string body=CharArrayToString(bytes,0,(int)size,CP_UTF8),lines[],header[];
 int n=StringSplit(body,'\n',lines);
 if(n<2 || n>130 || lines[n-1]!="END" || StringSplit(lines[0],'|',header)!=4)return false;
 if(header[0]!=(string)login || header[1]!=server || header[2]!=symbol || !NativeId(header[3]))return false;
 ulong parsed[];ArrayResize(parsed,n-2);
 for(int i=1;i<n-1;i++) {
  if(!NativeId(lines[i]))return false;parsed[i-1]=(ulong)StringToInteger(lines[i]);
  for(int j=0;j<i-1;j++)if(parsed[j]==parsed[i-1])return false;
 }
 ArrayResize(ids,n-2);for(int i=0;i<n-2;i++)ids[i]=parsed[i];cycle=header[3];return true;
}
#endif
