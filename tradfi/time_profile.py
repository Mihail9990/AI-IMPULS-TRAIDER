"""Explicit Bybit server wall-time encoding. Never infer an offset from a quote."""
from datetime import datetime, timedelta, timezone
import math

BYBIT_PROFILE = 'bybit_live6_time_v1'
TEST_PROFILE = 'injected_utc_test_v1'


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


def require_execution_profile(config):
    validate_profile(config)
    # No production history timestamp/filter profile has been validated yet.
    # User config cannot mark an unverified history profile as verified.
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
