"""C++ host harness for the entrypoint, substituting MT5 and its chart controller."""
from pathlib import Path
from subprocess import run
from tempfile import TemporaryDirectory
from unittest import TestCase, main

HERE = Path(__file__).resolve().parent
SHIM = r"""
#include <cassert>
#include <cmath>
#include <cstdint>
#include <regex>
#include <sstream>
#include <string>
#include <vector>
using string=std::string;
using ulong=unsigned long;
enum ENUM_TIMEFRAMES {PERIOD_CURRENT=-1,PERIOD_M1=1,PERIOD_H1=60};
enum {ACCOUNT_LOGIN, ACCOUNT_SERVER, ACCOUNT_MARGIN_MODE, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
      ACCOUNT_TRADE_ALLOWED, ACCOUNT_TRADE_EXPERT, TERMINAL_TRADE_ALLOWED, TERMINAL_CONNECTED,
      MQL_TRADE_ALLOWED, SYMBOL_TRADE_TICK_SIZE, SYMBOL_VOLUME_STEP,
      INIT_SUCCEEDED=0, INIT_PARAMETERS_INCORRECT=201, INIT_FAILED=202};
struct RecoverySessionContext {long chart,login,magic;string server,symbol,binding;};
struct RRSettings {
  ulong scope,generation;
  long initial_sl_ticks,support_first_ticks,support_second_ticks,support_offset_ticks,lot_steps[24];
};
struct RecoveryChartRuntime {};
struct MqlTradeTransaction {int value=0;};
struct MqlTradeRequest {};
struct MqlTradeResult {};
template <typename T> void ZeroMemory(T& value) {value=T{};}
long chart=10,login=42,margin=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;
string server="Test-Server",_Symbol="XAUUSD.s",watch_symbol="XAUUSD.s";
ENUM_TIMEFRAMES _Period=PERIOD_M1;
bool connected=true,terminal_ok=true,expert_ok=true,account_ok=true,account_expert=true,
     timer_ok=true,start_ok=true,symbol_ok=true,timer_set=false;
double tick=.01,step=.01;
int starts=0,stops=0,timers=0,trades=0;
bool saw_scope=false,saw_grid=false,saw_schedule=false;
long ChartID(){return chart;}
string ChartSymbol(long id){return id==20?_Symbol:id==30?watch_symbol:"";}
long AccountInfoInteger(int kind){
  switch(kind){case ACCOUNT_LOGIN:return login;case ACCOUNT_MARGIN_MODE:return margin;
  case ACCOUNT_TRADE_ALLOWED:return account_ok;case ACCOUNT_TRADE_EXPERT:return account_expert;}
  assert(false);return 0;
}
string AccountInfoString(int kind){assert(kind==ACCOUNT_SERVER);return server;}
long TerminalInfoInteger(int kind){
  if(kind==TERMINAL_CONNECTED)return connected;
  assert(kind==TERMINAL_TRADE_ALLOWED);return terminal_ok;
}
long MQLInfoInteger(int kind){assert(kind==MQL_TRADE_ALLOWED);return expert_ok;}
bool SymbolInfoDouble(const string& name,int field,double& out){
  assert(name==_Symbol);out=field==SYMBOL_TRADE_TICK_SIZE?tick:step;return symbol_ok;
}
bool EventSetTimer(int seconds){assert(seconds==1);timer_set=timer_ok;return timer_set;}
void EventKillTimer(){timer_set=false;}
void Print(const string& text){assert(!text.empty());}
int StringLen(const string& value){return (int)value.size();}
int StringFind(const string& haystack,const string& word){
  const auto p=haystack.find(word);return p==string::npos?-1:(int)p;
}
string StringSubstr(const string& value,int pos,int len){return value.substr(pos,len);}
int StringSplit(const string& value,int sep,std::vector<string>& tokens){
  std::istringstream data(value);
  for(string tok;std::getline(data,tok,(char)sep);)tokens.push_back(tok);
  return (int)tokens.size();
}
bool RecoveryPanelCommandUUID(const string& s){
  return std::regex_match(s,std::regex("[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}"));
}
bool RecoveryInvContext(const string& s){return !s.empty();}
bool RecoveryInvHex(const string& s){return s.size()==32 && s.find_first_not_of("0123456789abcdef")==string::npos;}
string RecoveryLiveUuid(const string& category,const string& value){
  assert(value=="00112233-4455-6677-8899-aabbccddeeff:"+string(64,'a'));
  if(category=="boot")return "00112233-4455-6677-8899-aabbccddeeff";
  if(category=="verifier")return "00112233-4455-6677-8899-000000000001";
  if(category=="watcher")return "00112233-4455-6677-8899-000000000002";
  assert(false);return "";
}
string RecoveryPanelBootEpoch(string value){
  while(value.find('-')!=string::npos)value.erase(value.find('-'),1);
  return value;
}
bool RecoveryPanelPrice(const string& arg,double quantum,long& units){
  if(quantum<=0 || !std::regex_match(arg,std::regex("(0|[1-9][0-9]*)([.][0-9]+)?")))return false;
  const auto ratio=std::stod(arg)/quantum;
  const auto rounded=std::round(ratio);
  if(!std::isfinite(ratio)||rounded<=0||std::fabs(ratio-rounded)>1e-8)return false;
  units=(long)rounded;return true;
}
bool RRSettingsValid(const RRSettings& settings){
  return settings.scope>0 && settings.initial_sl_ticks>0 && settings.support_second_ticks>0;
}
bool RecoveryChartStart(RecoveryChartRuntime&,const RecoverySessionContext& live,
        const string& profile,const string& seed,const string& verifier,long verifier_id,
        const RRSettings& cfg,double actual_tick,double actual_step,const string& watcher,long watcher_id){
  ++starts;
  assert(timer_set);
  saw_scope=profile=="00112233-4455-6677-8899-aabbccddeeff" && seed==string(64,'a') &&
      live.login==42 && live.server=="Test-Server" && live.symbol=="XAUUSD.s" && live.magic==123 &&
      verifier_id==20 && watcher_id==30 && verifier=="00112233445566778899000000000001" &&
      watcher=="00112233445566778899000000000002";
  saw_grid=actual_tick==.01 && actual_step==.01 && cfg.scope==123 && cfg.generation==1 &&
      cfg.initial_sl_ticks==80 && cfg.support_second_ticks==40 &&
      cfg.support_first_ticks==0 && cfg.support_offset_ticks==0;
  const long expected[24]={1,1,1,1,1,2,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10};
  saw_schedule=true;
  for(int i=0;i<24;i++)saw_schedule=saw_schedule && cfg.lot_steps[i]==expected[i];
  return start_ok;
}
void RecoveryChartStop(RecoveryChartRuntime&){++stops;}
bool RecoveryChartOnTimer(RecoveryChartRuntime&,const RecoverySessionContext&){++timers;return true;}
void RecoveryMoneyJournalTimer(long,const string&,const string&){}
void RecoveryMoneyFlushPending(){}
void RecoveryChartOnTrade(RecoveryChartRuntime&,const RecoverySessionContext&,const MqlTradeTransaction& event){
  assert(event.value==123);++trades;
}
"""
DRIVE = r"""
int main(){
  MqlTradeTransaction event; event.value=123;
  assert(OnInit()==INIT_PARAMETERS_INCORRECT && starts==0 && !timer_set);
  ExpectedLogin=42;ExpectedServer="Test-Server";ExpectedSymbol="XAUUSD.s";StrategyMagic=123;
  ProfileScope="00112233-4455-6677-8899-aabbccddeeff";InstanceSeed=string(64,'a');
  VerifierChart=20;WatcherChart=30;
  OnTradeTransaction(event,{},{});OnTimer();
  assert(timers==0 && trades==0);
  _Period=PERIOD_H1;
  assert(OnInit()==INIT_PARAMETERS_INCORRECT && starts==0);
  _Period=PERIOD_M1;
  margin=1;
  assert(OnInit()==INIT_PARAMETERS_INCORRECT && starts==0);
  margin=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;
  WatcherChart=VerifierChart;
  assert(OnInit()==INIT_PARAMETERS_INCORRECT && starts==0);
  WatcherChart=30;
  SupportThreshold="0.401";
  assert(OnInit()==INIT_PARAMETERS_INCORRECT && starts==0);
  SupportThreshold="0.40";
  const string schedule=LotSchedule;
  LotSchedule="0.01,0.02";
  assert(OnInit()==INIT_PARAMETERS_INCORRECT && starts==0);
  LotSchedule=schedule;
  account_expert=false;
  assert(OnInit()==INIT_FAILED && starts==0 && !timer_set);
  account_expert=true;
  timer_ok=false;
  assert(OnInit()==INIT_FAILED && starts==0);
  timer_ok=true;start_ok=false;
  assert(OnInit()==INIT_FAILED && starts==1 && !timer_set && timers==0 && trades==0);
  start_ok=true;
  assert(OnInit()==INIT_SUCCEEDED && starts==2 && timers==0 && trades==0 && timer_set);
  assert(saw_scope && saw_grid && saw_schedule);
  OnTimer();OnTradeTransaction(event,{},{});
  assert(timers==1 && trades==1);
  connected=false;
  OnTimer();OnTradeTransaction(event,{},{});
  assert(timers==1 && trades==1 && timer_set);
  connected=true;_Period=PERIOD_H1;
  OnTimer();OnTradeTransaction(event,{},{});
  assert(timers==1 && trades==1 && stops==1 && !timer_set);
  OnDeinit(0);
  assert(stops==1 && !timer_set);
}
"""


