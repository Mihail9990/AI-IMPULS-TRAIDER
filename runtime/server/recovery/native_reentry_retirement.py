"""Retire the old opposite only after same-side reentry has really filled.

No replacement order is submitted here. NativeInitialReverse.arm(order_key=...)
can create the replacement only after exact retirement and close reconciliation.
"""
from copy import deepcopy
from .engine import number
from .mt5_plan import cancel
from .native_entry_observer import NativeEntryObserver


class NativeReentryRetirement:
    def __init__(self, control):
        self.control = control
        self.session = control.session
        self.observer = NativeEntryObserver(control)

    def retire(self, range_id, *, order_key):
        c = self.control
        with self.session._lock:
            c._guard(); c.strategy_config()
            c._require_active_range(c._read(), range_id)
            data = c._read()
            reentry = data.get('reentry_commands', {}).get(order_key)
            opposite_fill = data.get('reverse_commands', {}).get(order_key)
            current_record = reentry or opposite_fill
            if current_record is None or current_record['intent']['range_id'] != range_id:
                raise ValueError('Exact mapped reentry or reverse required')
            current = self.observer.observe(range_id, order_key=order_key)
            if current['phase'] != 'awaiting_protection':
                raise RuntimeError('Reentry must finish filling before retiring opposite')
            data = c._read()
            confirmed = data.get('confirmed_protection', {}).get(str(current['evidence']['position_identifier']))
            if confirmed is None or number(confirmed['sl']) != number(current['evidence']['position']['sl']):
                raise RuntimeError('Reentry protection must be confirmed first')
            if reentry is not None:
                old_key = reentry['origin_order_key'] + ':initial-reverse'
                old = data.get('reverse_commands', {}).get(old_key)
            else:
                old_key = current_record['origin_order_key'] + ':support-reentry'
                old = data.get('reentry_commands', {}).get(old_key)
            if old is None:
                return {'phase': 'not_required', 'reason': 'No mapped old opposite'}
            key = order_key + ':retire-old-opposite'
            if key in data.get('retirement_commands', {}):
                return self.reconcile(key)
            previous = self.observer.observe(range_id, order_key=old_key)
            if number(previous['evidence']['filled_volume']) != 0:
                raise RuntimeError('Old opposite filled; reconcile both positions, do not replace')
            if previous['evidence']['pending_order'] is None:
                raise RuntimeError('Old opposite absent without this retirement checkpoint')
            data = c._read()
            if c._unresolved_entry_cancellations(data) or not self.session.ready:
                self.session.ready = False
                raise RuntimeError('Native checkpoint requires reconciliation')
            b = self.session.transport.binding
            request = cancel(previous['evidence']['pending_order'], expected_ticket=old['broker_order'],
                             expected_magic=c.magic, expected_symbol=b.symbol)
            scope = {'account': b.account, 'server': b.server, 'symbol': b.symbol, 'magic': c.magic}
            data.setdefault('retirement_commands', {})[key] = {
                'key': key, 'range_id': range_id, 'old_order_key': old_key,
                'request': request, 'scope': scope, 'phase': 'dispatching'}
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
            c._guard(); self.session.ready = False
            try:
                data = c._read()
                record = data.get('retirement_commands', {}).get(key)
                if record is None: raise ValueError('Unknown retirement checkpoint')
                self.session.reconcile()
                phase = 'unresolved'
                exists = c.db.execute('SELECT id FROM recovery_outbox WHERE id=?',(key,)).fetchone()
                if exists:
                    row = self.session.executor.read(key)
                    if row['payload'] != {'request':record['request'],'expected':record['scope']}:
                        raise ValueError('Retirement journal differs from checkpoint')
                    evidence = row.get('evidence') or {}
                    if (row['state']=='VERIFIED' and evidence.get('kind')=='cancel_verified'
                            and evidence.get('order')==record['request']['order']):
                        prior = self.observer.observe(record['range_id'],order_key=record['old_order_key'])
                        if (number(prior['evidence']['filled_volume'])==0
                                and prior['evidence']['terminal_order']
                                and prior['evidence']['pending_order'] is None):
                            phase = 'verified'
                    elif row['state']=='REJECTED': phase = 'rejected'
                # Observation may have committed a newer snapshot: don't overwrite it.
                data = c._read();record = data['retirement_commands'][key];record['phase']=phase
                c._save(data)
                if c._unresolved_entry_cancellations(data): self.session.ready=False
                return deepcopy(record)
            except BaseException:
                self.session.ready=False
                raise
