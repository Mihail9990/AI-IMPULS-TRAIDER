"""Prepared v2 range controller; runtime wiring waits for remaining TP/UI rules.

Uses the actual basket controller's ledger, ownership, OCO and intent gateway.
No broker calls outside that gateway. Never converts existing v1 active cycles.
"""
from .basket import BasketController
from .basket_math import last_closed_sl_price,recovery_range_after_sl

RANGE_POLICY='last_actual_close_v2'

class LatestCloseBasketController(BasketController):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        c=self.state.get('cycle')
        if c and c.get('range_policy')!=RANGE_POLICY:
            self.state['migration_block']='Активный цикл использует прежние диапазоны. Автоматического переноса нет; завершите сверку прежней версией.'
            self.state['phase']='MIGRATION_BLOCK';self.state['stop']=True
            self.save('latest_close_range_migration_block')
    def _new(self,snap):
        super()._new(snap)
        self.state['cycle']['range_policy']=RANGE_POLICY
        self.save('latest_close_range_policy')
    def _initial_position_recovery(self,snap,initial):
        # No reversal at the initial protective SL. Wait for actual closing proof.
        return
    def _recovery_levels(self,initial):
        c=self.state['cycle']
        if c['expanded'] is None:
            price=last_closed_sl_price([initial])
            c['expanded']=recovery_range_after_sl(initial['side'],initial['entry'],price,c['cfg']['EXPANSION_DISTANCE'],initial=True)
            c['range_anchor_exit']=price
            self.save('latest_close_initial_range_fixed')
        return c['expanded']
    def _after_stopped_group(self,snap,g,tickets):
        c=self.state['cycle']
        try:
            price=last_closed_sl_price(tickets)
            c['expanded']=recovery_range_after_sl(g['activated'],tickets[0]['entry'],price,c['cfg']['EXPANSION_DISTANCE'],initial=False)
        except (ValueError,ArithmeticError):
            self._halt(snap,'LAST_RECOVERY_EXIT_UNCONFIRMED_OR_AMBIGUOUS');return False
        c['range_anchor_exit']=price
        self.save('latest_close_recovery_range_fixed')
        return True

    def stage_entry_sl_once(self,group_id,trigger_position_id):
        """Owner-thread one-shot transaction; not a public unscoped action.

        Requires proven TP closure of index zero and the confirmed actual entry
        of index one. Manual SL edits after DONE are never overwritten by repeats.
        """
        from .control import ActionRejected
        from .broker import Spec
        from .settings import number
        snap=self._guard();c=self.state.get('cycle')
        if not c:raise ActionRejected('Нет активной корзины')
        g=next((g for g in c['groups'] if g['id']==group_id and g['role']!='initial'),None)
        if not g:raise ActionRejected('Корзина изменилась')
        if g.get('entry_sl_once'):return
        trigger=c['tickets'].get(trigger_position_id)
        if not trigger or trigger['group']!=group_id or trigger.get('closed') is not True or trigger.get('reason')!='TP' or trigger.get('index')!=0 or not trigger.get('exit_facts'):
            raise ActionRejected('TP ступени ещё не подтверждён')
        if c.get('edit') or self._uncertain() or snap['orders']:raise ActionRejected('Сначала сверка отмен, исполнений и предыдущей правки')
        second=[t for t in c['tickets'].values() if t['group']==group_id and t.get('index')==1]
        if len(second)!=1:raise ActionRejected('Фактический вход второй части ещё не подтверждён')
        anchor=second[0]['entry']
        spec=Spec.load(snap['spec']);targets=[]
        for t in c['tickets'].values():
            if t['group']!=group_id or t['closed']:continue
            live=next((p for p in snap['positions'] if p['id']==t['id']),None)
            if not live or number(live['size'])!=number(t['size']):raise ActionRejected('Остаток билета изменился; нужна сверка')
            try:spec.protection(t['side'],number(t['entry']),number(anchor),number(live['tp']),snap['quote'],modify=True)
            except (ValueError,ArithmeticError) as exc:raise ActionRejected('SL на входе пока недопустим по котировке или stop/freeze') from exc
            targets.append(dict(position_id=t['id'],sl=anchor,tp=live['tp'],old_sl=live['sl'],command_id=None))
        # Persist the one-shot marker and all targets before any broker intent.
        g['entry_sl_once']=dict(status='PREPARED',trigger=trigger_position_id,anchor_position_id=second[0]['id'],anchor_entry=anchor,targets=targets)
        self.save('entry_sl_once_prepared')

    def _advance_entry_sl_once(self,snap,g):
        from .broker import Spec
        from .settings import number
        c=self.state['cycle'];tx=g['entry_sl_once']
        if tx['status'] in ('DONE','FAILED'):return False
        active=[]
        for target in tx['targets']:
            t=c['tickets'][target['position_id']]
            if t['closed']:continue  # _observe already proved the full closing volume.
            live=next((p for p in snap['positions'] if p['id']==t['id']),None)
            if not live: return True
            active.append((target,t,live))
        if tx['status']=='PREPARED':
            # Revalidate all targets before staging any MODIFY; no batch atomicity claimed.
            try:
                spec=Spec.load(snap['spec'])
                for target,t,live in active:
                    if number(live['sl'])!=number(target['old_sl']) or number(live['tp'])!=number(target['tp']):raise ValueError('Protection changed')
                    spec.protection(t['side'],number(t['entry']),number(target['sl']),number(target['tp']),snap['quote'],modify=True)
            except (ValueError,ArithmeticError):
                tx['status']='FAILED';self._halt(snap,'ENTRY_SL_PREFLIGHT_CHANGED');return True
            for target,t,live in active:
                if number(live['sl'])!=number(target['sl']):
                    target['command_id']=self._intent('MODIFY',position_id=t['id'],sl=target['sl'],tp=target['tp'],purpose='ANCHOR')
            tx['status']='PENDING';self.save('entry_sl_once_intents');return True
        for target,t,live in active:
            cmd=self.state['commands'].get(target['command_id']) if target['command_id'] else None
            if cmd and cmd['status']=='REJECTED':
                tx['status']='FAILED';self._halt(snap,'ENTRY_SL_PARTIAL_OR_REJECTED');return True
            if cmd and cmd['status']!='DONE':return True
            if number(live['sl'])!=number(target['sl']) or number(live['tp'])!=number(target['tp']):
                # ACK without matching broker readback is not completion.
                return True
        for target,t,live in active:t.update(sl=live['sl'],tp=live['tp'])
        tx['status']='DONE';self.save('entry_sl_once_confirmed');return False

    def _stage_first_tp_if_ready(self,snap):
        from .control import ActionRejected
        c=self.state['cycle']
        if c.get('edit') or self._uncertain() or snap['orders']:return False
        for g in c['groups']:
            if g['role']=='initial' or g.get('entry_sl_once'):continue
            tickets=[t for t in c['tickets'].values() if t['group']==g['id']]
            trigger=next((t for t in tickets if t.get('index')==0 and t['closed'] and t.get('reason')=='TP'),None)
            if not trigger or not any(not t['closed'] for t in tickets):continue
            selected=[cid for cid in g['commands'] if self.state['commands'][cid]['side']==g.get('activated')]
            if len(tickets)!=len(selected):return False
            try:self.stage_entry_sl_once(g['id'],trigger['id'])
            except ActionRejected:
                self._halt(snap,'TP1_SECOND_ENTRY_SL_UNAVAILABLE');return True
            return True
        return False

    def _step(self,snap):
        if not self._observe(snap):return
        c=self.state['cycle']
        if c.get('manual'):return super()._step(snap)
        if self._stage_first_tp_if_ready(snap):return
        for g in c['groups']:
            if g.get('entry_sl_once') and self._advance_entry_sl_once(snap,g):return
        return super()._step(snap)

    def edit(self,**kwargs):
        from .control import ActionRejected
        c=self.state.get('cycle') or {}
        if any(g.get('entry_sl_once',{}).get('status') in ('PREPARED','PENDING') for g in c.get('groups',[])):
            raise ActionRejected('Перенос SL ещё не подтверждён; повторная правка заблокирована')
        return super().edit(**kwargs)