class EntryPoint(TestCase):
    def test_strict_callbacks_against_stubbed_terminal_and_controller(self):
        native = (HERE / "RecoveryTrader.mq5").read_text()
        # Account for MQL input/property declarations, dynamic arrays and
        # explicit string-to-integer formatting cast. Leave behavior unchanged.
        body = "\n".join(line for line in native.splitlines()
                         if not line.startswith(("#property", "#include")))
        body = body.replace("input ", "").replace("string tokens[];", "std::vector<string> tokens;")
        body = body.replace("(string)(long)FixedTimeframe", "std::to_string((long)FixedTimeframe)")
        with TemporaryDirectory() as directory:
            source = Path(directory) / "trader.cpp"
            executable = Path(directory) / "trader"
            source.write_text(SHIM + body + DRIVE)
            run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-Wno-unused-parameter", "-fsanitize=undefined", str(source), "-o", str(executable)],
                check=True)
            run([str(executable)], check=True)

    def test_source_delegates_trading_effects_to_controller_only(self):
        native = (HERE / "RecoveryTrader.mq5").read_text()
        for forbidden in ("OrderSend", "OrderCheck", "PositionClose", "HistorySelect", "#import", "WebRequest"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, native)
        init = native.split("int OnInit()", 1)[1].split("void RecoveryTraderStop()", 1)[0]
        self.assertNotIn("RecoveryChartOnTimer", init)
        self.assertNotIn("RecoveryChartOnTrade", init)


if __name__ == "__main__":
    main()
