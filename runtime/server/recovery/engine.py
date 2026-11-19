"""Deterministic simulated order ledger; all distances are raw price units.

No clock, network, MT5 import, or financial execution. Broker events are supplied
explicitly by tests/a future adapter. A recorded order is only a simulation intent.
"""
from decimal import Decimal, InvalidOperation
from copy import deepcopy
from .volume_schedule import volume_for_slot

D = Decimal
SL = D('0.70')
MIN_INITIAL_SL = D('0.60')


def number(value):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid number')
    if not result.is_finite():
        raise ValueError('Non-finite number')
    return result


def sign(side):
    if side not in ('BUY', 'SELL'):
        raise ValueError('Side must be BUY or SELL')
    return D(1) if side == 'BUY' else D(-1)


def other(side):
    return 'SELL' if side == 'BUY' else 'BUY'


class Engine:
    def __init__(self, state=None):
        self.data = deepcopy(state) if state is not None else {
            'mode': 'simulation', 'live_trading': False, 'ranges': [],
            'positions': [], 'orders': [], 'loss_history': [], 'active_range': None,
            'sequence': 0, 'events': [], 'seen_events': {},
            'settings': {'initial_sl_distance': '0.70', 'reentry_distance': '0.70'},
        }

    def validate_distance_policy(self):
        """Reject legacy invalid settings without rewriting prices or positions."""
        for settings in [self.data.get('settings',{})]+list(self.data.get('ranges',[])):
            if number(settings.get('initial_sl_distance','0.70')) < MIN_INITIAL_SL:
                raise ValueError('Initial SL distance must be at least 0.60; legacy settings require explicit replacement')
            if number(settings.get('reentry_distance','0.70')) <= 0:
                raise ValueError('Reentry distance must be positive')

    def state(self):
        result = deepcopy(self.data)
        result.pop('seen_events', None)
        result['can_cancel_ranges'] = not result['positions']
        return result

    def _id(self, prefix):
        self.data['sequence'] += 1
        return f'{prefix}-{self.data["sequence"]}'

    def _range(self, rid):
        return next((r for r in self.data['ranges'] if r['id'] == rid), None)

    def _order(self, rid, side, price, kind):
        self.validate_distance_policy()
        price = number(price)
        sl = price - sign(side) * number(self._range(rid).get('initial_sl_distance', '0.70'))
        if price <= 0 or sl <= 0:
            raise ValueError('Price and SL must be positive')
        order = {'id': self._id('order'), 'range_id': rid, 'side': side,
                 'price': str(price), 'sl': str(sl), 'volume': str(volume_for_slot(min(24, self._range(rid).get('full_sl_count', 0) + (2 if kind == 'reverse' else 1)))),
                 'kind': kind, 'status': 'simulated_pending'}
        self.data['orders'].append(order)
        return order

    def _archive(self, r, reason):
        if r['closed_trades']:
            self.data['loss_history'].append({
                'range_id': r['id'], 'session': r['session'], 'reason': reason,
                'loss': r['loss'], 'realized_net': r['realized_net'],
                'closed_trades': r['closed_trades'], 'full_sl_count': r.get('full_sl_count', 0),
            })
        r.update(loss='0', realized_net='0', closed_trades=0)

    def replace_ranges(self, ranges, initial_sl_distance='0.70', reentry_distance='0.70'):
        # Replacing a range with a live position has not been specified. Fail closed.
        if self.data['positions']:
            raise ValueError('Cannot replace ranges while positions are open')
        if not 1 <= len(ranges) <= 2:
            raise ValueError('Choose one or two ranges')
        initial_sl_distance, reentry_distance = number(initial_sl_distance), number(reentry_distance)
        if initial_sl_distance < MIN_INITIAL_SL:
            raise ValueError('Initial SL distance must be at least 0.60')
        if reentry_distance <= 0:
            raise ValueError('Reentry distance must be positive')
        ids = [r['id'] for r in ranges]
        if len(set(ids)) != len(ids) or any(i not in ('1', '2') for i in ids):
            raise ValueError('Range IDs must be unique: 1 or 2')
        prepared = []
        for r in ranges:
            price, volume = number(r['price']), number(r['volume'])
            if volume != volume_for_slot(1):
                raise ValueError('Initial volume is fixed by approved schedule at 0.01')
            if volume <= 0 or price <= 0 or price - sign(r['side']) * initial_sl_distance <= 0:
                raise ValueError('Invalid price, volume or stop')
            prepared.append({'id': r['id'], 'price': str(price), 'side': r['side'],
                             'volume': str(volume), 'loss': '0', 'realized_net': '0',
                             'closed_trades': 0, 'session': 0, 'full_sl_count': 0, 'run_state': 'active',
                             'initial_sl_distance': str(initial_sl_distance), 'reentry_distance': str(reentry_distance)})
        for r in self.data['ranges']:
            self._archive(r, 'replaced')
        self.data['orders'] = []
        self.data['ranges'] = prepared
        self.data['settings'] = {'initial_sl_distance': str(initial_sl_distance), 'reentry_distance': str(reentry_distance)}
        self.data['active_range'] = None
        for r in prepared:
            self._order(r['id'], r['side'], r['price'], 'primary')

    def cancel_ranges(self):
        if self.data['positions']:
            raise ValueError('Cancel ranges disabled while any position is open')
        for r in self.data['ranges']:
            self._archive(r, 'cancelled')
        self.data.update(ranges=[], orders=[], active_range=None)

    def fill(self, event_id, order_id, entry):
        """Inject a simulated fill, with actual execution price (slippage allowed)."""
        self.validate_distance_policy()
        payload = ['fill', order_id, str(entry)]
        if self._duplicate(event_id, payload):
            return
        order = next((o for o in self.data['orders'] if o['id'] == order_id), None)
        if not order:
            raise ValueError('Unknown or already filled order')
        if any(p['range_id'] == order['range_id'] for p in self.data['positions']):
            raise ValueError('Concurrent fill in the same range requires explicit broker reconciliation')
        if self._range(order['range_id']).get('run_state', 'active') != 'active':
            raise ValueError('Fill during cancellation/manual transition: requires explicit broker reconciliation')
        entry = number(entry)
        sl = entry - sign(order['side']) * number(self._range(order['range_id']).get('initial_sl_distance', '0.70'))
        if entry <= 0 or sl <= 0:
            raise ValueError('Invalid actual fill')
        r = self._range(order['range_id'])
        if r['session'] == 0:
            r['session'] = 1
        # Display-only last activation; accounting is independent for each range.
        self.data['active_range'] = r['id']
        self.data['orders'].remove(order)
        self.data['positions'].append({
            'id': self._id('position'), 'range_id': r['id'], 'side': order['side'],
            'entry': str(entry), 'sl': str(sl), 'volume': order['volume'],
            'stop_stage': 0, 'original_order_sl': order['sl'],
        })
        # Once a reentry fills the old opposite is replaced, never while merely waiting.
        self.data['orders'] = [o for o in self.data['orders'] if o['range_id'] != r['id']]
        # Final allowed position (slot 24): never arm a 25th opposite entry.
        # Old real tickets must still be cancelled/reconciled by a live adapter.
        if r.get('full_sl_count', 0) < 23:
            self._order(r['id'], other(order['side']), sl, 'reverse')
        self._record(event_id, payload)

    def quote(self, bid, ask):
        self.validate_distance_policy()
        bid, ask = number(bid), number(ask)
        if bid <= 0 or ask < bid:
            raise ValueError('Invalid BID/ASK')
        for p in self.data['positions']:
            entry, direction = number(p['entry']), sign(p['side'])
            executable = bid if p['side'] == 'BUY' else ask
            progress = (executable - entry) * direction
            stage = 2 if progress >= D('.50') else 1 if progress >= D('.25') else 0
            if stage > p['stop_stage']:
                target = entry if stage == 2 else entry - direction * D('.25')
                # A short configurable initial SL may already protect better than
                # the stage-1 target. Never loosen it or call an unapplied stage
                # confirmed: an unchanged initial stop remains an initial SL.
                improvement = (target - number(p['sl'])) * direction
                if improvement > 0:
                    p['stop_stage'] = stage
                    p['sl'] = str(target)
                    p.pop('stop_classification_pending', None)
                elif improvement == 0 and stage == 1 and p['stop_stage'] == 0:
                    # initial distance .25: threshold reached but stop never moved.
                    # User has not defined whether this counts as full/support SL.
                    p['stop_classification_pending'] = 'stage1_equals_initial'
        # Does not fabricate fills or SL execution from quotes. Closing is a separate event.

    def close(self, event_id, position_id, reason, realized_net):
        """Inject confirmed close; realized_net includes fees, swap and commission.

        reason: manual, initial_sl, support_sl. Support reentry uses the SL LEVEL,
        not the slipped close price. No profit target or ladder.
        """
        self.validate_distance_policy()
        payload = ['close', position_id, reason, str(realized_net)]
        if self._duplicate(event_id, payload):
            return
        p = next((p for p in self.data['positions'] if p['id'] == position_id), None)
        if not p:
            raise ValueError('Position already closed or unknown; no SL recalculation')
        if reason not in ('manual', 'initial_sl', 'support_sl'):
            raise ValueError('Unknown close reason')
        if reason != 'manual' and p.get('stop_classification_pending'):
            raise ValueError('Equal initial/support stop classification unresolved; explicit policy required')
        if reason == 'support_sl' and p['stop_stage'] == 0:
            raise ValueError('Support stop was not activated')
        if reason == 'initial_sl' and p['stop_stage'] != 0:
            raise ValueError('Initial stop is no longer active')
        if reason == 'support_sl':
            reentry = number(p['sl']) + sign(p['side']) * number(self._range(p['range_id']).get('reentry_distance', '0.70'))
            if reentry <= 0 or reentry - sign(p['side']) * number(self._range(p['range_id']).get('initial_sl_distance', '0.70')) <= 0:
                raise ValueError('Reentry price or SL is invalid; simulation paused')
        pnl = number(realized_net)
        r = self._range(p['range_id'])
        r['loss'] = str(number(r['loss']) + max(D(0), -pnl))
        r['realized_net'] = str(number(r['realized_net']) + pnl)
        r['closed_trades'] += 1
        self.data['positions'].remove(p)
        if reason == 'initial_sl':
            r['full_sl_count'] = r.get('full_sl_count', 0) + 1
            if r['full_sl_count'] >= 24:
                r['run_state'] = 'cancellation_pending'
                for pending in self.data['orders']:
                    if pending['range_id'] == r['id']:
                        pending['status'] = 'simulated_cancel_requested'
        if reason == 'support_sl':
            self._order(r['id'], p['side'], number(p['sl']) + sign(p['side']) * number(r.get('reentry_distance', '0.70')), 'reentry')
        # A support/manual close does not consume the projected full-stop slot.
        # Keep the opposite PRICE but reset its simulated volume to current slot.
        # A live controller must reconcile/replace its broker volume explicitly.
        if reason != 'initial_sl':
            for pending in self.data['orders']:
                if pending['range_id'] == r['id']:
                    pending['volume'] = str(volume_for_slot(r.get('full_sl_count', 0) + 1))
        # Manual and initial SL retain the existing opposite trigger.
        self._record(event_id, payload)

    def close_cancel(self, event_id, rid, realized_net):
        """Simulation-only close + cancel its range; not a market order API."""
        payload = ['close_cancel', rid, str(realized_net)]
        if self._duplicate(event_id, payload):
            return
        r = self._range(rid)
        positions = [p for p in self.data['positions'] if p['range_id'] == rid]
        if not r or len(positions) != 1:
            raise ValueError('Expected one current position in this range')
        self.close(event_id + ':close', positions[0]['id'], 'manual', realized_net)
        self._archive(r, 'closed_and_cancelled')
        self.data['ranges'].remove(r)
        self.data['orders'] = [o for o in self.data['orders'] if o['range_id'] != rid]
        if self.data['active_range'] == rid:
            self.data['active_range'] = None
        self._record(event_id, payload)

    def confirm_limit_cancellations(self, rid, cancelled_order_ids):
        """Simulation event only: caller supplies confirmed cancellations, not requests.

        Real transport must verify broker history and reconcile possible concurrent
        fills first. Empty snapshot alone must never call this method.
        """
        r = self._range(rid)
        if not r or r.get('run_state') != 'cancellation_pending':
            raise ValueError('Range not awaiting limit cancellations')
        if any(p['range_id'] == rid for p in self.data['positions']):
            raise ValueError('Position still open; manual transition unconfirmed')
        orders = [o for o in self.data['orders'] if o['range_id'] == rid]
        ids = {o['id'] for o in orders}
        if set(cancelled_order_ids) != ids:
            raise ValueError('All exact remaining order cancellations must be confirmed')
        self.data['orders'] = [o for o in self.data['orders'] if o['range_id'] != rid]
        r['run_state'] = 'manual'

    def _duplicate(self, event_id, payload):
        old = self.data['seen_events'].get(event_id)
        if old is not None:
            if old != payload:
                raise ValueError('Event ID reused with different payload')
            return True
        return False

    def _record(self, event_id, payload):
        self.data['seen_events'][event_id] = payload
        self.data['events'].append({'id': event_id, 'event': payload})
