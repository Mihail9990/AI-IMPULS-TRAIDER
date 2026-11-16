"""Candidate panel-to-EA outbox. Transport only: no MT5 import or trade decisions.

Not selected by runtime.py until the native owner's migration is complete.
A published request is never republished automatically, even if its file vanished.
That trades availability for an honest UNKNOWN result across the filesystem /
broker boundary. A receipt of acceptance does not imply broker execution.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

ACTIONS = frozenset({'start', 'stop_new_entries', 'cancel_triggers', 'close_all',
                     'sl_at_entry', 'sl_distance', 'modify_level', 'reverse_ladder',
                     'triggers_at_sl', 'save_settings', 'resume_manual', 'select_profile',
                     'save_sl_monitor', 'reconcile', 'reset_pause', 'writeoff_recovery', 'cancel_initial'})
IDENTITY_FIELDS = frozenset({'installation', 'login', 'server', 'symbol'})
FINAL = frozenset({'BROKER_CONFIRMED', 'APPLIED', 'REJECTED'})
TRANSITIONS = {
    'AWAITING_EA': {'AWAITING_EA', 'EA_ACCEPTED', 'AWAITING_BROKER', 'UNKNOWN', *FINAL},
    'UNKNOWN': {'UNKNOWN', 'EA_ACCEPTED', 'AWAITING_BROKER', *FINAL},
    'EA_ACCEPTED': {'EA_ACCEPTED', 'AWAITING_BROKER', 'UNKNOWN', *FINAL},
    'AWAITING_BROKER': {'AWAITING_BROKER', 'UNKNOWN', *FINAL},
    'BROKER_CONFIRMED': {'BROKER_CONFIRMED'},
    'APPLIED': {'APPLIED'},
    'REJECTED': {'REJECTED'},
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':'))


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Canonical UUID required')
    return value


class NativeOutbox:
    def __init__(self, directory, identity):
        if not isinstance(identity, dict) or set(identity) != IDENTITY_FIELDS:
            raise ValueError('Explicit native installation/account identity required')
        identifier(identity['installation'])
        if any(not isinstance(identity[k], str) or not identity[k] for k in IDENTITY_FIELDS):
            raise ValueError('Identity fields must be nonempty strings')
        if not identity['login'].isascii() or not identity['login'].isdigit() or int(identity['login']) <= 0:
            raise ValueError('Positive account login required')
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.requests = self.root / 'native_requests'
        self.requests.mkdir(exist_ok=True)
        self.identity = json.loads(encoded(identity))
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'native_panel_outbox.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, body TEXT NOT NULL,
                digest TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT);
        ''')
        try:
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO identity VALUES(1,?)', (encoded(identity),))
                if self.db.execute('SELECT body FROM identity').fetchone()[0] != encoded(identity):
                    raise ValueError('Outbox belongs to a different installation/account')
        except BaseException:
            self.db.close()
            raise
        self.fault = None

    def close(self):
        self.db.close()

    def submit(self, request_id, action, expected_revision):
        """Return promptly; same ID is a lookup, never a replay. EA validates semantics."""
        identifier(request_id)
        if not isinstance(action, dict) or action.get('type') not in ACTIONS:
            raise ValueError('Unsupported high-level action; manual order entry is unavailable')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('Nonnegative native snapshot revision required')
        payload = dict(protocol='native_panel_request_v1', id=request_id,
                       identity=self.identity, expected_revision=expected_revision, action=action)
        body = encoded(payload)
        if len(body.encode('utf-8')) > 8192:
            raise ValueError('Request exceeds transport limit')
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        wire = dict(protocol='native_panel_wire_v1', id=request_id, digest=digest,
                    **self.identity, revision=str(expected_revision), action=action['type'])
        for key, value in action.items():
            if key == 'type':
                continue
            if key in wire:
                raise ValueError('Action shadows protocol identity')
            if isinstance(value, (list, dict)):
                value = encoded(value)
            elif isinstance(value, bool):
                value = 'true' if value else 'false'
            else:
                value = str(value)
            wire[key] = value
        if any(any(c in str(v) for c in ('\n', '\r', '=')) for v in wire.values()):
            raise ValueError('Unsafe wire field')
        wire_body = '\n'.join(k+'='+v for k,v in wire.items())+'\n'
        with self.lock:
            old = self.db.execute('SELECT body FROM requests WHERE id=?', (request_id,)).fetchone()
            if old:
                if old[0] != body:
                    raise ValueError('Request ID reused with a different payload')
                return self.status(request_id)
            # Register uncertainty BEFORE exposing any file to an EA. A crash
            # after this commit but before publication leaves UNKNOWN, not retry.
            try:
                with self.db:
                    self.db.execute('INSERT INTO requests VALUES(?,?,?,?,NULL)',
                                    (request_id, body, digest, 'UNKNOWN'))
            except sqlite3.IntegrityError:
                # Another panel process won the durable identity reservation.
                old = self.db.execute('SELECT body FROM requests WHERE id=?', (request_id,)).fetchone()
                if old is None or old[0] != body:
                    raise ValueError('Concurrent request identity conflict') from None
                return self.status(request_id)
            temporary = self.requests / (request_id + '.tmp')
            destination = self.requests / (request_id + '.json')
            try:
                if self.fault:
                    self.fault('before_publication')
                if destination.exists():
                    raise OSError('Unexpected existing request; preserve it and reconcile')
                with temporary.open('x', encoding='utf-8', newline='\n') as handle:
                    handle.write(encoded(dict(payload=payload, digest=digest)))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                # JSON is the audit envelope; only .cmd is consumed by the EA.
                wire_temporary = self.requests / (request_id + '.wire.tmp')
                wire_destination = self.requests / (request_id + '.cmd')
                if wire_destination.exists():
                    raise OSError('Unexpected existing wire request')
                with wire_temporary.open('x', encoding='utf-8', newline='\n') as handle:
                    handle.write(wire_body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(wire_temporary, wire_destination)
                if self.fault:
                    self.fault('after_publication')
                with self.db:
                    self.db.execute("UPDATE requests SET status='AWAITING_EA' WHERE id=? AND status='UNKNOWN' AND receipt IS NULL", (request_id,))
            except OSError:
                # Do not call this REJECTED: publication may already have happened.
                pass
            return self.status(request_id)

    def status(self, request_id):
        identifier(request_id)
        with self.lock:
            row = self.db.execute('SELECT status,receipt FROM requests WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise KeyError(request_id)
            return dict(id=request_id, status=row[0], receipt=json.loads(row[1]) if row[1] else None)

    def accept_receipt(self, receipt):
        """Strict identity/version gate; not proof of the EA's broker reconciliation."""
        required = {'protocol', 'id', 'identity', 'digest', 'sequence', 'status', 'reason'}
        if not isinstance(receipt, dict) or set(receipt) != required or receipt['protocol'] != 'native_panel_receipt_v1':
            raise ValueError('Invalid receipt schema')
        identifier(receipt['id'])
        if receipt['identity'] != self.identity or receipt['status'] not in TRANSITIONS:
            raise ValueError('Receipt identity/status mismatch')
        if type(receipt['sequence']) is not int or receipt['sequence'] <= 0:
            raise ValueError('Positive durable receipt sequence required')
        if not isinstance(receipt['reason'], str) or len(receipt['reason']) > 1024:
            raise ValueError('Bounded receipt reason required')
        body = encoded(receipt)
        with self.lock, self.db:
            row = self.db.execute('SELECT digest,status,receipt FROM requests WHERE id=?', (receipt['id'],)).fetchone()
            if row is None or row[0] != receipt['digest']:
                raise ValueError('Receipt does not match the immutable request')
            old = json.loads(row[2]) if row[2] else None
            if old and receipt['sequence'] <= old['sequence']:
                if receipt['sequence'] == old['sequence'] and old != receipt:
                    raise ValueError('Receipt revision changed content')
                return self.status(receipt['id'])
            if receipt['status'] not in TRANSITIONS[row[1]]:
                raise ValueError('Receipt would regress a confirmed result')
            self.db.execute('UPDATE requests SET status=?,receipt=? WHERE id=?',
                            (receipt['status'], body, receipt['id']))
            return self.status(receipt['id'])
