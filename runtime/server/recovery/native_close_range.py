"""Explicit close-current-position + cancel THIS range, not manual-close observation.

Ordinary user closes elsewhere never call this component and leave triggers intact.
Pending orders of the chosen range are retired before its selected position closes.
"""
from copy import deepcopy
from .native_entry_observer import NativeEntryObserver
from .mt5_plan import cancel, close_position


class NativeCloseRange:
    def __init__(self, control):
        self.control=control;self.session=control.session;self.observer=NativeEntryObserver(control)

    def _mapped(self,data,rid):
        items=list(data['orders'])+list(data.get('reverse_commands',{}).values())
        items+=list(data.get('reentry_commands',{}).values())
        return [x for x in items if x['intent']['range_id']==rid]

    def close_and_cancel(self, range_id):
        c=self.control
        with self.session._lock:
            c._guard();c.strategy_config();data=c._read()
            group=data.get('range_close_commands',{}).get(range_id)
            if group is not None:
                return self.reconcile(range_id) # no replay or automatic fresh close
            if c._unresolved_entry_cancellations(data) or not self.session.ready:
                raise RuntimeError('Unresolved native checkpoint')
            positions={};pending=[]
            for item in self._mapped(data,range_id):
                if item['phase']=='staged':continue
                if item['phase']!='verified':raise RuntimeError('Range mapping unresolved')
                seen=self.observer.observe(range_id,order_key=item['key'])['evidence']
                if seen['position'] is not None:
                    positions[seen['position_identifier']]=(item['key'],seen['position'])
                if seen['pending_order'] is not None:pending.append(seen['pending_order'])
            if len(positions)!=1:
                raise ValueError('Exactly one confirmed current range position required')
            pid,(order_key,position)=next(iter(positions.items()))
            data=c._read();b=self.session.transport.binding
            scope={'account':b.account,'server':b.server,'symbol':b.symbol,'magic':c.magic}
            key='close-range:'+data['generation']+':'+range_id
            actions=[]
            for order in pending:
                actions.append({'key':key+':cancel:'+str(order['ticket']), 'phase':'planned',
                    'request':cancel(order,expected_ticket=order['ticket'],expected_magic=c.magic,expected_symbol=b.symbol),
                    'scope':scope})
            group={'phase':'closing','position_identifier':pid,'order_key':order_key,'actions':actions,'close_key':key+':position'}
            data.setdefault('range_close_commands',{})[range_id]=group
            data.setdefault('range_modes',{})[range_id]='closing'
            c._save(data)
            try:
                for action in group['actions']:
                    action['phase']='dispatching';c._save(data)
                    row=self.session.execute(action['key'],action['request'],action['scope'])
                    action['phase']='verified' if row['state']=='VERIFIED' else 'unresolved'
                    c._save(data)
                    if action['phase']!='verified':return self.reconcile(range_id)
                # Re-read after pending cancellations to include any same-position
                # late partial fill. Never close a different new position.
                seen=self.observer.observe(range_id,order_key=order_key)['evidence']
                position=seen['position']
                data=c._read();group=data['range_close_commands'][range_id]
                if position is None:
                    # Original position may have closed externally during cancel.
                    # Record no synthetic close command; verify range is empty.
                    return self.reconcile(range_id)
                if seen['position_identifier']!=pid:raise RuntimeError('Selected position changed')
                validator=self.session.transport.validator
                symbol,quote=validator._metadata({'action':'TRADE_ACTION_DEAL'})
                info=self.session.transport.api.symbol_info(b.symbol)
                api=self.session.transport.api
                filling=('ORDER_FILLING_FOK' if info.filling_mode & api.SYMBOL_FILLING_FOK
                         else 'ORDER_FILLING_IOC' if info.filling_mode & api.SYMBOL_FILLING_IOC else None)
                if filling is None:raise ValueError('No supported explicit close filling policy')
                request=close_position(position,symbol,quote,expected_ticket=position['ticket'],
                    expected_identifier=pid,expected_magic=c.magic,filling=filling)
                action={'key':group['close_key'],'phase':'dispatching','request':request,
                        'scope':dict(scope,identifier=pid)}
                group['actions'].append(action);c._save(data)
                row=self.session.execute(action['key'],request,action['scope'])
                action['phase']='verified' if row['state']=='VERIFIED' else 'unresolved';c._save(data)
                return self.reconcile(range_id)
            except BaseException:
                self.session.ready=False
                raise

    def reconcile(self,range_id):
        c=self.control
        with self.session._lock:
            c._guard();self.session.ready=False
            try:
                data=c._read();group=data.get('range_close_commands',{}).get(range_id)
                if group is None:raise ValueError('No range close checkpoint')
                self.session.reconcile();complete=True
                for action in group['actions']:
                    found=c.db.execute('SELECT id FROM recovery_outbox WHERE id=?',(action['key'],)).fetchone()
                    phase='unresolved'
                    if found:
                        row=self.session.executor.read(action['key'])
                        if row['payload']!={'request':action['request'],'expected':action['scope']}:
                            raise ValueError('Range close checkpoint differs from journal')
                        evidence=row.get('evidence') or {}
                        if row['state']=='VERIFIED':
                            if action['request']['action']=='TRADE_ACTION_REMOVE':
                                valid=evidence.get('kind')=='cancel_verified' and evidence.get('order')==action['request']['order']
                            else:
                                valid=evidence.get('kind')=='close_verified' and evidence.get('position_identifier')==group['position_identifier']
                            if valid:phase='verified'
                    action['phase']=phase;complete &= phase=='verified'
                c._save(data)
                # A late order fill can create another mapped exposure. Never mark
                # side disabled while any selected-range position/pending remains.
                for item in self._mapped(data,range_id):
                    if item['phase']=='staged':continue
                    observed=self.observer.observe(range_id,order_key=item['key'])['evidence']
                    if observed['position'] is not None or observed['pending_order'] is not None:complete=False
                data=c._read();group=data['range_close_commands'][range_id]
                group['phase']='verified' if complete else 'unresolved'
                data.setdefault('range_modes',{})[range_id]='cancelled' if complete else 'closing'
                c._save(data)
                if c._unresolved_entry_cancellations(data):self.session.ready=False
                return deepcopy(group)
            except BaseException:
                self.session.ready=False
                raise
