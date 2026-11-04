"""Read-only validation of MT5's documented UTC epoch contract; no offset guessing."""
import math
import threading

class NativeUTCVerifier:
    def __init__(self,broker,safety):
        self.broker,self.safety=broker,safety
        self.owner=threading.get_ident()
        self.reason='MT5_UTC_WAIT_REFERENCE'
    def __call__(self):
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
            raw=getattr(tick,'time_msc',None);seconds=getattr(tick,'time',None)
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in (raw,seconds)):
                self.reason='MT5_UTC_INVALID_TIMESTAMP';return False
            stamp=raw/1000
            if not 0<=stamp-seconds<1:self.reason='MT5_UTC_INVALID_TIMESTAMP';return False
            if stamp>now+2:self.reason='MT5_UTC_CONTRACT_MISMATCH';return False
            if now-stamp>b.max_age:self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
            times=[]
            for bar in bars:
                value=bar['time'];value=value.item() if hasattr(value,'item') else value
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 or value%60:
                    self.reason='MT5_UTC_INVALID_TIMESTAMP';return False
                times.append(value)
            current=int(stamp//60)*60
            if sorted(times)!=[current-60,current] or current!=int(now//60)*60:
                self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
            self.reason=None;return True
        except Exception:
            self.reason='MT5_UTC_WAIT_FRESH_DATA';return False
