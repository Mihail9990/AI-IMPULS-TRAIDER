"""Read-only analysis isolation. No controller, store or execution API crosses IPC.

HTTP reads a cache; the owner only replaces a small time proof. A supervisor
thread owns a killable spawn child and bounded IPC, never the trading loop.
"""
from copy import deepcopy
import json
import math
import multiprocessing
import ntpath
import os
import queue
import threading
import time

from .time_profile import market_timestamp_to_utc, TEST_PROFILE

TIMEFRAMES = {'M1': 60, 'M15': 900, 'M30': 1800, 'H1': 3600}
BAR_LIMIT = 500
LOOKBACKS = (50, 100, 200, 300, 500)


def validate_lookback(bars):
    if type(bars) is not int or bars not in LOOKBACKS:
        raise ValueError('INVALID_ANALYSIS_BARS')
    return bars


RESULT_LIMIT = 4 * 1024 * 1024


def analysis_clock():
    """One system-wide monotonic domain for analytics owner and child.

    Windows Python 3.12 monotonic uses GetTickCount64; 3.13 uses QPC.
    Their absolute samples must never cross IPC for subtraction. perf_counter
    uses system-wide QPC on both supported Windows interpreters (3.10+).
    Keep this clock local to read-only analysis; no trading clock is changed.
    """
    return time.perf_counter()


def empty_snapshot(symbol, timeframe, status='WARMING_UP', reason=None):
    value = dict(schema='analysis_v1', status=status, symbol=symbol,
                 timeframe=timeframe, source='MT5', generated_at=None, as_of=None,
                 closed_through=None, timestamp_semantics='UNVERIFIED',
                 feed_instance=None, bars=[],
                 modules={name: {'status': 'UNAVAILABLE'} for name in ('smc',)},
                 overlays=dict(structure=[]), notes=[])
    if reason:
        value['reason'] = reason
        value['diagnostics'] = dict(reason=reason)
    return value


def proof_status(proof, mono):
    if not proof or not proof.get('connected'): return 'DISCONNECTED'
    if proof.get('timestamp_semantics') == 'BROKER_RAW':
        stamp = proof.get('broker_time')
        return 'READY' if not isinstance(stamp, bool) and isinstance(stamp, (int, float)) and math.isfinite(stamp) and stamp > 0 else 'TIME_UNVERIFIED'
    if proof.get('timestamp_semantics') != 'VERIFIED': return 'TIME_UNVERIFIED'
    elapsed = mono - proof['monotonic']
    if elapsed < 0 or not math.isfinite(elapsed): return 'TIME_UNVERIFIED'
    if proof.get('status') != 'FRESH' or elapsed + proof['age'] > proof['max_age']: return 'STALE'
    return 'READY'


# Only literal codes cross diagnostic boundaries. Never format native exceptions.
SAFE_ERRORS = frozenset("""HISTORY_UNAVAILABLE HISTORY_LIMIT FIXED_TIME_PROFILE_REQUIRED
HISTORY_ORDER_OR_BOUNDARY HISTORY_FUTURE HISTORY_OHLC HISTORY_UTC_ORDER HISTORY_VOLUME
HISTORY_NO_CLOSED_BARS SOURCE_IDENTITY_MISMATCH MT5_INITIALIZE_FAILED RESULT_LIMIT COMMAND_LIMIT INVALID_RESULT
WORKER_EXITED IPC_READ_FAILED STARTUP_TIMEOUT RESPONSE_TIMEOUT""".split())


def safe_error(exc):
    code = exc.args[0] if len(exc.args) == 1 else None
    return code if isinstance(code, str) and code in SAFE_ERRORS else 'ANALYSIS_ERROR'


def proof_diagnostics(proof, mono):
    status = proof_status(proof, mono)
    reason = {'DISCONNECTED': 'PROOF_DISCONNECTED', 'TIME_UNVERIFIED': 'PROOF_TIME_UNVERIFIED',
              'STALE': 'PROOF_STALE', 'READY': 'PROOF_READY'}[status]
    return dict(proof_status=status, proof_reason=reason,
                proof_age_seconds=max(0, mono - proof['monotonic'] + proof['age']) if proof else None)


def elapsed_ms(start):
    return round((analysis_clock() - start) * 1000, 3)


