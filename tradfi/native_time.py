"""Read-only validation of explicitly configured MT5 market timestamps."""
import math
import threading
from .time_profile import market_timestamp_to_utc, market_tick_to_utc, validate_market_time_offset, TimeProfileError

# A failed verification, never permission to send an order. Only the durable
# controller latch is deferred; the deadline starts at the UTC minute boundary.
M1_TRANSITION_WAIT = 'MT5_UTC_M1_TRANSITION_WAIT'
M1_TRANSITION_SECONDS = 3.0


class NativeUTCVerifier:
    def __init__(self,broker,safety):
        self.broker,self.safety=broker,safety
        self.offset=validate_market_time_offset({'market_time_offset_seconds':getattr(broker,'market_time_offset_seconds',0)})
        self.owner=threading.get_ident()
        self.observation=None
        self.reason='MT5_UTC_WAIT_REFERENCE'
        self._transition=None
        self._last_verified=None
        self.transition_expired=False

    def _m1_transition(self, stamp, times, now, mono):
        """Recognize only the immediately adjacent, coherent M1 handover.

        A missed tick or bars may lag by one minute at the boundary. A gap,
        duplicate, future minute, malformed timestamp, slow read, old quote,
        or missing independent reference is not a handover. Repeated reads and
        UTC refreshes must not restart/extend this minute's monotonic deadline.
        """
        minute=int(now//60)*60
        if (self._last_verified is None
                or self._last_verified[0] != minute-60
                or not 0 < minute-self._last_verified[1] <= self.broker.max_age):
            return False
        tick_minute=int(stamp//60)*60
        pair=sorted(times)
        adjacent=(tick_minute in {minute-60,minute}
                  and pair in ([minute-120,minute-60],[minute-60,minute]))
        age=now-minute
        if not adjacent or not 0<=age<M1_TRANSITION_SECONDS:
            return False
        if self._transition is None or self._transition[0]!=minute:
            self._transition=(minute,mono-age,mono)
        _,started,last=self._transition
        if not math.isfinite(mono) or mono<last or not 0<=mono-started<M1_TRANSITION_SECONDS:
            return False
        self._transition=(minute,started,mono)
        return True

    def acknowledge_transition_timeout(self):
        """Owner calls only after a durable time_block exists."""
        if threading.get_ident()==self.owner:self.transition_expired=False

    def __call__(self):
        # HTTPS workers share this verifier through a shallow safety copy.
        # They must not call MT5 OR mutate the owner's observation/deadline.
        if threading.get_ident()!=self.owner:return False
        self.observation=None
        b=self.broker;start=b.monotonic();reference=self.safety.trusted_utc()
        if reference is None:self.reason='MT5_UTC_WAIT_REFERENCE';return False
        try:
            tick=b.api.symbol_info_tick(b.symbol)
            bars=b.api.copy_rates_from_pos(b.symbol,b.api.TIMEFRAME_M1,0,2)
            end=b.monotonic();now=self.safety.trusted_utc()
            if now is None or not math.isfinite(end-start) or not 0<=end-start<=2:
                self.reason='MT5_UTC_READ_TIMEOUT';return False
            if tick is None or bars is None or len(bars)!=2:
                self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
            def safe(value):
                value=value.item() if hasattr(value,'item') else value
                return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None
            self.observation=dict(raw_tick_time=safe(getattr(tick,'time',None)),raw_tick_time_msc=safe(getattr(tick,'time_msc',None)),raw_m1_times=[safe(b['time']) for b in bars],market_time_offset_seconds=self.offset,reference_utc=now,normalized_tick_utc=None,normalized_m1_utc=[])
            try:
                stamp=market_tick_to_utc(tick,self.offset)
            except TimeProfileError:
                self.reason='MT5_UTC_INVALID_TIMESTAMP';return False
            self.observation['normalized_tick_utc']=stamp
            times=[]
            for bar in bars:
                value=bar['time'];value=value.item() if hasattr(value,'item') else value
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 or value%60:
                    self.reason='MT5_UTC_INVALID_TIMESTAMP';return False
                times.append(market_timestamp_to_utc(value,self.offset))
            self.observation['normalized_m1_utc']=times
            if stamp>now+2:self.reason='MT5_UTC_CONTRACT_MISMATCH';return False
            if now-stamp>b.max_age:self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
            if self._transition is not None:
                _,started,last=self._transition
                if end<last or end-started>=M1_TRANSITION_SECONDS:
                    self.transition_expired=True
                    self._transition=None
            if self.transition_expired:
                self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
            current=int(stamp//60)*60
            if sorted(times)!=[current-60,current] or current!=int(now//60)*60:
                self.reason=(M1_TRANSITION_WAIT if self._m1_transition(stamp,times,now,end)
                             else 'MT5_UTC_WAIT_FRESH_DATA')
                return False
            self._last_verified=(int(now//60)*60,now)
            self._transition=None
            self.reason=None;return True
        except Exception:
            self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
