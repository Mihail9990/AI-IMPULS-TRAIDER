export function lotScheduleInput(values){
 if(!Array.isArray(values)||values.length!==24)throw new Error('Нужны все 24 значения шкалы.');
 return values.map((raw,index)=>{
  const text=typeof raw==='string'?raw.trim().replace(',','.'):'';
  if(!/^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?$/.test(text)||!/[1-9]/.test(text))throw new Error('Проверьте лот после '+index+' полных SL.');
  const [whole,fraction='']=text.split('.');
  const value=whole+'.'+fraction.replace(/0+$/,'').padEnd(2,'0');
  if(index===0&&value!=='0.01')throw new Error('Первый вход остаётся 0,01 лота.');
  return value;
 });
}
export function lotSchedule(widget){
 const values=widget?.lot_schedule;
 try{lotScheduleInput(values);return values.every(v=>typeof v==='string'&&/^[0-9]+(?:\.[0-9]+)?$/.test(v))?[...values]:null;}catch{return null;}
}
export function gate(result, acknowledged, operatorEnabled, hasLocks, pending) {
 const w=result?.widget,cap=w?.capabilities;
 const armed=result?.webTrading===true&&w?.mode==='native'&&w?.snapshot?.status==='fresh'&&typeof w.snapshot.context_id==='string';
 const enabled=armed&&acknowledged===true&&operatorEnabled===true&&hasLocks===true&&!pending;
 const flat=Array.isArray(w?.positions)&&w.positions.length===0;
 return {armed,enabled,lotSchedule:enabled&&flat&&w.recovery_cycle_active!==true&&!w.reconciliation_required&&cap?.set_lot_schedule===true&&lotSchedule(w)!==null,ranges:enabled&&flat&&!w.reconciliation_required&&cap?.set_ranges===true,
 cancel:enabled&&flat&&w.executor_ready===true&&!w.reconciliation_required&&cap?.cancel_ranges===true,
 close1:enabled&&w.executor_ready===true&&!w.reconciliation_required&&cap?.close_cancel_range_ids?.includes('1')===true,
 close2:enabled&&w.executor_ready===true&&!w.reconciliation_required&&cap?.close_cancel_range_ids?.includes('2')===true};
}
export function distance(value){const s=String(value).trim().replace(',','.');if(!/^\d+(\.\d+)?$/.test(s)||!Number.isFinite(Number(s))||Number(s)<=0)throw new Error('Укажите положительное число без экспоненты.');return Number(s);}
export function rangeInput(count,fields){
 if(count!=='1'&&count!=='2')throw new Error('Выберите 1 или 2 диапазона.');
 const sl=distance(fields.sl),reentry=distance(fields.reentry);if(sl<0.60)throw new Error('Исходный SL не может быть меньше 0,60.');
 return {initial_sl_distance:sl,reentry_distance:reentry,ranges:Array.from({length:Number(count)},(_,i)=>{
  const side=fields.sides[i];if(!['BUY','SELL'].includes(side))throw new Error('Выберите BUY или SELL.');
  return {id:String(i+1),side,price:distance(fields.prices[i]),volume:0.01};
 })};
}
