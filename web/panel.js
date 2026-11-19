(() => {
  'use strict';
  const M = window.RecoveryModel, $ = id => document.getElementById(id);
  let key = '', busy = false, timer = null, controller = null, epoch = 0, context = null, lastResult = null, actionBusy = false;
  const text = (id, value) => { $(id).textContent = value; };
  const node = (tag, value, cls) => { const e = document.createElement(tag); if (value !== undefined) e.textContent = value; if (cls) e.className = cls; return e; };
  const mode = { real:'Реальный счёт', demo:'Демо-счёт', contest:'Конкурсный счёт', unknown:'Тип счёта не подтверждён' };
  const secureOrigin = () => location.protocol === 'https:' || (location.protocol === 'http:' && ['localhost','127.0.0.1','[::1]'].includes(location.hostname));
  text('origin', location.protocol === 'file:' ? 'Откройте панель на вашем VPS через HTTPS. Локальный файл показывает только интерфейс.' : 'Сервер: ' + location.host);
  function controls() { $('refresh').disabled = !key || busy; $('logout').disabled = !key; $('auto').disabled = !key; $('lookup-button').disabled = !key || busy; $('connect').disabled = busy || actionBusy; $('dashboard').setAttribute('aria-busy', String(busy)); }
  function stopTimer() { clearTimeout(timer); timer = null; }
  function schedule() { stopTimer(); if (key && $('auto').checked && !document.hidden) timer = setTimeout(loadState, 5000); }
  function unavailable(message) { text('message',message); $('dashboard').classList.add('stale'); text('snapshot','Данные не подтверждены сейчас. Любые показанные значения относятся к прежнему снимку.'); text('executor','Не подтверждено'); }
  function clearData() { context=null;lastResult=null;window.dispatchEvent(new Event('recovery-session-change')); for (const name of ['positions','orders','ranges']) { $(name).replaceChildren(node('p','Данные не подтверждены','empty')); } for (const name of ['positions-count','orders-count','symbol','account']) text(name,'—'); text('executor','Нет данных'); text('snapshot','Снимок ещё не получен.'); text('timing',''); $('issues').replaceChildren(); $('settings').replaceChildren(); $('receipt-result').replaceChildren(); }
  function logout() { ++epoch; key=''; if(controller)controller.abort(); stopTimer(); busy=false; $('key').value=''; $('key').type='password'; text('reveal','Показать'); $('reveal').setAttribute('aria-pressed','false'); $('auto').checked=false; $('request-id').value=''; clearData(); text('message','Ключ удалён из памяти вкладки. VPS продолжает работу.'); text('session-label','Ключ не сохранён'); controls(); }
  async function read(kind, id) {
    if (!secureOrigin()) throw new Error('Для соединения откройте эту панель через HTTPS на вашем VPS.');
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes); const nonce = [...bytes].map(x=>x.toString(16).padStart(2,'0')).join('');
    const req = M.spec(kind,id,nonce,key); const requestController = new AbortController(); controller=requestController;
    const timeout = setTimeout(()=>requestController.abort(),15000);
    try {
      const response = await fetch(req.path,{...req.options,signal:requestController.signal});
      if (response.status===401 || response.status===403) throw new Error('Доступ отклонён. Проверьте ключ доступа.');
      if (!response.ok) throw new Error('VPS вернул ошибку HTTP '+response.status+'. Этот запрос был только чтением.');
      if (!response.headers.get('content-type')?.toLowerCase().includes('application/json')) throw new Error('VPS вернул не JSON. Проверьте адрес панели и настройку прокси.');
      const reader=response.body.getReader(), chunks=[];let size=0;
      while(true){const part=await reader.read();if(part.done)break;size+=part.value.length;if(size>1000000){await reader.cancel();throw new Error('Ответ VPS слишком большой.');}chunks.push(part.value);}
      const all=new Uint8Array(size);let offset=0;for(const chunk of chunks){all.set(chunk,offset);offset+=chunk.length;}
      const data=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(all));
      return kind==='state'?M.state(data,nonce):M.receipt(data,id);
    } finally {clearTimeout(timeout);if(controller===requestController)controller=null;}
  }
  function fields(container, pairs) {const dl=node('dl');for(const [label,value] of pairs){dl.append(node('dt',label),node('dd',M.text(value)));}container.append(dl);}
  function inventory(name, rows) {
    text(name+'-count',rows===null?'—':String(rows.length));const box=$(name);box.replaceChildren();
    if(rows===null){box.append(node('p','Данные не подтверждены. Это не означает отсутствие позиций или ордеров.','empty'));return;}
    if(!rows.length){box.append(node('p',name==='positions'?'В этом снимке открытых позиций нет.':'В этом снимке триггерных ордеров нет.','empty'));return;}
    for(const r of rows.slice(0,100)){const card=node('article',undefined,'item'),head=node('div',undefined,'item-head');head.append(node('span',M.text(r.side),r.side==='BUY'?'buy':r.side==='SELL'?'sell':''),node('span','№ '+M.text(r.ticket)));card.append(head);fields(card,[['Диапазон',r.range_id],['Объём, лоты',r.volume],[name==='positions'?'Вход':'Цена триггера',r.price],['SL',r.sl],['TP',r.tp]]);if(r.mapping_status!=='verified')card.append(node('p','Принадлежность диапазону не подтверждена.','warning'));box.append(card);}
    if(rows.length>100)box.append(node('p','Показаны первые 100 записей из '+rows.length+'.','small muted'));
  }
  function render(result) {
    lastResult=result;window.dispatchEvent(new CustomEvent('recovery-state',{detail:result}));const w=result.widget;context=w.snapshot.context_id; $('dashboard').classList.toggle('stale',w.snapshot.status!=='fresh');text('account',mode[w.account_trade_mode]);text('symbol',M.text(w.snapshot.symbol));
    text('executor',w.reconciliation_required?'Нужна сверка':w.executor_ready?'Готов':'Не готов');
    text('snapshot',w.snapshot.status==='fresh'?'Снимок MT5: '+M.text(w.snapshot.observed_at):'Снимок MT5 недоступен. Отсутствие позиций не подтверждено.');
    text('timing',result.age===null?'Возраст данных не предоставлен сервером.':'Возраст по данным сервера: '+result.age+' мс. Только диагностика.');
    $('issues').replaceChildren();for(const issue of w.issues.slice(0,30))$('issues').append(node('p',M.text(issue),'warning'));
    inventory('positions',w.positions);inventory('orders',w.orders);$('ranges').replaceChildren();
    if(!w.ranges.length)$('ranges').append(node('p','Диапазоны не настроены.','empty'));
    const lifecycle={active:'Активный',cancelled:'Отменён',manual:'Ручной режим'};
    for(const r of w.ranges.slice(0,10)){const card=node('article',undefined,'item'),head=node('div',undefined,'item-head');head.append(node('span','Диапазон №'+M.text(r.id)),node('span',M.text(r.side),r.side==='BUY'?'buy':r.side==='SELL'?'sell':''));card.append(head);const t=w.totals[r.id];fields(card,[['Первичная цена',r.price],['Начальный объём',r.volume],['Режим',Object.hasOwn(lifecycle,r.lifecycle)?lifecycle[r.lifecycle]:r.lifecycle],['Убытки закрытых позиций',t?.gross_negative_closed_net],['Результат',t?.realized_net],['Полных SL',t?.full_sl_count]]);$('ranges').append(card);}
    $('settings').replaceChildren();for(const [label,value] of [['Исходный SL',w.settings.initial_sl_distance],['После SL поддержки',w.settings.reentry_distance]])$('settings').append(node('dt',label),node('dd',M.text(value)));
    text('message','Состояние прочитано. Этот запрос не изменял ордера.');
  }
  async function loadState() {if(busy||!key)return;stopTimer();busy=true;const turn=epoch;controls();text('message','Читаю состояние VPS…');try{const result=await read('state');if(turn===epoch)render(result);}catch(e){if(turn===epoch)unavailable(e.name==='AbortError'?'Время ожидания истекло. Данные не подтверждены.':e instanceof SyntaxError?'Некорректный ответ VPS. Данные не подтверждены.':e.message);}finally{if(turn===epoch){busy=false;controls();schedule();}}}
  $('login').addEventListener('submit',e=>{e.preventDefault();if(busy||actionBusy)return;const candidate=$('key').value.trim();try{M.spec('state',null,'0'.repeat(32),candidate);}catch(err){text('message',err.message);return;}++epoch;key=candidate;$('key').value='';$('key').type='password';text('reveal','Показать');$('reveal').setAttribute('aria-pressed','false');clearData();text('session-label','Ключ только в памяти');controls();loadState();});
  $('reveal').addEventListener('click',()=>{const show=$('key').type==='password';$('key').type=show?'text':'password';text('reveal',show?'Скрыть':'Показать');$('reveal').setAttribute('aria-pressed',String(show));});
  $('refresh').addEventListener('click',loadState);$('logout').addEventListener('click',logout);$('auto').addEventListener('change',schedule);
  $('lookup').addEventListener('submit',async e=>{e.preventDefault();if(busy||!key)return;const id=$('request-id').value.trim().toLowerCase();if(!M.uuid.test(id)){text('receipt-result','Введите существующий request_id в формате UUID.');return;}busy=true;stopTimer();const turn=epoch;controls();text('receipt-result','Читаю запись…');try{const result=await read('receipt',id);if(turn!==epoch)return;$('receipt-result').replaceChildren(node('p',result.message,'warning'));if(context!==null&&result.context!==context)$('receipt-result').append(node('p','Контекст счёта изменился. Обновите состояние; не связывайте эту запись с прежним снимком.','warning'));const labels={confirmed:'первичная отправка подтверждена',not_dispatched:'не отправлен',rejected:'отклонён'};for(const r of result.outcomes.slice(0,100))$('receipt-result').append(node('p','Диапазон №'+M.text(r.range_id)+': '+(Object.hasOwn(labels,r.status)?labels[r.status]:'статус не подтверждён')));$('receipt-result').append(node('p','Запись только прочитана. Состояние прежней команды в Android не изменено.','small muted'));}catch(err){if(turn===epoch)text('receipt-result','Не удалось подтвердить запись. Ничего не повторялось.');}finally{if(turn===epoch){busy=false;controls();schedule();}}});
  function selectTab(name, focus=false) {
    const ranges=name==='ranges'; $('market').hidden=ranges; $('ranges-view').hidden=!ranges;
    for(const [id,selected] of [['tab-overview',!ranges],['tab-ranges',ranges]]) {$(id).setAttribute('aria-selected',String(selected));$(id).tabIndex=selected?0:-1;if(selected&&focus)$(id).focus();}
  }
  document.querySelectorAll('[data-tab]').forEach(button=>{button.addEventListener('click',()=>selectTab(button.dataset.tab));button.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();selectTab(event.key==='Home'?'overview':event.key==='End'?'ranges':button.dataset.tab==='overview'?'ranges':'overview',true);}});});
  document.querySelector('a[href="#settings-section"]').addEventListener('click',()=>selectTab('ranges'));
  document.querySelector('a[href="#market"]').addEventListener('click',()=>selectTab('overview'));
  window.RecoveryView=Object.freeze({getToken:async()=>key,getState:()=>lastResult,setActionBusy:value=>{actionBusy=value===true;controls();},refresh:loadState});
  window.dispatchEvent(new Event('recovery-ready'));
  document.addEventListener('visibilitychange'  ,()=>{if(document.hidden)stopTimer();else schedule();});window.addEventListener('pagehide',logout);controls();
})();
