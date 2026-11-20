#ifndef RECOVERY_PANEL_COMMAND_WIRE_MQH
#define RECOVERY_PANEL_COMMAND_WIRE_MQH
#include "../verifier/RecoveryInventoryReader.mqh"

enum RecoveryPanelOperation { RPC_NONE=0,RPC_MANUAL_WAIT,RPC_MANUAL_CANCEL,RPC_CONFIGURE_RANGES };
struct RecoveryPanelRange { int range_id,side; long price_ticks; };
struct RecoveryPanelCommand {
 RecoveryPanelOperation operation;
 string command_id,content_sha256;
 int range_count;
 RecoveryPanelRange ranges[2];
};
struct RecoveryPanelCommandScope { string scope_id,boot_id,context_id,generation_id; };

void RecoveryPanelCommandClear(RecoveryPanelCommand &out) {
 out.operation=RPC_NONE;out.command_id="";out.content_sha256="";out.range_count=0;
 for(int i=0;i<2;i++) {
    out.ranges[i].range_id=0;out.ranges[i].side=0;out.ranges[i].price_ticks=0;
 }
}

bool RecoveryPanelCommandHex(const string value,const int length) {
 if(StringLen(value)!=length)return false;
 for(int i=0;i<length;i++)
    if(StringFind("0123456789abcdef",StringSubstr(value,i,1))<0)return false;
 return true;
}
bool RecoveryPanelCommandUUID(const string value) {
 if(StringLen(value)!=36)return false;
 for(int i=0;i<36;i++) {
    const string c=StringSubstr(value,i,1);
    if(i==8 || i==13 || i==18 || i==23) { if(c!="-")return false; }
    else if(StringFind("0123456789abcdef",c)<0)return false;
 }
 return true;
}
bool RecoveryPanelTake(const string wire,int &pos,const string literal) {
 const int size=StringLen(literal);
 if(pos+size>StringLen(wire) || StringSubstr(wire,pos,size)!=literal)return false;
 pos+=size;
 return true;
}
bool RecoveryPanelCreatedAt(const string wire,int &pos) {
 const bool minus=RecoveryPanelTake(wire,pos,"-");
 const int start=pos,size=StringLen(wire);
 while(pos<size) {
    const ushort c=StringGetCharacter(wire,pos);
    if(c<48 || c>57)break;
    pos++;
    if(pos-start>19)return false;
 }
 // Canonical signed int64 only; deliberately no age/time comparison.
 return RecoveryInvNatural(StringSubstr(wire,start,pos-start),
                           minus ? "9223372036854775808" : "9223372036854775807",minus);
}
bool RecoveryPanelPrice(const string value,const double quantum,long &out) {
 out=0;
 const int n=StringLen(value);
 if(n<1 || n>32 || !MathIsValidNumber(quantum) || quantum<=0)return false;
 int dot=-1;
 for(int i=0;i<n;i++) {
    const ushort c=StringGetCharacter(value,i);
    if(c==46) {
       if(dot!=-1 || i==0 || i==n-1)return false;
       dot=i;
    } else if(c<48 || c>57)return false;
 }
 const int integers=dot<0 ? n : dot, fraction=dot<0 ? 0 : n-dot-1;
 if(integers>15 || fraction>16 || (integers>1 && StringGetCharacter(value,0)==48))return false;

 // Establish the broker's decimal step, then divide the JSON decimal digits
 // directly. Never parse the submitted price as a binary double, which could
 // silently round off-grid tails (e.g. 4000.0000000000000001 at step .01).
 const string q=DoubleToString(quantum,16);
 if(!RecoveryInvFixed(q,true) || StringToDouble(q)!=quantum)return false;
 const int q_dot=StringLen(q)-17;
 int q_end=StringLen(q);
 while(q_end>q_dot+1 && StringGetCharacter(q,q_end-1)==48)q_end--;
 const int places=q_end-q_dot-1;
 const long limit=1000000000000;
 long divisor=0;
 for(int i=0;i<q_end;i++) {
    if(i==q_dot)continue;
    divisor=divisor*10+(long)(StringGetCharacter(q,i)-48);
    if(divisor>limit)return false;
 }
 if(divisor==0)return false;
 for(int k=places;k<fraction;k++)
    if(StringGetCharacter(value,dot+1+k)!=48)return false;
 long remainder=0, units=0;
 for(int j=0;j<integers+places;j++) {
    int digit=0;
    if(j<integers)digit=(int)(StringGetCharacter(value,j)-48);
    else if(j-integers<fraction)digit=(int)(StringGetCharacter(value,dot+1+j-integers)-48);
    remainder=remainder*10+digit;
    units=units*10+remainder/divisor;
    remainder%=divisor;
    if(units>limit)return false;
 }
 if(remainder!=0 || units<=0)return false;
 out=units;
 return true;
}

