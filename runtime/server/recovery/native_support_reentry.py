"""Explicit same-side reentry after authoritative support-SL close classification.

The old opposite trigger is deliberately left in place until reentry actually fills.
"""
from copy import deepcopy
from .engine import number, sign
from .volume_schedule import volume_for_slot
from .mt5_plan import pending
from .native_entry_observer import NativeEntryObserver
from .native_close_ledger import NativeCloseLedger


class NativeSupportReentry:
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
            if observed['phase'] != 'closed_before_protection':
                raise ValueError('Confirmed fully closed position required for reentry')
            data = c._read()
            item = self.observer._item(data, range_id, order_key)
            ledger = NativeCloseLedger(c)
            close = ledger.record(item['broker_order'])
            if not close['fully_closed'] or close['classification'] != 'support_sl':
                raise RuntimeError('Authoritative support-SL classification required')
            support_level = number(close['stop_level'])
            if support_level <= 0:
                raise ValueError('Positive confirmed support stop required')
            data = c._read()
            key = item['key'] + ':support-reentry'
            records = data.setdefault('reentry_commands', {})
            if key in records:
                return self.reconcile(key)
            old_key = item['key'] + ':initial-reverse'
            if old_key in data.get('reverse_commands', {}):
                opposite = self.observer.observe(range_id, order_key=old_key)
                if number(opposite['evidence']['filled_volume']) > 0:
                    raise RuntimeError('Opposite already filled; do not start stale reentry branch')
                data = c._read()
                records = data.setdefault('reentry_commands', {})
            if c._unresolved_entry_cancellations(data) or not self.session.ready:
                self.session.ready = False
                raise RuntimeError('Native checkpoint requires reconciliation')
            totals = ledger.totals().get(range_id, {})
            if totals.get('classification_pending_count', 0):
                raise RuntimeError('Unclassified close prevents reentry lot calculation')
            count = totals.get('full_sl_count', 0)
            if count >= 24:
                raise RuntimeError('Full-SL limit reached: new manual ranges required')
            side = item['intent']['side']
            price = support_level + sign(side) * number(config['settings']['reentry_distance'])
            distance = number(config['settings']['initial_sl_distance'])
            intent = {'id': item['intent']['id'] + '-reentry', 'range_id': range_id,
                      'side': side, 'price': str(price), 'sl': str(price - sign(side) * distance),
                      'volume': str(volume_for_slot(count + 1)), 'kind': 'reentry'}
            validator = self.session.transport.validator
            symbol, quote = validator._metadata({'action': 'TRADE_ACTION_PENDING',
                'type': 'ORDER_TYPE_BUY_STOP' if side == 'BUY' else 'ORDER_TYPE_SELL_STOP'})
            validator._session()
            request = pending(intent, symbol, quote, c.magic)
            b = self.session.transport.binding
            scope = {'account': b.account, 'server': b.server, 'symbol': b.symbol, 'magic': c.magic}
            records[key] = {'key': key, 'range_id': range_id, 'intent': intent,
                'origin_order_key': item['key'], 'origin_position_identifier': close['position_identifier'],
                'request': request, 'scope': scope, 'phase': 'dispatching', 'broker_order': None}
            c._save(data)
            try:
                self.session.execute(key, request, scope)
                return self.reconcile(key)
            except BaseException:
                self.session.ready = False
                raise

    def reconcile(self, key):
        c = self.control
        with self.session._lock:
            c._guard()
            self.session.ready = False
            try:
                c.strategy_config()
                data = c._read()
                record = data.get('reentry_commands', {}).get(key)
                if record is None:
                    raise ValueError('Unknown reentry checkpoint')
                self.session.reconcile()
                exists = c.db.execute('SELECT id FROM recovery_outbox WHERE id=?', (key,)).fetchone()
                phase = 'unresolved'; ticket = None
                if exists:
                    row = self.session.executor.read(key)
                    if row['payload'] != {'request': record['request'], 'expected': record['scope']}:
                        raise ValueError('Reentry journal differs from checkpoint')
                    ticket = c._receipt(row)
                    phase = 'verified' if ticket else ('rejected' if row['state']=='REJECTED' else 'unresolved')
                record.update(phase=phase, broker_order=ticket)
                c._save(data)
                if c._unresolved_entry_cancellations(data): self.session.ready = False
                return deepcopy(record)
            except BaseException:
                self.session.ready = False
                raise
