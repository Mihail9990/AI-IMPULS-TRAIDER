'use strict';
// Projection/transport only. All trading policy, validation and execution remain in EA.
const NativePanelAdapter=(()=>{
 const mapping={INITIAL_SL_DISTANCE:'initial_sl',INITIAL_TP_DISTANCE:'initial_tp',INITIAL_ENTRY_DISTANCE:'offset',RECOVERY_SL_DISTANCE:'recovery_sl'};
 function settings(v){const s=v.native.settings;return {type:'save_settings',initial_sl:s.initial_sl,initial_tp:s.initial_tp,offset:s.offset,recovery_offset:s.recovery_offset,recovery_sl:s.recovery_sl,...Object.fromEntries(Array.from({length:9},(_,i)=>['profile_'+(i+2),s.profiles?.[i+2]||'']))};}
 function action(a,v){
  const simple=new Set(['start','stop_new_entries','cancel_triggers','close_all','sl_at_entry','sl_distance','modify_level','reverse_ladder','triggers_at_sl','resume_manual','save_sl_monitor','reconcile','reset_pause','cancel_initial','writeoff_recovery']);
  if(simple.has(a.type)){const keep={reverse_ladder:['enabled'],cancel_initial:[],writeoff_recovery:['expected_debt','confirmed']};return keep[a.type]?{type:a.type,...Object.fromEntries(keep[a.type].map(k=>[k,a[k]]))}:a;}
  if(a.type==='command'){const names={'/start':'start','/stop':'stop_new_entries','/resume_time':'reconcile','/reset_manual':'reconcile','/reset_pause':'reset_pause','/sync_mt5_time':'reconcile','/use_mt5_raw_time':'reconcile'};if(names[a.text])return{type:names[a.text]};}
  if(a.type==='own_entry_sl')return{type:'sl_at_entry'};
  if(a.type==='v3_adjust_sl')return{type:'sl_distance',distance:a.distance};
  if(a.type==='v3_link')return{type:'triggers_at_sl'};
  if(['v3_cancel_link','v3_cancel_entry','v3_cancel_request'].includes(a.type))return{type:'cancel_triggers'};
  if(a.type==='protection'){const level=a.old.sl!==a.new.sl?'SL':'TP';return{type:'modify_level',position_id:a.position_id,level,price:a.new[level.toLowerCase()],expected_sl:a.old.sl,expected_tp:a.old.tp};}
  if(a.type==='basket_settings'){const out=settings(v);for(const [key,value] of Object.entries(a.settings))if(mapping[key])out[mapping[key]]=value;return out;}
  if(a.type==='v3_settings')return{...settings(v),recovery_offset:a.entry_distance};
  if(a.type==='recovery_profile_select')return{type:'select_profile',steps:a.profile_id};
  if(a.type==='recovery_profiles'){
   const out=settings(v),seen=new Set();for(let n=2;n<=10;n++)out['profile_'+n]='';
   for(const p of a.profiles){const n=p.stages.length;if(n<2||n>10||seen.has(n)||p.stages.some(s=>Number(s.volume)!==.01))throw Error('Один профиль на каждый объём 0.02–0.10; каждая ступень строго 0.01.');seen.add(n);out['profile_'+n]=p.stages.map(s=>s.tp_distance).join(',');}
   out.recovery_sl=a.recovery_sl_distance;out.next_steps=a.profiles.find(p=>p.id===a.selected_profile_id)?.stages.length||0;return out;
  }
  throw Error('Команда не поддерживается нативным исполнителем: '+a.type);
 }
 function view(n){
  if(n.protocol!=='native_state_v1'&&n.phase!=='DISCONNECTED')throw Error('Несовместимый снимок EA');
  n={settings:{profiles:{}},positions:[],orders:[],history:[],candles:[],...n};
  const profiles=Object.entries(n.settings.profiles||{}).filter(([,p])=>p).map(([id,p])=>({id,name:'RECOVERY '+(Number(id)/100).toFixed(2),total_volume:(Number(id)/100).toFixed(2),stages:p.split(',').map(tp_distance=>({volume:'0.01',tp_distance})),sl_distance:n.settings.recovery_sl}));
  const selected=String(n.settings.next_steps||n.steps||2),connected=n.phase!=='DISCONNECTED',positions=n.positions.map(p=>({...p,size:p.volume,ticket:p.id})),orders=n.orders.map(p=>({...p,size:p.volume}));
  const results=Object.fromEntries(Object.entries(n.action_results||{}).map(([id,r])=>[id,{...r,reason:r.receipt?.reason||r.reason||''}]));
  return {...n,native:n,contract:'ticket_basket_v1',mode:'LIVE',recovery_mode:'V3',stopped:n.phase==='PAUSED',debt:n.recovery||'0',realized:n.realized||'0',positions,orders,action_results:results,
   feed:{status:connected?'OBSERVED':'DISCONNECTED',connected,timestamp_semantics:'BROKER_RAW',time_domain:'BROKER_RAW',broker_time:Number(n.broker_time),quote:{bid:n.bid,ask:n.ask},candles:n.candles.map(c=>({...c,broker_time:Number(c.time)}))},
   levels:Object.fromEntries(orders.filter(o=>o.price).map(o=>[o.side,o.price])),tickets:positions.map(p=>({id:p.id,index:p.stage})),journal:n.reason?[n.reason]:[],manual_reason:null,
   settings:Object.fromEntries(Object.entries(mapping).filter(([,key])=>n.settings[key]!==undefined).map(([key,field])=>[key,n.settings[field]])),active_settings:{},
   recovery_profiles:{profiles,selected_profile_id:selected,recovery_sl_distance:n.settings.recovery_sl},selected_recovery_profile:profiles.find(p=>p.id===selected)||profiles[0],current_recovery_profiles:[],v3_settings:{entry_distance:n.settings.recovery_offset},v3_sl_distance:n.settings.recovery_sl,
   v3_controls:{can_adjust_sl:positions.length>0,can_own_entry_sl:positions.length>0,can_link:positions.length>0,can_cancel_entry:orders.length>0,can_cancel_link:orders.length>0,can_writeoff_recovery:n.phase==='PAUSED'&&!positions.length&&!orders.length},
   reverse_controls:{can_enable:positions.length>0,can_disable:positions.length>0,reason:'Условия TP1 и защиты проверит EA'},
   time_status:{market_time_mode:'mt5_raw_v1'},metrics:{currency:n.currency,recovery_total:n.recovery,all_closed_net:n.realized,history_complete:true,history_note:'Только журнал этой установки; прежняя история MT5 не импортируется.'},
   position_history:n.history.map(h=>({...h,id:h.position_id,status:'closed',complete:true,currency:n.currency,close_reason:h.reason}))};
 }
 return{action,view};
})();
if(typeof module!=='undefined')module.exports=NativePanelAdapter;
