"""Explicit initial-range intent bridge, NOT a running RECOVERY service.

Uses Engine only as an offline planner; its simulated state is never reported as
broker state. No automatic submission, terminal initialization or public endpoint.
Full fill/SL/reversal lifecycle must be connected before deployment.
"""
import json
from copy import deepcopy
from uuid import uuid4
from .engine import Engine
from .mt5_plan import pending
from .native_executor_session import NativeExecutorSession


class NativeRecoveryControl:
    def __init__(self, session, *, magic):
        if not isinstance(session, NativeExecutorSession):
            raise TypeError('NativeExecutorSession required')
        self.session = session
        self.magic = magic
        with session._lock:
            self._guard()
            if type(magic) is not int or magic not in session.transport.binding.magics:
                raise ValueError('Owned magic required')
            self.db = session.executor.db
            self.db.execute('''CREATE TABLE IF NOT EXISTS native_recovery_control
                (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)''')
            self.db.commit()
            data = self._read()
            if data is not None and data['magic'] != magic:
                raise ValueError('Control belongs to another magic')

    def _guard(self):
        s = self.session
        if not s._active or s.closed or s.executor is None:
            raise RuntimeError('Active native owner required')
        s.transport._guard()

    def _read(self):
        row = self.db.execute('SELECT payload FROM native_recovery_control WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, data):
        self._guard()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO native_recovery_control VALUES(1,?)',
                            (json.dumps(data, sort_keys=True),))

    def stage_initial_ranges(self, ranges, *, initial_sl_distance='0.70', reentry_distance='0.70'):
        """Offline plan and persist only. One generation, never cancel old exposure."""
        with self.session._lock:
            self._guard()
            if self._read() is not None:
                raise RuntimeError('Existing range generation requires lifecycle reconciliation')
            # Ensure no unowned/unreconciled startup state is admitted.
            if not self.session.ready:
                raise RuntimeError('Native session is not reconciled')
            b = self.session.transport.binding
            snapshot = self.session.transport.snapshot({'account': b.account, 'server': b.server,
                'symbol': b.symbol, 'magic': self.magic})
            if any(x.get('symbol') == b.symbol and x.get('magic') in b.magics
                   for x in snapshot['orders'] + snapshot['positions']):
                raise RuntimeError('Existing owned exposure must be reconciled first')
            engine = Engine()
            engine.replace_ranges(ranges, initial_sl_distance, reentry_distance)
            generation = uuid4().hex
            data = {'schema_version': 2, 'magic': self.magic,
                    'generation': generation, 'orders': [],
                    # Canonical, validated input retained independently of broker
                    # mappings. Never infer missing distances from pending SLs.
                    'strategy_config': {
                        'settings': deepcopy(engine.data['settings']),
                        'ranges': [{k: r[k] for k in ('id', 'price', 'side', 'volume')}
                                   for r in engine.data['ranges']],
                    }}
            for order in engine.data['orders']:
                data['orders'].append({'intent': deepcopy(order),
                    'key': 'native-range:' + generation + ':' + order['id'],
                    'phase': 'staged', 'request': None, 'broker_order': None})
            self._save(data)
            return self.state()

    def strategy_config(self):
        """Return durable validated config; legacy missing config is not guessed.

        This is configuration, not a reconstructed position or trade ledger.
        A future lifecycle runner must separately restore broker-confirmed events.
        """
        with self.session._lock:
            self._guard()
            data = self._read()
            if data is None:
                raise ValueError('No staged ranges')
            if data.get('schema_version') != 2 or 'strategy_config' not in data:
                raise RuntimeError('Legacy generation has no durable strategy configuration')
            config = data['strategy_config']
            settings = config['settings']
            engine = Engine()
            engine.replace_ranges(config['ranges'], settings['initial_sl_distance'],
                                  settings['reentry_distance'])
            return deepcopy(config)

    def state(self):
        with self.session._lock:
            self._guard()
            return {'mode': 'native_initial_intent_bridge', 'strategy_ready': False,
                    'data': self._read()}

    def _receipt(self, row):
        # Binding uses verified exact broker evidence, never comments or ACK alone.
        evidence = row.get('evidence') or {}
        ticket = evidence.get('order')
        if row['state'] == 'VERIFIED' and evidence.get('kind') in (
                'pending_verified', 'order_filled_before_verification', 'partial_entry_verified'):
            if type(ticket) is not int or ticket <= 0:
                raise RuntimeError('Invalid verified ticket evidence')
            return ticket
        return None

    def _terminal_entry_checkpoint(self, record, data):
        if record['phase'] == 'verified': return True
        if (record['phase'] != 'rejected' or
                record['key'] not in data.get('acknowledged_rejections', [])):
            return False
        row = self.session.executor.read(record['key'])
        return (row['state'] == 'REJECTED' and
                row['payload'] == {'request':record['request'], 'expected':record['scope']} and
                record['request']['action'] == 'TRADE_ACTION_PENDING')

    def _unresolved_entry_cancellations(self, data):
        return (data.get('range_management') is not None
                or any(r['phase'] != 'verified' for r in data.get('range_close_commands', {}).values())
                or any(o.get('cancellation', {}).get('phase') not in (None, 'verified')
                    for o in data.get('entry_observations', {}).values())
                or any(r['phase'] != 'verified'
                       for r in data.get('protection_commands', {}).values())
                or any(not self._terminal_entry_checkpoint(r,data)
                       for r in data.get('reverse_commands', {}).values())
                or any(not self._terminal_entry_checkpoint(r,data)
                       for r in data.get('reentry_commands', {}).values())
                or any(r['phase'] != 'verified'
                       for r in data.get('retirement_commands', {}).values()))

    def protection_only_rejections(self, data):
        """Exact terminal rejected pending creations may allow SL protection ONLY.

        Return proven tombstones, never reinterpret UNKNOWN or failed protection.
        Actual-fill initial correction still needs InitialStopProof; support
        still obeys confirmed-stop monotonicity. No entry send is admitted.
        The ordinary entry/manual-command gate deliberately remains unchanged.
        """
        candidate = deepcopy(data)
        rejected = []
        for group in ('reverse_commands', 'reentry_commands'):
            for key, record in candidate.get(group, {}).items():
                if record['phase'] != 'rejected': continue
                row = self.session.executor.read(key)
                if (row['state'] != 'REJECTED' or
                        row['payload'] != {'request':record['request'], 'expected':record['scope']} or
                        record['request']['action'] != 'TRADE_ACTION_PENDING'):
                    return []
                record['phase'] = 'verified'  # predicate copy only, never saved
                rejected.append(key)
        if self._unresolved_entry_cancellations(candidate): return []
        if any(x['phase'] not in ('verified','staged','rejected') for x in candidate['orders']): return []
        return rejected

    def _require_active_range(self, data, range_id):
        if data.get('range_modes', {}).get(range_id, 'active') != 'active':
            raise RuntimeError('Range is closing or cancelled; new ranges require explicit reset')

    def submit_initial(self, range_id):
        """Explicitly send one staged initial trigger; never automatically replay.

        A crash after checkpoint but before journal preparation intentionally leaves
        dispatching unresolved. This is not grounds for a new ID or another send.
        """
        with self.session._lock:
            self._guard()
            data = self._read()
            if data is None: raise ValueError('No staged ranges')
            self._require_active_range(data, range_id)
            self.strategy_config()  # no new dispatch with missing/unknown configuration
            if self._unresolved_entry_cancellations(data):
                self.session.ready = False
                raise RuntimeError('Entry cancellation checkpoint requires reconciliation')
            item = next((x for x in data['orders'] if x['intent']['range_id'] == range_id), None)
            if item is None: raise ValueError('Unknown range')
            if item['phase'] != 'staged':
                raise RuntimeError('Intent already dispatched; reconcile, do not resend')
            if any(x['phase'] not in ('staged', 'verified', 'rejected') for x in data['orders']):
                raise RuntimeError('Another intent outcome is unresolved')
            if not self.session.ready: raise RuntimeError('Native session is not reconciled')
            b = self.session.transport.binding
            scope = {'account': b.account, 'server': b.server, 'symbol': b.symbol, 'magic': self.magic}
            validator = self.session.transport.validator
            validator._session()
            symbol, quote = validator._metadata({'action': 'TRADE_ACTION_PENDING',
                'type': 'ORDER_TYPE_BUY_STOP' if item['intent']['side'] == 'BUY' else 'ORDER_TYPE_SELL_STOP'})
            request = pending(item['intent'], symbol, quote, self.magic)
            validator._session()
            item.update(phase='dispatching', request=request)
            self._save(data)  # durable dispatch boundary before possible broker action
            try:
                row = self.session.execute(item['key'], request, scope)
                ticket = self._receipt(row)
                item['broker_order'] = ticket
                item['phase'] = 'verified' if ticket else ('rejected' if row['state'] == 'REJECTED' else 'unresolved')
                self._save(data)
            except BaseException:
                # Even if executor has VERIFIED, failed strategy commit must stop
                # subsequent submissions until the durable key is reconciled.
                self.session.ready = False
                raise
            return self.state()

    def resolve_initial_partial(self, range_id, *, order_key=None):
        """Receipt-bound authoritative evidence only; never send or guess a ticket.

        The generic executor's current-volume equality remains unchanged. A partial
        before its first verification needs exact entry history in addition to the
        original broker receipt. A lost receipt is intentionally not recoverable
        through comments, nearby prices or matching magic alone.
        """
        from .native_entry_reader import NativeEntryReader, positive_id
        with self.session._lock:
            self._guard()
            self.session.ready = False
            try:
                self.strategy_config()
                data = self._read()
                items = list(data['orders'])
                if order_key is not None:
                    items += list(data.get('reverse_commands', {}).values())
                    items += list(data.get('reentry_commands', {}).values())
                item = next((x for x in items if x['intent']['range_id'] == range_id
                             and (order_key is None or x['key'] == order_key)), None)
                if item is None or item['phase'] == 'staged':
                    raise ValueError('Dispatched range required')
                row = self.session.executor.read(item['key'])
                if row['payload']['request'] != item['request']:
                    raise ValueError('Entry journal differs from dispatch checkpoint')
                if self._receipt(row) is not None:
                    return self.reconcile()
                ticket = positive_id((row.get('receipt') or {}).get('order'))
                candidate = deepcopy(item)
                candidate['broker_order'] = ticket
                observed = NativeEntryReader(self.session).read_receipt(candidate, magic=self.magic)
                # Commit evidence first. A failed control commit is recoverable
                # from this same durable journal, without any second submission.
                self.session.executor._update(item['key'], 'VERIFIED', row['receipt'],
                    {'kind': 'partial_entry_verified', 'order': ticket, 'entry_snapshot': observed})
                item.update(phase='verified', broker_order=ticket)
                self._save(data)
                return self.reconcile()
            except BaseException:
                self.session.ready = False
                raise

    def reconcile(self):
        """Read-only broker reconciliation and local mapping, never submit."""
        with self.session._lock:
            self._guard()
            self.session.ready = False
            data = self._read()
            if data is None: return self.session.reconcile()
            self.session.reconcile()
            try:
                unresolved = False
                for item in (list(data['orders']) + list(data.get('reverse_commands', {}).values())
                             + list(data.get('reentry_commands', {}).values())):
                    if item['phase'] == 'staged': continue
                    exists = self.db.execute('SELECT id FROM recovery_outbox WHERE id=?', (item['key'],)).fetchone()
                    if exists is None:
                        unresolved = True
                        continue
                    row = self.session.executor.read(item['key'])
                    # Journal payload must still be the exact checkpointed command.
                    if row['payload']['request'] != item['request']:
                        raise RuntimeError('Journal differs from range dispatch checkpoint')
                    ticket = self._receipt(row)
                    item['broker_order'] = ticket
                    item['phase'] = 'verified' if ticket else ('rejected' if row['state'] == 'REJECTED' else 'unresolved')
                    unresolved |= item['phase'] == 'unresolved'
                self._save(data)
                if unresolved or self._unresolved_entry_cancellations(data): self.session.ready = False
                return self.state()
            except BaseException:
                self.session.ready = False
                raise
