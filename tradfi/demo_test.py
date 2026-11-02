"""Bounded, separately journaled native DEMO diagnostics. No strategy execution."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import secrets
import threading
import time
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from .control import ActionGateway, ActionRejected, OwnerMailbox
from .storage import Store
from .native_time import NativeUTCVerifier


def reject(message):
    raise ActionRejected(message, public_message=message)


def number(value):
    try:
        if isinstance(value, bool):raise ValueError()
        result=Decimal(str(value))
        if not result.is_finite():raise ValueError()
        return result
    except (ValueError, InvalidOperation):reject('Нужно конечное числовое значение.')


class DemoMailbox(OwnerMailbox):
    def __init__(self, session):
        super().__init__();self.session=session
    def submit(self, request):
        from .control import validate_request
        request=validate_request(request)
        if request['action'].get('type')=='disable':
            # Authenticated disable preempts queued steps, even before owner drain.
            self.session.disarm()
        return super().submit(request)


class DemoSession:
    symbol='XAUUSDT'
    def __init__(self,path,api,*,expected_login,expected_server,live_safe,clock=time.time,monotonic=time.monotonic,safety=None):
        if type(expected_login) is not int or expected_login<=0 or not expected_server:raise ValueError('Explicit demo account binding required')
        self.api=api;self.expected=(expected_login,expected_server);self.live_safe=live_safe
        self.clock=clock;self.monotonic=monotonic;self.store=Store(path)
        self.identity=hashlib.sha256(repr(self.expected).encode()).hexdigest()
        self.state=self.store.load() or dict(schema=1,phase='DEMO_OFF',commands={},identity=self.identity,magic=secrets.randbelow(2**30)+2**30)
        if self.state.get('identity')!=self.identity:self.store.close();raise ValueError('Demo journal belongs to another account; preserve it')
        self.scope='demo-'+secrets.token_hex(16);self.enabled=False;self.off=threading.Event();self.off.set();self.epoch=0
        self.account_view={};self.spec_view={};self.quote={};self.inventory={'positions':[],'orders':[]};self.reason='Ожидание проверки DEMO';self.ready=False
        from .time_safety import TimeSafety
        self.safety=safety or TimeSafety(clock=clock,monotonic=monotonic)
        self.verifier=NativeUTCVerifier(SimpleNamespace(api=api,symbol=self.symbol,monotonic=monotonic,max_age=5),self.safety)
        self.safety.semantics_verifier=self.verifier
        self.next_time=0;self.mailbox=DemoMailbox(self);self.gateway=ActionGateway(self,execute=self.action)
        self.save('demo_start_off')
    def save(self,event):
        stamp=datetime.fromtimestamp(self.clock(),timezone.utc)
        self.state['demo_event_seq']=self.state.get('demo_event_seq',0)+1
        item=dict(sequence=self.state['demo_event_seq'],run_id=self.scope,utc=stamp.isoformat(),server_display_utc_plus_2=stamp.astimezone(timezone(timedelta(hours=2))).isoformat(),event=event)
        if self.state['commands']:
            row=next(reversed(self.state['commands'].values()))
            item.update(operation_id=row['id'],receipt_id=row.get('receipt'),operation=row['operation'],status=row['status'],retcode=row.get('retcode'))
        self.state.setdefault('demo_events',[]).append(item);self.state['demo_events']=self.state['demo_events'][-500:]
        self.store.save(self.state,event)
    def disarm(self):
        self.off.set();self.enabled=False;self.epoch+=1
    def close(self):self.disarm();self.store.close()
    @property
    def blocking(self):
        return any(r['status']=='UNKNOWN' for r in self.state.get('control_receipts',{}).values()) or (not self.ready and bool(self.state['commands'])) or self.enabled or bool(self.inventory['positions'] or self.inventory['orders']) or any(c['status']=='UNKNOWN' for c in self.state['commands'].values())
    def account(self):
        a=self.api.account_info();t=self.api.terminal_info()
        mode=getattr(a,'trade_mode',None)
        self.account_view=dict(mode='DEMO' if type(mode) is int and mode==self.api.ACCOUNT_TRADE_MODE_DEMO else 'НЕ DEMO / неизвестно',server=str(getattr(a,'server','—')),identifier='••'+str(getattr(a,'login',''))[-2:],margin_mode=getattr(a,'margin_mode',None))
        if a is None or t is None or not t.connected:self.disarm();reject('Нет соединения MT5. После восстановления включите проверку заново.')
        if type(mode) is not int or mode!=self.api.ACCOUNT_TRADE_MODE_DEMO:self.disarm();reject('Операции разрешены только при ACCOUNT_TRADE_MODE_DEMO.')
        if (a.login,a.server)!=self.expected:self.disarm();reject('Счёт изменился. Действия запрещены; верните привязанный DEMO-счёт.')
        if not t.trade_allowed or t.tradeapi_disabled or not a.trade_allowed or not a.trade_expert:self.disarm();reject('MT5 запрещает торговые операции API.')
        if a.margin_mode!=self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:reject('Диагностика доступна; торговые тесты требуют hedging. Netting/exchange не поддерживаются.')
        return a
    def owned(self, item):
        return item.symbol==self.symbol and item.magic==self.state['magic'] and any(item.comment==c['comment'] for c in self.state['commands'].values())
    def read_inventory(self):
        positions=self.api.positions_get(symbol=self.symbol);orders=self.api.orders_get(symbol=self.symbol)
        if positions is None or orders is None:reject('Состояние ордеров/позиций MT5 неизвестно.')
        foreign=any(not self.owned(x) for x in (*positions,*orders))
        self.inventory={k:[dict(ticket=x.ticket,type=x.type,volume=float(getattr(x,'volume',getattr(x,'volume_current',0))),price=float(x.price_open),sl=float(x.sl),tp=float(x.tp)) for x in rows if self.owned(x)] for k,rows in [('positions',positions),('orders',orders)]}
        return positions,orders,foreign
    def context(self):
        a=self.account()
        if not self.live_safe():self.disarm();reject('Основной контур активен или не сверён. Сначала безопасно остановите и сверьте его.')
        reason=self.safety.check()
        if reason:reject('Нет подтверждённого свежего UTC: '+str(reason))
        i=self.api.symbol_info(self.symbol);q=self.api.symbol_info_tick(self.symbol)
        if i is None or getattr(i,'name',None)!=self.symbol or not i.visible:reject('Точный символ XAUUSDT недоступен/не выбран в Обзоре рынка. Замена символа запрещена.')
        if q is None or not -2<=self.clock()-float(q.time_msc)/1000<=5 or not 0<=float(q.time_msc)/1000-q.time<1:reject('Ожидание свежей котировки XAUUSDT.')
        if number(q.bid)<=0 or number(q.ask)<number(q.bid):reject('Некорректная Bid/Ask котировка.')
        keys=['volume_min','volume_step','volume_max','point','trade_tick_size','trade_contract_size','trade_stops_level','trade_freeze_level','filling_mode','trade_exemode','trade_mode','order_mode','expiration_mode','trade_calc_mode']
        self.spec_view={k:float(number(getattr(i,k))) for k in keys}
        self.spec_view['currency_profit']=str(i.currency_profit)
        if any(number(getattr(i,k))<=0 for k in ['volume_min','volume_step','volume_max','point','trade_tick_size','trade_contract_size']):reject('Неполная спецификация XAUUSDT.')
        if number(i.volume_min)%number(i.volume_step) or i.volume_min>i.volume_max:reject('Минимальный объём не соответствует шагу брокера.')
        self.quote=dict(bid=q.bid,ask=q.ask,time_msc=q.time_msc,age_seconds=self.clock()-q.time_msc/1000)
        self.spec_view['margin_buy']=self.api.order_calc_margin(self.api.ORDER_TYPE_BUY,self.symbol,i.volume_min,q.ask)
        self.spec_view['margin_sell']=self.api.order_calc_margin(self.api.ORDER_TYPE_SELL,self.symbol,i.volume_min,q.bid)
        if any(v is None or number(v)<0 for v in [self.spec_view['margin_buy'],self.spec_view['margin_sell']]):reject('Оценка маржи недоступна.')
        positions,orders,foreign=self.read_inventory()
        if foreign:reject('Есть посторонние позиции/ордера XAUUSDT. Они не изменяются; тесты заблокированы.')
        return a,i,q,positions,orders
    def step(self):
        self.safety.poll_refresh()
        if self.monotonic()>=self.next_time:self.safety.request_refresh();self.next_time=self.monotonic()+30
        self.ready=False
        try:self.context();self.ready=True;self.reason='DEMO подтверждён. Рыночное исполнение окончательно подтверждает только брокер.'
        except ActionRejected as e:self.reason=e.public_message;self.disarm()
        except Exception:self.reason='Чтение MT5 недоступно; проверка выключена.';self.disarm()
        self.mailbox.drain(self.gateway,self.view)
    def inventory_token(self):
        return hashlib.sha256(json.dumps([self.inventory,self.spec_view],sort_keys=True).encode()).hexdigest()
    def view(self):
        return dict(last_rejection=deepcopy(self.state.get('last_rejection')),last_error=deepcopy(self.state.get('last_error')),time_block={'reason':self.verifier.reason},inventory_token=self.inventory_token(),scope=self.scope,revision=self.state.get('control_revision',0),epoch=self.epoch,symbol=self.symbol,enabled=self.enabled and not self.off.is_set(),ready=self.ready,reason=self.reason,account=deepcopy(self.account_view),spec=deepcopy(self.spec_view),quote=deepcopy(self.quote),**deepcopy(self.inventory),commands=deepcopy(list(self.state['commands'].values())[-30:]),action_results=deepcopy(self.state.get('control_receipts',{})))
    def action(self,action):
        try:return self._action(action)
        except ActionRejected as exc:
            message=str(exc)
            categories=[('UNKNOWN','UNKNOWN_UNRESOLVED'),('DEMO','DEMO_ACCOUNT_REQUIRED'),('счёт','ACCOUNT_CHANGED'),('SL/TP','PROTECTION_INVALID'),('freeze','FREEZE_OR_STOPS'),('STOP','STOP_INVALID'),('шаг','TICK_OR_VOLUME_STEP'),('Билет','OWNERSHIP_OR_STALE'),('снимок','STALE_SNAPSHOT'),('выключена','DISABLED'),('UTC','TIME_UNVERIFIED'),('retcode','BROKER_REJECTED')]
            code=next((code for text,code in categories if text in message),'VALIDATION_REJECTED')
            self.state['last_rejection']=dict(reason=code,receipt_id=next(reversed(self.state['control_receipts'])),operation=action.get('operation'),request={k:action[k] for k in ('ticket','price','sl','tp') if k in action})
            self.save('demo_action_rejected');raise
        except (OSError,TimeoutError) as exc:
            from .context_diagnostics import safe_trace
            self.state['last_error']=safe_trace(exc);self.save('demo_action_unknown');raise
    def _action(self,action):
        kind=action.get('type')
        if kind=='disable' and set(action)=={'type'}:self.disarm();return
        if kind=='enable' and set(action)=={'type','epoch'}:
            if action['epoch']!=self.epoch:reject('Переключатель устарел. Обновите снимок.')
            self.context()
            if any(c['status']=='UNKNOWN' for c in self.state['commands'].values()) or any(r['status']=='UNKNOWN' for r in list(self.state.get('control_receipts',{}).values())[:-1]):reject('UNKNOWN: необходима сверка; повтор отправки запрещён.')
            self.off.clear();self.enabled=True;return
        if kind=='reconcile' and set(action)=={'type'}:
            self.context();self.reconcile();return
        if set(action)!={'type','operation','ticket','price','sl','tp','epoch','inventory_token'} or kind!='test':reject('Неизвестная тестовая команда.')
        if not self.enabled or self.off.is_set() or action['epoch']!=self.epoch:reject('Проверка выключена или подтверждение устарело.')
        if any(c['status']=='UNKNOWN' for c in self.state['commands'].values()):reject('UNKNOWN: повтор запрещён до положительной сверки.')
        started=self.monotonic()
        a,i,q,positions,orders=self.context()
        if action['inventory_token']!=self.inventory_token():reject('Брокерский снимок изменился; повторите предпросмотр.')
        op=action['operation'];ticket=action['ticket']
        if type(ticket) is not int or ticket<0:reject('Неверный билет.')
        values={k:number(action[k]) for k in ('price','sl','tp')}
        tick=number(i.trade_tick_size);distance=max(number(i.trade_stops_level),number(i.trade_freeze_level))*number(i.point)
        for v in values.values():
            if v<0 or v%tick:reject('Цена должна соответствовать шагу цены брокера; автоматическое округление отключено.')
        if i.trade_mode!=self.api.SYMBOL_TRADE_MODE_FULL:reject('Символ не разрешает полный торговый режим.')
        request=dict(symbol=self.symbol,magic=self.state['magic'],deviation=0)
        pending=op in {'stop_buy','stop_sell','modify_stop'}
        if op in {'buy','sell','stop_buy','stop_sell'}:
            if positions or orders:reject('Сначала завершите предыдущий тест: допускается только одна позиция или STOP.')
            if ticket:reject('Новый тест не принимает билет.')
            buy=op in {'buy','stop_buy'}
            request.update(action=self.api.TRADE_ACTION_PENDING if pending else self.api.TRADE_ACTION_DEAL,type=(self.api.ORDER_TYPE_BUY_STOP if buy else self.api.ORDER_TYPE_SELL_STOP) if pending else (self.api.ORDER_TYPE_BUY if buy else self.api.ORDER_TYPE_SELL),volume=i.volume_min)
        elif op in {'protection','close','modify_stop','cancel'}:
            rows=orders if op in {'modify_stop','cancel'} else positions
            matches=[x for x in rows if x.ticket==ticket and self.owned(x)]
            if len(matches)!=1:reject('Билет не принадлежит этому тестовому журналу или уже изменился.')
            target=matches[0];buy=target.type in {self.api.ORDER_TYPE_BUY,self.api.ORDER_TYPE_BUY_STOP}
            freeze=number(i.trade_freeze_level)*number(i.point)
            reference=number(q.bid if buy else q.ask)
            frozen=(abs(number(target.price_open)-number(q.ask if buy else q.bid))<=freeze) if op in {'modify_stop','cancel'} else any(number(v)>0 and abs(number(v)-reference)<=freeze for v in (target.sl,target.tp))
            if freeze>0 and frozen:reject('Билет находится в freeze-зоне брокера.')
            if op=='close':request.update(action=self.api.TRADE_ACTION_DEAL,position=ticket,volume=target.volume,type=self.api.ORDER_TYPE_SELL if buy else self.api.ORDER_TYPE_BUY)
            elif op=='protection':request.update(action=self.api.TRADE_ACTION_SLTP,position=ticket)
            elif op=='cancel':request.update(action=self.api.TRADE_ACTION_REMOVE,order=ticket)
            else:request.update(action=self.api.TRADE_ACTION_MODIFY,order=ticket,type=target.type)
        else:reject('Неизвестная операция.')
        if op not in {'close','cancel'}:
            price=values['price'] if pending else number(q.bid if buy else q.ask)
            if pending and (not price or (price-number(q.ask) if buy else number(q.bid)-price)<=distance):reject('STOP слишком близко к котировке или уже пересечён.')
            if not values['sl'] or not values['tp']:reject('Для теста обязательны положительные SL и TP.')
            if (price-values['sl'] if buy else values['sl']-price)<=distance or (values['tp']-price if buy else price-values['tp'])<=distance:reject('SL/TP нарушают направление или stops/freeze дистанцию.')
            request.update(sl=float(values['sl']),tp=float(values['tp']))
            if pending:request.update(price=float(price),type_time=self.api.ORDER_TIME_GTC)
            if not pending and op!='protection':request['price']=q.ask if buy else q.bid
            required=(4 if pending else 1)|16|32 # documented SYMBOL_ORDER_STOP/MARKET/SL/TP flags
            if i.order_mode & required != required:reject('Брокер не разрешает необходимый тип ордера/SL/TP.')
            if pending and not i.expiration_mode & 1:reject('GTC для этого символа не разрешён.')
        if request['action']==self.api.TRADE_ACTION_DEAL:
            if op=='close':request['price']=q.bid if buy else q.ask
            if not i.filling_mode & 1:reject('FOK не поддерживается. Частичное исполнение для этого теста не запрашивается.')
            request['type_filling']=self.api.ORDER_FILLING_FOK
        elif pending:request['type_filling']=self.api.ORDER_FILLING_RETURN
        expected_inventory=self.inventory_token()
        token=secrets.token_hex(10);comment='dt-'+token;request['comment']=comment
        # Check permissions/identity AGAIN around read-only broker validation.
        self.account()
        checked=self.api.order_check(request)
        if checked is None or checked.retcode!=0:reject('Брокер отклонил предварительную проверку; код '+str(getattr(checked,'retcode','нет ответа')))
        self.account()
        self.read_inventory()
        if self.inventory_token()!=expected_inventory:reject('Билет/ордер изменился во время проверки.')
        latest=self.api.symbol_info_tick(self.symbol)
        if latest is None or (latest.bid,latest.ask)!=(q.bid,q.ask) or not 0<=self.monotonic()-started<=2 or self.safety.check():reject('Котировка/время изменились во время проверки; обновите предпросмотр.')
        if not self.live_safe() or self.off.is_set() or action['epoch']!=self.epoch:reject('Проверка остановлена до отправки.')
        row=dict(quote=deepcopy(self.quote),spec=deepcopy(self.spec_view),account={'mode':'DEMO','identifier':self.account_view['identifier']},id=token,receipt=next(reversed(self.state['control_receipts'])),comment=comment,status='UNKNOWN',operation=op,request=request,started=self.clock(),retcode=None)
        self.state['commands'][token]=row;self.save('demo_intent_before_send')
        # No API calls between final identity gate and send. A terminal account
        # switch inside native order_send itself cannot be made atomic by Python.
        try:
            self.account()
            if not 0<=self.monotonic()-started<=2:reject('Отправка просрочена; обновите предпросмотр.')
            if self.off.is_set() or action['epoch']!=self.epoch:reject('Проверка выключена до отправки.')
        except ActionRejected:
            row['status']='NOT_SENT';self.save('demo_not_sent');raise
        try:result=self.api.order_send(request)
        except Exception as exc:
            from .context_diagnostics import safe_trace
            row.update(safe_trace(exc));result=None
        if result is not None:
            row['result']={k:float(getattr(result,k)) for k in ('price','volume','bid','ask') if isinstance(getattr(result,k,None),(int,float)) and math.isfinite(getattr(result,k))}
            row['retcode']=int(result.retcode);row['order']=int(getattr(result,'order',0));row['deal']=int(getattr(result,'deal',0))
            if result.retcode in {self.api.TRADE_RETCODE_DONE,self.api.TRADE_RETCODE_PLACED}:row['status']='ACKNOWLEDGED'
            elif result.retcode in {10004,10006,10013,10014,10015,10016,10017,10018,10019,10020,10021,10022,10030,10035}:row['status']='REJECTED'
        self.save('demo_result')
        if row['status']=='UNKNOWN':self.disarm();raise OSError('Uncertain demo operation; no replay')
        if row['status']=='REJECTED':reject('Брокер отклонил тестовую операцию; retcode '+str(row['retcode']))
        try:
            self.context()
            # An acknowledgement alone cannot prove current exposure/flatness.
            # Slow native propagation is UNKNOWN until positive reconciliation.
            owned=self.inventory['positions']+self.inventory['orders']
            if row['status']=='ACKNOWLEDGED' and op in {'buy','sell','stop_buy','stop_sell'} and not owned:row['status']='UNKNOWN'
            if row['status']=='ACKNOWLEDGED' and op in {'close','cancel'} and any(x['ticket']==ticket for x in owned):row['status']='UNKNOWN'
            if row['status']=='ACKNOWLEDGED' and op=='protection' and any(x['ticket']==ticket and (number(x['sl'])!=values['sl'] or number(x['tp'])!=values['tp']) for x in owned):row['status']='UNKNOWN'
            if row['status']=='ACKNOWLEDGED' and op=='modify_stop' and any(x['ticket']==ticket and (number(x['price'])!=values['price'] or number(x['sl'])!=values['sl'] or number(x['tp'])!=values['tp']) for x in owned):row['status']='UNKNOWN'
            self.save('demo_observation')
            if row['status']=='UNKNOWN':raise OSError('Await positive broker reconciliation')
        except Exception:
            self.disarm();raise OSError('Post-send observation unavailable')
    def reconcile(self):
        # Positive evidence only. Absence never proves a failed/timed-out send.
        before=deepcopy(self.state)
        ps,os,_=self.read_inventory()
        for row in self.state['commands'].values():
            if row['status']!='UNKNOWN':continue
            if row['operation'] in {'buy','sell','stop_buy','stop_sell'}:
                matches=[x for x in (*ps,*os) if self.owned(x) and x.comment==row['comment']]
                if len(matches)==1:
                    row['status']='OBSERVED';row['observed_ticket']=matches[0].ticket
                    receipt=self.state.get('control_receipts',{}).get(row.get('receipt'))
                    if receipt and receipt['status']=='UNKNOWN':receipt['status']='OBSERVED'
        for row in self.state['commands'].values():
            if row['status']!='UNKNOWN':continue
            req=row['request'];op=row['operation'];target=req.get('position',req.get('order'))
            if op=='protection':
                if any(p.ticket==target and self.owned(p) and number(p.sl)==number(req['sl']) and number(p.tp)==number(req['tp']) for p in ps):row['status']='OBSERVED'
            elif op=='modify_stop':
                if any(o.ticket==target and self.owned(o) and number(o.price_open)==number(req['price']) and number(o.sl)==number(req['sl']) and number(o.tp)==number(req['tp']) for o in os):row['status']='OBSERVED'
            elif op=='cancel' and hasattr(self.api,'history_orders_get'):
                history=self.api.history_orders_get(ticket=target)
                if history and any(o.ticket==target and self.owned(o) and getattr(o,'state',None)==self.api.ORDER_STATE_CANCELED for o in history):row['status']='OBSERVED'
            elif op=='close' and hasattr(self.api,'history_deals_get'):
                deals=self.api.history_deals_get(position=target)
                matching=[d for d in deals or [] if d.position_id==target and self.owned(d) and d.comment==row['comment'] and d.entry==self.api.DEAL_ENTRY_OUT]
                if not any(p.ticket==target for p in ps) and sum((number(d.volume) for d in matching),Decimal(0))>=number(req['volume']):row['status']='OBSERVED'
        for rid,receipt in list(self.state.get('control_receipts',{}).items())[:-1]:
            if receipt['status']!='UNKNOWN':continue
            linked=[r for r in self.state['commands'].values() if r.get('receipt')==rid]
            if not linked:receipt['status']='REJECTED' # durable send intent always precedes native send
            elif all(r['status'] in {'ACKNOWLEDGED','OBSERVED','NOT_SENT','REJECTED'} for r in linked):
                receipt['status']='OBSERVED'
        try:self.account()
        except Exception:
            self.state=before;self.disarm();raise
        self.save('demo_read_only_reconcile')
