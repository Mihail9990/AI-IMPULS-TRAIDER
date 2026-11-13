"""Read-only history of this bot's confirmed closes; never a broker query."""
from collections import defaultdict
from decimal import Decimal as D
import json
import sqlite3
from .basket_math import net_money
from .settings import number


def project_history(state,records,facts,*,symbol,magic,to_utc=None):
    currency=state.get('currency');groups=defaultdict(list);complete=True
    expected=set(state.get('seen_deals',[]));seen=set()
    for row in records:
        if row.get('backend')!=state.get('backend') or row.get('account')!=state.get('account'):continue
        if row.get('currency')!=currency or row.get('ticket') in seen:
            complete=False;continue
        seen.add(row.get('ticket'));groups[str(row.get('position_id'))].append(row)
    if seen!=expected:complete=False
    history=[];total=D(0);known_cycles=set()
    for pid,rows in groups.items():
        known_cycles.update(r.get('cycle_id') for r in rows)
        outs=[r.get('raw',{}) for r in rows]
        ins=[r for r in facts if str(r.get('position_id'))==pid and r.get('entry')==0]
        item=dict(position_id=pid,status='closed',side=None,opened_at=None,closed_at=None,entry_price=None,exit_price=None,reason=None,net=None,currency=currency)
        ok=True
        try:
            if not ins or not outs:raise ValueError()
            if len({str(r.get('ticket')) for r in ins})!=len(ins):raise ValueError()
            if any(str(r.get('ticket'))!=str(r.get('raw',{}).get('ticket')) or str(r.get('raw',{}).get('position_id'))!=pid for r in rows):raise ValueError()
            if any(r.get('symbol')!=symbol or r.get('magic')!=magic for r in ins+outs):raise ValueError()
            kinds={r['type'] for r in ins}
            if len(kinds)!=1 or not kinds<={0,1}:raise ValueError()
            side=next(iter(kinds));item['side']='BUY' if side==0 else 'SELL'
            if any(r.get('entry')!=1 or r.get('type')!=1-side for r in outs):raise ValueError()
            entered=sum((number(r['volume']) for r in ins),D(0));exited=sum((number(r['volume']) for r in outs),D(0))
            if entered<=0 or entered!=exited:raise ValueError()
            item['entry_price']=str(sum((number(r['price'])*number(r['volume']) for r in ins),D(0))/entered)
            item['exit_price']=str(sum((number(r['price'])*number(r['volume']) for r in outs),D(0))/exited)
            reasons={r.get('reason') for r in outs}
            # MT5 enum: SL=4, TP=5; never derive a reason from the P/L sign.
            item['reason']='TP' if reasons=={5} else 'SL' if reasons=={4} else None
            opening=net_money(ins);closing=net_money(outs)
            for r in rows:
                accounting=r.get('net_accounting')
                if accounting and (number(accounting['opening_net'])!=opening or number(accounting['closing_net'])!=net_money([r['raw']])):raise ValueError()
            item['net']=str(opening+closing);total+=opening+closing
            if to_utc:
                item['opened_at']=to_utc(min(number(r['time_msc']) for r in ins)/1000)
                item['closed_at']=to_utc(max(number(r['time_msc']) for r in outs)/1000)
        except (KeyError,TypeError,ValueError,ArithmeticError):
            ok=False;item['net']=None
        item['complete']=ok
        if not ok:item['status']='incomplete'
        if not ok:complete=False
        history.append(item)
    # Old aggregate cycle records cannot prove position-level P/L. Do not sum them.
    if any(c.get('id') not in known_cycles for c in state.get('completed',[])):complete=False
    for pid,ticket in (state.get('cycle') or {}).get('tickets',{}).items():
        if str(pid) in groups:continue
        if ticket.get('closed'):
            # Legacy/incomplete snapshots may know a close without its durable
            # money records. Keep it visible, but never fabricate a zero total.
            complete=False
            history.append(dict(position_id=str(pid),status='incomplete',side=ticket.get('side'),
                entry_price=ticket.get('entry'),exit_price=None,opened_at=None,closed_at=None,
                reason=None,net=None,currency=currency,complete=False))
            continue
        ins=[r for r in facts if str(r.get('position_id'))==str(pid) and r.get('entry')==0 and r.get('symbol')==symbol and r.get('magic')==magic]
        opened=None
        try:
            if to_utc and ins:opened=to_utc(min(number(r['time_msc']) for r in ins)/1000)
        except (ValueError,KeyError,TypeError,ArithmeticError):pass
        history.append(dict(position_id=str(pid),status='open',side=ticket.get('side'),entry_price=ticket.get('entry'),exit_price=None,opened_at=opened,closed_at=None,reason=None,net=None,currency=currency,complete=True))
    history.sort(key=lambda r:(r['closed_at'] is not None,r['closed_at'] or 0,r['position_id']),reverse=True)
    return dict(position_history=history[:200],history_metrics=dict(all_closed_net=str(total) if complete else None,history_complete=complete,
        history_note='Чистый итог закрытых позиций по сохранённой истории этого бота; комиссии, swap и fee учтены.' if complete else 'Старая история неполна или не сверена. Общий итог не подтверждён; отсутствующие данные не восстановлены.'))


def native_history(controller,*,verified_time=False):
    state=controller.state;broker=controller.broker
    try:
        rows=[json.loads(r[0]) for r in controller.store.db.execute('SELECT payload FROM consumed_deals WHERE backend=? AND account=?',(state.get('backend'),state.get('account')))]
        facts=[json.loads(r[0]) for r in controller.store.db.execute("SELECT payload FROM mt5_facts WHERE kind='deal'")]
        convert=(lambda value:broker.market_time_to_utc(float(value))) if verified_time and hasattr(broker,'market_time_to_utc') else None
        return project_history(state,rows,facts,symbol=broker.symbol,magic=broker.magic,to_utc=convert)
    except (sqlite3.Error,ValueError,TypeError,AttributeError):
        return dict(position_history=[],history_metrics=dict(all_closed_net=None,history_complete=False,history_note='Подтверждённая история позиций пока недоступна.'))
