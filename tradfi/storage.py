"""One durable owner; no network inside transactions. Commands and events live in snapshot."""
import json
import sqlite3
from pathlib import Path


class DurabilityError(BaseException):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, event TEXT NOT NULL, payload TEXT NOT NULL)')
        self.fault = None

    def load(self):
        row = self.db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        if data.get('schema') != 1:
            raise DurabilityError('Unsupported TradFi database schema')
        return data

    def save(self, data, event):
        try:
            payload = json.dumps(data, sort_keys=True, allow_nan=False)
            old = self.db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
            if old and old[0] == payload:
                return
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('INSERT OR REPLACE INTO snapshot VALUES (1, ?)', (payload,))
            cycle = data.get('cycle') or {}
            summary = json.dumps({'phase': data['phase'], 'cycle_id': cycle.get('id'),
                                  'recovery': cycle.get('recovery'), 'currency': data.get('currency'),
                                  'commands': len(data['commands'])})
            self.db.execute('INSERT INTO audit(event,payload) VALUES (?,?)', (event, summary))
            if self.fault:
                self.fault(event)
            self.db.execute('COMMIT')
        except Exception as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise DurabilityError('SQLite commit failed; stop owner and reload') from exc

    def close(self):
        self.db.close()
