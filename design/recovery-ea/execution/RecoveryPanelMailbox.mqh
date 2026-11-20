#ifndef RECOVERY_PANEL_MAILBOX_MQH
#define RECOVERY_PANEL_MAILBOX_MQH
#include "RecoveryPanelSessionApply.mqh"

struct RecoveryPanelMailbox {
 long handle;
 string filename,boot_id;
 bool initialized,diagnostic;
};

void RecoveryPanelMailboxClose(RecoveryPanelMailbox &m) {
 if(m.initialized && m.handle!=INVALID_HANDLE)FileFindClose(m.handle);
 m.handle=INVALID_HANDLE;m.filename="";m.boot_id="";m.initialized=false;m.diagnostic=false;
}

string RecoveryPanelMailboxBootPath(const string boot_id,const bool diagnostic=false) {
 if(!RecoveryPanelCommandUUID(boot_id))return "";
 return (diagnostic ? "RECOVERY_PANEL_PROBE\\sessions\\" : "RECOVERY_PANEL\\sessions\\")+boot_id;
}
bool RecoveryPanelMailboxCurrent(const RecoveryPanelCommandScope &mail,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live) {
 return RecoverySessionCurrent(life,live) && RecoveryPanelCommandUUID(mail.boot_id) &&
        RecoveryPanelBootEpoch(mail.boot_id)==life.epoch &&
        RecoveryPanelCommandUUID(mail.scope_id) && RecoveryPanelCommandUUID(mail.context_id) &&
        RecoveryPanelCommandUUID(mail.generation_id);
}

// The supplied name is one mailbox-owned basename, never a relative path.
bool RecoveryPanelMailboxFilename(const string leaf,string &id) {
 id="";
 if(StringLen(leaf)!=41 || StringSubstr(leaf,36)!=".json")return false;
 const string candidate=StringSubstr(leaf,0,36);
 if(!RecoveryPanelCommandUUID(candidate))return false;
 id=candidate;
 return true;
}
bool RecoveryPanelMailboxRead(const string path,string &wire) {
 wire="";
 const int h=FileOpen(path,FILE_READ|FILE_BIN|FILE_SHARE_READ);
 if(h==INVALID_HANDLE)return false;
 const ulong size=FileSize(h);
 if(size==0 || size>1024) {FileClose(h);return false;}
 uchar bytes[];
 const uint copied=FileReadArray(h,bytes,0,(int)size);
 const bool unchanged=(size==FileSize(h));
 FileClose(h);
 if(!unchanged || copied!=(uint)size)return false;
 // Protocol is canonical printable ASCII; no BOM, NUL, Unicode or newline.
 for(int i=0;i<(int)size;i++)if(bytes[i]<32 || bytes[i]>126)return false;
 wire=CharArrayToString(bytes,0,(int)size,CP_UTF8);
 return StringLen(wire)==(int)size;
}

bool RecoveryPanelMailboxReceiptText(const RecoveryPanelCommandScope &mail,
    const RecoveryPanelApplyResult &result,string &wire) {
 wire="";
 if(!RecoveryPanelCommandUUID(mail.boot_id) || !RecoveryPanelCommandUUID(mail.context_id) ||
    !RecoveryPanelCommandUUID(mail.generation_id) || !RecoveryPanelCommandUUID(mail.scope_id) ||
    !RecoveryPanelCommandUUID(result.command_id) || !RecoveryPanelCommandHex(result.digest,64))
    return false;
 const RecoveryPanelApplyCode code=result.code==RECP_DUPLICATE ? result.original : result.code;
 if(result.operation!=RPC_MANUAL_WAIT && result.operation!=RPC_MANUAL_CANCEL &&
    result.operation!=RPC_CONFIGURE_RANGES)return false;
 string status,reason;
 if(code==RECP_REJECTED) {status="rejected";reason="control_rejected";}
 else if(code==RECP_STAGED && result.operation==RPC_CONFIGURE_RANGES)
    {status="accepted";reason="range_staged";}
 else if(code==RECP_STAGED)
    {status="applied";reason="manual_mode_changed";}
 else return false;
 wire="{\"boot_id\":\""+mail.boot_id+"\",\"command_id\":\""+result.command_id+
      "\",\"content_sha256\":\""+result.digest+"\",\"context_id\":\""+mail.context_id+
      "\",\"generation_id\":\""+mail.generation_id+"\",\"reason\":\""+reason+
      "\",\"scope_id\":\""+mail.scope_id+"\",\"status\":\""+status+"\",\"version\":1}";
 return true;
}

