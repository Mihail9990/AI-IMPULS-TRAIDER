"""Explicit cancel-all / replace-generation operations, never manual-close hooks.

No position closure, terminal initialization or background sends. All owned
positions must be absent; unknown owned orders and uncertain commands stop work.
"""
import json
from copy import deepcopy
from uuid import uuid4
from .engine import Engine, number
from .mt5_plan import cancel
from .native_entry_observer import NativeEntryObserver
from .native_close_ledger import NativeCloseLedger
from .native_entry_reader import NativeEntryReader


class NativeRangeManagement:
    def __init__(self, control):
        self.control = control
        self.session = control.session
        with self.session._lock:
            control._guard()
            control.db.execute('''CREATE TABLE IF NOT EXISTS native_range_archives
                (generation TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
            control.db.commit()

    def _scope(self):
        b = self.session.transport.binding
        return dict(account=b.account, server=b.server, symbol=b.symbol, magic=self.control.magic)

    def _cancel_scope(self):
        return dict(self._scope(), require_no_owned_positions=True)

    def _items(self, data):
        return (list(data['orders']) + list(data.get('reverse_commands', {}).values())
                + list(data.get('reentry_commands', {}).values()))

    def _mapping(self, data):
        result = {}
        for item in self._items(data):
            ticket = item.get('broker_order')
            if item['phase'] not in ('staged', 'verified', 'rejected'):
                raise RuntimeError('Unresolved entry checkpoint')
            if item['phase'] != 'verified': continue
            if type(ticket) is not int or ticket <= 0 or ticket in result:
                raise ValueError('Missing or duplicate verified order mapping')
            row = self.session.executor.read(item['key'])
            if (self.control._receipt(row) != ticket or
                    row['payload'] != {'request':item['request'], 'expected':self._scope()}):
                raise ValueError('Entry mapping differs from verified scoped journal')
            result[ticket] = item
        return result

    def _snapshot(self, data):
        mapping = self._mapping(data)
        snapshot = self.session.transport.snapshot(self._scope())
        b = self.session.transport.binding
        owned = lambda x: x.get('symbol') == b.symbol and x.get('magic') in b.magics
        if any(owned(p) for p in snapshot['positions']):
            raise RuntimeError('Cancel ranges disabled while owned positions are open')
        orders = [o for o in snapshot['orders'] if owned(o)]
        if len({o['ticket'] for o in orders}) != len(orders):
            raise ValueError('Duplicate broker pending ticket')
        if any(o['ticket'] not in mapping or o['magic'] != self.control.magic for o in orders):
            raise RuntimeError('Unmapped owned pending exposure')
        return orders

    def _other_checkpoints(self, data):
        # Ignore only our own management gate; all other unresolved native
        # transitions still prevent a cancellation send or generation reset.
        without = deepcopy(data); without.pop('range_management', None)
        return self.control._unresolved_entry_cancellations(without)

    def _refresh_closed(self, data):
        observer = NativeEntryObserver(self.control)
        ledger = NativeCloseLedger(self.control)
        for item in self._mapping(data).values():
            observed = observer.observe(item['intent']['range_id'], order_key=item['key'])
            if observed['evidence']['pending_order'] is not None or observed['evidence']['position'] is not None:
                raise RuntimeError('Mapped exposure is not terminal and flat')
            if not observed['evidence']['terminal_order']:
                raise RuntimeError('Mapped order terminal history not confirmed')
            if number(observed['evidence']['filled_volume']) > 0:
                ledger.record(item['broker_order'])

    def cancel_ranges(self):
        """Explicit cancel button. Resume only never-dispatched planned tickets.

        Each dispatch checkpoint precedes possible send. An uncertain/missing
        outbox after dispatch is reconciled, never sent again under a new key.
        """
        c = self.control
        with self.session._lock:
            c._guard(); c.strategy_config()
            data = c._read()
            if self._other_checkpoints(data): raise RuntimeError('Other native checkpoint unresolved')
            orders = self._snapshot(data)
            if data.get('range_management') is None:
                if not self.session.ready: raise RuntimeError('Native session is not reconciled')
                commands = {}
                for order in orders:
                    request = cancel(order, expected_ticket=order['ticket'], expected_magic=c.magic,
                                     expected_symbol=self._scope()['symbol'])
                    key = 'cancel-generation:' + data['generation'] + ':' + str(order['ticket'])
                    commands[str(order['ticket'])] = {'key':key, 'request':request, 'phase':'staged'}
                data['range_management'] = {'phase':'cancelling', 'commands':commands}
                c._save(data)
            self.reconcile_cancel()
            data = c._read()
            if data['range_management']['phase'] == 'waiting': return deepcopy(data['range_management'])
            for ticket, command in data['range_management']['commands'].items():
                if command['phase'] != 'staged': continue
                # Refresh between cancellation requests: a sibling trigger may fill.
                current = self._snapshot(c._read())
                if int(ticket) not in {o['ticket'] for o in current}:
                    raise RuntimeError('Planned pending disappeared; reconcile terminal history')
                self.session.reconcile()
                if not self.session.ready: raise RuntimeError('Native executor uncertainty')
                data = c._read()
                if self._other_checkpoints(data): raise RuntimeError('Other native checkpoint unresolved')
                command = data['range_management']['commands'][ticket]
                if any(x['phase'] not in ('staged','verified','observed_terminal') for x in data['range_management']['commands'].values()):
                    raise RuntimeError('Range cancellation outcome unresolved')
                command['phase'] = 'dispatching'; c._save(data)
                try:
                    self.session.execute(command['key'], command['request'], self._cancel_scope())
                    self.reconcile_cancel()
                except BaseException:
                    self.session.ready = False
                    raise
            return self.reconcile_cancel()

    def reconcile_cancel(self):
        """Read-only recovery; never send cancellation or reset state."""
        c = self.control
        with self.session._lock:
            c._guard(); self.session.ready = False
            try:
                data = c._read()
                management = data.get('range_management') if data else None
                if management is None: raise ValueError('No explicit cancellation requested')
                self.session.reconcile()
                orders = self._snapshot(data)
                present = {o['ticket'] for o in orders}
                mapping = self._mapping(data)
                for command in management['commands'].values():
                    exists = c.db.execute('SELECT id FROM recovery_outbox WHERE id=?',(command['key'],)).fetchone()
                    if command['phase'] in ('staged', 'observed_terminal'):
                        if exists is not None:
                            raise ValueError('Never-dispatched cancellation unexpectedly has journal row')
                        ticket = command['request']['order']
                        if ticket in present:
                            if command['phase'] == 'observed_terminal':
                                raise ValueError('Previously terminal order reappeared')
                            continue
                        evidence = NativeEntryReader(self.session).read(mapping[ticket], magic=c.magic)
                        if (not evidence['terminal_order'] or evidence['pending_order'] is not None
                                or evidence['position'] is not None):
                            raise RuntimeError('Disappeared pending is not proven terminal and flat')
                        if command.get('terminal_evidence') not in (None, evidence):
                            raise ValueError('Previously terminal order evidence changed')
                        command.update(phase='observed_terminal', terminal_evidence=evidence)
                        continue
                    if exists is None:
                        command['phase'] = 'unresolved'; continue
                    row = self.session.executor.read(command['key'])
                    if row['payload'] != {'request':command['request'], 'expected':self._cancel_scope()}:
                        raise ValueError('Cancellation journal differs from checkpoint')
                    evidence = row.get('evidence') or {}
                    command['phase'] = ('verified' if row['state']=='VERIFIED' and
                        evidence.get('kind')=='cancel_verified' and
                        evidence.get('order')==command['request']['order'] else 'unresolved')
                management['phase'] = 'cancelling'; c._save(data)
                orders = self._snapshot(data)
                if not orders and all(x['phase'] in ('verified','observed_terminal') for x in management['commands'].values()):
                    self._refresh_closed(data)
                    data = c._read(); data['range_management']['phase'] = 'waiting'; c._save(data)
                return deepcopy(c._read()['range_management'])
            finally:
                # Old generation must not restart while waiting for new ranges.
                self.session.ready = False

    def replace_ranges(self, ranges, *, initial_sl_distance='0.70', reentry_distance='0.70'):
        """After explicit cancellation and broker-flat proof, archive and stage new.

        Does not submit new orders. Old generation and loss ledger are retained
        in the same atomic transaction. All strategy counters start from zero.
        """
        engine = Engine(); engine.replace_ranges(ranges, initial_sl_distance, reentry_distance)
        c = self.control
        with self.session._lock:
            c._guard()
            if self.reconcile_cancel()['phase'] != 'waiting':
                raise RuntimeError('Cancellation is not complete')
            old = c._read()
            if self._other_checkpoints(old): raise RuntimeError('Other checkpoint remains unresolved')
            self.session.reconcile()
            if not self.session.ready: raise RuntimeError('Executor has unresolved outcomes')
            if self._snapshot(old): raise RuntimeError('Broker is not flat')
            self._refresh_closed(old); old = c._read()
            generation = uuid4().hex
            new = {'schema_version':2, 'magic':c.magic, 'generation':generation,
                   'strategy_config':{'settings':deepcopy(engine.data['settings']),
                       'ranges':[{k:r[k] for k in ('id','price','side','volume')} for r in engine.data['ranges']]},
                   'orders':[]}
            for order in engine.data['orders']:
                new['orders'].append({'intent':deepcopy(order),
                    'key':'native-range:'+generation+':'+order['id'], 'phase':'staged',
                    'request':None, 'broker_order':None})
            try:
                with c.db:
                    c.db.execute('INSERT INTO native_range_archives VALUES(?,?)',
                                 (old['generation'],json.dumps(old,sort_keys=True)))
                    c.db.execute('UPDATE native_recovery_control SET payload=? WHERE id=1',
                                 (json.dumps(new,sort_keys=True),))
            except BaseException:
                self.session.ready = False
                raise
            return c.state()

    def history(self):
        with self.session._lock:
            self.control._guard()
            return [json.loads(row[0]) for row in self.control.db.execute(
                'SELECT payload FROM native_range_archives ORDER BY rowid')]
