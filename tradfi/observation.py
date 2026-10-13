"""Process-local display evidence. Never used to authorize trading or loaded from SQLite."""
from collections import Counter
from datetime import datetime, timezone
import math


SAFE_OBLIGATIONS = frozenset('''UNKNOWN API_EVIDENCE_UNAVAILABLE MISSING_DURABLE_JOURNAL
PENDING_OBSERVED FILLED_OPEN_EVIDENCE CLOSED_EVIDENCE NO_SEND_EVIDENCE REJECTED_RESPONSE
TERMINAL_UNFILLED_EVIDENCE CANCEL_TERMINAL_READBACK PROTECTION_READBACK
AWAIT_FILL_EVIDENCE AWAIT_POSITION_EVIDENCE ORDER_NOT_FINAL PARTIAL_FILL PARTIAL_CLOSE
EXTERNAL_EXIT UNSUPPORTED_EXIT AMBIGUOUS_HISTORY_TIME'''.split())


class Observation:
    """The owner thread records complete snapshots; formatting never contacts MT5."""
    max_age = 5.0
    clock_tolerance = 1.0

    def __init__(self, clock, monotonic):
        self.clock, self.monotonic = clock, monotonic
        self.last_good = None
        self.failure = 'NOT_OBSERVED_SINCE_START'
        self.anchor = None

    def _time(self):
        wall, mono = float(self.clock()), float(self.monotonic())
        if not math.isfinite(wall) or not math.isfinite(mono) or wall <= 0 or mono < 0:
            raise ValueError()
        return wall, mono

    def begin(self):
        try:
            return self._time()
        except Exception:
            return None

    def fail(self, reason='SNAPSHOT_UNAVAILABLE'):
        self.failure = reason

    def _continuous(self, before, after):
        return (after[1] >= before[1]
                and abs((after[0]-before[0])-(after[1]-before[1])) <= self.clock_tolerance)

    def record(self, snap, started):
        # Display bookkeeping cannot add a trading failure or relax a trading guard.
        try:
            ended = self._time()
            previous = self.anchor
            self.anchor = ended
            if started is None or not self._continuous(started, ended) or (previous and not self._continuous(previous, ended)):
                self.fail('CLOCK_DISCONTINUITY'); return
            if ended[1]-started[1] > self.max_age:
                self.fail('SNAPSHOT_TOO_SLOW'); return
            if not isinstance(snap['positions'], list) or not isinstance(snap['orders'], list):
                raise ValueError()
            statuses = Counter()
            for row in snap.get('obligations', {}).values():
                status = row.get('status')
                statuses[status if isinstance(status,str) and status in SAFE_OBLIGATIONS else 'OTHER'] += 1
            history = snap.get('history_time_status')
            history = history if history in {'UNVERIFIED','VERIFIED'} else 'UNKNOWN'
            stamp = datetime.fromtimestamp(ended[0], timezone.utc).isoformat(timespec='milliseconds')
            quote = snap.get('quote', {}).get('utc_time')
            quote = float(quote) if quote is not None and not isinstance(quote,bool) else None
            if quote is not None and (not math.isfinite(quote) or quote <= 0): quote = None
            self.last_good = dict(wall=ended[0], mono=ended[1], timestamp_utc=stamp,
                quote_utc=quote, summary=dict(positions=len(snap['positions']), orders=len(snap['orders']),
                obligations=dict(sorted(statuses.items())), history_time_status=history,
                history_complete=snap.get('history_complete') is True))
            self.failure = None
        except Exception:
            self.fail('OBSERVATION_INVALID')

    def view(self):
        result = dict(status='UNAVAILABLE', reason=self.failure,
                      timestamp_utc=None, age_seconds=None, current=None)
        if self.last_good is None:
            return result
        good = self.last_good
        result['timestamp_utc'] = good['timestamp_utc']
        try:
            now = self._time()
            if not self._continuous((good['wall'],good['mono']), now):
                raise ValueError()
            age = now[1]-good['mono']
            result['age_seconds'] = round(age,3)
        except Exception:
            self.fail('CLOCK_DISCONTINUITY')
            result['reason'] = self.failure
            result['last_good'] = dict(good['summary'])
            return result
        if self.failure:
            result['last_good'] = dict(good['summary'])
            return result
        if age > self.max_age:
            result.update(status='STALE', reason='AGE_LIMIT', last_good=dict(good['summary']))
            return result
        quote_age = now[0]-good['quote_utc'] if good['quote_utc'] is not None else None
        result.update(status='FRESH', reason=None, current=dict(good['summary']),
                      quote_status=('UNAVAILABLE' if quote_age is None else
                                    'FRESH' if abs(quote_age)<=self.max_age else 'STALE'))
        return result