def read_bars(api, config, timeframe, now):
    """Exact native TF, closed bars only; no interpolation or aggregation."""
    if config['time_mode'] == 'mt5_raw_v1':
        return read_raw_bars(api, config, timeframe, now)
    duration = TIMEFRAMES[timeframe]
    limit = validate_lookback(config.get('analysis_bars', BAR_LIMIT))
    raw = api.copy_rates_from_pos(config['symbol'], getattr(api, 'TIMEFRAME_' + timeframe), 1, limit)
    if raw is None or not len(raw): raise ValueError('HISTORY_UNAVAILABLE')
    if len(raw) > limit: raise ValueError('HISTORY_LIMIT')
    bars = []
    previous = 0
    for source in raw:
        stamp = source['time'].item() if hasattr(source['time'], 'item') else source['time']
        stamp = float(stamp)
        if config['time_mode'] != 'fixed': raise ValueError('FIXED_TIME_PROFILE_REQUIRED')
        utc = market_timestamp_to_utc(stamp, config['offset'])
        # MT5 candle boundaries belong to the source clock, not PC timezone.
        if stamp % duration or stamp <= previous: raise ValueError('HISTORY_ORDER_OR_BOUNDARY')
        previous = stamp
        if utc > now: raise ValueError('HISTORY_FUTURE')
        row = {key: float(source[key]) for key in ('open', 'high', 'low', 'close')}
        if not all(math.isfinite(x) and x > 0 for x in row.values()): raise ValueError('HISTORY_OHLC')
        if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            raise ValueError('HISTORY_OHLC')
        if utc + duration > now: continue
        if bars and utc <= bars[-1]['utc_time']: raise ValueError('HISTORY_UTC_ORDER')
        row.update(raw_time=stamp, utc_time=utc)
        for key in ('tick_volume', 'real_volume'):
            try: value = float(source[key])
            except (KeyError, ValueError, TypeError, IndexError): continue
            if not math.isfinite(value) or value < 0: raise ValueError('HISTORY_VOLUME')
            row[key] = value
        bars.append(row)
    if not bars: raise ValueError('HISTORY_NO_CLOSED_BARS')
    return bars


def read_raw_bars(api, config, timeframe, source_tick):
    """MT5 native bar0 identifies the open candle; never use host time."""
    duration = TIMEFRAMES[timeframe]
    limit = validate_lookback(config.get('analysis_bars', BAR_LIMIT))
    raw = api.copy_rates_from_pos(config['symbol'], getattr(api, 'TIMEFRAME_' + timeframe), 0, limit + 1)
    if raw is None or len(raw) < 2: raise ValueError('HISTORY_NO_CLOSED_BARS')
    if len(raw) > limit + 1: raise ValueError('HISTORY_LIMIT')
    bars, previous = [], 0
    for source in raw:
        stamp = float(source['time'])
        if not math.isfinite(stamp) or stamp <= previous or stamp % duration:
            raise ValueError('HISTORY_ORDER_OR_BOUNDARY')
        if stamp > source_tick: raise ValueError('HISTORY_FUTURE')
        previous = stamp
        row = {key: float(source[key]) for key in ('open', 'high', 'low', 'close')}
        if not all(math.isfinite(x) and x > 0 for x in row.values()): raise ValueError('HISTORY_OHLC')
        if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            raise ValueError('HISTORY_OHLC')
        row.update(raw_time=stamp, broker_time=stamp, utc_time=None, time_domain='BROKER_RAW', closed=True)
        for key in ('tick_volume', 'real_volume'):
            try: value = float(source[key])
            except (KeyError, ValueError, TypeError, IndexError): continue
            if not math.isfinite(value) or value < 0: raise ValueError('HISTORY_VOLUME')
            row[key] = value
        bars.append(row)
    # Last returned bar is native position zero. Gaps remain gaps.
    return bars[:-1]


