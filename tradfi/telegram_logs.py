"""Optional outbound-only diagnostic transport. Never reads or mutates trading state."""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from .context_diagnostics import project, VERSION

INTERVAL = 600
MAX_BATCH = 900_000
MAX_QUEUE = 50_000_000


class DeliveryUnknown(Exception):
    """Telegram may have accepted the document; do not automatically repeat."""


def send_document(token, chat_id, payload, batch_id):
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="live-logs-{batch_id}.txt"\r\n'
            'Content-Type: text/plain; charset=utf-8\r\n\r\n').encode() + payload + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendDocument', data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    # No redirects/proxy: do not disclose token to another host or ambient proxy.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=20) as response:
            result = json.loads(response.read(65536))
    except urllib.error.HTTPError as error:
        if error.code in (400, 401, 403, 413, 429):
            return None  # Explicit rejection; retained and retried next interval.
        raise DeliveryUnknown() from None
    except Exception:
        raise DeliveryUnknown() from None
    if result.get('ok') is False:
        return None
    message = result.get('result', {})
    if result.get('ok') is not True or type(message.get('message_id')) is not int or str(message.get('chat', {}).get('id')) != chat_id:
        raise DeliveryUnknown()
    return message['message_id']


class TelegramLogs:
    def __init__(self, path, token, chat_id, *, sender=send_document, clock=time.time):
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token) or not re.fullmatch(r'[1-9]\d*', chat_id):
            raise ValueError('Invalid Telegram configuration (private chat required)')
        self.token, self.chat_id = token, chat_id
        self.sender, self.clock = sender, clock
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        path = Path(path)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Unsafe outbox path')
        # Single owner across processes, including Windows VPS. Held for lifetime.
        lock_path = Path(str(path)+'.lock')
        if lock_path.is_symlink():
            raise ValueError('Unsafe lock path')
        self.owner_lock = open(lock_path, 'a+b')
        self.owner_lock.seek(0);self.owner_lock.write(b'0');self.owner_lock.flush();self.owner_lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.owner_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_lock.close()
            raise ValueError('Outbox already owned') from None
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS delivery(id INTEGER PRIMARY KEY CHECK(id=1), batch TEXT, through_id INTEGER,
        status TEXT NOT NULL, next_at REAL NOT NULL, message_id INTEGER, dropped INTEGER NOT NULL DEFAULT 0,
        destination TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS manual_requests(request_id TEXT PRIMARY KEY, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS manual_aliases(request_id TEXT PRIMARY KEY, target TEXT NOT NULL);''')
        self.db.execute('INSERT OR IGNORE INTO delivery(id,status,next_at,destination) VALUES(1,?,?,?)', ('READY', clock()+INTERVAL, chat_id))
        if self.db.execute('SELECT destination FROM delivery').fetchone()[0] != chat_id:
            self.db.close();self.owner_lock.close()
            raise ValueError('Outbox destination changed')
        # A crash after send cannot prove non-delivery. Retain and require review.
        self.db.execute("UPDATE delivery SET status='UNKNOWN' WHERE status='SENDING'")
        self.db.execute("UPDATE manual_requests SET result='UNKNOWN' WHERE result='SENDING'")
        self.db.commit()

    def append(self, record):
        source = json.loads(record)
        if source.get('scope') != 'live':
            return
        safe = {'version': VERSION, 'scope': 'live', 'context': project(source.get('context', {}))}
        stamp = source.get('observed_utc', '')
        if isinstance(stamp, str) and re.fullmatch(r'[0-9T:.+Z-]{20,40}', stamp):
            safe['observed_utc'] = stamp
        body = json.dumps(safe, ensure_ascii=False)
        if len(body.encode()) > 65536:
            raise ValueError('Diagnostic record too large')
        with self.lock, self.db:
            size = self.db.execute('SELECT COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM records').fetchone()[0]
            if size + len(body.encode()) > MAX_QUEUE:
                self.db.execute('UPDATE delivery SET dropped=dropped+1')
                return  # Existing undelivered data never evicted; loss is counted.
            self.db.execute('INSERT INTO records(body) VALUES(?)', (body,))

    def request_send(self, request_id):
        """Persist intent only. The sole sender worker performs outbound I/O."""
        if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', request_id):
            raise ValueError('Invalid request ID')
        with self.lock, self.db:
            alias = self.db.execute('SELECT target FROM manual_aliases WHERE request_id=?', (request_id,)).fetchone()
            if alias:
                request_id = alias[0]
            existing = self.db.execute('SELECT result FROM manual_requests WHERE request_id=?', (request_id,)).fetchone()
            if existing is None:
                status = self.db.execute('SELECT status FROM delivery').fetchone()[0]
                active = self.db.execute("SELECT request_id FROM manual_requests WHERE result IN ('QUEUED','SENDING','RETRY') LIMIT 1").fetchone()
                if active or status not in ('UNKNOWN', 'SENDING'):
                    count = self.db.execute('SELECT (SELECT COUNT(*) FROM manual_requests)+(SELECT COUNT(*) FROM manual_aliases)').fetchone()[0]
                    if count >= 10000:
                        raise ValueError('Manual request history full')
                if active:
                    self.db.execute('INSERT INTO manual_aliases VALUES(?,?)', (request_id, active[0]))
                    request_id = active[0]  # Coalesce simultaneous tabs/clicks.
                    if status == 'RETRY':
                        self.db.execute("UPDATE manual_requests SET result='QUEUED' WHERE request_id=?", (request_id,))
                        self.db.execute("UPDATE delivery SET status='QUEUED',next_at=?", (self.clock(),))
                elif status not in ('UNKNOWN', 'SENDING'):
                    # Keep deduplication IDs, never silently forget a prior send.
                    self.db.execute('INSERT INTO manual_requests VALUES(?,?)', (request_id, 'QUEUED'))
                    self.db.execute("UPDATE delivery SET status='QUEUED',next_at=?", (self.clock(),))
        return self.status(request_id)

    def tick(self):
        with self.lock, self.db:
            batch, through, status, due, dropped = self.db.execute('SELECT batch,through_id,status,next_at,dropped FROM delivery').fetchone()
            if status in ('UNKNOWN', 'SENDING') or self.clock() < due:
                return
            manual = self.db.execute("SELECT request_id FROM manual_requests WHERE result IN ('QUEUED','RETRY') LIMIT 1").fetchone()
            manual_id = manual[0] if manual else None
            rows = self.db.execute('SELECT id,body FROM records ORDER BY id LIMIT 1000').fetchall()
            selected, size = [], 0
            for row in rows:
                size += len(row[1].encode()) + 1
                if size > MAX_BATCH:
                    break
                selected.append(row)
            if not selected:
                self.db.execute("UPDATE delivery SET status='READY',next_at=?", (self.clock()+INTERVAL,))
                self.db.execute("UPDATE manual_requests SET result='EMPTY' WHERE request_id=?", (manual_id,))
                return
            batch, through = uuid.uuid4().hex, selected[-1][0]
            payload = (f'LIVE diagnostics; batch={batch}; dropped_at_capacity={dropped}; sanitized=true\n' + '\n'.join(r[1] for r in selected)).encode()
            self.db.execute("UPDATE delivery SET batch=?,through_id=?,status='SENDING'", (batch, through))
            self.db.execute("UPDATE manual_requests SET result='SENDING' WHERE request_id=?", (manual_id,))
        try:
            message_id = self.sender(self.token, self.chat_id, payload, batch)
        except Exception:
            with self.lock, self.db:
                self.db.execute("UPDATE delivery SET status='UNKNOWN'")
                self.db.execute("UPDATE manual_requests SET result='UNKNOWN' WHERE request_id=?", (manual_id,))
            return
        with self.lock, self.db:
            if message_id is not None:
                self.db.execute("UPDATE manual_requests SET result='SENT' WHERE request_id=?", (manual_id,))
                # Only exact delivered range; concurrent newer rows survive.
                self.db.execute('DELETE FROM records WHERE id<=?', (through,))
                self.db.execute("UPDATE delivery SET status='READY',message_id=?,next_at=?", (message_id, self.clock()+INTERVAL))
            else:
                self.db.execute("UPDATE manual_requests SET result='RETRY' WHERE request_id=?", (manual_id,))
                self.db.execute("UPDATE delivery SET status='RETRY',next_at=?", (self.clock()+INTERVAL,))

    def status(self, request_id=None):
        with self.lock:
            status, dropped, batch, message_id = self.db.execute('SELECT status,dropped,batch,message_id FROM delivery').fetchone()
            count = self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
            alias = self.db.execute('SELECT target FROM manual_aliases WHERE request_id=?', (request_id,)).fetchone()
            original_id = request_id
            if alias:
                request_id = alias[0]
            if request_id is None:
                request = self.db.execute('SELECT request_id,result FROM manual_requests ORDER BY rowid DESC LIMIT 1').fetchone()
            else:
                request = self.db.execute('SELECT request_id,result FROM manual_requests WHERE request_id=?', (request_id,)).fetchone()
        return {'request_id': (original_id or request[0]) if request else None, 'last_result': request[1] if request else None, 'status': status, 'queued_records': count, 'dropped_at_capacity': dropped, 'batch': batch, 'message_id': message_id}

    def resolve_unknown(self, batch, *, received):
        """Local operator only: confirm exact .txt batch received or authorize retry."""
        with self.lock, self.db:
            actual, through, status = self.db.execute('SELECT batch,through_id,status FROM delivery').fetchone()
            if status != 'UNKNOWN' or actual != batch:
                raise ValueError('Unknown batch does not match')
            if received:
                self.db.execute('DELETE FROM records WHERE id<=?', (through,))
            self.db.execute("UPDATE manual_requests SET result=? WHERE result='UNKNOWN'", ('SENT' if received else 'RETRY',))
            self.db.execute("UPDATE delivery SET status='READY',next_at=?", (self.clock()+INTERVAL,))

    def start(self):
        def run():
            while not self.stop.wait(1):
                try:
                    self.tick()
                except Exception:
                    pass  # No secret-bearing exception data or trading impact.
        self.worker = threading.Thread(target=run, daemon=True, name='telegram-diagnostics')
        self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=0.1)
        # In-flight HTTP may still own DB; process exit retains SENDING as UNKNOWN.
        if not self.worker or not self.worker.is_alive():
            self.db.close();self.owner_lock.close()


def from_environment(runtime_path):
    token = os.environ.get('AI_IMPULS_TELEGRAM_TOKEN', '')
    chat = os.environ.get('AI_IMPULS_TELEGRAM_CHAT_ID', '')
    if not token and not chat:
        return None
    return TelegramLogs(str(runtime_path)+'.telegram-outbox.sqlite3', token, chat)
