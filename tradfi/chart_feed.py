"""Native chart observations. Raw broker time never acquires UTC by implication."""
from copy import deepcopy
import uuid
import math
from .settings import number
from .time_profile import TEST_PROFILE,server_to_utc


class ChartFeed:
    version='mt5_chart_v1'
    def __init__(self,broker,*,clock,monotonic):
        self.broker=broker;self.clock=clock;self.monotonic=monotonic
        self.instance=uuid.uuid4().hex;self.sequence=0;self.last=None;self.last_mono=None
    def capture(self,snapshot):
        self.sequence+=1
        b=self.broker;start=self.monotonic()
        raw=b.api.copy_rates_from_pos(b.symbol,b.api.TIMEFRAME_M1,0,120)
        if raw is None or not len(raw):raise OSError('Chart candles unavailable')
        b._context(stage='chart_snapshot_complete')
        raw_mode=b.config.get('market_time_mode')=='mt5_raw_v1'
        verified=False if raw_mode else b._timestamp_semantics_verified()
        bars=[]
        for r in raw:
            stamp=r['time'].item() if hasattr(r['time'],'item') else r['time']
            if number(stamp)<=0:raise ValueError('Invalid raw candle timestamp')
            utc=None
            if verified:
                utc=float(number(stamp)) if b.time_profile==TEST_PROFILE else b.market_time_to_utc(float(number(stamp)))
                if utc<=0 or utc%60:raise ValueError('Invalid native M1 timestamp')
            row={k:str(number(r[k])) for k in ('open','high','low','close')}
            if number(row['low'])<=0 or number(row['high'])<max(number(row['open']),number(row['close'])) or number(row['low'])>min(number(row['open']),number(row['close'])):
                raise ValueError('Invalid OHLC')
            bars.append(dict(raw_time=stamp,utc_time=utc,**row))
        if len({bar['raw_time'] for bar in bars})!=len(bars):raise ValueError('Duplicate native candles')
        if raw_mode and any(a['raw_time'] >= z['raw_time'] for a,z in zip(bars,bars[1:])):
            raise ValueError('Unordered native candles')
        bars.sort(key=lambda r:r['raw_time'])
        if raw_mode:
            tick=float(snapshot['quote']['broker_time'])
            if not math.isfinite(tick) or tick<=0:raise ValueError('Invalid broker tick')
            for bar in bars:
                if bar['raw_time'] % 60 or bar['raw_time'] > tick:raise ValueError('Invalid native M1 timestamp')
                bar.update(broker_time=bar['raw_time'],time_domain='BROKER_RAW',closed=bar is not bars[-1])
        candle_status='UNVERIFIED'
        if verified:
            now=b._quote_clock();latest=max(r['utc_time'] for r in bars)
            if latest>int(now//60)*60:raise ValueError('Future native candle')
            candle_status='STALE' if latest<int(now//60)*60-60 else 'FRESH'
        quote=deepcopy(snapshot['quote']);timestamp=None if raw_mode else quote.get('utc_time')
        if raw_mode:quote['utc_time']=None
        age=None if timestamp is None else max(0,b._quote_clock()-timestamp)
        elapsed=self.monotonic()-start
        status='UNVERIFIED' if timestamp is None or not verified else ('STALE' if age>b.max_age or elapsed>b.max_age or candle_status=='STALE' else 'FRESH')
        value=dict(feed_version=self.version,instance=self.instance,sequence=self.sequence,
            source='INJECTED_MT5' if b.config.get('time_profile')==TEST_PROFILE or getattr(b.api,'is_test_binding',False) else 'MT5',
            symbol=b.symbol,connected=True,status=status,timestamp=timestamp,age_seconds=age,
            timestamp_semantics='VERIFIED' if verified else 'UNVERIFIED',
            candle_status=candle_status,display_time=deepcopy(snapshot.get('mt5_display_time')),quote=quote,candles=bars,observed_local_wall_time=self.clock())
        if raw_mode:
            value.update(status='OBSERVED',candle_status='OBSERVED',timestamp=None,age_seconds=None,
                broker_time=tick,raw_time=tick,timestamp_semantics='BROKER_RAW',time_domain='BROKER_RAW',
                freshness_policy='ACCEPT_LAST_OBSERVED',market_observation=deepcopy(snapshot.get('market_observation')))
        self.last=value;self.last_mono=self.monotonic();return deepcopy(value)
    def disconnected(self):
        value=deepcopy(self.last) if self.last else dict(feed_version=self.version,instance=self.instance,sequence=self.sequence,
            source='INJECTED_MT5' if self.broker.config.get('time_profile')==TEST_PROFILE or getattr(self.broker.api,'is_test_binding',False) else 'MT5',symbol=self.broker.symbol,timestamp=None,quote=None,candles=[],timestamp_semantics='UNVERIFIED')
        value.update(connected=False,status='DISCONNECTED')
        self.last=value
        return deepcopy(value)
    def view(self):
        if not self.last:return self.disconnected()
        value=deepcopy(self.last)
        if value.get('timestamp') is not None and self.last_mono is not None:
            elapsed=max(0,self.monotonic()-self.last_mono)
            value['age_seconds']=self.last.get('age_seconds',0)+elapsed
            if value.get('status')=='FRESH' and value['age_seconds']>self.broker.max_age:value['status']='STALE'
        return value
