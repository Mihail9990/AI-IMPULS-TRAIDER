#ifndef RECOVERY_LIVE_SESSION_MQH
#define RECOVERY_LIVE_SESSION_MQH
#include "RecoveryPanelMailbox.mqh"
#include "RecoverySessionIntentKey.mqh"

// One chart owns the trading panel mailbox inside this terminal's Files root.
// The open handle prevents a second writer in that same local sandbox. File
// contents are unused; nothing is restored after the handle closes.
struct RecoveryLiveSession {
 RecoverySessionLifecycle lifecycle;
 RecoveryPanelCommandScope mail;
 RecoveryPanelMailbox mailbox;
 RecoveryPanelApplySession panel_seen;
 long owner_handle;
 bool owns_mail;
};

const string RecoveryLivePanelRoot="RECOVERY_PANEL";

string RecoveryLiveStatePrefix(const RecoveryPanelCommandScope &mail)
  {
   return "{\"boot_id\":\""+mail.boot_id+"\",\"context_id\":\""+mail.context_id+
      "\",\"generation_id\":\""+mail.generation_id+"\",\"mode\":\"";
  }

// Remove only this chart's publication while it still holds the writer
// lock. Nothing in the command or receipt folders is read, removed or resumed.
void RecoveryLiveSessionWithdraw(const RecoveryLiveSession &s)
  {
   if(!s.owns_mail || s.owner_handle==INVALID_HANDLE || !s.lifecycle.active ||
      !RecoveryPanelCommandUUID(s.mail.boot_id) ||
      RecoveryPanelBootEpoch(s.mail.boot_id)!=s.lifecycle.epoch ||
      !RecoveryPanelCommandUUID(s.mail.context_id) || !RecoveryPanelCommandUUID(s.mail.generation_id) ||
      !RecoveryPanelCommandUUID(s.mail.scope_id))return;
   const string path=RecoveryLivePanelRoot+"\\state.json", prefix=RecoveryLiveStatePrefix(s.mail);
   string wire;
   if(RecoveryPanelMailboxRead(path,wire) && StringFind(wire,prefix)==0 &&
      StringFind(wire,"\",\"scope_id\":\""+s.mail.scope_id+"\",\"version\":1,",StringLen(prefix))>=0)
      FileDelete(path);
  }

void RecoveryLiveSessionStop(RecoveryLiveSession &s)
  {
   RecoveryPanelMailboxClose(s.mailbox);
   RecoveryClearPanelApply(s.panel_seen);
   RecoveryLiveSessionWithdraw(s);
   RecoverySessionReset(s.lifecycle);
   if(s.owns_mail)FileClose((int)s.owner_handle);
   s.owns_mail=false;s.owner_handle=INVALID_HANDLE;
   ZeroMemory(s.mail);
  }

bool RecoveryLiveSessionCurrent(const RecoveryLiveSession &s,const RecoverySessionContext &live)
  {
   return s.owns_mail && s.owner_handle!=INVALID_HANDLE &&
          RecoverySessionCurrent(s.lifecycle,live) &&
          TerminalInfoInteger(TERMINAL_CONNECTED)!=0 &&
          AccountInfoInteger(ACCOUNT_LOGIN)==live.login &&
          AccountInfoString(ACCOUNT_SERVER)==live.server && _Symbol==live.symbol;
  }

string RecoveryLiveUuid(const string category,const string seed)
  {
   const string hex=RecoverySessionSha256("RECOVERY_LIVE_SESSION_V1\t"+category+"\t"+seed);
   if(StringLen(hex)!=64)return "";
   return StringSubstr(hex,0,8)+"-"+StringSubstr(hex,8,4)+"-"+StringSubstr(hex,12,4)+
      "-"+StringSubstr(hex,16,4)+"-"+StringSubstr(hex,20,12);
  }

// Require the caller's scope to describe the configured account/chart. No
// market requests are made here. On failure the caller stays inactive.
// FileIsExist returns false and ERR_FILE_IS_DIRECTORY for a directory.
bool RecoveryLiveSessionPathUnused(const string path)
  {
   if(path=="")return false;
   ResetLastError();
   if(FileIsExist(path))return false;
   const int error=GetLastError();
   return error==0 || error==ERR_FILE_NOT_EXIST;
  }

