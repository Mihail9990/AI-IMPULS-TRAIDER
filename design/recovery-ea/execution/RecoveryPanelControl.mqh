#ifndef RECOVERY_PANEL_CONTROL_MQH
#define RECOVERY_PANEL_CONTROL_MQH
#include "../cycle/RecoveryRangeCoordinator.mqh"

enum RecoveryControlMode { REC_CONTROL_IDLE=0,REC_CONTROL_MANUAL_WAIT,REC_CONTROL_RANGES,REC_CONTROL_ADOPTED };
enum RecoveryControlOp { REC_CONTROL_UNSET=0,REC_CONTROL_WAIT,REC_CONTROL_CANCEL,REC_CONTROL_CONFIGURE };
enum RecoveryControlResult { REC_CONTROL_REJECTED=0,REC_CONTROL_APPLIED };

struct RecoveryControlRange {
 int id,side;
 long price_ticks; // already normalized on the exact current symbol grid
};
struct RecoveryControlCommand {
 RecoveryControlOp op;
 int count;
 RecoveryControlRange ranges[2];
};
struct RecoveryPanelControl {
 RecoveryControlMode mode;
 bool configured[2];
 RRSettings settings;
 RRRangeState ranges[2]; // physical slot is range ID - 1
};

// Acts on a normalized, authenticated, once-consumed current-session
// command. No terminal/broker calls here. Staging a range is not proof that an
// order was placed; the owner must dispatch and verify the primary STOPs.
RecoveryControlResult RecoveryApplyPanelControl(RecoveryPanelControl &state,
    const RecoveryControlCommand &command,const RRSettings &cfg) {
 if(command.op==REC_CONTROL_WAIT || command.op==REC_CONTROL_CANCEL)
   {
    if(command.count!=0)return REC_CONTROL_REJECTED;
    if(command.op==REC_CONTROL_WAIT && state.mode==REC_CONTROL_IDLE)
      {state.mode=REC_CONTROL_MANUAL_WAIT;return REC_CONTROL_APPLIED;}
    if(command.op==REC_CONTROL_CANCEL && state.mode==REC_CONTROL_MANUAL_WAIT)
      {state.mode=REC_CONTROL_IDLE;return REC_CONTROL_APPLIED;}
    return REC_CONTROL_REJECTED;
   }
 if(command.op!=REC_CONTROL_CONFIGURE || state.mode!=REC_CONTROL_IDLE ||
    command.count<1 || command.count>2 || !RRSettingsValid(cfg))
    return REC_CONTROL_REJECTED;
 RecoveryPanelControl next;
 ZeroMemory(next);
 next.settings=cfg;
 for(int i=0;i<command.count;i++)
   {
    const int id=command.ranges[i].id;
    if(id<1 || id>2)return REC_CONTROL_REJECTED;
    if(next.configured[id-1] ||
       !RRNewRange(next.ranges[id-1],cfg,id,command.ranges[i].side,command.ranges[i].price_ticks))
       return REC_CONTROL_REJECTED;
    next.configured[id-1]=true;
   }
 next.mode=REC_CONTROL_RANGES;
 state=next;
 return REC_CONTROL_APPLIED;
 }
#endif