bool RecoveryPanelMailboxReceipt(const RecoveryPanelCommandScope &mail,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RecoveryPanelApplyResult &result,const bool diagnostic=false) {
 string wire;
 const string boot=RecoveryPanelMailboxBootPath(mail.boot_id,diagnostic);
 if(boot=="" || !RecoveryPanelMailboxCurrent(mail,life,live) ||
    !RecoveryPanelMailboxReceiptText(mail,result,wire))return false;
 const string base=boot+"\\receipts\\"+result.command_id;
 const string dst=base+".json",tmp=base+".writing";
 if(FileIsExist(dst) || FileIsExist(tmp))return false;
 uchar bytes[];
 const int n=StringLen(wire);
 if(StringToCharArray(wire,bytes,0,n,CP_UTF8)!=n || ArraySize(bytes)!=n)return false;
 const int file=FileOpen(tmp,FILE_WRITE|FILE_BIN);
 if(file==INVALID_HANDLE)return false;
 const uint written=FileWriteArray(file,bytes,0,n);
 FileFlush(file);FileClose(file);
 if(written==(uint)n && RecoveryPanelMailboxCurrent(mail,life,live) && FileMove(tmp,0,dst,0))
    return true; // create only; do not replace any existing receipt.
 FileDelete(tmp);
 return false;
}

// Visits up to 16 names and applies at most one new valid command per timer.
// A search cursor is advanced before yielding, so old files cannot pin scan.
bool RecoveryPanelMailboxPoll(RecoveryPanelMailbox &m,const RecoveryPanelCommandScope &mail,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RRSettings &cfg,const double tick,RecoveryPanelApplySession &ram,
    RecoveryPanelControl &control,RecoveryPanelApplyResult &out,const bool diagnostic=false) {
 ZeroMemory(out);
 if(!RecoveryPanelMailboxCurrent(mail,life,live)) {RecoveryPanelMailboxClose(m);return false;}
 if(!m.initialized || m.boot_id!=mail.boot_id || m.diagnostic!=diagnostic) {
    RecoveryPanelMailboxClose(m);
    m.boot_id=mail.boot_id;m.initialized=true;m.diagnostic=diagnostic;
 }
 const string boot=RecoveryPanelMailboxBootPath(mail.boot_id,diagnostic);
 const string commands=boot+"\\commands\\",receipts=boot+"\\receipts\\";
 if(m.handle==INVALID_HANDLE) {
    m.handle=FileFindFirst(commands+"*.json",m.filename);
    if(m.handle==INVALID_HANDLE)return false;
 }
 for(int i=0;i<16;i++) {
    const string leaf=m.filename;
    if(!FileFindNext(m.handle,m.filename)) {
       FileFindClose(m.handle);m.handle=INVALID_HANDLE;m.filename="";
    }
    string id,wire;
    if(RecoveryPanelMailboxFilename(leaf,id) && !FileIsExist(receipts+leaf) &&
       RecoveryPanelMailboxRead(commands+leaf,wire) && RecoveryPanelMailboxCurrent(mail,life,live)) {
       RecoveryPanelApplyResult result;
       const RecoveryPanelApplyCode code=RecoveryDecodeAndStagePanel(
         wire,id,mail,life,live,cfg,tick,ram,control,result);
       if(code==RECP_STAGED || code==RECP_REJECTED || code==RECP_DUPLICATE) {
          // Receipt failure does not rerun a consumed command. The next pass
          // can recover a missing receipt from RAM and the immutable command.
          RecoveryPanelMailboxReceipt(mail,life,live,result,diagnostic);
          if(code!=RECP_DUPLICATE) {out=result;return true;}
       }
    }
    if(m.handle==INVALID_HANDLE)break;
 }
 return false;
}
#endif
