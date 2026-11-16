"""Narrow read-only classification of an in-flight broker protective close.

A relation is not ownership. These orders must never be cancelled/adopted as
entry commands. Completion and money still require the normal deal ledger.
"""
from .settings import number


def protective_close(order, audit, rows, api, symbol, magic):
    try:
        pid=str(order['position_id']); key=str(order['ticket'])
        if pid in {'0',''} or order.get('position_by_id',0):return None
        reports=[(cid,p) for cid,p in audit['obligations'].items()
                 if str(p.get('position_id'))==pid and p.get('status') in
                 {'FILLED_OPEN_EVIDENCE','PARTIAL_CLOSE','CLOSED_EVIDENCE'}]
        if len(reports)!=1:return None
        cid,proof=reports[0];cmd=rows[cid]['command']
        linked=[r for r in audit['current_orders'].values() if str(r.get('position_id'))==pid and str(r['ticket'])!=str(proof.get('order_id'))]
        if len(linked)!=1 or str(linked[0]['ticket'])!=key:return None
        if cmd['kind'] not in {'STOP','MARKET'} or key==str(proof.get('order_id')):return None
        opposite=api.ORDER_TYPE_SELL if cmd['side']=='BUY' else api.ORDER_TYPE_BUY
        reasons={getattr(api,'ORDER_REASON_SL',None),getattr(api,'ORDER_REASON_TP',None)}-{None}
        states={getattr(api,n,None) for n in ('ORDER_STATE_STARTED','ORDER_STATE_PLACED','ORDER_STATE_PARTIAL','ORDER_STATE_REQUEST_ADD','ORDER_STATE_REQUEST_MODIFY')}-{None}
        if (order['symbol']!=symbol or order['magic']!=magic or order['type']!=opposite
                or order.get('reason') not in reasons or order['state'] not in states):return None
        initial=number(order['volume_initial']);remaining=number(order['volume_current'])
        if not 0<remaining<=initial:return None
        facts=audit['raw_facts']['deal']
        entries=[d for d in facts.values() if str(d['order'])==str(proof['order_id']) and d['entry']==api.DEAL_ENTRY_IN]
        if not entries or number(order['time_setup_msc'])<max(number(d['time_msc']) for d in entries):return None
        exits=[d for d in facts.values() if str(d['order'])==key]
        if any(str(d['position_id'])!=pid or d['entry']!=api.DEAL_ENTRY_OUT
               or d['type']!=opposite or d['symbol']!=symbol or d['magic']!=magic
               or number(d['volume'])<=0 for d in exits):return None
        executed=sum((number(d['volume']) for d in exits),number(0))
        available=number(proof['filled_volume'])-number(proof['closed_volume'])
        if initial>available+executed or remaining>available or initial-remaining!=executed:return None
        return {'position_id':pid,'entry_command_id':cid,'status':'PROTECTIVE_CLOSE_PENDING',
                'reason':'SL' if order['reason']==getattr(api,'ORDER_REASON_SL',None) else 'TP'}
    except (KeyError,TypeError,ValueError,ArithmeticError,AttributeError):
        return None


def pending_closes(snap):
    if snap.get('reconciliation_contract')!='per_obligation_v1':return []
    return [o for o in snap.get('orders',[]) if not o.get('owned') and
            isinstance(o.get('related_close'),dict) and
            o['related_close'].get('status')=='PROTECTIVE_CLOSE_PENDING']


def foreign_obligations(snap):
    pending={o['id'] for o in pending_closes(snap)}
    return [p for p in snap['positions'] if not p.get('owned')]+[
        o for o in snap['orders'] if not o.get('owned') and o['id'] not in pending]


