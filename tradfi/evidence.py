"""Read-only evidence per durable obligation. Never authorizes execution or books PnL.

A range is discovery, not a completeness assertion. Returned totals replace prior
observations; consumers must never add them to the strategy ledger on every poll.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import re
import json
from pathlib import Path
import sqlite3

from .settings import number
from .time_profile import server_to_utc, TimeProfileError


class EvidenceConflict(ValueError):
    pass


def plain(value):
    if hasattr(value, '_asdict'):
        return {k: plain(v) for k, v in value._asdict().items()}
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def ident(value):
    if isinstance(value, bool):
        raise EvidenceConflict('INVALID_IDENTIFIER')
    text = str(value)
    if not text.isdecimal() or int(text) <= 0:
        raise EvidenceConflict('INVALID_IDENTIFIER')
    return str(int(text))


def history_time(raw_ms):
    """Preserve raw time; UTC is explicitly a hypothesis until independently verified."""
    raw = number(raw_ms)
    if raw <= 0 or raw % 1:
        raise EvidenceConflict('INVALID_HISTORY_TIME')
    try:
        candidate = server_to_utc(float(raw/1000))
    except TimeProfileError:
        candidate = None
    return dict(raw_time_msc=int(raw), utc_candidate_s=candidate,
                utc_status='UNVERIFIED' if candidate is not None else 'AMBIGUOUS_OR_INVALID')


def durable_rows(rows, state, account):
    if not rows and state is None:
        return {}
    if not state or state.get('backend') != 'mt5_direct_v1' or state.get('account') != account or state.get('currency') != 'UST':
        raise EvidenceConflict('DURABLE_ACCOUNT_OR_BACKEND_MISMATCH')
    result = deepcopy(rows)
    if state.get('scope_contract')=='cycle_scope_v1':
        cycle=state.get('cycle') or {}
        known={r['id'] for r in state.get('completed',[])} | {cycle.get('id')}
        for command in state.get('commands',{}).values():
            if not command.get('cycle_id') or command['cycle_id'] not in known:
                raise EvidenceConflict('MISSING_OR_UNKNOWN_CYCLE_BINDING')
        if any(state['commands'].get(cid,{}).get('cycle_id')!=cycle.get('id') for cid in cycle.get('entries',[])):
            raise EvidenceConflict('CROSS_CYCLE_ENTRY_BINDING')
    for cid, row in result.items():
        command = row['command']
        saved = state.get('commands', {}).get(cid)
        if (not re.fullmatch('[0-9a-f]{24}', cid) or command.get('id') != cid
                or row.get('account') != account or not saved
                or saved.get('status') not in {'UNKNOWN','ACCEPTED','FILLED','PENDING','DONE','REJECTED','CANCELLED','OBSOLETE'}
                or not isinstance(row.get('send_started'), bool)
                or any(saved.get(k) != v for k, v in command.items())):
            raise EvidenceConflict('DURABLE_INTENT_MISMATCH')
    for cid, command in state.get('commands', {}).items():
        if cid not in result and command.get('status') != 'OBSOLETE':
            if not re.fullmatch('[0-9a-f]{24}', cid) or command.get('id') != cid:
                raise EvidenceConflict('DURABLE_INTENT_MISMATCH')
            result[cid] = dict(command=deepcopy(command), account=account,
                               send_started=False, journal_missing=True)
    return result


def load_journal(path, expected_binding):
    """Open existing SQLite read-only; never create/migrate/relabel a journal."""
    try:
        source=Path(path).resolve()
        # SQLite mode=ro can still create/update WAL shared-memory sidecars.
        # Only an offline rollback-journal snapshot is supported by this audit loader.
        with source.open('rb') as handle:
            header=handle.read(100)
        if (header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01'
                or any(Path(str(source)+suffix).exists() for suffix in ('-wal','-shm','-journal'))):
            raise EvidenceConflict('OFFLINE_ROLLBACK_JOURNAL_SNAPSHOT_REQUIRED')
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            # One consistent read transaction for binding, controller, journal and facts.
            db.execute('BEGIN')
            binding=db.execute('SELECT payload FROM mt5_binding WHERE id=1').fetchone()
            saved=db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
            if not binding or json.loads(binding[0])!=expected_binding or not saved:
                raise EvidenceConflict('JOURNAL_BINDING_MISMATCH')
            state=json.loads(saved[0])
            if state.get('schema')!=1:
                raise EvidenceConflict('UNSUPPORTED_JOURNAL_SCHEMA')
            rows={cid:json.loads(payload) for cid,payload in db.execute('SELECT id,payload FROM mt5_commands')}
            facts={'order':{},'deal':{}}
            for kind,key,payload in db.execute('SELECT kind,ticket,payload FROM mt5_facts'):
                if kind not in facts:
                    raise EvidenceConflict('INVALID_JOURNAL_FACT_KIND')
                facts[kind][key]=json.loads(payload)
            durable_rows(rows,state,expected_binding['account'])
            return rows,state,facts
    except (sqlite3.Error, json.JSONDecodeError, OSError):
        raise EvidenceConflict('JOURNAL_UNAVAILABLE_OR_INVALID') from None


def _scope_facts(rows, facts):
    """Reachability from durable intents, before validating unrelated archive records."""
    entries={cid:r for cid,r in rows.items() if r['command']['kind'] in {'STOP','MARKET'} and r.get('send_started')}
    tickets={str((r.get('raw_result') or {}).get('order')) for r in entries.values()}-{'None','0'}
    orders=list(facts['order'].values()); deals=list(facts['deal'].values())
    owned=[r for r in orders if str(r.get('ticket')) in tickets or r.get('comment') in entries]
    tickets.update(str(r['ticket']) for r in owned)
    pids={str(r['position_id']) for r in owned if r.get('position_id')}
    pids.update(str(r['position_id']) for r in deals if str(r.get('order')) in tickets and r.get('entry')==0)
    return dict(order={k:r for k,r in facts['order'].items() if str(r.get('ticket')) in tickets or str(r.get('position_id')) in pids},
                deal={k:r for k,r in facts['deal'].items() if str(r.get('order')) in tickets or str(r.get('position_id')) in pids})


def collect(api, *, rows, state, account, symbol, magic, now, check_context,
            cached=None):
    """No writes/API mutations. Validate intents before I/O; return an immutable evidence batch."""
    rows = durable_rows(rows, state, account)
    scoped=bool(state and state.get('scope_contract')=='cycle_scope_v1')
    if scoped:
        active_cycle=(state.get('cycle') or {}).get('id')
        terminal={'DONE','REJECTED','CANCELLED','FILLED','OBSOLETE'}
        rows={cid:row for cid,row in rows.items() if row['command']['cycle_id']==active_cycle
              or state['commands'][cid]['status'] not in terminal}
    facts = deepcopy(cached or {'order': {}, 'deal': {}})
    if scoped:
        facts=_scope_facts(rows,facts)
    if set(facts) != {'order','deal'} or any(key!=ident(r['ticket']) for group in facts.values() for key,r in group.items()):
        raise EvidenceConflict('CACHED_FACT_IDENTITY_MISMATCH')
    mutable = {api.ORDER_STATE_STARTED, api.ORDER_STATE_PLACED, api.ORDER_STATE_PARTIAL,
               api.ORDER_STATE_REQUEST_ADD, api.ORDER_STATE_REQUEST_MODIFY, api.ORDER_STATE_REQUEST_CANCEL}
    sources = {}

    def merge(kind, records):
        for record in records:
            key = ident(record['ticket'])
            old = facts[kind].get(key)
            if old is not None and old != record:
                stable = ('ticket','symbol','magic','type','volume_initial','comment','time_setup','time_setup_msc')
                if (kind != 'order' or old['state'] not in mutable
                        or any(old.get(k) != record.get(k) for k in stable)):
                    raise EvidenceConflict('CONFLICTING_BROKER_FACT')
            facts[kind][key] = record

    def read(label, fn, *args, **kwargs):
        check_context()
        value = fn(*args, **kwargs)
        check_context()
        sources[label] = 'UNAVAILABLE' if value is None else 'RETURNED'
        return None if value is None else plain(value)

    # No rolling watermark can omit a delayed event or a winter +2h record.
    # Full lower bound is intentional until a separately proven incremental scheme exists.
    lower=0
    if scoped and rows and all(r['command']['cycle_id']==active_cycle for r in rows.values()):
        # Only discovery for this durable cycle; targeted IDs still recover delayed events.
        # Unknown obligations from an older cycle retain their own reconciliation scope.
        lower=max(0,float((state.get('cycle') or {}).get('started_at_utc',0))-60)
    start = datetime.fromtimestamp(lower, timezone.utc)
    end = datetime.fromtimestamp(now+10800+60, timezone.utc)
    active_orders = read('active_orders', api.orders_get, symbol=symbol)
    positions = read('positions', api.positions_get, symbol=symbol)
    need_history=not scoped or any(r.get('send_started') for r in rows.values())
    discovered_orders = read('order_discovery', api.history_orders_get, start, end) if need_history else []
    discovered_deals = read('deal_discovery', api.history_deals_get, start, end) if need_history else []
    if scoped:
        relevant=_scope_facts(rows,dict(order={**facts['order'], **{'discovery:'+str(i):r for i,r in enumerate(discovered_orders or [])}},
                                        deal={**facts['deal'], **{'discovery:'+str(i):r for i,r in enumerate(discovered_deals or [])}}))
        discovered_orders=list(relevant['order'].values())
        discovered_deals=list(relevant['deal'].values())
    for kind, records in (('order',discovered_orders),('deal',discovered_deals)):
        merge(kind, [r for r in records or [] if r['symbol']==symbol])
    # A pending current record is not allowed to overwrite a terminal historical fact.
    active = {ident(r['ticket']):r for r in active_orders or [] if r['symbol']==symbol}
    for key,r in active.items():
        if key in facts['order'] and facts['order'][key]['state'] not in mutable:
            raise EvidenceConflict('ACTIVE_TERMINAL_ORDER_CONFLICT')
    candidates = dict(facts['order']); candidates.update(active)
    entry_rows = {cid:r for cid,r in rows.items() if r['command']['kind'] in {'STOP','MARKET'}
                  and r.get('send_started') is True}
    ownership = {}
    known = {}
    for cid,row in entry_rows.items():
        cmd = row['command']
        size = number(cmd['size'])
        if size <= 0 or cmd.get('side') not in {'BUY','SELL'}:
            raise EvidenceConflict('INVALID_ENTRY_INTENT')
        ticket = (row.get('raw_result') or {}).get('order')
        known[cid] = ident(ticket) if ticket else None
        if known[cid]:
            records = read('order:'+known[cid], api.history_orders_get, ticket=int(known[cid]))
            if any(ident(r['ticket']) != known[cid] for r in records or []):
                raise EvidenceConflict('WRONG_ORDER_TICKET_RESPONSE')
            merge('order', records or [])
            candidates.update({ident(r['ticket']):r for r in records or []})
    # The exact 24-hex command marker can recover an unknown reply; magic alone cannot.
    for key,order in candidates.items():
        matches = []
        for cid,row in entry_rows.items():
            cmd = row['command']
            marker = order.get('comment') == cid
            linked = known[cid] == key
            if not (marker or linked):
                continue
            if cmd['kind']=='MARKET' and not linked:
                continue  # unknown MARKET lacks independent price/ticket correlation
            expected = (api.ORDER_TYPE_BUY_STOP if cmd['side']=='BUY' else api.ORDER_TYPE_SELL_STOP) if cmd['kind']=='STOP' else (api.ORDER_TYPE_BUY if cmd['side']=='BUY' else api.ORDER_TYPE_SELL)
            if (order['symbol']!=symbol or order['magic']!=magic or order['type']!=expected
                    or number(order['volume_initial'])!=number(cmd['size'])
                    or (cmd['kind']=='STOP' and number(order['price_open'])!=number(cmd['price']))
                    or (known[cid] is not None and known[cid]!=key)):
                raise EvidenceConflict('ORDER_INTENT_CORRELATION_CONFLICT')
            matches.append(cid)
        if len(matches)>1:
            raise EvidenceConflict('MULTIPLE_OWNERS_FOR_ORDER')
        if matches:
            cid=matches[0]
            if cid in ownership.values():
                raise EvidenceConflict('MULTIPLE_ORDERS_FOR_INTENT')
            ownership[key]=cid
    # Query correlated discovered orders as well, without using deals(ticket=...).
    for key in ownership:
        if 'order:'+key not in sources:
            records=read('order:'+key,api.history_orders_get,ticket=int(key))
            if any(ident(r['ticket'])!=key for r in records or []):
                raise EvidenceConflict('WRONG_ORDER_TICKET_RESPONSE')
            for r in records or []:
                previous=candidates[key]
                if any(r.get(k)!=previous.get(k) for k in ('symbol','magic','type','volume_initial','comment','price_open')):
                    raise EvidenceConflict('ORDER_READBACK_CHANGED_IDENTITY')
            merge('order',records or [])
    pids=set()
    for key in ownership:
        order=facts['order'].get(key) or candidates[key]
        if order.get('position_id'):
            pids.add(ident(order['position_id']))
        pids.update(ident(d['position_id']) for d in facts['deal'].values()
                    if str(d['order'])==key and d['entry']==api.DEAL_ENTRY_IN)
    for pid in sorted(pids):
        records=read('position:'+pid,api.history_deals_get,position=int(pid))
        if any(ident(r['position_id'])!=pid for r in records or []):
            raise EvidenceConflict('WRONG_POSITION_RESPONSE')
        merge('deal',records or [])
    check_context()
    return evaluate(api, rows=rows, facts=facts, ownership=ownership,
                    active=active, positions=positions or [], sources=sources,
                    symbol=symbol, magic=magic, manual=bool(state and (state.get('cycle') or {}).get('manual')))


def evaluate(api, *, rows, facts, ownership, active, positions, sources, symbol, magic, manual=False):
    reports={}; position_owner={}; closed_profit={}; times={}
    unavailable=[key for key,value in sources.items() if value=='UNAVAILABLE']
    for key,deal in facts['deal'].items():
        times[key]=history_time(deal['time_msc'])
    for cid,row in rows.items():
        cmd=row['command']
        if cmd['kind'] not in {'STOP','MARKET'}:
            continue
        report=dict(status='MISSING_DURABLE_JOURNAL' if row.get('journal_missing') else 'UNKNOWN', repeat_allowed=False)
        reports[cid]=report
        if row.get('journal_missing'):
            if cmd.get('status')=='READY' or cmd.get('never_sent') is True:
                report['status']='NO_SEND_EVIDENCE'
            continue
        if row.get('result',{}).get('status')=='REJECTED':
            if cid in ownership.values():
                raise EvidenceConflict('REJECTED_INTENT_HAS_BROKER_ORDER')
            report['status']='REJECTED_RESPONSE'; continue
        if row.get('send_started') is not True:
            report['status']='NO_SEND_EVIDENCE'; continue
        matches=[key for key,owner in ownership.items() if owner==cid]
        if not matches:
            continue
        key=matches[0]; order=facts['order'].get(key) or active[key]
        report['order_id']=key
        if (order['state']==api.ORDER_STATE_FILLED and number(order['volume_current'])!=0):
            raise EvidenceConflict('FILLED_ORDER_HAS_REMAINDER')
        entries=[d for d in facts['deal'].values() if str(d['order'])==key and d['entry']==api.DEAL_ENTRY_IN]
        for d in entries:
            side=api.DEAL_TYPE_BUY if cmd['side']=='BUY' else api.DEAL_TYPE_SELL
            if d['symbol']!=symbol or d['magic']!=magic or d['type']!=side or number(d['volume'])<=0:
                raise EvidenceConflict('ENTRY_DEAL_CONFLICT')
        pids={ident(d['position_id']) for d in entries}
        if len(pids)>1:
            raise EvidenceConflict('MULTIPLE_HEDGE_POSITIONS_FOR_INTENT')
        volume=sum((number(d['volume']) for d in entries),number(0))
        target=number(cmd['size'])
        if volume>target:
            raise EvidenceConflict('ENTRY_OVERFILL')
        report['filled_volume']=str(volume)
        if not entries:
            report['status']='PENDING_OBSERVED' if key in active else 'AWAIT_FILL_EVIDENCE'
            if order['state'] in {api.ORDER_STATE_CANCELED,api.ORDER_STATE_EXPIRED,api.ORDER_STATE_REJECTED} and number(order['volume_current'])==target:
                report['status']='TERMINAL_UNFILLED_EVIDENCE'
            continue
        pid=next(iter(pids)); report['position_id']=pid
        if pid in position_owner:
            raise EvidenceConflict('SHARED_POSITION_OWNERSHIP')
        position_owner[pid]=cid
        if order.get('position_id') and ident(order['position_id'])!=pid:
            raise EvidenceConflict('ORDER_POSITION_MISMATCH')
        related=[d for d in facts['deal'].values() if str(d['position_id'])==pid]
        if any(d['entry']==api.DEAL_ENTRY_IN and str(d['order'])!=key for d in related):
            raise EvidenceConflict('UNRELATED_ENTRY_ON_OWNED_POSITION')
        exits=[d for d in related if d['entry']==api.DEAL_ENTRY_OUT]
        if any(d['entry'] not in {api.DEAL_ENTRY_IN,api.DEAL_ENTRY_OUT} for d in related):
            report['status']='UNSUPPORTED_EXIT'; continue
        for d in exits:
            side=api.DEAL_TYPE_SELL if cmd['side']=='BUY' else api.DEAL_TYPE_BUY
            if d['symbol']!=symbol or d['type']!=side or number(d['volume'])<=0:
                raise EvidenceConflict('EXIT_DEAL_CONFLICT')
        out=sum((number(d['volume']) for d in exits),number(0))
        if out>volume:
            raise EvidenceConflict('EXIT_OVERFILL')
        report['closed_volume']=str(out)
        live=[p for p in positions if str(p['identifier'])==pid]
        if len(live)>1:
            raise EvidenceConflict('DUPLICATE_POSITION_IDENTIFIER')
        if live:
            p=live[0]
            expected=api.POSITION_TYPE_BUY if cmd['side']=='BUY' else api.POSITION_TYPE_SELL
            if p['symbol']!=symbol or p['magic']!=magic or p['type']!=expected:
                raise EvidenceConflict('LIVE_POSITION_OWNERSHIP_CONFLICT')
            if number(p['volume'])!=volume-out or volume-out<=0:
                report['status']='VOLUME_EVIDENCE_INCONSISTENT'; continue
            report['current_position_ticket']=ident(p['ticket'])
        if volume<target:
            report['status']='PARTIAL_FILL'; continue
        if any(d['reason'] not in {api.DEAL_REASON_SL,api.DEAL_REASON_TP} or d['magic']!=magic for d in exits):
            report['status']='EXTERNAL_EXIT'; continue
        if len({d['reason'] for d in exits})>1:
            report['status']='MIXED_EXIT_REASONS'; continue
        if (any(times[ident(d['ticket'])]['utc_candidate_s'] is None for d in related)
                or len({times[ident(d['ticket'])]['raw_time_msc']/1000-times[ident(d['ticket'])]['utc_candidate_s']
                        for d in related if times[ident(d['ticket'])]['utc_candidate_s'] is not None})>1):
            report['status']='AMBIGUOUS_HISTORY_TIME'; continue
        if unavailable:
            report['status']='API_EVIDENCE_UNAVAILABLE'; continue
        if order['state']!=api.ORDER_STATE_FILLED or key in active:
            report['status']='ORDER_NOT_FINAL'; continue
        if live and out==0:
            report['status']='FILLED_OPEN_EVIDENCE'
        elif not live and out==volume:
            report['status']='CLOSED_EVIDENCE'
            for d in exits:
                closed_profit[ident(d['ticket'])]=str(number(d['profit']))
        else:
            report['status']='PARTIAL_CLOSE' if out else 'AWAIT_POSITION_EVIDENCE'
    if manual and not unavailable:
        # Explicit manual settlement accepts user-initiated exits only through an already
        # owned opening order/position. It never changes automatic strategy classification.
        for cid,report in reports.items():
            cmd=rows[cid]['command']; key=report.get('order_id'); pid=report.get('position_id')
            if not key or not pid or key in active or any(str(p['identifier'])==pid for p in positions):
                continue
            order=facts['order'].get(key)
            if not order or order['state'] not in {api.ORDER_STATE_FILLED,api.ORDER_STATE_CANCELED,api.ORDER_STATE_EXPIRED}:
                continue
            related=[d for d in facts['deal'].values() if str(d['position_id'])==pid]
            ins=[d for d in related if d['entry']==api.DEAL_ENTRY_IN]
            outs=[d for d in related if d['entry']==api.DEAL_ENTRY_OUT]
            if not ins or not outs or len(ins)+len(outs)!=len(related): continue
            volume=sum((number(d['volume']) for d in ins),number(0))
            closed=sum((number(d['volume']) for d in outs),number(0))
            target=number(cmd['size'])
            if not 0<volume<=target or volume!=closed or number(order['volume_current'])!=target-volume: continue
            if order['state']==api.ORDER_STATE_FILLED and volume!=target: continue
            if any(str(d['order'])!=key for d in ins): continue
            if any(times[ident(d['ticket'])]['utc_candidate_s'] is None for d in related): continue
            report['reset_closed']=True
            report['reset_closing_deals']=sorted(ident(d['ticket']) for d in outs)
    for cid,row in rows.items():
        cmd=row['command']
        if cmd['kind'] in {'STOP','MARKET'}:
            continue
        report=dict(status='MISSING_DURABLE_JOURNAL' if row.get('journal_missing') else 'UNKNOWN',repeat_allowed=False); reports[cid]=report
        if row.get('journal_missing') and (cmd.get('status')=='READY' or cmd.get('never_sent') is True):
            report['status']='NO_SEND_EVIDENCE'; continue
        if row.get('result',{}).get('status')=='REJECTED':
            report['status']='REJECTED_RESPONSE'; continue
        if row.get('send_started') is not True:
            continue
        if cmd['kind']=='CANCEL':
            key=str(cmd['order_id'])
            order=facts['order'].get(key)
            if key in ownership and order and not unavailable and key not in active and order['state'] in {api.ORDER_STATE_CANCELED,api.ORDER_STATE_EXPIRED,api.ORDER_STATE_FILLED}:
                report['status']='CANCEL_TERMINAL_READBACK'
        elif cmd['kind']=='MODIFY':
            pid=str(cmd['position_id'])
            live=[p for p in positions if str(p['identifier'])==pid]
            owner=reports.get(position_owner.get(pid),{})
            if (owner.get('status')=='FILLED_OPEN_EVIDENCE' and len(live)==1
                    and number(live[0]['sl'])==number(cmd['sl']) and number(live[0]['tp'])==number(cmd['tp'])):
                report['status']='PROTECTION_READBACK'
                report['current_position_ticket']=ident(live[0]['ticket'])
    for cid,report in reports.items():
        report['cycle_id']=rows[cid]['command'].get('cycle_id')
    foreign_orders=[key for key in active if key not in ownership]
    foreign_positions=[ident(p['identifier']) for p in positions if str(p['identifier']) not in position_owner]
    return dict(history_complete=False, execution_ready=False, execution_blocker='HISTORY_TIME_PROFILE_UNVERIFIED',
                obligations=reports, source_status=sources, foreign_orders=foreign_orders,
                current_orders=active,current_positions={ident(p['identifier']):p for p in positions},
                foreign_positions=foreign_positions, raw_facts=facts, deal_times=times,
                order_times={key:{field:history_time(r[field]) for field in ('time_setup_msc','time_done_msc')
                                  if r.get(field)} for key,r in facts['order'].items()},
                closed_profit_by_deal=closed_profit,
                observed_closed_profit=str(sum((number(v) for v in closed_profit.values()),number(0))),
                pnl_semantics='REPLACEMENT_OBSERVATION_NOT_LEDGER_INCREMENT',
                evidence_blocked=bool(unavailable or foreign_orders or foreign_positions or not rows or
                    any(r['status'] not in {'FILLED_OPEN_EVIDENCE','CLOSED_EVIDENCE','TERMINAL_UNFILLED_EVIDENCE',
                                           'CANCEL_TERMINAL_READBACK','PROTECTION_READBACK'} for r in reports.values())))
