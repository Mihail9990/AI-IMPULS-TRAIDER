"""Explicit Bybit server wall-time encoding. Never infer an offset from a quote."""
from datetime import datetime, timedelta, timezone
import math

BYBIT_PROFILE = 'bybit_live6_time_v1'
TEST_PROFILE = 'injected_utc_test_v1'
RAW_MARKET_MODE = 'mt5_raw_v1'


def validate_market_time_mode(config):
    mode = config.get('market_time_mode', 'fixed_utc_v1')
    if mode not in {'fixed_utc_v1', RAW_MARKET_MODE}:
        raise TimeProfileError('INVALID_MARKET_TIME_MODE')
    if mode == RAW_MARKET_MODE and config.get('market_time_offset_seconds', 0) != 0:
        raise TimeProfileError('RAW_MARKET_TIME_REQUIRES_ZERO_OFFSET')
    return mode


class TimeProfileError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class HistoryTimeUnverified(TimeProfileError):
    def __init__(self):
        super().__init__('HISTORY_TIME_PROFILE_UNVERIFIED')


def validate_profile(config, *, injected=False):
    profile = config.get('time_profile')
    if profile == TEST_PROFILE and injected:
        return profile
    if profile != BYBIT_PROFILE or config.get('expected_server') != 'Bybit-Live-6':
        raise TimeProfileError('EXPLICIT_BYBIT_LIVE6_TIME_PROFILE_REQUIRED')
    return profile


def require_execution_profile(config, *, evidence_contract=None):
    validate_profile(config)
    # Only the cycle-scoped bridge can replace the legacy global-history gate.
    # Activation acknowledges the execution contract, never verifies historical UTC.
    if (evidence_contract=='per_obligation_v1' and config.get('enable_trading') is True
            and config.get('execution_contract')=='cycle_scoped_identity_v1'):
        return
    if evidence_contract is not None:
        raise TimeProfileError('EXPLICIT_EXECUTION_ACTIVATION_REQUIRED')
    raise HistoryTimeUnverified()


def offset_at(utc_seconds):
    """Bybit published transitions are at 02:00 UTC, not New York/Europe time."""
    stamp = datetime.fromtimestamp(utc_seconds, timezone.utc)
    def sunday(month, nth):
        first = datetime(stamp.year, month, 1, 2, tzinfo=timezone.utc)
        return first + timedelta(days=(6-first.weekday()) % 7 + 7*(nth-1))
    return 10800 if sunday(3, 2) <= stamp < sunday(11, 1) else 7200


def server_to_utc(raw_seconds):
    if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, (int, float)):
        raise TimeProfileError('INVALID_SERVER_TIMESTAMP')
    if not math.isfinite(raw_seconds) or raw_seconds <= 0:
        raise TimeProfileError('INVALID_SERVER_TIMESTAMP')
    try:
        candidates = [raw_seconds-offset for offset in (7200, 10800)
                      if offset_at(raw_seconds-offset) == offset]
    except (ValueError, OverflowError, OSError):
        raise TimeProfileError('INVALID_SERVER_TIMESTAMP') from None
    if len(candidates) != 1:
        # No guessing in the spring gap or autumn repeated hour, even for a recent tick.
        raise TimeProfileError('AMBIGUOUS_OR_NONEXISTENT_SERVER_TIME')
    return candidates[0]


def validate_market_time_offset(config):
    """Fixed market-data encoding only. Never calibrate against quotes or history."""
    value = config.get('market_time_offset_seconds', 0)
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 14*3600 or value % 60:
        raise TimeProfileError('INVALID_MARKET_TIME_OFFSET_SECONDS')
    return value


def market_timestamp_to_utc(raw_seconds, offset_seconds=0):
    """Normalize one raw tick/candle timestamp; callers must not pass normalized UTC."""
    offset = validate_market_time_offset({'market_time_offset_seconds': offset_seconds})
    raw_seconds = raw_seconds.item() if hasattr(raw_seconds, 'item') else raw_seconds
    if (isinstance(raw_seconds, bool) or not isinstance(raw_seconds, (int, float))
            or not math.isfinite(raw_seconds) or raw_seconds <= 0):
        raise TimeProfileError('INVALID_MARKET_TIMESTAMP')
    result = raw_seconds - offset
    if not math.isfinite(result) or result <= 0:
        raise TimeProfileError('INVALID_MARKET_TIMESTAMP')
    return result


def market_tick_raw_seconds(tick):
    """Validate native seconds/milliseconds agreement without assigning a timezone."""
    raw = getattr(tick, 'time_msc', None)
    seconds = getattr(tick, 'time', None)
    raw = raw.item() if hasattr(raw, 'item') else raw
    seconds = seconds.item() if hasattr(seconds, 'item') else seconds
    for value in (raw, seconds):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise TimeProfileError('INVALID_MARKET_TIMESTAMP')
    stamp = raw / 1000
    if not 0 <= stamp - seconds < 1:
        raise TimeProfileError('TICK_SECONDS_MILLISECONDS_DISAGREE')
    return stamp


def market_tick_to_utc(tick, offset_seconds=0):
    """Validate raw seconds/milliseconds agreement before applying the fixed offset."""
    return market_timestamp_to_utc(market_tick_raw_seconds(tick), offset_seconds)
