"""Standalone read-only history-window experiment; never approves a time profile."""
from datetime import datetime, timedelta, timezone
import importlib
import json
import math
import ntpath
from pathlib import Path
import time

TERMINAL = 'C:/Program Files/MetaTrader 5/terminal64.exe'
SERVER = 'Bybit-Live-6'
SYMBOL = 'XAUUSD.s'
DAYS = 90
SAMPLES = 3


def offset_at(stamp):
    dt = datetime.fromtimestamp(stamp, timezone.utc)
    def sunday(month, nth):
        first = datetime(dt.year, month, 1, 2, tzinfo=timezone.utc)
        return first + timedelta(days=(6-first.weekday()) % 7 + 7*(nth-1))
    return 10800 if sunday(3, 2) <= dt < sunday(11, 1) else 7200


def candidates(raw):
    # Hypotheses only: the market-data calendar has NOT validated history timestamps.
    return [raw-offset for offset in (7200, 10800) if offset_at(raw-offset) == offset]


def valid_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def identity(api):
    terminal, account = api.terminal_info(), api.account_info()
    if (terminal is None or account is None or not terminal.connected
            or ntpath.normcase(ntpath.normpath(terminal.path)) != ntpath.normcase(ntpath.dirname(TERMINAL))
            or account.server != SERVER or account.currency != 'UST'
            or account.margin_mode != api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING):
        raise ValueError('CONTEXT_UNAVAILABLE')
    return account.login, account.server


