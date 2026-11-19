"""Process-owned native Executor lifecycle, not the finished RECOVERY strategy.

Caller supplies an initialized terminal API. Entering does read-only reconciliation;
only an explicit execute(request, expected) can send. No terminal/login, background
loop or automatic strategy replay. Do not expose directly as a public HTTP API.
"""
import os
import json
from threading import RLock
from .executor import Executor
from .native_transport import NativeMT5Transport


class NativeExecutorSession:
    def __init__(self,api,binding,*,database):
        if not isinstance(database,str) or not database or database==':memory:':
            raise ValueError('Native executor requires a durable database path')
        self._lock=RLock();self._file=None;self._active=False;self.closed=False;self.ready=False
        self.database=database
        self.transport=NativeMT5Transport(api,binding,owner_guard=lambda:self._active and not self.closed)
        self.executor=None

    def __enter__(self):
        with self._lock:
            if self.closed or self._active:raise RuntimeError('Native session is single-use')
            self._file=open(os.path.realpath(self.database)+'.owner.lock','a+b')
            try:
                if os.name=='nt':
                    import msvcrt
                    self._file.seek(0);self._file.write(b'0');self._file.flush();self._file.seek(0)
                    msvcrt.locking(self._file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except Exception:
                self._file.close();self._file=None;self.closed=True
                raise RuntimeError('Another process owns this database')
            try:
                self._active=True
                self.executor=Executor(self.transport,self.database)
                self.transport.validator._session()
                self._bind_database()
                self.reconcile()
                return self
            except BaseException:
                self.close();raise

    def _bind_database(self):
        binding=self.transport.binding
        identity=json.dumps({'account':binding.account,'server':binding.server,
            'symbol':binding.symbol,'magics':sorted(binding.magics)},sort_keys=True)
        db=self.executor.db
        # Inspect pre-existing outbox scopes before admitting an older database.
        for (payload,) in db.execute('SELECT payload FROM recovery_outbox'):
            expected=json.loads(payload)['expected']
            self.transport.validator._scope(expected)
        db.execute('CREATE TABLE IF NOT EXISTS native_runtime_binding (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)')
        row=db.execute('SELECT identity FROM native_runtime_binding WHERE id=1').fetchone()
        if row is not None and row[0]!=identity:
            raise ValueError('Database belongs to another native runtime binding')
        with db:
            db.execute('INSERT OR IGNORE INTO native_runtime_binding VALUES(1,?)',(identity,))

    def reconcile(self):
        """Evidence-only pass; UNKNOWN is never retried or guessed from comments."""
        with self._lock:
            if not self._active or self.closed:raise RuntimeError('Native session is not active')
            self.ready=False
            self.transport.validator._session()
            rows=self.executor.db.execute("SELECT id FROM recovery_outbox WHERE state IN ('SENT','UNKNOWN','ACKNOWLEDGED')").fetchall()
            for (key,) in rows:self.executor.reconcile(key,refresh=True)
            unresolved=self.executor.db.execute("SELECT id FROM recovery_outbox WHERE state NOT IN ('VERIFIED','REJECTED')").fetchall()
            self.transport.validator._session()
            self.ready=not unresolved
            return {'ready_for_explicit_commands':self.ready,'unresolved_count':len(unresolved),
                    'strategy_ready':False}

    def execute(self,key,request,expected):
        """Explicit one-intent action; blocked after uncertainty until reconciled."""
        with self._lock:
            if not self._active or self.closed or not self.ready:
                raise RuntimeError('Native session is not reconciled for execution')
            # Disable before any durable transition or possible send. This also
            # survives BaseException (interrupt/exit), failed commits and failed
            # reconciliation, not just ordinary broker exceptions.
            self.ready=False
            self.executor.prepare(key,request,expected)
            result=self.executor.submit_once(key)
            if result['state'] not in ('VERIFIED','REJECTED'):
                result=self.executor.reconcile(key,refresh=True)
            unresolved=self.executor.db.execute(
                "SELECT id FROM recovery_outbox WHERE state NOT IN ('VERIFIED','REJECTED')").fetchall()
            self.transport.validator._session()
            self.ready=not unresolved
            return result

    def close(self):
        with self._lock:
            if self.closed:return
            self.closed=True;self.ready=False;self._active=False
            self.transport.invalidate()
            try:
                if self.executor is not None:self.executor.db.close()
            finally:
                if self._file:
                    try:
                        if os.name=='nt':
                            import msvcrt
                            self._file.seek(0);msvcrt.locking(self._file.fileno(),msvcrt.LK_UNLCK,1)
                        else:
                            import fcntl
                            fcntl.flock(self._file.fileno(),fcntl.LOCK_UN)
                    finally:self._file.close();self._file=None

    def __exit__(self,*args):self.close()
