"""Observed MT5 market coordinates, explicitly NOT verified UTC or fresh data.

The owner opted to accept delayed/cached quotes. This counter reports changes in
the observed tick, not receipt events from the server and not proof of freshness.
Nothing in this module sends orders or persists a monotonic clock across restart.
"""
import math
import uuid

from .time_profile import market_tick_raw_seconds

RAW_DATA_WAIT = 'MT5_RAW_WAIT_DATA'


class MT5DisplayClock:
    """Explicit process-local display alignment, never an execution/time proof."""
    def __init__(self,monotonic):
        self.monotonic=monotonic
        self.anchor=None
        self.connected=False
        self.last_mono=None

    def connection(self,connected):
        self.connected=bool(connected)
        if not connected:self.anchor=None;self.last_mono=None

    def align(self,tick):
        stamp=market_tick_raw_seconds(tick)
        mono=float(self.monotonic())
        if not math.isfinite(mono) or mono<0:raise ValueError('INVALID_MONOTONIC_CLOCK')
        self.anchor=(stamp,mono)
        self.last_mono=mono
        self.connected=True
        return self.view()

    def view(self):
        result=dict(status='UNALIGNED' if self.connected else 'DISCONNECTED',broker_time=None,
                    estimated_broker_time=None,elapsed_seconds=None,timestamp_semantics='BROKER_RAW',
                    verified_utc=False,source_connected=self.connected)
        if self.anchor is None:return result
        mono=float(self.monotonic())
        if not math.isfinite(mono) or mono<self.anchor[1] or (self.last_mono is not None and mono<self.last_mono):
            self.anchor=None;self.last_mono=None
            return result
        self.last_mono=mono
        stamp,start=self.anchor
        return dict(result,status='ALIGNED',broker_time=stamp,
                    estimated_broker_time=stamp+mono-start,elapsed_seconds=mono-start)


class RawMarketTime:
    def __init__(self, monotonic):
        self.monotonic = monotonic
        self.instance = uuid.uuid4().hex
        self.sequence = 0
        self.fingerprint = None
        self.last_observed_monotonic = None
        self.last_clock = None
        self.broker_time = None
        self.available = False
        self.connected = False
        self.reason = RAW_DATA_WAIT

    def invalidate(self, *, disconnected=False):
        self.available = False
        if disconnected:self.connected = False
        self.reason = RAW_DATA_WAIT

    def observe(self, tick):
        stamp = market_tick_raw_seconds(tick)
        bid,ask=float(tick.bid),float(tick.ask)
        if not math.isfinite(bid) or not math.isfinite(ask) or bid<=0 or ask<bid:
            raise ValueError('Invalid executable quote')
        extras=[]
        for key in ('last','volume','volume_real','flags'):
            value=getattr(tick,key,None)
            value=value.item() if hasattr(value,'item') else value
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float))
                                      or not math.isfinite(value) or value<0):
                raise ValueError('Invalid native tick identity')
            extras.append(value)
        mono = float(self.monotonic())
        if not math.isfinite(mono) or mono < 0 or (self.last_clock is not None and mono < self.last_clock):
            self.invalidate()
            raise ValueError('INVALID_MONOTONIC_CLOCK')
        self.last_clock = mono
        fingerprint = (stamp,bid,ask,*extras)
        changed=fingerprint != self.fingerprint
        if not self.connected or changed:
            self.sequence += 1
        if changed:
            self.last_observed_monotonic = mono
        self.fingerprint = fingerprint
        self.broker_time = stamp
        self.available = True
        self.connected = True
        self.reason = None
        return stamp

    def view(self):
        elapsed = None
        if self.last_observed_monotonic is not None:
            mono = float(self.monotonic())
            if math.isfinite(mono) and mono >= self.last_observed_monotonic:
                elapsed = mono - self.last_observed_monotonic
        return dict(instance=self.instance, sequence=self.sequence, available=self.available,connected=self.connected,
                    broker_time=self.broker_time, seconds_since_tick_change=elapsed,
                    freshness_policy='ACCEPT_LAST_OBSERVED', timestamp_semantics='BROKER_RAW')


def closed_pair(tick, rows):
    """Use native bar 0 and bar 1. Never synthesize missing minutes or use PC time."""
    if rows is None or len(rows) != 2:
        raise OSError('Raw M1 pair unavailable')
    values = []
    for row in rows:
        stamp = row['time'].item() if hasattr(row['time'], 'item') else row['time']
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0 or stamp % 60:
            raise ValueError('Invalid raw M1 timestamp')
        prices = [float(row[key]) for key in ('open', 'high', 'low', 'close')]
        op, hi, lo, close = prices
        if not all(math.isfinite(v) and v > 0 for v in prices) or not lo <= min(op, close) <= max(op, close) <= hi:
            raise ValueError('Invalid raw M1 OHLC')
        values.append(int(stamp))
    if values[0] >= values[1]:
        raise ValueError('Raw M1 order or duplicate invalid')
    if values[1] != int(market_tick_raw_seconds(tick) // 60) * 60:
        raise OSError('Raw tick and M1 observation disagree')
    return rows[0], values[1]
