void reset(){
 commands.clear();native_dispatch_cursor=0;native_tick_released.clear();native_saved_rows.clear();mock_files.clear();mock_orders.clear();mock_history_orders.clear();mock_positions.clear();mock_deals.clear();
 sends=0;check_move_sl=false;reject_check_position=0;lost_response=false;reject_send=false;disk_failure=false;connected=true;history_available=true;
 native_credit=0;native_next_steps=0;native_sl_first=.25;native_sl_second=.40;native_sl_first_offset=-.25;native_sl_second_offset=0;native_fixed_source=0;native_fixed_target=0;native_fixed_steps=0;native_fixed_side=0;native_fixed_price=0;next_order=1000;native_healthy=true;native_paused=false;native_reason="";native_serial=0;native_revision=0;native_basket=1;native_applied=0;native_steps=2;native_split_two=false;native_profiles[2]="";native_bar=0;native_carry=0;native_control="";native_close_all="";native_cancel_control="";native_tick_changed=true;native_tick_event=false;native_tick_stamp=180000;native_tick_bid=100;native_tick_ask=100.02;native_tick_seen=180000;
 native_sl=1.5;native_tp=.25;native_offset=.5;native_recovery_sl=1.5;native_recovery_offset=.5;
 ExpectedLogin=123;ExpectedServer="Mock";InstallationId="12345678-1234-1234-1234-123456789012";AllowTrading=true;RequireObserver=false;LegacyOwnerStopped=true;MigrationConfirmed=true;
 mock_quote=MqlTick{};mock_bar=MqlRates{};
 for(int n=2;n<=10;n++){native_profiles[n]="";for(int j=0;j<n;j++){if(j)native_profiles[n]+=",";native_profiles[n]+=DoubleToString(ApprovedTP(n,j),2);}}
 assert(SaveNative());
}
void reconcile_all(){for(int i=0;i<ArraySize(commands);i++)ReconcileNative(commands[i]);assert(SaveNative());}
string panel_command(string action,string fields=""){
 static int number=0;string suffix=std::to_string(++number);string id="bbbbbbbb-bbbb-bbbb-bbbb-"+string(12-suffix.size(),'0')+suffix;
 string name="test-control-"+suffix+".cmd";
 mock_files[MailboxFolder+"\\"+name]="protocol=native_panel_wire_v1\nid="+id+"\ndigest="+string(64,'b')+"\ninstallation="+InstallationId+"\nlogin=123\nserver=Mock\nsymbol=XAUUSD.s\nrevision="+(string)native_revision+"\naction="+action+"\n"+fields;
 ProcessNativeControl(name);return name;
}
int main(){
 reset();FollowNativeCandle();assert(sends==1 && commands.size()==1 && commands[0].status=="UNKNOWN");
 SendNative(0);assert(sends==1);reconcile_all();assert(commands[0].status=="PENDING");
 auto id=commands[0].order;fill_order(id,77,1,.02);reconcile_all();assert(commands[0].status=="OPEN" && commands[0].position==77);assert(std::abs(NativeDebt()-.02)<1e-8);
 assert(SaveNative());commands.clear();assert(LoadNative());assert(commands[0].position==77 && native_paused);assert(sends==1);
 exit_position(77,DEAL_REASON_SL,-3);reconcile_all();AdvanceNativeBasket();assert(native_steps==3 && native_basket==2);AdvanceNativeBasket();assert(native_steps==3 && native_basket==2);
 // Lost broker response: recover original order, never resend.
 reset();lost_response=true;FollowNativeCandle();assert(sends==1 && commands[0].order==0);assert(SaveNative());commands.clear();assert(LoadNative());SendNative(0);assert(sends==1);reconcile_all();assert(commands[0].status=="PENDING" && commands[0].order==1000);
 // Journal failure before send cannot reach broker.
 reset();disk_failure=true;FollowNativeCandle();assert(sends==0 && !native_healthy);
 // Unavailable history never changes an open position to a closed one.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();exit_position(77,DEAL_REASON_SL,-3);history_available=false;reconcile_all();assert(!commands[0].closed);AdvanceNativeBasket();assert(native_steps==2);history_available=true;reconcile_all();assert(commands[0].closed);
 // Protection refusal leaves broker SL and support classification unchanged.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();mock_quote.bid=101.2;mock_quote.ask=101.22;
 double oldsl=mock_positions[77].doubles[POSITION_SL];reject_send=true;int k=AddNative("MODIFY",1,.02,0,100.6,102,77);SendNative(k);assert(commands[k].status=="REJECTED");reconcile_all();assert(!commands[0].support && mock_positions[77].doubles[POSITION_SL]==oldsl);
 // Positive acknowledgement is not support until broker readback.
 reject_send=false;k=AddNative("MODIFY",1,.02,0,100.6,102,77);SendNative(k);assert(!commands[0].support && commands[k].status=="UNKNOWN");reconcile_all();assert(commands[0].support && commands[k].status=="DONE");
 assert(SaveNative());commands.clear();assert(LoadNative());assert(commands[0].support);exit_position(77,DEAL_REASON_SL,-.5);reconcile_all();AdvanceNativeBasket();assert(native_steps==2 && native_split_two && native_basket==2); // two .01, never grow total volume
 assert(SaveNative());native_split_two=false;commands.clear();assert(LoadNative());assert(native_split_two && native_profiles[2]=="0.30,0.70");
 native_profiles[2]="";
 native_paused=false;native_reason="";FollowNativeCandle();assert(native_reason=="MISSING_OR_INVALID_PROFILE_2");assert(sends==3);
 // Approved editable profile: exactly two .01 entries.
 native_profiles[2]="0.30,0.70";assert(SaveNative());commands.clear();assert(LoadNative());assert(native_profiles[2]=="0.30,0.70");
 native_paused=false;native_reason="";FollowNativeCandle();assert(commands[commands.size()-1].volume==.01 && commands[commands.size()-2].volume==.01);assert(commands[commands.size()-1].stage==1);
 // Split recovery retargets the last OPEN stage even after stage 1 TP.
 reset();native_split_two=true;native_carry=3;FollowNativeCandle();assert(sends==2);for(int j=0;j<2;j++)fill_order(1000+j,77+j,1,.01);reconcile_all();
 exit_position(78,DEAL_REASON_TP,.7);reconcile_all();int prior=sends;AdjustNativeRecoveryTP();assert(sends==prior+1 && commands.back().position==77 && commands.back().kind=="MODIFY");
 reset();native_split_two=true;FollowNativeCandle();for(int j=0;j<2;j++){fill_order(1000+j,77+j,1,.01);exit_position(77+j,DEAL_REASON_SL,-1.5);}reconcile_all();AdvanceNativeBasket();assert(native_steps==3 && native_basket==2);AdvanceNativeBasket();assert(native_steps==3);
 reset();native_split_two=true;FollowNativeCandle();for(int j=0;j<2;j++){fill_order(1000+j,77+j,1,.01);exit_position(77+j,DEAL_REASON_TP,1);}reconcile_all();AdvanceNativeBasket();assert(NativeInitial() && native_basket==2);
 // A concurrent more favorable manual SL arriving during OrderCheck is
 // read again after journaling; no request overwrites it.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();mock_quote.bid=101.2;mock_quote.ask=101.22;check_move_sl=true;
 int before_send=sends;k=AddNative("MODIFY",1,.02,0,100.6,102,77);SendNative(k);assert(sends==before_send && commands[k].status=="REJECTED" && std::abs(mock_positions[77].doubles[POSITION_SL]-100.8)<1e-8);
 // Mixed TP/original SL is owner-approved KEEP, one transition per basket.
 reset();native_steps=3;FollowNativeCandle();assert(sends==3);for(int j=0;j<3;j++)fill_order(1000+j,77+j,1,.01);reconcile_all();exit_position(77,DEAL_REASON_TP,.3);exit_position(78,DEAL_REASON_SL,-1.5);exit_position(79,DEAL_REASON_SL,-1.5);reconcile_all();AdvanceNativeBasket();assert(native_steps==3 && native_basket==2);AdvanceNativeBasket();assert(native_steps==3 && native_basket==2);assert(std::abs(NativeDebt()-2.79)<1e-7);
 // Full three-ticket SL increments once to four, not six.
 reset();native_steps=3;FollowNativeCandle();for(int j=0;j<3;j++){fill_order(1000+j,77+j,1,.01);exit_position(77+j,DEAL_REASON_SL,-1.5);}reconcile_all();AdvanceNativeBasket();assert(native_steps==4);AdvanceNativeBasket();assert(native_steps==4);
 // Missing 0.08 profile causes no entry and preserves debt.
 reset();native_steps=8;native_profiles[8]="";native_carry=17;FollowNativeCandle();assert(sends==0 && native_paused && NativeDebt()==17);
 // Partial entry remains protected but does not qualify for volume growth.
 reset();FollowNativeCandle();fill_order(1000,77,1,.01);reconcile_all();assert(native_paused && commands[0].filled==.01);exit_position(77,DEAL_REASON_SL,-1);reconcile_all();AdvanceNativeBasket();assert(native_steps==2);
 // Broker profit inversion for both sides, and nonpositive targets rejected.
 reset();double target=0;assert(RecoveryTarget(1,.01,100,.3,target)&&std::abs(target-100.3)<.011);assert(RecoveryTarget(-1,.01,100,.3,target)&&std::abs(target-99.7)<.011);assert(!RecoveryTarget(1,.01,100,0,target));
 // Close-all follows its durable resource intent and does not repeat a
 // rejected close automatically. A later explicit owner request has a new ID.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();native_close_all="owner-request";ContinueNativeCloseAll();assert(sends==2);ContinueNativeCloseAll();assert(sends==2);reconcile_all();ContinueNativeCloseAll();assert(native_close_all=="" && mock_positions.empty());
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();native_close_all="owner-request";reject_send=true;ContinueNativeCloseAll();int sent=sends;ContinueNativeCloseAll();assert(sends==sent && !mock_positions.empty() && native_close_all=="");
 // Observer is the default production source. Missing/stale/unbound proof
 // cannot claim closure or P&L; direct history is only the explicit fallback.
 reset();RequireObserver=true;FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();assert(commands[0].position==77 && commands[0].status=="UNKNOWN");
 string proof="NATIVE_OBS1|123|Mock|XAUUSD.s|77|OBSERVED|180|1\n10000|1000|"+(string)DEAL_ENTRY_IN+"|"+(string)DEAL_TYPE_BUY+"|0|"+(string)Magic+"|0.02|0|-0.02|0|0|99.1\nEND";
 mock_files[MailboxFolder+"\\position-77.proof"]=proof;reconcile_all();assert(commands[0].status=="OPEN" && std::abs(commands[0].net+.02)<1e-8);
 mock_files[MailboxFolder+"\\position-77.proof"]="corrupt";exit_position(77,DEAL_REASON_SL,-3);reconcile_all();assert(!commands[0].closed);
 // Hedging: stable POSITION_IDENTIFIER is not the mutable position ticket.
 // One frozen/rejected position must not modify the lowest-ticket neighbor
 // or prevent independent modifications of the remaining positions.
 reset();native_steps=3;FollowNativeCandle();for(int j=0;j<3;j++){fill_order(1000+j,77+j,1,.01);auto node=mock_positions.extract(77+j);node.key()=177+j;node.mapped().ints[POSITION_TICKET]=177+j;mock_positions.insert(std::move(node));}reconcile_all();
 mock_quote.bid=100.8;mock_quote.ask=100.82;reject_check_position=178;native_control="independent-batch";NativeModifyPositions("sl_at_entry");native_control="";
 assert(sends==5);assert(std::abs(mock_positions[177].doubles[POSITION_SL]-100.6)<1e-8);assert(std::abs(mock_positions[178].doubles[POSITION_SL]-99.1)<1e-8);assert(std::abs(mock_positions[179].doubles[POSITION_SL]-100.6)<1e-8);reconcile_all();
 // Late recommendation with an old SL basis after a manual improvement.
 mock_positions[179].doubles[POSITION_SL]=100.7;
 string recommendation="{\"protocol\":\"native_sl_proposal_v1\",\"status\":\"RECOMMENDED\",\"login\":\"123\",\"server\":\"Mock\",\"symbol\":\"XAUUSD.s\",\"cycle_id\":\"1\",\"position_id\":\"79\",\"revision\":\"1\",\"expected_sl\":\"99.1\",\"expected_tp\":\"101.6\",\"volume\":\"0.01\",\"entry\":\"100.6\",\"recommended_sl\":\"100.6\",\"quote_time_msc\":\"180000\"}";
 mock_files[MailboxFolder+"\\sl-1-79.json"]=recommendation;int before_stale=sends;ApplyNativeSLRecommendations();assert(sends==before_stale && mock_positions[179].doubles[POSITION_SL]==100.7);
 // Last TP may close first; select the last STILL OPEN original stage,
 // and allow its recovery target to be closer than the other open TP.
 reset();native_steps=3;native_carry=1.4;FollowNativeCandle();for(int j=0;j<3;j++)fill_order(1000+j,77+j,1,.01);reconcile_all();exit_position(79,DEAL_REASON_TP,1);reconcile_all();mock_quote.bid=100.61;mock_quote.ask=100.63;AdjustNativeRecoveryTP();assert(commands.back().kind=="MODIFY" && commands.back().position==78);assert(mock_positions[78].doubles[POSITION_TP]<mock_positions[77].doubles[POSITION_TP]);reconcile_all();int tp_sends=sends;AdjustNativeRecoveryTP();assert(sends==tp_sends);
 // A manual TP is retained, not overwritten on the next automatic tick.
 mock_positions[78].doubles[POSITION_TP]=101.11;reconcile_all();AdjustNativeRecoveryTP();assert(sends==tp_sends && mock_positions[78].doubles[POSITION_TP]==101.11);
 // All positions can close before the target update; carry the residual
 // into the next same-volume basket, without fabricating a closed TP edit.
 reset();native_steps=3;native_carry=10;FollowNativeCandle();for(int j=0;j<3;j++){fill_order(1000+j,77+j,1,.01);exit_position(77+j,DEAL_REASON_TP,.1);}reconcile_all();AdvanceNativeBasket();assert(native_steps==3 && native_basket==2 && std::abs(NativeDebt()-9.79)<1e-8);
 // A completed profitable cycle does not bank its old profits to suppress
 // the next independent initial stop-loss recovery.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);exit_position(77,DEAL_REASON_TP,.5);reconcile_all();AdvanceNativeBasket();assert(std::abs(NativeDebt())<1e-8);FollowNativeCandle();fill_order(1001,78,1,.02);exit_position(78,DEAL_REASON_SL,-3);reconcile_all();AdvanceNativeBasket();assert(std::abs(NativeDebt()-3.03)<1e-8);
 // Web threshold changes invalidate a queued recommendation from the old settings.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);mock_positions[77].doubles[POSITION_TP]=102;reconcile_all();mock_quote.bid=101.2;mock_quote.ask=101.22;
 string configured="{\"protocol\":\"native_sl_proposal_v1\",\"status\":\"RECOMMENDED\",\"login\":\"123\",\"server\":\"Mock\",\"symbol\":\"XAUUSD.s\",\"cycle_id\":\"1\",\"position_id\":\"77\",\"revision\":\"1\",\"first_threshold\":\"0.25\",\"second_threshold\":\"0.40\",\"first_offset\":\"-0.25\",\"second_offset\":\"0\",\"expected_sl\":\"99.1\",\"expected_tp\":\"102\",\"volume\":\"0.02\",\"entry\":\"100.6\",\"recommended_sl\":\"100.6\",\"quote_time_msc\":\"180000\"}";
 mock_files[MailboxFolder+"\\sl-1-77.json"]=configured;native_sl_first=.3;ApplyNativeSLRecommendations();assert(sends==1);native_sl_first=.25;ApplyNativeSLRecommendations();assert(sends==2);reconcile_all();assert(commands[0].support);
 // Debt 2.00: projected .30 + .70 leaves 1.00 for last stage, never 2.00.
 reset();native_steps=3;native_carry=1.94;FollowNativeCandle();for(int j=0;j<3;j++)fill_order(1000+j,77+j,1,.01);reconcile_all();double required=0,debt_before=NativeDebt();assert(std::abs(debt_before-2)<1e-8);assert(NativeRemainingGross(1,2,debt_before,required) && std::abs(required-1)<1e-8);assert(NativeDebt()==debt_before);ExitCostReserve=.05;assert(NativeRemainingGross(1,2,debt_before,required) && std::abs(required-1.15)<1e-8);ExitCostReserve=0;
 // A realized sibling is counted only in debt; no duplicate expected profit.
 exit_position(77,DEAL_REASON_TP,.3);reconcile_all();assert(NativeRemainingGross(1,2,NativeDebt(),required) && std::abs(required-1.01)<1e-8);reconcile_all();assert(NativeRemainingGross(1,2,NativeDebt(),required) && std::abs(required-1.01)<1e-8);
 // Re-price only remaining volume after partial realization, using actual manual TP.
 exit_position(78,DEAL_REASON_TP,.35,.005);reconcile_all();assert(NativeRemainingGross(1,2,NativeDebt(),required) && std::abs(required-1.02)<1e-8);
 mock_positions[78].doubles[POSITION_TP]=101.4;assert(NativeRemainingGross(1,2,NativeDebt(),required) && std::abs(required-.97)<1e-8);
 native_carry=0;int before_invalid=sends;AdjustNativeRecoveryTP();assert(sends==before_invalid && native_reason=="NONPOSITIVE_OR_UNAVAILABLE_RECOVERY_TP_NEEDS_DECISION");
 // CANCEL does not need a recent tick or valid quote, but still needs broker readback.
 reset();FollowNativeCandle();reconcile_all();native_tick_changed=false;mock_quote.bid=0;mock_quote.ask=0;lost_response=true;
 int cancel=AddNative("CANCEL",1,0,0,0,0,0,1000);assert(cancel>=0);SendNative(cancel);assert(sends==2 && commands[cancel].status=="UNKNOWN");SendNative(cancel);CancelNativeEntries();assert(sends==2);reconcile_all();assert(commands[cancel].status=="DONE" && commands[0].status=="CANCELLED");
 // Price actions are durably queued; disconnect cannot be reported as cancellation.
 reset();FollowNativeCandle();reconcile_all();native_tick_changed=false;int queued_stop=AddNative("STOP",1,.01,101,99,102);assert(queued_stop>=0);SendNative(queued_stop);assert(commands[queued_stop].status=="WAIT_TICK" && sends==1);connected=false;assert(AddNative("CANCEL",1,0,0,0,0,0,1000)<0 && sends==1 && commands[0].status=="PENDING");
 // Long quiet period: a price command is accepted, never rejected for age.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);mock_positions[77].doubles[POSITION_TP]=102;reconcile_all();native_tick_bid=mock_quote.bid=101.2;native_tick_ask=mock_quote.ask=101.22;
 RefreshNativeQuote();native_tick_seen=0;string waiting_request=panel_command("sl_at_entry");assert(sends==1 && commands.size()==1);
 ProcessNativeControl(waiting_request);assert(sends==1); // same tick and same intent, no operation
 assert(ReadNativeFile(waiting_request)!="");bool waiting_visible=false;for(auto item:mock_files)if(item.second.find("WAITING_NEXT_VALID_TICK_NOT_SENT")!=string::npos)waiting_visible=true;assert(waiting_visible);
 mock_quote.time_msc+=600000;mock_quote.time+=600;RefreshNativeQuote();assert(NativeTickObserved());ProcessNativeControl(waiting_request);assert(sends==2 && commands.back().status=="UNKNOWN");ProcessNativeControl(waiting_request);assert(sends==2);reconcile_all();ProcessNativeControl(waiting_request);assert(sends==2);
 // Low-level queued CLOSE survives restart; only a new valid tick releases it once.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();RefreshNativeQuote();int delayed=AddNative("CLOSE",1,.02,0,0,0,77);SendNative(delayed);assert(commands[delayed].status=="WAIT_TICK" && sends==1);assert(SaveNative());commands.clear();assert(LoadNative());DispatchNativeTickQueue();assert(sends==1);
 mock_quote.time_msc++;RefreshNativeQuote();lost_response=true;DispatchNativeTickQueue();assert(sends==2 && commands[delayed].status=="UNKNOWN");DispatchNativeTickQueue();assert(sends==2);reconcile_all();assert(commands[delayed].status=="DONE");
 // A queued unsent STOP can be cancelled locally without waiting for a tick.
 reset();RefreshNativeQuote();int unsent=AddNative("STOP",1,.01,101,99,102);SendNative(unsent);assert(commands[unsent].status=="WAIT_TICK" && sends==0);CancelNativeEntries();assert(commands[unsent].status=="CANCELLED");mock_quote.time_msc++;RefreshNativeQuote();DispatchNativeTickQueue();assert(sends==0);
 // Connection loss accepts the high-level request, but no broker action occurs.
 reset();FollowNativeCandle();reconcile_all();connected=false;string offline_cancel=panel_command("cancel_triggers");assert(sends==1 && commands[0].status=="PENDING");bool connection_visible=false;for(auto item:mock_files)if(item.second.find("WAITING_CONNECTION_NOT_SENT")!=string::npos)connection_visible=true;assert(connection_visible);
 connected=true;native_tick_changed=false;mock_quote.bid=0;mock_quote.ask=0;ProcessNativeControl(offline_cancel);assert(sends==2);ProcessNativeControl(offline_cancel);assert(sends==2);reconcile_all();assert(commands[0].status=="CANCELLED");
 // Connection can disappear after preparation; unsent CANCEL resumes without a tick.
 reset();FollowNativeCandle();reconcile_all();int delayed_cancel=AddNative("CANCEL",1,0,0,0,0,0,1000);connected=false;SendNative(delayed_cancel);assert(commands[delayed_cancel].status=="WAIT_TICK" && sends==1);connected=true;native_tick_changed=false;mock_quote.bid=0;mock_quote.ask=0;DispatchNativeTickQueue();assert(sends==2);DispatchNativeTickQueue();assert(sends==2);reconcile_all();assert(commands[delayed_cancel].status=="DONE");
 // Invalid quote stays waiting; a subsequent valid tick cannot worsen a manual SL.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);mock_positions[77].doubles[POSITION_TP]=102;reconcile_all();RefreshNativeQuote();int edit=AddNative("MODIFY",1,.02,0,100.6,102,77);SendNative(edit);assert(commands[edit].status=="WAIT_TICK");mock_quote.bid=0;mock_quote.ask=0;mock_quote.time_msc++;RefreshNativeQuote();DispatchNativeTickQueue();assert(sends==1);mock_positions[77].doubles[POSITION_SL]=100.8;mock_quote.bid=101.2;mock_quote.ask=101.22;mock_quote.time_msc++;RefreshNativeQuote();DispatchNativeTickQueue();assert(sends==1 && commands[edit].status=="REJECTED");
 // A single new tick releases the entire queue in bounded batches, not just four requests.
 reset();RefreshNativeQuote();for(int i=0;i<7;i++){int j=AddNative("STOP",1,.01,101,99,102,0,0,i);SendNative(j);}assert(sends==0);mock_quote.time_msc++;RefreshNativeQuote();DispatchNativeTickQueue();assert(sends==4);RefreshNativeQuote();assert(!NativeTickObserved());DispatchNativeTickQueue();assert(sends==7);DispatchNativeTickQueue();assert(sends==7);
 // Missing CANCEL head entries move to history/position reconciliation and cannot starve the tail.
 reset();native_steps=6;FollowNativeCandle();reconcile_all();assert(sends==6);for(int j=0;j<6;j++)assert(AddNative("CANCEL",1,0,0,0,0,0,1000+j)>=0);
 connected=false;for(int j=6;j<12;j++)SendNative(j);for(int j=6;j<12;j++)assert(commands[j].status=="WAIT_TICK");
 fill_order(1000,77,1,.01);fill_order(1001,78,1,.01);
 for(int j=2;j<4;j++){auto order=mock_orders.at(1000+j);order.ints[ORDER_STATE]=ORDER_STATE_CANCELED;mock_history_orders[1000+j]=order;mock_orders.erase(1000+j);}
 connected=true;history_available=false;native_tick_changed=false;DispatchNativeTickQueue();for(int j=6;j<10;j++)assert(commands[j].status=="RECONCILING");assert(sends==6);
 DispatchNativeTickQueue();assert(sends==8);DispatchNativeTickQueue();reconcile_all();assert(sends==8 && AnyUnknown());
 assert(SaveNative());commands.clear();assert(LoadNative());DispatchNativeTickQueue();assert(sends==8);history_available=true;
 ReconcileNative(commands[6]);assert(commands[6].status=="RECONCILING"); // FILLED history alone is insufficient: position not yet adopted
 reconcile_all();assert(!AnyUnknown());for(int j=6;j<12;j++)assert(commands[j].status=="DONE");assert(commands[6].exit_kind==5 && commands[7].exit_kind==5 && commands[8].exit_kind==6 && commands[9].exit_kind==6);assert(commands[0].position==77 && commands[0].status=="OPEN");assert(mock_positions.size()==2);DispatchNativeTickQueue();reconcile_all();assert(sends==8);
 // A previously OPEN source record is not fresh proof once the position disappeared.
 reset();FollowNativeCandle();reconcile_all();int before_fill_cancel=AddNative("CANCEL",1,0,0,0,0,0,1000);connected=false;SendNative(before_fill_cancel);connected=true;fill_order(1000,77,1,.02);ReconcileNative(commands[0]);assert(commands[0].status=="OPEN");exit_position(77,DEAL_REASON_TP,.5);DispatchNativeTickQueue();ReconcileNative(commands[before_fill_cancel]);assert(commands[before_fill_cancel].status=="RECONCILING" && sends==1);ReconcileNative(commands[0]);ReconcileNative(commands[before_fill_cancel]);assert(commands[0].status=="CLOSED" && commands[before_fill_cancel].status=="DONE" && sends==1);
 // A deferred command survives an unrelated journal revision, but not a changed target.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);mock_positions[77].doubles[POSITION_TP]=102;reconcile_all();native_tick_bid=mock_quote.bid=101.2;native_tick_ask=mock_quote.ask=101.22;RefreshNativeQuote();string bound=panel_command("sl_at_entry");native_credit=.01;assert(SaveNative());assert(LoadNative());mock_quote.time_msc++;RefreshNativeQuote();ProcessNativeControl(bound);assert(sends==2 && commands.back().status=="UNKNOWN");ProcessNativeControl(bound);assert(sends==2);
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);mock_positions[77].doubles[POSITION_TP]=102;reconcile_all();native_tick_bid=mock_quote.bid=101.2;native_tick_ask=mock_quote.ask=101.22;RefreshNativeQuote();string changed_target=panel_command("sl_at_entry");mock_positions[77].doubles[POSITION_SL]=100.8;mock_quote.time_msc++;RefreshNativeQuote();ProcessNativeControl(changed_target);assert(sends==1);bool target_rejected=false;for(auto file:mock_files)if(file.second.find("DEFERRED_TARGET_CHANGED_OR_UNAVAILABLE")!=string::npos)target_rejected=true;assert(target_rejected);
 // A timer-only cold snapshot is a baseline. The first real OnTick is sufficient,
 // even if it precedes baseline initialization or repeats the same quote fields.
 reset();native_tick_stamp=0;native_tick_changed=false;RefreshNativeQuote();assert(!NativeTickObserved());int cold=AddNative("STOP",1,.01,101,99,102);SendNative(cold);assert(sends==0 && commands[cold].status=="WAIT_TICK");OnTick();RefreshNativeQuote();assert(NativeTickObserved());DispatchNativeTickQueue();assert(sends==1);
 reset();native_tick_stamp=0;native_tick_changed=false;OnTick();RefreshNativeQuote();assert(NativeTickObserved());
 // Next settings are editable while a pending basket keeps its saved distances.
 reset();FollowNativeCandle();reconcile_all();double old_price=commands[0].price;
 string config="initial_sl=2\ninitial_tp=.6\noffset=.9\nrecovery_offset=.8\nrecovery_sl=2.5\n";for(int n=2;n<=10;n++)config+="profile_"+(string)n+"="+native_profiles[n]+"\n";
 panel_command("save_settings",config);assert(native_sl==2 && mock_orders[1000].doubles[POSITION_SL]==99.1);
 mock_bar.time+=60;mock_quote.time+=60;mock_quote.time_msc+=60000;native_tick_seen=mock_quote.time_msc;FollowNativeCandle();reconcile_all();FollowNativeCandle();assert(commands.back().kind=="STOP" && commands.back().price==old_price && std::abs(commands.back().tp-commands.back().price-.25)<1e-8);
 // Profile selection only affects a new basket, never the live one.
 panel_command("select_profile","steps=4\n");assert(native_steps==2 && native_next_steps==4);assert(SaveNative());commands.clear();assert(LoadNative());assert(native_next_steps==4 && native_sl==2);
 // One-time initial cancellation leaves automatic following enabled.
 reset();FollowNativeCandle();reconcile_all();panel_command("cancel_initial");reconcile_all();assert(!native_paused && commands[0].status=="CANCELLED");int once=sends;FollowNativeCandle();assert(sends==once);mock_bar.time+=60;mock_quote.time+=60;mock_quote.time_msc+=60000;native_tick_seen=mock_quote.time_msc;FollowNativeCandle();assert(sends==once+1);
 // Writing off debt changes the recovery baseline, never actual P&L/history.
 reset();native_carry=5;native_paused=true;panel_command("writeoff_recovery","expected_debt=5\nconfirmed=true\n");assert(NativeDebt()==0 && NativeNet()==0 && native_credit==5 && native_paused);assert(SaveNative());assert(LoadNative() && native_credit==5);
 reset();FollowNativeCandle();reconcile_all();native_paused=true;native_carry=5;panel_command("writeoff_recovery","expected_debt=5\nconfirmed=true\n");assert(NativeDebt()==5 && native_credit==0);
 reset();panel_command("save_sl_monitor","first=.3\nsecond=.5\nfirst_offset=-.2\nsecond_offset=.1\n");assert(native_sl_first==.3 && native_sl_second==.5 && native_sl_first_offset==-.2 && native_sl_second_offset==.1);assert(SaveNative());assert(LoadNative() && native_sl_second==.5);
 // Fixed SL orders: no candle or manual-SL following, durable no duplicate.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();string arm_reason;
 assert(ArmNativeSLTriggers(arm_reason));assert(native_fixed_target==2 && native_fixed_steps==3 && sends==4);double frozen=commands[1].price;
 reconcile_all();int fixed_sends=sends;mock_bar.time+=60;mock_quote.time+=60;mock_quote.time_msc+=60000;native_tick_seen=mock_quote.time_msc;mock_positions[77].doubles[POSITION_SL]+=.1;FollowNativeCandle();assert(sends==fixed_sends && commands[1].price==frozen);
 assert(!ArmNativeSLTriggers(arm_reason) && sends==fixed_sends);assert(SaveNative());commands.clear();assert(LoadNative());assert(native_fixed_price==frozen && native_fixed_target==2);FollowNativeCandle();assert(sends==fixed_sends);
 // Counter fill may arrive before source close; provisional value becomes actual, never added twice.
 fill_order(1001,78,-1,.01);reconcile_all();double estimated=0;assert(FixedNativeDebt(estimated) && estimated>0);AdjustNativeRecoveryTP();reconcile_all();
 exit_position(77,DEAL_REASON_SL,-2.8);reconcile_all();assert(FixedNativeDebt(estimated) && std::abs(estimated-NativeDebt())<1e-8);AdvanceNativeBasket();assert(native_basket==2 && native_steps==3); // changed source support keeps fixed orders, flags mismatch
 assert(native_reason=="FIXED_VOLUME_OUTCOME_REQUIRES_REVIEW");
 // Lost fixed order response never creates a second plan after reload.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();lost_response=true;assert(ArmNativeSLTriggers(arm_reason));int fixed_lost=sends;commands.clear();assert(LoadNative());assert(!ArmNativeSLTriggers(arm_reason) && sends==fixed_lost);reconcile_all();
 // Unknown source / missing profile / nonpositive provisional TP sends no counter order.
 reset();FollowNativeCandle();assert(!ArmNativeSLTriggers(arm_reason) && sends==1);
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();native_profiles[3]="";assert(!ArmNativeSLTriggers(arm_reason) && sends==1);
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);reconcile_all();mock_positions[77].doubles[POSITION_SL]=101;mock_quote.bid=102;mock_quote.ask=102.02;assert(!ArmNativeSLTriggers(arm_reason) && sends==1);
 // Explicit manual continuation after full 0.10, with exact profile confirmation.
 reset();native_steps=10;FollowNativeCandle();for(int j=0;j<10;j++){fill_order(1000+j,77+j,1,.01);exit_position(77+j,DEAL_REASON_SL,-1.5);}reconcile_all();AdvanceNativeBasket();assert(native_reason=="FULL_SL_010_MANUAL" && native_paused);int ten_sends=sends;FollowNativeCandle();assert(sends==ten_sends);
 string request_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",digest=string(64,'a');
 mock_files[MailboxFolder+"\\resume.cmd"]="protocol=native_panel_wire_v1\nid="+request_id+"\ndigest="+digest+"\ninstallation="+InstallationId+"\nlogin=123\nserver=Mock\nsymbol=XAUUSD.s\nrevision="+(string)native_revision+"\naction=resume_manual\nsteps=3\nprofile="+native_profiles[3]+"\nconfirmed=true\n";
 ProcessNativeControl("resume.cmd");assert(!native_paused && native_reason=="" && native_steps==3 && native_basket==2 && sends==ten_sends);FollowNativeCandle();assert(sends==ten_sends+3);ProcessNativeControl("resume.cmd");assert(sends==ten_sends+3);
 // Fresh bootstrap remains paused and binds the installation permanently.
 // A missing journal cannot silently reset the same installation to empty.
 reset();mock_files.clear();commands.clear();native_saved_rows.clear();native_revision=0;CandleOffset=.5;RecoveryCandleOffset=.7;InitialSLDistance=1.73;InitialTPDistance=.29;RecoverySLDistance=2.17;StartingRecovery=0;StartingSteps=2;
 assert(OnInit()==INIT_SUCCEEDED && native_paused && native_tp==.29 && native_sl==1.73);OnDeinit(0);mock_files.erase(journal_name);assert(OnInit()==INIT_FAILED && sends==0);
 // New accounting epoch ignores historical closed deals but never deletes them.
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);exit_position(77,DEAL_REASON_SL,-99);int history_count=mock_deals.size();
 commands.clear();native_saved_rows.clear();mock_files.clear();native_revision=0;native_serial=0;native_carry=0;NewAccountingStart=true;
 assert(OnInit()==INIT_SUCCEEDED && NativeDebt()==0 && commands.empty() && mock_deals.size()==history_count && native_accounting_start==TimeCurrent());OnDeinit(0);
 native_carry=7;assert(SaveNative());assert(OnInit()==INIT_SUCCEEDED && native_carry==7);OnDeinit(0); // input never resets an existing journal
 // Existing foreign/native live positions or pending orders block a fresh journal.
 reset();FollowNativeCandle();mock_files.clear();commands.clear();native_saved_rows.clear();native_revision=0;assert(OnInit()==INIT_PARAMETERS_INCORRECT && mock_orders.size()==1);assert(mock_files[MailboxFolder+"\\native-view.json"].find("UNMANAGED")!=string::npos);
 reset();FollowNativeCandle();fill_order(1000,77,1,.02);mock_files.clear();commands.clear();native_saved_rows.clear();native_revision=0;assert(OnInit()==INIT_PARAMETERS_INCORRECT && mock_positions.size()==1);assert(mock_files[MailboxFolder+"\\native-view.json"].find("\"owned\":false")!=string::npos);
 NewAccountingStart=false;
 // Saved journal corruption and foreign account cannot be adopted.
 reset();mock_files[journal_name]+="CORRUPT";assert(!LoadNative());reset();ExpectedLogin=456;assert(!LoadNative());
 return 0;
}
