"""Read-only, optional snapshot analytics. Never imports broker/trading code.

All historical coordinates are distinguished from availability: known_at is the
snapshot timestamp, not the original pivot/candle. No installation or plotting.
"""
from datetime import datetime
import importlib
import importlib.metadata
import math
import re

TIMEFRAME_SECONDS = {"M1": 60, "M15": 900, "M30": 1800, "H1": 3600}
MAX_BARS = 300
MAX_MARKS = 80
SWING_LENGTH = 5


def _number(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("nonfinite value")
    return value


def _time(value, integral=True):
    if isinstance(value, str) and not value.replace('.', '', 1).isdigit():
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError("UTC timestamp requires timezone")
        value = dt.timestamp()
    value = _number(value)
    if value < 0 or (integral and value != int(value)):
        raise ValueError("invalid epoch seconds")
    return int(value) if integral else value


def _coordinate(row):
    return row['broker_time'] if row.get('time_domain') == 'BROKER_RAW' else row['utc_time']


def _prepare(bars, timeframe, as_of):
    seconds = TIMEFRAME_SECONDS[timeframe]
    now = _time(as_of, integral=False)
    if not isinstance(bars, (list, tuple)) or len(bars) > 10000:
        raise ValueError("invalid bars")
    clean, last = [], -1
    for bar in bars:
        stamp = _time(_coordinate(bar))
        if stamp <= last:
            raise ValueError("unordered bars")
        last = stamp
        prices = {key: _number(bar[key]) for key in ('open', 'high', 'low', 'close')}
        if (min(prices.values()) <= 0 or prices['high'] < max(prices.values())
                or prices['low'] > min(prices.values())):
            raise ValueError("invalid OHLC")
        row = dict(prices, **({'broker_time': stamp, 'time_domain': 'BROKER_RAW', 'closed': bar.get('closed')} if bar.get('time_domain') == 'BROKER_RAW' else {'utc_time': stamp}))
        for key in ('tick_volume', 'real_volume'):
            value = bar.get(key)
            row[key] = None if value is None else _number(value)
            if row[key] is not None and row[key] < 0:
                raise ValueError("negative volume")
        # Open and future candles are excluded even if the upstream did not.
        if stamp + seconds <= now and (bar.get('time_domain') != 'BROKER_RAW' or bar.get('closed') is True):
            clean.append(row)
    return clean[-MAX_BARS:], now


def _load(name):
    """Single seam for offline injected engines; imports cached by Python worker."""
    return importlib.import_module(name)


def _version(module, distribution):
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = getattr(module, '__version__', None)
    return str(version)[:60] if version and re.fullmatch(r'[\w.+-]{1,60}', str(version)) else None


def _mark(kind, label, direction, index, rows, now, **extra):
    stamp = _coordinate(rows[index])
    return dict(id=f'{kind}:{stamp}:{label}', kind=kind, label=label,
                direction=direction, start_time=stamp, end_time=stamp,
                price=None, top=None, bottom=None, points=[], formed_at=stamp,
                known_at=now, status='SNAPSHOT', **extra)


def _volume_source(rows):
    # Real volume reported as all zero usually means the instrument lacks it.
    for key in ('real_volume', 'tick_volume'):
        if all(r[key] is not None for r in rows) and any(r[key] > 0 for r in rows):
            return key
    return None


def _smc_swings(engine, frame, rows, now, pd):
    swings = engine.swing_highs_lows(frame, swing_length=SWING_LENGTH).copy()
    if len(swings) != len(rows):
        raise ValueError('unexpected swing length')
    marks = []
    # SMC forces boundary pivots and revises recent/consecutive extrema. Strict
    # fully enclosed pivots only; all remain snapshot, never historic signals.
    for i in range(len(rows)):
        direction = swings.iloc[i]['HighLow']
        valid = SWING_LENGTH <= i < len(rows) - SWING_LENGTH
        if pd.isna(direction) or direction not in (-1, 1):
            valid = False
        if valid:
            key = 'high' if direction == 1 else 'low'
            neighbours = rows[i-SWING_LENGTH:i] + rows[i+1:i+SWING_LENGTH+1]
            valid = all(rows[i][key] > r[key] if direction == 1 else rows[i][key] < r[key] for r in neighbours)
        if not valid:
            swings.loc[swings.index[i], ['HighLow', 'Level']] = float('nan')
            continue
        price = rows[i]['high' if direction == 1 else 'low']
        swings.loc[swings.index[i], 'Level'] = price
        mark = _mark('swing', 'Swing high' if direction == 1 else 'Swing low', 'bearish' if direction == 1 else 'bullish', i, rows, now)
        mark['price'] = price
        marks.append(mark)
    return marks, swings


def _smc_fvg(engine, frame, rows, now, pd):
    gaps = engine.fvg(frame, join_consecutive=False)
    if len(gaps) != len(rows):
        raise ValueError('unexpected FVG length')
    marks = []
    for i in range(1, len(rows)-1):
        gap = gaps.iloc[i]
        if pd.isna(gap['FVG']) or gap['FVG'] not in (-1, 1):
            continue
        top, bottom = _number(gap['Top']), _number(gap['Bottom'])
        if top <= bottom:
            continue
        bullish = gap['FVG'] == 1
        if not (rows[i-1]['high'] < rows[i+1]['low'] if bullish else rows[i-1]['low'] > rows[i+1]['high']):
            continue
        expected_top = rows[i+1]['low'] if bullish else rows[i-1]['low']
        expected_bottom = rows[i-1]['high'] if bullish else rows[i+1]['high']
        # Accept at most float32 rounding, then draw the actual OHLC levels.
        if (not math.isclose(top, expected_top, rel_tol=2**-24, abs_tol=0)
                or not math.isclose(bottom, expected_bottom, rel_tol=2**-24, abs_tol=0)
                or not (rows[i]['close'] > rows[i]['open'] if bullish else rows[i]['close'] < rows[i]['open'])):
            continue
        top, bottom = expected_top, expected_bottom
        mark = _mark('fvg', 'FVG (snapshot)', 'bullish' if bullish else 'bearish', i, rows, now)
        mark.update(top=top, bottom=bottom, end_time=_coordinate(rows[i+1]))
        marks.append(mark)
    return marks


def _smc_breaks(engine, frame, swings, rows, now, pd):
    breaks = engine.bos_choch(frame, swings, close_break=True)
    if len(breaks) != len(rows):
        raise ValueError('unexpected break length')
    marks = []
    for i in range(len(rows)):
        item = breaks.iloc[i]
        # A break must originate at a genuine filtered swing, not an injected
        # boundary extreme, and terminate at an actually closed later candle.
        if pd.isna(swings.iloc[i]['HighLow']):
            continue
        for kind in ('BOS', 'CHOCH'):
            direction = item[kind]
            if pd.isna(direction) or direction not in (-1, 1):
                continue
            try:
                level, broken = _number(item['Level']), _number(item['BrokenIndex'])
            except (ValueError, TypeError, OverflowError):
                continue
            if level <= 0 or broken != int(broken) or not i < broken < len(rows):
                continue
            # SMC 0.0.27 stores at last_positions[-2], whose level is
            # level_order[-3] after adding the fourth pivot: the origin pivot.
            expected_level = _number(swings.iloc[i]['Level'])
            if not math.isclose(level, expected_level, rel_tol=2**-24, abs_tol=0):
                continue
            level = expected_level
            end = int(broken)
            if not (rows[end]['close'] > level if direction == 1 else rows[end]['close'] < level):
                continue
            mark = _mark(kind.lower(), ('CHoCH' if kind == 'CHOCH' else kind) + ' (snapshot)', 'bullish' if direction == 1 else 'bearish', i, rows, now)
            mark.update(price=level, end_time=_coordinate(rows[end]), points=[
                {'time': _coordinate(rows[i]), 'price': level},
                {'time': _coordinate(rows[end]), 'price': level}])
            marks.append(mark)
    return marks


LIQUIDITY_RANGE_PERCENT = 0.01


def _smc_liquidity(engine, frame, swings, rows, now, pd):
    """Validate vendor clusters against confirmed OHLC pivots, never order flow.

    The threshold is 1% of this bounded snapshot's full high-low range, NOT 1%
    of the price. Vendor float32 means require tolerance only for average Level;
    geometric inclusion and the earliest sweep index are checked exactly.
    """
    liquidity = engine.liquidity(frame, swings.copy(), range_percent=LIQUIDITY_RANGE_PERCENT)
    if len(liquidity) != len(rows):
        raise ValueError('unexpected liquidity length')
    width = (max(r['high'] for r in rows) - min(r['low'] for r in rows)) * LIQUIDITY_RANGE_PERCENT
    if not math.isfinite(width) or width <= 0:
        return []
    marks, consumed = [], set()
    for i in range(len(rows)):
        item = liquidity.iloc[i]
        try:
            direction = _number(item['Liquidity'])
            level, end, swept = (_number(item[k]) for k in ('Level', 'End', 'Swept'))
        except (TypeError, ValueError, OverflowError):
            continue
        if (direction not in (-1, 1) or i in consumed or pd.isna(swings.iloc[i]['HighLow'])
                or swings.iloc[i]['HighLow'] != direction or level <= 0
                or end != int(end) or not i < end < len(rows) - SWING_LENGTH
                or swept != int(swept) or swept < 0 or swept >= len(rows)):
            continue
        end, swept = int(end), int(swept)
        key = 'high' if direction == 1 else 'low'
        origin = rows[i][key]
        bottom, top = origin - width, origin + width
        if not math.isfinite(bottom) or not math.isfinite(top) or bottom <= 0 or top <= bottom:
            continue
        actual_sweep = next((j for j in range(i+1, len(rows))
                             if (rows[j]['high'] >= top if direction == 1 else rows[j]['low'] <= bottom)), 0)
        if swept != actual_sweep:
            continue
        candidates = [j for j in range(i, swept or len(rows))
                      if j not in consumed and swings.iloc[j]['HighLow'] == direction
                      and bottom <= rows[j][key] <= top]
        if len(candidates) < 2 or candidates[-1] != end:
            continue
        expected = sum(rows[j][key] for j in candidates) / len(candidates)
        if not math.isclose(level, expected, rel_tol=2e-7, abs_tol=1e-12):
            continue
        consumed.update(candidates[1:])
        # A sweep before the final pivot's right-hand confirmation could not
        # have acted on a confirmed zone. Omit these ambiguous historical cases.
        if swept and swept <= end + SWING_LENGTH:
            continue
        side = 'buy_side' if direction == 1 else 'sell_side'
        label = 'Ликвидность над максимумами' if direction == 1 else 'Ликвидность под минимумами'
        zone = _mark('liquidity_zone', label + ' (OHLC-оценка)', 'neutral', i, rows, now)
        zone.update(price=level, top=top, bottom=bottom,
                    end_time=_coordinate(rows[swept or len(rows)-1]), side=side,
                    estimate_basis='OHLC', threshold_range_percent=LIQUIDITY_RANGE_PERCENT,
                    threshold_price=width, zone_state='SWEPT' if swept else 'UNSWEPT',
                    last_pivot_time=_coordinate(rows[end]),
                    confirmation_bar_time=_coordinate(rows[end+SWING_LENGTH]),
                    pivot_count=len(candidates))
        marks.append(zone)
        if swept:
            sweep = _mark('liquidity_sweep', 'Пересечение зоны (OHLC-оценка)', 'neutral', swept, rows, now)
            sweep.update(id=f'liquidity_sweep:{side}:{_coordinate(rows[i])}:{_coordinate(rows[swept])}',
                         price=rows[swept][key], side=side, estimate_basis='OHLC',
                         zone_id=zone['id'])
            marks.append(sweep)
    return marks


def _smc(package, rows, now):
    pd = _load('pandas')
    engine = package.smc
    frame = pd.DataFrame([{k: r[k] for k in ('open', 'high', 'low', 'close')} for r in rows])
    source = _volume_source(rows)
    if source:
        frame['volume'] = [r[source] for r in rows]
    marks, components, swings = [], {}, None
    for name in ('swings', 'fvg', 'bos_choch', 'liquidity'):
        try:
            if name == 'swings':
                found, swings = _smc_swings(engine, frame, rows, now, pd)
            elif name == 'fvg':
                found = _smc_fvg(engine, frame, rows, now, pd)
            elif swings is None:
                components[name] = {'status': 'ERROR', 'reason': 'SWINGS_UNAVAILABLE'}
                continue
            elif name == 'bos_choch':
                found = _smc_breaks(engine, frame, swings, rows, now, pd)
            else:
                found = _smc_liquidity(engine, frame, swings, rows, now, pd)
            marks.extend(found)
            components[name] = {'status': 'READY', 'count': len(found)}
        except AttributeError:
            components[name] = {'status': 'UNAVAILABLE', 'reason': 'COMPONENT_UNAVAILABLE'}
        except Exception:
            components[name] = {'status': 'ERROR', 'reason': 'COMPONENT_FAILED'}
    # Reserve space for still-relevant zones even when their first pivot is
    # older than many recent candles. Keep linked sweeps only for shown zones.
    zones = sorted((m for m in marks if m['kind'] == 'liquidity_zone'),
                   key=lambda m: (m['end_time'], m['formed_at'], m['id']))[-12:]
    zone_ids = {m['id'] for m in zones}
    sweeps = sorted((m for m in marks if m['kind'] == 'liquidity_sweep' and m['zone_id'] in zone_ids),
                    key=lambda m: (m['formed_at'], m['id']))[-8:]
    other = sorted((m for m in marks if m['kind'] not in ('liquidity_zone', 'liquidity_sweep')),
                   key=lambda m: (m['formed_at'], m['id']))[-(MAX_MARKS-len(zones)-len(sweeps)):]
    return sorted(other + zones + sweeps, key=lambda m: (m['formed_at'], m['id'])), source, components


def analyze_bars(bars, timeframe, as_of):
    """Return JSON-safe analytics; optional dependencies fail independently.

    Numeric strings are accepted. Invalid OHLC/time/volume fails the whole input
    closed; valid incomplete/future bars are discarded. No order book inference,
    volume-dependent indicators, trade signals, or claimed confidence scores.
    """
    result = {'modules': {}, 'overlays': {'structure': []},
              'notes': ['Аналитика текущего снимка, не торговые рекомендации. Историческая координата не означает, что результат был известен тогда.',
                        'SMC: FVG, проверенные локальные экстремумы и BOS/CHoCH по закрытым свечам. Зоны ликвидности и их пересечения — только OHLC-оценки, не реальные заявки или стопы.',
                        'Индикаторы, зависящие от объёма, отключены. Структурные отметки могут изменяться при обновлении снимка.']}
    try:
        rows, now = _prepare(bars, timeframe, as_of)
    except (ValueError, TypeError, KeyError, OverflowError):
        for name in ('smc',):
            result['modules'][name] = {'status': 'ERROR', 'version': None, 'error': 'INVALID_INPUT', 'reason': 'Некорректные входные данные'}
        return result
    result['notes'].append(f'Analyzing {len(rows)} closed bars (maximum {MAX_BARS}).')
    for name, module_name, distribution, bucket, minimum in (
        ('smc', 'smartmoneyconcepts', 'smartmoneyconcepts', 'structure', 2*SWING_LENGTH+1),):
        info = {'status': 'UNAVAILABLE', 'version': None}
        result['modules'][name] = info
        try:
            engine = _load(module_name)
            info['version'] = _version(engine, distribution)
            if len(rows) < minimum:
                info.update(status='WARMING_UP', required_bars=minimum, available_bars=len(rows))
                continue
            marks, source, components = _smc(engine, rows, now)
            info.update(volume_source=source, volume_dependent_enabled=False, components=components)
            result['overlays'][bucket] = marks
            info.update(status='READY', count=len(marks))
            if name == 'smc' and any(c['status'] != 'READY' for c in info['components'].values()):
                info['reason'] = 'Часть структурных расчётов недоступна; см. components'
                if not any(c['status'] == 'READY' for c in info['components'].values()):
                    info['status'] = 'ERROR'
        except ImportError:
            info.update(status='UNAVAILABLE', error='OPTIONAL_DEPENDENCY_UNAVAILABLE', reason='Не установлена необязательная библиотека или её зависимость')
        except Exception:
            # Do not expose exception text (paths, account/server or vendor data).
            info.update(status='ERROR', error='ANALYSIS_FAILED', reason='Ошибка аналитической библиотеки')
    return result