def calculate_raw(api, config, proof, cache, *, analyzer=None):
    result = {}
    now = proof['broker_time']
    for tf, duration in TIMEFRAMES.items():
        try:
            bars = read_raw_bars(api, config, tf, now)
            if not identity_matches(api, config): raise ValueError('SOURCE_IDENTITY_MISMATCH')
            previous = cache.get(tf)
            reused = bool(previous and previous.get('timestamp_semantics') == 'BROKER_RAW'
                          and previous.get('feed_instance') == proof['instance'] and previous.get('bars') == bars)
            value = deepcopy(previous) if reused else empty_snapshot(config['symbol'], tf, 'READY')
            if not reused:
                if analyzer is None:
                    from .analysis_adapters import analyze_bars
                    analyzer = analyze_bars
                value.update(analyzer(bars, tf, now))
                value['notes'].append('Время MT5 без преобразования в UTC. Принимается последнее наблюдение, без проверки давности; пропуски свечей не заполняются.')
            value.update(status='READY', bars=bars, generated_at=now, as_of=now, broker_time=now,
                         closed_through=bars[-1]['broker_time'] + duration,
                         timestamp_semantics='BROKER_RAW', time_domain='BROKER_RAW',
                         freshness_policy='ACCEPT_LAST_OBSERVED', feed_instance=proof['instance'])
            value['diagnostics'] = dict(reason='CACHE_REUSED' if reused else 'CALCULATED', cache_reused=reused,
                                       bar_count=len(bars), actual_closed_through=value['closed_through'])
            json.dumps(value, allow_nan=False)
            result[tf] = value
        except Exception as exc:
            result[tf] = empty_snapshot(config['symbol'], tf, 'UNAVAILABLE', 'ANALYSIS_SOURCE_UNAVAILABLE')
            result[tf]['diagnostics']['error_code'] = safe_error(exc)
    return result


def identity_matches(api, config):
    account = api.account_info()
    terminal = api.terminal_info()
    expected_directory = ntpath.normcase(ntpath.normpath(ntpath.dirname(config['terminal_path'])))
    actual_directory = ntpath.normcase(ntpath.normpath(getattr(terminal, 'path', '') or ''))
    return bool(account and terminal and terminal.connected and
                expected_directory not in ('', '.') and actual_directory == expected_directory and
                account.login == config['expected_login'] and account.server == config['expected_server'])


