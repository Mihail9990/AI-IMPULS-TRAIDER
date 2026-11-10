'use strict';
(() => {
 const $=id=>document.getElementById(id),svg=$('chart'),ns='http://www.w3.org/2000/svg';
 const mode='live';
 let plotBottom=480,plotHeight=450,frameHeight=520,plotRight=855;
 const storageKey=k=>'basket-'+k;
 const apiPath=kind=>'/api/'+kind;
 let view=null,pending=localStorage.getItem(storageKey('request')),pendingScope=localStorage.getItem(storageKey('scope')),busy=false,polling=false,loadedScope=null,preview=null,lines=[],zoom=1,pan=0,gesture=null,hold=null,timeOffset=0,visibleCount=60,previewFocus=null,previewWasOpen=false;
 let lastActionReceipt=null;
 const names={INITIAL_SIZE:'Объём начальной позиции',INITIAL_ENTRY_DISTANCE:'STOP от Close M1',INITIAL_SL_DISTANCE:'Начальный SL',INITIAL_TP_DISTANCE:'Начальный TP',BASKET_COUNT:'Число билетов корзины',TICKET_SIZE:'Объём одного билета',RECOVERY_SL_DISTANCE:'Фиксированный SL корзины',COST_RESERVE:'Резерв будущих расходов, валюта счёта',TP_STEP_MULTIPLIER:'Множитель шага TP (не меньше 1)',CYCLE_COOLDOWN_SECONDS:'Пауза после цикла, секунды'};
 const headers=()=>({'X-Local-Control':'1','X-Controller-Scope':view?.scope??''});
 function notice(id,text,tone='info'){const el=$(id);el.textContent=text;el.dataset.tone=tone;}
 function friendlyReason(reason){
  if(!reason)return '';
  const text=String(reason);
  if(text.includes('V3_PARTIAL_OR_REJECTED_BATCH'))return 'Один или несколько ордеров отклонены. Проверьте выставленные ордера и позиции; новые действия приостановлены.';
  if(text.includes('V3_LINK_EDIT_REJECTED'))return 'Изменение связанных триггеров отклонено. Проверьте ордера и SL; новые действия приостановлены.';
  if(text.includes('TP1_OWN_ENTRY_SL_UNAVAILABLE'))return 'Защитная пауза: после первого TP не удалось перенести SL на цену входа. Проверьте оставшиеся позиции и их SL в настройках.';
  if(/^[A-Z][A-Z0-9_ :.-]+$/.test(text))return 'Требуется проверка состояния. Откройте журнал в настройках: там сохранены подробности.';
  return text;
 }
 function error(text,tone='warning'){notice('action-error',friendlyReason(text),tone);if(preview)$('preview-error').textContent=friendlyReason(text);}

 function clearPending(){pending=null;pendingScope=null;localStorage.removeItem(storageKey('request'));localStorage.removeItem(storageKey('scope'));}
 async function send(action,revision=view?.revision,scope=view?.scope){
  if(busy||pending)throw Error('Предыдущий исход не сверён. Повтор запрещён.');
  if(!view||scope!==view.scope)throw Error('Контекст изменился; обновите данные.');
  const request={id:crypto.randomUUID(),revision,action};pending=request.id;pendingScope=scope;localStorage.setItem(storageKey('request'),pending);localStorage.setItem(storageKey('scope'),scope);busy=true;lastActionReceipt=null;notice('action-error','');updateBotActivity();
  try{const r=await fetch(apiPath('action'),{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify(request),signal:AbortSignal.timeout(4000)});
   if(!r.ok){if([400,401,403,409,413,429].includes(r.status))clearPending();throw Error(r.status===409?'Снимок устарел; обновите данные.':'Команда отклонена сервером.');}
   const receipt=await r.json();lastActionReceipt={status:receipt.status,reason:receipt.reason};if(['APPLIED','REJECTED','CONFLICT'].includes(receipt.status))clearPending();if(preview&&receipt.status!=='APPLIED')$('preview-error').textContent=actionReceiptText(receipt.status,receipt.reason);
  }finally{busy=false;await poll();updateBotActivity();}
 }
 async function command(text,message){if(!confirm(message))return;try{await send({type:'command',text});}catch(e){error(e.message);}}
 const keyboard=$('cycle-keyboard');
 try{keyboard.open=localStorage.getItem('cycle-keyboard-open')!=='false';}catch{}
 keyboard.addEventListener('toggle',()=>{try{localStorage.setItem('cycle-keyboard-open',String(keyboard.open));}catch{}});
 $('cancel-initial').onclick=async()=>{
  if(!view)return;const revision=view.revision,scope=view.scope;
  const action={type:'cancel_initial',cycle_id:view.cycle_id,expected_orders:view.orders.map(o=>o.id)};
  if(!confirm('Отменить только начальный триггер? После следующей закрытой направленной M1 он снова выставится по свечному сигналу. Если ордер успел исполниться, позиция сохраняется.'))return;
  try{await send(action,revision,scope);}catch(e){error(e.message);}
 };
 async function reverseLadder(enabled){if(!view)return;const scope=view.scope,revision=view.revision,action={type:'reverse_ladder',cycle_id:view.cycle_id,enabled,expected_positions:view.positions.map(p=>p.id).sort()};if(!confirm(enabled?'После TP1: назначить SL по долям i/N от СОБСТВЕННОГО фактического входа каждой оставшейся части к текущей серверной цене? Билеты — по номерам TP. Уровни не округляются и не следуют за ценой.':'Перенести SL каждой оставшейся части на её СОБСТВЕННЫЙ фактический вход? TP сохраняются.'))return;try{await send(action,revision,scope);}catch(e){error(e.message);}}
 $('own-entry-sl').onclick=async()=>{if(!view)return;const scope=view.scope,revision=view.revision,action={type:'own_entry_sl',cycle_id:view.cycle_id,expected_positions:view.positions.map(p=>p.id).sort()};try{await send(action,revision,scope);}catch(e){error(e.message);}};
 $('reverse-enable').onclick=()=>reverseLadder(true);$('reverse-disable').onclick=()=>reverseLadder(false);
 $('start').onclick=()=>command('/start','Запустить следование закрытым M1 и восстановление корзинами?');
 $('stop').onclick=()=>command('/stop','Остановить новые входы и отменить ожидающие ордера? Открытые билеты сохраняют SL/TP. Долг не списывается.');
 $('resume').onclick=()=>command('/resume_time','Запросить свежую проверку времени? Другие блокировки не снимаются.');
 $('reset').onclick=()=>command('/reset_manual','Подтвердить ручную сверку всех ордеров и билетов? Требуются доказанное закрытие и отсутствие неопределённости. Долг сохраняется.');
 $('login-button').onclick=async()=>{const password=$('password').value;$('password').value='';try{const r=await fetch('/api/login',{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify({password}),signal:AbortSignal.timeout(3000)});if(!r.ok)throw Error('Вход отклонён');$('login-error').textContent='';await poll();}catch(e){$('login-error').textContent=e.message;}};
 $('logout').onclick=async()=>{await fetch('/api/logout',{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:'{}'});location.reload();};
 // Activity uses controller snapshots and receipts, never inferred fills.
 function actionReceiptText(status,reason){
  if(status==='APPLIED')return 'Команда обработана контроллером; исполнение и позиции проверяются отдельно по снимку.';
  if(status==='REJECTED')return 'Команда отклонена до исполнения'+(reason?': '+friendlyReason(reason):'.');
  if(status==='CONFLICT')return 'Команда отклонена: снимок устарел. Обновите данные и повторите команду.';
  if(status==='UNKNOWN')return 'Исход команды неизвестен. Повтор запрещён до сверки.';
  return 'Команда ожидает результата контроллера; исполнение не подтверждено.';
 }
 function updateBotActivity(){
  const write=(id,text)=>{if($(id).textContent!==text)$(id).textContent=text;};
  if(!view){write('bot-activity-state','Ожидание снимка контроллера.');return;}
  const phases={WAIT_START:'Ожидание запуска',WAIT_CANDLE:'Ожидаю закрытие свечи',INITIAL_WAIT:'Ожидаю закрытие свечи',INITIAL_PENDING:'Ожидаю сверку состояния ордеров',INITIAL_POSITION:'Ожидаю сверку состояния позиции',RECOVERY_POSITION:'Ожидаю сверку состояния позиций',RECOVERY_PENDING:'Ожидание ордеров RECOVERY / сверки',RECOVERY_CLEANUP:'Очистка ордеров RECOVERY / сверка',V3_WAIT_ENTRY:'Ожидаю ручной ордер',V3_WAIT_CANDLE:'Ожидаю закрытие свечи',COOLDOWN:'Пауза перед новым циклом',PAUSED:'Новые входы остановлены',PAUSED_CYCLE:'Цикл приостановлен',MANUAL:'Требуется ручная сверка',SAFE_PAUSE:'Защитная пауза',MIGRATION_BLOCK:'Блокировка переноса состояния'};
  const warnings=[];if(!v3Fresh)warnings.push('Нет свежего снимка: команды заблокированы.');
  if(view.read_only)warnings.push('Только просмотр: торговые команды отключены');
  if(view.migration_block)warnings.push(friendlyReason(view.migration_block));if(view.manual_reason)warnings.push(friendlyReason(view.manual_reason));
  if(view.time_block)warnings.push(timeSafetyText());
  if(view.feed?.status!=='FRESH'||!feedUtcVerified())warnings.push('Котировки или время не подтверждены как свежие.');
  write('bot-activity-warning',warnings.join(' · '));$('bot-activity-warning').dataset.tone='warning';$('bot-activity-state').dataset.tone='info';
  let detail=phases[view.phase]??('Состояние контроллера: '+(view.phase??'не указано'));
  const safetyBlocked=!v3Fresh||view.manual_reason||view.migration_block||view.time_block||['MANUAL','SAFE_PAUSE','MIGRATION_BLOCK'].includes(view.phase);
  if(!safetyBlocked){if(view.positions?.length)detail='Ожидаю исполнения TP или SL';else if(view.orders?.length)detail='Ожидаю исполнения триггер ордера';}
  else if(!v3Fresh)detail='';else detail='Действия приостановлены: требуется проверка';
  if(busy||pending)detail='Ожидаю подтверждения команды; повтор недоступен';
  if(view.v3_request)detail+=' · Запланирован '+view.v3_request.order_type+' по закрытию выбранной свечи';
  if(view.v3_link?.cancel)detail+=' · Отмена связанных триггеров ожидает сверки';
  else if(view.v3_link?.replace)detail+=' · Перенос связанных триггеров ожидает сверки';
  if(view.edit)detail+=' · Правка уровней: '+view.edit.stage;
  if(view.stopped&&!['PAUSED','PAUSED_CYCLE'].includes(view.phase))detail+=' · Новые входы остановлены';
  write('bot-activity-state',detail);
  let action='';
  if(busy)action='Команда отправляется; исполнение не подтверждено.';
  else if(pending){action=pendingScope!==view.scope?'Ожидается сверка команды другого контекста; новые команды заблокированы.':actionReceiptText(view.action_results?.[pending]?.status,view.action_results?.[pending]?.reason);}
  else if(lastActionReceipt)action='Последняя команда: '+actionReceiptText(lastActionReceipt.status,lastActionReceipt.reason);
  write('bot-activity-action',action);$('bot-activity-action').dataset.tone=pending?'unknown':lastActionReceipt?.status==='APPLIED'?'success':lastActionReceipt?.status==='UNKNOWN'?'unknown':lastActionReceipt?.status==='REJECTED'||lastActionReceipt?.status==='CONFLICT'?'warning':'info';
  // Raw controller journal remains in Settings; current safety reasons and receipts stay above.
 }
 // V3 reuses scoped receipts and snapshot revisions; no live execution is enabled here.
 // Display-only helpers: timestamp labels never establish a trading time contract.
 const feedUtcVerified=()=>view?.feed?.timestamp_semantics==='VERIFIED';
 function utcLabel(timestamp,short=false){
  if(!feedUtcVerified()||timestamp===null||timestamp===undefined||timestamp==='')return '';
  const date=new Date(Number(timestamp)*1000);
  return Number.isFinite(date.getTime())?(short?date.toISOString().slice(11,16):date.toISOString()):'';
 }
 function timeSafetyText(){
  if(!view?.time_block)return '';
  const reason=view.time_status?.current_reason??view.time_status?.latched_reason??view.time_block.reason;
  const message={MT5_UTC_WAIT_FRESH_DATA:'ожидание свежего тика и M1.',MT5_UTC_WAIT_REFERENCE:'ожидание независимой проверки UTC.',MT5_UTC_CONTRACT_MISMATCH:'метки MT5 не соответствуют UTC; запустите read-only диагностику.',MT5_UTC_READ_TIMEOUT:'чтение MT5 заняло слишком долго.'}[reason]??'нужна повторная проверка времени.';
  return 'Время: '+message+(reason?' ['+reason+']':'')+' Блокировка сохранена; нажмите «Проверить время» для повторной проверки.';
 }
 let v3Fresh=false;
 const v3Enabled=()=>view?.recovery_mode==='V3';
 const v3Blocked=()=>!view||!v3Fresh||busy||!!pending||view.read_only||!!view.manual_reason||!!view.migration_block||!!view.time_block;
 function connectionLabel(){
  const status=view?.feed?.status,verified=feedUtcVerified(),fresh=v3Fresh&&status==='FRESH'&&verified;
  const role=view?.read_only?'LIVE · READ ONLY':'LIVE';
  const warning=!v3Fresh?'нет свежего снимка':!verified?'время не проверено':status!=='FRESH'?'котировки не свежие':'';
  $('drawer-toggle').textContent='Команды';
  $('drawer-toggle').dataset.warning='false';$('drawer-toggle').title=role+' · '+(warning||'Свежие данные')+' · возраст котировки: '+(view?.feed?.age_seconds??'—')+' сек.';
  svg.dataset.feedFresh=String(fresh);
 }
 // Profile editor keeps draft strings intact: broker validation never rounds volumes or TP.
 let profileRows=[],profileDraft=null,profileSourceKey='',profileBaseline='',profileOptionsKey='',profileRemovalSource='',profileRemoved=new Set();
 const profileConfig=()=>view?.recovery_profiles;
 const profileTotal=stages=>{const values=stages.map(s=>String(s.volume));if(values.some(v=>!/^\d+(\.\d+)?$/.test(v)))return '—';const scale=Math.max(0,...values.map(v=>(v.split('.')[1]||'').length));const sum=values.reduce((n,v)=>n+BigInt(v.replace('.',''))*10n**BigInt(scale-(v.split('.')[1]||'').length),0n).toString().padStart(scale+1,'0');return scale?(sum.slice(0,-scale)+'.'+sum.slice(-scale)).replace(/0+$/,'').replace(/\.$/,''):sum;};
 const profileSummary=p=>p?`${p.name} · ${p.stages?.length??p.count??'—'} ступ. · ${p.total_volume??profileTotal(p.stages??[])} лота`:'Профиль недоступен';
 const profileDetails=p=>profileSummary(p)+'\n'+(p?.stages??[]).map((s,i)=>`${i+1}: ${s.volume} лота · TP ${s.tp_distance}`).join('\n');
 function renderProfileRows(){
  $('recovery-stage-rows').replaceChildren();profileRows.forEach((stage,i)=>{const row=document.createElement('div');row.className='recovery-stage-row';const title=document.createElement('strong');title.textContent=String(i+1);row.append(title);for(const [key,labelText] of [['volume','Объём'],['tp_distance','TP от входа']]){const label=document.createElement('label');label.textContent=labelText;const input=document.createElement('input');input.inputMode='decimal';input.value=stage[key];input.setAttribute('aria-label',labelText+' · ступень '+(i+1));input.oninput=()=>{stage[key]=input.value;updateProfileTotal();};label.append(input);row.append(label);}const remove=document.createElement('button');remove.type='button';remove.textContent='Удалить';remove.disabled=profileRows.length===1;remove.onclick=()=>{profileRows.splice(i,1);renderProfileRows();};row.append(remove);$('recovery-stage-rows').append(row);});updateProfileTotal();
 }
 function updateProfileTotal(){$('recovery-draft-total').textContent=profileRows.length+' / 20 ступеней · всего '+profileTotal(profileRows)+' лота';$('recovery-add-stage').disabled=profileRows.length>=20;}
 function profileDraftValue(){return JSON.stringify({name:$('recovery-profile-name').value,stages:profileRows});}
 function profileDraftDirty(){return !!profileBaseline&&profileDraftValue()!==profileBaseline;}
 function loadProfileDraft(profile){profileDraft=profile?.id??null;profileSourceKey=JSON.stringify(profileConfig());$('recovery-profile-name').value=profile?.name??'Новый профиль';profileRows=(profile?.stages??[{volume:'0.01',tp_distance:'0.25'}]).map(s=>({volume:String(s.volume),tp_distance:String(s.tp_distance)}));renderProfileRows();profileBaseline=profileDraftValue();}
 function revealProfileEditor(){
  $('recovery-profile-settings').open=true;$('recovery-profile-editor').open=true;
  $('recovery-profile-editor').scrollIntoView({block:'start'});
  $('recovery-profile-name').focus({preventScroll:true});
 }
 function openProfileDraft(profile){
  if(profileDraftDirty()&&!confirm('В редакторе есть несохранённые изменения. Заменить этот черновик?')){revealProfileEditor();return;}
  loadProfileDraft(profile);revealProfileEditor();
 }
 function renderProfiles(){
  const config=profileConfig(),selected=view?.selected_recovery_profile,available=!!config?.profiles?.length,blocked=v3Blocked()||!v3Enabled()||!available;
  const key=JSON.stringify(config);if(key!==profileOptionsKey){profileOptionsKey=key;$('recovery-profile-select').replaceChildren();for(const p of config?.profiles??[]){const option=document.createElement('option');option.value=p.id;option.textContent=profileSummary(p);$('recovery-profile-select').append(option);}$('recovery-profile-select').value=config?.selected_profile_id??'';}
  $('recovery-selected-summary').textContent='Следующая корзина: '+profileSummary(selected);
  $('recovery-profile-summary').textContent='Профили RECOVERY · '+profileSummary(selected);
  $('recovery-current-summary').textContent=(view?.current_recovery_profiles??[]).map(p=>'Зафиксировано · '+(p.group_id??'заявка')+': '+profileSummary(p)).join('\n')||'Нет активной или ожидающей корзины RECOVERY.';
  $('initial-settings-summary').textContent='Начальный вход · '+(view?.settings?.INITIAL_SIZE??'—')+' лота · TP '+(view?.settings?.INITIAL_TP_DISTANCE??'—')+' · SL '+(view?.settings?.INITIAL_SL_DISTANCE??'—');
  $('recovery-debt-summary').textContent='Остаток RECOVERY · '+(view?.debt??'—')+' UST';
  $('recovery-sl-summary').textContent='SL новых корзин · '+(config?.recovery_sl_distance??'—');
  $('recovery-offset-summary').textContent='Отступ STOP от M1 · '+(view?.v3_settings?.entry_distance??'—');
  for(const id of ['recovery-profile-apply','recovery-profile-save','recovery-save-sl','recovery-profile-save-list'])$(id).disabled=blocked;
  if(!available){$('v3-submit-entry').disabled=true;if(!view?.v3_link)$('v3-link-toggle').disabled=true;}
 }
 const positiveDecimal=(value,label)=>{const raw=String(value).trim();if(!/^\d+(\.\d+)?$/.test(raw)||!Number.isFinite(Number(raw))||Number(raw)<=0)throw Error(label+': требуется положительное десятичное число без округления.');return raw;};
 async function saveProfileConfig(config,message){if(v3Blocked()||!v3Enabled()||!profileConfig())return;const action={type:'recovery_profiles',cycle_id:view.cycle_id,...config},revision=view.revision,scope=view.scope;if(!confirm(message+'\nИзменится только следующая корзина. Открытые, ожидающие и запланированные заявки сохраняют свой профиль и SL.'))return;await send(action,revision,scope);}
 function checkProfileRemovals(){if(profileRemoved.size&&profileRemovalSource!==JSON.stringify(profileConfig()))throw Error('Список изменился. Загрузите актуальные настройки и повторите исключения.');}
 $('recovery-profile-remove').onclick=()=>{const id=$('recovery-profile-select').value;if(!profileConfig()?.profiles.some(p=>p.id===id))return;if(!profileRemoved.size)profileRemovalSource=JSON.stringify(profileConfig());if(profileRemoved.has(id))profileRemoved.delete(id);else profileRemoved.add(id);$('recovery-profile-removals').textContent='Исключены в черновике: '+(profileConfig().profiles.filter(p=>profileRemoved.has(p.id)).map(p=>p.name).join(', ')||'нет')+'. Повторное нажатие возвращает профиль. Сохраните исключения или профиль для применения.';};
 $('recovery-profile-save-list').onclick=async()=>{try{checkProfileRemovals();const config=profileConfig(),profiles=config.profiles.filter(p=>!profileRemoved.has(p.id));if(!profiles.length)throw Error('Оставьте хотя бы один профиль или сохраните новый.');const selected_profile_id=profiles.some(p=>p.id===config.selected_profile_id)?config.selected_profile_id:profiles[0].id;await saveProfileConfig({...config,profiles,selected_profile_id},'Сохранить список профилей?\nОстанутся: '+profiles.map(p=>p.name).join(', ')+'\nВыбран: '+profiles.find(p=>p.id===selected_profile_id).name);}catch(e){error(e.message);}};
 $('recovery-profile-edit').onclick=()=>openProfileDraft(profileConfig()?.profiles.find(p=>p.id===$('recovery-profile-select').value));
 $('recovery-profile-new').onclick=()=>openProfileDraft(null);
 $('recovery-add-stage').onclick=()=>{if(profileRows.length<20){profileRows.push({volume:'0.01',tp_distance:''});renderProfileRows();}};
 $('recovery-profile-apply').onclick=async()=>{if(v3Blocked()||!v3Enabled())return;try{const p=profileConfig()?.profiles.find(p=>p.id===$('recovery-profile-select').value);if(!p)throw Error('Выберите сохранённый профиль.');const revision=view.revision,scope=view.scope;if(!confirm('Выбрать для следующих корзин?\n'+profileDetails(p)+'\nОткрытые и ожидающие заявки не изменятся.'))return;await send({type:'recovery_profile_select',cycle_id:view.cycle_id,profile_id:p.id},revision,scope);}catch(e){error(e.message);}};
 $('recovery-profile-save').onclick=async()=>{try{if(profileSourceKey!==JSON.stringify(profileConfig()))throw Error('Профили изменились. Загрузите профиль заново перед сохранением.');const name=$('recovery-profile-name').value.trim();if(!name||name.length>80)throw Error('Название: от 1 до 80 символов.');if(profileRows.length<1||profileRows.length>20)throw Error('Нужно от 1 до 20 ступеней.');const stages=profileRows.map((s,i)=>({volume:positiveDecimal(s.volume,'Объём '+(i+1)),tp_distance:positiveDecimal(s.tp_distance,'TP '+(i+1))}));const p={id:profileDraft??('profile-'+Date.now().toString(36)),name,stages},config=profileConfig();checkProfileRemovals();const profiles=config.profiles.filter(x=>x.id!==p.id&&!profileRemoved.has(x.id)).concat([p]);await saveProfileConfig({...config,profiles,selected_profile_id:p.id},'Сохранить и выбрать профиль?\n'+profileDetails(p));}catch(e){error(e.message);}};
 $('recovery-save-sl').onclick=async()=>{try{await saveProfileConfig({...profileConfig(),recovery_sl_distance:positiveDecimal($('recovery-sl-distance').value,'SL')},'Сохранить SL для новых корзин: '+$('recovery-sl-distance').value+'?');}catch(e){error(e.message);}};
 function v3InputMode(){
  const stop=$('v3-order-type').value==='STOP';
  const now=$('v3-timing').querySelector('option[value="now"]');now.disabled=stop;
  if(stop&&$('v3-timing').value==='now')$('v3-timing').value='current';
  const candle=$('v3-timing').value!=='now';$('v3-side').disabled=candle;
  $('v3-direction-note').textContent=candle?'Направление — цвет выбранной свечи. При doji ордера не выставляются.':'Направление выбираете вы. Фактическая цена зависит от исполнения.';
 }
 function v3FillSettings(){
  if(!view)return;
  profileRemoved.clear();$('recovery-profile-removals').textContent='';
  $('v3-setting-distance').value=view.v3_settings?.entry_distance??'1.00';
  $('recovery-sl-distance').value=profileConfig()?.recovery_sl_distance??'';
  loadProfileDraft(profileConfig()?.profiles.find(p=>p.id===profileConfig().selected_profile_id));
 }
 let v3SlDraft=null;
 const slDraftKey=()=>JSON.stringify([view?.scope,view?.cycle_id,view?.revision,view?.v3_sl_distance,(view?.positions??[]).map(p=>[p.id,p.sl]).sort()]);
 function clearSlDraft(){v3SlDraft=null;v3Render();}
 function v3Render(){
  const enabled=v3Enabled(),controls=view?.v3_controls??{},blocked=v3Blocked();
  $('v3-panels').hidden=false;$('v3-settings').hidden=false;
  $('chart-and-tools').classList.toggle('v3-active',true);
  $('save-v3-settings').disabled=blocked||!enabled;
  if(v3SlDraft&&v3SlDraft.key!==slDraftKey())v3SlDraft=null;
  const shown=v3SlDraft?.distance??view?.v3_sl_distance;
  const distance=Number(shown);
  $('v3-sl-value').textContent=Number.isFinite(distance)&&distance>=0?String(shown):'—';
  $('v3-sl-minus').disabled=blocked||!enabled||controls.can_adjust_sl!==true||!Number.isFinite(distance)||distance<=.1;
  $('v3-sl-plus').disabled=blocked||!enabled||controls.can_adjust_sl!==true||!Number.isFinite(distance)||distance<0;
  $('v3-sl-confirm').disabled=blocked||!enabled||controls.can_adjust_sl!==true||!v3SlDraft||Number(v3SlDraft.distance)===Number(view?.v3_sl_distance);
  $('v3-sl-status').textContent='Расстояние SL от входа для действующих позиций. Шаг 0.10. Изменения отправляются только кнопкой «Подтвердить». Связанные триггеры следуют за SL первой ступени; частичный отказ требует сверки.';
  $('v3-submit-entry').disabled=blocked||!enabled||controls.can_enter!==true;
  $('v3-cancel-request').hidden=!view?.v3_request;
  $('v3-cancel-request').disabled=blocked||!enabled||!view?.v3_request;
  const request=view?.v3_request;const targetTime=request&&Number.isFinite(Number(request.bar_id))?(utcLabel(Number(request.bar_id)+60)?utcLabel(Number(request.bar_id)+60).slice(11,19)+' UTC':'выбранной свечи (время не проверено)'):'выбранной свечи';
  $('v3-request-status').textContent=request?'Ожидается закрытие '+targetTime+': '+request.order_type+', '+profileSummary(request.recovery_profile)+'. Направление по цвету свечи; doji — пропуск.':'Нет запланированного входа. Для следующей попытки RECOVERY дождитесь закрытия всей корзины.';
  const linked=!!view?.v3_link,canCancel=controls.can_cancel_link===true;
  $('v3-link-toggle').textContent=linked?'Отменить триггеры на SL':'Выставить триггеры на SL';
  $('v3-link-toggle').dataset.cancel=String(linked);
  $('v3-link-toggle').disabled=blocked||!enabled||(linked?!canCancel:controls.can_link!==true);
  const link=view?.v3_link;
  $('v3-link-status').textContent=linked?(link.cancel?'Отмена запрошена; ожидается сверка MT5.':link.replace?'Перенос SL и связанных триггеров; ожидается сверка.':profileSummary(link.recovery_profile)+' · '+link.side+' STOP на '+link.price+'. Якорь — SL первой ступени.'):'Триггеры не выставлены. После выставления они связаны с SL первой ступени.';
  $('v3-control-reason').textContent=!v3Fresh?'Нет свежего снимка. Действия заблокированы.':friendlyReason(controls.reason);
  $('v3-cancel-entry').hidden=controls.can_cancel_entry!==true;
  $('v3-cancel-entry').disabled=blocked||!enabled||controls.can_cancel_entry!==true;
  $('v3-entry-distance').textContent='STOP: от Close выбранной свечи на '+(view?.v3_settings?.entry_distance??'—')+'. Объём и TP — по выбранному профилю.';
  renderProfiles();
  v3InputMode();
 }
 $('v3-order-type').onchange=v3InputMode;$('v3-timing').onchange=v3InputMode;
 // Each drawer owns its state; opening one never opens or closes the other.
 const sidePanelOrder=[],sidePanelDragCancels=[];
 const cancelSidePanelDrags=()=>sidePanelDragCancels.forEach(cancel=>cancel());
 function setSidePanel(name,open,restoreFocus=false){
  const panel=$('v3-'+name+'-panel'),toggle=$('toggle-'+name+'-panel');
  panel.dataset.open=String(open);panel.inert=!open;panel.style.transform='';panel.classList.toggle('dragging',false);toggle.setAttribute('aria-expanded',String(open));
  toggle.textContent=open?'<':'>';toggle.setAttribute('aria-label',(open?'Закрыть ':'Открыть ')+(name==='sl'?'контроль SL':'выставление ордеров'));toggle.title=(name==='sl'?'Контроль SL':'Ордера')+': нажмите или потяните '+(open?'влево':'вправо');
  const index=sidePanelOrder.indexOf(name);if(index>=0)sidePanelOrder.splice(index,1);
  if(open){sidePanelOrder.push(name);sidePanelOrder.forEach((key,i)=>{$('v3-'+key+'-panel').style.zIndex=String(30+i);});$('close-'+name+'-panel').focus({preventScroll:true});}
  else {if(name==='sl'&&typeof clearSlDraft==='function')clearSlDraft();if(restoreFocus)toggle.focus({preventScroll:true});}
 }
 for(const name of ['sl','entry']){
  const handle=$('toggle-'+name+'-panel'),panel=$('v3-'+name+'-panel');
  let drag=null,suppressClick=false;
  const reset=()=>{drag=null;panel.style.transform='';panel.classList.toggle('dragging',false);};
  const cancel=()=>{if(drag)suppressClick=true;reset();};
  sidePanelDragCancels.push(cancel);
  handle.onclick=e=>{if(suppressClick&&e?.detail!==0){suppressClick=false;e?.preventDefault();return;}cancel();setSidePanel(name,panel.dataset.open!=='true');};
  handle.addEventListener('pointerdown',e=>{
   if(drag){cancel();return;}
   if(e.isPrimary===false||(e.button!==undefined&&e.button!==0))return;
   suppressClick=false;drag={id:e.pointerId,x:e.clientX,y:e.clientY,open:panel.dataset.open==='true',dx:0,dy:0};
   handle.setPointerCapture(e.pointerId);e.stopPropagation();
  });
  handle.addEventListener('pointermove',e=>{
   if(!drag||e.pointerId!==drag.id)return;
   drag.dx=e.clientX-drag.x;drag.dy=e.clientY-drag.y;
   if(Math.abs(drag.dy)>12&&Math.abs(drag.dy)>Math.abs(drag.dx)){cancel();return;}
   if(Math.abs(drag.dx)<8)return;
   suppressClick=true;e.preventDefault();e.stopPropagation();
   const width=panel.getBoundingClientRect().width||320;
   const progress=Math.max(0,Math.min(1,(drag.open?1:0)+drag.dx/width));
   panel.classList.toggle('dragging',true);panel.style.transform='translateX('+((progress-1)*105)+'%)';
  });
  handle.addEventListener('pointerup',e=>{
   if(!drag||e.pointerId!==drag.id)return;
   const saved=drag,dx=e.clientX-saved.x,dy=e.clientY-saved.y;
   const moved=Math.abs(dx)>=8||Math.abs(dy)>=8;
   reset();
   if(moved){suppressClick=true;e.preventDefault();e.stopPropagation();if(Math.abs(dx)>=36&&Math.abs(dx)>Math.abs(dy))setSidePanel(name,dx>0);}
  });
  handle.addEventListener('pointercancel',cancel);handle.addEventListener('lostpointercapture',cancel);
  $('close-'+name+'-panel').onclick=()=>{cancel();setSidePanel(name,false,true);};
 }
 $('save-v3-settings').onclick=async()=>{if(v3Blocked()||!v3Enabled())return;try{const distance=positiveDecimal($('v3-setting-distance').value,'Отступ'),revision=view.revision,scope=view.scope;if(!confirm('Сохранить отступ RECOVERY от Close: '+distance+'?'))return;await send({type:'v3_settings',cycle_id:view.cycle_id,entry_distance:distance},revision,scope);}catch(e){error(e.message);}};
 $('v3-submit-entry').onclick=async()=>{if(v3Blocked()||!v3Enabled()||view.v3_controls?.can_enter!==true||!view.selected_recovery_profile)return;try{const action={type:'v3_entry',cycle_id:view.cycle_id,order_type:$('v3-order-type').value,timing:$('v3-timing').value,side:$('v3-side').value},revision=view.revision,scope=view.scope;if(action.order_type==='STOP'&&action.timing==='now')throw Error('STOP выставляется только после текущей или следующей M1.');const timing=action.timing==='now'?'Сейчас':action.timing==='current'?'После закрытия текущей M1':'После закрытия следующей M1';if(action.order_type==='MARKET'&&!confirm('ТОРГОВЛЯ'+' — '+action.order_type+', '+profileDetails(view.selected_recovery_profile)+'\nSL: '+view.selected_recovery_profile.sl_distance+'\n'+timing+'\nНаправление: '+(action.timing==='now'?action.side:'по цвету свечи; doji — пропуск')+(action.order_type==='STOP'?'\nОтступ от Close: '+view.v3_settings.entry_distance:'\nЦена определяется рыночным исполнением')+'\nПосле исполнения бот сопровождает корзину.'))return;await send(action,revision,scope);}catch(e){error(e.message);}};
 $('v3-cancel-request').onclick=async()=>{if(v3Blocked()||!v3Enabled()||!view.v3_request)return;const action={type:'v3_cancel_request',cycle_id:view.cycle_id},revision=view.revision,scope=view.scope;try{await send(action,revision,scope);}catch(e){error(e.message);}};
 $('v3-link-toggle').onclick=async()=>{if(v3Blocked()||!v3Enabled())return;try{const cancel=!!view.v3_link;if(cancel?view.v3_controls?.can_cancel_link!==true:view.v3_controls?.can_link!==true||!view.selected_recovery_profile)return;const action=cancel?{type:'v3_cancel_link',cycle_id:view.cycle_id}:{type:'v3_link',cycle_id:view.cycle_id,expected_positions:view.positions.map(p=>p.id).sort()},revision=view.revision,scope=view.scope;await send(action,revision,scope);}catch(e){error(e.message);}};
 $('v3-cancel-entry').onclick=async()=>{if(v3Blocked()||!v3Enabled()||view.v3_controls?.can_cancel_entry!==true)return;try{await send({type:'v3_cancel_entry',cycle_id:view.cycle_id},view.revision,view.scope);}catch(e){error(e.message);}};
 const shiftSlDistance=(raw,delta)=>{const value=String(raw);if(!/^\d+(\.\d+)?$/.test(value))return null;const fraction=(value.split('.')[1]||'').length,scale=Math.max(2,fraction),units=BigInt(value.replace('.',''))*10n**BigInt(scale-fraction)+BigInt(delta)*10n**BigInt(scale-2);if(units<=0n)return null;const text=units.toString().padStart(scale+1,'0');return text.slice(0,-scale)+'.'+text.slice(-scale);};
 function v3AdjustSl(delta){if(v3Blocked()||!v3Enabled()||view.v3_controls?.can_adjust_sl!==true)return;if(v3SlDraft&&v3SlDraft.key!==slDraftKey())v3SlDraft=null;const distance=shiftSlDistance(v3SlDraft?.distance??view.v3_sl_distance,delta);if(distance===null)return;v3SlDraft={key:slDraftKey(),distance};v3Render();}
 $('v3-sl-minus').onclick=()=>v3AdjustSl(-10);$('v3-sl-plus').onclick=()=>v3AdjustSl(10);
 $('v3-sl-confirm').onclick=async()=>{if(v3Blocked()||!v3Enabled()||view.v3_controls?.can_adjust_sl!==true||!v3SlDraft)return;if(v3SlDraft.key!==slDraftKey()){clearSlDraft();error('Данные SL изменились. Выберите расстояние заново.');return;}const distance=v3SlDraft.distance;if(Number(distance)===Number(view.v3_sl_distance))return;const action={type:'v3_adjust_sl',cycle_id:view.cycle_id,distance,expected_positions:view.positions.map(p=>p.id).sort()},revision=view.revision,scope=view.scope;v3SlDraft=null;v3Render();try{await send(action,revision,scope);}catch(e){error(e.message);}};

 function fields(){if(!view)return;v3FillSettings();$('fields').replaceChildren();for(const [k,v] of Object.entries(view.settings)){if(['EXPANSION_DISTANCE','TICKET_SIZE','BASKET_COUNT','RECOVERY_SL_DISTANCE'].includes(k)||(view.tp_distances&&['COST_RESERVE','TP_STEP_MULTIPLIER'].includes(k)))continue;const label=document.createElement('label');label.textContent=names[k]??k;const input=document.createElement('input');input.id='setting-'+k;input.value=v;input.inputMode='decimal';label.append(input);const active=document.createElement('small');active.textContent='В цикле: '+(view.active_settings?.[k]??'—');label.append(active);$('fields').append(label);} $('tp-grid-settings').hidden=true;$('tp-fields').replaceChildren();if(view.tp_distances)for(let i=0;i<Number(view.settings.BASKET_COUNT);i++){const label=document.createElement('label');label.textContent='TP '+(i+1)+(i===0?' · обязательно':' · пусто = авто');const input=document.createElement('input');input.id='tp-distance-'+i;input.inputMode='decimal';input.value=view.tp_distances[i]??'';label.append(input);const active=document.createElement('small');active.textContent='В цикле: '+(view.active_tp_distances?.[i]??'авто / не задано');label.append(active);$('tp-fields').append(label);}}
 $('refresh-settings').onclick=()=>{if((profileDraftDirty()||profileRemoved.size)&&!confirm('Загрузить актуальные параметры? Несохранённый черновик профиля и исключения будут заменены.'))return;fields();error('Загружены актуальные параметры.');};
 $('save-settings').onclick=async()=>{if(!view)return;const settings={};for(const k of Object.keys(view.settings)){if(['EXPANSION_DISTANCE','TICKET_SIZE','BASKET_COUNT','RECOVERY_SL_DISTANCE'].includes(k)||(view.tp_distances&&['COST_RESERVE','TP_STEP_MULTIPLIER'].includes(k))){settings[k]=view.settings[k];continue;}const raw=$('setting-'+k).value.trim();if(raw===''||!Number.isFinite(Number(raw))){error('Проверьте числовое значение: '+(names[k]??k));return;}settings[k]=k==='BASKET_COUNT'?Number(raw):raw;}const revision=view.revision,scope=view.scope;if(!confirm('Применить параметры? Объём открытых билетов не меняется.\n'+Object.entries(settings).filter(([k])=>!['EXPANSION_DISTANCE','TICKET_SIZE','BASKET_COUNT','RECOVERY_SL_DISTANCE'].includes(k)).map(([k,v])=>(names[k]??k)+': '+v).join('\n')))return;try{await send({type:'basket_settings',settings,cycle_id:view.cycle_id},revision,scope);}catch(e){error(e.message);}};
 $('writeoff-recovery').onclick=async()=>{if(v3Blocked()||view.positions.length||view.v3_controls?.can_writeoff_recovery!==true)return;const v=view,revision=v.revision,scope=v.scope;const action={type:'writeoff_recovery',cycle_id:v.cycle_id,expected_debt:v.debt,confirmed:true};if(!confirm('ТОРГОВЛЯ'+' — списать RECOVERY '+v.debt+' UST? Фактическая доходность не изменится: убыток уже учтён. Разрешено только без открытых позиций. Ожидающие триггеры и выбранная свеча сохраняются. Новые убытки после сброса снова учитываются.'))return;try{await send(action,revision,scope);}catch(e){error(e.message);}};
 $('save-tp-grid').onclick=async()=>{if(!view?.tp_distances)return;const values=[...view.tp_distances],scope=view.scope,revision=view.revision,cycle_id=view.cycle_id;for(let i=0;i<Number(view.settings.BASKET_COUNT);i++){const raw=$('tp-distance-'+i).value.trim();if((i===0&&!raw)||(raw&&(!Number.isFinite(Number(raw))||Number(raw)<=0))){error('Задайте положительное расстояние TP '+(i+1));return;}values[i]=raw||null;}if(!confirm('Применить расстояния TP? SL остаются на фактических текущих уровнях.\n'+values.slice(0,Number(view.settings.BASKET_COUNT)).map((v,i)=>'TP '+(i+1)+': '+(v??'авто')).join('\n')))return;try{await send({type:'tp_distances',cycle_id,distances:values},revision,scope);}catch(e){error(e.message);}};
 function node(tag,attrs,text){const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;return n;}
 function chartPoint(clientX,clientY){const point=svg.createSVGPoint();point.x=clientX;point.y=clientY;return point.matrixTransform(svg.getScreenCTM().inverse());}
 // Viewport changes only: never request or alter orders while navigating history.
 function visibleCandles(){const all=view?.feed?.candles??[];const count=Math.max(10,Math.min(240,Math.round(visibleCount)));timeOffset=Math.max(0,Math.min(timeOffset,Math.max(0,all.length-count)));const end=all.length-Math.round(timeOffset);return all.slice(Math.max(0,end-count),end);}
 function domain(){if(preview?.dragDomain)return preview.dragDomain;const prices=[...lines.map(l=>l.price),...visibleCandles().flatMap(c=>[Number(c.low),Number(c.high)]),Number(view?.feed?.quote?.bid),Number(view?.feed?.quote?.ask)].filter(x=>Number.isFinite(x)&&x>0);let lo=Math.min(...prices),hi=Math.max(...prices);if(!prices.length){lo=0;hi=1;}const mid=(lo+hi)/2+pan,span=Math.max(1,hi-lo)*1.25/zoom;return{min:mid-span/2,max:mid+span/2};}
 function syncPreviewDialog(){
  const active=!!preview,open=active&&!preview.dragging;$('preview-backdrop').hidden=!open;$('preview').hidden=!open;
  for(const id of ['command-drawer','v3-panels','settings-page','history-page','bottom-tabs','analysis-pane','bot-activity'])$(id).inert=active;
  if(open&&!previewWasOpen){cancelSidePanelDrags();previewFocus=document.activeElement;$('cancel-level').focus({preventScroll:true});}
  if(!open&&previewWasOpen)(previewFocus?.isConnected?previewFocus:$('drawer-toggle')).focus({preventScroll:true});
  previewWasOpen=open;
 }
 function draw(){
  syncPreviewDialog();
  if(!view)return;lines=[];const colors=prefs.colors;
  for(const [side,price] of Object.entries(view.levels)){const pendingOrder=view.orders.some(o=>o.side===side&&Number(o.price)===Number(price));if(pendingOrder||view.frozen_levels)lines.push({key:'trigger-'+side,kind:'trigger',side,price:Number(price),label:side+(pendingOrder?' STOP':' · уровень цикла'),color:colors[side]});}
  for(const p of view.positions)for(const key of ['entry','tp','sl'])lines.push({key:p.id+'-'+key,kind:key,pid:p.id,price:Number(p[key]),label:key.toUpperCase()+(Number.isInteger(view.tickets?.find(t=>t.id===p.id)?.index)?' · ступень '+(view.tickets.find(t=>t.id===p.id).index+1):'')+' · #'+(p.ticket??p.id).slice(-6),color:colors[key]});
  if(!lines.length&&!(view.feed.candles??[]).length&&!Number(view.feed.quote?.bid)){svg.replaceChildren(node('text',{x:230,y:250,fill:'#969ba6','font-size':28},'Ожидание реальных данных MT5'));$('level-buttons').replaceChildren();drawAnalysis();return;}
  frameHeight=Math.max(240,svg.clientHeight/Math.max(1,svg.clientWidth)*1000);plotBottom=frameHeight-45;plotHeight=plotBottom-20;svg.setAttribute('viewBox','0 0 1000 '+frameHeight);
  const labelSize=svg.clientWidth<680?32:17;const labelRows=[];
  // Reserve a price-only gutter before positioning any candle, at every zoom level.
  const quoteText=key=>key.toUpperCase()+' '+Number(view.feed.quote?.[key]).toFixed(2);
  const quoteWidth=Math.max(155,...['ask','bid'].map(key=>quoteText(key).length*labelSize*.9*.65+14));
  const badgeX=Math.max(500,998-quoteWidth);plotRight=badgeX-18;
  const d=domain(),y=p=>plotBottom-(p-d.min)/(d.max-d.min)*plotHeight;svg.replaceChildren();
  for(let i=0;i<=6;i++){const price=d.min+(d.max-d.min)*i/6,py=y(price);svg.append(node('line',{x1:0,x2:plotRight,y1:py,y2:py,stroke:colors.grid}),node('text',{x:badgeX+7,y:py+5,fill:'#afc0d2','font-size':labelSize},price.toFixed(2)));}
  const candles=visibleCandles(),w=(plotRight-20)/Math.max(3,candles.length+2);svg.dataset.firstCandle=String(candles[0]?.utc_time??'');svg.dataset.visibleCount=String(candles.length);candles.forEach((c,i)=>{const x=10+(i+.5)*w,green=Number(c.close)>=Number(c.open),color=green?colors.up:colors.down;svg.append(node('line',{x1:x,x2:x,y1:y(Number(c.high)),y2:y(Number(c.low)),stroke:color}),node('rect',{x:x-w*.3,y:Math.min(y(Number(c.open)),y(Number(c.close))),width:Math.max(2,w*.6),height:Math.max(1,Math.abs(y(Number(c.open))-y(Number(c.close)))),fill:color}));});
  for(const l of lines){const py=y(l.price);if(py<0||py>frameHeight)continue;const g=node('g',{'data-line':l.key,visibility:prefs.mainCycle?'visible':'hidden'});g.append(node('line',{x1:0,x2:plotRight,y1:py,y2:py,stroke:l.color,'stroke-width':2,'stroke-dasharray':'none'}),node('line',{x1:0,x2:plotRight,y1:py,y2:py,stroke:'transparent','stroke-width':18}));g.append(node('title',{},l.label+' '+l.price.toFixed(2)));if(!labelRows.some(row=>Math.abs(row-py)<labelSize)){g.append(node('text',{x:badgeX+7,y:py-5,fill:l.color,'font-size':labelSize-2},l.price.toFixed(2)));labelRows.push(py);}svg.append(g);}
  // Bid/Ask are display-only live quote lines, never draggable trading levels.
  const quoteLabels=[];
  for(const [key,color] of [['ask','#ff5b63'],['bid','#24d99a']]){
   const price=Number(view.feed.quote?.[key]);if(!Number.isFinite(price)||price<=0)continue;
   const py=y(price);if(py<20||py>plotBottom)continue;
   const badgeHalf=labelSize/2+5,quoteGap=labelSize+14;
   let ly=Math.max(20+badgeHalf,Math.min(plotBottom-badgeHalf,py));
   for(const previous of quoteLabels)if(Math.abs(ly-previous)<quoteGap)ly=previous+quoteGap<=plotBottom-badgeHalf?previous+quoteGap:previous-quoteGap;
   quoteLabels.push(ly);
   const g=node('g',{'data-quote':key,'pointer-events':'none'});
   g.append(node('line',{x1:0,x2:badgeX-5,y1:py,y2:py,stroke:color,'stroke-width':1.5,'stroke-dasharray':'3 4'}),node('line',{x1:badgeX-5,x2:badgeX,y1:py,y2:ly,stroke:color}),node('rect',{x:badgeX,y:ly-labelSize/2-5,width:998-badgeX,height:labelSize+10,rx:7,fill:'#08090b',stroke:color}),node('text',{x:badgeX+7,y:ly+labelSize/3,fill:'#f1f3f5','font-size':labelSize*.9},quoteText(key)));svg.append(g);
  }
  candles.forEach((c,i)=>{if(i%Math.max(1,Math.floor(candles.length/5))===0&&utcLabel(c.utc_time,true)){const label=utcLabel(c.utc_time,true);svg.append(node('text',{x:10+(i+.5)*w,y:frameHeight-10,fill:'#969ba6','font-size':labelSize},label));}});
  svg.append(node('text',{x:badgeX+7,y:frameHeight-10,fill:'#969ba6','font-size':labelSize},feedUtcVerified()?'UTC':'RAW'));
  if(!feedUtcVerified())svg.append(node('title',{},'Время MT5 не проверено; часовые метки скрыты.'));
  if(!candles.length)svg.append(node('text',{x:300,y:250,fill:'#969ba6','font-size':20},'Ожидание данных MT5'));
  if(preview){const py=y(preview.value);svg.append(node('line',{x1:0,x2:plotRight,y1:py,y2:py,stroke:'#fff','stroke-width':3,'stroke-dasharray':'none'}));}
  drawAnalysis();
  $('level-buttons').replaceChildren();for(const l of lines){const b=document.createElement('button');b.textContent=l.label+' '+l.price.toFixed(2);b.style.borderColor=l.color;b.disabled=l.kind==='entry';b.onclick=()=>openPreview(l,l.price);$('level-buttons').append(b);}
 }
 function openPreview(line,value,dragging=false){
  if(v3Blocked()||line.kind==='entry')return false;
  if(line.pid&&!view.positions.some(p=>p.id===line.pid&&Number(p[line.kind])===line.price))return false;
  const dragDomain=dragging?domain():null;
  $('preview-error').textContent='';preview={line:{...line},value,backend:structuredClone(view),dragging,dragDomain};
  $('preview').hidden=dragging;$('new-price').value=value.toFixed(2);$('preview-text').textContent=line.label+': '+line.price.toFixed(2)+' → '+value.toFixed(2);
  $('preview-scope').textContent=line.kind==='sl'?'SL изменится у всех живых билетов этой корзины.':line.kind==='tp'?'TP выбранного билета. Другие ручные TP сохранятся.':'Правка ожидающего триггера.';
  draw();return true;
 }
 function priceAt(clientY){const r=svg.getBoundingClientRect(),d=domain(),local=chartPoint(r.left,clientY);return Math.round((d.min+(plotBottom-local.y)/plotHeight*(d.max-d.min))*100)/100;}
 const pointers=new Map();let pinch=null;
 const distanceBetween=points=>Math.hypot(points[0].x-points[1].x,points[0].y-points[1].y);
 function cancelGesture(){clearTimeout(hold);pointers.clear();pinch=null;if(gesture?.drag){preview=null;$('preview').hidden=true;}gesture=null;}
 svg.addEventListener('pointerdown',e=>{
  if(preview){if(preview.dragging){cancelGesture();draw();}return;}if(e.button!==undefined&&e.button!==0)return;svg.setPointerCapture(e.pointerId);pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pointers.size>1){clearTimeout(hold);gesture=null;pinch={distance:distanceBetween([...pointers.values()]),count:visibleCount,zoom};return;}
  const l=lines.find(l=>l.key===e.target.closest('[data-line]')?.dataset.line);
  gesture={id:e.pointerId,start:e.clientY,startX:e.clientX,pan,offset:timeOffset,span:domain().max-domain().min,line:l,drag:false,moved:false};
  if(l&&l.kind!=='entry')hold=setTimeout(()=>{if(gesture&&!gesture.moved&&gesture.id===e.pointerId){if(openPreview(l,l.price,true))gesture.drag=true;}},1000);
 });
 svg.addEventListener('pointermove',e=>{
  if(!pointers.has(e.pointerId))return;pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pinch&&pointers.size===2){const ratio=distanceBetween([...pointers.values()])/Math.max(1,pinch.distance);visibleCount=Math.max(10,Math.min(240,pinch.count/ratio));draw();return;}
  if(!gesture||gesture.id!==e.pointerId)return;
  if(gesture.drag&&preview){preview.value=priceAt(e.clientY);$('new-price').value=preview.value.toFixed(2);$('preview-text').textContent=preview.line.label+': '+preview.line.price.toFixed(2)+' → '+preview.value.toFixed(2);draw();return;}
  const dx=e.clientX-gesture.startX,dy=e.clientY-gesture.start;if(Math.hypot(dx,dy)<=8&&!gesture.moved)return;
  clearTimeout(hold);gesture.moved=true;
  const local=chartPoint(e.clientX,e.clientY),start=chartPoint(gesture.startX,gesture.start),localDx=local.x-start.x,localDy=local.y-start.y;
  // Use SVG coordinates, including letterboxing on short or split chart panes.
  if(Math.abs(dx)>=Math.abs(dy))timeOffset=gesture.offset+localDx/Math.max(1,plotRight)*visibleCount;
  else if(local.x>plotRight)pan=gesture.pan+localDy/Math.max(1,plotHeight)*gesture.span;
  else timeOffset=gesture.offset-localDy/Math.max(1,plotHeight)*visibleCount;
  draw();
 });
 const end=e=>{clearTimeout(hold);pointers.delete(e.pointerId);if(gesture?.id===e.pointerId&&gesture.drag&&preview){if(e.type==='pointerup'){preview.dragging=false;$('preview').hidden=false;}else{preview=null;$('preview').hidden=true;}}if(gesture?.id===e.pointerId)gesture=null;if(pointers.size<2)pinch=null;draw();};
 svg.addEventListener('contextmenu',e=>{if(gesture?.line||preview)e.preventDefault();});
 svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);svg.addEventListener('lostpointercapture',end);
 svg.addEventListener('wheel',e=>{e.preventDefault();if(preview)return;visibleCount=Math.max(10,Math.min(240,visibleCount*(e.deltaY>0?1.12:1/1.12)));draw();},{passive:false});
 $('zoom-in').onclick=()=>{visibleCount=Math.max(10,visibleCount/1.3);draw();};$('zoom-out').onclick=()=>{visibleCount=Math.min(240,visibleCount*1.3);draw();};$('chart-reset').onclick=()=>{zoom=1;pan=0;timeOffset=0;visibleCount=60;draw();};
 $('cancel-level').onclick=()=>{cancelGesture();preview=null;$('preview').hidden=true;draw();};
 $('apply-level').onclick=async()=>{if(!preview||preview.dragging)return;const p=preview;if(v3Blocked()||view.scope!==p.backend.scope||view.cycle_id!==p.backend.cycle_id||view.revision!==p.backend.revision){error('Данные изменились или недоступны. Отмените правку и выберите линию заново.');return;}if(p.line.pid&&JSON.stringify(view.positions.map(x=>x.id).sort())!==JSON.stringify(p.backend.positions.map(x=>x.id).sort())){error('Состав позиций изменился. Отмените правку и выберите линию заново.');return;}if(p.line.pid){const current=view.positions.find(x=>x.id===p.line.pid),original=p.backend.positions.find(x=>x.id===p.line.pid);if(!current||!original||String(current.sl)!==String(original.sl)||String(current.tp)!==String(original.tp)){error('Позиция или её уровни изменились. Отмените правку и выберите линию заново.');return;}}const value=Number($('new-price').value);if(!Number.isFinite(value)||value<=0){error('Цена должна быть положительным числом.');return;}let action;if(p.line.kind==='trigger'){action={type:'basket_levels',cycle_id:p.backend.cycle_id,expected_orders:p.backend.orders.map(o=>o.id),expected:p.backend.levels,levels:{...p.backend.levels,[p.line.side]:String(value)}};}else{const position=p.backend.positions.find(x=>x.id===p.line.pid);action={type:'protection',cycle_id:p.backend.cycle_id,position_id:position.id,old:{sl:position.sl,tp:position.tp},new:{sl:position.sl,tp:position.tp,[p.line.kind]:String(value)}};}try{await send(action,p.backend.revision,p.backend.scope);preview=null;$('preview').hidden=true;}catch(e){error(e.message);}draw();};
 $('new-price').oninput=()=>{if(!preview||preview.dragging)return;const value=Number($('new-price').value);if(!Number.isFinite(value)||value<=0)return;preview.value=value;$('preview-text').textContent=preview.line.label+': '+preview.line.price.toFixed(2)+' → '+value.toFixed(2);draw();};
 function renderTotalsHistory(){
  const metrics=view?.metrics;
  const money=(value,currency)=>value===null||value===undefined||value===''?'—':String(value)+(currency?' '+currency:'');
  const debt=metrics?.recovery_total;
  const negative=debt===null||debt===undefined?null:Number(debt)===0?'0':'-'+String(debt).replace(/^[-+]/,'');
  notice('activity-recovery',money(negative,metrics?.currency),'info');
  const total=metrics?.all_closed_net;
  notice('activity-total-net',money(total,metrics?.currency),total===null||total===undefined?'info':Number(total)<0?'loss':'success');
  $('activity-totals-note').textContent=metrics?.history_complete===true?'Общий результат: закрытые позиции с комиссиями, без плавающего P/L.':'Общий результат недоступен: история исполнений неполна.';
  const records=Array.isArray(view?.position_history)?view.position_history:[];
  $('position-history-note').textContent=metrics?.history_note??(metrics?.history_complete===true?'':'Нет подтверждённой полной истории.');
  $('position-history-empty').textContent=records.length?'':'Исполненных позиций пока нет.';
  $('position-history-list').replaceChildren();
  const stamp=value=>{if(value===null||value===undefined||value==='')return '—';const date=new Date(Number(value)*1000);return Number.isFinite(date.getTime())?date.toISOString().replace('T',' ').replace('.000Z',' UTC'):'—';};
  const reasons={TP:'TP',SL:'SL',MANUAL:'Ручное закрытие',MANUAL_CLOSE:'Ручное закрытие',PAPER_INTERRUPT:'Прерывание пользователем',EXPERT:'Закрытие советником',MIXED:'Несколько причин закрытия'};
  for(const record of records){
   const card=document.createElement('article');card.className='position-history-card';
   const heading=document.createElement('h2');heading.textContent=['BUY','SELL'].includes(record.side)?record.side:'Сторона не подтверждена';card.append(heading);
   const reason=reasons[record.reason]??(record.reason?'Другая подтверждённая причина':'Причина не подтверждена');
   const lines=[['Открытие',stamp(record.opened_at)],['Цена открытия',record.entry_price??'—'],['Закрытие',record.status==='open'?'Позиция открыта':record.status==='partial'?'Закрыта частично':stamp(record.closed_at)],['Цена закрытия',record.exit_price??'—']];
   for(const [label,value] of lines){const line=document.createElement('p');line.textContent=label+': '+value;card.append(line);}
   const result=document.createElement('strong');result.dataset.tone=record.net===null||record.net===undefined?'info':Number(record.net)<0?'warning':'success';
   result.textContent=(record.status==='open'?'Итог после закрытия':reason)+' · '+money(record.net,record.currency);card.append(result);
   if(record.complete===false){const note=document.createElement('p');note.textContent='Данные позиции неполны; неизвестные значения показаны как —.';card.append(note);}
   $('position-history-list').append(card);
  }
 }
 function render(){
  renderTotalsHistory();
  connectionLabel();updateBotActivity();
  $('phase').textContent=view.phase;$('cycle').textContent=view.cycle_id??'Ожидание цикла';$('debt').textContent=view.debt+' UST';$('realized').textContent='Учёт с переносом: '+view.realized+(view.metrics?.currency?' '+view.metrics.currency:'');$('feed-status').textContent=view.feed.status;$('mode').textContent=view.read_only?'READ_ONLY':view.stopped?'Новые входы остановлены':'EXECUTION';
  $('safety').textContent=''; // Safety is shown once in the colored activity area.
  $('quote').textContent='Bid '+(view.feed.quote?.bid??'—')+' / Ask '+(view.feed.quote?.ask??'—')+' · возраст '+(view.feed.age_seconds??'—')+' сек.';
  $('state-heading').textContent='Брокерское состояние';
  $('execution-note').textContent='Открытые билеты сохраняют брокерские SL/TP. Закрытие браузера не останавливает сервис.';
  $('source-label').textContent='Источник: MT5 / торговый контур'+' · '+(utcLabel(view.feed.timestamp)||'время MT5 не проверено');
  $('reverse-status').textContent=(view.reverse_ladders??[]).some(x=>x.enabled)?'Обратная лестница включена. Фактические SL показаны на графике; автоматического подтягивания нет.':'Обратная лестница выключена; доступна после подтверждённого переноса SL по TP1.';
  $('projection').textContent=view.projection?'Прибыль по текущим TP: '+view.projection.gross_tp+'; резерв расходов: '+view.projection.cost_reserve+'; ожидаемое недопокрытие: '+view.projection.shortfall+' UST.'+' Будущие комиссии неизвестны до подтверждения.':'Расчёт покрытия пока недоступен.';
  $('edit-status').textContent=view.edit?'Правка '+view.edit.stage+': показываются фактические данные брокера. При частичном отказе автоматизация остановится.':'';$('journal').textContent=view.journal.join('\n');
  $('tickets').replaceChildren();for(const p of [...view.positions.map(p=>({...p,label:'Позиция'})),...view.orders.map(p=>({...p,entry:p.price,label:'STOP'}))]){const row=document.createElement('tr');for(const text of[p.ticket??p.id,p.label,p.side+' / '+p.size,p.entry,p.tp,p.sl]){const td=document.createElement('td');td.textContent=text;row.append(td);}$('tickets').append(row);}
  $('request-status').textContent=pending?'Запрос '+pending+': '+(pendingScope===view.scope?view.action_results[pending]?.status??'ожидание':'другой контекст')+'. До сверки новые команды запрещены. При UNKNOWN проверьте журнал и брокера на VPS; перезагрузка не разрешает повтор.':'';
  for(const id of ['start','stop','resume','reset','cancel-initial','save-settings','reverse-enable','reverse-disable','save-tp-grid','apply-level'])$(id).disabled=!!pending||busy;
  $('own-entry-sl').disabled=!!pending||busy||view.read_only||!view.positions.length||(!v3Enabled()||view.v3_controls?.can_own_entry_sl!==true)||!!view.edit||!!view.manual_close||!!view.manual_reason||!!view.migration_block||!!view.time_block;
  $('writeoff-recovery').disabled=v3Blocked()||view.positions.length>0||view.v3_controls?.can_writeoff_recovery!==true;
  $('reverse-enable').disabled=!!pending||busy||view.reverse_controls?.can_enable!==true;
  $('reverse-disable').disabled=!!pending||busy||view.reverse_controls?.can_disable!==true;
  $('reverse-enable').title=view.reverse_controls?.reason??'';$('reverse-disable').title=view.reverse_controls?.reason??'';
  if(loadedScope!==view.scope){fields();loadedScope=view.scope;}v3Render();draw();
 }
 async function poll(){if(polling||busy)return;polling=true;try{const r=await fetch(apiPath('state'),{headers:headers(),signal:AbortSignal.timeout(2500)});if(!r.ok){if([401,403].includes(r.status)){$('login').hidden=false;$('workspace').hidden=true;}throw Error('Нет авторизованного снимка');}const v=await r.json();if(v.contract!=='ticket_basket_v1'||v.mode==='PAPER')throw Error('Несовместимая версия контроллера');view=v;v3Fresh=true;const status=pending&&pendingScope===v.scope?v.action_results[pending]?.status:null;if(['APPLIED','REJECTED','CONFLICT'].includes(status)){lastActionReceipt={status,reason:v.action_results[pending]?.reason};clearPending();}$('login').hidden=true;$('workspace').hidden=false;render();}catch(e){v3Fresh=false;if(view){v3Render();connectionLabel();updateBotActivity();}$('feed-status').textContent='Нет свежего снимка';}finally{polling=false;}}
 const defaultPrefs={mainTrend:false,mainCycle:true,pane:null,pattern:{trend:false,cycle:true},figure:{trend:false,cycle:true},liquidity:false,opacity:20,colors:{BUY:'#26a69a',SELL:'#ef5350',sl:'#ef5350',tp:'#26a69a',entry:'#e0b565',up:'#26a69a',down:'#ef5350',grid:'#25282e',zone:'#e0b565'}};
 let prefs=structuredClone(defaultPrefs);try{const saved=JSON.parse(localStorage.getItem('chart-preferences-v1')??'null');if(saved){for(const k of ['mainTrend','mainCycle','liquidity'])if(typeof saved[k]==='boolean')prefs[k]=saved[k];if(['pattern','figure',null].includes(saved.pane))prefs.pane=saved.pane;for(const pane of ['pattern','figure'])for(const k of ['trend','cycle'])if(typeof saved[pane]?.[k]==='boolean')prefs[pane][k]=saved[pane][k];if(Number.isFinite(saved.opacity))prefs.opacity=Math.max(0,Math.min(100,saved.opacity));for(const k of Object.keys(prefs.colors))if(/^#[0-9a-f]{6}$/i.test(saved.colors?.[k]))prefs.colors[k]=saved.colors[k];}}catch{}
 const savePrefs=()=>{localStorage.setItem('chart-preferences-v1',JSON.stringify(prefs));layout();draw();};
 function layout(){$('analysis-pane').hidden=!prefs.pane;$('chart-workspace').classList.toggle('split',!!prefs.pane);$('main-trend').checked=prefs.mainTrend;$('main-cycle').checked=prefs.mainCycle;$('liquidity-visible').checked=prefs.liquidity;$('zone-opacity').value=prefs.opacity;if(prefs.pane){$('analysis-title').textContent=prefs.pane==='pattern'?'Свечные паттерны':'Графические фигуры';$('analysis-trend').checked=prefs[prefs.pane].trend;$('analysis-cycle').checked=prefs[prefs.pane].cycle;}}
 function drawAnalysis(){const target=$('analysis-chart');target.replaceChildren();if(!prefs.pane)return;const height=Math.max(240,target.clientHeight/Math.max(1,target.clientWidth)*1000),ratio=(height-65)/plotHeight;target.setAttribute('viewBox','0 0 1000 '+height);for(const child of svg.children){if(child.hasAttribute('data-line')&&!prefs[prefs.pane].cycle)continue;const copy=child.cloneNode(true);if(copy.hasAttribute('data-line'))copy.setAttribute('visibility','visible');for(const element of [copy,...copy.querySelectorAll('*')]){for(const attr of ['y','y1','y2'])if(element.hasAttribute(attr)){const value=Number(element.getAttribute(attr));element.setAttribute(attr,value>=frameHeight-15?height-10:20+(value-20)*ratio);}if(element.tagName==='rect'&&element.hasAttribute('height'))element.setAttribute('height',Number(element.getAttribute('height'))*ratio);}target.append(copy);}}

 $('open-pattern').onclick=()=>{prefs.pane=prefs.pane==='pattern'?null:'pattern';savePrefs();};$('open-figure').onclick=()=>{prefs.pane=prefs.pane==='figure'?null:'figure';savePrefs();};$('close-analysis').onclick=()=>{prefs.pane=null;savePrefs();};
 for(const [id,key]of [['main-trend','mainTrend'],['main-cycle','mainCycle'],['liquidity-visible','liquidity']])$(id).onchange=e=>{prefs[key]=e.target.checked;savePrefs();};
 for(const key of ['trend','cycle'])$('analysis-'+key).onchange=e=>{if(prefs.pane){prefs[prefs.pane][key]=e.target.checked;savePrefs();}};
 $('zone-opacity').oninput=e=>{prefs.opacity=Number(e.target.value);savePrefs();};
 const colorNames={BUY:'BUY STOP',SELL:'SELL STOP',sl:'SL',tp:'TP',entry:'Вход',up:'Растущая свеча',down:'Падающая свеча',grid:'Сетка',zone:'Будущие зоны'};
 for(const [k,v]of Object.entries(prefs.colors)){const label=document.createElement('label');label.textContent=colorNames[k];const input=document.createElement('input');input.type='color';input.id='color-'+k;input.value=v;input.oninput=()=>{prefs.colors[k]=input.value;savePrefs();};label.append(input);$('color-fields').append(label);}
 let logPage=0;
 async function showLogs(){const scope=$('log-scope').value;try{const r=await fetch('/api/logs?scope='+encodeURIComponent(scope)+'&page='+logPage,{headers:{'X-Local-Control':'1'},signal:AbortSignal.timeout(4000)});if(!r.ok)throw Error('Логи недоступны: '+r.status);const v=await r.json();if(scope!==$('log-scope').value)return;$('logs-view').textContent=v.text;$('logs-status').textContent='Страница '+(v.page+1)+' · ограниченная выгрузка';$('logs-prev').disabled=v.page===0;$('logs-next').disabled=!v.has_more;}catch(e){$('logs-status').textContent=e.message;}}
 $('logs-refresh').onclick=()=>{logPage=0;showLogs();};$('log-scope').onchange=()=>{logPage=0;showLogs();};$('logs-prev').onclick=()=>{logPage=Math.max(0,logPage-1);showLogs();};$('logs-next').onclick=()=>{logPage++;showLogs();};
 $('logs-download').onclick=async()=>{const scope=$('log-scope').value;try{const r=await fetch('/api/logs/download?scope='+encodeURIComponent(scope),{headers:{'X-Local-Control':'1'},signal:AbortSignal.timeout(5000)});if(!r.ok)throw Error('Скачивание отклонено: '+r.status);const blob=await r.blob();if(blob.size>1048576)throw Error('Файл превысил лимит');const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='tradfi-'+scope+'-logs.txt';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);$('logs-status').textContent='Подготовлен файл UTF-8: '+a.download;}catch(e){$('logs-status').textContent=e.message;}};
 // Local display preference only; each controller role has its own storage key.
 const chartHeightKey=storageKey('chart-height-v1');
 function normalizeChartHeight(value){
  const percent=typeof value?.percent==='number'&&Number.isFinite(value.percent)?Math.max(30,Math.min(85,Math.round(value.percent/5)*5)):60;
  return{mode:value?.mode==='custom'?'custom':'auto',percent};
 }
 let chartHeight=normalizeChartHeight(null);
 try{chartHeight=normalizeChartHeight(JSON.parse(localStorage.getItem(chartHeightKey)??'null'));}catch{}
 function applyChartHeight(save=false){
  chartHeight=normalizeChartHeight(chartHeight);
  $('chart-page').dataset.chartHeight=chartHeight.mode;
  $('chart-page').style.setProperty('--chart-height',chartHeight.percent+'vh');
  $('chart-page').style.setProperty('--chart-height-dynamic',chartHeight.percent+'dvh');
  $('chart-height-mode').value=chartHeight.mode;$('chart-height-range').value=String(chartHeight.percent);
  $('chart-height-range').disabled=chartHeight.mode!=='custom';$('chart-height-value').textContent=chartHeight.percent+'%';
  $('chart-height-status').textContent=chartHeight.mode==='auto'?'График занимает доступную высоту.':'Выбрано '+chartHeight.percent+'% высоты экрана.';
  if(save){try{localStorage.setItem(chartHeightKey,JSON.stringify(chartHeight));}catch{$('chart-height-status').textContent+=' Сохранение недоступно: выбор действует до перезагрузки.';}}
  requestAnimationFrame(draw);
 }
 $('chart-height-mode').onchange=()=>{chartHeight.mode=$('chart-height-mode').value;applyChartHeight(true);};
 $('chart-height-range').oninput=()=>{chartHeight.percent=Number($('chart-height-range').value);applyChartHeight(true);};
 $('chart-height-reset').onclick=()=>{chartHeight.mode='auto';applyChartHeight(true);};
 applyChartHeight();
 // Presentation-only navigation: the LIVE role and API paths never change.
 let activeTab='chart';
 function setTab(tab){if(preview)return;cancelGesture();cancelSidePanelDrags();for(const name of [...sidePanelOrder])setSidePanel(name,false);activeTab=['chart','history','settings'].includes(tab)?tab:'chart';for(const name of ['chart','history','settings']){$(name+'-page').hidden=activeTab!==name;$('tab-'+name).setAttribute('aria-selected',String(activeTab===name));}requestAnimationFrame(draw);}
 $('tab-chart').onclick=()=>setTab('chart');$('tab-history').onclick=()=>setTab('history');$('tab-settings').onclick=()=>setTab('settings');
 for(const name of ['chart','history','settings'])$('tab-'+name).addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const tabs=['chart','history','settings'],other=tabs[(tabs.indexOf(name)+(e.key==='ArrowRight'?1:2))%tabs.length];setTab(other);$('tab-'+other).focus();}});
 function setDrawer(open){$('command-drawer').dataset.open=String(open);$('drawer-toggle').setAttribute('aria-expanded',String(open));$('drawer-content').hidden=!open;requestAnimationFrame(draw);}
 $('drawer-toggle').onclick=()=>setDrawer($('command-drawer').dataset.open!=='true');
 let drawerY=null,drawerSwiped=false;
 $('drawer-toggle').addEventListener('pointerdown',e=>{drawerY=e.clientY;drawerSwiped=false;$('drawer-toggle').setPointerCapture(e.pointerId);});
 $('drawer-toggle').addEventListener('pointerup',e=>{if(drawerY!==null&&Math.abs(e.clientY-drawerY)>22){drawerSwiped=true;setDrawer(e.clientY<drawerY);}drawerY=null;});
 $('drawer-toggle').addEventListener('click',e=>{if(drawerSwiped){e.stopImmediatePropagation();drawerSwiped=false;}},true);
 $('drawer-toggle').addEventListener('pointercancel',()=>{drawerY=null;drawerSwiped=false;});
 document.addEventListener('keydown',e=>{if(preview&&e.key==='Tab'){const controls=[...$('preview').querySelectorAll('input,button')].filter(x=>!x.disabled),current=controls.indexOf(document.activeElement),next=e.shiftKey?(current<=0?controls.length-1:current-1):(current+1)%controls.length;e.preventDefault();controls[next]?.focus();return;}if(e.key==='Escape'){cancelSidePanelDrags();if(preview){$('cancel-level').click();}else if(sidePanelOrder.length){setSidePanel(sidePanelOrder.at(-1),false,true);}else if($('command-drawer').dataset.open==='true'){setDrawer(false);$('drawer-toggle').focus();}else setTab('chart');}});
 // Reflow the SVG after drawer resizing and device rotation without affecting its data.
 if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>requestAnimationFrame(draw)).observe($('chart-workspace'));
 layout();window.addEventListener('blur',()=>{cancelSidePanelDrags();cancelGesture();draw();});window.addEventListener('resize',()=>{cancelSidePanelDrags();cancelGesture();requestAnimationFrame(draw);});
 setInterval(poll,1000);poll();
})();
