#property strict
#property version "1.00"
#property description "Read-only mailbox diagnosis. No trading or mailbox writes."

const string DIR="AI_IMPULS_TRADFI\\commands\\";
const int MAX_BYTES=8192;
const int MAX_LINES=64;
string keys[],vals[];

bool ValidId(string s) {
   if(StringLen(s)!=24) return false;
   for(int i=0;i<24;i++)
      if(StringFind("0123456789abcdef",StringSubstr(s,i,1))<0) return false;
   return true;
}
string Get(string key) {
   for(int i=0;i<ArraySize(keys);i++) if(keys[i]==key) return vals[i];
   return "";
}
void ClearFields() { ArrayResize(keys,0); ArrayResize(vals,0); }
bool AddLine(string line) {
   if(StringLen(line)==0) return true;
   int split=StringFind(line,"="), n=ArraySize(keys);
   if(split<=0 || n>=32) return false;
   string key=StringSubstr(line,0,split);
   for(int j=0;j<n;j++) if(keys[j]==key) return false;
   ArrayResize(keys,n+1); ArrayResize(vals,n+1);
   keys[n]=key; vals[n]=StringSubstr(line,split+1);
   return true;
}
void OnStart() {
   Print("TFDIAG BEGIN v1.00 READ ONLY; no requests are executed");
   string name,next;
   ResetLastError();
   long search=FileFindFirst(DIR+"*.cmd",name,FILE_COMMON);
   int err=GetLastError();
   PrintFormat("TFDIAG FIND common=1 ok=%d error=%d",search!=INVALID_HANDLE,err);
   if(search==INVALID_HANDLE) return;
   ResetLastError();
   bool more=FileFindNext(search,next);
   err=GetLastError();
   FileFindClose(search);
   PrintFormat("TFDIAG FIND_NEXT more=%d error=%d",more,err);
   if(more) { Print("TFDIAG STOP multiple requests"); return; }
   if(err!=0 && err!=ERR_FILE_NOT_EXIST) {
      Print("TFDIAG STOP enumeration error"); return;
   }
   if(StringLen(name)!=28 || StringSubstr(name,24)!=".cmd" ||
      !ValidId(StringSubstr(name,0,24))) {
      Print("TFDIAG STOP invalid filename"); return;
   }
   string stem=StringSubstr(name,0,24), path=DIR+name;
   // Binary preflight: identify PROFIT without relying on FileReadString line endings.
   // No raw text, filename, id or account value is printed.
   ResetLastError();
   int h=FileOpen(path,FILE_READ|FILE_BIN|FILE_COMMON);
   err=GetLastError();
   PrintFormat("TFDIAG BINARY_OPEN ok=%d error=%d",h!=INVALID_HANDLE,err);
   if(h==INVALID_HANDLE) return;
   ulong size=FileSize(h);
   if(size==0 || size>(ulong)MAX_BYTES) {
      FileClose(h); Print("TFDIAG STOP binary size limit"); return;
   }
   uchar bytes[];
   ArrayResize(bytes,(int)size);
   ResetLastError();
   uint got=FileReadArray(h,bytes,0,(int)size);
   err=GetLastError();
   FileClose(h);
   PrintFormat("TFDIAG BINARY_READ bytes=%d error=%d",(int)got,err);
   if(got!=(uint)size || err!=0) { Print("TFDIAG STOP short/read error"); return; }
   int lf=0,crlf=0;
   for(int i=0;i<(int)got;i++) {
      if(bytes[i]==0) { Print("TFDIAG STOP embedded NUL"); return; }
      if(bytes[i]==10) { lf++; if(i>0 && bytes[i-1]==13) crlf++; }
   }
   bool bom=(got>=3 && bytes[0]==239 && bytes[1]==187 && bytes[2]==191);
   int offset=bom?3:0;
   string text=CharArrayToString(bytes,offset,(int)got-offset,CP_UTF8);
   uchar encoded[];
   int count=StringToCharArray(text,encoded,0,WHOLE_ARRAY,CP_UTF8);
   bool utf8=(count==(int)got-offset+1);
   if(utf8) for(int i=0;i<count-1;i++)
      if(encoded[i]!=bytes[i+offset]) { utf8=false; break; }
   PrintFormat("TFDIAG BYTES LF=%d CRLF=%d BOM=%d UTF8_roundtrip=%d",lf,crlf,bom,utf8);
   if(!utf8) { Print("TFDIAG STOP encoding"); return; }
   string lines[];
   int n=StringSplit(text,10,lines);
   if(n<1 || n>MAX_LINES) { Print("TFDIAG STOP raw line limit"); return; }
   ClearFields();
   bool valid=true;
   for(int i=0;i<n;i++) {
      string line=lines[i];
      int len=StringLen(line);
      if(len>0 && StringGetCharacter(line,len-1)==13) line=StringSubstr(line,0,len-1);
      if(!AddLine(line)) { valid=false; break; }
   }
   bool profit=(Get("kind")=="PROFIT");
   PrintFormat("TFDIAG RAW_PARSE valid=%d fields=%d idlen=%d ValidId=%d match=%d PROFIT=%d",
      valid,ArraySize(keys),StringLen(Get("id")),ValidId(Get("id")),Get("id")==stem,profit);
   if(!valid || !profit) { Print("TFDIAG STOP malformed or non-PROFIT"); return; }
   // Exact production EA 1.02 text-read flags, delimiter and codepage.
   ResetLastError();
   h=FileOpen(path,FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON,0,CP_UTF8);
   err=GetLastError();
   PrintFormat("TFDIAG EA_TEXT_OPEN ok=%d error=%d",h!=INVALID_HANDLE,err);
   if(h==INVALID_HANDLE) return;
   size=FileSize(h);
   if(size==0 || size>(ulong)MAX_BYTES || size!=(ulong)got) {
      FileClose(h); Print("TFDIAG STOP text size changed/limit"); return;
   }
   ClearFields();
   int reads=0,firstlen=0,embeddedlf=0,readerr=0;
   valid=true;
   while(!FileIsEnding(h) && reads<MAX_LINES && !IsStopped()) {
      if(FileSize(h)>(ulong)MAX_BYTES) { valid=false; break; }
      ResetLastError();
      string line=FileReadString(h);
      readerr=GetLastError();
      reads++;
      if(reads==1) firstlen=StringLen(line);
      if(StringFind(line,"\n")>=0) embeddedlf++;
      if(readerr!=0 || !AddLine(line)) { valid=false; break; }
   }
   bool ended=FileIsEnding(h);
   FileClose(h);
   if(!ended || IsStopped()) valid=false;
   PrintFormat("TFDIAG EA_READ lines=%d firstlen=%d embeddedLF=%d error=%d ended=%d",
      reads,firstlen,embeddedlf,readerr,ended);
   PrintFormat("TFDIAG EA_PARSE valid=%d fields=%d idlen=%d ValidId=%d match=%d",
      valid,ArraySize(keys),StringLen(Get("id")),ValidId(Get("id")),Get("id")==stem);
   if(Get("kind")=="PROFIT") Print("TFDIAG EA_KIND PROFIT");
   else Print("TFDIAG EA_KIND missing_or_other; STOP");
   Print("TFDIAG END READ ONLY; no request executed, changed or deleted");
}