bool RecoveryPanelReadRange(const string wire,int &pos,const double quantum,RecoveryPanelRange &out) {
 if(!RecoveryPanelTake(wire,pos,"{\"price\":\""))return false;
 const int start=pos;
 while(pos<StringLen(wire) && StringGetCharacter(wire,pos)!=34 && pos-start<=32)pos++;
 if(pos==StringLen(wire) || pos-start>32 || !RecoveryPanelPrice(StringSubstr(wire,start,pos-start),quantum,out.price_ticks) ||
    !RecoveryPanelTake(wire,pos,"\",\"range_id\":"))return false;
 if(RecoveryPanelTake(wire,pos,"1"))out.range_id=1;
 else if(RecoveryPanelTake(wire,pos,"2"))out.range_id=2;
 else return false;
 if(!RecoveryPanelTake(wire,pos,",\"side\":\""))return false;
 if(RecoveryPanelTake(wire,pos,"BUY"))out.side=1;
 else if(RecoveryPanelTake(wire,pos,"SELL"))out.side=-1;
 else return false;
 return RecoveryPanelTake(wire,pos,"\"}");
}

bool RecoveryPanelDigest(const string payload,string &hex) {
 hex="";
 uchar bytes[],key[],hash[];
 const int n=StringLen(payload);
 if(n<=0 || StringToCharArray(payload,bytes,0,n,CP_UTF8)!=n ||
    ArraySize(bytes)!=n || CryptEncode(CRYPT_HASH_SHA256,bytes,key,hash)!=32 ||
    ArraySize(hash)!=32)return false;
 const string digits="0123456789abcdef";
 for(int i=0;i<32;i++) {
    const int value=(int)hash[i];
    hex+=StringSubstr(digits,value/16,1)+StringSubstr(digits,value%16,1);
 }
 return true;
}

// Purpose-built recognizer, not an extensible JSON parser. Only the exact
// canonical wire produced by channel.encode() and bound to the current boot.
bool RecoveryDecodePanelCommand(const string wire,const string expected_command_id,
    const RecoveryPanelCommandScope &scope,const double price_tick,RecoveryPanelCommand &out) {
 RecoveryPanelCommandClear(out);
 const int size=StringLen(wire);
 if(size<1 || size>1024 ||
    !RecoveryPanelCommandUUID(expected_command_id) || !RecoveryPanelCommandUUID(scope.scope_id) ||
    !RecoveryPanelCommandUUID(scope.boot_id) || !RecoveryPanelCommandUUID(scope.context_id) ||
    !RecoveryPanelCommandUUID(scope.generation_id))return false;
 int at=0;
 if(!RecoveryPanelTake(wire,at,"{\"boot_id\":\""+scope.boot_id+"\",\"command_id\":\""+
                              expected_command_id+"\""))return false;
 const int digest_start=at;
 if(!RecoveryPanelTake(wire,at,",\"content_sha256\":\""))return false;
 const string expected=StringSubstr(wire,at,64);
 if(!RecoveryPanelCommandHex(expected,64))return false;
 at+=64;
 if(!RecoveryPanelTake(wire,at,"\""))return false;
 const int digest_end=at;
 if(!RecoveryPanelTake(wire,at,",\"context_id\":\""+scope.context_id+"\",\"created_at\":") ||
    !RecoveryPanelCreatedAt(wire,at) ||
    !RecoveryPanelTake(wire,at,",\"generation_id\":\""+scope.generation_id+"\",\"operation\":\""))return false;
 RecoveryPanelCommand parsed;
 RecoveryPanelCommandClear(parsed);
 if(RecoveryPanelTake(wire,at,"manual_wait"))parsed.operation=RPC_MANUAL_WAIT;
 else if(RecoveryPanelTake(wire,at,"manual_cancel"))parsed.operation=RPC_MANUAL_CANCEL;
 else if(RecoveryPanelTake(wire,at,"configure_ranges"))parsed.operation=RPC_CONFIGURE_RANGES;
 else return false;
 if(!RecoveryPanelTake(wire,at,"\",\"payload\":{"))return false;
 if(parsed.operation==RPC_CONFIGURE_RANGES) {
    if(!RecoveryPanelTake(wire,at,"\"ranges\":[") ||
       !RecoveryPanelReadRange(wire,at,price_tick,parsed.ranges[0]))return false;
    parsed.range_count=1;
    if(RecoveryPanelTake(wire,at,",")) {
       if(!RecoveryPanelReadRange(wire,at,price_tick,parsed.ranges[1]) ||
          parsed.ranges[0].range_id==parsed.ranges[1].range_id)return false;
       parsed.range_count=2;
    }
    if(!RecoveryPanelTake(wire,at,"]"))return false;
 }
 if(!RecoveryPanelTake(wire,at,"},\"scope_id\":\""+scope.scope_id+"\",\"version\":1}") || at!=size)
    return false;
 string actual;
 if(!RecoveryPanelDigest(StringSubstr(wire,0,digest_start)+StringSubstr(wire,digest_end),actual) ||
    actual!=expected)return false;
 out.operation=parsed.operation;out.command_id=expected_command_id;out.content_sha256=expected;
 out.range_count=parsed.range_count;
 for(int i=0;i<parsed.range_count;i++)out.ranges[i]=parsed.ranges[i];
 return true;
}
#endif
