"""Capability-limited MT5 market reader. No order, position or trade calls; identity/currency only from account metadata."""
from copy import deepcopy
from types import SimpleNamespace
from .native_time import NativeUTCVerifier
from .time_safety import TimeSafety
from .broker import Spec
from .settings import number

class MarketReader:
    def __init__(self,api,*,symbol,clock,monotonic,safety=None,expected_account=None):
        if symbol!='XAUUSD.s':raise ValueError('Exact configured XAUUSD.s required')
        # Capture only these read capabilities; never retain the native module/broker.
        self._account_info=api.account_info;self._expected_account=expected_account
        self._info=api.symbol_info;self._tick=api.symbol_info_tick;self._bars=api.copy_rates_from_pos
        self.symbol=symbol;self.clock=clock;self.monotonic=monotonic;self.tf=api.TIMEFRAME_M1
        self.safety=safety or TimeSafety(clock=clock,monotonic=monotonic)
        proxy=SimpleNamespace(symbol_info_tick=self._tick,copy_rates_from_pos=self._bars,TIMEFRAME_M1=self.tf)
        self.verifier=NativeUTCVerifier(SimpleNamespace(api=proxy,symbol=symbol,monotonic=monotonic,max_age=5),self.safety)
        self.safety.semantics_verifier=self.verifier
        self.next_check=0;self.last=None;self.reason='WAIT_FRESH_MT5'
    def read(self):
        self.safety.poll_refresh()
        if self.monotonic()>=self.next_check:self.safety.request_refresh();self.next_check=self.monotonic()+30
        reason=self.safety.check()
        if reason:self.reason=reason;raise OSError(reason)
        account=self._account_info()
        if account is None:raise OSError('MT5_SOURCE_UNAVAILABLE')
        identity=str(account.login)+'@'+account.server
        if account.currency!='UST' or account.server!='Bybit-Live-6':raise ValueError('Unsupported source currency/server')
        if self._expected_account is None:self._expected_account=identity
        if identity!=self._expected_account:raise OSError('MT5_SOURCE_IDENTITY_CHANGED')
        before=self.monotonic();info=self._info(self.symbol);tick=self._tick(self.symbol);bars=self._bars(self.symbol,self.tf,0,120)
        now=self.clock()
        if info is None or not info.visible or tick is None or bars is None or len(bars)<2:raise OSError('WAIT_FRESH_MT5')
        stamp=number(tick.time_msc)/1000;bid=number(tick.bid);ask=number(tick.ask)
        if not 0<=stamp-number(tick.time)<1 or not -2<=now-float(stamp)<=5 or not 0<=self.monotonic()-before<=2:raise OSError('STALE_MT5')
        if bid<=0 or ask<bid:raise ValueError('Invalid quote')
        spec=dict(symbol=self.symbol,currency='UST',profit_currency=str(info.currency_profit),calc_mode=str(info.trade_calc_mode),
            contract_size=str(info.trade_contract_size),tick_size=str(info.trade_tick_size),tick_value_profit=str(info.trade_tick_value_profit),tick_value_loss=str(info.trade_tick_value_loss),
            volume_min=str(info.volume_min),volume_step=str(info.volume_step),volume_max=str(info.volume_max),point=str(info.point),stops_level=str(info.trade_stops_level),freeze_level=str(info.trade_freeze_level))
        Spec.load(spec)
        if self.last is not None and int(tick.time_msc)<self.last['tick_msc']:raise OSError('OUT_OF_ORDER_MT5')
        candles=[]
        for b in bars:
            t=float(number(b['time']))
            if t<=0 or t%60:raise ValueError('Invalid UTC candle')
            row=dict(utc_time=t,**{k:str(number(b[k])) for k in ('open','high','low','close')})
            if number(row['low'])<=0 or number(row['low'])>min(number(row['open']),number(row['close'])) or number(row['high'])<max(number(row['open']),number(row['close'])):raise ValueError('Invalid OHLC')
            candles.append(row)
        candles.sort(key=lambda b:b['utc_time']);current=int(float(stamp)//60)*60
        if len({b['utc_time'] for b in candles})!=len(candles) or [b['utc_time'] for b in candles[-2:]]!=[current-60,current]:raise OSError('WAIT_FRESH_M1')
        self.last=dict(source='MT5_READ_ONLY',symbol=self.symbol,timestamp_semantics='VERIFIED',status='FRESH',timestamp=float(stamp),tick_msc=int(tick.time_msc),
            quote=dict(bid=str(bid),ask=str(ask),utc_time=float(stamp)),candles=candles,spec=spec,
            candle=dict(id=int(candles[-2]['utc_time']),open=candles[-2]['open'],close=candles[-2]['close']))
        self.reason=None;return deepcopy(self.last)
    def view(self):
        result=deepcopy(self.last) if self.last else dict(source='MT5_READ_ONLY',symbol=self.symbol,timestamp_semantics='UNVERIFIED',quote={},candles=[],timestamp=None,status='WAITING')
        age=None if result['timestamp'] is None else self.clock()-result['timestamp']
        result.update(age_seconds=age,status='FRESH' if self.reason is None and age is not None and -2<=age<=5 else 'WAITING',reason=self.reason)
        return result
