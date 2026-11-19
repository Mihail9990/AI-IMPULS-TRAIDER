"""Explicit initial opposite trigger while the first position is still open.

Not a close/reentry runner. Support SL never moves this initial opposite order.
"""
from copy import deepcopy
from .engine import number, sign, other
from .volume_schedule import volume_for_slot
from .mt5_plan import pending
from .native_entry_observer import NativeEntryObserver
from .native_close_ledger import NativeCloseLedger


class NativeInitialReverse:
    def __init__(self, control):
        self.control = control
        self.session = control.session
        self.observer = NativeEntryObserver(control)

    def arm(self, range_id, *, order_key=None):
        c = self.control
        with self.session._lock:
            c._guard()
            c._require_active_range(c._read(), range_id)
            config = c.strategy_config()
            observed = self.observer.observe(range_id, order_key=order_key)
            if observed['phase'] != 'awaiting_protection':
                raise ValueError('Open confirmed initial position required')
            data = c._read()
            item = self.observer._item(data, range_id, order_key)
            pid = observed['evidence']['position_identifier']
            confirmed = data.get('confirmed_protection', {}).get(str(pid))
            if confirmed is None:
                raise RuntimeError('Confirm initial actual-entry protection before arming reverse')
            # Do not silently operate after an external SL edit. Support stages
            # are allowed but do not change the original reverse trigger level.
            if number(observed['evidence']['position']['sl']) != number(confirmed['sl']):
                raise ValueError('Position stop differs from confirmed protection')
            key = item['key'] + ':initial-reverse'
            records = data.setdefault('reverse_commands', {})
            if key in records:
                return self.reconcile(key)
            if c._unresolved_entry_cancellations(data) or not self.session.ready:
                self.session.ready = False
                raise RuntimeError('Native checkpoint requires reconciliation')
            # A successor needs previous position closes classified before the
            # next lot is known. Never count reverse execution itself as full SL.
            if order_key is not None:
                ledger = NativeCloseLedger(c)
                mappings = list(data['orders']) + list(data.get('reverse_commands', {}).values())
                mappings += list(data.get('reentry_commands', {}).values())
                for previous in mappings:
                    if previous['key'] == item['key'] or previous['intent']['range_id'] != range_id:
                        continue
                    if previous['phase'] != 'verified':
                        raise RuntimeError('Previous range command is unresolved')
                    prior = self.observer.observe(range_id, order_key=previous['key'])
                    if number(prior['evidence']['filled_volume']) == 0:
                        if prior['evidence']['pending_order'] is not None:
                            raise RuntimeError('Previous opposite must be retired before replacement')
                        continue
                    close = ledger.record(previous['broker_order'])
                    if not close['fully_closed'] or close['classification'] is None:
                        raise RuntimeError('Previous position close requires classification')
                data = c._read()
                records = data.setdefault('reverse_commands', {})
            totals = NativeCloseLedger(c).totals().get(range_id, {})
            if totals.get('classification_pending_count', 0):
                raise RuntimeError('Unclassified close prevents scheduled lot calculation')
            count = totals.get('full_sl_count', 0)
            if count >= 23:
                raise RuntimeError('Final scheduled position: no 25th reverse trigger')
            entry = number(observed['evidence']['average_price'])
            distance = number(config['settings']['initial_sl_distance'])
            side = other(item['intent']['side'])
            price = entry - sign(item['intent']['side']) * distance
            # Lot is derived only from classified full-SL closes, never fills
            # or support closes. Owner-approved exception: an already prearmed
            # opposite keeps its volume after a manual/support exit, even at a
            # schedule boundary (four full stops: retain the prearmed .02).
            intent = {'id': item['intent']['id'] + '-reverse', 'range_id': range_id,
                      'side': side, 'price': str(price),
                      'sl': str(price - sign(side) * distance),
                      'volume': str(volume_for_slot(count + 2)), 'kind': 'reverse'}
            validator = self.session.transport.validator
            symbol, quote = validator._metadata({'action': 'TRADE_ACTION_PENDING',
                'type': 'ORDER_TYPE_BUY_STOP' if side == 'BUY' else 'ORDER_TYPE_SELL_STOP'})
            validator._session()
            request = pending(intent, symbol, quote, c.magic)
            b = self.session.transport.binding
            scope = {'account': b.account, 'server': b.server, 'symbol': b.symbol, 'magic': c.magic}
            records[key] = {'key': key, 'range_id': range_id, 'intent': intent,
                'origin_position_identifier': pid, 'origin_order_key': item['key'],
                'request': request, 'scope': scope,
                'phase': 'dispatching', 'broker_order': None}
            c._save(data)
            try:
                self.session.execute(key, request, scope)
                return self.reconcile(key)
            except BaseException:
                self.session.ready = False
                raise

    def reconcile(self, key):
        """Map an existing opposite trigger only; never resend or move it."""
        c = self.control
        with self.session._lock:
            c._guard()
            self.session.ready = False
            try:
                c.strategy_config()
                data = c._read()
                record = data.get('reverse_commands', {}).get(key)
                if record is None:
                    raise ValueError('Unknown reverse checkpoint')
                self.session.reconcile()
                exists = c.db.execute('SELECT id FROM recovery_outbox WHERE id=?', (key,)).fetchone()
                phase = 'unresolved'; ticket = None
                if exists:
                    row = self.session.executor.read(key)
                    if row['payload'] != {'request': record['request'], 'expected': record['scope']}:
                        raise ValueError('Reverse journal differs from checkpoint')
                    ticket = c._receipt(row)
                    phase = 'verified' if ticket else ('rejected' if row['state']=='REJECTED' else 'unresolved')
                record.update(phase=phase, broker_order=ticket)
                c._save(data)
                if c._unresolved_entry_cancellations(data):
                    self.session.ready = False
                return deepcopy(record)
            except BaseException:
                self.session.ready = False
                raise
