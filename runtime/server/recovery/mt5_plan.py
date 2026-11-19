"""OFFLINE MT5 request planner. Does not import MT5, connect, or order_send.

Consumes explicit snapshots; yields reviewable templates with symbolic MT5 enum
names. A template is NOT an execution result. Never pass directly to order_send.
"""
from dataclasses import dataclass
from .engine import number, sign, MIN_INITIAL_SL


@dataclass(frozen=True)
class Symbol:
    name: str
    tick_size: str
    point: str
    stops_level: int
    freeze_level: int
    volume_min: str
    volume_max: str
    volume_step: str

    def validate(self):
        if not self.name or self.stops_level < 0 or self.freeze_level < 0:
            raise ValueError('Invalid symbol metadata')
        for value in (self.tick_size, self.point, self.volume_min, self.volume_max, self.volume_step):
            if number(value) <= 0:
                raise ValueError('Invalid symbol metadata')
        if number(self.volume_max) < number(self.volume_min):
            raise ValueError('Invalid volume bounds')


@dataclass(frozen=True)
class Quote:
    bid: str
    ask: str

    def validate(self):
        if number(self.bid) <= 0 or number(self.ask) < number(self.bid):
            raise ValueError('Invalid quote')


def exact_price(price, symbol):
    """Do not silently move user's exact level to another tick."""
    price = number(price)
    if price <= 0 or price % number(symbol.tick_size) != 0:
        raise ValueError('Price must be positive and exactly on symbol tick grid')
    return str(price)


def exact_volume(volume, symbol):
    volume = number(volume)
    if not number(symbol.volume_min) <= volume <= number(symbol.volume_max):
        raise ValueError('Volume outside symbol bounds')
    if volume % number(symbol.volume_step) != 0:
        raise ValueError('Volume not on symbol lot grid')
    return str(volume)


def pending(order, symbol, quote, magic):
    """Plan BUY_STOP/SELL_STOP only. Never convert a crossed level to a market fill."""
    symbol.validate(); quote.validate()
    if not isinstance(magic, int) or magic <= 0:
        raise ValueError('Dedicated positive magic required')
    direction = sign(order['side'])
    price = exact_price(order['price'], symbol)
    sl = exact_price(order['sl'], symbol)
    volume = exact_volume(order['volume'], symbol)
    minimum = number(symbol.point) * symbol.stops_level
    market = number(quote.ask if order['side'] == 'BUY' else quote.bid)
    if (number(price) - market) * direction <= minimum:
        raise ValueError('Trigger crossed or inside minimum stop distance')
    if (number(price) - number(sl)) * direction < MIN_INITIAL_SL:
        raise ValueError('Initial SL distance must be at least 0.60')
    if (number(price) - number(sl)) * direction <= minimum:
        raise ValueError('Stop invalid or inside minimum stop distance')
    return {'action': 'TRADE_ACTION_PENDING', 'symbol': symbol.name,
            'type': 'ORDER_TYPE_BUY_STOP' if order['side'] == 'BUY' else 'ORDER_TYPE_SELL_STOP',
            'price': price, 'sl': sl, 'tp': '0', 'volume': volume, 'magic': magic,
            'type_time': 'ORDER_TIME_GTC', 'type_filling': 'ORDER_FILLING_RETURN',
            'comment': 'recovery:' + str(order['id'])[:20]}


def protect(position, target_sl, symbol, quote, *, expected_ticket, expected_identifier, expected_magic):
    """Protect only a mapped owned position from a fresh snapshot; preserve TP."""
    symbol.validate(); quote.validate()
    for key, expected in [('ticket', expected_ticket), ('identifier', expected_identifier), ('magic', expected_magic)]:
        if not isinstance(expected, int) or expected <= 0 or position.get(key) != expected:
            raise ValueError('Position ownership/identity mismatch')
    if position.get('symbol') != symbol.name:
        raise ValueError('Position symbol mismatch')
    direction = sign(position['side'])
    sl = number(exact_price(target_sl, symbol))
    old = number(position['sl'])
    # Initial actual-fill correction can widen a pre-attached SL. Do not assume
    # that permission for this request overrides an already tightened support SL.
    if position.get('stop_stage', 0) > 0 and old > 0 and (sl-old)*direction < 0:
        raise ValueError('Refusing to weaken confirmed support stop')
    market = number(quote.bid if position['side'] == 'BUY' else quote.ask)
    minimum = number(symbol.point) * max(symbol.stops_level, symbol.freeze_level)
    if (market-sl)*direction <= minimum:
        raise ValueError('SL crossed or inside stop/freeze distance')
    return {'action':'TRADE_ACTION_SLTP', 'symbol':symbol.name,
            'position':expected_ticket, 'sl':str(sl), 'tp':str(number(position['tp']))}


def cancel(order_snapshot, *, expected_ticket, expected_magic, expected_symbol):
    if not isinstance(expected_ticket, int) or expected_ticket <= 0 or expected_magic <= 0:
        raise ValueError('Invalid owned order reference')
    if (order_snapshot.get('ticket') != expected_ticket or order_snapshot.get('magic') != expected_magic
            or order_snapshot.get('symbol') != expected_symbol):
        raise ValueError('Order ownership/identity mismatch')
    return {'action':'TRADE_ACTION_REMOVE', 'order':expected_ticket}


def close_position(position, symbol, quote, *, expected_ticket, expected_identifier,
                   expected_magic, filling):
    """Offline full remaining-position close; filling policy must be explicit."""
    symbol.validate();quote.validate()
    for field,value in (('ticket',expected_ticket),('identifier',expected_identifier),('magic',expected_magic)):
        if not isinstance(value,int) or value<=0 or position.get(field)!=value:
            raise ValueError('Close position ownership mismatch')
    if position.get('symbol')!=symbol.name:raise ValueError('Close symbol mismatch')
    if filling not in ('ORDER_FILLING_FOK','ORDER_FILLING_IOC'):
        raise ValueError('Explicit supported market filling policy required')
    sign(position['side'])
    volume=exact_volume(position['volume'],symbol)
    return {'action':'TRADE_ACTION_DEAL','symbol':symbol.name,'position':expected_ticket,
            'type':'ORDER_TYPE_SELL' if position['side']=='BUY' else 'ORDER_TYPE_BUY',
            'volume':volume,'price':str(number(quote.bid if position['side']=='BUY' else quote.ask)),
            'type_filling':filling,'magic':expected_magic,'comment':'recovery-close'}
