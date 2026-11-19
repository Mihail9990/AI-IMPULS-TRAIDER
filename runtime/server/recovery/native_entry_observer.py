"""Durable native entry observation and explicit partial-remainder cancellation.

Not a complete strategy runner: confirmed entries await SL/reversal integration.
No polling, initialization or implicit send. Caller owns NativeRecoveryControl.
"""
from copy import deepcopy
from .engine import number
from .mt5_plan import cancel
from .native_entry_reader import NativeEntryReader


class NativeEntryObserver:
    def __init__(self, control):
        self.control = control
        self.session = control.session
        self.reader = NativeEntryReader(self.session)

    def _item(self, data, range_id, order_key=None):
        if data is None:
            raise ValueError('No staged ranges')
        items = list(data['orders'])
        if order_key is not None:
            items += list(data.get('reverse_commands', {}).values())
            items += list(data.get('reentry_commands', {}).values())
        item = next((x for x in items if x['intent']['range_id'] == range_id
                     and (order_key is None or x['key'] == order_key)), None)
        if item is None or item['phase'] != 'verified' or not item['broker_order']:
            raise ValueError('Verified mapped broker order required')
        return item

    def observe(self, range_id, *, order_key=None):
        """Read broker evidence, commit immutable deal ledger; never send."""
        c = self.control
        with self.session._lock:
            c._guard()
            c.strategy_config()
            data = c._read()
            item = self._item(data, range_id, order_key)
            try:
                evidence = self.reader.read(item, magic=c.magic)
                observations = data.setdefault('entry_observations', {})
                key = str(item['broker_order'])
                old = observations.get(key)
                journal = self.session.executor.read(item['key'])
                journal_evidence = journal.get('evidence') or {}
                baselines = [old['evidence']] if old else []
                if journal_evidence.get('kind') == 'partial_entry_verified':
                    baselines.append(journal_evidence['entry_snapshot'])
                for baseline in baselines:
                    for field in ('deals', 'exit_deals'):
                        before = {str(d['ticket']): d for d in baseline[field]}
                        after = {str(d['ticket']): d for d in evidence[field]}
                        if any(after.get(k) != v for k, v in before.items()):
                            raise ValueError('Previously observed deal disappeared or changed')
                    pid = baseline['position_identifier']
                    if pid is not None and pid != evidence['position_identifier']:
                        raise ValueError('Entry position identifier changed')
                filled = number(evidence['filled_volume'])
                if filled == 0:
                    phase = 'pending' if evidence['pending_order'] else 'unfilled_terminal'
                elif evidence['pending_order'] is not None:
                    phase = 'partial_remainder_pending'
                elif not evidence['terminal_order']:
                    raise ValueError('Entry terminal outcome not confirmed')
                elif evidence['position'] is None:
                    phase = 'closed_before_protection'
                else:
                    phase = 'awaiting_protection'
                observation = {'phase': phase, 'evidence': deepcopy(evidence)}
                if old and 'cancellation' in old:
                    observation['cancellation'] = old['cancellation']
                observations[key] = observation
                c._save(data)
                return deepcopy(observation)
            except BaseException:
                self.session.ready = False
                raise

    def cancel_partial_remainder(self, range_id, *, order_key=None):
        """Explicit one-shot cancellation, then re-read all fills including late ones.

        Existing cancellation checkpoints are reconciled, never submitted again.
        Unknown outcomes remain unknown even if a later observation finds a fill.
        """
        c = self.control
        with self.session._lock:
            c._guard()
            observation = self.observe(range_id, order_key=order_key)
            data = c._read()
            item = self._item(data, range_id, order_key)
            saved = data['entry_observations'][str(item['broker_order'])]
            existing = saved.get('cancellation')
            if existing is not None:
                return self.reconcile_partial(range_id, order_key=order_key)
            if observation['phase'] != 'partial_remainder_pending':
                raise ValueError('Confirmed partial fill with pending remainder required')
            if c._unresolved_entry_cancellations(data) and not c.protection_only_rejections(data):
                self.session.ready = False
            if not self.session.ready:
                raise RuntimeError('Native session is not reconciled')
            b = self.session.transport.binding
            request = cancel(observation['evidence']['pending_order'],
                expected_ticket=item['broker_order'], expected_magic=c.magic, expected_symbol=b.symbol)
            scope = {'account': b.account, 'server': b.server, 'symbol': b.symbol, 'magic': c.magic}
            checkpoint = {'key': item['key'] + ':partial-remainder', 'request': request,
                          'phase': 'dispatching'}
            saved['cancellation'] = checkpoint
            c._save(data)
            try:
                self.session.execute(checkpoint['key'], request, scope)
                return self.reconcile_partial(range_id, order_key=order_key)
            except BaseException:
                self.session.ready = False
                raise

    def reconcile_partial(self, range_id, *, order_key=None):
        """Evidence-only recovery of cancellation + fresh entry/exit observation."""
        c = self.control
        with self.session._lock:
            c._guard()
            c.strategy_config()
            data = c._read()
            item = self._item(data, range_id, order_key)
            saved = data.get('entry_observations', {}).get(str(item['broker_order']), {})
            checkpoint = saved.get('cancellation')
            if checkpoint is None:
                raise ValueError('No partial cancellation checkpoint')
            self.session.ready = False
            try:
                self.session.reconcile()
                exists = c.db.execute('SELECT id FROM recovery_outbox WHERE id=?',
                                      (checkpoint['key'],)).fetchone()
                phase = 'unresolved'
                if exists:
                    row = self.session.executor.read(checkpoint['key'])
                    if row['payload']['request'] != checkpoint['request']:
                        raise ValueError('Cancellation journal differs from checkpoint')
                    evidence = row.get('evidence') or {}
                    if (row['state'] == 'VERIFIED' and evidence.get('kind') == 'cancel_verified'
                            and evidence.get('order') == item['broker_order']):
                        phase = 'verified'
                    elif row['state'] == 'REJECTED':
                        phase = 'rejected'
                checkpoint['phase'] = phase
                c._save(data)
                result = self.observe(range_id, order_key=order_key)
                if c._unresolved_entry_cancellations(c._read()):
                    self.session.ready = False
                return result
            except BaseException:
                self.session.ready = False
                raise
