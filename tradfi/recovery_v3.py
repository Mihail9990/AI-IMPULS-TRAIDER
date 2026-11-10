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
from .recovery_profiles import defaults,validate_settings,selected_snapshot,validate_snapshot,exact_targets


class ControlledRecoveryController(LatestCloseBasketController):
    def __init__(self,store,broker,**kwargs):
        # Direct construction must enforce the same persisted clock contract as
        # runtime startup, before inherited code can save/reconcile any state.
        from .time_profile import BYBIT_PROFILE
        config=getattr(broker,'config',None)
        if isinstance(config,dict) and config.get('time_profile')==BYBIT_PROFILE:
            from .market_time_state import bind_market_time_state
            bind_market_time_state(store,config)
        super().__init__(store,broker,**kwargs)
        self.state.setdefault('v3_settings',dict(entry_distance='1.00',count_presets=[4,6,10]))
        self.state.setdefault('recovery_profiles',defaults(self.state['basket_settings']['RECOVERY_SL_DISTANCE']))
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

    def _validate_cfg(self,cfg,spec):
        # V3 recovery volumes and distances belong to profiles, not archived
        # TICKET_SIZE/EXPANSION_DISTANCE fields. Initial candle rules stay exact.
        if not self._v3():return super()._validate_cfg(cfg,spec)
        size=number(cfg.INITIAL_SIZE)
        if not spec.volume_min<=size<=spec.volume_max or size%spec.volume_step:
            raise ValueError('INITIAL_SIZE: min/step/max брокера; округления нет')
        for key in ('INITIAL_ENTRY_DISTANCE','INITIAL_SL_DISTANCE','INITIAL_TP_DISTANCE'):
            distance=spec.price(getattr(cfg,key))
            if distance<spec.point*spec.stops_level:raise ValueError(key+': stops брокера')

    def _validate_start_ladder(self,spec):
        if not self._v3():return super()._validate_start_ladder(spec)
        try:selected_snapshot(self.state['recovery_profiles'],spec)
        except (ValueError,KeyError,StopIteration,TypeError,ArithmeticError) as exc:
            raise ActionRejected(str(exc),public_message='Выбранный профиль RECOVERY не соответствует брокеру; проверьте объёмы, TP и SL. Округления нет.') from exc

    def _profile_request(self,snap,a):
        try:profile=selected_snapshot(self.state['recovery_profiles'],Spec.load(snap['spec']))
        except (ValueError,KeyError,StopIteration,TypeError,ArithmeticError) as exc:
            raise ActionRejected(str(exc),public_message='Профиль недопустим по min/step/max объёма или tick/stops брокера. Округления нет.') from exc
        if 'count' in a and self._count(a['count'])!=len(profile['stages']):
            raise ActionRejected('Количество уже определяется выбранным профилем; обновите панель')
        return profile

    def _save_profiles(self,a):
        if set(a)!={'type','cycle_id','selected_profile_id','profiles','recovery_sl_distance'} or a['cycle_id']!=(self.state.get('cycle') or {}).get('id'):
            raise ActionRejected('Обновите настройки профилей RECOVERY')
        snap=self._guard(entry=True)
        try:settings=validate_settings({k:a[k] for k in ('selected_profile_id','profiles','recovery_sl_distance')},Spec.load(snap['spec']))
        except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
            raise ActionRejected(str(exc),public_message=str(exc)) from exc
        # Only future requests use the new settings. No active cfg, group,
        # command, scheduled request, protection or debt is rewritten here.
        self.state['recovery_profiles']=settings;self.save('recovery_profiles_saved')

    def _select_profile(self,a):
        if set(a)!={'type','cycle_id','profile_id'} or a['cycle_id']!=(self.state.get('cycle') or {}).get('id'):
            raise ActionRejected('Обновите выбранный профиль RECOVERY')
        snap=self._guard(entry=True);settings=deepcopy(self.state['recovery_profiles']);settings['selected_profile_id']=a['profile_id']
        try:selected_snapshot(settings,Spec.load(snap['spec']))
        except (ValueError,KeyError,StopIteration,TypeError,ArithmeticError) as exc:
            raise ActionRejected(str(exc),public_message='Выбранный профиль отсутствует или недопустим для брокера') from exc
        self.state['recovery_profiles']=settings;self.save('recovery_profile_selected')

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
        if kind=='recovery_profiles':return self._save_profiles(a)
        if kind=='recovery_profile_select':return self._select_profile(a)
        if kind=='v3_settings':
            if set(a) not in ({'type','cycle_id','entry_distance'},{'type','cycle_id','entry_distance','count_presets'}) or a['cycle_id']!=(self.state.get('cycle') or {}).get('id'):raise ActionRejected('Обновите настройки V3')
            snap=self._guard(entry=True);spec=Spec.load(snap['spec'])
            try:
                distance=spec.price(a['entry_distance'])
                if distance<=0 or distance<spec.point*spec.stops_level:raise ValueError()
                presets=a.get('count_presets',self.state['v3_settings']['count_presets'])
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
        if kind=='v3_cancel_entry':return self._cancel_v3_entry(a)
        if kind=='v3_cancel_request':
            if set(a)!={'type','cycle_id'}:raise ActionRejected('Неверные поля отмены')
            self._guard();c=self.state.get('cycle')
            if not c or a['cycle_id']!=c['id'] or not c.get('v3_request'):raise ActionRejected('Нет запланированного входа')
            c.pop('v3_request');self.save('v3_schedule_cancelled');return
        if kind=='v3_adjust_sl':return self._adjust_sl(a)
        return super().action(a)

    def _cancel_entry_groups(self,snap,c):
        # Linked-to-SL batches retain their separate cancellation owner.
        linked=(c.get('v3_link') or {}).get('group')
        return [g for g in c.get('groups',[]) if g['role']=='v3' and not g.get('linked_source')
                and g['id']!=linked and not g.get('retired')
                and (g.get('cancel_entry') or self._pending(snap,g['commands'])
                     or any(self.state['commands'][cid]['kind']=='STOP' and self.state['commands'][cid]['status']=='READY' for cid in g['commands']))]

    def _cancel_v3_entry(self,a):
        if set(a)!={'type','cycle_id'}:raise ActionRejected('Неверные поля отмены')
        snap=self._guard();c=self.state.get('cycle')
        if not self._v3() or not c or a['cycle_id']!=c['id']:raise ActionRejected('Цикл изменился; обновите панель')
        if c.get('edit') or c.get('v3_edit') or c.get('v3_rearm'):raise ActionRejected('Сначала дождитесь завершения правки')
        groups=self._cancel_entry_groups(snap,c)
        if not groups:raise ActionRejected('Нет ожидающих ручных триггеров')
        for g in groups:g['cancel_entry']=True
        # Persist ownership before sending cancellation; a restart must finish it.
        self.save('v3_entry_cancel_requested')
        for g in groups:self._cancel(snap,g['commands'])

    def _advance_entry_cancel(self,snap):
        c=self.state['cycle'];waiting=False
        for g in c['groups']:
            if not g.get('cancel_entry'):continue
            ids=g['commands'];self._cancel(snap,ids)
            if self._pending(snap,ids) or not self._resolved(ids) or self._uncertain():
                waiting=True;continue
            # Fills win cancellation races. Keep their protections, original
            # profile stage indices and ledger; discard only proven cancellations.
            g['commands']=[cid for cid in ids if self.state['commands'][cid]['status']!='CANCELLED']
            g['v3_count']=len(g['commands']);g.pop('cancel_entry')
            if not g['commands']:g['retired']=True
            self.save('v3_entry_cancel_confirmed')
        return waiting

    def _entry_ready(self,snap,c):
        initial=next((t for t in c['tickets'].values() if t['role']=='initial'),None)
        return bool(initial and initial['closed'] and number(c['recovery'])>0 and not snap['positions'] and not snap['orders'] and not self._unresolved() and not c.get('v3_link') and not c.get('v3_request') and not c.get('v3_rearm') and not c.get('v3_relink'))

    def _request_entry(self,a):
        if set(a) not in ({'type','cycle_id','order_type','timing','side'},{'type','cycle_id','order_type','timing','side','count'}):raise ActionRejected('Неверные поля входа V3')
        snap,c=self._context(a);profile=self._profile_request(snap,a);count=len(profile['stages'])
        if not self._entry_ready(snap,c):raise ActionRejected('Дождитесь завершения всей корзины и сверки ордеров')
        if a['order_type'] not in ('STOP','MARKET') or a['timing'] not in ('now','current','next') or a['side'] not in ('BUY','SELL') or (a['order_type']=='STOP' and a['timing']=='now'):
            raise ActionRejected('Неверный тип или момент входа')
        request=dict(order_type=a['order_type'],timing=a['timing'],side=a['side'],count=count,distance=self.state['v3_settings']['entry_distance'],recovery_profile=profile)
        if a['timing']=='now':self._arm_v3(snap,request,a['side']);return
        request['bar_id']=int(number(snap['now'])//60)*60+(60 if a['timing']=='next' else 0)
        c['v3_request']=request;self.state['phase']='V3_WAIT_CANDLE';self.save('v3_schedule_created')

    def _recovery_tps(self,cfg,spec,side,entries,sizes,debt):
        # An owner's single filled part keeps TP1 even when it cannot repay
        # the whole ledger. The unpaid balance waits for another manual entry.
        if self._v3() and len(entries)==1:debt='0'
        return super()._recovery_tps(cfg,spec,side,entries,sizes,debt)

    def _plans_v3(self,snap,request,side,level=None,debt=None):
        c=self.state['cycle'];cfg=BasketSettings.load(c['cfg']);spec=Spec.load(snap['spec'])
        profile=request.get('recovery_profile')
        if profile:
            profile=validate_snapshot(profile,spec);count=len(profile['stages'])
            sizes=[number(stage['volume']) for stage in profile['stages']];sl_distance=profile['sl_distance']
        else:
            # Previously submitted V3 batches keep their pre-upgrade contract.
            count=self._count(request['count']);sizes=[D('.01')]*count;sl_distance=cfg.RECOVERY_SL_DISTANCE
            if sizes[0]<spec.volume_min or sizes[0]>spec.volume_max or sizes[0]%spec.volume_step:raise ActionRejected('Брокер не поддерживает отдельные части 0.01')
        sign=1 if side=='BUY' else -1
        entry=spec.price(level if level is not None else snap['quote']['ask' if side=='BUY' else 'bid'])
        if request['order_type']=='STOP':
            quote=number(snap['quote']['ask' if side=='BUY' else 'bid'])
            if sign*(entry-quote)<=0 or sign*(entry-quote)<spec.point*spec.stops_level:raise ActionRejected('STOP пересечён или слишком близко; MARKET не создаётся')
        sl=spec.price(entry-sign*number(sl_distance))
        tps=exact_targets(spec,profile,side,[dict(index=i,entry=str(entry),size=str(size)) for i,size in enumerate(sizes)]) if profile else self._recovery_tps(cfg,spec,side,[entry]*count,sizes,c['recovery'] if debt is None else debt)
        result=[]
        for i,(tp,size) in enumerate(zip(tps,sizes)):
            spec.protection(side,entry,sl,number(tp),snap['quote'] if request['order_type']=='MARKET' else None)
            result.append(dict(kind=request['order_type'],side=side,price=str(entry),trigger=str(entry),sl=str(sl),tp=tp,size=str(size),sl_distance=sl_distance,crossed_policy='reject',basket_index=i))
        return result

    def _group_recovery_tps(self,cfg,spec,g,tickets,debt):
        if g.get('recovery_profile'):
            return exact_targets(spec,g['recovery_profile'],g['activated'],tickets,g.get('manual_tp_distances'))
        return super()._group_recovery_tps(cfg,spec,g,tickets,debt)

    def _live_ladder(self,snap,g):
        if not g.get('recovery_profile'):return super()._live_ladder(snap,g)
        c=self.state['cycle'];tickets=sorted((t for t in c['tickets'].values() if t['group']==g['id']),key=lambda t:t['index'])
        anchored=set(g.get('profile_anchored_positions',[]))
        pending=[t for t in tickets if t['id'] not in anchored]
        if not pending:
            if not g['ladder_applied'] and len(tickets)==len(g['commands']):
                g['ladder_applied']=True;self.save('profile_fill_subset_confirmed')
            return
        if any(t['closed'] for t in pending):
            self._halt(snap,'ACTUAL_LADDER_GAP_EXIT_REQUIRES_REVIEW');return
        if g.get('actual_ladder_tx',{}).get('status')=='PENDING':return
        # Exact targets need neither the source's debt nor the rest of a linked
        # batch. Anchor every fully evidenced ticket while other STOPs wait.
        try:
            spec=Spec.load(snap['spec']);targets=[]
            tps=exact_targets(spec,g['recovery_profile'],g['activated'],pending,g.get('manual_tp_distances'))
            for t,tp in zip(pending,tps):
                live=next(p for p in snap['positions'] if p['id']==t['id'])
                if number(live['size'])!=number(t['size']):raise ValueError('Объём изменился')
                if number(live['tp'])!=number(tp):
                    spec.protection(t['side'],number(t['entry']),number(live['sl']),number(tp),snap['quote'],modify=True)
                targets.append(dict(position_id=t['id'],sl=live['sl'],tp=tp,command_id=None))
        except (ValueError,ArithmeticError,StopIteration,KeyError):
            self._halt(snap,'ACTUAL_FILL_LADDER_REQUIRES_REVIEW');return
        for u in targets:
            live=next(p for p in snap['positions'] if p['id']==u['position_id'])
            if number(live['tp'])!=number(u['tp']):
                u['command_id']=self._intent('MODIFY',position_id=u['position_id'],sl=u['sl'],tp=u['tp'],purpose='ANCHOR')
        g['actual_ladder_tx']=dict(status='PENDING',targets=targets)
        g['profile_anchored_positions']=sorted(anchored|{t['id'] for t in pending})
        selected=[cid for cid in g['commands'] if self.state['commands'][cid]['side']==g['activated']]
        g['ladder_applied']=len(tickets)==len(selected)
        self.save('profile_actual_fill_targets_prepared')

    def _group_indexes_valid(self,g,tickets):
        if not g.get('recovery_profile'):return super()._group_indexes_valid(g,tickets)
        commands={cid:self.state['commands'][cid] for cid in g['commands']}
        return (len(tickets)==len(commands) and len({t['index'] for t in tickets})==len(tickets)
                and all(t['command_id'] in commands and t['index']==commands[t['command_id']]['basket_index']
                        and 0<=t['index']<len(g['recovery_profile']['stages']) for t in tickets))

    def _arm_v3(self,snap,request,side,level=None,debt=None,*,persist=True):
        c=self.state['cycle']
        try:plans=self._plans_v3(snap,request,side,level,debt)
        except (ValueError,ArithmeticError) as exc:raise ActionRejected(str(exc),public_message='Вход V3 недопустим по цене, объёму или TP-сетке') from exc
        group=dict(id=uuid.uuid4().hex,role='v3',levels={side:plans[0]['price']},commands=[],activated=None,ladder_applied=False,v3_count=len(plans))
        if request.get('recovery_profile'):group['recovery_profile']=deepcopy(request['recovery_profile'])
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

    def _request_link(self,a,*,recovery_profile=None,legacy=False):
        if set(a) not in ({'type','cycle_id','expected_positions'},{'type','cycle_id','count','expected_positions'}) or not isinstance(a['expected_positions'],list):raise ActionRejected('Неверные поля связанных триггеров')
        snap,c=self._context(a)
        profile=None if legacy else deepcopy(recovery_profile) if recovery_profile is not None else self._profile_request(snap,a)
        count=self._count(a['count']) if legacy else len(profile['stages'])
        if snap['orders'] or self._unresolved() or c.get('v3_link') or c.get('v3_request'):raise ActionRejected('Сначала сверка прежних ордеров')
        source,g,p=self._source(snap,c,a['expected_positions'])
        side='SELL' if source['side']=='BUY' else 'BUY'
        # Include the projected source basket loss only for provisional pending TP.
        # Confirmed actual ledger remains untouched and is recalculated after fills.
        net=number(c['realized'])+sum((number(self.broker.profit(t['side'],number(t['size']),number(t['entry']),number(p['sl']))) for t in c['tickets'].values() if t['group']==g['id'] and not t['closed']),D(0))
        group=self._arm_v3(snap,dict(order_type='STOP',count=count,recovery_profile=profile),side,p['sl'],max(D(0),-net),persist=False)
        group['linked_source']=g['id']
        c['v3_link']=dict(source_position=source['id'],source_group=g['id'],group=group['id'],ids=group['commands'][:],count=count,side=side,price=p['sl'],cancel=False,recovery_profile=deepcopy(profile))
        self.save('v3_link_created')

    def _active_sl_distance(self,snap,c,active):
        cfg=c.get('cfg',self.state['basket_settings'])
        key='INITIAL_SL_DISTANCE' if active and active[0]['role']=='initial' else 'RECOVERY_SL_DISTANCE'
        if not active or len({t['group'] for t in active})!=1:return cfg[key]
        distances={abs(number(p['sl'])-number(p['entry'])) for p in snap.get('positions',[]) if p['id'] in {t['id'] for t in active}}
        if len(distances)==1:return str(next(iter(distances)))
        group=next(g for g in c['groups'] if g['id']==active[0]['group'])
        # Unequal actual fills may have a common native SL at different own-entry
        # distances. The displayed group baseline must remain usable for +/- .10.
        return group.get('v3_sl_distance',group.get('recovery_profile',{}).get('sl_distance',cfg[key]))

    def _adjust_sl(self,a):
        if set(a)!={'type','cycle_id','distance','expected_positions'} or not isinstance(a['expected_positions'],list):raise ActionRejected('Неверные поля SL')
        snap,c=self._context(a);active=[t for t in c['tickets'].values() if not t['closed']]
        if sorted(a['expected_positions'])!=sorted(p['id'] for p in snap['positions']) or not active or len({t['group'] for t in active})!=1:raise ActionRejected('Состав корзины изменился')
        spec=Spec.load(snap['spec'])
        try:
            distance=spec.price(a['distance']);key='INITIAL_SL_DISTANCE' if active[0]['role']=='initial' else 'RECOVERY_SL_DISTANCE'
            baseline=number(self._active_sl_distance(snap,c,active))
            if distance<=0 or (distance-baseline)%D('.10'):raise ValueError()
            updates=[]
            for t in active:
                p=next(p for p in snap['positions'] if p['id']==t['id']);sl=spec.price(number(t['entry'])-(1 if t['side']=='BUY' else -1)*distance)
                spec.protection(t['side'],number(t['entry']),sl,number(p['tp']),snap['quote'],modify=True)
                updates.append(dict(position_id=t['id'],sl=str(sl),tp=p['tp']))
        except (ValueError,ArithmeticError,StopIteration):raise ActionRejected('Расстояние SL недопустимо; шаг 0.10, проверки tick/stops/freeze обязательны')
        cfg=deepcopy(c['cfg']);cfg[key]=str(distance)
        edit=dict(stage='CANCEL',settings=cfg,levels=None,updates=updates,cancel_ids=[],groups=[],fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],expected_protection={p['id']:{k:p[k] for k in ('sl','tp')} for p in snap['positions']})
        edit.update(v3_sl_group=active[0]['group'],v3_sl_distance=str(distance))
        if c.get('v3_link') and not c['tickets'][c['v3_link']['source_position']]['closed']:
            c['v3_edit']={'prepared_edit':edit};c['v3_link']['replace']=True
        elif self._blocking_protection_orders(snap) or any(cmd['status'] not in TERMINAL and cid not in (c.get('v3_link') or {}).get('ids',[]) for cid,cmd in self.state['commands'].items()):raise ActionRejected('Сначала сверка ордеров')
        else:c['edit']=edit
        self.save('v3_sl_adjust_requested')

    def _edit_dragged_tp(self,protection):
        c=self.state.get('cycle') or {};ticket=c.get('tickets',{}).get(protection.get('position_id'))
        group=next((g for g in c.get('groups',[]) if ticket and g['id']==ticket['group']),None)
        if not group or not group.get('recovery_profile'):return super()._edit_dragged_tp(protection)
        snap=self._guard(entry=True)
        if ticket['closed'] or c.get('edit') or c.get('initial_cancel') or self._uncertain() or self._blocking_protection_orders(snap):
            raise ActionRejected('Сначала сверка билетов, ожидающих ордеров и предыдущей правки')
        tickets=[t for t in c['tickets'].values() if t['group']==group['id']]
        try:
            if not self._group_indexes_valid(group,tickets):raise ValueError('Состав ступеней изменился')
            if any(not isinstance(protection[k],dict) or set(protection[k])!={'sl','tp'} for k in ('old','new')):raise ValueError()
            live=next(p for p in snap['positions'] if p['id']==ticket['id'])
            if any(number(protection['old'][k])!=number(live[k]) for k in ('sl','tp')):raise ValueError()
            if number(protection['new']['sl'])!=number(live['sl']) or number(live['size'])!=number(ticket['size']):raise ValueError()
            spec=Spec.load(snap['spec']);tp=spec.price(protection['new']['tp'])
            distance=spec.price((1 if ticket['side']=='BUY' else -1)*(tp-number(ticket['entry'])))
            spec.protection(ticket['side'],number(ticket['entry']),number(live['sl']),tp,snap['quote'],modify=True)
        except (ValueError,ArithmeticError,KeyError,TypeError,StopIteration):
            raise ActionRejected('TP недопустим по фактическому входу, tick/stops/freeze или билет изменился. Другие ступени сохранены.')
        overrides=deepcopy(group.get('manual_tp_distances',{}));overrides[str(ticket['index'])]=str(distance)
        # An explicit chart edit changes just this ticket. The saved profile and
        # immutable origin snapshot are not edited, and debt cannot stretch peers.
        updates=[dict(position_id=ticket['id'],sl=live['sl'],tp=str(tp))]
        c['edit']=dict(stage='CANCEL',settings=deepcopy(c['cfg']),levels=None,updates=updates,cancel_ids=[],groups=[],
            fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],
            manual_tp_group=group['id'],manual_tp_distances=overrides,
            expected_protection={ticket['id']:{k:live[k] for k in ('sl','tp')}})
        self.save('profile_ticket_tp_edit_requested')

    def edit(self,**kwargs):
        c=self.state.get('cycle') or {}
        if self._v3() and kwargs.get('levels') is not None and c.get('tickets'):
            raise ActionRejected('Диапазоны V3 не используются; отмените ожидающий вход и выберите новый')
        if self._v3() and c.get('v3_link'):
            snap,_=self._context({'cycle_id':c['id']})
            try:
                if kwargs.get('settings') is not None:self._validate_cfg(BasketSettings.load(kwargs['settings']),Spec.load(snap['spec']))
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
                g=pending[0]
                if g.get('recovery_profile'):return super().edit(**kwargs)
                side=next(iter(g['levels']))
                rearm=dict(order_type='STOP',count=g['v3_count'],side=side,level=g['levels'][side])
                if g.get('recovery_profile'):
                    rearm['recovery_profile']=deepcopy(g['recovery_profile'])
                    if kwargs.get('settings') is not None and kwargs['settings']['RECOVERY_SL_DISTANCE']!=c['cfg']['RECOVERY_SL_DISTANCE']:
                        rearm['recovery_profile']['sl_distance']=kwargs['settings']['RECOVERY_SL_DISTANCE']
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
        if e.get('v3_sl_group'):
            group=next(g for g in self.state['cycle']['groups'] if g['id']==e['v3_sl_group'])
            group['v3_sl_distance']=e['v3_sl_distance']

    def _preserve_pending_edit_group(self,group):
        c=self.state.get('cycle') or {};link=c.get('v3_link')
        return bool(self._v3() and (group.get('recovery_profile') or (link and group['id']==link['group'] and c['tickets'][link['source_position']]['closed'])))

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
        if self._advance_entry_cancel(snap):return
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
            try:self._request_link(dict(type='v3_link',cycle_id=c['id'],count=request['count'],expected_positions=sorted(p['id'] for p in snap['positions'])),recovery_profile=request.get('recovery_profile'),legacy=not bool(request.get('recovery_profile')))
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
                    if not g.get('recovery_profile') and anchor and (not anchor['closed'] or (anchor.get('reason')=='SL' and any(not t['closed'] for t in source_tickets))):continue
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
                    if not group.get('recovery_profile'):
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
            if link.get('recovery_profile'):c['v3_relink']['recovery_profile']=deepcopy(link['recovery_profile'])
            if edit:
                if 'prepared_edit' in edit:c['edit']=edit['prepared_edit']
                else:
                    try:super().edit(**edit['kwargs'])
                    except ActionRejected:self._halt(snap,'V3_LINK_EDIT_REJECTED');return True
        self.save('v3_link_cancel_confirmed');return True

    def v3_view(self,snap):
        c=self.state.get('cycle') or {};active=[t for t in c.get('tickets',{}).values() if not t['closed']]
        blocked=bool(not snap.get('spec') or not self._v3() or self.state['stop'] or self.state.get('time_block') or self.state.get('migration_block') or c.get('manual') or c.get('edit') or c.get('v3_edit') or self._uncertain() or getattr(self.broker,'read_only',False))
        controls=dict(can_enter=False,can_link=False,can_cancel_link=bool(c.get('v3_link')),can_cancel_entry=bool(c and not getattr(self.broker,'read_only',False) and not c.get('edit') and not c.get('v3_edit') and not c.get('v3_rearm') and not c.get('manual') and not self.state.get('migration_block') and self.state['phase'] not in {'SAFE_PAUSE','MANUAL'} and self._cancel_entry_groups(snap,c)),can_adjust_sl=False,can_own_entry_sl=False,reason='Дождитесь сверки или подходящего этапа V3')
        if c and not blocked:
            controls.update(can_enter=self._entry_ready(snap,c),can_link=bool(active and len({t['group'] for t in active})==1 and any(t['index']==0 for t in active) and not snap.get('orders') and not self._unresolved() and not c.get('v3_link')),can_adjust_sl=bool(active and len({t['group'] for t in active})==1),can_own_entry_sl=bool(active and len({t['group'] for t in active})==1 and not self._protection_busy(snap)))
        cfg=c.get('cfg',self.state['basket_settings']);key='INITIAL_SL_DISTANCE' if active and active[0]['role']=='initial' else 'RECOVERY_SL_DISTANCE'
        profile_settings=deepcopy(self.state['recovery_profiles']);selected=selected_snapshot(profile_settings)
        current=[]
        for g in c.get('groups',[]):
            if g.get('retired') or g['role']=='initial':continue
            if not any(t['group']==g['id'] and not t['closed'] for t in c.get('tickets',{}).values()) and not any(self.state['commands'][cid]['status'] not in TERMINAL for cid in g['commands']):continue
            if g.get('recovery_profile'):current.append(dict(group_id=g['id'],**deepcopy(g['recovery_profile'])))
            else:current.append(dict(group_id=g['id'],id='legacy',name='Прежний стандарт V3',stages=[],total_volume=str(sum((number(self.state['commands'][cid]['size']) for cid in g['commands']),D(0))),sl_distance=cfg['RECOVERY_SL_DISTANCE']))
        sl_distance=self._active_sl_distance(snap,c,active)
        return dict(recovery_profiles=profile_settings,selected_recovery_profile=selected,current_recovery_profiles=current,recovery_mode=c.get('recovery_mode',self.state['recovery_mode']),v3_settings=deepcopy(self.state['v3_settings']),v3_request=deepcopy(c.get('v3_request')),v3_link=deepcopy(c.get('v3_link')),v3_controls=controls,v3_sl_distance=sl_distance)
