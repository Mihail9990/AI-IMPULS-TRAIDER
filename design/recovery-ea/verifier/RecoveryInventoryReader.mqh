#ifndef RECOVERY_INVENTORY_READER_MQH
#define RECOVERY_INVENTORY_READER_MQH
// Offline MQL response reader. No broker operations; not an authorization gate.

struct RecoveryInventoryScope {
   string nonce;
   string executor_epoch;
   string verifier_epoch;
   long generation;
   long login;
   string server;
   string symbol;
   long magic;
   long verifier_chart;
};

struct RecoveryInventorySnapshot {
   bool usable;       // True only if parsing and scope matching succeeded.
   bool complete;     // Successful parsing can still report unavailable.
   string reason;
   long sequence;
   int position_count;
   int order_count;
   string rows[];     // P then O; signed/unsigned IDs and prices remain wire text.
};

void RecoveryInvClear(RecoveryInventorySnapshot &snapshot) {
   snapshot.usable=false; snapshot.complete=false; snapshot.reason="";
   snapshot.sequence=0; snapshot.position_count=0; snapshot.order_count=0;
   ArrayFree(snapshot.rows);
}

bool RecoveryInvHex(const string token) {
   if(StringLen(token)!=32) return false;
   for(int i=0;i<32;i++)
      if(StringFind("0123456789abcdef",StringSubstr(token,i,1))<0) return false;
   return true;
}

bool RecoveryInvContext(const string value) {
   if(StringLen(value)==0 || StringLen(value)>100) return false;
   for(int i=0;i<StringLen(value);i++) {
      ushort c=StringGetCharacter(value,i);
      if(c<32 || c==127 || c==0xfffd) return false;
   }
   return true;
}

bool RecoveryInvScopeOK(const RecoveryInventoryScope &scope) {
   return RecoveryInvHex(scope.nonce) && RecoveryInvHex(scope.executor_epoch) &&
          RecoveryInvHex(scope.verifier_epoch) && scope.executor_epoch!=scope.verifier_epoch &&
          scope.generation>=0 && scope.login>0 && scope.magic>0 && scope.verifier_chart>0 &&
          RecoveryInvContext(scope.server) && RecoveryInvContext(scope.symbol);
}

bool RecoveryInvNatural(const string value,const string max,
                        const bool positive=false) {
   int n=StringLen(value);
   if(n<1 || n>StringLen(max) || (n>1 && StringGetCharacter(value,0)==48)) return false;
   if(positive && value=="0") return false;
   for(int i=0;i<n;i++) {
      ushort c=StringGetCharacter(value,i);
      if(c<48 || c>57) return false;
   }
   return n<StringLen(max) || StringCompare(value,max)<=0;
}

bool RecoveryInvFixed(const string value,const bool positive=false) {
   const int n=StringLen(value), dot=n-17;
   if(dot<1 || dot>15 || StringGetCharacter(value,dot)!=46 ||
      (dot>1 && StringGetCharacter(value,0)==48)) return false;
   bool nonzero=false;
   for(int i=0;i<n;i++) {
      if(i==dot) continue;
      ushort c=StringGetCharacter(value,i);
      if(c<48 || c>57) return false;
      if(c!=48) nonzero=true;
   }
   return !positive || nonzero;
}

bool RecoveryInvPosition(const string &fields[]) {
   const string max="18446744073709551615";
   return ArraySize(fields)==9 && fields[0]=="P" &&
          RecoveryInvNatural(fields[1],max,true) && RecoveryInvNatural(fields[2],max,true) &&
          RecoveryInvNatural(fields[3],max) && (fields[4]=="BUY" || fields[4]=="SELL") &&
          RecoveryInvFixed(fields[5],true) && RecoveryInvFixed(fields[6],true) &&
          RecoveryInvFixed(fields[7]) && RecoveryInvFixed(fields[8]);
}

bool RecoveryInvOrder(const string &fields[]) {
   const string max="18446744073709551615";
   if(ArraySize(fields)!=12 || fields[0]!="O" ||
      !RecoveryInvNatural(fields[1],max,true) || !RecoveryInvNatural(fields[2],max) ||
      !RecoveryInvNatural(fields[3],max) || !RecoveryInvNatural(fields[4],max) ||
      !RecoveryInvNatural(fields[5],max) || !RecoveryInvFixed(fields[6],true)) return false;
   for(int i=7;i<12;i++) if(!RecoveryInvFixed(fields[i])) return false;
   // Both numbers have a fixed 16-place fraction and canonical integer part.
   int n_current=StringLen(fields[7]), n_initial=StringLen(fields[6]);
   return n_current<n_initial || (n_current==n_initial && StringCompare(fields[7],fields[6])<=0);
}

