"""Staged interactive controller, restricted to the offline simulator adapter.

The production MT5 service continues to use the audited Controller until the new
CLOSE/evidence contract has a separately verified native adapter. No bypass flag.
"""
from copy import deepcopy
from decimal import Decimal as D
import json
import uuid

from .engine import Controller, TERMINAL
from .control import ActionRejected, ActionGateway
from .broker import Spec, target_price
from .settings import Settings, number
from .strategies.levels import user_triggers, recovery_triggers
from .strategies.fixed_recovery import RecoveryConfig, initial, installment, resize, settle, net_money


class InteractiveController(Controller):
    def __init__(self, store, broker, **kwargs):
        if getattr(broker,'backend_id',None) != 'interactive_simulator_v1':
            raise ActionRejected('Interactive upgrades require the explicit offline simulator adapter')
        fresh=store.load() is None
        super().__init__(store,broker,**kwargs)
        if fresh:
            self.state['settings']['BASE_TP_DISTANCE']='0.25'
        self.state.setdefault('operation_mode','auto')
        self.state.setdefault('modules',dict(engine='legacy',recovery=RecoveryConfig().dump()))

    def action(self, action):
        """Common authenticated transport boundary; caller uses ActionGateway."""
        if not isinstance(action,dict) or 'type' not in action: raise ActionRejected('Action type required')
        kind=action['type']
        if kind=='command' and set(action)=={'type','text'} and isinstance(action['text'],str): return self.command(action['text'])
        if kind=='start' and set(action)=={'type','mode'} and action['mode'] in ('auto','semi'): return self.command('/start '+action['mode'])
        if kind=='settings' and set(action)=={'type','mode','engine','recovery'}:
            mode=action['mode'];c=self.state.get('cycle')
            if mode not in ('auto','semi') or (c and c.get('operation_mode')!=mode):raise ActionRejected('Mode change requires completed cycle')
            if mode==self.state['operation_mode']:return self.configure(action['engine'],action['recovery'])
            snap=self._interactive_guard(entry=True)
            if c or snap['positions'] or snap['orders'] or self._unresolved():raise ActionRejected('Mode change requires confirmed flat')
            if self.state['phase']=='WAIT_START' and not self._fresh_closed_candle(snap):raise ActionRejected('Fresh closed UTC candle required')
            Spec.load(snap['spec']).validate(Settings.load(self.state['settings']))
            config=self._module_config(action['engine'],action['recovery'],snap)
            # Mode, phase and modules share one durable write after all validation.
            self.state['modules']=dict(engine=action['engine'],recovery=config.dump())
            self.state['operation_mode']=mode;self.state['account']=snap['account']
            self.state['last_candle']=snap['candle']['id']
            self.state['phase']='WAIT_TRIGGER' if mode=='semi' else ('COOLDOWN' if snap['now']<self.state.get('cooldown_until',0) else 'WAIT_CANDLE')
            self.state['stop']=False;self.state['stop_mode']=''
            self.save('interactive_settings_mode');return
        if kind=='modules' and set(action)=={'type','engine','recovery'}: return self.configure(action['engine'],action['recovery'])
        if kind=='triggers' and set(action)=={'type','representation','levels'}:
            return self.triggers(action['representation'],action['levels'])
        if kind=='protection' and set(action)=={'type','position_id','old','new'}:
            return self.protection(action['position_id'],action['old'],action['new'])
        if kind=='delete_trigger' and set(action)=={'type','order_id','old_price'}:
            return self.delete_trigger(action['order_id'],action['old_price'])
        if kind=='close_position' and set(action)=={'type','position_id','expected_size','confirmed'} and action['confirmed'] is True:
            return self.close_position(action['position_id'],action['expected_size'])
        raise ActionRejected('Unsupported action or fields')

    def _interactive_guard(self, *, entry=False):
        self._deadline_guard(); self._time_guard()
        if getattr(self.broker,'read_only',False) or self.state['phase']=='SAFE_PAUSE': raise ActionRejected('Execution unavailable')
        if entry and (self.state.get('time_block') or (self.state.get('cycle') or {}).get('manual')):
            raise ActionRejected('Time/manual guard blocks new entries')
        snap=self.snapshot()
        if any(not x.get('owned',False) for x in snap['orders']+snap['positions']): raise ActionRejected('Ownership conflict')
        c=self.state.get('cycle') or {}
        known=set(c.get('opened_ids',[]))|{d['position_id'] for d in snap['deals'] if d['entry']=='IN' and d.get('command_id') in c.get('entries',[])}
        if c.get('position'):known.add(c['position']['id'])
        if any(p['id'] not in known for p in snap['positions']):raise ActionRejected('Unidentified position; reconcile first')
        return snap

    def command(self,text,update_id=None):
        if update_id is not None and update_id<self.state['telegram_offset']:return
        raw=text.strip()
        if raw=='/start':
            self.notify('Выберите режим: /start auto — прежняя стратегия; /start semi — ждать уровни. Панель открывается кнопкой, не автоматически.')
            self.state['notifications'][-1]['reply_markup']={'inline_keyboard':[
                [{'text':'Авто','callback_data':'/start auto'},{'text':'Полуавто','callback_data':'/start semi'}],
                [{'text':'Локальная MOCK панель','url':'http://127.0.0.1:8765/?controller=mock'}]]}
            self._finish_command(update_id,'mode_selection');return
        if raw in {'/start auto','/start semi'}:
            mode=raw.split()[1];c=self.state.get('cycle')
            if c and c.get('operation_mode','auto')!=mode: raise ActionRejected('Mode change waits for completed cycle')
            snap=self._interactive_guard(entry=True)
            if self.state.get('time_block') or (c or {}).get('manual'):raise ActionRejected('Time/manual block requires reconciliation')
            if self.state['phase']=='WAIT_START' and not self._fresh_closed_candle(snap):raise ActionRejected('Fresh closed UTC candle required')
            Spec.load(snap['spec']).validate(Settings.load(self.state['settings']))
            if self.state['phase']=='PAUSED_CYCLE' or not c:
                if snap['positions'] or snap['orders'] or self._unresolved():raise ActionRejected('Start requires confirmed flat')
                if mode=='semi':self.state['phase']='WAIT_TRIGGER'
                elif c:self.state['phase']='NEXT_PAIR'
                else:
                    self.state['last_candle']=snap['candle']['id']
                    self.state['phase']='COOLDOWN' if snap['now']<self.state.get('cooldown_until',0) else 'WAIT_CANDLE'
            self.state['account']=snap['account'];self.state['operation_mode']=mode
            self.state['stop']=False;self.state['stop_mode']=''
            self._finish_command(update_id,'mode_selected');return
        if raw.startswith('/action '):
            try:action=json.loads(raw[8:])
            except ValueError:raise ActionRejected('Invalid action JSON')
            if update_id is None:self.action(action)
            else:
                ActionGateway(self,execute=self.action).apply(dict(id='telegram-update-'+str(update_id),
                    revision=self.state.get('control_receipts',{}).get('telegram-update-'+str(update_id),{}).get('revision',self.state.get('control_revision',0)+1)-1,action=action))
            self._finish_command(update_id,'interactive_action');return
        return super().command(text,update_id)

    def _finish_command(self,uid,event):
        if uid is not None:self.state['telegram_offset']=uid+1
        self.save(event)

    def _module_config(self,engine,recovery,snap):
        if engine not in {'legacy','fixed'}:raise ActionRejected('Choose exactly one TP engine')
        try:config=RecoveryConfig.load(recovery)
        except (ValueError,TypeError,KeyError):raise ActionRejected('Invalid recovery configuration')
        spec=Spec.load(snap['spec'])
        for key in ('after_sl','continuation','opposite','fixed_sl'):
            if number(getattr(config,key))%spec.tick_size:raise ActionRejected('Exact recovery distance must align with ticks')
        return config

    def configure(self,engine,recovery):
        snap=self._interactive_guard()
        config=self._module_config(engine,recovery,snap)
        c=self.state.get('cycle')
        self.state['modules']=dict(engine=engine,recovery=config.dump())
        if c and c.get('engine')=='fixed':
            changed=c['module_config']['parts']!=config.parts
            c['module_config']=config.dump()
            if changed:
                if c.get('position'):c['position'].pop('user_protection',None)
                c['fixed_recovery']=resize(c['fixed_recovery'],config.parts)
                if c.get('position'):c['position']['recalculate_tp']=True
        self.notify('Модули сохранены; смена движка — следующий полный цикл. N и расстояния текущего fixed recovery обновлены без сброса долга.')
        self.save('interactive_modules')
        if c and c.get('position') and c.get('engine')=='fixed':
            self._position(snap);self.dispatch({'MODIFY','CLOSE'})

    def _valid_levels(self,snap,levels):
        spec=Spec.load(snap['spec'])
        for side,value in levels.items():
            price=spec.price(value);quote=number(snap['quote']['ask' if side=='BUY' else 'bid'])
            if (price-quote)*(1 if side=='BUY' else -1)<=0:
                raise ActionRejected('Crossed user trigger rejected; never converted to MARKET')
        return levels

    def triggers(self,representation,levels):
        snap=self._interactive_guard(entry=True);spec=Spec.load(snap['spec'])
        if representation not in {'ranges','prices'}:raise ActionRejected('Choose ranges or prices')
        try:
            plan=user_triggers(spec,snap['quote'],**{representation:levels})
            prices=self._valid_levels(snap,plan.prices())
        except (ValueError,TypeError):raise ActionRejected('Invalid or crossed trigger levels')
        c=self.state.get('cycle')
        if not c:
            if self.state['operation_mode']!='semi' or self.state['phase']!='WAIT_TRIGGER':raise ActionRejected('Select semi mode before supplying triggers')
            if snap['orders'] or snap['positions'] or self._unresolved():raise ActionRejected('Confirmed flat required')
            self.state['stop']=False;self.state['stop_mode']=''
            self._new_interactive_cycle(snap,prices)
        else:
            if self.state['phase']=='WAIT_TRIGGER' and not c.get('position') and not snap['positions'] and not snap['orders'] and not self._unresolved():
                self.state['stop']=False;self.state['stop_mode']=''
            c['replacement']=dict(levels=prices,basis=plan.basis,bid=plan.bid,ask=plan.ask)
            for cid in c['entries']:
                cmd=self.state['commands'][cid]
                if cmd['status']=='READY':cmd.update(status='CANCELLED',never_sent=True)
            self.save('trigger_replacement_requested')
            self._cancel_pending(snap,c['entries']);self.dispatch({'CANCEL'})

    def _new_interactive_cycle(self,snap,levels):
        cfg=Settings.load(self.state['settings']);spec=Spec.load(snap['spec']);spec.validate(cfg)
        center=spec.price(snap['quote']['ask'])
        self.state['account']=snap['account']
        self.state['cycle']=dict(id=uuid.uuid4().hex,currency=spec.currency,started_at_utc=self.clock(),max_duration_seconds=86400,
            settings=cfg.dump(),original_center=str(center),center=str(center),branch='FIRST',first=True,target=None,recovery=None,
            realized='0',round=0,entries=[],position=None,opened_ids=[],level_plan=levels)
        c=self.state['cycle'];c['deadline_utc']=c['started_at_utc']+86400;self._start_branch_clock(c['started_at_utc'])
        c['last_observed_utc']=c['started_at_utc'];self._initialize_cycle(c)
        self.state['phase']='NEXT_PAIR';self.save('semi_cycle_received_quote')

    def _initialize_cycle(self,c):
        if 'engine' not in c:
            c['engine']=self.state['modules']['engine'];c['module_config']=deepcopy(self.state['modules']['recovery'])
            c['operation_mode']=self.state['operation_mode']
            if c['engine']=='fixed':c['fixed_recovery']=initial(RecoveryConfig.load(c['module_config']))

    def tick(self):
        if not self.state.get('cycle') and self.state['operation_mode']=='semi' and self.state['phase'] in {'COOLDOWN','WAIT_CANDLE'}:
            self.state['phase']='WAIT_TRIGGER';self.save('semi_wait_next_command')
        super().tick()

    def _cycle_tick(self,snap):
        c=self.state['cycle'];self._initialize_cycle(c)
        replacement=c.get('replacement')
        if replacement:
            self._cancel_pending(snap,c['entries']);self.dispatch({'CANCEL'})
            # Existing fill wins replacement; never drop ownership of old entries.
            has_fill=bool(c.get('position') or any(d['entry']=='IN' and d.get('command_id') in c['entries'] for d in snap['deals']))
            if not has_fill and not snap['orders'] and not snap['positions'] and not self._unresolved():
                try:self._valid_levels(snap,replacement['levels'])
                except ValueError:
                    c.pop('replacement');self.state['phase']='WAIT_TRIGGER';self.notify('Новые уровни пересечены: замена отклонена, ждём команду.');self.save('replacement_crossed');return
                c['level_plan']=replacement['levels'];c.pop('replacement');self.state['phase']='NEXT_PAIR';self.save('replacement_after_confirmed_cancel')
            elif self.state['phase']=='NEXT_PAIR':return
        if self.state['phase']=='WAIT_TRIGGER':return
        super()._cycle_tick(snap)

    def _pair(self,snap):
        c=self.state['cycle'];self._initialize_cycle(c)
        if self._time_guard() or c.get('manual'):return
        explicit=c.get('level_plan')
        if c['engine']=='legacy' and not explicit:return super()._pair(snap)
        cfg=Settings.load(c['settings']);spec=Spec.load(snap['spec']);spec.validate(cfg)
        module=RecoveryConfig.load(c['module_config'])
        prices=explicit or c.get('recovery_levels') or {side:str(number(c['center'])+sign*cfg.BRANCH1_RANGE) for side,sign in [('BUY',1),('SELL',-1)]}
        self._valid_levels(snap,prices)
        distance=number(module.fixed_sl) if c['engine']=='fixed' and not c['first'] else cfg.BRANCH1_SL_DISTANCE
        plans=[]
        for side,value in prices.items():
            entry=spec.price(value);sign=1 if side=='BUY' else -1
            money=installment(c['fixed_recovery']) if c['engine']=='fixed' else number(c.get('recovery') or 0)
            tp=entry+sign*cfg.BASE_TP_DISTANCE if c['first'] else target_price(self.broker,spec,side,cfg.INITIAL_SIZE,entry,money)
            sl=entry-sign*distance;spec.protection(side,entry,sl,tp)
            plans.append(dict(kind='STOP',side=side,trigger=str(entry),price=str(entry),sl=str(sl),tp=str(tp),size=str(cfg.INITIAL_SIZE),sl_distance=str(distance)))
        c.update(round=c['round']+1,entries=[],position=None,opened_ids=[],sl_distance=str(distance));c.pop('level_plan',None)
        for plan in plans:c['entries'].append(self._intent(**plan))
        self.state['phase']='PAIR';self.save('interactive_pair_intents')

    def _adopt(self,opening,snap):
        c=self.state['cycle']
        if c.get('engine')!='fixed':
            super()._adopt(opening,snap)
            if c.get('position'):
                c['position']['opening_net']=str(net_money(opening));self.save('opening_costs_observed')
            return
        cfg=Settings.load(c['settings']);size=sum((number(d['volume']) for d in opening),D(0))
        if size!=cfg.INITIAL_SIZE:self.pause('Partial/oversize opening requires reconciliation');return
        entry=sum((number(d['price'])*number(d['volume']) for d in opening),D(0))/size
        costs=net_money(opening)
        c['target']='0';c['recovery']=c['fixed_recovery']['debt']
        c['position']=dict(id=opening[0]['position_id'],entry=str(entry),size=str(size),side=opening[0]['side'],stepped=False,reanchored=False,opening_net=str(costs))
        self.state['phase']='MANUAL' if c.get('manual') else 'POSITION';self.save('fixed_actual_fill_adopted')

    def _position(self,snap):
        c=self.state['cycle'];p=c.get('position')
        if not p:return
        live=next((x for x in snap['positions'] if x['id']==p['id']),None)
        exits=[d for d in snap['deals'] if d['entry']=='OUT' and d['position_id']==p['id']]
        if exits or not live:return super()._position(snap)
        if c.get('engine')!='fixed' and not p.get('user_protection'):return super()._position(snap)
        if number(live['size'])!=number(p['size']):self.pause('Live volume changed; reconcile before protection');return
        if not self._mutation_allowed('MODIFY'):return
        if any(cmd['kind'] in {'MODIFY','CLOSE'} and cmd.get('position_id')==p['id'] and cmd['status'] not in TERMINAL for cmd in self.state['commands'].values()):return
        rejected=[cmd for cmd in self.state['commands'].values() if cmd['kind'] in {'MODIFY','CLOSE'}
            and cmd.get('position_id')==p['id'] and cmd['status']=='REJECTED' and not cmd.get('retry_replaced')]
        if rejected:
            cmd=rejected[0]
            if not cmd.get('result',{}).get('retryable_not_sent'):
                self.pause('Protection/close rejected; no automatic repeat');return
            retry=getattr(self.broker,'can_retry_not_sent',None)
            if retry is None or not retry(cmd['id']):return
            fields=('position_id','sl','tp','purpose') if cmd['kind']=='MODIFY' else ('position_id','size','purpose','expected_tp')
            replacement=self._intent(cmd['kind'],**{k:cmd[k] for k in fields if k in cmd})
            cmd['retry_replaced']=replacement;self.save('interactive_known_not_sent_retry');return
        spec=Spec.load(snap['spec']);cfg=Settings.load(c['settings']);side=p['side'];sign=1 if side=='BUY' else -1
        entry=number(p['entry']);size=number(p['size']);sl=number(live['sl'])
        if not p['reanchored']:sl=entry-sign*number(c['sl_distance'])
        if p.get('user_protection'):
            desired=p['user_protection'];sl=number(desired['sl']);tp=number(desired['tp'])
        else:
            goal=installment(c['fixed_recovery'])-number(p.get('opening_net',0))
            tp=entry+sign*cfg.BASE_TP_DISTANCE if c['first'] else target_price(self.broker,spec,side,size,entry,max(D(0),goal))
        executable=number(snap['quote']['bid' if side=='BUY' else 'ask'])
        if p.get('recalculate_tp') and sign*(executable-tp)>=0 and self.broker.profit(side,size,entry,executable)>0:
            self._intent('CLOSE',position_id=p['id'],size=p['size'],purpose='RECOVERY_TARGET',expected_tp=str(tp));self.save('recovery_target_reached_close');return
        if number(live['sl'])==sl and number(live['tp'])==tp:
            p['reanchored']=True;p.pop('recalculate_tp',None);self.save('interactive_protection_readback');return
        try:spec.protection(side,entry,sl,tp,snap['quote'],modify=True)
        except ValueError:return  # retain broker protection until legal fresh levels
        self._intent('MODIFY',position_id=p['id'],sl=str(sl),tp=str(tp),purpose='ANCHOR');self.save('interactive_protection_update')

    def protection(self,pid,old,new):
        snap=self._interactive_guard();c=self.state.get('cycle');p=(c or {}).get('position')
        if not p or p['id']!=pid:raise ActionRejected('Owned active position required')
        live=next((x for x in snap['positions'] if x['id']==pid),None)
        if not live or not isinstance(old,dict) or not isinstance(new,dict) or set(old)!={'sl','tp'} or set(new)!={'sl','tp'}:raise ActionRejected('Complete protection preview required')
        if any(number(old[k])!=number(live[k]) for k in old):raise ActionRejected('Protection changed; refresh preview')
        try:Spec.load(snap['spec']).protection(p['side'],number(p['entry']),number(new['sl']),number(new['tp']),snap['quote'],modify=True)
        except (ValueError,TypeError):raise ActionRejected('Invalid protection')
        p['user_protection']={k:str(number(v)) for k,v in new.items()};self.save('user_protection_requested');self._position(snap);self.dispatch({'MODIFY'})

    def delete_trigger(self,oid,old_price):
        snap=self._interactive_guard();c=self.state.get('cycle')
        order=next((o for o in snap['orders'] if o['id']==oid and o['owned']),None)
        if not c or not order or number(order['price'])!=number(old_price):raise ActionRejected('Trigger changed; refresh preview')
        self._cancel_pending(dict(snap,orders=[order]),c['entries']);self.dispatch({'CANCEL'})

    def close_position(self,pid,expected_size):
        snap=self._interactive_guard();c=self.state.get('cycle');p=(c or {}).get('position')
        live=next((x for x in snap['positions'] if x['id']==pid and x['owned']),None)
        if not p or p['id']!=pid or not live or number(live['size'])!=number(expected_size):raise ActionRejected('Position changed; refresh confirmation')
        if any(cmd['kind']=='CLOSE' and cmd.get('position_id')==pid and cmd['status'] not in TERMINAL for cmd in self.state['commands'].values()):raise ActionRejected('Close outcome unresolved')
        self.state['stop']=True;self.state['stop_mode']='FINISH';c['user_close_requested']=True
        self._cancel_pending(snap,c['entries'])
        self._intent('CLOSE',position_id=pid,size=p['size'],purpose='MANUAL');self.save('user_close_intent');self.dispatch({'CANCEL','CLOSE'})

    def _closed(self,deals,snap):
        c=self.state['cycle'];p=c['position']
        if c.get('engine')!='fixed' and not c.get('user_close_requested'):
            return super()._closed(deals,snap)
        if snap['positions'] or snap['orders'] or self._unresolved():return
        if any(d['id'] in self.state['seen_deals'] for d in deals):return
        if sum((number(d['volume']) for d in deals),D(0))!=number(p['size']):self.pause('Full close evidence required');return
        reasons={d['reason'] for d in deals}
        if len(reasons)!=1 or not reasons<={'SL','TP','MANUAL','RECOVERY_TARGET'}:self.pause('Explicit closure evidence required');return
        reason=next(iter(reasons));net=net_money(deals)+number(p.get('opening_net',0))
        close=number(deals[-1]['price'])
        if c.get('engine')=='fixed':
            c['fixed_recovery']=settle(c['fixed_recovery'],round_id=p['id'],reason='TP' if reason=='RECOVERY_TARGET' else reason,net=net)
            c['recovery']=c['fixed_recovery']['debt'];c['realized']=c['fixed_recovery']['realized_net'];complete=number(c['recovery'])<=0
        else:
            c['recovery']=str(number(c['recovery'])-net);c['realized']=str(number(c['realized'])+net);complete=False
        self.state['seen_deals'].extend(d['id'] for d in deals);c['position']=None;c['first']=False;c['entries']=[];c['opened_ids']=[]
        if c.get('manual'):
            c['manual_closure']=dict(reason=reason,accounted_deals=[d['id'] for d in deals],target_satisfied=complete);self.state['phase']='MANUAL'
        elif reason=='MANUAL' or c.get('user_close_requested'):
            self.state['phase']='WAIT_TRIGGER' if c['operation_mode']=='semi' else 'PAUSED_CYCLE'
            c['user_close_requested']=False;c['replacement']=None
            if complete:
                self.state['completed'].append(dict(id=c['id'],currency=c['currency'],target='0',realized=c['realized'],forgiven_residual='0',reason='MANUAL'))
                self.state['cycle']=None
            self.state['stop']=True;self.state['stop_mode']='CANCEL'
            self.notify('Подтверждено ручное закрытие. RECOVERY обновлён по фактическому net; новые входы остановлены, ждём команду.')
        elif complete:
            self.state['completed'].append(dict(id=c['id'],currency=c['currency'],target='0',realized=c['realized'],forgiven_residual='0'))
            self.state['cycle']=None;self.state['phase']='WAIT_TRIGGER' if c['operation_mode']=='semi' else ('PAUSED' if self.state['stop'] else 'COOLDOWN')
            self.state['cooldown_until']=snap['now']+int(number(c['settings']['CYCLE_COOLDOWN_SECONDS']))
        else:
            cfg=RecoveryConfig.load(c['module_config'])
            c['recovery_levels']=recovery_triggers(Spec.load(snap['spec']),close,p['side'],'TP' if reason=='RECOVERY_TARGET' else reason,
                after_sl=cfg.after_sl,continuation=cfg.continuation,opposite=cfg.opposite)
            c['branch']='FIXED_RECOVERY';c['center']=str(close);self.state['phase']='NEXT_PAIR'
            if reason=='SL':self._start_branch_clock(self.clock())
        self.save('interactive_confirmed_closure')

    def _reconcile_commands(self,snap):
        super()._reconcile_commands(snap)
        for cmd in self.state['commands'].values():
            if cmd['kind']=='CLOSE' and cmd['status'] not in TERMINAL:
                exits=[d for d in snap['deals'] if d['entry']=='OUT' and d.get('close_command_id')==cmd['id'] and d['position_id']==cmd['position_id']]
                if not any(p['id']==cmd['position_id'] for p in snap['positions']):
                    all_exits=[d for d in snap['deals'] if d['entry']=='OUT' and d['position_id']==cmd['position_id']]
                    if sum((number(d['volume']) for d in exits),D(0))==number(cmd['size']):cmd['status']='DONE'
                    elif sum((number(d['volume']) for d in all_exits),D(0))==number(cmd['size']):
                        cmd.update(status='OBSOLETE',closed_by_deals=[d['id'] for d in all_exits])
        self.save('interactive_close_evidence')
