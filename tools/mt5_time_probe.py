"""Standalone read-only MT5 time probe. No strategy imports or time correction."""
import importlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import time

TERMINAL = 'C:/Program Files/MetaTrader 5/terminal64.exe'
SYMBOL = 'XAUUSD.s'
DURATION = 90
INTERVAL = 10


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def history_probe(api, emit, wall=time.time):
    """Compare fixed windows; +3h is a query hypothesis, never a clock correction."""
    now = wall()
    start = int(now - 86400)
    short_end, wide_end = int(now + 60), int(now + 10800 + 60)
    emit(dict(event='history_windows', start_epoch=start, end_a_epoch=short_end,
              end_b_epoch=wide_end, hypothesis_seconds=10800))
    for kind, method in (('orders', api.history_orders_get), ('deals', api.history_deals_get)):
        a = method(start, short_end, group=SYMBOL)
        b = method(start, wide_end, group=SYMBOL)
        if a is None or b is None:
            emit(dict(event='history', kind=kind, status='API_NONE_INCONCLUSIVE'))
            continue
        # Additional exact symbol filter; identifiers never leave this function.
        a = [x for x in a if x.symbol == SYMBOL]
        b = [x for x in b if x.symbol == SYMBOL]
        ids_a, ids_b = {x.ticket for x in a}, {x.ticket for x in b}
        field = 'time_done_msc' if kind == 'orders' else 'time_msc'
        times_a = [numeric(getattr(x, field, None)) for x in a]
        times_b = [numeric(getattr(x, field, None)) for x in b]
        emit(dict(event='history', kind=kind, count_a=len(a), count_b=len(b),
                  only_b_count=len(ids_b-ids_a), only_a_count=len(ids_a-ids_b),
                  latest_a_raw_ms=max((x for x in times_a if x is not None), default=None),
                  latest_b_raw_ms=max((x for x in times_b if x is not None), default=None),
                  status='OBSERVED_NOT_PROOF_OF_COMPLETENESS' if a or b else 'EMPTY_INCONCLUSIVE'))
        records = b or a
        if not records:
            continue
        selected = max(records, key=lambda x: numeric(getattr(x, field, None)) or 0)
        # For deals, ticket= is an ORDER ticket, not a deal ticket.
        query_id = selected.ticket if kind == 'orders' else selected.order
        if not isinstance(query_id, int) or query_id <= 0:
            emit(dict(event='ticket_comparison', kind=kind, status='NO_USABLE_ID_INCONCLUSIVE'))
            continue
        direct = method(ticket=query_id)
        if direct is None:
            emit(dict(event='ticket_comparison', kind=kind, status='API_NONE_INCONCLUSIVE'))
            continue
        exact = [x for x in direct if x.symbol == SYMBOL and x.ticket == selected.ticket]
        emit(dict(event='ticket_comparison', kind=kind, returned_count=len(direct),
                  selected_present=bool(exact),
                  selected_record_matches=any(x == selected for x in exact),
                  selected_raw_ms=numeric(getattr(selected, field, None)),
                  status='OBSERVED_NOT_PROOF_OF_TIMEZONE'))


def run(api, emit, *, wall=time.time, monotonic=time.monotonic, sleep=time.sleep,
        duration=DURATION, interval=INTERVAL):
    emit(dict(event='start', duration_s=duration, interval_s=interval, mode='READ_ONLY',
              clock_correction=False, symbol=SYMBOL))
    try:
        if not api.initialize(TERMINAL, timeout=10000):
            emit(dict(event='stop', status='INITIALIZE_FAILED'))
            return 1
        origin = monotonic()
        previous_tick = previous_bar = None
        # Ten observations at 0,10,...90 seconds under normal API latency.
        for index in range(int(duration // interval) + 1):
            remaining = origin + index * interval - monotonic()
            if remaining > 0:
                sleep(remaining)
            before = wall()
            tick = api.symbol_info_tick(SYMBOL)
            after = wall()
            tick_s = numeric(getattr(tick, 'time', None))
            tick_ms = numeric(getattr(tick, 'time_msc', None))
            stamp = None if tick_ms is None else tick_ms / 1000
            emit(dict(event='tick', sample=index, elapsed_s=round(monotonic()-origin, 3),
                      utc_iso=datetime.fromtimestamp(after, timezone.utc).isoformat(),
                      pc_before_epoch_s=before, pc_after_epoch_s=after,
                      raw_time_s=tick_s, raw_time_ms=tick_ms,
                      age_s=None if stamp is None else after-stamp,
                      delta_raw_ms=None if previous_tick is None or tick_ms is None else tick_ms-previous_tick,
                      status='TICK_NONE' if tick is None else 'OBSERVED'))
            previous_tick = tick_ms
            rates = api.copy_rates_from_pos(SYMBOL, api.TIMEFRAME_M1, 0, 3)
            # Positional query includes current + two previous available bars.
            bars = None if rates is None else sorted(int(r['time']) for r in rates)
            current = bars[-1] if bars else None
            emit(dict(event='m1', sample=index, pc_epoch_s=wall(),
                      raw_times_oldest_first=bars,
                      current_bar_advanced=None if current is None or previous_bar is None else current>previous_bar,
                      status='API_NONE' if rates is None else ('OBSERVED' if len(bars)==3 else 'INCOMPLETE')))
            previous_bar = current
        history_probe(api, emit, wall)
        emit(dict(event='complete', status='DIAGNOSTIC_ONLY_NO_TIME_PROFILE_APPROVED'))
        return 0
    except KeyboardInterrupt:
        emit(dict(event='stop', status='INTERRUPTED'))
        return 130
    except Exception:
        # Do not expose raw API exception messages or traceback locals.
        emit(dict(event='stop', status='API_OR_DATA_ERROR_REDACTED'))
        return 1
    finally:
        try:
            api.shutdown()
            emit(dict(event='shutdown', status='DONE'))
        except Exception:
            emit(dict(event='shutdown', status='ERROR_REDACTED'))


def main():
    try:
        # Exclusive creation prevents overwriting any previous report.
        name = 'mt5_time_report_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ') + '.txt'
        with Path(__file__).resolve().with_name(name).open('x', encoding='utf-8') as report:
            def emit(data):
                line = json.dumps(data, ensure_ascii=True, allow_nan=False)
                report.write(line + '\n')
                report.flush()
                print(line, flush=True)
            print('Report: ' + name, flush=True)
            print('Observe for 90 seconds; history reads may add time. Keep Algo Trading OFF.', flush=True)
            try:
                api = importlib.import_module('MetaTrader5')
            except Exception:
                emit(dict(event='stop', status='METATRADER5_IMPORT_FAILED'))
                return 1
            return run(api, emit)
    except Exception:
        print('Probe stopped: report/console unavailable. Private error details suppressed.', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
