"""Exact user triggers anchored to one received quote, not an M1 close."""
from dataclasses import dataclass
from decimal import Decimal as D
from ..broker import Spec
from ..settings import number


@dataclass(frozen=True)
class TriggerPlan:
    buy: str | None
    sell: str | None
    bid: str
    ask: str
    basis: str

    def prices(self):
        return {side: value for side, value in [('BUY', self.buy), ('SELL', self.sell)] if value is not None}


def user_triggers(spec: Spec, quote: dict, *, ranges=None, prices=None):
    """BUY stop triggers on Ask, SELL on Bid, consistent with MT5Broker._request.

    Exactly one representation. Range mode always has both sides; explicit prices
    may have one or both. No silent tick rounding or crossed-level decision here.
    The executor must revalidate the received immutable levels before sending.
    """
    if (ranges is None) == (prices is None):
        raise ValueError('Choose ranges or exact prices')
    bid, ask = number(quote['bid']), number(quote['ask'])
    if bid <= 0 or ask < bid:
        raise ValueError('Invalid quote')
    if ranges is not None:
        if not isinstance(ranges, dict) or set(ranges) != {'BUY', 'SELL'}:
            raise ValueError('Ranges require BUY and SELL')
        distances={side:number(value) for side,value in ranges.items()}
        if any(value <= 0 or value % spec.tick_size for value in distances.values()):
            raise ValueError('Ranges must be positive whole ticks')
        resolved={'BUY':spec.price(ask+distances['BUY']), 'SELL':spec.price(bid-distances['SELL'])}
        basis='RECEIVED_ASK_BUY_BID_SELL'
    else:
        if not isinstance(prices,dict) or not prices or not set(prices) <= {'BUY','SELL'}:
            raise ValueError('Exact prices require one or two sides')
        resolved={side:spec.price(value) for side,value in prices.items()}
        basis='EXACT_USER_PRICES'
    if len(resolved)==2 and resolved['BUY'] <= resolved['SELL']:
        raise ValueError('BUY must be above SELL')
    return TriggerPlan(buy=str(resolved['BUY']) if 'BUY' in resolved else None,
                       sell=str(resolved['SELL']) if 'SELL' in resolved else None,
                       bid=str(bid),ask=str(ask),basis=basis)


def recovery_triggers(spec, anchor, previous_side, reason, *, after_sl, continuation, opposite):
    """After SL symmetric around actual close; after TP directional continuation.

    Example BUY TP at 4140: continuation=1/opposite=3 -> BUY4141, SELL4137.
    SELL is its exact mirror. This planner never interprets time or changes debt.
    """
    anchor=spec.price(anchor)
    if previous_side not in {'BUY','SELL'} or reason not in {'SL','TP'}:
        raise ValueError('Confirmed SL/TP and position side required')
    distances=[number(x) for x in (after_sl,continuation,opposite)]
    if any(x<=0 or x%spec.tick_size for x in distances):
        raise ValueError('Distances must be positive whole ticks')
    after_sl,continuation,opposite=distances
    buy=sell=after_sl
    if reason=='TP':
        buy,sell=(continuation,opposite) if previous_side=='BUY' else (opposite,continuation)
    return {'BUY':str(spec.price(anchor+buy)), 'SELL':str(spec.price(anchor-sell))}