def independent_action(snap, command, *, state=None):
    """Current evidence authorizes only a disjoint, exact existing resource.

    Not an entry permission: basket P&L-dependent sizing/cleanup remains pending.
    Call against a newly validated snapshot, including inside the final send guard.
    """
    sources=pending_sources(snap,state)
    if not (pending_closes(snap) or sources) or foreign_obligations(snap):return False
    blocked={o['related_close']['position_id'] for o in pending_closes(snap)}|set(sources)
    kind=command.get('kind')
    if kind=='CANCEL':
        orders=[o for o in snap['orders'] if o.get('owned') and str(o['id'])==str(command.get('order_id'))]
        if len(orders)!=1:return False
        proof=snap.get('obligations',{}).get(orders[0].get('command_id'),{})
        return proof.get('status')=='PENDING_OBSERVED' and str(proof.get('order_id'))==str(orders[0]['id'])
    if kind not in {'MODIFY','CLOSE'}:return False
    pid=str(command.get('position_id'))
    if pid in blocked:return False
    positions=[p for p in snap['positions'] if p.get('owned') and str(p['id'])==pid]
    proofs=[p for p in snap.get('obligations',{}).values() if str(p.get('position_id'))==pid
            and p.get('status')=='FILLED_OPEN_EVIDENCE']
    if len(positions)!=1 or len(proofs)!=1:return False
    if kind=='CLOSE':
        # Automatic cleanup uses the same MANUAL purpose label: only an explicit
        # independent owner intent may bypass basket-accounting dependencies.
        return command.get('independent_owner_request') is True and command.get('purpose')=='MANUAL'
    if command.get('purpose')=='RECOVERY_TARGET':
        try:return recovery_target_valid(snap,command,state) and number(command['sl'])==number(positions[0]['sl'])
        except (KeyError,TypeError,ValueError,ArithmeticError):return False
    try:
        p=positions[0];sl=number(command['sl']);old=number(p['sl'])
        if number(command['tp'])!=number(p['tp']):return False
        return sl>0 and (old<=0 or (sl>=old if p['side']=='BUY' else sl<=old))
    except (KeyError,TypeError,ValueError,ArithmeticError):return False


def resource_conflict(left, right):
    if left.get('kind')=='CANCEL' and right.get('kind')=='CANCEL':
        return str(left.get('order_id'))==str(right.get('order_id'))
    if left.get('kind') in {'MODIFY','CLOSE'} and right.get('kind') in {'MODIFY','CLOSE'}:
        return str(left.get('position_id'))==str(right.get('position_id'))
    return False


def financial_basis(snap,state):
    """Exact readback basis for a newly computed debt-dependent TP intent.

    Quote ticks are not part of this basis; normal broker stop/freeze checks still
    run. All position protection/size facts and pending source proof changes are.
    """
    from copy import deepcopy
    c=state.get('cycle')
    if not c:return None
    estimates=deepcopy(c.get('provisional_recovery',{}))
    sources=[]
    for pid,row in sorted(estimates.items()):
        proof=snap.get('obligations',{}).get(row.get('command_id'),{})
        sources.append({'id':str(pid),'status':proof.get('status'),
            'position_id':str(proof.get('position_id')),
            'filled_volume':proof.get('filled_volume'),'closed_volume':proof.get('closed_volume')})
    positions=[{k:str(p[k]) for k in ('id','side','size','entry','sl','tp')}
        for p in snap.get('positions',[]) if p.get('owned')]
    return {'cycle_id':c['id'],'revision':int(c.get('recovery_revision',0)),
        'realized':str(c['realized']),'waived':str(c.get('recovery_waived','0')),
        'estimates':estimates,'sources':sources,
        'positions':sorted(positions,key=lambda p:p['id'])}


def recovery_target_valid(snap,command,state):
    if command.get('kind')!='MODIFY' or command.get('purpose')!='RECOVERY_TARGET':return False
    if not state or not isinstance(command.get('recovery_target_basis'),dict):return False
    try:return command['recovery_target_basis']==financial_basis(snap,state)
    except (KeyError,TypeError,ValueError,ArithmeticError):return False


def resource_basis(snap,command):
    """Stable pre-dispatch target facts, retained even when the close disappears."""
    kind=command.get('kind')
    if kind=='CANCEL':
        rows=[o for o in snap['orders'] if o.get('owned') and str(o['id'])==str(command.get('order_id'))]
        fields=('id','command_id','status','side','price','size','sl','tp')
    elif kind in {'MODIFY','CLOSE'}:
        rows=[p for p in snap['positions'] if p.get('owned') and str(p['id'])==str(command.get('position_id'))]
        fields=('id','ticket','side','entry','size','sl','tp')
    else:return None
    if len(rows)!=1:return None
    return {k:str(rows[0].get(k)) for k in fields}