def calculate(api, config, proof, cache, *, mono=None, analyzer=None, retry_state=None):
    """Pure orchestration around an injected read-only API, usable offline."""
    started = analysis_clock()
    mono = started if mono is None else mono
    state = proof_status(proof, mono)
    if state != 'READY':
        values = {tf: empty_snapshot(config['symbol'], tf, state) for tf in TIMEFRAMES}
        for value in values.values(): value['diagnostics'] = dict(proof_diagnostics(proof, mono), reason=proof_diagnostics(proof, mono)['proof_reason'])
        return values
    if not identity_matches(api, config):
        return {tf: empty_snapshot(config['symbol'], tf, 'DISCONNECTED', 'SOURCE_IDENTITY_MISMATCH') for tf in TIMEFRAMES}
    if config['time_mode'] == 'mt5_raw_v1':
        if proof.get('timestamp_semantics') != 'BROKER_RAW':
            return {tf: empty_snapshot(config['symbol'], tf, 'TIME_UNVERIFIED') for tf in TIMEFRAMES}
        return calculate_raw(api, config, proof, cache, analyzer=analyzer)
    if proof.get('timestamp_semantics') == 'BROKER_RAW':
        return {tf: empty_snapshot(config['symbol'], tf, 'TIME_UNVERIFIED') for tf in TIMEFRAMES}
    now = proof['utc'] + mono - proof['monotonic']
    retry_state = {} if retry_state is None else retry_state
    result = {}
    for tf, duration in TIMEFRAMES.items():
        diagnostic = dict(proof_diagnostics(proof, mono), history_ms=0, calculation_ms=0, cache_reused=False)
        stage = 'HISTORY'
        try:
            previous = cache.get(tf)
            latest_expected = math.floor((now + config['offset']) / duration) * duration - config['offset']
            diagnostic['expected_closed_through'] = latest_expected
            if (previous and previous.get('bars') and previous.get('status') in {'READY', 'STALE'}
                    and previous.get('timestamp_semantics') == 'VERIFIED'
                    and previous.get('feed_instance') == proof['instance']
                    and previous.get('closed_through') == latest_expected):
                # Closed history is immutable for this polling interval. Vendor
                # corrections are picked up at the next native candle close.
                value = deepcopy(previous)
                value['status'] = proof_status(proof, mono + analysis_clock() - started)
                diagnostic.update(cache_reused=True, reason='CACHE_REUSED', actual_closed_through=value.get('closed_through'), total_ms=elapsed_ms(started))
                value['diagnostics'] = diagnostic
                result[tf] = value
                continue
            retry = retry_state.get(tf)
            if retry and retry['instance'] == proof['instance'] and mono < retry['after']:
                result[tf] = deepcopy(previous) if previous else empty_snapshot(config['symbol'], tf, 'UNAVAILABLE')
                prior = result[tf].get('diagnostics', {})
                for key in ('error_code', 'stage', 'last_failure_reason'):
                    if key in prior: diagnostic[key] = prior[key]
                if prior.get('reason') != 'HISTORY_RETRY_WAIT':
                    diagnostic['last_failure_reason'] = prior.get('reason')
                diagnostic.update(reason='HISTORY_RETRY_WAIT', retry_in_seconds=max(0, retry['after']-mono), actual_closed_through=result[tf].get('closed_through'))
                result[tf]['diagnostics'] = diagnostic
                continue
            phase_started = analysis_clock()
            bars = read_bars(api, config, tf, now)
            diagnostic.update(history_ms=elapsed_ms(phase_started), bar_count=len(bars), actual_closed_through=bars[-1]['utc_time']+duration)
            if proof_status(proof, mono + analysis_clock() - started) != 'READY':
                result[tf] = empty_snapshot(config['symbol'], tf, 'STALE')
                diagnostic.update(reason='PROOF_EXPIRED_DURING_HISTORY', total_ms=elapsed_ms(started))
                result[tf]['diagnostics'] = diagnostic
                continue
            # Account switch during native read cannot contaminate the cache.
            if not identity_matches(api, config): raise ValueError('SOURCE_IDENTITY_MISMATCH')
            previous = cache.get(tf)
            if previous and previous['bars'] == bars and previous['feed_instance'] == proof['instance']:
                value = deepcopy(previous)
                diagnostic['cache_reused'] = True
            else:
                value = empty_snapshot(config['symbol'], tf, 'READY')
                value.update(bars=bars, generated_at=now, as_of=now,
                             closed_through=bars[-1]['utc_time'] + duration,
                             timestamp_semantics='VERIFIED', feed_instance=proof['instance'])
                if analyzer is None:
                    from .analysis_adapters import analyze_bars
                    analyzer = analyze_bars
                stage = 'CALCULATION'
                phase_started = analysis_clock()
                value.update(analyzer(bars, tf, now))
                diagnostic['calculation_ms'] = elapsed_ms(phase_started)
                value['notes'].append('История использует заданное фиксированное смещение времени MT5, без автоматической сезонной поправки. Для старой истории смещение не проверялось отдельно.')
                json.dumps(value, allow_nan=False)
            # A missing most recently closed period is explicit, never filled.
            value['status'] = 'STALE' if value['closed_through'] < latest_expected else 'READY'
            if value['status'] == 'STALE':
                retry_state[tf] = dict(instance=proof['instance'], after=mono + 5)
            else:
                retry_state.pop(tf, None)
            final_status = proof_status(proof, mono + analysis_clock() - started)
            if final_status != 'READY': value['status'] = final_status
            diagnostic.update(reason=('PROOF_EXPIRED_DURING_CALCULATION' if final_status != 'READY' else 'MISSING_LAST_CLOSED_PERIOD' if value['status'] == 'STALE' else 'CACHE_REUSED' if diagnostic['cache_reused'] else 'CALCULATED'), total_ms=elapsed_ms(started))
            value['diagnostics'] = diagnostic
            result[tf] = value
        except Exception as exc:
            retry_state[tf] = dict(instance=proof['instance'], after=mono + 5)
            result[tf] = empty_snapshot(config['symbol'], tf, 'UNAVAILABLE', 'ANALYSIS_SOURCE_UNAVAILABLE')
            diagnostic.update(reason='ANALYSIS_SOURCE_UNAVAILABLE', error_code=safe_error(exc), stage=stage, total_ms=elapsed_ms(started), retry_in_seconds=5)
            result[tf]['diagnostics'] = diagnostic
    return result


