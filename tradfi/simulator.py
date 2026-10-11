"""Deterministic broker fixture. The specification below is ARTIFICIAL, not Bybit metadata."""
from copy import deepcopy
from decimal import Decimal as D


ARTIFICIAL_SPEC = dict(symbol='XAUUSD.s', currency='USD', contract_size='200', tick_size='0.01',
    tick_value_profit='2', tick_value_loss='2', volume_min='0.03', volume_step='0.03',
    volume_max='12', point='0.01', stops_level='1', freeze_level='1',
    profit_currency='USD', calc_mode='SIMULATED_LINEAR')


class Simulator:
    def __init__(self, spec=None):
        self.data = dict(account='SIMULATOR_ONLY', hedging=True, history_complete=True,
            spec=deepcopy(spec or ARTIFICIAL_SPEC), now=0,
            quote={'bid': '4000', 'ask': '4000.10'}, candle={'id': 0, 'close': '4000'},
            orders=[], positions=[], deals=[], order_history=[])
        self.commands = []
        self.results = {}
        self.fail_before = set()
        self.fail_after = set()
        self.cancel_race = None

    def snapshot(self):
        return deepcopy(self.data)

    def result(self, cid):
        return deepcopy(self.results.get(cid, {'status': 'UNKNOWN'}))

    def profit(self, side, size, entry, close):
        sign = D(1) if side == 'BUY' else D(-1)
        return sign * (close - entry) * size * D(self.data['spec']['contract_size'])

    def candle(self, cid, close='4000'):
        self.data['candle'] = {'id': cid, 'close': close}
        self.data['now'] = cid

    def quote(self, bid, ask):
        self.data['quote'] = {'bid': str(bid), 'ask': str(ask)}

    def submit(self, cmd):
        if any(x['id'] == cmd['id'] for x in self.commands):
            raise AssertionError('Duplicate mutation intent')
        self.commands.append(deepcopy(cmd))
        kind = cmd['kind']
        if kind in self.fail_before:
            raise TimeoutError('simulated lost request / unknown')
        if kind in {'STOP', 'MARKET'}:
            oid = 'o-' + cmd['id']
            order = dict(id=oid, command_id=cmd['id'], side=cmd['side'], status='PENDING',
                         size=cmd['size'], price=cmd['price'], sl=cmd['sl'], tp=cmd['tp'], owned=True)
            self.data['orders'].append(order)
            self.results[cmd['id']] = {'status': 'ACCEPTED', 'order_id': oid}
            if kind == 'MARKET':
                self.fill(cmd['side'], cmd['price'])
        elif kind == 'CANCEL':
            order = next((x for x in self.data['orders'] if x['id'] == cmd['order_id']), None)
            if order and self.cancel_race == order['side']:
                self.cancel_race = None
                self.fill(order['side'], order['price'])
                self.results[cmd['id']] = {'status': 'UNKNOWN'}
            else:
                if order:
                    self.data['orders'].remove(order)
                    order['status'] = 'CANCELLED'
                    self.data['order_history'].append(order)
                self.results[cmd['id']] = {'status': 'DONE'}
        elif kind == 'MODIFY':
            p = next(x for x in self.data['positions'] if x['id'] == cmd['position_id'])
            p.update(sl=cmd['sl'], tp=cmd['tp'])
            self.results[cmd['id']] = {'status': 'DONE'}
        else:
            raise AssertionError('Unsupported / forbidden mutation: ' + kind)
        if kind in self.fail_after:
            self.results.pop(cmd['id'], None)
            raise TimeoutError('simulated lost response')

    def fill(self, side, price=None, size=None):
        order = next(x for x in self.data['orders'] if x['side'] == side)
        self.data['orders'].remove(order)
        order['status'] = 'FILLED'
        self.data['order_history'].append(deepcopy(order))
        pid = 'p-' + order['id']
        price, size = str(price or order['price']), str(size or order['size'])
        self.data['positions'].append(dict(id=pid, side=side, entry=price, size=size,
                                          sl=order['sl'], tp=order['tp'], owned=True))
        self.data['deals'].append(dict(id='in-' + pid, position_id=pid, order_id=order['id'],
            command_id=order['command_id'], entry='IN', side=side, volume=size, price=price,
            reason='EXPERT', profit='0', time=self.data['now']))
        return pid

    def close(self, reason='SL', price=None, profit=None):
        p = self.data['positions'].pop(0)
        price = str(price if price is not None else p['sl' if reason == 'SL' else 'tp'])
        pnl = self.profit(p['side'], D(p['size']), D(p['entry']), D(price)) if profit is None else D(profit)
        self.quote(D(price) - D('.05'), D(price) + D('.05'))
        self.data['deals'].append(dict(id='out-' + p['id'], position_id=p['id'], order_id='exit-' + p['id'],
            command_id='', entry='OUT', side=p['side'], volume=p['size'], price=price,
            reason=reason, profit=str(pnl), commission='-999', swap='-888', sl=p['sl'], tp=p['tp'],
            time=self.data['now']))
        return p
