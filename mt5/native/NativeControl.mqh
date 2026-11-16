#ifndef IMPULS_NATIVE_CONTROL
#define IMPULS_NATIVE_CONTROL
string control_keys[],control_values[];
string ControlValue(string key){for(int i=0;i<ArraySize(control_keys);i++)if(control_keys[i]==key)return control_values[i];return "";}
bool ReadControl(string name) {
 string text=ReadNativeFile(name),lines[];int n=StringSplit(text,'\n',lines);
 ArrayResize(control_keys,0);ArrayResize(control_values,0);
 if(StringLen(text)>12000 || n<8 || n>40)return false;
 for(int i=0;i<n;i++) {
  if(lines[i]=="")continue;int sep=StringFind(lines[i],"=");if(sep<1)return false;
  string key=StringSubstr(lines[i],0,sep),value=StringSubstr(lines[i],sep+1);
  for(int j=0;j<ArraySize(control_keys);j++)if(control_keys[j]==key)return false;
  int k=ArraySize(control_keys);ArrayResize(control_keys,k+1);ArrayResize(control_values,k+1);control_keys[k]=key;control_values[k]=value;
 }
 return true;
}
string JsonScalar(string body,string key) {
 string token="\""+key+"\":";int start=StringFind(body,token);if(start<0)return "";start+=StringLen(token);
 if(StringSubstr(body,start,1)=="\""){start++;int end=StringFind(body,"\"",start);return end<0?"":StringSubstr(body,start,end-start);}
 int end=start;while(end<StringLen(body) && StringFind(",}\r\n",StringSubstr(body,end,1))<0)end++;return StringSubstr(body,start,end-start);
}
bool ControlReceipt(string id,string digest,string status,string reason) {
 string old=ReadNativeFile("native_receipts\\"+id+".json");
 if(JsonScalar(old,"status")==status && JsonScalar(old,"reason")==reason)return true;
 long seq=StringToInteger(JsonScalar(old,"sequence"))+1;
 string body="{\"protocol\":\"native_panel_receipt_v1\",\"id\":"+NativeQ(id)+",\"identity\":{\"installation\":"+NativeQ(InstallationId)+",\"login\":"+NativeU((ulong)ExpectedLogin)+",\"server\":"+NativeQ(ExpectedServer)+",\"symbol\":"+NativeQ(TradeSymbol)+"},\"digest\":"+NativeQ(digest)+",\"sequence\":"+(string)seq+",\"status\":"+NativeQ(status)+",\"reason\":"+NativeQ(reason)+"}";
 return NativePublish(MailboxFolder,"native_receipts\\"+id+".json",body);
}
void FinalizeControl(string id,string digest) {
 string prior=JsonScalar(ReadNativeFile("native_receipts\\"+id+".json"),"status");
 if(prior=="BROKER_CONFIRMED" || prior=="APPLIED" || prior=="REJECTED")return;
 int count=0;bool pending=(native_close_all==id || native_cancel_control==id),rejected=false,waiting_tick=false,broker_sent=false,reconciling=false,filled_before_cancel=false;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].control==id) {
  count++;if(commands[i].status=="RECONCILING")reconciling=true;if(commands[i].kind=="CANCEL" && commands[i].status=="DONE" && commands[i].exit_kind==5)filled_before_cancel=true;if(commands[i].status=="UNKNOWN" || commands[i].status=="DONE" || commands[i].status=="PENDING" || commands[i].status=="OPEN" || commands[i].status=="PARTIAL" || commands[i].status=="CLOSED")broker_sent=true;if(commands[i].status=="WAIT_TICK")waiting_tick=true;if(commands[i].status=="UNKNOWN" || commands[i].status=="RECONCILING" || commands[i].status=="WAIT_TICK" || commands[i].status=="READY")pending=true;
  if(commands[i].status=="REJECTED")rejected=true;
 }
 if(count==0){ControlReceipt(id,digest,"UNKNOWN","INTERRUPTED_CONTROL_REQUIRES_REVIEW");return;}
 if(pending && reconciling && !broker_sent){ControlReceipt(id,digest,prior=="AWAITING_BROKER"?"AWAITING_BROKER":"EA_ACCEPTED","RECONCILING_MISSING_ORDER_NO_REMOVE_SENT");return;}
 if(pending)ControlReceipt(id,digest,(waiting_tick && !broker_sent && prior!="AWAITING_BROKER")?"EA_ACCEPTED":"AWAITING_BROKER",waiting_tick?(broker_sent?"WAITING_NEXT_TICK_AND_BROKER_READBACK":"WAITING_NEXT_VALID_TICK_NOT_SENT"):"WAITING_FOR_BROKER_READBACK_NO_RETRY");
 else ControlReceipt(id,digest,rejected?"REJECTED":"BROKER_CONFIRMED",rejected?"SOME_OPERATIONS_REJECTED_REVIEW_POSITIONS":(filled_before_cancel?"ORDER_FILLED_BEFORE_CANCEL_POSITION_RECONCILED":"BROKER_STATE_OBSERVED"));
}
void NativeModifyPositions(string action) {
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].position>0 && !commands[i].closed) {
  NativeCommand c=commands[i];if(UnknownResource(c.position) || !SelectNativePosition(c.position))continue;
  double entry=PositionGetDouble(POSITION_PRICE_OPEN),volume=PositionGetDouble(POSITION_VOLUME),sl=PositionGetDouble(POSITION_SL),tp=PositionGetDouble(POSITION_TP),target=sl;
  if(action=="close_all") {
   bool already=false;for(int j=0;j<ArraySize(commands);j++)if(commands[j].kind=="CLOSE" && commands[j].position==c.position && commands[j].control==native_control)already=true;
   if(already)continue;
   int k=AddNative("CLOSE",c.side,volume,0,0,0,c.position);SendNative(k);continue;}
  if(action=="sl_at_entry")target=entry;
  else if(action=="sl_distance") {double distance=StringToDouble(ControlValue("distance"));if(!PolicyPositive(distance))continue;target=entry-c.side*distance;}
  else if(action=="modify_level") {
   if((ulong)StringToInteger(ControlValue("position_id"))!=c.position)continue;
   double price=StringToDouble(ControlValue("price"));
   if(ControlValue("level")=="SL")target=price;
   else if(ControlValue("level")=="TP" && ProfitableTarget(c.side==1,entry,price)) {
    tp=price;if(!NativePublish(MailboxFolder,"manual-tp-"+(string)c.position+".txt",NativeDecimal(tp)))continue;
   }
   else continue;
  }
  if(sl>0 && (c.side==1?target<sl:target>sl))continue;
  int k=AddNative("MODIFY",c.side,volume,0,target,tp,c.position);SendNative(k);
 }
}
string NativeControlTargetBasis(string action) {
 if(!NativeIdentity(ExpectedLogin,ExpectedServer))return "";
 string basis="";int count=0;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].position>0 && !commands[i].closed) {
  NativeCommand c=commands[i];
  if(action=="modify_level" && c.position!=(ulong)StringToInteger(ControlValue("position_id")))continue;
  if((action=="triggers_at_sl" || action=="reverse_ladder") && c.basket!=native_basket)continue;
  if(!SelectNativePosition(c.position))return "";
  double sl=PositionGetDouble(POSITION_SL),tp=PositionGetDouble(POSITION_TP);
  if(action=="modify_level" && ((ControlValue("expected_sl")!="" && sl!=StringToDouble(ControlValue("expected_sl"))) || (ControlValue("expected_tp")!="" && tp!=StringToDouble(ControlValue("expected_tp")))))return "";
  basis+=(string)c.position+":"+(string)PositionGetInteger(POSITION_TICKET)+":"+(string)c.basket+":"+NativeDecimal(PositionGetDouble(POSITION_VOLUME))+":"+NativeDecimal(PositionGetDouble(POSITION_PRICE_OPEN))+":"+NativeDecimal(sl)+":"+NativeDecimal(tp)+"\n";count++;
 }
 if(action=="triggers_at_sl")basis+="CONFIG:"+(string)native_steps+":"+NativeDecimal(native_recovery_sl)+":"+native_profiles[native_steps]+":"+(native_steps<10?native_profiles[native_steps+1]:"");
 return count>0?basis:"";
}
void ProcessNativeControl(string relative) {
 if(!ReadControl(relative))return;
 string id=ControlValue("id"),digest=ControlValue("digest"),action=ControlValue("action");
 if(!NativeFolder(id) || StringLen(id)!=36 || StringLen(digest)!=64 || ControlValue("protocol")!="native_panel_wire_v1" ||
  ControlValue("installation")!=InstallationId || ControlValue("login")!=(string)ExpectedLogin ||
  ControlValue("server")!=ExpectedServer || ControlValue("symbol")!=TradeSymbol)return;
 string accepted="native_accepted\\"+id+".cmd";
 string prior=JsonScalar(ReadNativeFile("native_receipts\\"+id+".json"),"status");
 if(prior=="BROKER_CONFIRMED" || prior=="APPLIED" || prior=="REJECTED") {
  FileDelete(MailboxFolder+"\\native_requests\\"+id+".cmd",FILE_COMMON);
  if(FileIsExist(MailboxFolder+"\\"+accepted,FILE_COMMON))FileMove(MailboxFolder+"\\"+accepted,FILE_COMMON,MailboxFolder+"\\native_accepted\\"+id+".done",FILE_COMMON);
  return;
 }
 if(FileIsExist(MailboxFolder+"\\"+accepted,FILE_COMMON)){FinalizeControl(id,digest);return;}
 if(!AllowTrading){ControlReceipt(id,digest,"REJECTED","EXECUTION_DISABLED");return;}
 bool price_action=action=="sl_at_entry" || action=="sl_distance" || action=="modify_level" || action=="reverse_ladder" || action=="triggers_at_sl";
 bool independent=action=="stop_new_entries" || action=="cancel_triggers" || action=="close_all";
 string deferred_name="native_deferred\\"+id+".basis",deferred=ReadNativeFile(deferred_name);
 if(price_action && deferred!="") {
  // Global revision changes are not target changes. Revalidate the bound resource instead.
  if(!NativeContext()){ControlReceipt(id,digest,"EA_ACCEPTED","WAITING_CONNECTION_NOT_SENT");return;}
  string basis=NativeControlTargetBasis(action);
  if(basis=="" || deferred!=digest+"\n"+basis){ControlReceipt(id,digest,"REJECTED","DEFERRED_TARGET_CHANGED_OR_UNAVAILABLE");return;}
 } else {
  if(!independent && (ulong)StringToInteger(ControlValue("revision"))!=native_revision){ControlReceipt(id,digest,"REJECTED","STALE_NATIVE_REVISION_AT_ACCEPTANCE");return;}
  if(price_action && (!NativeContext() || !NativeTickObserved())) {
   string basis=NativeControlTargetBasis(action);
   if(basis==""){ControlReceipt(id,digest,"REJECTED","TARGET_BASIS_UNAVAILABLE_NO_ACTION_SENT");return;}
   if(!NativePublish(MailboxFolder,deferred_name,digest+"\n"+basis))return;
  }
 }
 if(!NativeContext()){ControlReceipt(id,digest,"EA_ACCEPTED","WAITING_CONNECTION_NOT_SENT");return;}
 if(price_action && !NativeTickObserved()){ControlReceipt(id,digest,"EA_ACCEPTED","WAITING_NEXT_VALID_TICK_NOT_SENT");return;}

 if(price_action && NativeControlTargetBasis(action)==""){ControlReceipt(id,digest,"REJECTED","TARGET_CHANGED_OR_UNAVAILABLE_NO_ACTION_SENT");return;}
 if(!NativePublish(MailboxFolder,accepted,ReadNativeFile(relative)))return;
 if(!ControlReceipt(id,digest,"EA_ACCEPTED","REQUEST_ACCEPTED_NOT_EXECUTED"))return;
 native_control=id;
 bool local=false;
 if(action=="stop_new_entries") {native_paused=true;native_cancel_control=id;if(SaveNative())CancelNativeEntries();local=true;}
 else if(action=="cancel_triggers") {native_paused=true;native_cancel_control=id;if(SaveNative())CancelNativeEntries();local=true;}
 else if(action=="close_all") {native_paused=true;native_close_all=id;if(SaveNative()){CancelNativeEntries();NativeModifyPositions(action);}}
 else if(action=="sl_at_entry" || action=="sl_distance" || action=="modify_level")NativeModifyPositions(action);
 else if(action=="reverse_ladder") {
  bool enable=ControlValue("enabled")=="true",tp1=false,protected_positions=true;
  for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_basket) {
   if(commands[i].stage==0 && commands[i].closed && commands[i].exit_kind==3)tp1=true;
   if(!commands[i].closed && commands[i].position>0) {
    if(UnknownResource(commands[i].position) || !SelectNativePosition(commands[i].position))protected_positions=false;
    else {double e=PositionGetDouble(POSITION_PRICE_OPEN),sl=PositionGetDouble(POSITION_SL);if(sl<=0 || (commands[i].side==1?sl<e:sl>e))protected_positions=false;}
   }
  }
  if(enable && (!tp1 || !protected_positions))ControlReceipt(id,digest,"REJECTED","TP1_AND_ENTRY_SL_CONFIRMATION_REQUIRED");
  else {native_reverse=enable;if(!enable){for(int i=0;i<ArraySize(commands);i++)if(commands[i].basket==native_basket && commands[i].position>0)FileDelete(MailboxFolder+"\\reverse-"+(string)native_basket+"-"+(string)commands[i].position+".txt",FILE_COMMON);NativeModifyPositions("sl_at_entry");}local=true;}
 }
 else if(action=="start") {
  if(native_reason=="FULL_SL_010_MANUAL" || native_close_all!="" || AnyUnknown() || !NativeOwnedOnly())ControlReceipt(id,digest,"REJECTED","RECONCILIATION_OR_MANUAL_MODE_REQUIRED");
  else {native_paused=false;native_reason="";local=true;}
 }
 else if(action=="resume_manual") {
  int steps=(int)StringToInteger(ControlValue("steps"));double profile[];bool ready=native_reason=="FULL_SL_010_MANUAL" && !AnyUnknown() && native_close_all=="" && native_cancel_control=="" && NativeOwnedOnly() && native_fixed_target==0 && ProfileNative(steps,profile);
  for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && !commands[i].closed && commands[i].status!="CANCELLED" && commands[i].status!="REJECTED")ready=false;
  if(ControlValue("profile")!=native_profiles[steps>=2 && steps<=10?steps:0] || ControlValue("confirmed")!="true")ready=false;
  if(!ready)ControlReceipt(id,digest,"REJECTED","MANUAL_RESUME_PROFILE_OR_RECONCILIATION_INVALID");
  else{native_applied=native_basket;native_basket++;native_steps=steps;native_next_steps=0;native_split_two=steps==2;native_bar=0;native_reverse=false;native_paused=false;native_reason="";local=true;}
 }
 else if(action=="select_profile") {int steps=(int)StringToInteger(ControlValue("steps"));double values[];if(!ProfileNative(steps,values))ControlReceipt(id,digest,"REJECTED","INVALID_NEXT_PROFILE");else {native_next_steps=steps;local=true;}}
 else if(action=="save_sl_monitor") {
  double first=StringToDouble(ControlValue("first")),second=StringToDouble(ControlValue("second")),a=StringToDouble(ControlValue("first_offset")),b=StringToDouble(ControlValue("second_offset"));
  if(!PolicyPositive(first)||!PolicyPositive(second)||second<=first||!MathIsValidNumber(a)||!MathIsValidNumber(b)||b<a)ControlReceipt(id,digest,"REJECTED","INVALID_SL_MONITOR_SETTINGS");
  else{native_sl_first=first;native_sl_second=second;native_sl_first_offset=a;native_sl_second_offset=b;local=true;}
 }
 else if(action=="reconcile") {native_cursor=0;local=true;}
 else if(action=="reset_pause" || action=="writeoff_recovery") {
  bool flat=!AnyUnknown() && NativeOwnedOnly() && native_close_all=="" && native_cancel_control=="";
  for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && !commands[i].closed && commands[i].status!="CANCELLED" && commands[i].status!="REJECTED")flat=false;
  if(!flat || !native_paused || (action=="reset_pause" && native_reason=="FULL_SL_010_MANUAL") || (action=="writeoff_recovery" && (ControlValue("confirmed")!="true" || MathAbs(StringToDouble(ControlValue("expected_debt"))-NativeDebt())>1e-8)))ControlReceipt(id,digest,"REJECTED","FLAT_PAUSED_CONFIRMED_STATE_REQUIRED");
  else{if(action=="writeoff_recovery"){native_credit+=NativeDebt();native_applied=native_basket;native_basket++;native_steps=2;native_next_steps=0;native_split_two=false;native_fixed_target=0;native_fixed_source=0;}native_reason="";local=true;}
 }
 else if(action=="cancel_initial") {
  if(!NativeInitial() || native_fixed_target>0)ControlReceipt(id,digest,"REJECTED","NO_INITIAL_TRIGGER");
  else{for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].basket==native_basket && commands[i].status=="PENDING" && !UnknownResource(0,commands[i].order)){int k=AddNative("CANCEL",commands[i].side,0,0,0,0,0,commands[i].order);SendNative(k);}}
 }
 else if(action=="save_settings") {
  bool valid=true;int selected=ControlValue("next_steps")==""?native_next_steps:(int)StringToInteger(ControlValue("next_steps"));if(selected!=0 && (selected<2 || selected>10))valid=false;double sl=StringToDouble(ControlValue("initial_sl")),tp=StringToDouble(ControlValue("initial_tp")),offset=StringToDouble(ControlValue("offset")),rsl=StringToDouble(ControlValue("recovery_sl")),roffset=StringToDouble(ControlValue("recovery_offset"));
  if(!PolicyPositive(sl)||!PolicyPositive(tp)||!PolicyPositive(offset)||!PolicyPositive(rsl)||!PolicyPositive(roffset))valid=false;
  for(int n=2;n<=10;n++) {
   string profile=ControlValue("profile_"+(string)n),parts[];if(profile=="")continue;
   if(StringSplit(profile,',',parts)!=n){valid=false;break;}
   for(int j=0;j<n;j++)if(!NativePrice(StringToDouble(parts[j])) || (j>0 && StringToDouble(parts[j])<=StringToDouble(parts[j-1])))valid=false;
  }
  if(valid){native_next_steps=selected;native_sl=sl;native_tp=tp;native_offset=offset;native_recovery_sl=rsl;native_recovery_offset=roffset;for(int n=2;n<=10;n++)native_profiles[n]=ControlValue("profile_"+(string)n);local=true;}
  else ControlReceipt(id,digest,"REJECTED","SETTINGS_INVALID_OR_ACTIVE_BASKET");
 } else if(action=="triggers_at_sl"){string reason;if(!ArmNativeSLTriggers(reason))ControlReceipt(id,digest,"REJECTED",reason);}
 else ControlReceipt(id,digest,"REJECTED","UNSUPPORTED_ACTION");
 if(!SaveNative()){native_control="";return;}
 int effects=0;for(int i=0;i<ArraySize(commands);i++)if(commands[i].control==id)effects++;
 if(effects>0)FinalizeControl(id,digest);
 else if(native_close_all==id || native_cancel_control==id)ControlReceipt(id,digest,"AWAITING_BROKER","WAITING_FOR_RESOURCE_RECONCILIATION");
 else if(local)ControlReceipt(id,digest,"APPLIED","LOCAL_STATE_SAVED");
 else if(JsonScalar(ReadNativeFile("native_receipts\\"+id+".json"),"status")=="EA_ACCEPTED")ControlReceipt(id,digest,"REJECTED","NO_ELIGIBLE_PROVED_RESOURCES");
 native_control="";
}
void ContinueNativeCancellation() {
 if(native_cancel_control=="" || !NativeContext())return;
 native_control=native_cancel_control;CancelNativeEntries();native_control="";
 bool outstanding=false;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && (commands[i].status=="PENDING" || commands[i].status=="PARTIAL" || commands[i].status=="UNKNOWN" || commands[i].status=="RECONCILING" || commands[i].status=="WAIT_TICK" || commands[i].status=="READY"))outstanding=true;
 if(!outstanding){string id=native_cancel_control;native_cancel_control="";if(SaveNative() && ReadControl("native_accepted\\"+id+".cmd")) {
  int effects=0;for(int i=0;i<ArraySize(commands);i++)if(commands[i].control==id)effects++;
  if(effects==0)ControlReceipt(id,ControlValue("digest"),"APPLIED","NO_PENDING_NATIVE_ORDERS");
 }}
}
void ContinueNativeCloseAll() {
 if(native_close_all=="" || !NativeContext())return;
 native_control=native_close_all;CancelNativeEntries();NativeModifyPositions("close_all");native_control="";
 bool rejected=false;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].control==native_close_all && commands[i].status=="REJECTED" && (commands[i].kind!="CANCEL" || OrderSelect(commands[i].order)))rejected=true;
 if(rejected){native_close_all="";native_reason="CLOSE_ALL_PARTIAL_FAILURE_REQUIRES_REVIEW";SaveNative();return;}
 bool outstanding=false;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].status!="CANCELLED" && commands[i].status!="REJECTED" && !commands[i].closed)outstanding=true;
 if(!outstanding){string id=native_close_all;native_close_all="";native_reason="OWNER_CLOSE_COMPLETED_MANUAL_REVIEW";if(SaveNative() && ReadControl("native_accepted\\"+id+".cmd")) {
  int effects=0;for(int i=0;i<ArraySize(commands);i++)if(commands[i].control==id)effects++;
  if(effects==0)ControlReceipt(id,ControlValue("digest"),"BROKER_CONFIRMED","NO_NATIVE_POSITIONS_OR_PENDING_ORDERS");
 }}
}
void RefreshNativeSLAcks() {
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].position>0) {
  string file="sl-ack-"+(string)commands[i].position+".json",body=ReadNativeFile(file);
  string id=JsonScalar(body,"command"),revision=JsonScalar(body,"revision");
  if(id=="" || JsonScalar(body,"status")!="AWAITING_BROKER")continue;
  for(int j=0;j<ArraySize(commands);j++)if(commands[j].id==id && (commands[j].status=="DONE" || commands[j].status=="REJECTED"))
   NativePublish(MailboxFolder,file,"{\"revision\":"+NativeQ(revision)+",\"command\":"+NativeQ(id)+",\"status\":"+NativeQ(commands[j].status=="DONE"?"BROKER_CONFIRMED":"REJECTED")+"}");
 }
}
int native_control_cursor=0;
void PollNativeControls() {
 string names[];int count=0;
 for(int directory=0;directory<2;directory++) {
  string prefix=directory==0?"native_requests\\":"native_accepted\\",file;
  long h=FileFindFirst(MailboxFolder+"\\"+prefix+"*.cmd",file,FILE_COMMON);if(h==INVALID_HANDLE)continue;
  do {if(count>=1024){native_reason="CONTROL_BACKLOG_REQUIRES_REVIEW";break;}ArrayResize(names,count+1);names[count++]=prefix+file;}while(FileFindNext(h,file));FileFindClose(h);
 }
 if(count==0)return;
 for(int i=0;i<24 && i<count;i++){native_control_cursor%=count;ProcessNativeControl(names[native_control_cursor]);native_control_cursor++;}
}
void ApplyNativeSLRecommendations() {
 if(!NativeContext() || !NativeTickObserved())return;
 for(int i=0;i<ArraySize(commands);i++)if(commands[i].kind=="STOP" && commands[i].position>0 && !commands[i].closed) {
  NativeCommand c=commands[i];if(UnknownResource(c.position) || !SelectNativePosition(c.position))continue;
  string name="sl-"+(string)c.basket+"-"+(string)c.position+".json",body=ReadNativeFile(name);
  if(JsonScalar(body,"protocol")!="native_sl_proposal_v1" || JsonScalar(body,"status")!="RECOMMENDED" ||
    JsonScalar(body,"login")!=(string)ExpectedLogin || JsonScalar(body,"server")!=ExpectedServer || JsonScalar(body,"symbol")!=TradeSymbol ||
    JsonScalar(body,"cycle_id")!=(string)c.basket || JsonScalar(body,"position_id")!=(string)c.position)continue;
  if(JsonScalar(body,"first_threshold")=="" || JsonScalar(body,"second_threshold")=="" || JsonScalar(body,"first_offset")=="" || JsonScalar(body,"second_offset")=="" || StringToDouble(JsonScalar(body,"first_threshold"))!=native_sl_first || StringToDouble(JsonScalar(body,"second_threshold"))!=native_sl_second || StringToDouble(JsonScalar(body,"first_offset"))!=native_sl_first_offset || StringToDouble(JsonScalar(body,"second_offset"))!=native_sl_second_offset)continue;
  if(PositionGetDouble(POSITION_SL)!=StringToDouble(JsonScalar(body,"expected_sl")) || PositionGetDouble(POSITION_TP)!=StringToDouble(JsonScalar(body,"expected_tp")) || PositionGetDouble(POSITION_VOLUME)!=StringToDouble(JsonScalar(body,"volume")) || PositionGetDouble(POSITION_PRICE_OPEN)!=StringToDouble(JsonScalar(body,"entry")))continue;
  MqlTick q;if(!SymbolInfoTick(TradeSymbol,q))continue;
  long stamp=StringToInteger(JsonScalar(body,"quote_time_msc"));if(stamp<=0 || stamp>q.time_msc)continue;
  string ack=ReadNativeFile("sl-ack-"+(string)c.position+".json");
  if(JsonScalar(ack,"revision")==JsonScalar(body,"revision"))continue;
  double target=StringToDouble(JsonScalar(body,"recommended_sl"));
  int k=AddNative("MODIFY",c.side,PositionGetDouble(POSITION_VOLUME),0,target,PositionGetDouble(POSITION_TP),c.position);SendNative(k);
  // Same-resource UNKNOWN blocks repeat, successful readback changes expected_sl.
  // Refusal is retained: identical recommendation revision is not retried.
  if(k>=0)NativePublish(MailboxFolder,"sl-ack-"+(string)c.position+".json","{\"revision\":"+NativeQ(JsonScalar(body,"revision"))+",\"command\":"+NativeQ(commands[k].id)+",\"status\":\"AWAITING_BROKER\"}");
 }
}
#endif