def _worker(config, incoming, outgoing):
    # Child-local limits only, before numerical libraries are imported.
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                 'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS'):
        os.environ[name] = '1'
    api = None
    try:
        import MetaTrader5 as api
        if not api.initialize(config['terminal_path'], timeout=10000): raise RuntimeError('MT5_INITIALIZE_FAILED')
        caches = {}
        retries = {}
        while True:
            proof = incoming.get()
            if proof is None: break
            lookback = validate_lookback(proof.get('analysis_bars', BAR_LIMIT))
            scoped_config = dict(config, analysis_bars=lookback)
            cache = calculate(api, scoped_config, proof, caches.get(lookback, {}),
                              retry_state=retries.setdefault(lookback, {}))
            caches[lookback] = cache
            for value in cache.values(): value['requested_bars'] = lookback
            final_status = proof_status(proof, analysis_clock())
            if final_status != 'READY':
                for value in cache.values():
                    value['status'] = final_status
                    diagnostic = value.setdefault('diagnostics', {})
                    diagnostic.update(proof_diagnostics(proof, analysis_clock()), reason='PROOF_EXPIRED_BEFORE_PUBLISH')
            payload = json.dumps(cache, allow_nan=False).encode('utf-8')
            if len(payload) > RESULT_LIMIT: raise ValueError('RESULT_LIMIT')
            outgoing.send_bytes(payload)
    except Exception as exc:
        try:
            failed = {tf: empty_snapshot(config['symbol'], tf, 'UNAVAILABLE', 'ANALYSIS_WORKER_UNAVAILABLE') for tf in TIMEFRAMES}
            for value in failed.values(): value['diagnostics']['error_code'] = safe_error(exc)
            outgoing.send_bytes(json.dumps(failed).encode('utf-8'))
        except Exception: pass
    finally:
        outgoing.close()
        if api is not None:
            try: api.shutdown()
            except Exception: pass


def _read_results(connection, completed):
    """Only this thread may block on a partial IPC frame; never the watchdog.

    There is exactly one reader per child. Killing the sole writer closes its
    pipe and releases recv_bytes with EOF, including a truncated frame.
    """
    try:
        while True:
            payload = connection.recv_bytes(maxlength=RESULT_LIMIT)
            value = json.loads(payload)
            if not isinstance(value, dict) or set(value) != set(TIMEFRAMES):
                raise ValueError('INVALID_RESULT')
            completed.put_nowait((True, value))
    except Exception as exc:
        code = safe_error(exc)
        try: completed.put_nowait((False, code if code != 'ANALYSIS_ERROR' else 'IPC_READ_FAILED'))
        except queue.Full: pass
    finally:
        connection.close()


class DisabledAnalysisService:
    """Optional startup failure never prevents the trading panel starting."""
    def __init__(self, symbol): self.symbol = symbol
    def observe(self, feed): pass
    def close(self): pass
    def view(self, timeframe, bars=BAR_LIMIT):
        validate_lookback(bars)
        value = empty_snapshot(self.symbol, timeframe, 'UNAVAILABLE', 'ANALYSIS_STARTUP_UNAVAILABLE')
        value['requested_bars'] = bars
        return value


