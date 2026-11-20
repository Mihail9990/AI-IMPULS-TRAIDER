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
#include <cmath>
#include <iostream>
#include <string>
using string=std::string;using ulong=unsigned long;using uint=unsigned int;
enum {TRADE_ACTION_SLTP=6,TRADE_RETCODE_DONE=10009,TRADE_RETCODE_REQUOTE,TRADE_RETCODE_REJECT,
 TRADE_RETCODE_INVALID_STOPS,TRADE_RETCODE_MARKET_CLOSED,TRADE_RETCODE_PRICE_CHANGED,TRADE_RETCODE_PRICE_OFF,
 TRADE_RETCODE_FROZEN,TRADE_RETCODE_TOO_MANY_REQUESTS,TRADE_RETCODE_TRADE_DISABLED,TRADE_RETCODE_CLIENT_DISABLES_AT,
 TRADE_RETCODE_SERVER_DISABLES_AT,SYMBOL_TRADE_STOPS_LEVEL,SYMBOL_TRADE_FREEZE_LEVEL,SYMBOL_POINT};
struct MqlTick{long time_msc;double bid,ask;};
struct MqlTradeRequest{int action;string symbol;ulong position;double sl,tp;};
struct MqlTradeCheckResult{uint retcode;};struct MqlTradeResult{uint retcode;};
struct Owner{ulong id,ticket,magic;long opened,entry,advance;int side;string row;};
struct Saved{string phase;long anchor,confirmed_sl,confirmed_tp;ulong cursor,retcode,cooldown;};
struct View{double entry,sl,tp;long sl_ticks,tp_ticks;};
string ExpectedSymbol="TEST",Run="run";long LatestTick=0;ulong LastQuoteAt=0;
bool permitted,owned,present,can_check,write_ok,tracked,actual_send,change_tp;
int reads,sends;uint returned;ulong uptime;
MqlTick quote;Saved disk;View broker;MqlTradeRequest sent;
ulong GetTickCount64(){return uptime;}
bool Permissions(){return permitted;}
bool CurrentOwner(const Owner&,const string&){return owned;}
bool SymbolInfoTick(const string&,MqlTick& q){q=quote;return true;}
bool SymbolInfoInteger(const string&,int,long& x){x=0;return true;}
bool SymbolInfoDouble(const string&,int,double& v){v=.01;return true;}
bool ReadView(const Owner&,double,View& v){++reads;if(change_tp&&reads==3){broker.tp=170;broker.tp_ticks=17000;}v=broker;return present;}
string Mark(const Owner&){return "position";}
bool FileIsExist(const string&){return tracked;}
bool ReadSaved(const Owner&,Saved& s){if(!tracked)return false;s=disk;return true;}
bool Save(const Owner&,const Saved& s,bool replace=true){if(!write_ok||(!replace&&tracked))return false;tracked=true;disk=s;return true;}
bool OrderCheck(const MqlTradeRequest& req,MqlTradeCheckResult& c){assert(req.action==TRADE_ACTION_SLTP);c.retcode=0;return can_check;}
bool OrderSend(const MqlTradeRequest& req,MqlTradeResult& r){assert(tracked&&disk.phase=="submitting");++sends;sent=req;r.retcode=returned;return actual_send;}
int RecoveryPriceStage(int side,double entry,double bid,double ask,double,double advance){
 return (side==1?bid-entry:entry-ask)+1e-9>=advance?2:0;
}
"""
MAIN=r"""
bool TicksHit(const Owner& o,const View& v,double t,const MqlTick& q,Saved&){return EligibleQuote(o,v,t,q);}
string Submit(const Owner& o,const string& run,double t,View& v,Saved& s){return SubmitLocked(o,run,t,v,s);}
OBSERVE
struct Case{
 Owner o{11,10,0,900,10000,40,1,"mock"};
 Case(){permitted=owned=present=can_check=write_ok=actual_send=true;change_tp=tracked=false;reads=sends=0;
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
 std::cout<<"12 support-SL broker and restart scenarios passed\n";
}
"""
if __name__=="__main__":
    code=HOST+"\n"+fun("Protection")+"\n"+fun("Rejected")+"\n"+fun("EligibleQuote")+"\n"+fun("SubmitLocked")+"\n"+MAIN.replace("OBSERVE",fun("Observe"))
    with tempfile.TemporaryDirectory(prefix="recovery-py-sl-") as td:
        p=Path(td);src=p/"case.cpp";exe=p/"case.exe";src.write_text(code)
        subprocess.run([os.getenv("CXX","c++"),"-std=c++17","-fsanitize=undefined",str(src),"-o",str(exe)],check=True)
        subprocess.run([str(exe)],check=True)
