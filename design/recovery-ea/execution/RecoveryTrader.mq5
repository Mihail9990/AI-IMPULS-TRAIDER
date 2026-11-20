#property strict
#property version "0.10"
#property description "RECOVERY main owner: one explicitly attached chart; start is an empty session."
#include "RecoveryChartRuntime.mqh"

input long ExpectedLogin=0;
input string ExpectedServer="";
input string ExpectedSymbol="";
input ENUM_TIMEFRAMES FixedTimeframe=PERIOD_M1;
input long StrategyMagic=0;
input string ProfileScope="";
input string InstanceSeed=""; // one fresh 64-character seed; all three roles use its epochs
input long VerifierChart=0;   // observed ID of the verifier in this terminal
input long WatcherChart=0;    // observed ID of the price watcher in this terminal
input string InitialSLDistance="0.80";
input string SupportThreshold="0.40";
input string LotSchedule="0.01,0.01,0.01,0.01,0.01,0.02,0.02,0.02,0.03,0.03,0.04,0.04,0.05,0.05,0.06,0.06,0.07,0.07,0.08,0.08,0.09,0.09,0.10,0.10";

RecoveryChartRuntime TraderRuntime;
bool TraderOwned=false;

bool RecoveryTraderSeedOK(const string seed)
  {
   if(StringLen(seed)!=64)return false;
   const string alphabet="0123456789abcdef";
   for(int i=0;i<64;i++)
      if(StringFind(alphabet,StringSubstr(seed,i,1))<0)return false;
   return true;
  }

bool RecoveryTraderPermissions()
  {
   return TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)!=0 &&
          MQLInfoInteger(MQL_TRADE_ALLOWED)!=0 &&
          AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)!=0 &&
          AccountInfoInteger(ACCOUNT_TRADE_EXPERT)!=0;
  }

bool RecoveryTraderLive(RecoverySessionContext &live)
  {
   live.chart=ChartID();
   live.login=AccountInfoInteger(ACCOUNT_LOGIN);
   live.server=AccountInfoString(ACCOUNT_SERVER);
   live.symbol=_Symbol;
   live.magic=StrategyMagic;
   live.binding=ProfileScope+":"+InstanceSeed+":"+(string)(long)FixedTimeframe;
   return live.chart>0 && StrategyMagic>0 && ExpectedLogin>0 && live.login==ExpectedLogin &&
          RecoveryInvContext(ExpectedServer) && live.server==ExpectedServer &&
          RecoveryInvContext(ExpectedSymbol) && live.symbol==ExpectedSymbol &&
          FixedTimeframe!=PERIOD_CURRENT && _Period==FixedTimeframe &&
          AccountInfoInteger(ACCOUNT_MARGIN_MODE)==ACCOUNT_MARGIN_MODE_RETAIL_HEDGING &&
          VerifierChart>0 && WatcherChart>0 && VerifierChart!=WatcherChart &&
          VerifierChart!=live.chart && WatcherChart!=live.chart &&
          ChartSymbol(VerifierChart)==live.symbol && ChartSymbol(WatcherChart)==live.symbol;
  }

bool RecoveryTraderSettings(const double price_tick,const double volume_step,RRSettings &cfg)
  {
   ZeroMemory(cfg);
   if(!RecoveryPanelPrice(InitialSLDistance,price_tick,cfg.initial_sl_ticks) ||
      !RecoveryPanelPrice(SupportThreshold,price_tick,cfg.support_second_ticks))return false;
   string tokens[];
   if(StringSplit(LotSchedule,44,tokens)!=24)return false;
   for(int i=0;i<24;i++)
      if(!RecoveryPanelPrice(tokens[i],volume_step,cfg.lot_steps[i]))return false;
   cfg.scope=(ulong)StrategyMagic; // namespaced by this executor epoch and account in transport
   cfg.generation=1;
   cfg.support_first_ticks=0;
   cfg.support_offset_ticks=0;
   return RRSettingsValid(cfg);
  }

int OnInit()
  {
   TraderOwned=false;
   RecoverySessionContext live;
   if(!RecoveryPanelCommandUUID(ProfileScope) || !RecoveryTraderSeedOK(InstanceSeed) ||
      TerminalInfoInteger(TERMINAL_CONNECTED)==0 || !RecoveryTraderLive(live))
     {
      Print("RECOVERY main: account, fixed timeframe or role configuration does not match.");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(!RecoveryTraderPermissions())
     {
      Print("RECOVERY main: terminal/EA/account does not permit expert trading.");
      return INIT_FAILED;
     }
   const string seed=ProfileScope+":"+InstanceSeed;
   const string executor=RecoveryPanelBootEpoch(RecoveryLiveUuid("boot",seed));
   const string verifier=RecoveryPanelBootEpoch(RecoveryLiveUuid("verifier",seed));
   const string watcher=RecoveryPanelBootEpoch(RecoveryLiveUuid("watcher",seed));
   if(!RecoveryInvHex(executor) || !RecoveryInvHex(verifier) || !RecoveryInvHex(watcher) ||
      executor==verifier || executor==watcher || verifier==watcher)return INIT_FAILED;
   double tick=0.0,step=0.0;
   RRSettings settings;
   if(!SymbolInfoDouble(live.symbol,SYMBOL_TRADE_TICK_SIZE,tick) ||
      !SymbolInfoDouble(live.symbol,SYMBOL_VOLUME_STEP,step) ||
      !RecoveryTraderSettings(tick,step,settings))
     {
      Print("RECOVERY main: invalid distances, volume schedule or symbol grid.");
      return INIT_PARAMETERS_INCORRECT;
     }
   // Arm the local timer first; a failed timer must not advertise readiness.
   // MT5 invokes callbacks only after this initialization handler returns.
   if(!EventSetTimer(1))return INIT_FAILED;
   if(!RecoveryChartStart(TraderRuntime,live,ProfileScope,InstanceSeed,verifier,VerifierChart,
                          settings,tick,step,watcher,WatcherChart))
     {
      EventKillTimer();
      Print("RECOVERY main: a fresh single-owner session could not start.");
      return INIT_FAILED;
     }
   TraderOwned=true;
   Print("RECOVERY main: new empty session. Awaiting an explicitly confirmed panel command.");
   return INIT_SUCCEEDED;
  }

void RecoveryTraderStop()
  {
   EventKillTimer();
   if(TraderOwned)RecoveryChartStop(TraderRuntime);
   TraderOwned=false;
  }

void OnDeinit(const int reason)
  {
   RecoveryTraderStop();
  }

bool RecoveryTraderCurrent(RecoverySessionContext &live)
  {
   if(!TraderOwned)return false;
   if(TerminalInfoInteger(TERMINAL_CONNECTED)==0)return false;
   if(RecoveryTraderLive(live) && RecoveryTraderPermissions())return true;
   Print("RECOVERY main: chart/account, role binding or trade permission changed; session stopped.");
   RecoveryTraderStop();
   return false;
  }

void OnTimer()
  {
   RecoverySessionContext live;
   if(RecoveryTraderCurrent(live))RecoveryChartOnTimer(TraderRuntime,live);
  }

void OnTradeTransaction(const MqlTradeTransaction &transaction,
                        const MqlTradeRequest &request,const MqlTradeResult &result)
  {
   RecoverySessionContext live;
   if(RecoveryTraderCurrent(live))RecoveryChartOnTrade(TraderRuntime,live,transaction);
  }
