"""Original bounded OHLC geometry heuristics, without third-party pattern code.

Experimental candidates, not breakout confirmations or trading instructions.
Each pivot requires three strictly lower highs/higher lows on BOTH sides; only
closed bars can confirm it. known_at is observation in this snapshot, never an
assertion of earliest historical availability. No future extrapolation.
"""
import math

SECONDS = {'M1': 60, 'M15': 900, 'M30': 1800, 'H1': 3600}
MAX_BARS = 300
MAX_FIGURES = 12
RADIUS = 3
NOTES = [
    'Собственная геометрия: экспериментальные кандидаты, не подтверждённые пробои и не торговые рекомендации.',
    'Экстремумы требуют по 3 закрытые свечи с каждой стороны. Допуски эвристические; фигуры могут пересматриваться.',
    'Фигуры старше 100 свечей или с последующим закрытием за внешней границей удаляются; пробой не подтверждается. До 300 свечей и 12 фигур. known_at означает обнаружение в текущем снимке, не первую историческую доступность.',
]


def _numeric(value):
    if isinstance(value, bool):
        raise ValueError('boolean')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('nonfinite')
    return value


def _prepare(bars, timeframe, as_of):
    seconds = SECONDS[timeframe]
    now = _numeric(as_of)
    if now <= 0 or not isinstance(bars, (list, tuple)) or len(bars) > 10000:
        raise ValueError('invalid input')
    rows, last = [], -1
    for bar in bars:
        t = _numeric(bar['utc_time'])
        prices = {k: _numeric(bar[k]) for k in ('open', 'high', 'low', 'close')}
        if t <= last or t <= 0 or not t.is_integer():
            raise ValueError('invalid time')
        last = t
        if min(prices.values()) <= 0 or prices['high'] < max(prices.values()) or prices['low'] > min(prices.values()):
            raise ValueError('invalid prices')
        if t + seconds <= now:
            rows.append(dict(prices, utc_time=int(t)))
    return rows[-MAX_BARS:], now


def _pivots(rows):
    result = []
    for i in range(RADIUS, len(rows)-RADIUS):
        neighbours = rows[i-RADIUS:i] + rows[i+1:i+RADIUS+1]
        high = all(rows[i]['high'] > r['high'] for r in neighbours)
        low = all(rows[i]['low'] < r['low'] for r in neighbours)
        # An outside bar with both extremes has no defensible intrabar ordering.
        if high == low:
            continue
        kind, price = ('H', rows[i]['high']) if high else ('L', rows[i]['low'])
        pivot = (i, kind, price)
        if result and result[-1][1] == kind:
            if (price > result[-1][2]) if high else (price < result[-1][2]):
                result[-1] = pivot
        else:
            result.append(pivot)
    return result


def _figure(kind, label, pivots, rows, now, seconds, points=None):
    coords = [{'time': rows[p[0]]['utc_time'], 'price': p[2]} for p in (points or pivots)]
    return {'id': f"geometry:{kind}:{rows[pivots[0][0]]['utc_time']}:{rows[pivots[-1][0]]['utc_time']}",
            'kind': 'figure_' + kind, 'label': label + ' · экспериментально',
            'direction': 'neutral', 'status': 'EXPERIMENTAL_CANDIDATE',
            'start_time': rows[pivots[0][0]]['utc_time'], 'end_time': rows[pivots[-1][0]]['utc_time'],
            'price': pivots[-1][2], 'top': None, 'bottom': None, 'points': coords,
            'formed_at': rows[pivots[-1][0]]['utc_time'], 'known_at': now,
            'confirmed_through': rows[pivots[-1][0]+RADIUS]['utc_time'] + seconds}


