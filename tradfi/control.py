"""Owner-thread command gateway for the authenticated web UI.

A durable receipt precedes execution. A crash between execution and completion
leaves UNKNOWN, never permission to replay. No worker accesses Controller/SQLite.
"""
from copy import deepcopy
import hashlib
import json
import math
import queue
import re
import threading


class Conflict(ValueError):
    pass


class ActionRejected(ValueError):
    """Executor positively established that the requested action was refused."""
    def __init__(self,message,*,public_message=None):
        super().__init__(message)
        self.public_message=public_message


class TransientDataWait(ActionRejected):
    """Only a pre-effect guard may raise this: execution provably did not start."""


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {'id', 'revision', 'action'}:
        raise ValueError('Request requires id, revision, action')
    if not isinstance(request['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', request['id']):
        raise ValueError('Invalid action id')
    revision = request['revision']
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError('Invalid revision')
    if not isinstance(request['action'], dict):
        raise ValueError('Invalid action')
    try:
        encoded = json.dumps(request, sort_keys=True, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ValueError('Action must contain finite JSON values') from exc
    if len(encoded.encode('utf-8')) > 8192:
        raise ValueError('Action too large')
    return json.loads(encoded)


class ActionGateway:
    def __init__(self, controller, execute=None):
        self.controller = controller
        self.owner = threading.get_ident()
        self.execute = execute or self._execute
        # A persisted wait proves no execution began, but is not restart authority.
        changed = False
        for receipt in controller.state.get('control_receipts', {}).values():
            if receipt.get('status') == 'WAITING_DATA':
                self._reject_wait(receipt, 'Ожидание отменено после перезапуска; нужна новая команда.')
                changed = True
        if changed:
            controller.save('control_wait_restart_cancelled')

    @staticmethod
    def _reject_wait(receipt, reason):
        receipt.update(status='REJECTED', reason=reason)
        receipt.pop('deferred', None)

    @staticmethod
    def _immediate(action):
        if action.get('type') == 'command' and set(action) == {'type', 'text'}:
            return isinstance(action['text'], str) and action['text'].strip().lower() in {'/stop', '/status', '/settings', '/resume_time', '/sync_mt5_time', '/use_mt5_raw_time'}
        return action.get('type') in {'cancel_initial', 'v3_cancel_request', 'v3_cancel_entry', 'v3_cancel_link'}

    def _hard_guard(self):
        s = self.controller.state
        return bool(getattr(getattr(self.controller, 'broker', None), 'read_only', False) or s.get('migration_block') or s.get('phase') in {'SAFE_PAUSE', 'MANUAL'} or (s.get('cycle') or {}).get('manual'))

    def _context(self):
        s = self.controller.state
        return deepcopy({key: s.get(key) for key in ('account', 'backend', 'settings', 'basket_settings', 'v3_settings', 'recovery_profiles', 'tp_distances')} | {'cycle_id': (s.get('cycle') or {}).get('id')})

    def _raw_clock(self):
        return getattr(getattr(self.controller,'broker',None),'raw_market',None)

    def _deferred(self,action,context,accepted_utc,accepted_market_time=None):
        value=dict(action=deepcopy(action),context=deepcopy(context),accepted_utc=accepted_utc)
        raw=self._raw_clock()
        if raw is not None:
            value.update(tick_instance=raw.instance,tick_sequence=raw.sequence,
                         accepted_market_time=raw.broker_time if accepted_market_time is None else accepted_market_time)
        return value

    def _cancel_waiting(self, action):
        kind = action.get('type')
        stop = (set(action) == {'type', 'text'} and kind == 'command'
                and isinstance(action['text'], str) and action['text'].strip().lower() == '/stop')
        scopes = {'v3_cancel_request': {'v3_entry'}, 'v3_cancel_entry': {'v3_entry'},
                  'v3_cancel_link': {'v3_link'}, 'cancel_initial': {'start', 'command'}}
        valid = kind in scopes and set(action) == ({'type', 'cycle_id', 'expected_orders'} if kind == 'cancel_initial' else {'type', 'cycle_id'})
        if valid and kind == 'cancel_initial':
            valid = isinstance(action['expected_orders'], list) and all(isinstance(x, str) for x in action['expected_orders'])
        if not stop and not valid:
            return False
        changed = False
        for receipt in self.controller.state.get('control_receipts', {}).values():
            deferred = receipt.get('deferred', {})
            queued = deferred.get('action', {})
            if receipt.get('status') != 'WAITING_DATA':
                continue
            scoped = (not stop and queued.get('type') in scopes[kind]
                      and action.get('cycle_id') == deferred.get('context', {}).get('cycle_id'))
            if scoped and kind == 'cancel_initial' and queued.get('type') == 'command':
                scoped = (isinstance(queued.get('text'), str)
                          and queued['text'].strip().lower() in {'/start', '/start auto'})
            if stop or scoped:
                self._reject_wait(receipt, 'Отменено командой остановки или отмены до исполнения.')
                changed = True
        if changed:
            self.controller.save('control_wait_cancelled')
        return changed

    def flush_waiting(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError('Controller actions require the owner thread')
        for action_id in list(self.controller.state.get('control_receipts', {})):
            receipt = self.controller.state['control_receipts'][action_id]
            if receipt.get('status') != 'WAITING_DATA':
                continue
            blocked = self.controller._time_guard()
            deferred = receipt['deferred']
            if self.controller.state.get('time_block'):
                reason = 'Ожидание завершено блокировкой времени; нужна новая команда после проверки.'
            elif self._hard_guard():
                reason = 'Защитная блокировка: команда отменена до исполнения.'
            elif self._context() != deferred['context']:
                reason = 'Контекст команды изменился; обновите панель и отправьте новую команду.'
            else:
                reason = None
            if reason:
                self._reject_wait(receipt, reason)
                self.controller.save('control_wait_rejected')
                continue
            if blocked or self.controller._time_waiting():
                continue
            raw=self._raw_clock()
            if raw is not None:
                if deferred.get('tick_instance')!=raw.instance:
                    self._reject_wait(receipt,'Наблюдение MT5 перезапущено; нужна новая команда.')
                    self.controller.save('control_wait_instance_changed')
                    continue
                if not raw.available or raw.sequence<=deferred['tick_sequence']:
                    continue
            action = deepcopy(deferred['action'])
            accepted_utc = deferred['accepted_utc']
            receipt.pop('deferred')
            receipt['status'] = 'UNKNOWN'
            self.controller.save('control_action_reserved')
            self._run(action_id, action, accepted_utc=accepted_utc, accepted_context=deferred['context'],
                      accepted_market_time=deferred.get('accepted_market_time'))

    def _run(self, action_id, action, *, accepted_utc=None, accepted_context=None, cancelled_wait=False,accepted_market_time=None):
        # Capture before execution. A pre-effect boundary race must not shift
        # the candle or adopt configuration changed inside the attempted action.
        accepted_context = deepcopy(self._context() if accepted_context is None else accepted_context)
        frozen_utc = accepted_utc
        if accepted_utc is None and hasattr(self.controller, '_time_waiting'):
            safety = getattr(getattr(self.controller, 'broker', None), 'time_safety', None)
            accepted_utc = getattr(safety,'trusted_utc',lambda:None)() if safety is not None else self.controller.clock()
        deferred = None
        self.controller._active_control_id = action_id
        self.controller._deferred_action_accepted_utc = frozen_utc
        self.controller._deferred_action_accepted_market_time = accepted_market_time
        reason = None
        try:
            self.execute(deepcopy(action))
        except TransientDataWait:
            safe_time = (isinstance(accepted_utc, (int, float)) and not isinstance(accepted_utc, bool)
                         and math.isfinite(accepted_utc) and accepted_utc > 0)
            safe_time = safe_time or self._raw_clock() is not None
            if (safe_time and not self._immediate(action) and not self._hard_guard()
                    and not self.controller.state.get('time_block')
                    and self.controller._time_waiting() and self._context() == accepted_context):
                status = 'WAITING_DATA'
                deferred = self._deferred(action,accepted_context,accepted_utc,accepted_market_time)
            else:
                status = 'APPLIED' if cancelled_wait else 'REJECTED'
                reason = 'Контекст или защита изменились до исполнения; нужна новая команда.'
        except ActionRejected as exc:
            status = 'APPLIED' if cancelled_wait else 'REJECTED'
            if exc.public_message is not None:
                reason = str(exc.public_message)[:400]
        except (ValueError, OSError, TimeoutError):
            status = 'UNKNOWN'
        else:
            status = 'APPLIED'
        finally:
            self.controller._active_control_id = None
            self.controller._deferred_action_accepted_utc = None
            self.controller._deferred_action_accepted_market_time = None
        receipt = self.controller.state['control_receipts'][action_id]
        receipt['status'] = status
        if deferred is not None:
            receipt['deferred'] = deferred
        if reason and status == 'REJECTED':
            receipt['reason'] = reason
        self.controller.save('control_action_completed')
        return deepcopy(receipt)

    def _execute(self, action):
        if set(action) != {'type', 'text'} or action['type'] != 'command' or not isinstance(action['text'], str):
            raise ActionRejected('Unsupported action')
        # Only the controller's typed, pre-effect refusals are safe to classify.
        # Generic ValueError can follow a durable effect and must remain UNKNOWN.
        self.controller.command(action['text'])

    def apply(self, request):
        if threading.get_ident() != self.owner:
            raise RuntimeError('Controller actions require the owner thread')
        request = validate_request(request)
        state = self.controller.state
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        receipts = state.get('control_receipts', {})
        previous = receipts.get(request['id'])
        if previous:
            if previous['digest'] != digest:
                raise Conflict('Action id reused with different payload')
            return deepcopy(previous)
        revision = state.get('control_revision', 0)
        if request['revision'] != revision:
            raise Conflict('State changed; refresh preview before Apply')
        action = request['action']
        waiting = False
        if hasattr(self.controller, '_time_waiting'):
            self.controller._time_guard()
            waiting = self.controller._time_waiting() and not self._immediate(action) and not self._hard_guard()
            if self._raw_clock() is not None:
                # Price-dependent commands wait for the NEXT observed tick,
                # including delayed/cached data after reconnect, as requested.
                waiting = not self._immediate(action) and not self._hard_guard()
        receipt = dict(digest=digest, status='WAITING_DATA' if waiting else 'UNKNOWN', revision=revision+1)
        if waiting:
            safety = getattr(getattr(self.controller, 'broker', None), 'time_safety', None)
            accepted = None if self._raw_clock() is not None else (safety.trusted_utc() if safety is not None else self.controller.clock())
            if self._raw_clock() is None and (accepted is None or not math.isfinite(float(accepted))):
                raise ActionRejected('Нет подтверждённого времени для ожидания команды')
            receipt['deferred'] = self._deferred(action,self._context(),accepted)
        state.setdefault('control_receipts', {})[request['id']] = receipt
        state['control_revision'] = revision + 1
        self.controller.save('control_action_waiting_data' if waiting else 'control_action_reserved')
        if waiting:
            return deepcopy(receipt)
        cancelled = self._cancel_waiting(action)
        return self._run(request['id'], action, cancelled_wait=cancelled)



class OwnerMailbox:
    """Transport workers only queue immutable requests and read copied views."""
    def __init__(self, capacity=32):
        self.requests = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self._view = {'status': 'STARTING'}
        self.outcomes = {}

    def submit(self, request):
        request = validate_request(request)
        self.requests.put_nowait(request)
        return {'id': request['id'], 'status': 'QUEUED'}

    def view(self):
        with self.lock:
            return deepcopy(self._view)

    def drain(self, gateway, view_factory, limit=8):
        if threading.get_ident() != gateway.owner:
            raise RuntimeError('Mailbox drain and snapshot publication require the owner thread')
        for _ in range(limit):
            try:
                request = self.requests.get_nowait()
            except queue.Empty:
                break
            try:
                result = gateway.apply(request)
                self.outcomes[request['id']] = {k: result[k] for k in ('status', 'revision')}
            except Conflict:
                self.outcomes[request['id']] = {'status': 'CONFLICT'}
            except ValueError:
                self.outcomes[request['id']] = {'status': 'REJECTED'}
            finally:
                self.requests.task_done()
        # Never release deferred entries ahead of commands still in the mailbox.
        if self.requests.empty() and hasattr(gateway.controller, '_time_waiting'):
            gateway.flush_waiting()
        # Receipts remain durable; this bounded transport cache is only presentation.
        while len(self.outcomes) > 256:
            self.outcomes.pop(next(iter(self.outcomes)))
        view = deepcopy(view_factory())
        durable = deepcopy(view.get('action_results', {}))
        # A conflicting reuse must not erase an existing UNKNOWN receipt.
        published = deepcopy(self.outcomes)
        published.update(durable)
        view['action_results'] = published
        with self.lock:
            self._view = view
