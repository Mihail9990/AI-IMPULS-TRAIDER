#property strict
#property version "0.10"
#include "../execution/RecoverySessionLifecycle.mqh"

// Diagnostic only; no trade/position/history APIs, mailbox or files.
input long ProbeMagic=70001;
input string RequiredSymbol="";
input string BindingSettings="probe-v1";

RecoverySessionLifecycle Session;
ulong LocalSerial=0,SeenProof=0,Transitions=0;
bool LoggedTimer=false;

void ReadContext(RecoverySessionContext &c) {
 c.chart=ChartID();c.login=AccountInfoInteger(ACCOUNT_LOGIN);c.magic=ProbeMagic;
 c.server=AccountInfoString(ACCOUNT_SERVER);c.symbol=_Symbol;
 c.binding=BindingSettings;
}
void ResetProbe() { SeenProof=0;Transitions=0; }
string NextDiagnosticEpoch() {
 ++LocalSerial;
 return (string)ChartID()+"-"+(string)TimeLocal()+"-"+(string)GetTickCount64()+
        "-"+(string)GetMicrosecondCount()+"-"+(string)LocalSerial;
}
int OnInit() {
 RecoverySessionContext context;
 ReadContext(context);
 const bool allowed=RecoverySessionContextValid(context) &&
                    (RequiredSymbol=="" || RequiredSymbol==context.symbol);
 bool timer_ready=false;
 if(allowed)timer_ready=EventSetTimer(1);
 const RecoverySessionInitResult state=RecoverySessionInit(Session,context,timer_ready,
                                                           NextDiagnosticEpoch());
 LoggedTimer=false;
 ResetProbe();
 if(state==RSL_INACTIVE) {
    EventKillTimer();
    Print("RECOVERY session probe inactive: invalid binding or timer failure");
    return INIT_FAILED;
 }
 PrintFormat("RECOVERY session probe empty epoch=%s proof=%I64u transitions=%I64u",
             Session.epoch,SeenProof,Transitions);
 return INIT_SUCCEEDED;
}
void OnDeinit(const int reason) {
 PrintFormat("RECOVERY session probe stopped reason=%d",reason);
 EventKillTimer();
 RecoverySessionReset(Session);ResetProbe();
}
void OnTimer() {
 RecoverySessionContext live;
 ReadContext(live);
 if(!RecoverySessionCurrent(Session,live)) {
    EventKillTimer();RecoverySessionReset(Session);ResetProbe();
    Print("RECOVERY session probe: binding mismatch; idle");
    return;
 }
 // Use one synthetic ID on the first timer tick after initialization.
 if(LoggedTimer)return;
 LoggedTimer=true;
 const ulong simulated_proof=42;
 const bool duplicate=SeenProof==simulated_proof;
 if(!duplicate) { SeenProof=simulated_proof;++Transitions; }
 PrintFormat("RECOVERY probe timer active epoch=%s ID=%I64u %s transitions=%I64u",
             Session.epoch,simulated_proof,duplicate?"duplicate":"accepted",Transitions);
}