bool RecoveryLiveSessionStart(RecoveryLiveSession &s,const RecoverySessionContext &live,
    const string profile_scope,const string instance_seed)
  {
   if(s.owns_mail || !RecoverySessionContextValid(live) || instance_seed=="" ||
      !RecoveryPanelCommandUUID(profile_scope) ||
      TerminalInfoInteger(TERMINAL_CONNECTED)==0 ||
      AccountInfoInteger(ACCOUNT_LOGIN)!=live.login ||
      AccountInfoString(ACCOUNT_SERVER)!=live.server || _Symbol!=live.symbol ||
      ChartID()!=live.chart) return false;
   RecoveryLiveSessionStop(s);
   FolderCreate(RecoveryLivePanelRoot);
   FolderCreate(RecoveryLivePanelRoot+"\\sessions");
   const int h=FileOpen(RecoveryLivePanelRoot+"\\writer.lock",FILE_READ|FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE)return false; // Another owner never gets a second writer.
   s.owns_mail=true;s.owner_handle=h;

   s.mail.scope_id=profile_scope;
   const string seed=profile_scope+":"+instance_seed;
   s.mail.boot_id=RecoveryLiveUuid("boot",seed);
   s.mail.context_id=RecoveryLiveUuid("context",seed);
   s.mail.generation_id=RecoveryLiveUuid("generation",seed);
   const string path=RecoveryPanelMailboxBootPath(s.mail.boot_id);
   if(!RecoveryLiveSessionPathUnused(path) || !RecoveryPanelCommandUUID(s.mail.context_id) ||
      !RecoveryPanelCommandUUID(s.mail.generation_id) ||
      !FolderCreate(path) || !FolderCreate(path+"\\commands") || !FolderCreate(path+"\\receipts") ||
      RecoverySessionInit(s.lifecycle,live,true,RecoveryPanelBootEpoch(s.mail.boot_id))!=RSL_NEW)
     {
      RecoveryLiveSessionStop(s);
      return false;
     }
   return true;
  }

bool RecoveryLiveSessionPublish(const RecoveryLiveSession &s,const RecoverySessionContext &live,
                                const RecoveryControlMode mode)
  {
   if(!RecoveryLiveSessionCurrent(s,live))return false;
   const string mode_name=mode==REC_CONTROL_MANUAL_WAIT ? "manual_wait" :
                          (mode==REC_CONTROL_PENDING_WAIT ? "pending_wait" :
                           (mode==REC_CONTROL_ADOPTED ? "active" : "ready"));
   const string path=RecoveryLivePanelRoot+"\\state.json",tmp=path+"."+s.mail.boot_id+".writing";
   const string wire=RecoveryLiveStatePrefix(s.mail)+mode_name+
      "\",\"scope_id\":\""+s.mail.scope_id+"\",\"version\":1,\"written_at\":"+(string)TimeLocal()+"}";
   const int len=StringLen(wire);
   uchar bytes[];
   if(StringToCharArray(wire,bytes,0,len,CP_UTF8)!=len)return false;
   const int h=FileOpen(tmp,FILE_WRITE|FILE_BIN);
   if(h==INVALID_HANDLE)return false;
   const uint n=FileWriteArray(h,bytes,0,len);
   FileFlush(h);FileClose(h);
   if(n!=(uint)len || !RecoveryLiveSessionCurrent(s,live) || !FileMove(tmp,0,path,FILE_REWRITE))
     {FileDelete(tmp);return false;}
   return true;
  }
// Rotate the panel generation only when the new ready state can be published.
// The verifier/watcher remain bound to this boot, and old mailbox files remain scoped.
bool RecoveryLiveNextGeneration(RecoveryLiveSession &s,const RecoverySessionContext &live)
  {
   if(!RecoveryLiveSessionCurrent(s,live))return false;
   const string previous=s.mail.generation_id;
   const string next=RecoveryLiveUuid("next-generation",previous);
   if(!RecoveryPanelCommandUUID(next) || next==previous)return false;
   s.mail.generation_id=next;
   if(RecoveryLiveSessionPublish(s,live,REC_CONTROL_IDLE))return true;
   s.mail.generation_id=previous;return false;
  }
#endif