bool RecoveryInvParse(const string wire,const RecoveryInventoryScope &expected,
                      RecoveryInventorySnapshot &snapshot) {
   RecoveryInvClear(snapshot);
   if(StringLen(wire)>65536 || !RecoveryInvScopeOK(expected)) return false;
   int start=StringFind(wire,"\n");
   if(start<1) return false;
   string fields[];
   if(StringSplit(StringSubstr(wire,0,start),9,fields)!=15 ||
      fields[0]!="RECOVERY_INVENTORY_V1" ||
      fields[1]!=expected.nonce || fields[2]!=expected.executor_epoch ||
      fields[3]!=expected.verifier_epoch || fields[4]!=(string)expected.generation ||
      fields[5]!=(string)expected.login || fields[6]!=expected.server ||
      fields[7]!=expected.symbol || fields[8]!=(string)expected.magic ||
      fields[9]!=(string)expected.verifier_chart ||
      !RecoveryInvNatural(fields[10],"9223372036854775807",true) ||
      !RecoveryInvNatural(fields[13],"128") || !RecoveryInvNatural(fields[14],"128")) return false;

   const int positions=(int)StringToInteger(fields[13]), orders=(int)StringToInteger(fields[14]);
   const long sequence=StringToInteger(fields[10]);
   const bool complete=fields[11]=="complete";
   const string reason=fields[12];
   if(complete ? reason!="ok" :
      (fields[11]!="unavailable" || positions!=0 || orders!=0 ||
       (reason!="read" && reason!="changed" && reason!="limit" && reason!="session"))) return false;

   string identifiers[128], position_tickets[128], order_tickets[128], rows[];
   if(ArrayResize(rows,positions+orders)!=positions+orders) return false;
   start++;
   for(int i=0;i<positions+orders;i++) {
      int next=StringFind(wire,"\n",start);
      if(next<=start) return false;
      string row=StringSubstr(wire,start,next-start);
      start=next+1;
      if(StringSplit(row,9,fields)<1) return false;
      if(i<positions) {
         if(!RecoveryInvPosition(fields)) return false;
         for(int j=0;j<i;j++)
            if(identifiers[j]==fields[1] || position_tickets[j]==fields[2]) return false;
         identifiers[i]=fields[1]; position_tickets[i]=fields[2];
      } else {
         if(!RecoveryInvOrder(fields)) return false;
         int index=i-positions;
         for(int j=0;j<index;j++) if(order_tickets[j]==fields[1]) return false;
         order_tickets[index]=fields[1];
      }
      rows[i]=row;
   }
   if(StringSubstr(wire,start)!="END\n") return false;
   if(positions+orders>0 && ArrayCopy(snapshot.rows,rows)!=positions+orders) {
      RecoveryInvClear(snapshot);
      return false;
   }
   snapshot.usable=true; snapshot.complete=complete; snapshot.reason=reason;
   snapshot.sequence=sequence; snapshot.position_count=positions; snapshot.order_count=orders;
   return true;
}

bool RecoveryInvRead(const RecoveryInventoryScope &expected,RecoveryInventorySnapshot &snapshot) {
   RecoveryInvClear(snapshot);
   if(!RecoveryInvScopeOK(expected)) return false;
   int h=FileOpen("RECOVERY_VERIFY\\"+expected.nonce+".snapshot",FILE_READ|FILE_BIN);
   if(h==INVALID_HANDLE) return false;
   ulong size=FileSize(h);
   if(size<1 || size>65536) { FileClose(h); return false; }
   uchar bytes[];
   uint copied=FileReadArray(h,bytes,0,(int)size);
   FileClose(h);
   if(copied!=(uint)size) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]<32 && bytes[i]!=9 && bytes[i]!=10) return false;
   string wire=CharArrayToString(bytes,0,(int)size,CP_UTF8);
   uchar canonical[];
   if(StringToCharArray(wire,canonical,0,WHOLE_ARRAY,CP_UTF8)!=(int)size+1) return false;
   for(int i=0;i<(int)size;i++) if(bytes[i]!=canonical[i]) return false;
   return RecoveryInvParse(wire,expected,snapshot);
}
#endif
