"""One-click own-entry SL batch; TP and exposure are preserved."""
from copy import deepcopy
from .control import ActionRejected
from .broker import Spec
from .settings import number

class OwnEntrySL:
    def request_own_entry_sl(self,a):
        if set(a)!={'type','cycle_id','expected_positions'}:
            raise ActionRejected('Неверные поля команды SL на вход')
        snap=self._guard(entry=True);c=self.state.get('cycle')
        if not c or a['cycle_id']!=c['id']:raise ActionRejected('Цикл изменился; обновите панель')
        if c.get('edit') or c.get('manual_close') or c.get('initial_cancel') or self._protection_busy(snap):
            raise ActionRejected('Сначала сверка предыдущих операций и ожидающих ордеров')
        if any(r.get('status')=='UNKNOWN' and rid!=getattr(self,'_active_control_id',None) for rid,r in self.state.get('control_receipts',{}).items()):
            raise ActionRejected('Предыдущая команда имеет UNKNOWN; требуется сверка')
        try:
            active=[t for t in c['tickets'].values() if not t['closed']]
            ids=sorted(t['id'] for t in active)
            if not ids or not isinstance(a['expected_positions'],list) or sorted(a['expected_positions'])!=ids:raise ValueError()
            live={p['id']:p for p in snap['positions']}
            if sorted(live)!=ids or len({t['group'] for t in active})!=1:raise ValueError()
            g=next(g for g in c['groups'] if g['id']==active[0]['group'])
            if g.get('entry_sl_once',{}).get('status') in ('PREPARED','PENDING') or g.get('actual_ladder_tx',{}).get('status')=='PENDING':raise ValueError()
            spec=Spec.load(snap['spec']);updates=[]
            for t in active:
                p=live[t['id']]
                if number(p['size'])!=number(t['size']) or number(p['entry'])!=number(t['entry']) or p['side']!=t['side']:raise ValueError()
                sl=spec.price(t['entry'])
                spec.protection(t['side'],sl,sl,number(p['tp']),snap['quote'],modify=True)
                updates.append(dict(position_id=t['id'],sl=str(sl),tp=p['tp']))
        except (ValueError,TypeError,KeyError,StopIteration,ArithmeticError):
            raise ActionRejected('SL на фактическом входе недопустим: проверьте состав, цену, tick и stop/freeze. Ни один MODIFY не отправлен.')
        c['edit']=dict(stage='CANCEL',settings=deepcopy(c['cfg']),levels=None,updates=updates,cancel_ids=[],groups=[],
            fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],
            own_entry_sl_group=g['id'],expected_protection={p['id']:{k:p[k] for k in ('sl','tp')} for p in snap['positions']})
        self.save('own_entry_sl_prepared')
