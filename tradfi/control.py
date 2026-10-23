"""Owner-thread command gateway shared by local UI and Telegram.

A durable receipt precedes execution. A crash between execution and completion
leaves UNKNOWN, never permission to replay. No worker accesses Controller/SQLite.
"""
from copy import deepcopy
import hashlib
import json
import queue
import re
import threading


class Conflict(ValueError):
    pass


class ActionRejected(ValueError):
    """Executor positively established that the requested action was refused."""


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

    def _execute(self, action):
        if set(action) != {'type', 'text'} or action['type'] != 'command' or not isinstance(action['text'], str):
            raise ActionRejected('Unsupported action')
        try:
            # Controller.command reports validation refusal with ValueError and
            # never dispatches trading requests synchronously.
            self.controller.command(action['text'])
        except ValueError as exc:
            raise ActionRejected('Controller refused command') from exc

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
        receipt = dict(digest=digest, status='UNKNOWN', revision=revision+1)
        state.setdefault('control_receipts', {})[request['id']] = receipt
        state['control_revision'] = revision + 1
        self.controller.save('control_action_reserved')
        try:
            self.execute(deepcopy(request['action']))
        except ActionRejected:
            status = 'REJECTED'
        except (ValueError, OSError, TimeoutError):
            status = 'UNKNOWN'
        else:
            status = 'APPLIED'
        # The command may have replaced the in-memory state while reconciling.
        receipt = self.controller.state['control_receipts'][request['id']]
        receipt['status'] = status
        self.controller.save('control_action_completed')
        return deepcopy(receipt)


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
        # Receipts remain durable; this bounded transport cache is only presentation.
        while len(self.outcomes) > 256:
            self.outcomes.pop(next(iter(self.outcomes)))
        view = deepcopy(view_factory())
        view['action_results'] = deepcopy(self.outcomes)
        with self.lock:
            self._view = view
