"""Closed-candle and latest-close recovery controller.

Uses the actual basket controller's ledger, ownership, OCO and intent gateway.
No broker calls outside that gateway. Never converts existing v1 active cycles.
"""
from .basket import BasketController
from .basket_math import last_closed_sl_price,recovery_range_after_sl,configured_ladder,BasketSettings
from .control import ActionRejected
from .broker import Spec
from .settings import number
from copy import deepcopy

RANGE_POLICY='last_actual_close_v2'

class LatestCloseBasketController(BasketController):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.state.setdefault('tp_distances',[None]*20)
        c=self.state.get('cycle')
        if c and (c.get('range_policy')!=RANGE_POLICY or 'tp_distances' not in c):
            self.state['migration_block']='Активный цикл использует прежние диапазоны. Автоматического переноса нет; завершите сверку прежней версией.'
            self.state['phase']='MIGRATION_BLOCK';self.state['stop']=True
            self.save('latest_close_range_migration_block')
    def _new(self,snap):
        super()._new(snap)
        self.state['cycle']['range_policy']=RANGE_POLICY
        self.state['cycle']['tp_distances']=deepcopy(self.state['tp_distances'])
        self.save('latest_close_range_policy')
    def _validate_distances(self,values,spec):
        if not isinstance(values,list) or len(values)!=20:
            raise ActionRejected('Нужны 20 слотов TP; незаданные — null')
        try:
            result=[None if v is None else str(spec.price(v)) for v in values]
            if result[0] is None or any(number(v)<=0 for v in result if v is not None):raise ValueError()
            fixed=[number(v) for v in result if v is not None]
            if any(a>=b for a,b in zip(fixed,fixed[1:])):raise ValueError()
            indexes=[i for i,v in enumerate(result) if v is not None]
            for left,right in zip(indexes,indexes[1:]):
                if ((number(result[right])-number(result[left]))/(right-left))%spec.tick_size:raise ValueError()
            if number(result[0])<spec.point*spec.stops_level:raise ValueError()
        except (ValueError,TypeError,ArithmeticError):
            raise ActionRejected('Задайте TP1 и положительные возрастающие расстояния с точностью tick; пустые ступени рассчитываются автоматически')
        return result

    def command(self,text,update_id=None):
        if text.strip().lower() in {'/start','/start auto'}:
            snap=self._guard(entry=True)
            self._validate_distances(self.state['tp_distances'],Spec.load(snap['spec']))
        return super().command(text,update_id)

    def _recovery_tps(self,cfg,spec,side,entries,sizes,debt):
        values=self.state['cycle']['tp_distances'][:len(entries)]
        return configured_ladder(self.broker,spec,side,entries,sizes,debt,values)

    def action(self,a):
        if isinstance(a,dict) and a.get('type')=='tp_distances':
            if set(a)!={'type','cycle_id','distances'} or a['cycle_id']!=(self.state.get('cycle') or {}).get('id'):
                raise ActionRejected('Цикл изменился; обновите параметры')
            return self.edit_tp_distances(a['distances'])
        return super().action(a)

    def edit_tp_distances(self,values):
        snap=self._guard(entry=True);spec=Spec.load(snap['spec'])
        values=self._validate_distances(values,spec);c=self.state.get('cycle')
        if not c:
            self.state['tp_distances']=values;self.save('tp_distances_saved');return
        if c.get('edit') or c.get('initial_cancel') or self._uncertain() or any(g.get('entry_sl_once',{}).get('status') in ('PREPARED','PENDING') or g.get('actual_ladder_tx',{}).get('status')=='PENDING' for g in c['groups']):
            raise ActionRejected('Предыдущая правка или исполнение ещё не сверены')
        pending=[];updates=[];live={p['id']:p for p in snap['positions']};cfg=BasketSettings.load(c['cfg'])
        try:
            for g in c['groups']:
                if g.get('retired'):continue
                ts=sorted([t for t in c['tickets'].values() if t['group']==g['id']],key=lambda t:t['index'])
                orders=self._pending(snap,g['commands'])
                if ts and orders:raise ValueError('Partial fill')
                if g['role']=='initial':continue
                if ts and any(t['closed'] for t in ts):
                    if any(not t['closed'] for t in ts):raise ValueError('Partially closed ladder: use individual chart edits')
                    continue
                if ts:
                    tps=configured_ladder(self.broker,spec,g['activated'],[t['entry'] for t in ts],[t['size'] for t in ts],c['recovery'],values[:len(ts)],quote=snap['quote'],modify=True)
                    for t,tp in zip(ts,tps):
                        p=live[t['id']]
                        if number(p['size'])!=number(t['size']):raise ValueError('Volume changed')
                        spec.protection(t['side'],number(t['entry']),number(p['sl']),number(tp),snap['quote'],modify=True)
                        updates.append(dict(position_id=t['id'],sl=p['sl'],tp=tp))
                if orders:
                    for side,entry in g['levels'].items():
                        configured_ladder(self.broker,spec,side,[entry]*cfg.BASKET_COUNT,[cfg.TICKET_SIZE]*cfg.BASKET_COUNT,c['recovery'],values[:cfg.BASKET_COUNT])
                    pending.append(g)
        except (ValueError,ArithmeticError,KeyError):
            raise ActionRejected('Сетка недопустима или корзина частично исполнена/закрыта. После частичного TP меняйте оставшиеся уровни на графике; SL сохраняется.')
        c['edit']=dict(stage='CANCEL',settings=deepcopy(c['cfg']),levels=None,updates=updates,
            cancel_ids=[cid for g in pending for cid in g['commands']],groups=[g['id'] for g in pending],
            fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],tp_distances=values)
        self.save('tp_grid_edit_requested')

    def _commit_edit_extra(self,e):
        if 'tp_distances' in e:
            self.state['tp_distances']=deepcopy(e['tp_distances'])
            self.state['cycle']['tp_distances']=deepcopy(e['tp_distances'])

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

    def _live_ladder(self,snap,g):
        c=self.state['cycle'];tickets=sorted([t for t in c['tickets'].values() if t['group']==g['id']],key=lambda t:t['index'])
        selected=[cid for cid in g['commands'] if self.state['commands'][cid]['side']==g['activated']]
        if len(tickets)!=len(selected) or g['ladder_applied']:return
        if any(t['closed'] for t in tickets):
            self._halt(snap,'ACTUAL_LADDER_GAP_EXIT_REQUIRES_REVIEW');return
        targets=[]
        try:
            spec=Spec.load(snap['spec']);cfg=BasketSettings.load(c['cfg'])
            tps=self._recovery_tps(cfg,spec,g['activated'],[t['entry'] for t in tickets],[t['size'] for t in tickets],c['recovery'])
            for t,tp in zip(tickets,tps):
                live=next(p for p in snap['positions'] if p['id']==t['id'])
                spec.protection(t['side'],number(t['entry']),number(live['sl']),number(tp),snap['quote'],modify=True)
                targets.append(dict(position_id=t['id'],sl=live['sl'],tp=tp,command_id=None))
        except (ValueError,ArithmeticError,StopIteration):
            self._halt(snap,'ACTUAL_FILL_LADDER_REQUIRES_REVIEW');return
        for u in targets:
            live=next(p for p in snap['positions'] if p['id']==u['position_id'])
            if number(live['tp'])!=number(u['tp']):
                u['command_id']=self._intent('MODIFY',position_id=u['position_id'],sl=u['sl'],tp=u['tp'],purpose='ANCHOR')
        g['actual_ladder_tx']=dict(status='PENDING',targets=targets)
        g['ladder_applied']=True;self.save('actual_fill_ladder_intents')

    def _advance_actual_ladder(self,snap,g):
        tx=g.get('actual_ladder_tx')
        if not tx or tx['status']=='DONE':return False
        for u in tx['targets']:
            t=self.state['cycle']['tickets'][u['position_id']]
            if t['closed']:continue
            cmd=self.state['commands'].get(u['command_id']) if u['command_id'] else None
            if cmd and cmd['status']=='REJECTED':
                self._halt(snap,'ACTUAL_LADDER_PARTIAL_FAILURE');return True
            if cmd and cmd['status']!='DONE':return True
            live=next((p for p in snap['positions'] if p['id']==u['position_id']),None)
            if not live or any(number(live[k])!=number(u[k]) for k in ('sl','tp')):return True
        for u in tx['targets']:
            self.state['cycle']['tickets'][u['position_id']].update(sl=u['sl'],tp=u['tp'])
        tx['status']='DONE';self.save('actual_fill_ladder_confirmed');return False

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
        for g in c['groups']:
            if self._advance_actual_ladder(snap,g):return
        if self._stage_first_tp_if_ready(snap):return
        for g in c['groups']:
            if g.get('entry_sl_once') and self._advance_entry_sl_once(snap,g):return
        return super()._step(snap)

    def edit(self,**kwargs):
        from .control import ActionRejected
        c=self.state.get('cycle') or {}
        settings=kwargs.get('settings')
        if settings is not None and isinstance(settings,dict):
            if any(settings.get(k)!=self.state['basket_settings'][k] for k in ('COST_RESERVE','TP_STEP_MULTIPLIER')):
                raise ActionRejected('Резерв и множитель прежней стратегии не используются; меняйте расстояния ступеней TP')
        if any(g.get('entry_sl_once',{}).get('status') in ('PREPARED','PENDING') or g.get('actual_ladder_tx',{}).get('status')=='PENDING' for g in c.get('groups',[])):
            raise ActionRejected('Перенос SL ещё не подтверждён; повторная правка заблокирована')
        return super().edit(**kwargs)
