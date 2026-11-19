export function gate(result, acknowledged, operatorEnabled, hasLocks, pending) {
 const w=result?.widget,cap=w?.capabilities;
 const armed=result?.webTrading===true&&w?.mode==='native'&&w?.snapshot?.status==='fresh'&&typeof w.snapshot.context_id==='string';
 const enabled=armed&&acknowledged===true&&operatorEnabled===true&&hasLocks===true&&!pending;
 const flat=Array.isArray(w?.positions)&&w.positions.length===0;
 return {armed,enabled,ranges:enabled&&flat&&!w.reconciliation_required&&cap?.set_ranges===true,
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
