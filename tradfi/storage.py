"""One durable owner; no network inside transactions. Commands and events live in snapshot."""
import json
import sqlite3
from pathlib import Path


class DurabilityError(BaseException):
    pass


_NO_EXPECTED = object()


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

    def save(self, data, event, *, expected=_NO_EXPECTED):
        try:
            payload=json.dumps(data,sort_keys=True,allow_nan=False)
            self.db.execute('BEGIN IMMEDIATE')
            if expected is not _NO_EXPECTED and self.load()!=expected:
                raise DurabilityError('Concurrent/stale controller snapshot')
            self.verify_deal_ledger(data)
            old=self.db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
            if old and old[0]==payload:
                self.db.execute('ROLLBACK'); return
            self.db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?)',(payload,))
            cycle=data.get('cycle') or {}
            summary=json.dumps({'phase':data['phase'],'cycle_id':cycle.get('id'),
                                'recovery':cycle.get('recovery'),'currency':data.get('currency'),
                                'commands':len(data['commands'])})
            self.db.execute('INSERT INTO audit(event,payload) VALUES(?,?)',(event,summary))
            if self.fault: self.fault(event)
            self.db.execute('COMMIT')
        except BaseException as exc:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            if isinstance(exc,DurabilityError): raise
            raise DurabilityError('SQLite commit failed; stop owner and reload') from exc

    def close(self):
        self.db.close()

    def enable_deal_ledger(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS consumed_deals ('
                        'backend TEXT, account TEXT, ticket TEXT, payload TEXT NOT NULL, '
                        'PRIMARY KEY(backend,account,ticket))')

    def verify_deal_ledger(self, data):
        if data.get('ledger_contract') != 'deal_ledger_v1':
            return
        records=self.db.execute('SELECT ticket,payload FROM consumed_deals WHERE backend=? AND account=?',
                                (data['backend'],data.get('account'))).fetchall()
        tickets=[ticket for ticket,_ in records]
        if len(data['seen_deals'])!=len(set(data['seen_deals'])) or set(tickets)!=set(data['seen_deals']):
            raise DurabilityError('Consumed deal ledger and controller snapshot disagree')
        for ticket,payload in records:
            row=json.loads(payload)
            if (row['currency']!=data['currency'] or row['ticket']!=ticket
                    or row['backend']!=data['backend'] or row['account']!=data['account']
                    or str(row['raw']['ticket'])!=ticket
                    or str(row['raw']['position_id'])!=row['position_id']):
                raise DurabilityError('Consumed deal ledger identity mismatch')

    def commit_deals(self, expected, data, records, event, *, manual_reset=False):
        """CAS snapshot + immutable consumed deals + audit, in one FULL/WAL transaction."""
        encode=lambda value: json.dumps(value,sort_keys=True,allow_nan=False)
        try:
            self.db.execute('BEGIN IMMEDIATE')
            current=self.load()
            encoded=[(r,encode(r)) for r in records]
            if (not records and not manual_reset) or len({r['ticket'] for r in records})!=len(records):
                raise DurabilityError('Invalid closure deal batch')
            before=set(expected['seen_deals']); batch={r['ticket'] for r in records}
            if any(data[k]!=expected[k] for k in ('backend','account','currency','ledger_contract')):
                raise DurabilityError('Closure cannot change ledger identity')
            if before & batch or set(data['seen_deals'])!=before | batch:
                raise DurabilityError('Closure must append exactly its new consumed deal batch')
            if manual_reset and (expected['phase']!='MANUAL' or not expected['cycle'].get('manual')
                    or data['phase']!='WAIT_START' or data['cycle'] is not None
                    or data.get('last_manual_reset')!=expected['cycle']['id']):
                raise DurabilityError('Invalid manual reset transition')
            positions=set(expected['cycle'].get('reset_position_ids',[])) if manual_reset else {expected['cycle']['position']['id']}
            for row,payload in encoded:
                if (row['backend']!=data['backend'] or row['account']!=data['account']
                        or row['currency']!=data['currency'] or row['ticket'] not in data['seen_deals']
                        or row['cycle_id']!=expected['cycle']['id']
                        or row['position_id'] not in positions
                        or str(row['raw']['ticket'])!=row['ticket']
                        or str(row['raw']['position_id'])!=row['position_id']
                        or row['raw']['entry']!=1):
                    raise DurabilityError('Closure deal identity mismatch')
                previous=self.db.execute('SELECT payload FROM consumed_deals WHERE backend=? AND account=? AND ticket=?',
                                         (row['backend'],row['account'],row['ticket'])).fetchone()
                if previous and previous[0]!=payload:
                    raise DurabilityError('Conflicting reuse of consumed deal ticket')
            if current==data:
                self.verify_deal_ledger(data)
                self.db.execute('ROLLBACK')
                return False  # exact replay of an already committed transition
            if current!=expected:
                raise DurabilityError('Concurrent/stale closure snapshot')
            self.verify_deal_ledger(expected)
            for row,payload in encoded:
                self.db.execute('INSERT INTO consumed_deals VALUES(?,?,?,?)',
                                (row['backend'],row['account'],row['ticket'],payload))
            if self.fault: self.fault('manual_reset_deals_inserted' if manual_reset else 'deals_inserted')
            self.db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?)',(encode(data),))
            if self.fault: self.fault('manual_reset_snapshot_written' if manual_reset else 'closure_snapshot_written')
            self.verify_deal_ledger(data)
            detail=dict(phase=data['phase'],deal_count=len(records))
            if manual_reset: detail['settlement']=data['completed'][-1]
            self.db.execute('INSERT INTO audit(event,payload) VALUES(?,?)',(event,encode(detail)))
            if self.fault: self.fault('manual_reset_before_commit' if manual_reset else 'closure_before_commit')
            self.db.execute('COMMIT')
            if self.fault: self.fault('manual_reset_after_commit' if manual_reset else 'closure_after_commit')
            return True
        except BaseException as exc:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            if isinstance(exc,DurabilityError): raise
            raise DurabilityError('Atomic closure commit failed; reload durable state') from exc
