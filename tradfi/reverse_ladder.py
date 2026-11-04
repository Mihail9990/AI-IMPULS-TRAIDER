"""Fixed reverse SL ladder, one level per remaining independent ticket."""
from .settings import number
from .control import ActionRejected
from .broker import Spec
from copy import deepcopy


def reverse_sl_prices(spec,side,anchor,current,count):
    if side not in ('BUY','SELL') or type(count) is not int or count<1:
        raise ValueError('Нужна сторона и положительное число билетов')
    anchor,current=spec.price(anchor),spec.price(current)
    sign=1 if side=='BUY' else -1
    if sign*(current-anchor)<=0:raise ValueError('Цена должна быть в прибыли относительно входа второй части')
    step=(current-anchor)/count
    # No rounding: rounding changes user-approved equal spacing.
    return [str(spec.price(anchor+i*step)) for i in range(count)]


class ReverseLadderControl:
    def request_reverse_ladder(self,a):
        if set(a)!={'type','cycle_id','enabled','expected_positions'} or type(a['enabled']) is not bool:
            raise ActionRejected('Неверные параметры обратной лестницы')
        snap=self._guard(entry=True);c=self.state.get('cycle')
        if not c or c['id']!=a['cycle_id']:raise ActionRejected('Цикл изменился')
        if c.get('edit') or c.get('manual_close') or self._unresolved() or snap['orders']:
            raise ActionRejected('Сначала сверка предыдущих изменений и OCO')
        active=sorted([t for t in c['tickets'].values() if not t['closed']],key=lambda t:t['index'])
        try:
            if not active or any(t['role']=='initial' for t in active):raise ValueError()
            if not isinstance(a['expected_positions'],list) or sorted(a['expected_positions'])!=sorted(t['id'] for t in active):raise ValueError()
            if sorted(p['id'] for p in snap['positions'])!=sorted(t['id'] for t in active):raise ValueError()
            if len({t['group'] for t in active})!=1 or len({t['side'] for t in active})!=1:raise ValueError()
            group=next(g for g in c['groups'] if g['id']==active[0]['group'])
            if group.get('actual_ladder_tx',{}).get('status')=='PENDING':raise ValueError()
            second=[t for t in c['tickets'].values() if t['group']==group['id'] and t['index']==1]
            if len(second)!=1:raise ValueError()
            spec=Spec.load(snap['spec']);anchor=spec.price(second[0]['entry'])
        except (ValueError,TypeError,KeyError,StopIteration):
            raise ActionRejected('Состав корзины или фактический вход второй части не подтверждён')
        # Priority between TP1 automation and an earlier reverse activation has
        # not yet been specified; don't silently replace either protection plan.
        if group.get('entry_sl_once',{}).get('status')!='DONE':
            raise ActionRejected('До подтверждённого переноса SL после TP1 приоритет обратной лестницы ещё не задан')
        old=group.get('reverse_ladder')
        if not a['enabled'] and not (old and old.get('enabled')):
            raise ActionRejected('Обратная лестница не включена')
        if a['enabled'] and old and old.get('enabled'):
            raise ActionRejected('Лестница уже включена; для нового расчёта сначала отмените её')
        side=active[0]['side'];quote=snap['quote'];current=quote['bid' if side=='BUY' else 'ask']
        try:
            sls=reverse_sl_prices(spec,side,anchor,current,len(active)) if a['enabled'] else [str(anchor)]*len(active)
            live={p['id']:p for p in snap['positions']};updates=[]
            for ticket,sl in zip(active,sls):
                p=live[ticket['id']]
                if number(p['size'])!=number(ticket['size']):raise ValueError()
                spec.protection(side,number(ticket['entry']),number(sl),number(p['tp']),quote,modify=True)
                updates.append(dict(position_id=ticket['id'],sl=sl,tp=p['tp']))
        except (ValueError,KeyError,ArithmeticError):
            raise ActionRejected('Обратная лестница/отмена недопустима по tick, Bid/Ask или stop/freeze. Защита не изменена; округления нет.')
        c['edit']=dict(stage='CANCEL',settings=deepcopy(c['cfg']),levels=None,updates=updates,cancel_ids=[],groups=[],
            fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],
            reverse_group=group['id'],reverse_ladder=dict(enabled=a['enabled'],anchor=str(anchor),anchor_position_id=second[0]['id'],quote=str(current),targets=deepcopy(updates)),
            expected_protection={p['id']:{k:p[k] for k in ('sl','tp')} for p in snap['positions']})
        self.save('reverse_ladder_change_prepared')
