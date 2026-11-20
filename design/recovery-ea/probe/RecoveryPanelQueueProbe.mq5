#property strict
#property version "0.10"
#include "../execution/RecoveryPanelMailbox.mqh"
#include "../execution/RecoverySessionIntentKey.mqh"

// Diagnostic only. This uses a separate queue and stages ranges only in RAM.
// No trade operations or broker order APIs are called from this entrypoint.
input string ScopeId="";
input string RequiredSymbol="XAUUSD.s";

const string QueueProbeRoot="RECOVERY_PANEL_PROBE";
RecoveryPanelMailbox QueueProbeMailbox;
RecoveryPanelCommandScope QueueProbeScope;
RecoverySessionLifecycle QueueProbeLife;
RecoveryPanelApplySession QueueProbeSeen;
RecoveryPanelControl QueueProbeControl;
RRSettings QueueProbeSettings;
ulong QueueProbeSerial=0;
bool QueueProbeStateDirty=false;

void QueueProbeContext(RecoverySessionContext &live)
  {
   live.chart=ChartID();live.login=AccountInfoInteger(ACCOUNT_LOGIN);
   live.server=AccountInfoString(ACCOUNT_SERVER);live.symbol=_Symbol;
   live.magic=881001;live.binding=ScopeId;
  }

string QueueProbeUuid(const string kind,const string seed)
  {
   const string raw=RecoverySessionSha256("RECOVERY_PANEL_PROBE_V1\t"+kind+"\t"+seed);
   if(StringLen(raw)!=64)return "";
   return StringSubstr(raw,0,8)+"-"+StringSubstr(raw,8,4)+"-"+StringSubstr(raw,12,4)+
      "-"+StringSubstr(raw,16,4)+"-"+StringSubstr(raw,20,12);
  }

bool QueueProbePrepare()
  {
   ++QueueProbeSerial;
   // Clock values are diagnostic ID salt, never freshness or trading checks.
   const string seed=(string)ChartID()+":"+(string)TimeLocal()+":"+
      (string)GetTickCount64()+":"+(string)GetMicrosecondCount()+":"+(string)QueueProbeSerial;
   QueueProbeScope.scope_id=ScopeId;
   QueueProbeScope.boot_id=QueueProbeUuid("boot",seed);
   QueueProbeScope.context_id=QueueProbeUuid("context",seed);
   QueueProbeScope.generation_id=QueueProbeUuid("generation",seed);
   if(!RecoveryPanelCommandUUID(QueueProbeScope.boot_id) ||
      !RecoveryPanelCommandUUID(QueueProbeScope.context_id) ||
      !RecoveryPanelCommandUUID(QueueProbeScope.generation_id))return false;
   FolderCreate(QueueProbeRoot);
   FolderCreate(QueueProbeRoot+"\\sessions");
   const string path=RecoveryPanelMailboxBootPath(QueueProbeScope.boot_id,true);
   return FolderCreate(path) && FolderCreate(path+"\\commands") && FolderCreate(path+"\\receipts");
  }

bool QueueProbePublishState()
  {
   const string mode=QueueProbeControl.mode==REC_CONTROL_MANUAL_WAIT ? "manual_wait":"ready";
   const string path=QueueProbeRoot+"\\state.json";
   const string temporary=path+"."+QueueProbeScope.boot_id+".writing";
   const string wire="{\"boot_id\":\""+QueueProbeScope.boot_id+"\",\"context_id\":\""+
      QueueProbeScope.context_id+"\",\"generation_id\":\""+QueueProbeScope.generation_id+
      "\",\"mode\":\""+mode+"\",\"scope_id\":\""+QueueProbeScope.scope_id+
      "\",\"version\":1,\"written_at\":"+(string)TimeLocal()+"}";
   uchar bytes[];
   const int size=StringToCharArray(wire,bytes,0,StringLen(wire),CP_UTF8);
   if(size!=StringLen(wire))return false;
   const int handle=FileOpen(temporary,FILE_BIN|FILE_WRITE);
   if(handle==INVALID_HANDLE)return false;
   const uint written=FileWriteArray(handle,bytes,0,size);
   FileFlush(handle);FileClose(handle);
   if(written!=(uint)size || !FileMove(temporary,0,path,FILE_REWRITE))
     {
      FileDelete(temporary);
      return false;
     }
   return true;
  }

void QueueProbeReset()
  {
   RecoveryPanelMailboxClose(QueueProbeMailbox);
   RecoveryClearPanelApply(QueueProbeSeen);
   RecoverySessionReset(QueueProbeLife);
   ZeroMemory(QueueProbeControl);
   QueueProbeStateDirty=false;
  }

int OnInit()
  {
   QueueProbeReset();
   RecoverySessionContext live;
   QueueProbeContext(live);
   if(!RecoveryPanelCommandUUID(ScopeId) || RequiredSymbol!=live.symbol ||
      !RecoverySessionContextValid(live) || !QueueProbePrepare() || !EventSetTimer(1))
      return INIT_FAILED;
   if(RecoverySessionInit(QueueProbeLife,live,true,RecoveryPanelBootEpoch(QueueProbeScope.boot_id))!=RSL_NEW ||
      !QueueProbePublishState())
     {EventKillTimer();QueueProbeReset();return INIT_FAILED;}
   // Synthetic grid and sizes, never used for a real request.
   QueueProbeSettings.scope=1;QueueProbeSettings.generation=1;
   QueueProbeSettings.initial_sl_ticks=80;QueueProbeSettings.support_first_ticks=25;
   QueueProbeSettings.support_second_ticks=40;QueueProbeSettings.support_offset_ticks=25;
   for(int i=0;i<24;i++)QueueProbeSettings.lot_steps[i]=1;
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();QueueProbeReset();
   PrintFormat("RECOVERY panel probe stopped: %d",reason);
  }

void OnTimer()
  {
   RecoverySessionContext live;
   QueueProbeContext(live);
   if(!RecoverySessionCurrent(QueueProbeLife,live))
     {EventKillTimer();QueueProbeReset();return;}
   RecoveryPanelApplyResult result;
   if(RecoveryPanelMailboxPoll(QueueProbeMailbox,QueueProbeScope,QueueProbeLife,live,
                             QueueProbeSettings,0.01,QueueProbeSeen,QueueProbeControl,result,true))
     {
      QueueProbeStateDirty=true;
      PrintFormat("RECOVERY panel probe command %s outcome %d",
                  result.command_id,(int)result.code);
     }
   if(QueueProbeStateDirty && QueueProbePublishState())QueueProbeStateDirty=false;
  }
