"""Single-owner candle entry plus independent hedged recovery tickets.

No recovery action delegates to the archived legacy/fixed cycle engines.
Broker SL and opposite STOP are not atomic: every overlap is reconciled or halted.
"""
from copy import deepcopy
from decimal import Decimal as D
import uuid
from .engine import Controller,TERMINAL
from .control import ActionRejected
from .settings import number
from .broker import Spec
from .basket_math import BasketSettings,ladder,expanded_levels,net_money

CONTRACT='ticket_basket_v1'

class BasketController(Controller):
    def __init__(self,store,broker,**kwargs):
        prior=store.load()
        super().__init__(store,broker,**kwargs)
        self.state.setdefault('basket_settings',BasketSettings().dump())
        self.state.setdefault('basket_carry','0')
        c=self.state.get('cycle')
        if (c and c.get('contract')!=CONTRACT) or self.state.get('fixed_recovery_carry'):
            self.state.setdefault('migration_legacy_snapshot',deepcopy(prior))
            self.state['migration_block']='Старый активный цикл/долг: новая стратегия не запускается. Сохраните базу и завершите сверку старой версией; автоматического переноса или списания нет.'
            self.state['phase']='MIGRATION_BLOCK';self.state['stop']=True
        elif prior and prior.get('cycle') is None and 'basket_settings' not in prior:
            # No old recovery parameters are interpreted as basket parameters.
            old=prior.get('settings',{})
            for key in ('INITIAL_SIZE','INITIAL_ENTRY_DISTANCE','INITIAL_TP_DISTANCE','CYCLE_COOLDOWN_SECONDS'):
                if key in old:self.state['basket_settings'][key]=old[key]
        if not self.state.get('migration_block'):
            self.state['operation_mode']='auto';self.state['modules']={'engine':'basket','recovery':{}}
        self.save('basket_controller_loaded')

    def _guard(self,entry=False):
        if self.state.get('migration_block'):raise ActionRejected(self.state['migration_block'])
        self._deadline_guard();self._time_guard()
        if getattr(self.broker,'read_only',False):raise ActionRejected('READ_ONLY: торговля отключена')
        if self.state['phase'] in {'SAFE_PAUSE','MANUAL'}:raise ActionRejected('Нужна ручная сверка; защита брокера сохранена')
        if entry and self.state.get('time_block'):raise ActionRejected('Время не подтверждено; новые входы запрещены')
        snap=self.snapshot()
        if not snap.get('hedging'):raise ActionRejected('Нужен hedging-счёт с независимыми билетами')
        if any(not x.get('owned') for x in snap['positions']+snap['orders']):raise ActionRejected('Есть неизвестные позиции/ордера')
        if entry and self.evidence_contract=='per_obligation_v1' and snap['quote'].get('utc_time') is None:raise ActionRejected('Нет свежей проверенной котировки')
        return snap

    def command(self,text,update_id=None):
        raw=text.strip().lower()
        if raw in {'/status','/settings'}:
            self.notify(str(self.state['basket_settings']) if raw=='/settings' else self.status());self.save('basket_status');return
        if self.state.get('migration_block'):raise ActionRejected(self.state['migration_block'])
        if raw=='/resume_time':return self._resume_time(update_id)
        if raw=='/reset_manual':return self._reset_manual(update_id)
        if raw=='/stop':
            self.state['stop']=True;self.state['stop_mode']='FINISH' if (self.state.get('cycle') or {}).get('tickets') else 'CANCEL'
            self._cancel_ready();self.save('basket_stop');return
        if raw not in {'/start','/start auto'}:raise ActionRejected('Доступны Старт, Стоп, настройки корзины и ручная сверка')
        snap=self._guard(entry=True)
        try:BasketSettings.load(self.state['basket_settings']).validate(Spec.load(snap['spec']))
        except (ValueError,ArithmeticError) as exc:raise ActionRejected(str(exc),public_message='Параметры не соответствуют min/step/max объёма или tick/stops брокера. Округления и расширения нет.') from exc
        if self.state.get('cycle'):
            if self.state['phase']!='PAUSED_CYCLE':raise ActionRejected('Цикл уже активен')
            if snap['positions'] or snap['orders'] or self._unresolved():raise ActionRejected('Сначала подтвердите отсутствие прежних обязательств')
            self.state['phase']='RECOVERY_WAIT' if self.state['cycle'].get('tickets') else 'INITIAL_WAIT'
            if not self.state['cycle']['tickets']:
                self.state['cycle']['wait_for_signal']=True
                for g in self.state['cycle']['groups']:g['retired']=True
        else:
            if snap['positions'] or snap['orders'] or self._unresolved():raise ActionRejected('Старт требует подтверждённого отсутствия позиций и ордеров')
            self.state['phase']='WAIT_CANDLE'
        self.state['account']=snap['account'];self.state['last_candle']=max(snap['candle']['id'],int(number(snap['now'])//60)*60-60)
        self.state['stop']=False;self.state['stop_mode']='';self.save('basket_start')

    def action(self,a):
        if not isinstance(a,dict):raise ActionRejected('Неверная команда')
        kind=a.get('type')
        if kind=='command' and set(a)=={'type','text'} and isinstance(a['text'],str):return self.command(a['text'])
        if kind=='start' and set(a)=={'type','mode'} and a['mode']=='auto':return self.command('/start')
        if kind=='cancel_initial' and set(a)=={'type','cycle_id','expected_orders'}:
            return self.cancel_initial(a['cycle_id'],a['expected_orders'])
        if kind in ('basket_settings','basket_levels','protection'):
            if a.get('cycle_id')!=(self.state.get('cycle') or {}).get('id'):raise ActionRejected('Цикл изменился; обновите предпросмотр')
            if self.state.get('cycle') and 'cycle_id' not in a:raise ActionRejected('Нужен ID активного цикла')
        if kind=='basket_settings' and set(a)<={'type','settings','cycle_id'} and 'settings' in a:return self.edit(settings=a['settings'])
        if kind=='basket_levels' and set(a)=={'type','levels','expected','cycle_id','expected_orders'}:return self.edit(levels=a['levels'],expected=a['expected'],expected_orders=a['expected_orders'])
        if kind=='protection' and set(a)=={'type','position_id','old','new','cycle_id'}:return self.edit(protection=a)
        raise ActionRejected('Неподдерживаемые поля команды')

    def cancel_initial(self,cycle_id,expected_orders):
        """Stage one candle-entry cancellation; fills always supersede this request."""
        snap=self._guard();c=self.state.get('cycle')
        if not c or cycle_id!=c['id']:raise ActionRejected('Цикл изменился; обновите панель')
        if c.get('tickets') or snap['positions']:raise ActionRejected('Начальный ордер уже исполнен; позиция сохраняет защиту')
        if c.get('edit') or c.get('initial_cancel') or self._uncertain():raise ActionRejected('Сначала дождитесь сверки предыдущей операции')
        if self.state['stop'] or self.state['phase'] not in {'INITIAL_PENDING','INITIAL_WAIT'}:raise ActionRejected('Нет активного начального триггера')
        if not isinstance(expected_orders,list) or not expected_orders or any(not isinstance(x,str) for x in expected_orders) or sorted(expected_orders)!=sorted(o['id'] for o in snap['orders']):raise ActionRejected('Начальные ордера изменились; обновите панель')
        ids=[cid for g in c['groups'] if g['role']=='initial' and not g.get('retired') for cid in g['commands']]
        if not ids or len(self._pending(snap,ids))!=len(snap['orders']):raise ActionRejected('Есть ордера другого назначения; отмена не принята')
        boundary=max(self.state['last_candle'],snap['candle']['id'],int(number(snap['now'])//60)*60-60)
        c['initial_cancel']={'ids':ids,'after_candle':boundary}
        self.save('basket_cancel_initial_requested')

    def _cancel_ready(self,ids=None):
        for cid,cmd in self.state['commands'].items():
            if (ids is None or cid in ids) and cmd['kind'] in {'STOP','MARKET'} and cmd['status']=='READY':cmd.update(status='CANCELLED',never_sent=True)

    def _pending(self,snap,ids):return [o for o in snap['orders'] if o.get('command_id') in ids or any(self.state['commands'][cid].get('order_id')==o['id'] for cid in ids)]
    def _resolved(self,ids):return all(self.state['commands'][cid]['status'] in TERMINAL for cid in ids)
    def _uncertain(self):return any(cmd['status'] not in TERMINAL|{'PENDING'} for cmd in self.state['commands'].values())
    def _cancel(self,snap,ids):
        self._cancel_ready(ids);self._cancel_pending(snap,ids);self.save('basket_cancel_pending');self.dispatch({'CANCEL'})

    def _new(self,snap):
        cfg=BasketSettings.load(self.state['basket_settings']);cfg.validate(Spec.load(snap['spec']))
        now=self.clock();bar=snap['candle']
        c=dict(id=uuid.uuid4().hex,contract=CONTRACT,engine='basket',currency=snap['spec']['currency'],settings=deepcopy(self.state['settings']),cfg=cfg.dump(),
            started_at_utc=now,deadline_utc=now+86400,max_duration_seconds=86400,last_observed_utc=now,
            branch='INITIAL',first=True,target='0',recovery=self.state['basket_carry'],realized=str(-number(self.state['basket_carry'])),
            groups=[],tickets={},entries=[],opened_ids=[],position=None,expanded=None,round=0,
            signal=dict(side='BUY' if number(bar['close'])>number(bar['open']) else 'SELL',close=bar['close'],id=bar['id']))
        self.state['cycle']=c;self.state['account']=snap['account'];self.state['last_candle']=bar['id'];self.state['phase']='INITIAL_WAIT';self._start_branch_clock(now);self.save('basket_new_cycle')

    def _recovery_tps(self,cfg,spec,side,entries,sizes,debt):
        return ladder(self.broker,spec,side,entries,sizes,debt,reserve=cfg.COST_RESERVE,multiplier=cfg.TP_STEP_MULTIPLIER)

    def _plans(self,snap,role,levels,cfg=None):
        c=self.state['cycle'];cfg=cfg or BasketSettings.load(c['cfg']);spec=Spec.load(snap['spec']);cfg.validate(spec)
        count=1 if role=='initial' else cfg.BASKET_COUNT;size=number(cfg.INITIAL_SIZE if role=='initial' else cfg.TICKET_SIZE)
        plans=[]
        debt=number(c['recovery'])
        if role=='reversal':
            initial=next(t for t in c['tickets'].values() if t['role']=='initial')
            if not initial['closed']:debt=max(D(0),-number(c['realized'])-number(self.broker.profit(initial['side'],number(initial['size']),number(initial['entry']),number(initial['sl']))))
        for side,value in levels.items():
            sign=1 if side=='BUY' else -1;entry=spec.price(value);quote=number(snap['quote']['ask' if side=='BUY' else 'bid'])
            if sign*(entry-quote)<=0 or sign*(entry-quote)<spec.point*spec.stops_level:raise ValueError('STOP пересечён/слишком близко. MARKET не создаётся')
            distance=number(cfg.INITIAL_SL_DISTANCE if role=='initial' else cfg.RECOVERY_SL_DISTANCE)
            sl=spec.price(entry-sign*distance)
            tps=[str(entry+sign*number(cfg.INITIAL_TP_DISTANCE))] if role=='initial' else self._recovery_tps(cfg,spec,side,[entry]*count,[size]*count,debt)
            for i,tp in enumerate(tps):
                spec.protection(side,entry,sl,number(tp));plans.append(dict(kind='STOP',side=side,price=str(entry),trigger=str(entry),sl=str(sl),tp=tp,size=str(size),sl_distance=str(distance),crossed_policy='reject',basket_index=i))
        return plans

    def _arm(self,snap,role,levels):
        c=self.state['cycle']
        try:plans=self._plans(snap,role,levels)
        except (ValueError,ArithmeticError):
            if c.get('level_wait')!=levels:self.notify('Уровни корзины недопустимы или пересечены. Ждём допустимую котировку/подтверждённую правку; входа MARKET нет.')
            c['level_wait']=deepcopy(levels);self.save('basket_level_wait');return False
        group=dict(id=uuid.uuid4().hex,role=role,levels=deepcopy(levels),commands=[],activated=None,ladder_applied=False)
        for plan in plans:
            cid=self._intent(**plan);group['commands'].append(cid);c['entries'].append(cid)
        c['groups'].append(group);c.pop('level_wait',None);c['round']+=1;self.save('basket_group_armed');return True

    def _halt(self,snap,reason):
        c=self.state.get('cycle')
        if c:
            self._cancel(snap,c['entries'])
            self._enter_manual(c,reason,self.clock())
        else:self.pause(reason)

    def _observe(self,snap):
        c=self.state['cycle'];changed=False
        if any(o.get('command_id') not in c['entries'] and not any(self.state['commands'][cid].get('order_id')==o['id'] for cid in c['entries']) for o in snap['orders']):self._halt(snap,'UNKNOWN_ORDER_IDENTITY');return False
        for g in c['groups']:
            for cid in g['commands']:
                cmd=self.state['commands'][cid];ins=[d for d in snap['deals'] if d['entry']=='IN' and d.get('command_id')==cid]
                if not ins:continue
                ids={d['position_id'] for d in ins}
                if len(ids)!=1 or sum((number(d['volume']) for d in ins),D(0))!=number(cmd['size']):self._halt(snap,'PARTIAL_FILL_OR_NONINDEPENDENT_TICKET');return False
                pid=next(iter(ids));proof=snap.get('obligations',{}).get(cid,{})
                if self.evidence_contract=='per_obligation_v1' and proof.get('status') not in {'FILLED_OPEN_EVIDENCE','CLOSED_EVIDENCE'}:
                    if proof.get('status') in {'PARTIAL_FILL','PARTIAL_CLOSE','EXTERNAL_EXIT','UNSUPPORTED_EXIT','MIXED_EXIT_REASONS','VOLUME_EVIDENCE_INCONSISTENT'}:self._halt(snap,'INCOMPLETE_TICKET_EVIDENCE')
                    return False
                if pid not in c['tickets']:
                    entry=sum((number(d['price'])*number(d['volume']) for d in ins),D(0))/number(cmd['size'])
                    try:cost=net_money(ins)
                    except ValueError:self._halt(snap,'OPENING_COSTS_UNKNOWN');return False
                    c['tickets'][pid]=dict(id=pid,command_id=cid,group=g['id'],role=g['role'],side=cmd['side'],entry=str(entry),size=cmd['size'],sl=cmd['sl'],tp=cmd['tp'],index=cmd['basket_index'],opening_net=str(cost),closed=False)
                    c['realized']=str(number(c['realized'])+cost);c['recovery']=str(max(D(0),-number(c['realized'])));c['opened_ids'].append(pid);changed=True
        if changed:self.save('basket_fills_adopted')
        if any(p['id'] not in c['tickets'] for p in snap['positions']):self._halt(snap,'UNKNOWN_POSITION_IDENTITY');return False
        if len({t['command_id'] for t in c['tickets'].values()})!=len(c['tickets']):self._halt(snap,'MERGED_TICKETS_UNSUPPORTED');return False
        for pid,t in c['tickets'].items():
            exits=[d for d in snap['deals'] if d['entry']=='OUT' and d['position_id']==pid]
            live=next((p for p in snap['positions'] if p['id']==pid),None)
            if live and number(live['size'])!=number(t['size']):self._halt(snap,'PARTIAL_POSITION_CHANGE');return False
            if exits and not t['closed']:
                if live or sum((number(d['volume']) for d in exits),D(0))!=number(t['size']):self._halt(snap,'PARTIAL_CLOSE_UNSUPPORTED');return False
                reasons={d['reason'] for d in exits}
                cleanup_close=(reasons=={'MANUAL'} and all(d.get('close_command_id') in c.get('cleanup_ids',[]) for d in exits))
                if (not reasons<={'TP','SL'} and not cleanup_close) or len(reasons)!=1:self._halt(snap,'UNEXPECTED_CLOSE_REASON');return False
                if self.evidence_contract=='per_obligation_v1' and snap['obligations'].get(t['command_id'],{}).get('status')!='CLOSED_EVIDENCE':return False
                try:net=net_money(exits)
                except ValueError:self._halt(snap,'CLOSING_COSTS_UNKNOWN');return False
                expected=deepcopy(self._durable_state)
                t.update(closed=True,reason=next(iter(reasons)),exit_net=str(net),exit_facts=[{k:d.get(k) for k in ('id','price','volume','time','reason')} for d in exits]);c['realized']=str(number(c['realized'])+net);c['recovery']=str(max(D(0),-number(c['realized'])))
                self.state['seen_deals'].extend(d['id'] for d in exits)
                for cmd in self.state['commands'].values():
                    if cmd['kind']=='MODIFY' and cmd.get('position_id')==pid and cmd['status'] not in TERMINAL:cmd.update(status='OBSOLETE',closed_by_deals=[d['id'] for d in exits])
                if self.evidence_contract=='per_obligation_v1':
                    records=[dict(backend=self.state['backend'],account=self.state['account'],currency=self.state['currency'],ticket=d['id'],position_id=pid,cycle_id=c['id'],raw=snap['ledger_facts'][d['id']],net_accounting={'opening_net':t['opening_net'],'closing_net':str(net_money([d]))}) for d in exits]
                    try:self.store.commit_deals(expected,self.state,records,'basket_ticket_closed');self._durable_state=deepcopy(self.state)
                    except BaseException:self.state=self.store.load() or expected;self._durable_state=deepcopy(self.state);raise
                else:self.save('basket_ticket_closed')
        return True

    def _reconcile_commands(self,snap):
        super()._reconcile_commands(snap)
        for cmd in self.state['commands'].values():
            if cmd['kind']=='CLOSE' and cmd['status'] not in TERMINAL:
                exits=[d for d in snap['deals'] if d['entry']=='OUT' and d.get('close_command_id')==cmd['id'] and d['position_id']==cmd['position_id']]
                if not any(p['id']==cmd['position_id'] for p in snap['positions']) and sum((number(d['volume']) for d in exits),D(0))==number(cmd['size']):cmd['status']='DONE'
        self.save('basket_close_reconciliation')

    def _finish(self,snap):
        c=self.state['cycle'];self._cancel(snap,c['entries'])
        if snap['orders'] or snap['positions'] or self._unresolved():return
        if number(c['recovery'])>0:return
        self.state['completed'].append(dict(id=c['id'],contract=CONTRACT,realized=c['realized'],recovery='0',currency=c['currency']))
        self.state['basket_carry']='0';self.state['cycle']=None;self.state['last_candle']=snap['candle']['id']
        self.state['cooldown_until']=snap['now']+int(number(c['cfg']['CYCLE_COOLDOWN_SECONDS']))
        self.state['phase']='PAUSED' if self.state['stop'] else 'COOLDOWN';self.save('basket_cycle_complete')

    def _live_ladder(self,snap,g):
        c=self.state['cycle'];tickets=sorted([t for t in c['tickets'].values() if t['group']==g['id']],key=lambda t:t['index'])
        active=[t for t in tickets if not t['closed']]
        selected=[cid for cid in g['commands'] if self.state['commands'][cid]['side']==g['activated']]
        if len(tickets)!=len(selected):return
        if not active or g['ladder_applied']:return
        cfg=BasketSettings.load(c['cfg']);spec=Spec.load(snap['spec'])
        if any(t['closed'] for t in tickets):g['ladder_applied']=True;self.save('basket_gap_exit_observed');return
        try:
            tps=self._recovery_tps(cfg,spec,g['activated'],[t['entry'] for t in active],[t['size'] for t in active],c['recovery'])
            plans=[]
            for t,tp in zip(active,tps):
                live=next(p for p in snap['positions'] if p['id']==t['id'])
                spec.protection(t['side'],number(t['entry']),number(live['sl']),number(tp),snap['quote'],modify=True)
                if number(live['tp'])!=number(tp):plans.append((t,live['sl'],tp))
        except (ValueError,ArithmeticError,StopIteration):self._halt(snap,'ACTUAL_FILL_LADDER_REQUIRES_REVIEW');return
        for t,sl,tp in plans:
            self._intent('MODIFY',position_id=t['id'],sl=sl,tp=tp,purpose='ANCHOR');t.update(sl=sl,tp=tp)
        g['ladder_applied']=True;self.save('basket_actual_fill_ladder')

    def _step(self,snap):
        c=self.state['cycle']
        if not self._observe(snap):return
        if c.get('manual'):self._cancel(snap,c['entries']);return
        if c.get('edit'):
            if self._advance_edit(snap):return
        active=[t for t in c['tickets'].values() if not t['closed']]
        initial=next((t for t in c['tickets'].values() if t['role']=='initial'),None)
        request=c.get('initial_cancel')
        if request:
            if initial is not None:
                c.pop('initial_cancel');self.save('basket_cancel_initial_fill_won')
            else:
                self._cancel(snap,request['ids'])
                if snap['orders'] or snap['positions'] or not self._resolved(request['ids']) or self._uncertain():return
                for g in c['groups']:g['retired']=True
                c.pop('replacement',None);c.pop('signal_override',None);c.pop('level_wait',None)
                c['wait_for_signal']=True
                self.state['last_candle']=max(self.state['last_candle'],request['after_candle'])
                c.pop('initial_cancel');self.state['phase']='INITIAL_WAIT'
                self.save('basket_cancel_initial_wait_fresh_close');return
        recoveries=[t for t in active if t['role']!='initial']
        if initial and not initial['closed'] and recoveries:self._halt(snap,'INITIAL_AND_RECOVERY_OVERLAP');return
        for g in c['groups']:
            sides={t['side'] for t in c['tickets'].values() if t['group']==g['id']}
            if len(sides)>1:self._halt(snap,'BOTH_RECOVERY_SIDES_FILLED');return
            if sides:
                g['activated']=next(iter(sides));opposite=[cid for cid in g['commands'] if self.state['commands'][cid]['side']!=g['activated']]
                self._cancel(snap,opposite)
                selected=[cid for cid in g['commands'] if self.state['commands'][cid]['side']==g['activated']]
                if any(self.state['commands'][cid]['status']=='CANCELLED' for cid in selected):self._halt(snap,'INCOMPLETE_BASKET_CANCELLED_TICKETS');return
        rejected_initial=[cmd for cmd in self.state['commands'].values() if cmd.get('cycle_id')==c['id'] and cmd['kind']=='STOP' and cmd['status']=='REJECTED' and not cmd.get('edit_superseded')]
        if not c['tickets'] and rejected_initial and not snap['orders'] and not self._uncertain():
            for cmd in rejected_initial:cmd['edit_superseded']=True
            for g in c['groups']:g['retired']=True
            c['wait_for_signal']=True;self.state['last_candle']=snap['candle']['id'];self.notify('Начальный STOP отклонён: ждём новую закрытую M1 без MARKET.');self.save('basket_initial_rejected_wait');return
        if any(cmd['status']=='REJECTED' for cmd in self.state['commands'].values() if cmd.get('cycle_id')==c['id'] and cmd['kind'] in {'STOP','MODIFY','CLOSE'} and not cmd.get('edit_superseded')):
            self._halt(snap,'BASKET_COMMAND_REJECTED');return
        if initial is None:
            if self.state['stop']:
                self._cancel(snap,c['entries']);self.state['phase']='PAUSED_CYCLE';self.save('basket_paused');return
            self._follow(snap);return
        if initial['closed'] and initial['reason']=='TP':
            if any(t['role']!='initial' for t in c['tickets'].values()):self._halt(snap,'INITIAL_TP_AND_REVERSAL_FILLED');return
            if number(c['recovery'])<=0:self._finish(snap)
            else:self._halt(snap,'INITIAL_TP_NET_LOSS_REQUIRES_REVIEW')
            return
        if not initial['closed']:
            self.state['phase']='INITIAL_POSITION'
            if self.state['stop'] or self.state.get('time_block'):self._cancel(snap,[cid for g in c['groups'] if g['role']!='initial' for cid in g['commands']]);return
            self._initial_position_recovery(snap,initial)
            self.save('basket_initial_protected');return
        # Initial SL confirmed; recovery fills are adopted only with independent ticket evidence.
        if active and (number(c['recovery'])<=0 or c.get('cleanup')):
            c['cleanup']=True;self.state['phase']='RECOVERY_CLEANUP'
            self._cancel(snap,c['entries'])
            if not snap['orders'] and not self._uncertain():
                existing={self.state['commands'][cid]['position_id'] for cid in c.get('cleanup_ids',[])}
                for t in active:
                    if t['id'] not in existing:c.setdefault('cleanup_ids',[]).append(self._intent('CLOSE',position_id=t['id'],size=t['size'],purpose='MANUAL'))
                self.save('basket_gap_profit_cleanup')
            return
        if not active:
            c.pop('cleanup',None)
            # Completion may span cancellation reconciliation and a restart.
            # A retired round must never re-arm once its actual debt is covered.
            if number(c['recovery'])<=0:self._finish(snap);return
        if active:
            self.state['phase']='RECOVERY_POSITION'
            for g in c['groups']:
                if g['role']!='initial' and g['activated']:self._live_ladder(snap,g)
            return
        groups=[g for g in c['groups'] if g['role']!='initial' and not g.get('retired')]
        if groups:
            g=groups[-1];tickets=[t for t in c['tickets'].values() if t['group']==g['id']]
            if self._pending(snap,g['commands']) or not self._resolved(g['commands']):
                self.state['phase']='RECOVERY_PENDING';return
            if tickets:
                g['retired']=True
                if number(c['recovery'])<=0:self._finish(snap);return
                if not self._after_stopped_group(snap,g,tickets):return
        if self.state['stop'] or self.state.get('time_block'):
            self._cancel(snap,c['entries']);self.state['phase']='PAUSED_CYCLE';self.save('basket_paused_debt_retained');return
        levels=self._recovery_levels(initial)
        self.state['phase']='RECOVERY_WAIT'
        if not snap['orders'] and not self._uncertain():self._arm(snap,'expanded' if c['expanded'] else 'reversal',levels)

    def _initial_position_recovery(self,snap,initial):
        c=self.state['cycle']
        if not any(g['role']=='reversal' and not g.get('retired') for g in c['groups']):
            self._arm(snap,'reversal',{'SELL' if initial['side']=='BUY' else 'BUY':initial['sl']})

    def _after_stopped_group(self,snap,g,tickets):
        c=self.state['cycle']
        if any(t.get('reason')=='SL' for t in tickets) and c['expanded'] is None:
            side=g['activated'];entry=g['levels'][side];sl=next(t['sl'] for t in tickets if t['reason']=='SL')
            c['expanded']=expanded_levels(side,entry,sl,c['cfg']['EXPANSION_DISTANCE']);self.save('basket_expanded_once')
        return True

    def _recovery_levels(self,initial):
        return self.state['cycle']['expanded'] or {'SELL' if initial['side']=='BUY' else 'BUY':initial['sl']}

    def _follow(self,snap):
        c=self.state['cycle'];bar=snap['candle'];signal=c['signal']
        if c.get('wait_for_signal'):
            if bar['id']<=self.state['last_candle'] or not self._fresh_closed_candle(snap) or number(bar.get('open',bar['close']))==number(bar['close']):return
            c['replacement']=dict(side='BUY' if number(bar['close'])>number(bar['open']) else 'SELL',close=bar['close'],id=bar['id']);c.pop('wait_for_signal')
        if bar['id']>self.state['last_candle'] and self._fresh_closed_candle(snap) and 'open' in bar:
            self.state['last_candle']=bar['id'];direction='BUY' if number(bar['close'])>number(bar['open']) else 'SELL' if number(bar['close'])<number(bar['open']) else None
            if direction and (direction!=signal['side'] or c.get('level_wait')):
                c['replacement']=dict(side=direction,close=bar['close'],id=bar['id'])
            self.save('basket_candle_observed')
        if c.get('replacement'):
            self._cancel(snap,c['entries'])
            if snap['orders'] or snap['positions'] or not self._resolved(c['entries']) or self._uncertain():return
            c['signal']=c.pop('replacement');signal=c['signal'];c['groups']=[];c['entries']=[];c.pop('signal_override',None);c.pop('level_wait',None);self.save('basket_signal_replaced')
        if any(not g.get('retired') for g in c['groups']):return
        if c.get('level_wait'):return
        sign=1 if signal['side']=='BUY' else -1
        level=str(number(signal['close'])+sign*number(c['cfg']['INITIAL_ENTRY_DISTANCE']))
        self._arm(snap,'initial',c.get('signal_override') or {signal['side']:level});self.state['phase']='INITIAL_PENDING'

    def tick(self):
        if self.state.get('migration_block'):return
        self.poll_time();self._time_guard();self._deadline_guard()
        if self.state['phase']=='SAFE_PAUSE':return
        try:snap=self.snapshot()
        except (OSError,TimeoutError):return
        except ValueError:self.pause('Не удалось подтвердить брокерские факты');return
        if getattr(self.broker,'read_only',False):return
        self._reconcile_commands(snap)
        if any(not x.get('owned') for x in snap['positions']+snap['orders']):self.pause('Неизвестные ордера/позиции');return
        c=self.state.get('cycle')
        if c:
            try:self._step(snap)
            except (ValueError,ArithmeticError):self._halt(snap,'BASKET_VALIDATION_FAILED');return
        elif self.state['phase']=='COOLDOWN':
            if snap['now']>=self.state.get('cooldown_until',0):self.state['phase']='WAIT_CANDLE';self.state['last_candle']=snap['candle']['id'];self.save('basket_cooldown_finished')
        elif self.state['phase']=='WAIT_CANDLE' and not self.state['stop'] and not self.state.get('time_block'):
            bar=snap['candle']
            if bar['id']>self.state['last_candle'] and self._fresh_closed_candle(snap) and 'open' in bar:
                if number(bar['open'])==number(bar['close']):self.state['last_candle']=bar['id'];self.save('basket_doji');return
                if snap['positions'] or snap['orders'] or self._unresolved():self.pause('Новый цикл требует подтверждённого отсутствия обязательств');return
                try:self._new(snap);self._step(snap)
                except (ValueError,ArithmeticError):self.pause('Параметры корзины не соответствуют брокеру');return
        self.save('basket_tick')
        if self.state['phase'] not in {'SAFE_PAUSE','MANUAL'}:self.dispatch()
        else:self.dispatch({'CANCEL'})

    def edit(self,*,settings=None,levels=None,expected=None,protection=None,expected_orders=None):
        """Validate everything before staging a durable multi-step edit.

        Pending replacements are cancelled first. New fills/closures during cancel
        abort the edit. Protection batches are not atomic at the broker; partial
        success is displayed from broker truth and stops further automation.
        """
        try:
            cfg=BasketSettings.load(settings if settings is not None else self.state['basket_settings'])
            if levels is not None:
                if not isinstance(levels,dict) or not levels or not set(levels)<={'BUY','SELL'}:raise ValueError('Нужны уровни BUY/SELL')
                levels={k:str(number(v)) for k,v in levels.items()}
                if not isinstance(expected,dict):raise ValueError('Нужен снимок прежних уровней')
            if protection is not None:
                if not isinstance(protection['position_id'],str):raise ValueError('Нужен ID билета')
                for field in ('old','new'):
                    if not isinstance(protection[field],dict) or set(protection[field])!={'sl','tp'}:raise ValueError('Нужны SL и TP')
                    if any(number(v)<=0 for v in protection[field].values()):raise ValueError('SL/TP должны быть положительными')
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:raise ActionRejected(str(exc),public_message='Неверные параметры: нужны конечные положительные числа, целое число билетов 1–20 и полный набор ключей. Резерв и пауза могут быть нулём.') from exc
        snap=self._guard(entry=True);spec=Spec.load(snap['spec']);c=self.state.get('cycle')
        try:cfg.validate(spec)
        except (ValueError,ArithmeticError) as exc:raise ActionRejected(str(exc),public_message='Параметры не соответствуют min/step/max объёма или tick/stops брокера. Округления и расширения нет.') from exc
        if not c:
            if levels is not None or protection is not None:raise ActionRejected('Нет активного цикла')
            self.state['basket_settings']=cfg.dump();self.save('basket_settings_saved');return
        if c.get('edit') or c.get('initial_cancel') or self._uncertain():raise ActionRejected('Предыдущая операция ещё не сверена; правка не принята')
        if any(g['activated'] and self._pending(snap,g['commands']) for g in c['groups']):raise ActionRejected('Корзина исполняется частично: сначала сверка всех билетов')
        oldcfg=BasketSettings.load(c['cfg']);live={p['id']:p for p in snap['positions']}
        current_levels=self.levels()
        if expected_orders is not None and (not isinstance(expected_orders,list) or sorted(expected_orders)!=sorted(o['id'] for o in snap['orders'])):raise ActionRejected('Ордера уже изменились; обновите предпросмотр')
        if levels is not None:
            if {k:str(v) for k,v in expected.items()}!=current_levels:raise ActionRejected('Уровни уже изменились; обновите предпросмотр')
            if set(levels)!=set(current_levels):raise ActionRejected('Правка не может незаметно добавить сторону корзины')
        pending_groups=[g for g in c['groups'] if not g.get('retired') and any(self.state['commands'][cid]['status'] in {'PENDING','READY','UNKNOWN','ACCEPTED'} for cid in g['commands'])]
        updates=[]
        try:
            if levels is not None:
                for side,value in levels.items():
                    p=spec.price(value);q=number(snap['quote']['ask' if side=='BUY' else 'bid']);sign=1 if side=='BUY' else -1
                    if sign*(p-q)<=0 or sign*(p-q)<spec.point*spec.stops_level:raise ValueError('Новый STOP пересечён или слишком близок; округления/расширения нет')
            for pid,p in live.items():
                t=c['tickets'].get(pid)
                if not t:raise ValueError('Билет ещё не сверён')
                sl,tp=p['sl'],p['tp'];sign=1 if t['side']=='BUY' else -1
                group=next(g for g in c['groups'] if g['id']==t['group'])
                key='INITIAL_SL_DISTANCE' if t['role']=='initial' else 'RECOVERY_SL_DISTANCE'
                if getattr(cfg,key)!=getattr(oldcfg,key):sl=str(number(group['levels'][t['side']])-sign*number(getattr(cfg,key)))
                if t['role']=='initial' and cfg.INITIAL_TP_DISTANCE!=oldcfg.INITIAL_TP_DISTANCE:tp=str(number(t['entry'])+sign*number(cfg.INITIAL_TP_DISTANCE))
                if t['role']=='initial' and levels is not None:
                    opposite='SELL' if t['side']=='BUY' else 'BUY'
                    if opposite in levels:sl=levels[opposite]
                if protection is not None and protection['position_id'] in c['tickets'] and t['group']==c['tickets'][protection['position_id']]['group'] and number(protection['new']['sl'])!=number(protection['old']['sl']):sl=str(number(protection['new']['sl']))
                if protection is not None and pid==protection['position_id']:
                    if any(number(protection['old'][k])!=number(p[k]) for k in ('sl','tp')):raise ValueError('Защита уже изменилась; обновите предпросмотр')
                    sl,tp=map(lambda k:str(number(protection['new'][k])),('sl','tp'))
                updates.append(dict(position_id=pid,sl=sl,tp=tp))
            if protection is not None and protection['position_id'] not in live:raise ValueError('Билет уже закрыт или изменился')
            if settings is not None and (cfg.COST_RESERVE!=oldcfg.COST_RESERVE or cfg.TP_STEP_MULTIPLIER!=oldcfg.TP_STEP_MULTIPLIER):
                for g in c['groups']:
                    ts=sorted([t for t in c['tickets'].values() if t['group']==g['id'] and not t['closed'] and t['role']!='initial'],key=lambda t:t['index'])
                    if ts:
                        tps=ladder(self.broker,spec,ts[0]['side'],[t['entry'] for t in ts],[t['size'] for t in ts],c['recovery'],reserve=cfg.COST_RESERVE,multiplier=cfg.TP_STEP_MULTIPLIER)
                        for t,tp in zip(ts,tps):next(u for u in updates if u['position_id']==t['id'])['tp']=tp
            for u in updates:
                p=live[u['position_id']];spec.protection(p['side'],number(p['entry']),number(u['sl']),number(u['tp']),snap['quote'],modify=True)
            # Complete validation of all planned replacement requests before cancelling anything.
            for g in pending_groups:
                target=dict(levels or g['levels'])
                if g['role']=='initial' and levels is None and cfg.INITIAL_ENTRY_DISTANCE!=oldcfg.INITIAL_ENTRY_DISTANCE:
                    signal=c['signal'];target={signal['side']:str(number(signal['close'])+(1 if signal['side']=='BUY' else -1)*number(cfg.INITIAL_ENTRY_DISTANCE))}
                if g['role']=='reversal':
                    t=next(t for t in c['tickets'].values() if t['role']=='initial')
                    u=next((u for u in updates if u['position_id']==t['id']),None)
                    if u:target={'SELL' if t['side']=='BUY' else 'BUY':u['sl']}
                self._plans(snap,g['role'],target,cfg)
        except (ValueError,ArithmeticError,StopIteration) as exc:raise ActionRejected(str(exc),public_message='Правка отклонена: проверьте точность цен, stop/freeze ограничения и актуальность билетов. Уровни не округлены; MARKET не создаётся.') from exc
        if cfg.BASKET_COUNT!=oldcfg.BASKET_COUNT or cfg.TICKET_SIZE!=oldcfg.TICKET_SIZE or cfg.INITIAL_SIZE!=oldcfg.INITIAL_SIZE:
            self.notify('Объём и число билетов изменятся только для ещё не открытых корзин. Открытые позиции не увеличиваются и не уменьшаются.')
        c['edit']=dict(stage='CANCEL',settings=cfg.dump(),levels=levels,updates=updates,
            cancel_ids=[cid for g in pending_groups for cid in g['commands']],groups=[g['id'] for g in pending_groups],
            fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[])
        self.save('basket_edit_requested')

    def _commit_edit_extra(self,e):
        pass

    def _advance_edit(self,snap):
        c=self.state['cycle'];e=c['edit']
        if sorted(c['tickets'])!=e['fills'] or sorted(pid for pid,t in c['tickets'].items() if t['closed'])!=e['closed']:
            c.pop('edit');self.notify('Правка прервана: во время сверки произошло исполнение/закрытие. Сохранена брокерская защита; нужна ручная сверка.')
            self._halt(snap,'EDIT_FILL_OR_CLOSE_RACE');return True
        if e['stage']=='CANCEL':
            self._cancel(snap,e['cancel_ids'])
            if self._pending(snap,e['cancel_ids']) or not self._resolved(e['cancel_ids']) or self._uncertain():return True
            # Revalidate fresh quote before creating any protection intents.
            spec=Spec.load(snap['spec']);live={p['id']:p for p in snap['positions']}
            for pid,old in e.get('expected_protection',{}).items():
                if pid not in live or any(number(live[pid][k])!=number(old[k]) for k in ('sl','tp')):
                    self._halt(snap,'EDIT_PROTECTION_CHANGED');return True
            try:
                for u in e['updates']:
                    p=live[u['position_id']];spec.protection(p['side'],number(p['entry']),number(u['sl']),number(u['tp']),snap['quote'],modify=True)
            except (ValueError,KeyError):self._halt(snap,'EDIT_QUOTE_CHANGED');return True
            for u in e['updates']:
                p=live[u['position_id']]
                if any(number(p[k])!=number(u[k]) for k in ('sl','tp')):e['modify_ids'].append(self._intent('MODIFY',purpose='ANCHOR',**u))
            e['stage']='MODIFY';self.save('basket_edit_modifications');return True
        commands=[self.state['commands'][cid] for cid in e['modify_ids']]
        if any(cmd['status']=='REJECTED' for cmd in commands):self._halt(snap,'EDIT_PARTIAL_FAILURE_REVIEW_BROKER_TRUTH');return True
        if any(cmd['status']!='DONE' for cmd in commands):return True
        live={p['id']:p for p in snap['positions']}
        for u in e['updates']:
            p=live.get(u['position_id'])
            if not p or any(number(p[k])!=number(u[k]) for k in ('sl','tp')):return True
        for u in e['updates']:
            t=c['tickets'][u['position_id']];t.update(sl=u['sl'],tp=u['tp'])
            next(g for g in c['groups'] if g['id']==t['group'])['ladder_applied']=True
        c['cfg']=e['settings'];self.state['basket_settings']=deepcopy(e['settings'])
        for g in c['groups']:
            if g['id'] in e['groups']:g['retired']=True
        if e['levels'] is not None:
            if c['expanded'] is not None:c['expanded']=deepcopy(e['levels'])
            elif not c['tickets']:c['signal_override']=deepcopy(e['levels'])
        self._commit_edit_extra(e)
        c.pop('edit');c.pop('level_wait',None);self.notify('Правка подтверждена брокером. Долг сохранён; дальнейшие входы используют новые параметры/уровни.');self.save('basket_edit_confirmed');return False

    def levels(self):
        c=self.state.get('cycle') or {}
        if c.get('expanded'):return deepcopy(c['expanded'])
        for g in reversed(c.get('groups',[])):
            if not g.get('retired'):return deepcopy(g['levels'])
        return deepcopy(c.get('signal_override') or {})

    def _reset_manual(self,update_id=None):
        if self.state.get('migration_block'):raise ActionRejected('Старый цикл сохраняется. Сверка выполняется прежней версией; новая логика его не меняет.')
        c=self.state.get('cycle')
        if not c or c.get('contract')!=CONTRACT or not c.get('manual'):raise ActionRejected('Нужен незавершённый ручной цикл корзины')
        if self.evidence_contract!='per_obligation_v1':raise ActionRejected('Нужны положительные брокерские доказательства завершения всех билетов')
        snap,positions,fresh=self._manual_reset_facts(c)
        delta=net_money(fresh) if fresh else D(0)
        for pid in {d['position_id'] for d in fresh}:
            if pid not in c['tickets']:
                delta+=net_money([d for d in snap['deals'] if d['entry']=='IN' and d['position_id']==pid])
        c['reset_position_ids']=sorted(positions);self.save('basket_manual_reset_evidence');expected=deepcopy(self._durable_state)
        try:
            debt=max(D(0),-number(c['realized'])-delta)
            self.state['basket_carry']=str(debt)
            self.state['completed'].append(dict(id=c['id'],contract=CONTRACT,recovery_retained=str(debt),written_off_recovery='0',completion='MANUAL_RESET_RETAINED'))
            self.state['seen_deals'].extend(d['id'] for d in fresh)
            for cmd in self.state['commands'].values():
                if cmd.get('cycle_id')==c['id'] and cmd['status'] not in TERMINAL:cmd['status']='OBSOLETE'
            self.state.update(cycle=None,phase='WAIT_START',stop=True,stop_mode='',last_manual_reset=c['id'])
            records=[dict(backend=self.state['backend'],account=self.state['account'],currency=self.state['currency'],ticket=d['id'],position_id=d['position_id'],cycle_id=c['id'],raw=snap['ledger_facts'][d['id']]) for d in fresh]
            self.store.commit_deals(expected,self.state,records,'basket_manual_reset',manual_reset=True);self._durable_state=deepcopy(self.state)
        except BaseException:self.state=self.store.load() or expected;self._durable_state=deepcopy(self.state);raise
