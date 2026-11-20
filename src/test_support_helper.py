"""Compile the actual support-SL transitions against a synthetic broker. No terminal connection."""
from pathlib import Path
import os,re,subprocess,tempfile

ROOT=Path(__file__).resolve().parent
text=(ROOT/"mql/RecoveryPythonSL.mq5").read_text()


def fun(name):
    start=re.search(r"^(?:bool|int|string) "+name+r"\(",text,re.M).start()
    end=text.index("\n}",start)+2
    return text[start:end]


HOST=r"""
#include <cassert>
#define ZeroMemory(p) (p = {})
#include <cmath>
#include <iostream>
#include <string>
using string=std::string;using ulong=unsigned long;using uint=unsigned int;
enum {TRADE_ACTION_SLTP=6,TRADE_RETCODE_DONE=10009,TRADE_RETCODE_REQUOTE,TRADE_RETCODE_REJECT,
 TRADE_RETCODE_INVALID_STOPS,TRADE_RETCODE_INVALID_PRICE,TRADE_RETCODE_MARKET_CLOSED,TRADE_RETCODE_PRICE_CHANGED,TRADE_RETCODE_PRICE_OFF,
 TRADE_RETCODE_FROZEN,TRADE_RETCODE_TOO_MANY_REQUESTS,TRADE_RETCODE_TRADE_DISABLED,TRADE_RETCODE_CLIENT_DISABLES_AT,
 TRADE_RETCODE_SERVER_DISABLES_AT,SYMBOL_TRADE_STOPS_LEVEL,SYMBOL_TRADE_FREEZE_LEVEL,SYMBOL_POINT};
struct MqlTick{long time_msc;double bid,ask;};
struct MqlTradeRequest{int action;string symbol;ulong position;double sl,tp;};
struct MqlTradeCheckResult{uint retcode;};struct MqlTradeResult{uint retcode;};
struct Owner{ulong id,ticket,magic;long opened,entry,advance;int side;string row;long cap;};
struct Saved{string phase;long anchor,confirmed_sl,confirmed_tp;ulong cursor,retcode,cooldown;};
struct View{double entry,sl,tp;long sl_ticks,tp_ticks;};
string ExpectedSymbol="TEST",Run="run",CapStatus;long LatestTick=0;ulong LastQuoteAt=0;
bool permitted,owned,present,can_check,write_ok,tracked,cap_tracked,actual_send,change_tp,improve_stop;int disappear_at;long stops,freeze;
int reads,sends;uint returned;ulong uptime;
MqlTick quote;Saved disk,cap_disk;View broker;MqlTradeRequest sent;
ulong GetTickCount64(){return uptime;}
bool Permissions(){return permitted;}
bool CurrentOwner(const Owner&,const string&){return owned;}
bool SymbolInfoTick(const string&,MqlTick& q){q=quote;return true;}
bool SymbolInfoInteger(const string&,int k,long& x){x=(k==SYMBOL_TRADE_STOPS_LEVEL?stops:freeze);return true;}
bool SymbolInfoDouble(const string&,int,double& v){v=.01;return true;}
bool ReadView(const Owner&,double,View& v){++reads;if(reads==disappear_at)present=false;if(improve_stop&&reads==3){broker.sl=100;broker.sl_ticks=10000;}if(change_tp&&reads==3){broker.tp=170;broker.tp_ticks=17000;}v=broker;return present;}
string Mark(const Owner&,bool cap=false){return cap?"cap":"position";}
bool FileIsExist(const string& key){return key=="cap"?cap_tracked:tracked;}
bool ReadSaved(const Owner&,Saved& s,bool cap=false){if(!(cap?cap_tracked:tracked))return false;s=cap?cap_disk:disk;return true;}
bool Save(const Owner&,const Saved& s,bool replace=true,bool cap=false){bool &seen=cap?cap_tracked:tracked;if(!write_ok||(!replace&&seen))return false;seen=true;(cap?cap_disk:disk)=s;return true;}
bool OrderCheck(const MqlTradeRequest& req,MqlTradeCheckResult& c){assert(req.action==TRADE_ACTION_SLTP);c.retcode=0;return can_check;}
bool OrderSend(const MqlTradeRequest& req,MqlTradeResult& r){assert((tracked&&disk.phase=="submitting")||(cap_tracked&&cap_disk.phase=="submitting"));++sends;sent=req;r.retcode=returned;return actual_send;}
int RecoveryPriceStage(int side,double entry,double bid,double ask,double,double advance){
 return (side==1?bid-entry:entry-ask)+1e-9>=advance?2:0;
}
"""
MAIN=r"""
bool TicksHit(const Owner& o,const View& v,double t,const MqlTick& q,Saved&){return EligibleQuote(o,v,t,q);}
string Submit(const Owner& o,const string& run,double t,View& v,Saved& s,bool cap=false){return SubmitLocked(o,run,t,v,s,cap);}
RISKCAP
OBSERVE
struct Case{
 Owner o{11,10,0,900,10000,40,1,"mock"};
 Case(){permitted=owned=present=can_check=write_ok=actual_send=true;change_tp=tracked=cap_tracked=improve_stop=false;reads=sends=disappear_at=0;stops=freeze=0;
  returned=TRADE_RETCODE_DONE;uptime=200;quote={1000,100.41,100.44};broker={100,99,150,9900,15000};LatestTick=0;LastQuoteAt=0;disk={};sent={};}
 string step(bool submit=true){View v{};Saved s{};return Observe(o,Run,.01,v,s,submit);}
};
int main(){
 {Case c;quote.bid=100.39;quote.ask=101;assert(c.step()=="watching"&&!sends);quote.bid=100.40;
  assert(c.step()=="pending"&&sends==1&&sent.position==10&&sent.action==TRADE_ACTION_SLTP&&sent.sl==100&&sent.tp==150);
  assert(c.step()=="pending"&&sends==1);broker.sl_ticks=10000;broker.sl=100;assert(c.step()=="confirmed");present=false;
  assert(c.step()=="last_confirmed"&&sends==1);}
 {Case c;c.o.side=-1;broker={100,101,90,10100,9000};quote={1000,99.4,99.61};assert(c.step()=="watching");
  quote.ask=99.60;assert(c.step()=="pending"&&sent.sl==100&&sent.tp==90);}
 {Case c;broker.sl=101;broker.sl_ticks=10100;assert(c.step()=="protected"&&!sends);present=false;
  assert(c.step()=="last_protected");present=true;broker.sl=99;broker.sl_ticks=9900;assert(c.step()=="changed"&&!sends);}
 {Case c;permitted=false;assert(c.step()=="blocked"&&!sends);permitted=true;assert(c.step()=="pending"&&sends==1);}
 {Case c;owned=false;assert(c.step()=="inactive"&&!sends);}
 {Case c;assert(c.step(false)=="hit"&&!sends&&disk.phase=="hit");quote.bid=99;quote.ask=99.1;assert(c.step()=="hit"&&!sends);}
 {Case c;actual_send=false;returned=0;assert(c.step()=="pending"&&sends==1);assert(c.step()=="pending"&&sends==1);}
 {Case c;actual_send=false;returned=TRADE_RETCODE_INVALID_STOPS;assert(c.step()=="rejected"&&sends==1);
  uptime+=1000;assert(c.step()=="rejected"&&sends==1);uptime+=2100;returned=TRADE_RETCODE_DONE;actual_send=true;
  quote.time_msc++;assert(c.step()=="pending"&&sends==2);}
 {Case c;change_tp=true;assert(c.step()=="hit"&&!sends);change_tp=false;assert(c.step()=="pending"&&sent.tp==170);}
 {Case c;present=false;assert(c.step()=="missing"&&!sends);}
 {Case c;write_ok=false;assert(c.step()=="unknown"&&!sends);}
 {Case c;can_check=false;assert(c.step()=="rejected"&&!sends);}
 // Tighten a slipped SELL using its actual fill. A cap ACK cannot masquerade as BE.
 {Case c;c.o.side=-1;c.o.entry=352502;c.o.cap=352582;broker={3525.02,3526.43,3500,352643,350000};
  quote={1000,3525.12,3525.25};assert(c.step()=="watching"&&CapStatus=="pending");
  assert(sends==1&&std::abs(sent.sl-3525.82)<1e-8&&sent.tp==3500&&disk.retcode==0);
  assert(c.step()=="watching"&&sends==1);broker.sl=3525.82;broker.sl_ticks=352582;
  assert(c.step()=="watching"&&CapStatus=="within");quote={1001,3524.55,3524.60};
  assert(c.step()=="pending"&&sends==2&&sent.sl==3525.02);}
 {Case c;c.o.cap=9920;quote={1000,99.8,99.9};assert(c.step()=="watching"&&CapStatus=="pending"&&sent.sl==99.20);
  quote={1001,100.41,100.44};assert(c.step()=="hit"&&CapStatus=="pending"&&sends==1);
  broker.sl=99.2;broker.sl_ticks=9920;assert(c.step()=="pending"&&sends==2&&sent.sl==100);}
 {Case c;c.o.cap=9920;broker.sl=99.80;broker.sl_ticks=9980;quote={1000,99.9,100.0};
  assert(c.step()=="watching"&&CapStatus=="within"&&!sends);}
 {Case c;c.o.cap=9920;quote={1000,99.30,99.40};stops=20;
  assert(c.step()=="watching"&&CapStatus=="waiting"&&!sends);quote={1001,99.80,99.90};
  assert(c.step()=="watching"&&CapStatus=="pending"&&sends==1);}
 {Case c;c.o.cap=9920;quote={1000,99.80,99.90};present=false;assert(c.step()=="missing"&&!sends);}
 {Case c;c.o.cap=9920;quote={1000,99.80,99.90};disappear_at=4;
  assert(c.step()=="watching"&&!sends);assert(c.step()=="missing"&&!sends);}
 {Case c;c.o.cap=9920;quote={1000,99.80,99.90};improve_stop=true;
  assert(c.step()=="watching"&&!sends);assert(c.step()=="confirmed"&&!sends);}
 {Case c;c.o.cap=9920;quote={1000,99.80,99.90};actual_send=false;returned=0;
  assert(c.step()=="watching"&&CapStatus=="pending"&&sends==1);quote={1001,100.5,100.55};
  assert(c.step()=="hit"&&sends==1);assert(cap_disk.phase=="submitting");}
 {Case c;c.o.cap=9920;quote={1000,99.80,99.90};actual_send=false;returned=TRADE_RETCODE_INVALID_STOPS;
  assert(c.step()=="watching"&&CapStatus=="rejected"&&sends==1);uptime+=100;
  assert(c.step()=="watching"&&CapStatus=="rejected"&&sends==1);uptime+=3001;
  assert(c.step()=="watching"&&sends==2);}
 {Case c;c.o.cap=9920;assert(c.step()=="pending"&&sends==1&&sent.sl==100);quote={1001,99.8,99.9};
  assert(c.step()=="pending"&&CapStatus=="be_pending"&&sends==1);}
 {Case c;c.o.cap=9920;quote={1000,99.80,99.90};actual_send=false;returned=0;
  assert(c.step()=="watching"&&sends==1);broker.sl=99.5;broker.sl_ticks=9950;
  quote={1001,100.5,100.55};assert(c.step()=="hit"&&CapStatus=="pending"&&sends==1);}
 {Case c;c.o.cap=9920;quote={1000,99.8,99.9};actual_send=false;returned=TRADE_RETCODE_INVALID_PRICE;
  assert(c.step()=="watching"&&CapStatus=="rejected"&&sends==1);
  assert(c.step()=="watching"&&sends==1);uptime+=3001;returned=TRADE_RETCODE_DONE;
  assert(c.step()=="watching"&&CapStatus=="pending"&&sends==2);}
 std::cout<<"24 SL broker and restart scenarios passed\n";
}
"""
if __name__=="__main__":
    code=HOST+"\n"+fun("Protection")+"\n"+fun("Rejected")+"\n"+fun("EligibleQuote")+"\n"+fun("SubmitLocked")+"\n"+MAIN.replace("OBSERVE",fun("Observe")).replace("RISKCAP",fun("RiskCap"))
    with tempfile.TemporaryDirectory(prefix="recovery-py-sl-") as td:
        p=Path(td);src=p/"case.cpp";exe=p/"case.exe";src.write_text(code)
        subprocess.run([os.getenv("CXX","c++"),"-std=c++17","-fsanitize=undefined",str(src),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)