def _detect(rows, now, seconds):
    pivots = _pivots(rows)
    found = []
    for end in range(3, len(pivots)+1):
        triple = pivots[end-3:end]
        a, b, c = triple
        depth = abs((a[2]+c[2])/2-b[2])
        local = rows[a[0]:c[0]+1]
        span = max(r['high'] for r in local)-min(r['low'] for r in local)
        spacing = (b[0]-a[0])/(c[0]-b[0])
        if span > 0 and depth >= span*.55 and abs(a[2]-c[2]) <= depth*.12 and .4 <= spacing <= 2.5:
            top = a[1] == 'H'
            found.append(_figure('double_top' if top else 'double_bottom', 'Двойная вершина' if top else 'Двойное дно', triple, rows, now, seconds))
        if end >= 5:
            five = pivots[end-5:end]
            a, b, c, d, e = five
            sign = 1 if a[1] == 'H' else -1
            shoulders = (a[2]+e[2])/2
            neck = (b[2]+d[2])/2
            height = sign*(shoulders-neck)
            head = sign*(c[2]-shoulders)
            spacing = (c[0]-a[0])/(e[0]-c[0])
            if height > 0 and head >= height*.25 and head <= height*2 and abs(a[2]-e[2]) <= height*.2 and abs(b[2]-d[2]) <= height*.25 and .4 <= spacing <= 2.5:
                found.append(_figure('head_shoulders' if sign == 1 else 'inverse_head_shoulders', 'Голова и плечи' if sign == 1 else 'Обратная голова и плечи', five, rows, now, seconds))
        if end >= 6:
            six = pivots[end-6:end]
            highs = [p for p in six if p[1] == 'H']
            lows = [p for p in six if p[1] == 'L']
            h0, hm, h1 = highs
            l0, lm, l1 = lows
            width = h0[2]-l0[2]
            if width <= 0:
                continue
            dh, dl = h1[2]-h0[2], l1[2]-l0[2]
            # Three touches per boundary; middle touch must fit its own line.
            def fitted(p, first, last):
                expected = first[2]+(last[2]-first[2])*(p[0]-first[0])/(last[0]-first[0])
                return abs(p[2]-expected) <= width*.1
            if not fitted(hm, h0, h1) or not fitted(lm, l0, l1):
                continue
            remaining = h1[2]-l1[2]
            if not width*.2 <= remaining <= width*.8:
                continue
            kind = None
            if abs(dh) <= width*.08 and dl >= width*.2:
                kind, label = 'ascending_triangle', 'Восходящий треугольник'
            elif abs(dl) <= width*.08 and dh <= -width*.2:
                kind, label = 'descending_triangle', 'Нисходящий треугольник'
            elif dh <= -width*.12 and dl >= width*.12:
                kind, label = 'symmetric_triangle', 'Симметричный треугольник'
            if kind:
                found.append(_figure(kind, label, six, rows, now, seconds, points=[h0, h1, l1, l0, h0]))
    retained = []
    by_time = {r['utc_time']: i for i, r in enumerate(rows)}
    for mark in found:
        index = by_time[mark['formed_at']]
        if len(rows)-1-index >= 100:
            continue
        prices = [p['price'] for p in mark['points']]
        low, high = min(prices), max(prices)
        later = rows[index+RADIUS+1:]
        kind = mark['kind']
        if kind in ('figure_double_top', 'figure_head_shoulders'):
            invalid = any(r['close'] > high for r in later)
        elif kind in ('figure_double_bottom', 'figure_inverse_head_shoulders'):
            invalid = any(r['close'] < low for r in later)
        else:
            invalid = any(r['close'] < low or r['close'] > high for r in later)
        if not invalid:
            retained.append(mark)
    return sorted(retained, key=lambda m: (m['formed_at'], m['id']))[-MAX_FIGURES:]


def analyze_patterns(bars, timeframe, as_of):
    """JSON-safe module result; invalid input fails closed, future bars excluded."""
    result = {'status': 'READY', 'overlays': [], 'notes': list(NOTES)}
    try:
        rows, now = _prepare(bars, timeframe, as_of)
        if len(rows) < RADIUS*2+3:
            result.update(status='WARMING_UP', reason='Недостаточно закрытых свечей для геометрии')
        else:
            result['overlays'] = _detect(rows, now, SECONDS[timeframe])
    except (ValueError, TypeError, KeyError, OverflowError):
        result.update(status='ERROR', reason='Некорректные OHLC или время для геометрии')
    return result
