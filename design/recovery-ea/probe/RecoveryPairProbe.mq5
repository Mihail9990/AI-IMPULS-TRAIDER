#property strict
#property version "0.10"
#property description "Read-only diagnostic: one direct inventory exchange between two charts."

#include "../execution/RecoveryInventoryRequest.mqh"
#include "../verifier/RecoveryInventoryReader.mqh"

// Values provided by a demo test fixture, not future settings in the web panel.
input long ExpectedLogin=0;
input string ExpectedServer="";
input string ExpectedSymbol="";
input long StrategyMagic=0;
input long VerifierChart=0;
input string VerifierEpoch="";
input string ExecutorEpoch="";
input string ProbeNonce=""; // New random 32-hex identifier for each probe.
input long ContextGeneration=0;

RecoveryInventoryScope ProbeScope;
bool ProbePublished=false;
bool ProbeFinished=false;

bool ProbeSameAccount()
  {
   return TerminalInfoInteger(TERMINAL_CONNECTED)!=0 &&
          AccountInfoInteger(ACCOUNT_LOGIN)==ProbeScope.login &&
          AccountInfoString(ACCOUNT_SERVER)==ProbeScope.server &&
          _Symbol==ProbeScope.symbol;
  }

int OnInit()
  {
   ProbePublished=false;
   ProbeFinished=false;
   // Native terminal smoke testing only. Tester sandboxes are not the deployment path.
   if(MQLInfoInteger(MQL_TESTER) ||
      AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO)
     {
      Print("RECOVERY probe: demo terminal only.");
      return INIT_FAILED;
     }
   ProbeScope.nonce=ProbeNonce;
   ProbeScope.executor_epoch=ExecutorEpoch;
   ProbeScope.verifier_epoch=VerifierEpoch;
   ProbeScope.generation=ContextGeneration;
   ProbeScope.login=ExpectedLogin;
   ProbeScope.server=ExpectedServer;
   ProbeScope.symbol=ExpectedSymbol;
   ProbeScope.magic=StrategyMagic;
   ProbeScope.verifier_chart=VerifierChart;
   if(!RecoveryInvScopeOK(ProbeScope) || VerifierChart==ChartID() ||
      ExpectedSymbol!=_Symbol)
      return INIT_PARAMETERS_INCORRECT;
   if(!EventSetTimer(1)) return INIT_FAILED;
   Print("RECOVERY probe: waiting to request a read-only inventory.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   if(ProbeFinished || !ProbeSameAccount() ||
      AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO)
      return;
   if(!ProbePublished)
     {
      const string base="RECOVERY_VERIFY\\"+ProbeScope.nonce;
      if(FileIsExist(base+".inv") || FileIsExist(base+".snapshot"))
        {
         ProbeFinished=true;
         EventKillTimer();
         Print("RECOVERY probe: nonce already used; choose a fresh one.");
         return;
        }
      ProbePublished=RecoveryRequestInventory(ProbeScope.nonce,ProbeScope.executor_epoch,
         ProbeScope.verifier_epoch,ProbeScope.generation,ProbeScope.login,
         ProbeScope.server,ProbeScope.symbol,ProbeScope.magic);
      if(!ProbePublished) return;
      Print("RECOVERY probe: inventory query published.");
      return;
     }
   RecoveryInventorySnapshot snapshot;
   if(!RecoveryInvRead(ProbeScope,snapshot) || !snapshot.usable || !ProbeSameAccount())
      return;
   ProbeFinished=true;
   EventKillTimer();
   if(!snapshot.complete)
      Print("RECOVERY probe: verifier responded, inventory unavailable: ",snapshot.reason);
   else
      PrintFormat("RECOVERY probe: direct response confirmed, positions=%d orders=%d.",
         snapshot.position_count,snapshot.order_count);
  }
