#ifndef RECOVERY_PANEL_SESSION_APPLY_MQH
#define RECOVERY_PANEL_SESSION_APPLY_MQH
#include "RecoveryPanelControl.mqh"
#include "RecoveryPanelCommandWire.mqh"
#include "RecoverySessionLifecycle.mqh"

enum RecoveryPanelApplyCode {
 RECP_INVALID=0, RECP_REJECTED, RECP_STAGED, RECP_DUPLICATE, RECP_CONFLICT
};
struct RecoveryPanelSeen {
 string command_id,digest;
 RecoveryPanelApplyCode original;
};
struct RecoveryPanelApplySession {
 string boot_id;
 RecoveryPanelSeen seen[];
};
struct RecoveryPanelApplyResult {
 RecoveryPanelApplyCode code,original;
 RecoveryPanelOperation operation;
 string command_id,digest;
};

void RecoveryClearPanelApply(RecoveryPanelApplySession &ram)
  {
   ram.boot_id="";
   ArrayFree(ram.seen);
  }

string RecoveryPanelBootEpoch(const string boot)
  {
   if(StringLen(boot)!=36)return "";
   string no_dashes=boot;
   if(StringReplace(no_dashes,"-","")!=4 || !RecoveryInvHex(no_dashes))return "";
   return no_dashes;
  }

// Called only with terminal-owned mailbox scope, never browser-supplied scope.
// Valid command identity is reserved before control changes. Return STAGED for
// ranges because no broker effect is proven yet.
RecoveryPanelApplyCode RecoveryDecodeAndStagePanel(
    const string wire,const string filename_id,const RecoveryPanelCommandScope &mail,
    const RecoverySessionLifecycle &life,const RecoverySessionContext &live,
    const RRSettings &cfg,const double tick,RecoveryPanelApplySession &ram,
    RecoveryPanelControl &control,RecoveryPanelApplyResult &out)
  {
   ZeroMemory(out);
   if(!RecoverySessionCurrent(life,live) || RecoveryPanelBootEpoch(mail.boot_id)!=life.epoch ||
      (ram.boot_id!="" && ram.boot_id!=mail.boot_id)) return RECP_INVALID;
   RecoveryPanelCommand parsed;
   if(!RecoveryDecodePanelCommand(wire,filename_id,mail,tick,parsed))return RECP_INVALID;
   out.command_id=parsed.command_id;out.digest=parsed.content_sha256;
   out.operation=parsed.operation;
   for(int i=ArraySize(ram.seen)-1;i>=0;i--)
     {
      if(ram.seen[i].command_id!=parsed.command_id)continue;
      if(ram.seen[i].digest!=parsed.content_sha256)
         {out.code=RECP_CONFLICT;return out.code;}
      out.original=ram.seen[i].original;
      out.code=RECP_DUPLICATE;return out.code;
     }
   RecoveryControlCommand next;
   ZeroMemory(next);
   if(parsed.operation==RPC_MANUAL_WAIT)next.op=REC_CONTROL_WAIT;
   else if(parsed.operation==RPC_MANUAL_CANCEL)next.op=REC_CONTROL_CANCEL;
   else if(parsed.operation==RPC_CONFIGURE_RANGES)
     {
      if(parsed.range_count<1 || parsed.range_count>2)return RECP_INVALID;
      next.op=REC_CONTROL_CONFIGURE;next.count=parsed.range_count;
      for(int i=0;i<next.count;i++)
        {
         next.ranges[i].id=parsed.ranges[i].range_id;
         next.ranges[i].side=parsed.ranges[i].side;
         next.ranges[i].price_ticks=parsed.ranges[i].price_ticks;
        }
     }
   else return RECP_INVALID;
   const int n=ArraySize(ram.seen);
   if(ArrayResize(ram.seen,n+1,32)!=n+1)return RECP_INVALID;
   ram.boot_id=mail.boot_id;
   ram.seen[n].command_id=parsed.command_id;
   ram.seen[n].digest=parsed.content_sha256;
   out.code=RecoveryApplyPanelControl(control,next,cfg)==REC_CONTROL_APPLIED ?
      RECP_STAGED:RECP_REJECTED;
   ram.seen[n].original=out.code;
   return out.code;
  }
#endif
