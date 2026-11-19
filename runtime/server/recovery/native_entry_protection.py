"""Explicit verified native initial/support SL transitions, not a strategy loop.

Requires authoritative terminal entry observation; no supplied fill/price assertions.
Native preflight remains responsible for never weakening external/support stops.
"""
from copy import deepcopy
from .engine import number, sign
from .mt5_plan import protect
from .native_entry_observer import NativeEntryObserver
from .initial_stop_proof import InitialStopProof


class NativeEntryProtection:
    def __init__(self, control):
        self.control = control
        self.session = control.session
        self.observer = NativeEntryObserver(control)
        self.session.transport.validator.initial_stop_guard = InitialStopProof(control).validate

    def initial(self, range_id, *, order_key=None):
        return self._apply(range_id, support=False, order_key=order_key)

    def support(self, range_id, *, order_key=None):
        return self._apply(range_id, support=True, order_key=order_key)

    def _apply(self, range_id, *, support, order_key=None):
        c = self.control
        with self.session._lock:
            c._guard()
            c._require_active_range(c._read(), range_id)
            config = c.strategy_config()
            observed = self.observer.observe(range_id, order_key=order_key)
            if observed['phase'] != 'awaiting_protection':
                raise ValueError('Confirmed terminal entry with open position required')
            data = c._read()
            item = self.observer._item(data, range_id, order_key)
            pid = observed['evidence']['position_identifier']
            position = observed['evidence']['position']
            states = data.setdefault('confirmed_protection', {})
            state = states.get(str(pid))
            entry = number(observed['evidence']['average_price'])
            direction = sign(item['intent']['side'])
            validator = self.session.transport.validator
            symbol, quote = validator._metadata({'action': 'TRADE_ACTION_SLTP'})
            validator._session()
            if support:
                if state is None:
                    raise RuntimeError('Initial protection must be confirmed first')
                executable = number(quote.bid if direction > 0 else quote.ask)
                progress = (executable - entry) * direction
                stage = 2 if progress >= number('.50') else 1 if progress >= number('.25') else 0
                if stage <= state['stage']:
                    if number(position['sl']) != number(state['sl']):
                        raise ValueError('Broker stop differs from confirmed protection')
                    return deepcopy(state)
                target = entry if stage == 2 else entry - direction * number('.25')
            else:
                stage = 0
                target = entry - direction * number(config['settings']['initial_sl_distance'])
                if state is not None:
                    if number(position['sl']) != number(state['sl']):
                        raise ValueError('Broker stop differs from confirmed protection')
                    # Never re-run initial correction after support, or overwrite
                    # newer confirmed protection with an older executor receipt.
                    return deepcopy(state)
            expected_stop = number(state['sl']) if state else number(item['request']['sl'])
            if number(position['sl']) != expected_stop:
                raise ValueError('Broker stop differs from last confirmed strategy protection')
            key = item['key'] + ':protection:' + str(stage)
            records = data.setdefault('protection_commands', {})
            if key in records:
                return self.reconcile(key)
            protective_only = bool(c.protection_only_rejections(data))
            if (c._unresolved_entry_cancellations(data) and not protective_only) or not self.session.ready:
                self.session.ready = False
                raise RuntimeError('Outstanding native checkpoint requires reconciliation')
            if number(position['sl']) == target:
                # Actual broker snapshot is evidence; no unnecessary SLTP send.
                state = {'stage': stage, 'sl': str(target), 'position_identifier': pid,
                         'position_ticket': position['ticket'], 'source': 'observed_existing_stop'}
                states[str(pid)] = state
                c._save(data)
                return deepcopy(state)
            request = protect(position, str(target), symbol, quote,
                expected_ticket=position['ticket'], expected_identifier=pid, expected_magic=c.magic)
            b = self.session.transport.binding
            scope = {'account': b.account, 'server': b.server, 'symbol': b.symbol,
                     'magic': c.magic, 'identifier': pid}
            if stage == 0:
                scope['initial_stop_key'] = key
            records[key] = {'key': key, 'range_id': range_id, 'position_identifier': pid,
                            'stage': stage, 'request': request, 'scope': scope, 'phase': 'dispatching'}
            c._save(data)
            try:
                self.session.execute(key, request, scope)
                return self.reconcile(key)
            except BaseException:
                self.session.ready = False
                raise

    def reconcile(self, key):
        """Resolve only the existing protection key. Never retry a send."""
        c = self.control
        with self.session._lock:
            c._guard()
            self.session.ready = False
            try:
                c.strategy_config()
                data = c._read()
                record = data.get('protection_commands', {}).get(key)
                if record is None:
                    raise ValueError('Unknown protection checkpoint')
                self.session.reconcile()
                found = c.db.execute('SELECT id FROM recovery_outbox WHERE id=?', (key,)).fetchone()
                phase = 'unresolved'
                if found:
                    row = self.session.executor.read(key)
                    if row['payload'] != {'request': record['request'], 'expected': record['scope']}:
                        raise ValueError('Protection journal differs from checkpoint')
                    evidence = row.get('evidence') or {}
                    if (row['state'] == 'VERIFIED' and evidence.get('kind') == 'protection_verified'
                            and evidence.get('position') == record['request']['position']):
                        phase = 'verified'
                    elif row['state'] == 'REJECTED':
                        phase = 'rejected'
                record['phase'] = phase
                if phase == 'verified':
                    states = data.setdefault('confirmed_protection', {})
                    pid = str(record['position_identifier'])
                    old = states.get(pid)
                    if old is None or record['stage'] > old['stage']:
                        states[pid] = {'stage': record['stage'], 'sl': record['request']['sl'],
                            'position_identifier': record['position_identifier'],
                            'position_ticket': record['request']['position'], 'source': key}
                c._save(data)
                if c._unresolved_entry_cancellations(data):
                    self.session.ready = False
                return deepcopy(record)
            except BaseException:
                self.session.ready = False
                raise
