"""Recover only a missing future pending from a proved closed full-SL chain.

Never reproduce missed historical trades, use market entry, move the level, reset
counts, or retry an uncertain/terminal prior send. This is not atomic with broker
prices; late preflight failures remain reconciliation-required.
"""
import hashlib
import json
from copy import deepcopy
from .engine import number, other, sign
from .mt5_plan import pending
from .volume_schedule import volume_for_slot
from .native_entry_observer import NativeEntryObserver
from .native_close_ledger import NativeCloseLedger
from .native_initial_reverse import NativeInitialReverse


class NativeInterruptedRecovery:
    def __init__(self, control):
        self.control=control;self.session=control.session
        self.observer=NativeEntryObserver(control)
        self.ledger=NativeCloseLedger(control)
        self.reverse=NativeInitialReverse(control)

    @staticmethod
    def items(data):
        return list(data['orders'])+list(data.get('reverse_commands',{}).values())+list(data.get('reentry_commands',{}).values())

    def advance(self, range_id):
        c=self.control
        with self.session._lock:
            c._guard();data=c._read();config=c.strategy_config()
            mode=data.get('range_modes',{}).get(range_id,'active')
            if mode not in ('interrupted','waiting_exact_level'):
                return {'phase':'not_applicable'}
            if c._unresolved_entry_cancellations(data) or not self.session.ready:
                raise RuntimeError('Unresolved native checkpoint blocks interrupted recovery')
            # Recovery after a verified send/control commit interruption maps the
            # EXISTING key only; it does not calculate a different successor.
            for key, plan in data.get('interrupted_recovery',{}).items():
                if plan['range_id']!=range_id or plan['phase']!='dispatching': continue
                record=data.get('reverse_commands',{}).get(key)
                if record is None: raise RuntimeError('Recovery dispatch checkpoint lacks order mapping')
                result=self.reverse.reconcile(key)
                if result['phase']=='verified':
                    data=c._read();data['interrupted_recovery'][key]['phase']='verified'
                    data.setdefault('range_modes',{})[range_id]='active';c._save(data)
                return result
            items=self.items(data)
            observed={}
            # Whole owned inventory must be explainable, including other ranges.
            for item in items:
                if item['phase']=='staged':
                    if not item.get('not_dispatched'): raise RuntimeError('Unfinished staged entry blocks recovery')
                    continue
                if item['phase']=='rejected' and item['intent']['range_id']!=range_id:
                    row=self.session.executor.read(item['key'])
                    bound=self.session.transport.binding
                    expected=dict(account=bound.account,server=bound.server,symbol=bound.symbol,magic=c.magic)
                    if row['state']!='REJECTED' or row['payload']!={'request':item['request'],'expected':expected}:
                        raise RuntimeError('Unproven other-range rejection')
                    continue
                if item['phase']!='verified': raise RuntimeError('Nonverified entry blocks recovery')
                observed[item['key']]=self.observer.observe(item['intent']['range_id'],order_key=item['key'])
            b=self.session.transport.binding
            scope=dict(account=b.account,server=b.server,symbol=b.symbol,magic=c.magic)
            snapshot=self.session.transport.snapshot(scope)
            for field, ef in (('orders','pending_order'),('positions','position')):
                actual={x['ticket'] for x in snapshot[field] if x['symbol']==b.symbol and x['magic'] in b.magics}
                expected={o['evidence'][ef]['ticket'] for o in observed.values() if o['evidence'][ef] is not None}
                if actual!=expected: raise RuntimeError('Unmapped or changing owned exposure blocks recovery')
            own=[item for item in items if item['intent']['range_id']==range_id]
            if not own or any(item['phase']!='verified' for item in own):
                raise RuntimeError('Incomplete range mapping')
            filled={};closed={}
            for item in own:
                evidence=observed[item['key']]['evidence']
                if evidence['position'] is not None or evidence['pending_order'] is not None:
                    raise RuntimeError('Interrupted range is no longer flat')
                if not evidence['terminal_order']: raise RuntimeError('Nonterminal range order')
                if number(evidence['filled_volume'])>0:
                    row=self.ledger.record(item['broker_order'])
                    if not row['fully_closed'] or row['classification'] is None:
                        return {'phase':'waiting_evidence'}
                    filled[item['key']]=item;closed[item['key']]=row
            if not filled: return {'phase':'not_applicable'}
            roots={x['key'] for x in data['orders'] if x['intent']['range_id']==range_id}
            children={key:set() for key in filled}
            for key,item in filled.items():
                parent=item.get('origin_order_key')
                if key not in roots:
                    if parent not in filled: raise RuntimeError('Broken filled chain parent')
                    kind=item['intent'].get('kind')
                    suffix={'reverse':':initial-reverse','reentry':':support-reentry'}.get(kind)
                    expected_side=other(filled[parent]['intent']['side']) if kind=='reverse' else filled[parent]['intent']['side']
                    if (suffix is None or key!=parent+suffix or
                            item.get('origin_position_identifier')!=closed[parent]['position_identifier'] or
                            item['intent']['side']!=expected_side):
                        raise RuntimeError('Filled chain provenance differs from canonical successor')
                    children[parent].add(key)
                # Walk all ancestors: do not select by timestamps/event order.
                visited={key};cursor=item
                while cursor['key'] not in roots:
                    parent=cursor.get('origin_order_key')
                    if parent not in filled or parent in visited: raise RuntimeError('Ambiguous/cyclic recovery chain')
                    visited.add(parent);cursor=filled[parent]
            leaves=[key for key,value in children.items() if not value]
            if len(leaves)!=1: raise RuntimeError('Multiple terminal branches; no unique successor')
            source=filled[leaves[0]];close=closed[source['key']]
            if close['classification']!='initial_sl': return {'phase':'not_applicable'}
            totals=self.ledger.totals().get(range_id,{})
            count=totals.get('full_sl_count',0)
            if totals.get('classification_pending_count',0): return {'phase':'waiting_evidence'}
            if count>=24:
                data=c._read();data.setdefault('range_modes',{})[range_id]='manual';c._save(data)
                return {'phase':'limit_reached'}
            if count<1 or not close.get('sl_resolutions') or close.get('stop_level') is None:
                raise RuntimeError('Authoritative full-SL stop evidence required')
            key=source['key']+':initial-reverse'
            data=c._read()
            # Do not recreate manually cancelled/rejected successors or a key
            # whose dispatch outcome is uncertain. Existing graph nodes are final.
            if key in data.get('reverse_commands',{}) or c.db.execute('SELECT id FROM recovery_outbox WHERE id=?',(key,)).fetchone():
                return {'phase':'existing_successor_requires_review'}
            side=other(source['intent']['side'])
            price=number(close['stop_level']);distance=number(config['settings']['initial_sl_distance'])
            intent=dict(id=source['intent']['id']+'-reverse',range_id=range_id,side=side,
                        price=str(price),sl=str(price-sign(side)*distance),
                        volume=str(volume_for_slot(count+1)),kind='reverse')
            proof=dict(generation=data['generation'],source=source['key'],close=close,
                       full_sl_count=count,config=config,scope=scope,intent=intent)
            fingerprint=hashlib.sha256(json.dumps(proof,sort_keys=True).encode()).hexdigest()
            plans=data.setdefault('interrupted_recovery',{})
            plan=plans.get(key)
            if plan is not None and (plan['fingerprint']!=fingerprint or plan['phase']!='waiting_exact_level'):
                raise RuntimeError('Interrupted recovery proof changed; no replacement order')
            if plan is None:
                plan=dict(key=key,range_id=range_id,source_order_key=source['key'],
                          phase='waiting_exact_level',fingerprint=fingerprint,intent=intent)
                plans[key]=plan
            data.setdefault('range_modes',{})[range_id]='waiting_exact_level';c._save(data)
            validator=self.session.transport.validator
            symbol,quote=validator._metadata(dict(action='TRADE_ACTION_PENDING',
                type='ORDER_TYPE_BUY_STOP' if side=='BUY' else 'ORDER_TYPE_SELL_STOP'))
            validator._session()
            try: request=pending(intent,symbol,quote,c.magic)
            except ValueError as exc:
                if str(exc)=='Trigger crossed or inside minimum stop distance':
                    return deepcopy(plan)
                raise
            try: validator.validate(request,scope)
            except ValueError as exc:
                if str(exc)=='Trigger crossed or inside minimum stop distance': return deepcopy(plan)
                raise
            # Last owned flat proof immediately before the durable send boundary.
            fresh=self.observer.observe(range_id,order_key=source['key'])['evidence']
            if fresh['position'] is not None or fresh['pending_order'] is not None:
                raise RuntimeError('Source exposure changed before rearm')
            latest=self.session.transport.snapshot(scope)
            for field in ('orders','positions'):
                before={x['ticket'] for x in snapshot[field] if x['symbol']==b.symbol and x['magic'] in b.magics}
                after={x['ticket'] for x in latest[field] if x['symbol']==b.symbol and x['magic'] in b.magics}
                if before!=after: raise RuntimeError('Owned exposure changed before rearm')
            data=c._read();plan=data['interrupted_recovery'][key];plan['phase']='dispatching'
            data.setdefault('reverse_commands',{})[key]=dict(key=key,range_id=range_id,intent=intent,
                origin_position_identifier=close['position_identifier'],origin_order_key=source['key'],
                request=request,scope=scope,phase='dispatching',broker_order=None)
            c._save(data)
            try:
                self.session.execute(key,request,scope)
                result=self.reverse.reconcile(key)
                data=c._read()
                if result['phase']=='verified':
                    data['interrupted_recovery'][key]['phase']='verified'
                    data.setdefault('range_modes',{})[range_id]='active';c._save(data)
                return result
            except BaseException:
                self.session.ready=False
                raise
