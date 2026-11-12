'use strict';
// Run: node --test tests/test_analysis_scenario.js
const test = require('node:test');
const assert = require('node:assert/strict');
const {selectAnalysisScenario: select} = require('../web/basket-panel/analysis-scenario.js');
function snapshot() {
 const bars = Array.from({length:16},(_,i) => ({utc_time:600+i*60,open:100,high:102,low:98,close:100}));
 return {schema:'analysis_v1',source:'MT5',status:'READY',timeframe:'M1',timestamp_semantics:'VERIFIED',as_of:1565,closed_through:1560,bars,modules:{talib:{status:'READY'},geometry:{status:'READY'}},overlays:{candlestick:[],figures:[]}};
}
function candle(d, direction='bullish', label='ENGULFING') {
 const t=d.bars.at(-1).utc_time;
 const m={id:label+direction,kind:'candle_pattern',label,status:'SNAPSHOT',direction,formed_at:t,start_time:t,end_time:t,known_at:d.as_of,price:d.bars.at(-1).close};
 d.overlays.candlestick.push(m);return m;
}
function figure(d, bottom=false) {
 d.bars[3].high=110;d.bars[7].low=90;d.bars[11].high=110;
 d.bars[15].close=89;d.bars[15].low=88;
 if(bottom) for(const b of d.bars) {const h=b.high;b.high=200-b.low;b.low=200-h;b.open=200-b.open;b.close=200-b.close;}
 const p=[3,7,11].map((i,j)=>({time:d.bars[i].utc_time,price:d.bars[i][(j===1)!==bottom?'low':'high']}));
 const m={id:'figure',kind:bottom?'figure_double_bottom':'figure_double_top',direction:'neutral',status:'EXPERIMENTAL_CANDIDATE',points:p,start_time:p[0].time,end_time:p[2].time,formed_at:p[2].time,known_at:d.as_of,confirmed_through:d.bars[14].utc_time+60};
 d.overlays.figures.push(m);return m;
}
test('latest closed candle returns only conditional direction and anchor, no target',()=>{
 const d=snapshot();candle(d);const before=JSON.stringify(d);
 assert.deepEqual(select(d,{now:1570}),{direction:'bullish',time:1500,price:100,evidenceIds:['ENGULFINGbullish'],basis:'latest_closed_candle',conditional:true});
 assert.equal(JSON.stringify(d),before);
});
test('neutral, conflicting, historical and unknown candle evidence is suppressed',()=>{
 for(const mutate of [d=>candle(d,'neutral','DOJI'),d=>{candle(d);candle(d,'bearish');},d=>{const m=candle(d);m.formed_at-=60;},d=>candle(d,'bullish','UNKNOWN')]) {const d=snapshot();mutate(d);assert.equal(select(d),null);}
});
test('stale, invalid, future, unavailable and unknown-domain snapshots fail closed',()=>{
 for(const mutate of [d=>d.status='STALE',d=>d.as_of=1620,d=>d.closed_through=1500,d=>d.bars.at(-1).high=99,d=>d.bars.at(-1).utc_time=1560,d=>d.timestamp_semantics='UNKNOWN',d=>d.modules.talib.status='ERROR',d=>d.bars[1].utc_time=d.bars[0].utc_time,d=>d.bars[0].open=true]) {const d=snapshot();candle(d);mutate(d);assert.equal(select(d),null);}
 const d=snapshot();candle(d);assert.equal(select(d,{now:1620}),null);assert.equal(select(d,{now:1500}),null);
});
test('broker raw ignores UTC now but requires closed broker bars',()=>{
 const d=snapshot();candle(d);d.timestamp_semantics=d.time_domain='BROKER_RAW';
 for(const b of d.bars){b.broker_time=b.utc_time;b.utc_time=null;b.time_domain='BROKER_RAW';b.closed=true;}
 assert.equal(select(d,{now:9999999999}).direction,'bullish');
 d.bars[0].closed=false;assert.equal(select(d),null);
});
test('first closed neckline breakout after confirmed double top/bottom',()=>{
 for(const bottom of [false,true]) {const d=snapshot();figure(d,bottom);const before=JSON.stringify(d);const result=select(d,{group:'figures'});assert.equal(result?.direction,bottom?'bullish':'bearish');assert.equal(result.basis,'closed_neckline_break');assert.equal(JSON.stringify(d),before);}
});
test('candidate alone, wick touch, prior breakout, bad pivots and extrapolated triangles have no arrow',()=>{
 for(const mutate of [d=>d.bars.at(-1).close=100,d=>d.bars.at(-1).close=90,d=>{d.bars[14].close=89;d.bars[14].low=88;},d=>d.overlays.figures[0].confirmed_through-=60,d=>d.bars[3].high=109,d=>d.overlays.figures[0].kind='figure_symmetric_triangle',d=>d.modules.geometry.status='ERROR',d=>d.overlays.figures[0].points[0].price=null,d=>{d.bars[13].close=111;d.bars[13].high=112;}]) {const d=snapshot();figure(d);mutate(d);assert.equal(select(d,{group:'figures'}),null);}
});
test('no data and unsupported groups do not invent a direction',()=>{
 assert.equal(select(null),null);assert.equal(select(snapshot()),null);assert.equal(select(snapshot(),{group:'structure'}),null);
});
