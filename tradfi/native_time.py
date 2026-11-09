"""Read-only validation of explicitly configured MT5 market timestamps."""
import math
import threading
from .time_profile import market_timestamp_to_utc, market_tick_to_utc, validate_market_time_offset, TimeProfileError

class NativeUTCVerifier:
    def __init__(self,broker,safety):
        self.broker,self.safety=broker,safety
        self.offset=validate_market_time_offset({'market_time_offset_seconds':getattr(broker,'market_time_offset_seconds',0)})
        self.owner=threading.get_ident()
        self.observation=None
        self.reason='MT5_UTC_WAIT_REFERENCE'
    def __call__(self):
        self.observation=None
        # External HTTPS measurement workers must never call the MT5 API.
        if threading.get_ident()!=self.owner:return False
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
            current=int(stamp//60)*60
            if sorted(times)!=[current-60,current] or current!=int(now//60)*60:
                self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
            self.reason=None;return True
        except Exception:
            self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
