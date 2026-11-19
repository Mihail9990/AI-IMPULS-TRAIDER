"""Native lifecycle orchestration. No terminal login, scheduler or startup sends.

The owner calls tick explicitly. Unknown command outcomes stop dispatch, rather
than being retried. GET state is a cached durable view, never a trading action.
"""
import json
import hashlib
from copy import deepcopy
from .native_entry_observer import NativeEntryObserver
from .native_entry_protection import NativeEntryProtection
from .native_close_ledger import NativeCloseLedger
from .native_initial_reverse import NativeInitialReverse
from .native_support_reentry import NativeSupportReentry
from .native_reentry_retirement import NativeReentryRetirement
from .native_close_range import NativeCloseRange
from .native_interrupted_recovery import NativeInterruptedRecovery
from .native_range_management import NativeRangeManagement
from .ea_stop_evidence import StopEvidenceUnavailable
from .engine import Engine
from .mt5_plan import pending


class NativeCoordinator:
    def __init__(self, control):
        self.control = control
        self.session = control.session
        self.observer = NativeEntryObserver(control)
        self.protection = NativeEntryProtection(control)
        self.ledger = NativeCloseLedger(control)
        self.reverse = NativeInitialReverse(control)
        self.reentry = NativeSupportReentry(control)
        self.retirement = NativeReentryRetirement(control)
        self.close = NativeCloseRange(control)
        self.interrupted = NativeInterruptedRecovery(control)
        self.ranges = NativeRangeManagement(control)
        with self.session._lock:
            control._guard()
            control.db.execute('''CREATE TABLE IF NOT EXISTS native_api_commands
                (id TEXT PRIMARY KEY, payload TEXT NOT NULL, phase TEXT NOT NULL, result TEXT)''')
            control.db.commit()
        self.last_tick = {'status': 'not_run', 'issues': []}

    @staticmethod
    def _items(data):
        return (list(data['orders']) + list(data.get('reverse_commands', {}).values())
                + list(data.get('reentry_commands', {}).values()))

    def state(self):
        with self.session._lock:
            self.control._guard()
            api_pending = self._unfinished_api_command()
            return {'mode': 'native', 'executor_ready': self.session.ready and not api_pending,
                    'api_reconciliation_required': api_pending,
                    'data': self.control._read(), 'totals': self.ledger.totals(),
                    'tick': deepcopy(self.last_tick)}

    def _unfinished_api_command(self):
        return self.control.db.execute(
            "SELECT id FROM native_api_commands WHERE phase NOT IN ('completed','rejected') LIMIT 1").fetchone() is not None

    def context_id(self):
        b = self.session.transport.binding
        return hashlib.sha256(json.dumps(dict(account=b.account, server=b.server,
            symbol=b.symbol, magic=self.control.magic), sort_keys=True).encode()).hexdigest()

    def command(self, request_id, payload):
        """Durable at-most-once HTTP command. Interrupted commands need review.

        A client retry returns the saved result; it never repeats a broker send.
        Even pre-dispatch failures retain their ID and require a new explicit
        user command after the cause is addressed.
        """
        encoded = json.dumps(payload, sort_keys=True)
        c = self.control
        with self.session._lock:
            c._guard()
            old = c.db.execute('SELECT payload,phase,result FROM native_api_commands WHERE id=?',
                               (str(request_id),)).fetchone()
            if old:
                if old[0] != encoded: raise ValueError('Request ID reused for another command')
                if old[1] == 'rejected': raise ValueError(json.loads(old[2])['error'])
                if old[1] != 'completed': raise RuntimeError('Command outcome requires reconciliation; do not resend')
                return json.loads(old[2])
            try:
                # Public native API requires both fields. Internal owner callers
                # may omit them, but supplied preconditions are never ignored.
                if 'expected_context' in payload or 'expected_generation' in payload:
                    current = c._read()
                    if (payload.get('expected_context') != self.context_id() or
                            payload.get('expected_generation') != (current or {}).get('generation')):
                        raise ValueError('Displayed trading context or generation changed')
                if self._unfinished_api_command():
                    self.session.ready = False
                    raise RuntimeError('Interrupted API command requires explicit reconciliation')
                kind = payload.get('kind')
                if kind not in ('ranges', 'cancel', 'close_cancel'):
                    raise ValueError('Unknown native command')
                if kind == 'ranges':
                    engine = Engine()
                    engine.replace_ranges(payload['ranges'], payload['initial_sl_distance'], payload['reentry_distance'])
                    # Validate the ENTIRE batch before cancelling an old generation
                    # or dispatching the first new order. Each send still gets its
                    # own fresh preflight, so this is not a quote reservation.
                    b = self.session.transport.binding
                    scope = dict(account=b.account,server=b.server,symbol=b.symbol,magic=c.magic)
                    validator = self.session.transport.validator
                    for planned in engine.data['orders']:
                        symbol, quote = validator._metadata(dict(action='TRADE_ACTION_PENDING',
                            type='ORDER_TYPE_BUY_STOP' if planned['side']=='BUY' else 'ORDER_TYPE_SELL_STOP'))
                        request = pending(planned,symbol,quote,c.magic)
                        validator.validate(request,scope)
                data = c._read()
                # Ordinary disabled controls are rejected BEFORE any durable dispatch
                # checkpoint. They must not disable protection on an open position.
                if kind in ('cancel', 'close_cancel') and data is None:
                    raise ValueError('No ranges configured')
                if data is not None:
                    if not self.session.ready or c._unresolved_entry_cancellations(data):
                        # A completed cancel generation is deliberately waiting for
                        # replacement and keeps ready false.
                        waiting = data.get('range_management', {}).get('phase') == 'waiting'
                        flat_reset = kind in ('ranges','cancel') and bool(c.protection_only_rejections(data))
                        if not ((kind == 'ranges' and waiting) or flat_reset):
                            raise RuntimeError('Native checkpoint requires reconciliation')
                    if kind in ('cancel', 'ranges'):
                        self.ranges._snapshot(data)  # rejects open/unmapped exposure
                    if kind == 'close_cancel':
                        rid = payload.get('range_id')
                        if rid not in {x['id'] for x in c.strategy_config()['ranges']}:
                            raise ValueError('Unknown range')
                        positions = set()
                        for item in self.close._mapped(data, rid):
                            if item['phase'] != 'verified': continue
                            e = self.observer.observe(rid, order_key=item['key'])['evidence']
                            if e['position'] is not None: positions.add(e['position_identifier'])
                        if len(positions) != 1:
                            raise ValueError('Exactly one confirmed current range position required')
            except (ValueError, RuntimeError) as exc:
                # Pre-dispatch rejection is durable and terminal for this UUID;
                # it cannot be reused later when conditions have changed.
                with c.db:
                    c.db.execute('INSERT INTO native_api_commands VALUES(?,?,?,?)',
                        (str(request_id), encoded, 'rejected', json.dumps({'error':str(exc)})))
                raise
            with c.db:
                c.db.execute('INSERT INTO native_api_commands VALUES(?,?,?,NULL)',
                             (str(request_id), encoded, 'dispatching'))
            range_generation = None
            try:
                # Explicit flat cancel/reset is the sole way to retire terminal
                # rejected pending creations. Preserve tombstones and never retry.
                data = c._read()
                rejected = c.protection_only_rejections(data) if data else []
                if kind in ('ranges','cancel') and rejected:
                    self.ranges._snapshot(data)  # repeat flat proof after checkpoint
                    data['acknowledged_rejections'] = sorted(set(data.get('acknowledged_rejections', [])) | set(rejected))
                    for group in ('reverse_commands','reentry_commands'):
                        for key in rejected:
                            item = data.get(group, {}).get(key)
                            if item: data.setdefault('range_modes', {})[item['intent']['range_id']] = 'manual'
                    c._save(data)
                    self.session.reconcile()
                if kind == 'ranges':
                    kwargs = {k: payload[k] for k in ('initial_sl_distance', 'reentry_distance')}
                    if c._read() is None:
                        c.stage_initial_ranges(payload['ranges'], **kwargs)
                    else:
                        self.ranges.cancel_ranges()
                        self.ranges.replace_ranges(payload['ranges'], **kwargs)
                    range_generation = c._read()['generation']
                    for item in c._read()['orders']:
                        c.submit_initial(item['intent']['range_id'])
                        self._resolve_receipt_bound_entries()
                    if any(x['phase'] != 'verified' for x in c._read()['orders']):
                        terminal = self._finish_partial_initial_batch(str(request_id),range_generation,
                            'One or more initial ranges were rejected or not dispatched')
                        if terminal is not None: return terminal
                elif kind == 'cancel':
                    self.ranges.cancel_ranges()
                else:
                    self.close.close_and_cancel(payload['range_id'])
                with c.db:
                    c.db.execute('UPDATE native_api_commands SET phase=? WHERE id=?',
                                 ('completed', str(request_id)))
                    result = self.state()
                    if kind == 'ranges':
                        result['range_outcomes'] = [{'range_id':x['intent']['range_id'],'status':'confirmed'}
                            for x in c._read()['orders'] if x['phase']=='verified']
                    c.db.execute('UPDATE native_api_commands SET result=? WHERE id=?',
                                 (json.dumps(result), str(request_id)))
                return result
            except (ValueError, RuntimeError) as exc:
                if kind == 'ranges' and range_generation is not None:
                    result = self._finish_partial_initial_batch(str(request_id),range_generation,str(exc))
                    if result is not None: return result
                self.session.ready = False
                raise
            except BaseException:
                self.session.ready = False
                raise

    def _finish_partial_initial_batch(self, request_id, generation, error):
        # Only untouched stage + proven terminal outcomes qualify. A dispatch
        # checkpoint without an outbox is NOT proof of no-send and is preserved.
        c = self.control; data = c._read()
        if data is None or data['generation'] != generation: return None
        b = self.session.transport.binding
        scope = dict(account=b.account,server=b.server,symbol=b.symbol,magic=c.magic)
        untouched = []
        for item in data['orders']:
            found = c.db.execute('SELECT id FROM recovery_outbox WHERE id=?',(item['key'],)).fetchone()
            if item['phase'] == 'staged':
                if found or item.get('request') is not None or item.get('broker_order') is not None: return None
                untouched.append(item)
            elif item['phase'] in ('verified','rejected') and found:
                row = self.session.executor.read(item['key'])
                if row['payload'] != {'request':item['request'],'expected':scope}: return None
                if item['phase']=='verified' and c._receipt(row)!=item['broker_order']: return None
                if item['phase']=='rejected' and row['state']!='REJECTED': return None
            else: return None
        rejected = [x for x in data['orders'] if x['phase']=='rejected']
        if not untouched and not rejected: return None
        self.session.reconcile()
        if not self.session.ready: return None
        for item in untouched: item['not_dispatched'] = True
        for item in untouched + rejected:
            data.setdefault('range_modes',{})[item['intent']['range_id']] = 'cancelled'
        data['initial_batch_issue'] = dict(request_id=request_id,error=error,
            not_dispatched_range_ids=[x['intent']['range_id'] for x in untouched],
            rejected_range_ids=[x['intent']['range_id'] for x in rejected])
        c._save(data)
        with c.db:
            applied = any(x['phase']=='verified' for x in data['orders'])
            phase = 'completed' if applied else 'rejected'
            c.db.execute('UPDATE native_api_commands SET phase=? WHERE id=?',(phase,request_id))
            result = self.state(); result['command_outcome']='partially_applied' if applied else 'rejected'
            result['command_phase']=phase; result['error']=error
            result['range_outcomes'] = [{'range_id':x['intent']['range_id'], 'status':
                'not_dispatched' if x.get('not_dispatched') else 'confirmed' if x['phase']=='verified' else 'rejected'}
                for x in data['orders']]
            c.db.execute('UPDATE native_api_commands SET result=? WHERE id=?',(json.dumps(result),request_id))
        return result

    def _resolve_receipt_bound_entries(self):
        """Evidence-only recovery for partial fills before first verification.

        Never re-submit an entry, guess a ticket, or bypass unfinished API commands.
        Missing receipts/checkpoints remain a manual reconciliation requirement.
        """
        data = self.control._read()
        if data is None:
            return
        for item in self._items(data):
            if item['phase'] not in ('dispatching', 'unresolved'):
                continue
            found = self.control.db.execute('SELECT id FROM recovery_outbox WHERE id=?',
                                            (item['key'],)).fetchone()
            if found is None:
                continue
            row = self.session.executor.read(item['key'])
            ticket = (row.get('receipt') or {}).get('order')
            if type(ticket) is not int or ticket <= 0:
                continue
            if row['state'] not in ('UNKNOWN', 'ACKNOWLEDGED', 'VERIFIED'):
                continue
            self.control.resolve_initial_partial(item['intent']['range_id'], order_key=item['key'])

    def reconcile(self, *, expected_context, expected_generation):
        """Explicit evidence-only recovery. Never re-send or abandon checkpoints.

        A missing outbox or ambiguous broker result stays unresolved. Interrupted
        HTTP commands complete only when their ENTIRE concrete effect is proven.
        """
        c = self.control
        with self.session._lock:
            c._guard()
            if expected_context != self.context_id() or expected_generation != (c._read() or {}).get('generation'):
                raise ValueError('Displayed trading context or generation changed')
            issues = []
            def attempt(operation):
                try: operation()
                except (ValueError, RuntimeError) as exc: issues.append(str(exc))
            attempt(self.session.reconcile)
            if c._read() is not None:
                attempt(c.reconcile)
                attempt(self._resolve_receipt_bound_entries)
                data = c._read()
                for group, helper in (('protection_commands',self.protection),
                                      ('reverse_commands',self.reverse),
                                      ('reentry_commands',self.reentry),
                                      ('retirement_commands',self.retirement)):
                    for key, record in data.get(group, {}).items():
                        if record['phase'] != 'verified':
                            attempt(lambda key=key,helper=helper: helper.reconcile(key))
                for item in self._items(c._read()):
                    observation = c._read().get('entry_observations', {}).get(str(item.get('broker_order')), {})
                    if observation.get('cancellation', {}).get('phase') not in (None,'verified'):
                        attempt(lambda item=item: self.observer.reconcile_partial(item['intent']['range_id'],order_key=item['key']))
                for rid, record in c._read().get('range_close_commands', {}).items():
                    if record['phase'] != 'verified': attempt(lambda rid=rid:self.close.reconcile(rid))
                if c._read().get('range_management'): attempt(self.ranges.reconcile_cancel)
                data = c._read()
                for key, encoded in c.db.execute("SELECT id,payload FROM native_api_commands WHERE phase='dispatching'").fetchall():
                    payload = json.loads(encoded)
                    if payload.get('expected_context') != self.context_id(): continue
                    proven = False
                    if payload.get('kind') == 'ranges' and data.get('generation') != payload.get('expected_generation'):
                        engine = Engine()
                        try:
                            engine.replace_ranges(payload['ranges'],payload['initial_sl_distance'],payload['reentry_distance'])
                            planned = {'settings':engine.data['settings'], 'ranges':[
                                {k:r[k] for k in ('id','price','side','volume')} for r in engine.data['ranges']]}
                            same_plan = data.get('strategy_config') == planned
                            proven = same_plan and all(x['phase']=='verified' for x in data['orders'])
                            if same_plan and not proven and not issues:
                                recovered = self._finish_partial_initial_batch(key,data['generation'],
                                    'Evidence-only recovery: remaining ranges were never dispatched')
                                if recovered is not None: continue
                        except (KeyError, ValueError): pass
                    elif data.get('generation') == payload.get('expected_generation'):
                        if payload.get('kind') == 'cancel':
                            proven = data.get('range_management', {}).get('phase') == 'waiting'
                        elif payload.get('kind') == 'close_cancel':
                            proven = data.get('range_close_commands', {}).get(payload.get('range_id'), {}).get('phase') == 'verified'
                    if proven and not issues:
                        with c.db:
                            c.db.execute("UPDATE native_api_commands SET phase='completed' WHERE id=?",(key,))
                            result = self.state(); result['command_outcome'] = 'confirmed'
                            c.db.execute('UPDATE native_api_commands SET result=? WHERE id=?',(json.dumps(result),key))
            data = c._read()
            if (self._unfinished_api_command() or (data and c._unresolved_entry_cancellations(data))):
                self.session.ready = False
            self.last_tick = {'status':'reconciled' if self.session.ready else 'reconciliation_required', 'issues':issues}
            return self.state()

    def _mark_interrupted_ranges(self):
        """A polled strategy cannot pre-arm a successor before an unseen fill.

        If a whole range is already flat with classified closes and no trigger,
        require explicit fresh ranges. Never invent a late market entry/rearm.
        """
        c = self.control; data = c._read()
        if data is None: return []
        for configured in data['strategy_config']['ranges']:
            rid = configured['id']
            if data.get('range_modes',{}).get(rid,'active') != 'active': continue
            rows = [r for r in data.get('close_ledger',{}).values() if r['range_id']==rid]
            if not rows or any(not r['fully_closed'] or r['classification'] is None for r in rows): continue
            mapped = [x for x in self._items(data) if x['intent']['range_id']==rid]
            if any(x['phase']!='verified' for x in mapped): continue
            empty = True
            for item in mapped:
                evidence = self.observer.observe(rid,order_key=item['key'])['evidence']
                if evidence['position'] is not None or evidence['pending_order'] is not None:
                    empty = False
            if empty:
                current = c._read(); current.setdefault('range_modes',{})[rid]='interrupted'
                c._save(current)
        return [rid for rid,mode in c._read().get('range_modes',{}).items() if mode in ('interrupted','waiting_exact_level')]

    def tick(self):
        """One bounded snapshot/advance pass; no recursive catch-up or retries."""
        c = self.control
        with self.session._lock:
            c._guard()
            self.last_tick = {'status': 'running', 'issues': []}
            try:
                if self._unfinished_api_command():
                    self.session.ready = False
                    self.last_tick['status'] = 'reconciliation_required'
                    return self.state()
                data = c._read()
                if data is None:
                    self.last_tick['status'] = 'waiting_ranges'
                    return self.state()
                self._resolve_receipt_bound_entries()
                data = c._read()
                rejected = c.protection_only_rejections(data)
                if rejected:
                    self.session.reconcile()  # evidence only, no entry retry
                    if not self.session.ready: raise RuntimeError('Executor uncertainty blocks protection')
                    entries = [item for item in self._items(data) if item['phase']=='verified']
                    seen, live = {}, {}
                    for item in entries:
                        rid = item['intent']['range_id']
                        seen[item['key']] = self.observer.observe(rid, order_key=item['key'])
                        evidence = seen[item['key']]['evidence']
                        if evidence['position'] is not None:
                            live.setdefault(rid,set()).add(evidence['position_identifier'])
                    if any(len(pids)>1 for pids in live.values()):
                        raise RuntimeError('Multiple live positions in one range require reconciliation')
                    b = self.session.transport.binding
                    snapshot = self.session.transport.snapshot(dict(account=b.account,server=b.server,symbol=b.symbol,magic=c.magic))
                    for field, evidence_field in (('orders','pending_order'),('positions','position')):
                        actual = {x['ticket'] for x in snapshot[field] if x['symbol']==b.symbol and x['magic'] in b.magics}
                        expected = {o['evidence'][evidence_field]['ticket'] for o in seen.values() if o['evidence'][evidence_field] is not None}
                        if actual != expected: raise RuntimeError('Owned exposure changed or is not mapped')
                    for item in entries:
                        rid = item['intent']['range_id']
                        if data.get('range_modes',{}).get(rid,'active') != 'active': continue
                        observed = seen[item['key']]
                        if observed['phase'] == 'partial_remainder_pending':
                            # Cancel only this receipt-bound retained position's
                            # unfilled remainder; no replacement entry is allowed.
                            self.session.reconcile()
                            self.observer.cancel_partial_remainder(rid, order_key=item['key'])
                            observed = self.observer.observe(rid, order_key=item['key'])
                            if not c.protection_only_rejections(c._read()):
                                raise RuntimeError('Partial remainder cancellation requires reconciliation')
                            if observed['phase'] == 'partial_remainder_pending':
                                raise RuntimeError('Partial remainder still pending')
                        if observed['phase'] == 'closed_before_protection' or observed['evidence']['exit_deals']:
                            row = self.ledger.record(item['broker_order'])
                            if row['fully_closed'] and row['classification'] is None and row['stop_evidence_requests']:
                                try: self.ledger.resolve_sl(item['broker_order'])
                                except StopEvidenceUnavailable: pass
                        if observed['phase'] == 'awaiting_protection':
                            self.session.reconcile()
                            # Another already-placed range can fill while one
                            # reverse was rejected. Confirm its actual-fill SL
                            # through the existing narrow InitialStopProof first.
                            self.protection.initial(rid, order_key=item['key'])
                            self.session.reconcile()
                            self.protection.support(rid, order_key=item['key'])
                    self.session.ready = False  # only proven protective SL path is admitted
                    self.last_tick = {'status':'degraded_protection_only',
                                      'issues':['Rejected pending creation; operator reconciliation required']}
                    return self.state()
                if (c._unresolved_entry_cancellations(data) or not self.session.ready
                        or any(x['phase'] not in ('staged', 'verified', 'rejected') for x in data['orders'])):
                    self.last_tick['status'] = 'reconciliation_required'
                    return self.state()
                items = [x for x in self._items(data) if x['phase'] == 'verified']
                observed = {}
                # Complete read pass before any send; refuse two live positions
                # within a range instead of arbitrarily selecting one.
                live = {}
                for item in items:
                    rid = item['intent']['range_id']
                    o = self.observer.observe(rid, order_key=item['key'])
                    observed[item['key']] = o
                    if o['evidence']['position'] is not None:
                        live.setdefault(rid, set()).add(o['evidence']['position_identifier'])
                if any(len(pids) > 1 for pids in live.values()):
                    raise RuntimeError('Multiple live positions in one range require reconciliation')
                b = self.session.transport.binding
                snapshot = self.session.transport.snapshot(dict(account=b.account, server=b.server,
                    symbol=b.symbol, magic=c.magic))
                owned = lambda x: x.get('symbol') == b.symbol and x.get('magic') in b.magics
                for field, evidence_field in (('orders', 'pending_order'), ('positions', 'position')):
                    actual = {x['ticket'] for x in snapshot[field] if owned(x)}
                    expected = {o['evidence'][evidence_field]['ticket'] for o in observed.values()
                                if o['evidence'][evidence_field] is not None}
                    if actual != expected:
                        raise RuntimeError('Owned exposure changed or is not mapped; reconcile before dispatch')
                for item in items:
                    o = observed[item['key']]
                    if o['phase'] == 'closed_before_protection' or o['evidence']['exit_deals']:
                        row = self.ledger.record(item['broker_order'])
                        if row['fully_closed'] and row['classification'] is None and row['stop_evidence_requests']:
                            try: self.ledger.resolve_sl(item['broker_order'])
                            except StopEvidenceUnavailable as exc:
                                self.last_tick['issues'].append(str(exc))
                data = c._read()
                for rid, total in self.ledger.totals().items():
                    if total['full_sl_count'] >= 24 and not live.get(rid):
                        if any(observed[x['key']]['evidence']['pending_order'] is not None
                               for x in items if x['intent']['range_id'] == rid):
                            raise RuntimeError('Final SL limit reached with pending exposure')
                        data.setdefault('range_modes', {})[rid] = 'manual'
                c._save(data)
                # Protective transitions can proceed while close evidence is
                # pending; new successor lots may not.
                for item in items:
                    rid, key = item['intent']['range_id'], item['key']
                    if c._read().get('range_modes', {}).get(rid, 'active') != 'active': continue
                    o = observed[key]
                    if o['phase'] == 'partial_remainder_pending':
                        self.observer.cancel_partial_remainder(rid, order_key=key)
                        o = self.observer.observe(rid, order_key=key)
                    if o['phase'] == 'awaiting_protection':
                        self.protection.initial(rid, order_key=key)
                        self.protection.support(rid, order_key=key)
                        totals = self.ledger.totals().get(rid, {})
                        if totals.get('classification_pending_count', 0): continue
                        if key in c._read().get('reverse_commands', {}) or key in c._read().get('reentry_commands', {}):
                            self.retirement.retire(rid, order_key=key)
                        if totals.get('full_sl_count', 0) < 23:
                            self.reverse.arm(rid, order_key=key)
                    elif o['phase'] == 'closed_before_protection':
                        data = c._read()
                        row = data.get('close_ledger', {}).get(str(item['broker_order']), {})
                        totals = self.ledger.totals().get(rid, {})
                        if row.get('classification') != 'support_sl' or totals.get('classification_pending_count', 0): continue
                        if totals.get('full_sl_count', 0) >= 24 or key + ':support-reentry' in data.get('reentry_commands', {}): continue
                        opposite = data.get('reverse_commands', {}).get(key + ':initial-reverse')
                        if opposite and float(observed.get(opposite['key'], {}).get('evidence', {}).get('filled_volume', 0)) > 0: continue
                        self.reentry.arm(rid, order_key=key)
                if c._unresolved_entry_cancellations(c._read()) or not self.session.ready:
                    self.last_tick['status'] = 'reconciliation_required'
                    self.last_tick['issues'].append('Native checkpoint not confirmed')
                else:
                    interrupted = self._mark_interrupted_ranges()
                    for rid in interrupted:
                        self.interrupted.advance(rid)
                    modes = c._read().get('range_modes',{})
                    waiting = [rid for rid in interrupted if modes.get(rid)=='waiting_exact_level']
                    stopped = [rid for rid in interrupted if modes.get(rid)=='interrupted']
                    if c._unresolved_entry_cancellations(c._read()) or not self.session.ready:
                        self.last_tick['status']='reconciliation_required'
                        self.last_tick['issues'].append('Interrupted recovery outcome requires reconciliation')
                    elif waiting:
                        self.last_tick['status']='waiting_exact_level'
                        self.last_tick['issues'].append('Waiting for original pending level in ranges: '+','.join(waiting))
                    elif stopped:
                        self.last_tick['status'] = 'interrupted_chain'
                        self.last_tick['issues'].append('No provable automatic successor in ranges: '+','.join(stopped)+'. Set new ranges explicitly.')
                    else:
                        self.last_tick['status'] = 'waiting_evidence' if self.last_tick['issues'] else 'ok'
            except (ValueError, RuntimeError) as exc:
                self.session.ready = False
                self.last_tick = {'status': 'reconciliation_required', 'issues': [str(exc)]}
            return self.state()