def pending_sources(snap,state):
    """Previously evidenced source awaiting exit history, never inferred closed."""
    c=(state or {}).get('cycle') or {};result={}
    if snap.get('reconciliation_contract')!='per_obligation_v1':return result
    for pid,row in c.get('provisional_recovery',{}).items():
        ticket=c.get('tickets',{}).get(pid,{})
        cid=row.get('command_id');cmd=(state or {}).get('commands',{}).get(cid,{})
        proof=snap.get('obligations',{}).get(cid,{})
        if (row.get('cycle_id')==c.get('id') and row.get('position_id')==pid
                and ticket.get('id')==pid and not ticket.get('closed')
                and ticket.get('command_id')==cid and cmd.get('cycle_id')==c.get('id')
                and cmd.get('kind') in {'STOP','MARKET'} and str(proof.get('position_id'))==pid
                and proof.get('status') in {'AWAIT_POSITION_EVIDENCE','PARTIAL_CLOSE'}):
            result[pid]=cid
    return result


def authorize_counter_group(state,group,source_group,source_positions):
    """Called only by the owner's counter-STOP action before first dispatch."""
    c=state['cycle']
    group['counter_authorization']={'kind':'OWNER_PLACED_COUNTER_STOP',
        'cycle_id':c['id'],'group_id':group['id'],'source_group':source_group,
        'source_position_ids':sorted(str(pid) for pid in source_positions),
        'entry_commands':list(group['commands'])}


def authorized_counter_group(state,group,source):
    c=(state or {}).get('cycle') or {};a=group.get('counter_authorization',{})
    if (a.get('kind') not in {'OWNER_PLACED_COUNTER_STOP','OWNER_MANUAL_COUNTER_ENTRY'} or a.get('cycle_id')!=c.get('id')
            or a.get('group_id')!=group.get('id') or a.get('source_group')!=source.get('group')
            or str(source.get('id')) not in a.get('source_position_ids',[])
            or group.get('linked_source')!=source.get('group')):return False
    ids=group.get('commands',[])
    if not ids or not set(ids)<=set(a.get('entry_commands',[])):return False
    commands=(state or {}).get('commands',{})
    return all(cid in c.get('entries',[]) and commands.get(cid,{}).get('kind') in ({'STOP'} if a.get('kind')=='OWNER_PLACED_COUNTER_STOP' else {'STOP','MARKET'})
        and commands[cid].get('cycle_id')==c.get('id')
        and commands[cid].get('side') in {'BUY','SELL'}
        and commands[cid]['side']!=source.get('side') for cid in ids)


def counter_source_valid(snap,basis,*,state=None,entry_command=None):
    """A manual counter request never follows a different or unproved source."""
    if not isinstance(basis,dict) or not isinstance(basis.get('positions'),list) or not basis['positions']:return False
    c=(state or {}).get('cycle') or {}
    if basis.get('cycle_id')!=c.get('id'):return False
    try:
        if any(number(p[k])<=0 for p in basis['positions'] for k in ('entry','size','sl')):return False
        if any(p.get('side') not in {'BUY','SELL'} for p in basis['positions']):return False
        expected={str(p['id']):p for p in basis['positions']}
    except (KeyError,TypeError,ValueError,ArithmeticError):return False
    if len(expected)!=len(basis['positions']):return False
    actual={str(p['id']):p for p in snap.get('positions',[])}
    allowed=set(expected)
    if entry_command:
        g=next((g for g in c.get('groups',[]) if entry_command.get('id') in g.get('commands',[])),None)
        if not g or not g.get('manual_counter') or g.get('counter_entry_source')!=basis:return False
        if not all(authorized_counter_group(state,g,c.get('tickets',{}).get(pid,{})) for pid in expected):return False
        allowed.update(t['id'] for t in c.get('tickets',{}).values() if t.get('group')==g['id'] and not t.get('closed'))
        # A preceding sibling may have filled before controller adoption.
        for cid in g['commands']:
            proof=snap.get('obligations',{}).get(cid,{})
            if proof.get('status')=='FILLED_OPEN_EVIDENCE':allowed.add(str(proof.get('position_id')))
    if set(actual)-allowed:return False
    for pid,p in expected.items():
        live=actual.get(pid);t=c.get('tickets',{}).get(pid,{})
        if not live or not live.get('owned') or t.get('closed') or t.get('group')!=basis.get('source_group'):return False
        if any(str(live.get(k))!=str(p.get(k)) for k in ('id','side','entry','size','sl')):return False
        if snap.get('reconciliation_contract')=='per_obligation_v1':
            proof=snap.get('obligations',{}).get(t.get('command_id'),{})
            if proof.get('status')!='FILLED_OPEN_EVIDENCE' or str(proof.get('position_id'))!=pid:return False
    return True
