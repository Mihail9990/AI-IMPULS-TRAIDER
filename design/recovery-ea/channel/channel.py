"""OFFLINE protocol prototype; no MT5 imports, network, execution, or replay loop."""
from pathlib import Path
import hashlib
import json
import os
import re
import tempfile
import time
import uuid

VERSION = 1
LIMIT = 65536
OPS = {'configure_ranges', 'manual_wait', 'manual_cancel'}
STATES = {'accepted', 'applied', 'rejected', 'unknown'}

class ProtocolError(ValueError): pass


def ident(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ProtocolError('canonical UUID required')
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('ascii')


def digest(value): return hashlib.sha256(encode(value)).hexdigest()


def read_json(path):
    path = Path(path)
    if path.is_symlink(): raise ProtocolError('symlink rejected')
    with path.open('rb') as stream: raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT: raise ProtocolError('oversized file')
    def pairs(items):
        obj = {}
        for k, v in items:
            if k in obj: raise ProtocolError('duplicate JSON key')
            obj[k] = v
        return obj
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('nonfinite')))
    except (UnicodeError, json.JSONDecodeError) as exc: raise ProtocolError('invalid JSON') from exc
    if not isinstance(result, dict): raise ProtocolError('object required')
    return result


def atomic_new(path, value):
    """Publish without overwrite, using same-volume hard link. Fail closed if unsupported.
    Temporary files are ignored by EA. ACL-protected local NTFS deployment prerequisite.
    """
    path = Path(path)
    raw = encode(value)
    if len(raw) > LIMIT: raise ProtocolError('oversized command')
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path)  # atomic create-only; never overwrite identity
    finally:
        os.unlink(temporary)


_PRICE = re.compile(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,16})?")


def validate_payload(operation, payload):
    if not isinstance(operation, str) or operation not in OPS or not isinstance(payload, dict):
        raise ProtocolError('unsupported command')
    if operation in {'manual_wait', 'manual_cancel'}:
        if payload: raise ProtocolError(f'{operation} takes no parameters')
        return
    if payload.keys() != {'ranges'}:
        raise ProtocolError('configure_ranges requires ranges')
    ranges = payload['ranges']
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 2:
        raise ProtocolError('provide one or two exact-price triggers')
    seen = set()
    for row in ranges:
        if not isinstance(row, dict) or row.keys() != {'range_id', 'side', 'price'}:
            raise ProtocolError('each range requires range_id, side and price')
        range_id, side, price = row['range_id'], row['side'], row['price']
        if type(range_id) is not int or range_id not in (1, 2) or range_id in seen:
            raise ProtocolError('each range id must be unique (1 or 2)')
        if side not in ('BUY', 'SELL'):
            raise ProtocolError('side must be BUY or SELL')
        if not isinstance(price, str) or not _PRICE.fullmatch(price) or not re.search('[1-9]', price):
            raise ProtocolError('price must be a positive plain decimal string')
        seen.add(range_id)


class Channel:
    def __init__(self, root, scope_id, *, clock=time.time):
        self.root = Path(root)
        self.scope_id = ident(scope_id)
        self.clock = clock
        # Never auto-create or change ACLs; installer must provision private root.
        for folder in (self.root, self.root/'sessions'):
            if folder.is_symlink() or not folder.is_dir(): raise ProtocolError('private channel folders required')

    def state(self):
        value = read_json(self.root/'state.json')
        if type(value.get('version')) is not int or value['version'] != VERSION or value.get('scope_id') != self.scope_id:
            raise ProtocolError('state scope/version mismatch')
        ident(value.get('boot_id')); ident(value.get('context_id')); ident(value.get('generation_id'))
        if value.get('mode') not in {'ready', 'manual_wait', 'manual_cancel', 'unknown'}:
            raise ProtocolError('bad mode')
        return value

    def _session_file(self, boot_id, category, command_id):
        """Resolve only an explicit session, never the former flat queue."""
        if category not in {'commands', 'receipts'}: raise ProtocolError('bad session category')
        session = self.root/'sessions'/ident(boot_id)
        bucket = session/category
        for folder in (self.root, self.root/'sessions', session, bucket):
            if folder.is_symlink() or not folder.is_dir():
                raise ProtocolError('private session folders required')
        return bucket/f'{ident(command_id)}.json'

    def command_record(self, command_id, *, boot_id):
        if self.state()['boot_id'] != boot_id: raise ProtocolError('session changed')
        return read_json(self._session_file(boot_id, 'commands', command_id))

    def publish(self, operation, payload, *, command_id, expected_boot, expected_context, expected_generation,
                confirmed=False):
        """Publish ONCE after local caller confirmation. Never retry this on timeout.
        No generic arbitrary payload or trading method is exposed.
        """
        if confirmed is not True: raise ProtocolError('explicit confirmation required')
        ident(command_id); ident(expected_boot); ident(expected_context); ident(expected_generation)
        validate_payload(operation, payload)
        state = self.state()
        if (state['boot_id'], state['context_id'], state['generation_id']) != (expected_boot, expected_context, expected_generation):
            raise ProtocolError('context changed; new confirmation required')
        if state['mode'] == 'unknown': raise ProtocolError('status-only state')
        now = int(self.clock())
        cmd = dict(version=VERSION, scope_id=self.scope_id, boot_id=expected_boot,
                   context_id=expected_context, generation_id=expected_generation,
                   command_id=command_id, created_at=now, operation=operation, payload=payload)
        cmd['content_sha256'] = digest(cmd)
        # Existing ID always errors, even identical. Read status instead of resubmission.
        atomic_new(self._session_file(expected_boot, 'commands', command_id), cmd)
        return cmd

    def receipt(self, command):
        if command.get('scope_id') != self.scope_id:
            raise ProtocolError('receipt scope mismatch')
        boot_id = ident(command['boot_id'])
        if self.state()['boot_id'] != boot_id: raise ProtocolError('session changed')
        path = self._session_file(boot_id, 'receipts', command['command_id'])
        try: value = read_json(path)
        except FileNotFoundError:
            return {'status': 'unknown', 'reason': 'no_receipt', 'retry_allowed': False}
        for field in ('version','scope_id','boot_id','context_id','generation_id','command_id','content_sha256'):
            if value.get(field) != command.get(field): raise ProtocolError('receipt binding mismatch')
        if value.get('status') not in STATES: raise ProtocolError('invalid receipt state')
        if self.state()['boot_id'] != boot_id: raise ProtocolError('session changed')
        # Never translate accepted into applied or unknown into safe-to-retry.
        return dict(value, retry_allowed=False)
