'use strict';
/** Pure, display-only interpretation of a current analysis_v1 snapshot.
 * No prices are projected, no targets, orders, confidence or future timestamps.
 * group: 'candlestick' | 'figures'. now: optional UTC epoch seconds, used ONLY
 * for VERIFIED snapshots. RAW is last-observed broker time, not UTC freshness.
 * Returns null or {direction,time,price,evidenceIds,basis,conditional:true}.
 * The caller owns refreshing/clearing the display on connection loss.
 */
function selectAnalysisScenario(data, {group = 'candlestick', now} = {}) {
 const periods = {M1:60,M15:900,M30:1800,H1:3600};
 const numeric = v => (typeof v === 'number' || (typeof v === 'string' && v.trim() !== '')) && Number.isFinite(Number(v));
 const positive = v => numeric(v) && Number(v) > 0;
 const raw = data?.timestamp_semantics === 'BROKER_RAW' && data?.time_domain === 'BROKER_RAW';
 const utc = data?.timestamp_semantics === 'VERIFIED' && (!data.time_domain || data.time_domain === 'UTC');
 const period = periods[data?.timeframe], bars = data?.bars;
 if (data?.schema !== 'analysis_v1' || data.source !== 'MT5' || data.status !== 'READY' || (!raw && !utc) || !period || !Array.isArray(bars) || !bars.length || bars.length > 10000 || !positive(data.as_of)) return null;
 const time = b => Number(raw ? b?.broker_time ?? b?.raw_time : b?.utc_time);
 const times = [];
 for (const b of bars) {
  const t = time(b);
  if (!b || !Number.isInteger(t) || t <= 0 || t + period > Number(data.as_of) || (times.length && t <= times.at(-1)) ||
      (raw && (b.closed !== true || b.time_domain !== 'BROKER_RAW' || b.utc_time != null || (b.raw_time != null && Number(b.raw_time) !== t))) ||
      ['open','high','low','close'].some(k => !positive(b[k])) || Number(b.low) > Math.min(Number(b.open), Number(b.close)) || Number(b.high) < Math.max(Number(b.open), Number(b.close))) return null;
  times.push(t);
 }
 const last = bars.at(-1), stamp = times.at(-1), closeTime = stamp + period;
 if (!numeric(data.closed_through) || Number(data.closed_through) !== closeTime || Number(data.as_of) - closeTime >= period) return null;
 if (utc && now !== undefined && (!positive(now) || Number(now) < Number(data.as_of) || Number(now) - closeTime >= period)) return null;
 const index = new Map(times.map((t,i) => [t,i]));
 const observations = [];
 const validMark = m => m && typeof m.id === 'string' && m.id.length > 0 && positive(m.known_at) && Number(m.known_at) <= Number(data.as_of) && Number(m.known_at) >= closeTime && numeric(m.formed_at) && index.has(Number(m.formed_at));
 if (group === 'candlestick') {
  if (data.modules?.talib?.status !== 'READY' || !Array.isArray(data.overlays?.candlestick)) return null;
  // TA-Lib uses the final candle as availability for multi-candle patterns too.
  const labels = new Set(['ENGULFING','HAMMER','SHOOTINGSTAR','MORNINGSTAR','EVENINGSTAR']);
  for (const m of data.overlays.candlestick) {
   if (!validMark(m) || m.kind !== 'candle_pattern' || m.status !== 'SNAPSHOT' || !labels.has(m.label) || Number(m.formed_at) !== stamp || Number(m.start_time) !== stamp || Number(m.end_time) !== stamp || !positive(m.price) || Number(m.price) !== Number(last.close)) continue;
   if (m.direction === 'bullish' || m.direction === 'bearish') observations.push({id:m.id,direction:m.direction});
  }
 } else if (group === 'figures') {
  if (data.modules?.geometry?.status !== 'READY' || !Array.isArray(data.overlays?.figures)) return null;
  for (const m of data.overlays.figures) {
   // Do not turn a neutral candidate's nominal shape into a directional signal.
   // Only double tops/bottoms have an unambiguous horizontal neckline here.
   // Triangles and sloped necklines are intentionally unsupported: rendering
   // polylines alone does not certify their extrapolated breakout boundaries.
   if (!validMark(m) || m.status !== 'EXPERIMENTAL_CANDIDATE' || m.direction !== 'neutral' || !['figure_double_top','figure_double_bottom'].includes(m.kind) || !Array.isArray(m.points) || m.points.length !== 3) continue;
   const p = m.points;
   if (p.some(v => !v || !positive(v.price) || !numeric(v.time) || !index.has(Number(v.time)))) continue;
   const [a,b,c] = p.map(v => index.get(Number(v.time)));
   if (!(a >= 3 && a < b && b < c && c + 3 < bars.length - 1) || times[a] !== Number(m.start_time) || times[c] !== Number(m.end_time) || times[c] !== Number(m.formed_at) || bars.length - 1 - c >= 100 || Number(m.confirmed_through) !== times[c+3] + period) continue;
   // Require all underlying pivots to have the detector's three closed bars on
   // both sides. An outside bar with two extremes has ambiguous pivot ordering.
   const top = m.kind === 'figure_double_top';
   let valid = true;
   for (const [j,i] of [a,b,c].entries()) {
    const high = j === 1 ? !top : top;
    const neighbors = bars.slice(i-3,i).concat(bars.slice(i+1,i+4));
    const isHigh = neighbors.every(r => Number(bars[i].high) > Number(r.high));
    const isLow = neighbors.every(r => Number(bars[i].low) < Number(r.low));
    if (isHigh === isLow || (high ? !isHigh : !isLow) || Number(p[j].price) !== Number(bars[i][high ? 'high' : 'low'])) valid = false;
   }
   const [pa,pb,pc] = p.map(v => Number(v.price)), depth = Math.abs((pa+pc)/2-pb);
   const local = bars.slice(a,c+1), span = Math.max(...local.map(r => Number(r.high))) - Math.min(...local.map(r => Number(r.low)));
   const spacing = (b-a)/(c-b);
   if (!valid || span <= 0 || depth < span*.55 || Math.abs(pa-pc) > depth*.12 || spacing < .4 || spacing > 2.5 || (top ? Math.min(pa,pc) <= pb : Math.max(pa,pc) >= pb)) continue;
   const beyond = price => top ? price < pb : price > pb;
   const invalidated = price => top ? price > Math.max(pa,pc) : price < Math.min(pa,pc);
   // A breakout during right-hand confirmation cannot be treated as one that
   // happened after a known figure. Require first close beyond the neck now;
   // reject prior breaks/retests, opposite invalidation and missing bar gaps.
   if (times.slice(c).some((t,i,ts) => i && t-ts[i-1] !== period) ||
       bars.slice(c+1,-1).some(r => beyond(Number(r.close))) ||
       bars.slice(c+1).some(r => invalidated(Number(r.close))) || !beyond(Number(last.close))) continue;
   observations.push({id:m.id,direction:top ? 'bearish' : 'bullish'});
  }
 } else return null;
 const directions = new Set(observations.map(v => v.direction));
 if (directions.size !== 1) return null;
 return {direction:observations[0].direction,time:stamp,price:Number(last.close),evidenceIds:[...new Set(observations.map(v => v.id))],basis:group === 'figures' ? 'closed_neckline_break' : 'latest_closed_candle',conditional:true};
}
if (typeof module !== 'undefined' && module.exports) module.exports = {selectAnalysisScenario};
