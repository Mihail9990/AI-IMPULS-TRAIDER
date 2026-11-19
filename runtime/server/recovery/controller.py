"""Mock-only confirmed-deal controller with durable partial-fill aggregation.

Not a broker listener. No reads/sends. Explicit verified mappings and simulated
broker deals are supplied by tests. Full events reach Engine only after exact
volume reconciliation; partial/mixed/ambiguous events never invent strategy state.
"""
import json
import sqlite3
import threading
from copy import deepcopy
from .engine import Engine, number, sign


class Controller:
    def __init__(self, engine, database=':memory:', *, test_mode=False):
        if test_mode is not True or engine.data.get('live_trading') is not False:
            raise RuntimeError('Controller is mock-only')
        self.database=database
        self.lock=threading.RLock()
        self.db=sqlite3.connect(database,check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS recovery_controller(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
        row=self.db.execute('SELECT data FROM recovery_controller WHERE id=1').fetchone()
        self.data=json.loads(row[0]) if row else {'engine':deepcopy(engine.data),'bindings':{},'deals':{},'blocked':{}}
        if row is None:
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO recovery_controller VALUES(1,?)',(json.dumps(self.data),))
            self.reload()

    def reload(self):
        """Refresh authoritative snapshot only after service ownership acquisition."""
        with self.lock:
            row=self.db.execute("SELECT data FROM recovery_controller WHERE id=1").fetchone()
            if row is None:raise RuntimeError("Controller snapshot missing")
            data=json.loads(row[0])
            if data["engine"].get("live_trading") is not False:raise RuntimeError("Non-mock snapshot")
            self.data=data

    def _save(self):
        with self.db:self.db.execute('INSERT OR REPLACE INTO recovery_controller VALUES(1,?)',(json.dumps(self.data),))

    def engine(self):
        return Engine(self.data['engine'])

    def state(self):
        with self.lock:
            bindings=deepcopy(self.data['bindings'])
            for binding in bindings.values():
                binding['observed_open_volume']=str(number(binding['entry_volume'])-number(binding['exit_volume']))
            return {'mode':'simulation','engine':self.engine().state(),
                    'bindings':bindings,'blocked':deepcopy(self.data['blocked'])}

    def bind_verified_order(self, logical_order_id, broker_ticket, *, account, server, symbol, magic):
        """Supply only a ticket confirmed by Executor evidence, never a guessed comment.

        This is test/controller interface, not a network endpoint. It deliberately
        requires broker scope and maps to an existing logical order.
        """
        if not all((account,server,symbol,magic)) or not isinstance(broker_ticket,int) or broker_ticket<=0:
            raise ValueError('Verified account/order scope required')
        with self.lock:
            o=next((o for o in self.data['engine']['orders'] if o['id']==logical_order_id),None)
            if not o:raise ValueError('Unknown logical order')
            binding={'logical_order_id':logical_order_id,'range_id':o['range_id'],
                     'account':account,'server':server,'symbol':symbol,'magic':magic,
                     'side':o['side'],'expected_volume':o['volume'],'position_identifier':None,
                     'entry_volume':'0','entry_notional':'0','exit_volume':'0','realized_net':'0',
                     'close_reasons':[],'engine_position_id':None,'phase':'pending'}
            key=str(broker_ticket)
            old=self.data['bindings'].get(key)
            # This prototype has no multi-ticket logical-order model. Refuse a
            # second broker order rather than summing distinct positions into one
            # fake weighted entry and losing real exposure from the ledger.
            if any(k != key and b['logical_order_id'] == logical_order_id
                   for k, b in self.data['bindings'].items()):
                raise ValueError('Logical order already bound to another broker ticket; reconcile exposure')
            if old:
                for field in ('logical_order_id','account','server','symbol','magic'):
                    if old[field]!=binding[field]:raise ValueError('Ticket mapping collision')
                return
            self.data['bindings'][key]=binding
            try:
                self._save()
            except Exception:
                del self.data['bindings'][key]
                raise

    def deal(self, event):
        """Consume a confirmed mock deal with original entry-order binding.

        Keys: deal_id (unique), entry_order_ticket, account/server/symbol/magic,
        position_identifier, entry ('IN' or 'OUT'), side (position direction),
        volume, price, realized_net (includes all charges for this deal),
        close_reason ('manual'/'initial_sl'/'support_sl' for OUT).
        Exit-deal direction must be normalized to position side by future listener.
        """
        self.engine().validate_distance_policy()
        required=('deal_id','entry_order_ticket','account','server','symbol','magic',
                  'position_identifier','entry','side','volume','price','realized_net')
        if any(k not in event for k in required):raise ValueError('Incomplete deal')
        key=str(event['deal_id'])
        if not isinstance(event['deal_id'],int) or event['deal_id']<=0:
            raise ValueError('Positive deal ticket required')
        volume,price,pnl=number(event['volume']),number(event['price']),number(event['realized_net'])
        if volume<=0 or price<=0 or event['entry'] not in ('IN','OUT'):
            raise ValueError('Invalid deal values')
        sign(event['side'])
        if not isinstance(event['position_identifier'],int) or event['position_identifier']<=0:
            raise ValueError('Position identifier required')
        if event['entry']=='OUT' and event.get('close_reason') not in ('manual','initial_sl','support_sl'):
            raise ValueError('Unknown close reason; reconciliation required')
        with self.lock:
            previous=self.data['deals'].get(key)
            if previous:
                if previous!=event:raise ValueError('Deal ticket reused with different event')
                return self.state()
            b=self.data['bindings'].get(str(event['entry_order_ticket']))
            if not b:raise ValueError('Unmapped deal: no strategy transition')
            for field in ('account','server','symbol','magic','side'):
                if event[field]!=b[field]:raise ValueError('Deal scope/ownership mismatch')
            if b['position_identifier'] not in (None,event['position_identifier']):
                raise ValueError('Position identifier changed')
            if any(other is not b and other['position_identifier'] == event['position_identifier']
                   for other in self.data['bindings'].values()):
                raise ValueError('Position identifier already belongs to another binding; reconcile exposure')
            # Persist evidence even if it cannot safely advance strategy. Keep all
            # processing in one transaction and rollback in-memory on IO failure.
            old=deepcopy(self.data)
            try:
                self.data['deals'][key]=deepcopy(event)
                b['position_identifier']=event['position_identifier']
                b['realized_net']=str(number(b['realized_net'])+pnl)
                if event['entry']=='IN':
                    b['entry_volume']=str(number(b['entry_volume'])+volume)
                    b['entry_notional']=str(number(b['entry_notional'])+volume*price)
                else:
                    b['exit_volume']=str(number(b['exit_volume'])+volume)
                    if event['close_reason'] not in b['close_reasons']:b['close_reasons'].append(event['close_reason'])
                self._advance(b)
                self._save()
            except Exception:
                self.data=old
                raise
            return self.state()

    def _advance(self,b):
        rid=b['range_id'];expected=number(b['expected_volume'])
        entered,exited=number(b['entry_volume']),number(b['exit_volume'])
        if entered>expected or exited>entered:
            self.data['blocked'][rid]='Volume mismatch/overfill; reconcile observed exposure'
            b['phase']='reconciliation_required';return
        if rid in self.data['blocked']:
            b['phase']='reconciliation_required';return
        if exited and entered<expected:
            self.data['blocked'][rid]='Exit before full entry; partial entry lifecycle needs reconciliation'
            b['phase']='reconciliation_required';return
        if entered<expected:
            b['phase']='partial_entry';return
        candidate=self.engine()
        try:
            position_id=b['engine_position_id']
            if position_id is None:
                weighted=number(b['entry_notional'])/entered
                candidate.fill('aggregate-fill:'+b['logical_order_id'],b['logical_order_id'],str(weighted))
                p=next(p for p in candidate.data['positions'] if p['range_id']==rid)
                position_id=p['id']
            if exited<entered:
                b['phase']='partial_exit' if exited else 'open'
            else:
                if len(b['close_reasons'])!=1:
                    self.data['blocked'][rid]='Mixed close reasons; do not guess full-SL counter'
                    b['phase']='reconciliation_required';return
                candidate.close('aggregate-close:'+b['logical_order_id'],position_id,
                                b['close_reasons'][0],b['realized_net'])
                b['phase']='closed'
            b['engine_position_id']=position_id
            self.data['engine']=candidate.data
        except ValueError as exc:
            self.data['blocked'][rid]=str(exc)
            b['phase']='reconciliation_required'

    def quote(self,bid,ask):
        """Simulated protection transition only, not a real SL acknowledgment."""
        with self.lock:
            candidate=self.engine();candidate.quote(bid,ask)
            previous=self.data['engine']
            try:
                self.data['engine']=candidate.data
                self._save()
            except Exception:
                self.data['engine']=previous
                raise