def inspect_history(api, emit, wall=time.time):
    bound = identity(api)
    now = wall()
    start, end = int(now-DAYS*86400-10800), int(now+10800+60)
    emit(dict(event='window', days=DAYS, start_epoch=start, end_epoch=end,
              pc_utc_epoch=now, padding_seconds=10800, history_time_status='UNVERIFIED'))

    def query(method, label, *args, **kwargs):
        # No selectors, account identifiers or arbitrary exception text in output.
        if identity(api) != bound:
            raise ValueError('ACCOUNT_CHANGED')
        emit(dict(event='progress', operation=label))
        began = time.monotonic()
        rows = method(*args, **kwargs)
        if identity(api) != bound:
            raise ValueError('ACCOUNT_CHANGED')
        emit(dict(event='query', operation=label, elapsed_s=round(time.monotonic()-began, 3),
                  count=None if rows is None else len(rows),
                  status='API_NONE_INCONCLUSIVE' if rows is None else 'RETURNED'))
        return rows

    def match(rows, selected):
        if rows is None:
            return dict(selected_present=None, selected_record_matches=None)
        exact = [r for r in rows if r.symbol == SYMBOL and r.ticket == selected.ticket]
        return dict(selected_present=bool(exact), selected_record_matches=any(r == selected for r in exact))

    for kind, method, fields in (
            ('orders', api.history_orders_get, ('time_setup_msc', 'time_done_msc')),
            ('deals', api.history_deals_get, ('time_msc',))):
        # Ungrouped fetch separates no history from no exact-symbol history.
        rows = query(method, kind+'_broad', start, end)
        if rows is None:
            continue
        exact = [r for r in rows if r.symbol == SYMBOL]
        emit(dict(event='coverage', kind=kind, total_count=len(rows), exact_symbol_count=len(exact),
                  status='EVENTS_AVAILABLE_NOT_COMPLETENESS_PROOF' if exact else 'EMPTY_SYMBOL_INCONCLUSIVE'))
        if not exact:
            continue
        field = fields[-1]
        eligible = sorted((r for r in exact if valid_number(getattr(r, field, None))),
                          key=lambda r: (getattr(r, field), r.ticket))
        # At most oldest, middle and latest. Ordinal labels are not broker identifiers.
        indexes = sorted({0, len(eligible)//2, len(eligible)-1}) if eligible else []
        for sample, index in enumerate(indexes[:SAMPLES]):
            selected = eligible[index]
            raw_fields = {f: getattr(selected, f) if valid_number(getattr(selected, f, None)) else None for f in fields}
            emit(dict(event='sample', kind=kind, sample=sample, raw_times_ms=raw_fields))
            selector = selected.ticket if kind == 'orders' else selected.order
            if isinstance(selector, int) and not isinstance(selector, bool) and selector > 0:
                direct = query(method, kind+'_ticket', ticket=selector)
                emit(dict(event='direct_comparison', kind=kind, sample=sample,
                          selector_type='ticket', **match(direct, selected)))
            position = getattr(selected, 'position_id', 0)
            if isinstance(position, int) and not isinstance(position, bool) and position > 0:
                direct = query(method, kind+'_position', position=position)
                emit(dict(event='direct_comparison', kind=kind, sample=sample,
                          selector_type='position', **match(direct, selected)))
            for field in fields:
                raw_ms = raw_fields[field]
                if raw_ms is None:
                    continue
                raw = raw_ms/1000
                possible = candidates(raw)
                emit(dict(event='time_hypotheses', kind=kind, sample=sample, field=field,
                          raw_epoch_s=raw, raw_as_utc_iso=datetime.fromtimestamp(raw, timezone.utc).isoformat(),
                          bybit_calendar_utc_candidates=possible,
                          status='HYPOTHESIS_ONLY' if len(possible)==1 else 'DST_AMBIGUOUS_OR_GAP'))
                windows = [('RAW_ENCODING', raw)]
                # Do not guess if the inverse calendar is ambiguous.
                if len(possible)==1:
                    windows.append(('BYBIT_UTC_HYPOTHESIS', possible[0]))
                for hypothesis, center in windows:
                    lo, hi = math.floor(center)-2, math.ceil(center)+2
                    for form in ('epoch', 'utc_datetime'):
                        args = (lo, hi) if form=='epoch' else (
                            datetime.fromtimestamp(lo, timezone.utc), datetime.fromtimestamp(hi, timezone.utc))
                        found = query(method, kind+'_narrow_'+hypothesis+'_'+form, *args)
                        emit(dict(event='narrow_comparison', kind=kind, sample=sample, field=field,
                                  hypothesis=hypothesis, argument_form=form, start_epoch=lo, end_epoch=hi,
                                  **match(found, selected)))
        # Re-read same bounds: changed counts/IDs can flag loading, not prove completeness.
        again = query(method, kind+'_broad_repeat', start, end)
        emit(dict(event='repeat_comparison', kind=kind,
                  same_ticket_set=None if again is None else {r.ticket for r in rows}=={r.ticket for r in again},
                  status='NO_AUTOMATIC_PROFILE_APPROVAL'))


def run(api, emit):
    try:
        emit(dict(event='start', mode='READ_ONLY', history_time_status='UNVERIFIED', execution_ready=False))
        if not api.initialize(TERMINAL, timeout=10000):
            emit(dict(event='stop', status='INITIALIZE_FAILED'))
            return 1
        inspect_history(api, emit)
        emit(dict(event='complete', status='DIAGNOSTIC_ONLY', history_time_status='UNVERIFIED', execution_ready=False))
        return 0
    except KeyboardInterrupt:
        emit(dict(event='stop', status='INTERRUPTED'))
        return 130
    except Exception:
        emit(dict(event='stop', status='CONTEXT_API_OR_DATA_ERROR_REDACTED'))
        return 1
    finally:
        try:
            api.shutdown()
            emit(dict(event='shutdown', status='DONE'))
        except Exception:
            emit(dict(event='shutdown', status='ERROR_REDACTED'))


def main():
    try:
        name = 'mt5_history_report_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ') + '.txt'
        with Path(__file__).resolve().with_name(name).open('x', encoding='utf-8') as report:
            def emit(data):
                line = json.dumps(data, ensure_ascii=True, allow_nan=False)
                report.write(line+'\n'); report.flush()
                print(line, flush=True)
            print('Report: '+name, flush=True)
            print('Reading up to 90 days. Keep Algo Trading OFF. API reads may take time.', flush=True)
            try:
                api = importlib.import_module('MetaTrader5')
            except Exception:
                emit(dict(event='stop', status='METATRADER5_IMPORT_FAILED'))
                return 1
            return run(api, emit)
    except Exception:
        print('Report/console unavailable. Private error details suppressed.', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
