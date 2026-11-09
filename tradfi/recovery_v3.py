"""Owner-directed V3 recovery, the only supported active strategy.

All orders use the existing durable intent/evidence gateway. No manual MT5
positions are adopted, no missed candle is replayed, and no STOP becomes MARKET.
"""
from copy import deepcopy
from decimal import Decimal as D
import uuid
from .basket_latest_close import LatestCloseBasketController
from .basket_math import BasketSettings
from .broker import Spec
from .control import ActionRejected
from .settings import number
from .engine import TERMINAL


class ControlledRecoveryController(LatestCloseBasketController):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.state.setdefault('v3_settings',dict(entry_distance='1.00',count_presets=[4,6,10]))
        c=self.state.get('cycle')
        legacy=bool(c and (c.get('recovery_mode')!='V3' or c.get('manual_close') or c.get('range_override')))
        orphan=not c and any(cmd['status'] not in TERMINAL for cmd in self.state['commands'].values())
        if legacy or orphan:
            self.state.setdefault('v3_migration_snapshot',deepcopy(self.state))
            self.state['migration_block']='Обнаружен незавершённый прежний цикл или несверенные ордера. Долг, позиции и ордера сохранены; автоматического переноса в V3 нет. Нужна сверка прежней версией перед обновлением.'
            self.state['phase']='MIGRATION_BLOCK';self.state['stop']=True
        # Only idle state is upgraded; a live legacy cycle keeps its own label.
        self.state['recovery_mode']='V3'
        if c and not self.state.get('migration_block') and c.get('v3_request'):
            c.pop('v3_request');self.notify('Запланированный V3 вход отменён после перезапуска; выберите новую свечу.')
        self.save('primary_v3_loaded')

    def _v3(self):return not self.state.get('migration_block') and (self.state.get('cycle') or {}).get('recovery_mode','V3')=='V3'

    def _new(self,snap):
        super()._new(snap)
        self.state['cycle']['recovery_mode']='V3'
        self.save('recovery_mode_bound')

    def _mutation_allowed(self,kind=None):
        if self.state.get('migration_block'):return False
        return super()._mutation_allowed(kind)

    def dispatch(self,kinds=None):
        if self.state.get('migration_block'):return
        return super().dispatch(kinds)

    def request_manual_close(self,a):
        raise ActionRejected('Закрытие с выбором диапазонов больше не используется')

    def command(self,text,update_id=None):
        if self._v3() and text.strip().lower()=='/stop':
            c=self.state.get('cycle') or {};c.pop('v3_request',None)
            if c.get('v3_link'):c['v3_link']['cancel']=True
        return super().command(text,update_id)

    @staticmethod
    def _count(value):
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=20:
            raise ActionRejected('Число ступеней: целое от 1 до 20')
        return value

    def _context(self,a,entry=True):
        snap=self._guard(entry=entry);c=self.state.get('cycle')
        if not self._v3() or not c or a.get('cycle_id')!=c['id']:
            raise ActionRejected('Нужен текущий цикл V3; обновите панель')
        if c.get('edit') or c.get('v3_edit') or c.get('manual_close') or c.get('initial_cancel') or self._uncertain():
            raise ActionRejected('Предыдущая операция ещё не сверена')
        if self.state['stop']:raise ActionRejected('Новые входы остановлены')
        if any(g.get('actual_ladder_tx',{}).get('status')=='PENDING' or g.get('entry_sl_once',{}).get('status') in ('PREPARED','PENDING') for g in c['groups']):
            raise ActionRejected('Защита корзины ещё не сверена')
        return snap,c

    def action(self,a):
        if not isinstance(a,dict):raise ActionRejected('Неверная команда')
        kind=a.get('type')
        if kind=='recovery_mode':raise ActionRejected('V3 — основная логика; переключение версий удалено')
        if kind=='v3_settings':
            if set(a)!={'type','cycle_id','entry_distance','count_presets'} or a['cycle_id']!=(self.state.get('cycle') or {}).get('id'):raise ActionRejected('Обновите настройки V3')
            snap=self._guard(entry=True);spec=Spec.load(snap['spec'])
            try:
                distance=spec.price(a['entry_distance'])
                if distance<=0 or distance<spec.point*spec.stops_level:raise ValueError()
                presets=a['count_presets']
                if not isinstance(presets,list) or len(presets)!=3:raise ValueError()
                presets=[self._count(x) for x in presets]
            except (ValueError,TypeError,ArithmeticError):raise ActionRejected('Нужны положительный отступ с точностью tick и три количества 1–20')
            self.state['v3_settings']=dict(entry_distance=str(distance),count_presets=presets);self.save('v3_settings_saved');return
        if kind=='manual_close':raise ActionRejected('Выбор диапазона после закрытия в V3 не используется')
        if kind=='v3_entry':return self._request_entry(a)
        if kind=='v3_link':return self._request_link(a)
        if kind=='v3_cancel_link':
            if set(a)!={'type','cycle_id'}:raise ActionRejected('Неверные поля отмены')
            snap=self._guard();c=self.state.get('cycle')
            if not c or a['cycle_id']!=c['id'] or not c.get('v3_link'):raise ActionRejected('Нет связанных триггеров')
            c['v3_link']['cancel']=True;self.save('v3_link_cancel_requested')
            self._cancel(snap,c['v3_link']['ids']);return
        if kind=='v3_cancel_request':
            if set(a)!={'type','cycle_id'}:raise ActionRejected('Неверные поля отмены')
            self._guard();c=self.state.get('cycle')
            if not c or a['cycle_id']!=c['id'] or not c.get('v3_request'):raise ActionRejected('Нет запланированного входа')
            c.pop('v3_request');self.save('v3_schedule_cancelled');return
        if kind=='v3_adjust_sl':return self._adjust_sl(a)
        return super().action(a)

    def _entry_ready(self,snap,c):
        initial=next((t for t in c['tickets'].values() if t['role']=='initial'),None)
        return bool(initial and initial['closed'] and number(c['recovery'])>0 and not snap['positions'] and not snap['orders'] and not self._unresolved() and not c.get('v3_link') and not c.get('v3_request') and not c.get('v3_rearm') and not c.get('v3_relink'))

    def _request_entry(self,a):
        if set(a)!={'type','cycle_id','order_type','timing','side','count'}:raise ActionRejected('Неверные поля входа V3')
        snap,c=self._context(a);count=self._count(a['count'])
        if not self._entry_ready(snap,c):raise ActionRejected('Дождитесь завершения всей корзины и сверки ордеров')
        if a['order_type'] not in ('STOP','MARKET') or a['timing'] not in ('now','current','next') or a['side'] not in ('BUY','SELL') or (a['order_type']=='STOP' and a['timing']=='now'):
            raise ActionRejected('Неверный тип или момент входа')
        request=dict(order_type=a['order_type'],timing=a['timing'],side=a['side'],count=count,distance=self.state['v3_settings']['entry_distance'])
        if a['timing']=='now':self._arm_v3(snap,request,a['side']);return
        request['bar_id']=int(number(snap['now'])//60)*60+(60 if a['timing']=='next' else 0)
        c['v3_request']=request;self.state['phase']='V3_WAIT_CANDLE';self.save('v3_schedule_created')

    def _recovery_tps(self,cfg,spec,side,entries,sizes,debt):
        # An owner's single filled part keeps TP1 even when it cannot repay
        # the whole ledger. The unpaid balance waits for another manual entry.
        if self._v3() and len(entries)==1:debt='0'
        return super()._recovery_tps(cfg,spec,side,entries,sizes,debt)

    def _plans_v3(self,snap,request,side,level=None,debt=None):
        c=self.state['cycle'];cfg=BasketSettings.load(c['cfg']);spec=Spec.load(snap['spec']);cfg.validate(spec)
        count=self._count(request['count']);size=D('.01')
        if size<spec.volume_min or size>spec.volume_max or size%spec.volume_step:raise ActionRejected('Брокер не поддерживает отдельные части 0.01')
        sign=1 if side=='BUY' else -1
        entry=spec.price(level if level is not None else snap['quote']['ask' if side=='BUY' else 'bid'])
        if request['order_type']=='STOP':
            quote=number(snap['quote']['ask' if side=='BUY' else 'bid'])
            if sign*(entry-quote)<=0 or sign*(entry-quote)<spec.point*spec.stops_level:raise ActionRejected('STOP пересечён или слишком близко; MARKET не создаётся')
        sl=spec.price(entry-sign*number(cfg.RECOVERY_SL_DISTANCE))
        tps=self._recovery_tps(cfg,spec,side,[entry]*count,[size]*count,c['recovery'] if debt is None else debt)
        result=[]
        for i,tp in enumerate(tps):
            spec.protection(side,entry,sl,number(tp),snap['quote'] if request['order_type']=='MARKET' else None)
            result.append(dict(kind=request['order_type'],side=side,price=str(entry),trigger=str(entry),sl=str(sl),tp=tp,size=str(size),sl_distance=cfg.RECOVERY_SL_DISTANCE,crossed_policy='reject',basket_index=i))
        return result

    def _arm_v3(self,snap,request,side,level=None,debt=None,*,persist=True):
        c=self.state['cycle']
        try:plans=self._plans_v3(snap,request,side,level,debt)
        except (ValueError,ArithmeticError) as exc:raise ActionRejected(str(exc),public_message='Вход V3 недопустим по цене, объёму или TP-сетке') from exc
        group=dict(id=uuid.uuid4().hex,role='v3',levels={side:plans[0]['price']},commands=[],activated=None,ladder_applied=False,v3_count=request['count'])
        for plan in plans:
            cid=self._intent(**plan);group['commands'].append(cid);c['entries'].append(cid)
        c['groups'].append(group);c['round']+=1;self._on_group_armed(group)
        self.state['phase']='RECOVERY_PENDING'
        if persist:self.save('v3_entry_prepared')
        return group

    def _source(self,snap,c,expected):
        active=[t for t in c['tickets'].values() if not t['closed']]
        if not active or len({t['group'] for t in active})!=1 or sorted(t['id'] for t in active)!=sorted(expected) or sorted(p['id'] for p in snap['positions'])!=sorted(expected):raise ActionRejected('Состав текущей корзины изменился')
        g=next(g for g in c['groups'] if g['id']==active[0]['group'])
        source=next((t for t in active if t['index']==0),None)
        if not source:raise ActionRejected('Первая ступень уже закрыта; новый SL-якорь не выбран')
        p=next(p for p in snap['positions'] if p['id']==source['id'])
        return source,g,p

    def _request_link(self,a):
        if set(a)!={'type','cycle_id','count','expected_positions'} or not isinstance(a['expected_positions'],list):raise ActionRejected('Неверные поля связанных триггеров')
        snap,c=self._context(a);self._count(a['count'])
        if snap['orders'] or self._unresolved() or c.get('v3_link') or c.get('v3_request'):raise ActionRejected('Сначала сверка прежних ордеров')
        source,g,p=self._source(snap,c,a['expected_positions'])
        side='SELL' if source['side']=='BUY' else 'BUY'
        # Include the projected source basket loss only for provisional pending TP.
        # Confirmed actual ledger remains untouched and is recalculated after fills.
        net=number(c['realized'])+sum((number(self.broker.profit(t['side'],number(t['size']),number(t['entry']),number(p['sl']))) for t in c['tickets'].values() if t['group']==g['id'] and not t['closed']),D(0))
        group=self._arm_v3(snap,dict(order_type='STOP',count=a['count']),side,p['sl'],max(D(0),-net),persist=False)
        group['linked_source']=g['id']
        c['v3_link']=dict(source_position=source['id'],source_group=g['id'],group=group['id'],ids=group['commands'][:],count=a['count'],side=side,price=p['sl'],cancel=False)
        self.save('v3_link_created')

    def _adjust_sl(self,a):
        if set(a)!={'type','cycle_id','distance','expected_positions'} or not isinstance(a['expected_positions'],list):raise ActionRejected('Неверные поля SL')
        snap,c=self._context(a);active=[t for t in c['tickets'].values() if not t['closed']]
        if sorted(a['expected_positions'])!=sorted(p['id'] for p in snap['positions']) or not active or len({t['group'] for t in active})!=1:raise ActionRejected('Состав корзины изменился')
        spec=Spec.load(snap['spec'])
        try:
            distance=spec.price(a['distance']);key='INITIAL_SL_DISTANCE' if active[0]['role']=='initial' else 'RECOVERY_SL_DISTANCE'
            if distance<=0 or (distance-number(c['cfg'][key]))%D('.10'):raise ValueError()
            updates=[]
            for t in active:
                p=next(p for p in snap['positions'] if p['id']==t['id']);sl=spec.price(number(t['entry'])-(1 if t['side']=='BUY' else -1)*distance)
                spec.protection(t['side'],number(t['entry']),sl,number(p['tp']),snap['quote'],modify=True)
                updates.append(dict(position_id=t['id'],sl=str(sl),tp=p['tp']))
        except (ValueError,ArithmeticError,StopIteration):raise ActionRejected('Расстояние SL недопустимо; шаг 0.10, проверки tick/stops/freeze обязательны')
        cfg=deepcopy(c['cfg']);cfg[key]=str(distance)
        edit=dict(stage='CANCEL',settings=cfg,levels=None,updates=updates,cancel_ids=[],groups=[],fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],expected_protection={p['id']:{k:p[k] for k in ('sl','tp')} for p in snap['positions']})
        if c.get('v3_link') and not c['tickets'][c['v3_link']['source_position']]['closed']:
            c['v3_edit']={'prepared_edit':edit};c['v3_link']['replace']=True
        elif self._blocking_protection_orders(snap) or any(cmd['status'] not in TERMINAL and cid not in (c.get('v3_link') or {}).get('ids',[]) for cid,cmd in self.state['commands'].items()):raise ActionRejected('Сначала сверка ордеров')
        else:c['edit']=edit
        self.save('v3_sl_adjust_requested')

    def edit(self,**kwargs):
        c=self.state.get('cycle') or {}
        if self._v3() and kwargs.get('levels') is not None and c.get('tickets'):
            raise ActionRejected('Диапазоны V3 не используются; отмените ожидающий вход и выберите новый')
        if self._v3() and c.get('v3_link'):
            snap,_=self._context({'cycle_id':c['id']})
            try:
                if kwargs.get('settings') is not None:BasketSettings.load(kwargs['settings']).validate(Spec.load(snap['spec']))
                protection=kwargs.get('protection')
                if protection is not None:
                    p=next(p for p in snap['positions'] if p['id']==protection['position_id'])
                    if any(number(p[k])!=number(protection['old'][k]) for k in ('sl','tp')):raise ValueError()
                    spec=Spec.load(snap['spec'])
                    spec.protection(p['side'],number(p['entry']),spec.price(protection['new']['sl']),spec.price(protection['new']['tp']),snap['quote'],modify=True)
            except (ValueError,KeyError,TypeError,StopIteration,ArithmeticError):raise ActionRejected('Правка недопустима или защита изменилась; триггеры сохранены')
            if c['tickets'][c['v3_link']['source_position']]['closed']:
                protection=kwargs.get('protection');snap=self._guard(entry=True)
                if kwargs.get('settings') is not None:return super().edit(**kwargs)
                if not protection:raise ActionRejected('Нужна правка защиты или настроек')
                try:
                    pid=protection['position_id'];ticket=c['tickets'][pid]
                    original=next(p for p in snap['positions'] if p['id']==pid)
                    if any(number(original[k])!=number(protection['old'][k]) for k in ('sl','tp')):raise ValueError()
                    if number(protection['new']['tp'])!=number(protection['old']['tp']):
                        return self._edit_dragged_tp(protection)
                    spec=Spec.load(snap['spec']);sl=spec.price(protection['new']['sl']);updates=[]
                    for t in c['tickets'].values():
                        if t['group']!=ticket['group'] or t['closed']:continue
                        p=next(p for p in snap['positions'] if p['id']==t['id'])
                        spec.protection(t['side'],number(t['entry']),sl,number(p['tp']),snap['quote'],modify=True)
                        updates.append(dict(position_id=t['id'],sl=str(sl),tp=p['tp']))
                except (KeyError,ValueError,TypeError,StopIteration,ArithmeticError):raise ActionRejected('Защита изменилась или уровень SL недопустим')
                c['edit']=dict(stage='CANCEL',settings=deepcopy(c['cfg']),levels=None,updates=updates,cancel_ids=[],groups=[],fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],expected_protection={p['id']:{k:p[k] for k in ('sl','tp')} for p in snap['positions']})
                self.save('v3_frozen_link_protection_edit');return
            c['v3_edit']={'kwargs':deepcopy(kwargs)};c['v3_link']['replace']=True
            self.save('v3_link_edit_requested');return
        if self._v3() and c and not c.get('v3_link'):
            snap=self._guard(entry=True)
            pending=[g for g in c['groups'] if g['role']=='v3' and self._pending(snap,g['commands'])]
            if pending:
                if snap['positions'] or len(pending)!=1:raise ActionRejected('Сначала сверка частичных исполнений V3')
                g=pending[0];side=next(iter(g['levels']))
                rearm=dict(order_type='STOP',count=g['v3_count'],side=side,level=g['levels'][side])
                self._pending_v3_rearm=rearm
                try:return super().edit(**kwargs)
                finally:self._pending_v3_rearm=None
        return super().edit(**kwargs)

    def _prepare_edit_extra(self,e):
        super()._prepare_edit_extra(e)
        if getattr(self,'_pending_v3_rearm',None):e['v3_rearm']=deepcopy(self._pending_v3_rearm)

    def _commit_edit_extra(self,e):
        super()._commit_edit_extra(e)
        if e.get('v3_rearm'):self.state['cycle']['v3_rearm']=deepcopy(e['v3_rearm'])

    def _preserve_pending_edit_group(self,group):
        c=self.state.get('cycle') or {};link=c.get('v3_link')
        return bool(self._v3() and link and group['id']==link['group'] and c['tickets'][link['source_position']]['closed'])

    def _preserve_pending_tp_group(self,group):
        return self._v3() and group['role']=='v3'

    def _protection_busy(self,snap):
        if not self._v3():return super()._protection_busy(snap)
        c=self.state.get('cycle') or {};link=c.get('v3_link') or {}
        if c.get('v3_edit') or link.get('cancel') or link.get('replace'):return True
        if self._blocking_protection_orders(snap):return True
        return any(cmd['status'] not in TERMINAL and cid not in link.get('ids',[]) for cid,cmd in self.state['commands'].items())

    def _step(self,snap):
        if not self._v3():return
        c=self.state['cycle']
        if not self._observe(snap):return
        if c.get('manual'):
            self._cancel(snap,c['entries']);return
        if self._advance_link(snap):return
        if c.get('edit'):
            if self._advance_edit(snap):return
        if c.get('v3_rearm') and not c.get('edit'):
            request=c.pop('v3_rearm')
            if self.state['stop'] or self.state.get('time_block'):
                self.save('v3_pending_replacement_stopped');return
            try:self._arm_v3(snap,request,request['side'],request['level'])
            except ActionRejected:self._halt(snap,'V3_PENDING_REPLACEMENT_REQUIRES_REVIEW')
            return
        if c.get('v3_relink') and not c.get('edit'):
            request=c.pop('v3_relink');self.save('v3_relink_consumed')
            try:self._request_link(dict(type='v3_link',cycle_id=c['id'],count=request['count'],expected_positions=sorted(p['id'] for p in snap['positions'])))
            except ActionRejected:self._halt(snap,'V3_LINK_REPLACEMENT_REQUIRES_REVIEW')
            return
        for g in c['groups']:
            if self._advance_actual_ladder(snap,g):return
        if self._stage_first_tp_if_ready(snap):return
        for g in c['groups']:
            if g.get('entry_sl_once') and self._advance_entry_sl_once(snap,g):return
        initial=next((t for t in c['tickets'].values() if t['role']=='initial'),None)
        if initial is None or (not initial['closed'] and not any(t['role']!='initial' for t in c['tickets'].values())):
            return super()._step(snap)
        active=[t for t in c['tickets'].values() if not t['closed']]
        if self.state['stop'] or self.state.get('time_block'):
            c.pop('v3_request',None);self._cancel(snap,c['entries'])
        if any(cmd['status']=='REJECTED' and not cmd.get('edit_superseded') for cmd in self.state['commands'].values() if cmd.get('cycle_id')==c['id']):
            self._halt(snap,'V3_PARTIAL_OR_REJECTED_BATCH');return
        if active:
            self.state['phase']='RECOVERY_POSITION'
            for g in c['groups']:
                ts=[t for t in active if t['group']==g['id']]
                if ts and g['role']!='initial':
                    g['activated']=ts[0]['side']
                    if any(self.state['commands'][cid]['status']=='CANCELLED' for cid in g['commands']):self._halt(snap,'V3_PARTIAL_BATCH_CANCELLED');return
                    source_tickets=[t for t in c['tickets'].values() if t['group']==g.get('linked_source')]
                    anchor=next((t for t in source_tickets if t['index']==0),None)
                    # Broker fills can precede source exit evidence. Keep native
                    # provisional protection until the loss is actually booked.
                    if anchor and (not anchor['closed'] or (anchor.get('reason')=='SL' and any(not t['closed'] for t in source_tickets))):continue
                    self._live_ladder(snap,g)
            return
        if c.get('v3_link') and snap['orders']:
            self.state['phase']='RECOVERY_PENDING';return
        if number(c['recovery'])<=0:
            c.pop('v3_request',None);self._finish(snap);return
        if snap['orders'] or self._unresolved():self.state['phase']='RECOVERY_PENDING';return
        for g in c['groups']:g['retired']=True
        if self.state['stop'] or self.state.get('time_block'):
            self.state['phase']='PAUSED_CYCLE';self.save('v3_wait_paused');return
        request=c.get('v3_request')
        if request:
            bar=snap['candle'];target=request['bar_id']
            if number(snap['now'])<target+60:self.state['phase']='V3_WAIT_CANDLE';return
            if bar['id']!=target or not self._fresh_closed_candle(snap):
                c.pop('v3_request');self.notify('Свеча V3 пропущена или данные устарели. Вход отменён, выберите новую свечу.');self.save('v3_missed_candle');return
            c.pop('v3_request');self.save('v3_schedule_consumed')
            direction='BUY' if number(bar['close'])>number(bar['open']) else 'SELL' if number(bar['close'])<number(bar['open']) else None
            if direction is None:self.notify('Doji: вход V3 не выставлен.');self.state['phase']='V3_WAIT_ENTRY';self.save('v3_doji');return
            level=str(number(bar['close'])+(1 if direction=='BUY' else -1)*number(request['distance'])) if request['order_type']=='STOP' else None
            try:self._arm_v3(snap,request,direction,level)
            except ActionRejected:self.notify('Выбранный вход V3 недопустим. Выберите новую команду.');self.state['phase']='V3_WAIT_ENTRY';self.save('v3_entry_invalid')
            return
        self.state['phase']='V3_WAIT_ENTRY';self.save('v3_wait_owner')

    def _blocking_protection_orders(self,snap):
        if not self._v3():return super()._blocking_protection_orders(snap)
        link=(self.state.get('cycle') or {}).get('v3_link')
        if not link:return bool(snap['orders'])
        known={o['id'] for o in self._pending(snap,link['ids'])}
        return any(o['id'] not in known for o in snap['orders'])

    def _advance_link(self,snap):
        c=self.state['cycle'];link=c.get('v3_link')
        if not link:return False
        source=c['tickets'][link['source_position']];group=next(g for g in c['groups'] if g['id']==link['group'])
        linked=[t for t in c['tickets'].values() if t['group']==group['id']]
        source_live=[t for t in c['tickets'].values() if t['group']==link['source_group'] and not t['closed']]
        if linked:
            # Never claim repayment while the source loss is still unaccounted.
            # A linked basket may coexist with old remaining tickets. Keep
            # their protections separate and never infer the source exit.
            if source.get('closed') and source.get('reason') not in ('SL','TP'):
                self._halt(snap,'V3_SOURCE_EXIT_UNCONFIRMED');return True
            if link.get('cancel') or link.get('replace'):
                self._cancel(snap,link['ids'])
                if self._pending(snap,link['ids']) or not self._resolved(link['ids']):return True
                if len(linked)!=link['count']:
                    # Cancellation only affects unfilled remainders. Adopt the
                    # proven filled subset as its own protected basket.
                    filled=sorted(linked,key=lambda t:t['index'])
                    group['commands']=[cid for cid in group['commands'] if self.state['commands'][cid]['status']!='CANCELLED']
                    for index,t in enumerate(filled):t['index']=index
                    link['count']=len(filled);group['v3_count']=len(filled)
                    self.save('v3_partial_cancel_adopted')
            if len(linked)<link['count']:
                # Retain cancellation ownership and protection-order identity
                # through asynchronous partial fills. Never duplicate a batch.
                return False
            c.pop('v3_link');c.pop('v3_edit',None);self.save('v3_link_fills_adopted');return False
        source_tickets=[t for t in c['tickets'].values() if t['group']==link['source_group']]
        if not source_live and all(t.get('reason')=='TP' for t in source_tickets):link['cancel']=True
        # TP1 closes the anchor, not the rest of the basket. Keep last pending
        # level frozen until the whole source basket is closed or owner cancels.
        if self.state['stop'] or self.state.get('time_block'):link['cancel']=True
        p=next((p for p in snap['positions'] if p['id']==source['id']),None)
        if p and number(p['sl'])!=number(link['price']):link['replace']=True
        if not link.get('cancel') and not link.get('replace'):return False
        self._cancel(snap,link['ids'])
        if self._pending(snap,link['ids']) or not self._resolved(link['ids']) or self._uncertain():return True
        group['retired']=True;c.pop('v3_link')
        edit=c.pop('v3_edit',None)
        if link.get('replace') and not link.get('cancel') and not source['closed']:
            c['v3_relink']={'count':link['count']}
            if edit:
                if 'prepared_edit' in edit:c['edit']=edit['prepared_edit']
                else:
                    try:super().edit(**edit['kwargs'])
                    except ActionRejected:self._halt(snap,'V3_LINK_EDIT_REJECTED');return True
        self.save('v3_link_cancel_confirmed');return True

    def v3_view(self,snap):
        c=self.state.get('cycle') or {};active=[t for t in c.get('tickets',{}).values() if not t['closed']]
        blocked=bool(not snap.get('spec') or not self._v3() or self.state['stop'] or self.state.get('time_block') or self.state.get('migration_block') or c.get('manual') or c.get('edit') or c.get('v3_edit') or self._uncertain() or getattr(self.broker,'read_only',False))
        controls=dict(can_enter=False,can_link=False,can_cancel_link=bool(c.get('v3_link')),can_adjust_sl=False,can_own_entry_sl=False,reason='Дождитесь сверки или подходящего этапа V3')
        if c and not blocked:
            controls.update(can_enter=self._entry_ready(snap,c),can_link=bool(active and len({t['group'] for t in active})==1 and any(t['index']==0 for t in active) and not snap.get('orders') and not self._unresolved() and not c.get('v3_link')),can_adjust_sl=bool(active and len({t['group'] for t in active})==1),can_own_entry_sl=bool(active and len({t['group'] for t in active})==1 and not self._protection_busy(snap)))
        cfg=c.get('cfg',self.state['basket_settings']);key='INITIAL_SL_DISTANCE' if active and active[0]['role']=='initial' else 'RECOVERY_SL_DISTANCE'
        return dict(recovery_mode=c.get('recovery_mode',self.state['recovery_mode']),v3_settings=deepcopy(self.state['v3_settings']),v3_request=deepcopy(c.get('v3_request')),v3_link=deepcopy(c.get('v3_link')),v3_controls=controls,v3_sl_distance=cfg[key])
