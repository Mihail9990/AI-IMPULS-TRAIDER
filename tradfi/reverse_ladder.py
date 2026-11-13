"""Fixed reverse SL ladder, one level per remaining independent ticket."""
from .settings import number
from .control import ActionRejected
from .broker import Spec
from copy import deepcopy


def reverse_sl_prices(spec,side,entries,current,count):
    if side not in ('BUY','SELL') or type(count) is not int or count<1 or not isinstance(entries,list) or len(entries)!=count:
        raise ValueError('Нужны фактические входы всех оставшихся билетов')
    entries=[spec.price(e) for e in entries];current=spec.price(current)
    sign=1 if side=='BUY' else -1
    if any(sign*(current-e)<=0 for e in entries):raise ValueError('Цена должна быть в прибыли относительно каждого входа')
    # Rank fraction is preserved, but each ticket has its own actual anchor.
    values=[spec.price(e+i*(current-e)/count) for i,e in enumerate(entries)]
    if any(sign*(b-a)<=0 for a,b in zip(values,values[1:])):
        raise ValueError('Разные входы нарушают порядок SL-ступеней; перестановки и округления нет')
    return [str(p) for p in values]


class ReverseLadderControl:
    @staticmethod
    def _tp1_reverse_confirmed(c,group):
        first=[t for t in c.get('tickets',{}).values() if t.get('group')==group['id'] and t.get('index')==0 and t.get('role')!='initial']
        if len(first)!=1:return False
        t=first[0];transfer=group.get('entry_sl_once',{})
        if t.get('closed') is not True or t.get('reason')!='TP' or transfer.get('status')!='DONE' or transfer.get('trigger')!=t.get('id'):return False
        facts=t.get('exit_facts') or []
        try:
            return bool(facts) and all(f.get('reason')=='TP' and number(f['volume'])>0 for f in facts) and sum(number(f['volume']) for f in facts)==number(t['size'])
        except (KeyError,TypeError,ValueError,ArithmeticError):return False

    def reverse_controls(self,snap):
        """Read-only UI capability snapshot; every action repeats server checks."""
        denied=lambda reason:dict(can_enable=False,can_disable=False,reason=reason)
        c=self.state.get('cycle')
        if getattr(self.broker,'read_only',False):return denied('READ_ONLY: изменение SL запрещено')
        if not c or self.state.get('phase') in ('SAFE_PAUSE','MANUAL','MIGRATION_BLOCK') or c.get('manual') or self.state.get('migration_block') or self.state.get('time_block'):
            return denied('Нет доступной для управления recovery-корзины')
        if c.get('edit') or c.get('manual_close') or self._protection_busy(snap):
            return denied('Ожидается сверка предыдущих изменений')
        active=[t for t in c['tickets'].values() if not t['closed']]
        if not active or any(t['role']=='initial' for t in active) or len({t['group'] for t in active})!=1:
            return denied('Нет активной recovery-корзины')
        if sorted(t['id'] for t in active)!=sorted(p['id'] for p in snap.get('positions',[])):
            return denied('Состав позиций ещё не сверён')
        group=next(g for g in c['groups'] if g['id']==active[0]['group'])
        if not self._tp1_reverse_confirmed(c,group):
            return denied('Доступно только после подтверждённого TP №1 и переноса SL')
        if group.get('actual_ladder_tx',{}).get('status')=='PENDING':return denied('Ожидается подтверждение TP')
        enabled=bool(group.get('reverse_ladder',{}).get('enabled'))
        return dict(can_enable=not enabled,can_disable=enabled,reason='')

    def request_reverse_ladder(self,a):
        if set(a)!={'type','cycle_id','enabled','expected_positions'} or type(a['enabled']) is not bool:
            raise ActionRejected('Неверные параметры обратной лестницы')
        snap=self._guard(entry=True);c=self.state.get('cycle')
        if not c or c['id']!=a['cycle_id']:raise ActionRejected('Цикл изменился')
        if c.get('edit') or c.get('manual_close') or self._protection_busy(snap):
            raise ActionRejected('Сначала сверка предыдущих изменений и OCO')
        active=sorted([t for t in c['tickets'].values() if not t['closed']],key=lambda t:t['index'])
        try:
            if not active or any(t['role']=='initial' for t in active):raise ValueError()
            if not isinstance(a['expected_positions'],list) or sorted(a['expected_positions'])!=sorted(t['id'] for t in active):raise ValueError()
            if sorted(p['id'] for p in snap['positions'])!=sorted(t['id'] for t in active):raise ValueError()
            if len({t['group'] for t in active})!=1 or len({t['side'] for t in active})!=1:raise ValueError()
            group=next(g for g in c['groups'] if g['id']==active[0]['group'])
            if group.get('actual_ladder_tx',{}).get('status')=='PENDING':raise ValueError()
            spec=Spec.load(snap['spec']);entries=[spec.price(t['entry']) for t in active]
        except (ValueError,TypeError,KeyError,StopIteration):
            raise ActionRejected('Состав корзины или фактические входы частей не подтверждены')
        # User-confirmed rule: only proven TP of slot zero, then completed SL transfer.
        if not self._tp1_reverse_confirmed(c,group):
            raise ActionRejected('Обратная лестница разрешена только после подтверждённого TP №1 и завершённого переноса SL')
        old=group.get('reverse_ladder')
        if not a['enabled'] and not (old and old.get('enabled')):
            raise ActionRejected('Обратная лестница не включена')
        if a['enabled'] and old and old.get('enabled'):
            raise ActionRejected('Лестница уже включена; для нового расчёта сначала отмените её')
        side=active[0]['side'];quote=snap['quote'];current=quote['bid' if side=='BUY' else 'ask']
        try:
            sls=reverse_sl_prices(spec,side,entries,current,len(active)) if a['enabled'] else [str(e) for e in entries]
            live={p['id']:p for p in snap['positions']};updates=[]
            for ticket,sl in zip(active,sls):
                p=live[ticket['id']]
                if number(p['size'])!=number(ticket['size']) or number(p['entry'])!=number(ticket['entry']):raise ValueError()
                spec.protection(side,number(ticket['entry']),number(sl),number(p['tp']),quote,modify=True)
                updates.append(dict(position_id=ticket['id'],sl=sl,tp=p['tp']))
        except (ValueError,KeyError,ArithmeticError):
            raise ActionRejected('Обратная лестница/отмена недопустима по tick, Bid/Ask или stop/freeze. Защита не изменена; округления нет.')
        c['edit']=dict(stage='CANCEL',settings=deepcopy(c['cfg']),levels=None,updates=updates,cancel_ids=[],groups=[],
            fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],
            reverse_group=group['id'],reverse_ladder=dict(enabled=a['enabled'],anchor_policy='own_entry_v1',entries={t['id']:str(e) for t,e in zip(active,entries)},quote=str(current),targets=deepcopy(updates)),
            expected_protection={p['id']:{k:p[k] for k in ('sl','tp')} for p in snap['positions']})
        self.save('reverse_ladder_change_prepared')
