"""Durable broker-intent executor with injected transport; no live transport supplied.

Tested only with a deterministic fake. Does not initialize MT5 or expose HTTP routes.
SENT/UNKNOWN intents are NEVER automatically resubmitted, even after restart.
"""
import json
import sqlite3
import threading
from decimal import Decimal


class Executor:
    def __init__(self, transport, database=':memory:'):
        self.transport = transport
        self.lock = threading.RLock()
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute('''CREATE TABLE IF NOT EXISTS recovery_outbox (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
            receipt TEXT, evidence TEXT, error TEXT)''')
        self.db.commit()

    def read(self, key):
        row = self.db.execute('SELECT payload,state,receipt,evidence,error FROM recovery_outbox WHERE id=?',(key,)).fetchone()
        if not row:
            raise ValueError('Unknown intent')
        return dict(id=key, payload=json.loads(row[0]), state=row[1],
                    receipt=json.loads(row[2]) if row[2] else None,
                    evidence=json.loads(row[3]) if row[3] else None, error=row[4])

    def prepare(self, key, request, expected):
        """expected binds intent to account, server, symbol and owned broker IDs.

        No submission here. Caller must supply fresh, validated planner request.
        """
        if not key or not expected.get('account') or not expected.get('server'):
            raise ValueError('Intent ID and account binding required')
        action=request.get('action')
        if action not in ('TRADE_ACTION_PENDING','TRADE_ACTION_SLTP','TRADE_ACTION_REMOVE','TRADE_ACTION_DEAL'):
            raise ValueError('Unsupported action; market close requires separate implementation')
        for field in ('symbol','magic'):
            if field in request and (not expected.get(field) or request[field]!=expected[field]):
                raise ValueError('Request scope differs from expected '+field)
        if action == 'TRADE_ACTION_DEAL':
            if any(not request.get(k) for k in ('symbol','magic')):
                raise ValueError('Close request requires symbol and magic')
        if action == 'TRADE_ACTION_PENDING':
            for field in ('symbol','magic'):
                if not expected.get(field) or request.get(field)!=expected[field]:
                    raise ValueError('Pending request must match explicit expected '+field)

        if action != 'TRADE_ACTION_PENDING':
            for field in ('magic','symbol'):
                if not expected.get(field): raise ValueError('Mapped ownership required')
        if action in ('TRADE_ACTION_SLTP','TRADE_ACTION_DEAL') and not expected.get('identifier'):
            raise ValueError('Position identifier required')
        payload=json.dumps({'request':request,'expected':expected},sort_keys=True)
        with self.lock:
            old=self.db.execute('SELECT payload FROM recovery_outbox WHERE id=?',(key,)).fetchone()
            if old:
                if old[0] != payload: raise ValueError('Intent ID reused with different payload')
            else:
                with self.db:self.db.execute('INSERT INTO recovery_outbox(id,payload,state) VALUES(?,?,?)',(key,payload,'PREPARED'))
            return self.read(key)

    def _update(self,key,state,receipt=None,evidence=None,error=None):
        with self.db:
            self.db.execute('UPDATE recovery_outbox SET state=?,receipt=?,evidence=?,error=? WHERE id=?',
                (state,json.dumps(receipt) if receipt is not None else None,
                 json.dumps(evidence) if evidence is not None else None,error,key))
        return self.read(key)

    def submit_once(self,key):
        with self.lock:
            intent=self.read(key)
            if intent['state'] != 'PREPARED': return intent
            request=intent['payload']['request']; expected=intent['payload']['expected']
            # Transport checks fresh account identity, ownership and executable
            # constraints. Preflight failure cannot be construed as a broker send.
            try:
                self.transport.preflight(request, expected)
            except Exception as exc:
                # Another process may already have claimed this intent. Never
                # overwrite its SENT state with our stale preflight rejection.
                with self.db:
                    self.db.execute('UPDATE recovery_outbox SET state=?,error=? WHERE id=? AND state=?',
                                    ('REJECTED','Preflight: '+str(exc),key,'PREPARED'))
                return self.read(key)
            # Atomic database claim, not just the in-process lock. Exactly one
            # process can change PREPARED to SENT and therefore invoke transport.
            # Crash after this commit remains SENT/UNKNOWN and cannot resubmit.
            with self.db:
                claimed=self.db.execute('UPDATE recovery_outbox SET state=?,error=NULL WHERE id=? AND state=?',
                                        ('SENT',key,'PREPARED')).rowcount
            if claimed != 1:
                return self.read(key)
            try:
                receipt=self.transport.send(request)
            except Exception as exc:
                return self._update(key,'UNKNOWN',error='Submission uncertain: '+str(exc))
            if not isinstance(receipt,dict) or receipt.get('status') not in ('accepted','rejected'):
                return self._update(key,'UNKNOWN',receipt=receipt if isinstance(receipt,dict) else None,
                                    error='Malformed or uncertain broker response')
            if receipt['status']=='rejected':
                return self._update(key,'REJECTED',receipt=receipt)
            return self._update(key,'ACKNOWLEDGED',receipt=receipt)

    @staticmethod
    def _equal(a,b):
        try: return Decimal(str(a)) == Decimal(str(b))
        except Exception: return False

    def reconcile(self,key, *, refresh=False):
        """Confirm from broker snapshot + order history. Absence alone is insufficient.

        Pending creation with lost receipt has no ticket: remains UNKNOWN rather
        than guessing by magic/comment and accidentally binding another order.
        """
        with self.lock:
            i=self.read(key)
            if i['state'] in ('PREPARED','REJECTED') or (i['state']=='VERIFIED' and not refresh): return i
            r=i['payload']['request']; x=i['payload']['expected']; receipt=i['receipt']
            try: s=self.transport.snapshot(x)
            except Exception as exc:
                return self._update(key,'UNKNOWN',receipt,error='Snapshot unavailable: '+str(exc))
            if s.get('account')!=x['account'] or s.get('server')!=x['server']:
                return self._update(key,'UNKNOWN',receipt,error='Account binding mismatch')
            action=r['action']; evidence=None
            if action=='TRADE_ACTION_PENDING':
                ticket=(receipt or {}).get('order')
                if ticket:
                    o=next((o for o in s.get('orders',[]) if o.get('ticket')==ticket),None)
                    if o and o.get('symbol')==r['symbol'] and o.get('magic')==r['magic'] and o.get('type')==r['type'] and all(self._equal(o.get(f),r[f]) for f in ('price','sl','tp','volume')):
                        evidence={'kind':'pending_verified','order':ticket,'snapshot':o}
                    # A fill before reread is not failure. Verify authoritative history,
                    # but do not infer position SL from the original pending request.
                    h=next((h for h in s.get('history_orders',[]) if h.get('ticket')==ticket),None)
                    if not o and h and h.get('symbol')==r['symbol'] and h.get('magic')==r['magic'] and h.get('state')=='filled' and h.get('type')==r['type'] and self._equal(h.get('volume_initial'),r['volume']):
                        evidence={'kind':'order_filled_before_verification','order':ticket,'history':h}
            elif action=='TRADE_ACTION_SLTP':
                p=next((p for p in s.get('positions',[]) if p.get('ticket')==r['position']),None)
                if p and p.get('identifier')==x['identifier'] and p.get('magic')==x['magic'] and p.get('symbol')==x['symbol'] and self._equal(p.get('sl'),r['sl']) and self._equal(p.get('tp'),r['tp']):
                    evidence={'kind':'protection_verified','position':r['position'],'snapshot':p}
            elif action=='TRADE_ACTION_DEAL':
                order=(receipt or {}).get('order')
                still_open=any(p.get('identifier')==x['identifier'] for p in s.get('positions',[]))
                exits=[d for d in s.get('close_deals',[]) if d.get('order')==order and d.get('position_identifier')==x['identifier'] and d.get('symbol')==x['symbol'] and d.get('entry')=='OUT']
                volume=sum((Decimal(str(d['volume'])) for d in exits),Decimal(0))
                if order and not still_open and self._equal(volume,r['volume']):
                    evidence={'kind':'close_verified','position_identifier':x['identifier'],'deals':exits}
            else:
                ticket=r['order']
                present=any(o.get('ticket')==ticket for o in s.get('orders',[]))
                h=next((h for h in s.get('history_orders',[]) if h.get('ticket')==ticket),None)
                if not present and h and h.get('state')=='cancelled' and h.get('magic')==x['magic'] and h.get('symbol')==x['symbol']:
                    evidence={'kind':'cancel_verified','order':ticket,'history':h}
            if evidence:return self._update(key,'VERIFIED',receipt,evidence)
            return self._update(key,'UNKNOWN',receipt,error='No conclusive matching broker evidence; do not resubmit')
