#ifndef RECOVERY_PYTHON_SHARED_MQH
#define RECOVERY_PYTHON_SHARED_MQH
input long ExpectedLogin=0;
input string ExpectedServer="";
input string ExpectedSymbol="";
input string PythonScope="";

string Box="",Instance="";
int RoleLock=INVALID_HANDLE;
ulong Sequence=0;
bool PyHex(const string s) {
 if(StringLen(s)!=32)return false;
 for(int i=0;i<32;i++)if(StringFind("0123456789abcdef",StringSubstr(s,i,1))<0)return false;
 return true;
}
bool PyField(const string s) {
 const int n=StringLen(s);if(n<1 || n>100)return false;
 for(int i=0;i<n;i++)if(StringGetCharacter(s,i)<32 || StringGetCharacter(s,i)==127)return false;
 return true;
}
string PyNumber(const double x){return DoubleToString(x==0?0.0:x,16);}
bool PyNat(const string s,ulong &v) {
 const int n=StringLen(s);v=0;
 if(n<1 || n>20 || (n>1 && StringGetCharacter(s,0)==48) ||
    (n==20 && StringCompare(s,"18446744073709551615")>0))return false;
 for(int i=0;i<n;i++){ushort k=StringGetCharacter(s,i);if(k<48 || k>57)return false;v=v*10+k-48;}
 return true;
}
bool PyUnits(const double x,const double step,long &u) {
 if(!MathIsValidNumber(x) || !MathIsValidNumber(step) || step<=0 || x<0 || x/step>1e12)return false;
 u=(long)MathRound(x/step);
 const double err=2.2204460492503131e-16*4*MathMax(1,x);
 return err<step*0.00001 && MathAbs(x-u*step)<=err;
}
bool PySession() {return TerminalInfoInteger(TERMINAL_CONNECTED)!=0 && AccountInfoInteger(ACCOUNT_LOGIN)==ExpectedLogin &&
    AccountInfoString(ACCOUNT_SERVER)==ExpectedServer && _Symbol==ExpectedSymbol;}
bool PyGrid(double &tick,double &step) {
 return SymbolInfoDouble(ExpectedSymbol,SYMBOL_TRADE_TICK_SIZE,tick) && tick>0 &&
        SymbolInfoDouble(ExpectedSymbol,SYMBOL_VOLUME_STEP,step) && step>0;
}
string PySha(const string s) {
 uchar bytes[],key[],digest[];const int n=StringToCharArray(s,bytes,0,WHOLE_ARRAY,CP_UTF8);
 if(n<2 || ArrayResize(bytes,n-1)!=n-1 || CryptEncode(CRYPT_HASH_SHA256,bytes,key,digest)!=32)return "";
 string out="";for(int i=0;i<16;i++)out+=StringFormat("%02x",digest[i]);return out;
}
string PyBinding() {return PythonScope+"\t"+(string)ExpectedLogin+"\t"+ExpectedServer+"\t"+ExpectedSymbol;}
bool PyInit(const string role) {
 if(ExpectedLogin<=0 || !PyHex(PythonScope) || !PyField(ExpectedServer) || !PyField(ExpectedSymbol) ||
    ExpectedSymbol!=_Symbol || ChartID()<=0)return false;
 Box="RECOVERY_PY\\"+PythonScope+"\\";
 FolderCreate("RECOVERY_PY");FolderCreate("RECOVERY_PY\\"+PythonScope);
 RoleLock=FileOpen(Box+role+".lock",FILE_READ|FILE_WRITE|FILE_BIN);
 if(RoleLock==INVALID_HANDLE)return false;
 // The local serial survives fast reattachment. Never reuse the predecessor's
 // instance or its registration if this counter is unreadable.
 const string path=Box+role+".serial";
 ulong serial=0;string saved;
 if(FileIsExist(path) && (!PyRead(path,saved,128) || !PyNat(saved,serial) || serial==ULONG_MAX))return false;
 serial++;
 Instance=PySha(PyBinding()+"\t"+role+"\t"+(string)ChartID()+"\t"+(string)serial+"\t"+(string)GetMicrosecondCount());
 return PyHex(Instance) && PyWrite(path,(string)serial);
}
void PyClose(){if(RoleLock!=INVALID_HANDLE)FileClose(RoleLock);RoleLock=INVALID_HANDLE;}
bool PyWrite(const string path,const string wire,const bool replace=true) {
 const string tmp=path+"."+Instance+".tmp";
 uchar bytes[];const int count=StringToCharArray(wire,bytes,0,WHOLE_ARRAY,CP_UTF8)-1;
 if(count<=0)return false;
 const int h=FileOpen(tmp,FILE_WRITE|FILE_BIN);
 if(h==INVALID_HANDLE)return false;
 const uint n=FileWriteArray(h,bytes,0,count);FileFlush(h);FileClose(h);
 if(n==(uint)count && FileMove(tmp,0,path,replace?FILE_REWRITE:0))return true;
 FileDelete(tmp);return false;
}
bool PyRead(const string path,string &wire,const int limit) {
 wire="";
 const int h=FileOpen(path,FILE_READ|FILE_BIN|FILE_SHARE_READ);
 if(h==INVALID_HANDLE)return false;
 const ulong size=FileSize(h);
 if(size<1 || size>(ulong)limit){FileClose(h);return false;}
 uchar b[];const uint n=FileReadArray(h,b,0,(int)size);FileClose(h);if(n!=(uint)size)return false;
 for(int i=0;i<(int)size;i++)if(b[i]<32 && b[i]!=9 && b[i]!=10)return false;
 wire=CharArrayToString(b,0,(int)size,CP_UTF8);uchar canonical[];
 if(StringToCharArray(wire,canonical,0,WHOLE_ARRAY,CP_UTF8)!=(int)size+1)return false;
 for(int i=0;i<(int)size;i++)if(b[i]!=canonical[i])return false;
 return true;
}
string PyHeader(const string schema) {
 return schema+"\t"+PyBinding()+"\t"+Instance+"\t"+(string)(++Sequence)+"\t"+(string)GetTickCount64();
}
#endif