class AnalysisService:
    def __init__(self, broker, *, context=None, timeout=20):
        # Explicit allowlist. Never pass the broker/config, credentials or state.
        account = str(getattr(broker, 'account', '')).split('@', 1)
        login = getattr(broker, 'login', None)
        if login is None and account[0].isdigit(): login = int(account[0])
        self.config = dict(terminal_path=getattr(broker, 'path', ''), symbol=broker.symbol,
                           expected_login=login, expected_server=getattr(broker, 'server', ''),
                           time_mode='mt5_raw_v1' if getattr(broker, 'config', {}).get('market_time_mode') == 'mt5_raw_v1' else 'fixed' if hasattr(broker, 'market_time_offset_seconds') or getattr(broker, 'time_profile', None) == TEST_PROFILE else 'server',
                           offset=getattr(broker, 'market_time_offset_seconds', 0))
        self.enabled = bool(self.config['time_mode'] in ('fixed', 'mt5_raw_v1') and self.config['terminal_path'] and login and self.config['expected_server'] and
                            not getattr(broker.api, 'is_test_binding', False) and getattr(broker, 'time_profile', None) != TEST_PROFILE)
        self.external_python = None
        self.unavailable_reason = None
        configured_python = getattr(broker, 'config', {}).get('analysis_python')
        if configured_python is not None or (os.name == 'nt' and not getattr(broker.api, 'is_test_binding', False)):
            from pathlib import Path
            from .analysis_process import validate_python
            from .analysis_runtime import VENV_DIRECTORY
            candidate = configured_python or str(Path(__file__).resolve().parents[1] / VENV_DIRECTORY / 'Scripts' / 'python.exe')
            try: self.external_python = validate_python(candidate)
            except Exception:
                self.enabled = False
                self.unavailable_reason = 'ANALYSIS_ENVIRONMENT_UNAVAILABLE'
        self.max_age = float(broker.max_age)
        self.context = context or multiprocessing.get_context('spawn')
        self.timeout = timeout
        self.worker_target = _worker
        self.proof = None
        self.cache = {}
        self.lookback_cache = {}
        self.lookback_received = {}
        self.lookback_access = {}
        self.stop = threading.Event()
        self.thread = None
        self.start_lock = threading.Lock()
        self.failure = None
        self.last_access = 0
        self.retry_after = 0
        self.supervisor = dict(supervisor_state='IDLE', failures=0)
        self.received_at = None
        self.received_mono = None

    def observe(self, feed):
        """Owner handoff: no locks, IO, queues, process start or calculation."""
        try:
            if self.config['time_mode'] == 'mt5_raw_v1':
                self.proof = dict(connected=feed.get('connected', False), status=feed.get('status'),
                    timestamp_semantics=feed.get('timestamp_semantics'), broker_time=feed.get('broker_time'),
                    monotonic=analysis_clock(), age=0, max_age=self.max_age, instance=feed.get('instance'))
                return
            age = float(feed.get('age_seconds') or 0)
            utc = float(feed.get('timestamp') or 0) + age
            if not math.isfinite(age) or not math.isfinite(utc): raise ValueError()
            self.proof = dict(connected=feed.get('connected', False), status=feed.get('status'),
                              timestamp_semantics=feed.get('timestamp_semantics'),
                              monotonic=analysis_clock(), utc=utc, age=age,
                              max_age=self.max_age, instance=feed.get('instance'))
        except Exception:
            self.proof = None

    def view(self, timeframe, bars=BAR_LIMIT):
        validate_lookback(bars)
        if timeframe not in TIMEFRAMES: raise ValueError('INVALID_TIMEFRAME')
        self.last_access = analysis_clock()
        # At most five fixed keys. Copy-on-write lets the supervisor iterate
        # a stable demand snapshot without locking the owner/trading thread.
        self.lookback_access = dict(self.lookback_access, **{str(bars): self.last_access})
        if self.enabled and not self.stop.is_set() and self.thread is None and self.last_access >= self.retry_after:
            with self.start_lock:
                if self.thread is None:
                    self.thread = threading.Thread(target=self._supervise, name='analysis-supervisor', daemon=True)
                    try: self.thread.start()
                    except Exception:
                        self.thread = None
                        self.failure = 'UNAVAILABLE'
                        self.retry_after = analysis_clock() + 60
                        self.supervisor = dict(supervisor_state='RETRY_WAIT', failures=1, error_code='SUPERVISOR_START_FAILED')
        cache = self.cache if bars == BAR_LIMIT else self.lookback_cache.get(bars, {})
        value = deepcopy(cache.get(timeframe) or empty_snapshot(self.config['symbol'], timeframe))
        # observe() replaces the whole proof. Never mix two observations or
        # compare a new proof against a clock sampled before that observation.
        proof = self.proof
        mono = analysis_clock()
        status = proof_status(proof, mono)
        if not self.enabled:
            status = 'UNAVAILABLE'
            if self.unavailable_reason:
                value['reason'] = self.unavailable_reason
                value['notes'] = ['Запустите отдельную установку аналитики SETUP_ANALYSIS.cmd; торговая среда не меняется.']
        elif self.failure: status = self.failure
        elif status == 'READY': status = value['status']
        if value['feed_instance'] and proof and value['feed_instance'] != proof['instance']:
            value = empty_snapshot(self.config['symbol'], timeframe)
            status = 'WARMING_UP'
        if status == 'READY' and value['bars'] and proof and self.config['time_mode'] != 'mt5_raw_v1':
            now = proof['utc'] + mono - proof['monotonic']
            offset = self.config['offset']
            expected = math.floor((now + offset) / TIMEFRAMES[timeframe]) * TIMEFRAMES[timeframe] - offset
            if value['closed_through'] < expected: status = 'STALE'
        value['status'] = status
        diagnostic = dict(value.get('diagnostics', {}))
        diagnostic.update(self.supervisor)
        diagnostic.update(proof_diagnostics(proof, mono))
        received_at, received_mono = self.lookback_received.get(bars,
            (self.received_at, self.received_mono) if bars == BAR_LIMIT else (None, None))
        diagnostic.update(generated_at=value.get('generated_at'), received_at=received_at,
                          cache_age_seconds=max(0, mono-received_mono) if received_mono is not None else None,
                          supervisor_retry_in_seconds=max(0, self.retry_after-mono))
        if proof and self.config['time_mode'] != 'mt5_raw_v1':
            now = proof['utc'] + mono - proof['monotonic']
            duration = TIMEFRAMES[timeframe]
            diagnostic['expected_closed_through'] = math.floor((now+self.config['offset'])/duration)*duration-self.config['offset']
        diagnostic['actual_closed_through'] = value.get('closed_through')
        if status == 'STALE':
            if diagnostic['proof_status'] != 'READY':
                diagnostic['reason'] = diagnostic['proof_reason']
            elif (value.get('closed_through') is not None
                    and diagnostic.get('expected_closed_through') is not None
                    and value['closed_through'] < diagnostic['expected_closed_through']):
                diagnostic['reason'] = 'MISSING_LAST_CLOSED_PERIOD'
            # A fresh owner proof cannot retroactively validate an expired
            # worker proof. Preserve that cached reason until worker refresh.
        value['diagnostics'] = diagnostic
        value['requested_bars'] = bars
        return value

    def diagnostic_view(self):
        # Fixed four-row cached projection only. Never calls view/start/MT5/IPC.
        cache, supervisor, proof = self.cache, self.supervisor, self.proof
        mono = analysis_clock()
        current_proof = proof_diagnostics(proof, mono)
        rows = []
        for tf in TIMEFRAMES:
            value = cache.get(tf, {})
            row = dict(value.get('diagnostics', {}))
            row.update(supervisor)
            row.update(current_proof_status=current_proof['proof_status'],
                       current_proof_reason=current_proof['proof_reason'],
                       current_proof_age_seconds=current_proof['proof_age_seconds'],
                       cache_age_seconds=max(0, mono-self.received_mono) if self.received_mono is not None else None)
            row.update(timeframe=tf, status=value.get('status', 'WARMING_UP'),
                       generated_at=value.get('generated_at'), received_at=self.received_at)
            rows.append(row)
        return rows


    def _supervise(self):
        failures = 0
        reader_stuck = False
        while not self.stop.is_set() and failures < 3:
            process = incoming = outgoing = reader_pipe = reader = None
            reader_stuck = False
            self.supervisor = dict(supervisor_state='STARTING', failures=failures)
            try:
                completed = queue.Queue(maxsize=1)
                if self.external_python:
                    from .analysis_process import ExternalWorker
                    process = ExternalWorker(self.external_python, self.config)
                    process.start()
                    incoming, reader_pipe = process.input, process.output
                else:
                    incoming = self.context.Queue(maxsize=1)
                    reader_pipe, outgoing = self.context.Pipe(duplex=False)
                    process = self.context.Process(target=self.worker_target, args=(self.config, incoming, outgoing), daemon=True)
                    process.start()
                    outgoing.close()  # The child must be the sole remaining writer.
                    outgoing = None
                reader = threading.Thread(target=_read_results, args=(reader_pipe, completed),
                                          name='analysis-result-reader', daemon=True)
                reader.start()
                pending = None
                pending_lookback = BAR_LIMIT
                last_dispatched = {}
                launched = analysis_clock()
                received = False
                next_send = 0
                while not self.stop.wait(.05):
                    now = analysis_clock()
                    if now - self.last_access > 60: break
                    if not process.is_alive(): raise RuntimeError('WORKER_EXITED')
                    if not received and now - launched > self.timeout: raise TimeoutError('STARTUP_TIMEOUT')
                    if pending is not None:
                        try:
                            ok, result = completed.get_nowait()
                            if not ok: raise RuntimeError(result)
                            if any(v.get('requested_bars', BAR_LIMIT) != pending_lookback for v in result.values()):
                                raise ValueError('INVALID_RESULT')
                            received_at = time.time()
                            self.lookback_received = {**self.lookback_received, pending_lookback: (received_at, now)}
                            if pending_lookback == BAR_LIMIT:
                                self.received_at = received_at
                                self.received_mono = now
                            self.supervisor = dict(supervisor_state='READY', failures=failures, ipc_ms=round((now-pending)*1000, 3))
                            # The outstanding command owns the result key. Never
                            # let one selected depth overwrite another depth.
                            self.lookback_cache = {**self.lookback_cache, pending_lookback: result}
                            if pending_lookback == BAR_LIMIT: self.cache = result
                            received = True
                            self.failure = None
                            pending = None
                            next_send = now + 1
                        except queue.Empty:
                            if now - pending > self.timeout: raise TimeoutError('RESPONSE_TIMEOUT')
                    elif now >= next_send:
                        # Validate and send the same immutable owner observation.
                        proof = self.proof
                        dispatched = analysis_clock()
                        if proof_status(proof, dispatched) == 'READY':
                            active = [int(k) for k, accessed in self.lookback_access.items() if now-accessed <= 60]
                            if not active: active = [BAR_LIMIT]
                            pending_lookback = min(active, key=lambda key: last_dispatched.get(key, 0))
                            incoming.put_nowait(proof if pending_lookback == BAR_LIMIT else dict(proof, analysis_bars=pending_lookback))
                            last_dispatched[pending_lookback] = now
                            pending = dispatched
                            self.supervisor = dict(supervisor_state='WAITING', failures=failures)
            except Exception as exc:
                failures += 1
                self.supervisor = dict(supervisor_state='RETRY_WAIT', failures=failures, error_code=safe_error(exc))
                self.failure = 'UNAVAILABLE'
            finally:
                if process is not None:
                    # A failed courtesy shutdown must never skip termination.
                    if incoming is not None and not getattr(process, 'is_external', False):
                        try: incoming.put_nowait(None); process.join(timeout=.2)
                        except Exception: pass
                    try:
                        if process.is_alive(): process.terminate()
                    except Exception: pass
                    try: process.join(timeout=.5)
                    except Exception: pass
                    try:
                        if process.is_alive(): process.kill()
                    except Exception: pass
                    try: process.join(timeout=.5)
                    except Exception: pass
                    try: reader_stuck = process.is_alive()
                    except Exception: reader_stuck = True
                if outgoing is not None:
                    try: outgoing.close()
                    except Exception: pass
                if reader is not None:
                    reader.join(timeout=.5)
                    reader_stuck = reader_stuck or reader.is_alive()
                if reader_pipe is not None and not reader_stuck:
                    try: reader_pipe.close()
                    except Exception: pass
                if incoming is not None:
                    try: incoming.cancel_join_thread(); incoming.close()
                    except Exception: pass
            if reader_stuck:
                # Do not accumulate orphan readers if OS cleanup fails.
                self.enabled = False
                self.failure = 'UNAVAILABLE'
                self.supervisor = dict(supervisor_state='CLEANUP_FAILED', failures=failures)
                break
            if analysis_clock() - self.last_access > 60 or self.stop.wait(5): break
        if failures >= 3:
            self.retry_after = analysis_clock() + 60
            self.supervisor = dict(self.supervisor, supervisor_state='RETRY_EXHAUSTED')
        if self.stop.is_set():
            self.supervisor = dict(self.supervisor, supervisor_state='STOPPED')
        elif not failures and not reader_stuck:
            self.supervisor = dict(self.supervisor, supervisor_state='IDLE')
        self.thread = None

    def close(self):
        self.stop.set()
        thread = self.thread
        if thread is not None: thread.join(timeout=2)
