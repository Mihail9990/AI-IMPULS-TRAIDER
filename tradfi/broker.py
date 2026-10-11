"""Gateway contract and exact symbol validation (all monetary results in account currency)."""
from dataclasses import dataclass
from decimal import Decimal as D, ROUND_FLOOR
from typing import Protocol
from .settings import number


@dataclass(frozen=True)
class Spec:
    symbol: str
    currency: str
    contract_size: D
    tick_size: D
    tick_value_profit: D
    tick_value_loss: D
    volume_min: D
    volume_step: D
    volume_max: D
    point: D
    stops_level: D
    freeze_level: D
    profit_currency: str
    calc_mode: str

    @classmethod
    def load(cls, data):
        strings = {'symbol', 'currency', 'profit_currency', 'calc_mode'}
        obj = cls(**{k: str(data[k]) if k in strings else number(data[k])
                     for k in cls.__dataclass_fields__})
        if obj.symbol != 'XAUUSD.s' or obj.currency != 'USD':
            raise ValueError('Requires XAUUSD.s and USD account for the $1 tolerance')
        positive = ('contract_size', 'tick_size', 'tick_value_profit', 'tick_value_loss',
                    'volume_min', 'volume_step', 'volume_max', 'point')
        if any(getattr(obj, k) <= 0 for k in positive):
            raise ValueError('Incomplete/nonpositive live symbol specification')
        if obj.stops_level < 0 or obj.freeze_level < 0:
            raise ValueError('Invalid stop/freeze levels')
        return obj

    def validate(self, settings):
        size = settings.INITIAL_SIZE
        if not self.volume_min <= size <= self.volume_max or size % self.volume_step:
            raise ValueError('INITIAL_SIZE violates broker volume min/step/max; no rounding')
        for key, value in settings.dump().items():
            if key not in {'INITIAL_SIZE', 'CYCLE_COOLDOWN_SECONDS'} and D(value) % self.tick_size:
                raise ValueError(key + ' violates tick size; no rounding')
        minimum = self.stops_level * self.point
        for key in ('BRANCH1_RANGE', 'BRANCH1_SL_DISTANCE', 'BASE_TP_DISTANCE',
                    'RANGE_AFTER_PARTIAL_TP', 'SL_DISTANCE_AFTER_PARTIAL_TP',
                    'RANGE_AFTER_SL', 'SL_DISTANCE_AFTER_SL'):
            if getattr(settings, key) < minimum:
                raise ValueError(key + ' violates stop level')

    def price(self, value):
        value = number(value)
        if value <= 0 or value % self.tick_size:
            raise ValueError('Exact price violates tick size; no adjustment')
        return value

    def protection(self, side, entry, sl, tp, quote=None, modify=False):
        for p in (entry, sl, tp):
            self.price(p)
        sign = D(1) if side == 'BUY' else D(-1)
        minimum = self.point * (max(self.stops_level, self.freeze_level) if modify else self.stops_level)
        anchor = D(quote['bid' if side == 'BUY' else 'ask']) if quote else entry
        if sign * (anchor - sl) < minimum or sign * (tp - anchor) < minimum:
            raise ValueError('SL/TP violates current stop/freeze distance; original protection retained')
        if sl == anchor or tp == anchor:
            raise ValueError('SL/TP cannot equal executable quote')


class Broker(Protocol):
    def snapshot(self) -> dict: ...
    def submit(self, command: dict) -> None: ...
    def result(self, command_id: str) -> dict: ...
    def profit(self, side: str, size: D, entry: D, close: D) -> D: ...


def target_price(broker, spec, side, size, entry, money):
    """Smallest favorable whole-tick distance covering money by OrderCalcProfit.

    Derived TP rounds outward explicitly; user levels/SL distances are never silently rounded.
    Uses the broker calculator, not contract-size guesses or fixed tick values.
    """
    sign = D(1) if side == 'BUY' else D(-1)
    def at(ticks):
        price = entry + sign * spec.tick_size * ticks
        if price <= 0:
            raise ValueError('Recovery TP is outside positive price space')
        return number(broker.profit(side, size, entry, price))
    high = 1
    while at(high) < money:
        high *= 2
        if high > 2**30:
            raise ValueError('Unable to bracket monetary TP')
    low = 0
    while high - low > 1:
        mid = (high + low) // 2
        if at(mid) >= money:
            high = mid
        else:
            low = mid
    return entry + sign * spec.tick_size * high


def stepped_stop(side, entry, quote, activation, step):
    sign = D(1) if side == 'BUY' else D(-1)
    executable = D(quote['bid' if side == 'BUY' else 'ask'])
    move = sign * (executable - entry)
    if move < activation:
        return None
    steps = ((move - activation) / step).to_integral_value(rounding=ROUND_FLOOR)
    return entry + sign * steps * step
