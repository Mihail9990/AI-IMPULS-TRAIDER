"""Persistent paper exchange. Receives immutable market facts, never an MT5 object."""
from copy import deepcopy
from decimal import Decimal as D
import hashlib,json,uuid
from .recovery_v3 import ControlledRecoveryController
from .control import ActionRejected,ActionGateway,OwnerMailbox
from .broker import Spec
from .settings import number
from .engine import TERMINAL
from .storage import Store,DurabilityError

class PaperExchange:
    backend_id='paper_mt5_quotes_v1'
    read_only=False
    symbol='XAUUSD.s'
    def __init__(self,store,*,clock):
        self.store=store;self.clock=clock;self.market=None;self.available=False
        store.db.execute('CREATE TABLE IF NOT EXISTS paper_exchange (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        row=store.db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()
        self.p=json.loads(row[0]) if row else dict(identity='PAPER-'+uuid.uuid4().hex,orders=[],positions=[],deals=[],order_history=[],results={},cursor=None,cursor_keys=[],commission_per_lot_side='0',paused_gap=False)
        self.account=self.p['identity'];self.boot=True
        if row and (self.p['orders'] or self.p['positions']):self.p['paused_gap']=True
        self.persist()
    def persist(self):
        try:self.store.db.execute('INSERT OR REPLACE INTO paper_exchange VALUES(1,?)',(json.dumps(self.p,sort_keys=True,allow_nan=False),))
        except Exception as exc:raise DurabilityError('Paper persistence failed; stop and reload') from exc
    def profit(self,side,size,entry,close):
        if self.market is None:raise OSError('No verified MT5 specification')
        spec=Spec.load(self.market['spec']);movement=(number(close)-number(entry))*(1 if side=='BUY' else -1)
        # Frozen current native tick-value model; never claimed actual execution P&L.
        value=spec.tick_value_profit if movement>=0 else spec.tick_value_loss
        return movement/spec.tick_size*number(size)*value
    def _fee(self,size):return str(-number(size)*number(self.p['commission_per_lot_side']))
    def _close(self,p,reason,price,close_id=None):
        self.p['positions'].remove(p)
        self.p['deals'].append(dict(id='out-'+p['id'],position_id=p['id'],order_id='exit-'+p['id'],command_id='',close_command_id=close_id,
            entry='OUT',side=p['side'],volume=p['size'],price=str(price),reason=reason,profit=str(self.profit(p['side'],p['size'],p['entry'],price)),
            commission=self._fee(p['size']),swap='0',fee='0',sl=p['sl'],tp=p['tp'],time=self.clock()))
    def _fill(self,o,price):
        self.p['orders'].remove(o);o['status']='FILLED';self.p['order_history'].append(deepcopy(o));pid='paper-p-'+o['id']
        self.p['positions'].append(dict(id=pid,side=o['side'],entry=str(price),size=o['size'],sl=o['sl'],tp=o['tp'],owned=True))
        self.p['deals'].append(dict(id='in-'+pid,position_id=pid,order_id=o['id'],command_id=o['command_id'],entry='IN',side=o['side'],volume=o['size'],price=str(price),reason='EXPERT',profit='0',commission=self._fee(o['size']),swap='0',fee='0',time=self.clock()))
    def observe(self,market):
        if market['symbol']!=self.symbol:raise ValueError('Wrong paper symbol')
        Spec.load(market['spec'])
        if self.p['cursor'] is not None and market['tick_msc']<self.p['cursor']:return
        self.market=deepcopy(market)
        self.available=market['status']=='FRESH' and -2<=self.clock()-market['timestamp']<=5
        if not self.available:return
        stamp=market['tick_msc'];key=hashlib.sha256(json.dumps(market['quote'],sort_keys=True).encode()).hexdigest()
        previous=self.p['cursor']
        if previous is not None and (stamp<previous or (stamp==previous and key in self.p['cursor_keys'])):return
        if previous is not None and stamp-previous>5000 and (self.p['orders'] or self.p['positions']):self.p['paused_gap']=True
        # First sample after restart is a baseline, never an inferred historical fill.
        baseline=self.boot;self.boot=False
        if previous!=stamp:self.p['cursor_keys']=[]
        self.p['cursor']=stamp;self.p['cursor_keys'].append(key)
        if baseline or self.p['paused_gap']:self.persist();return
        q=market['quote']
        def close_crossed():
            for p in list(self.p['positions']):
                price=number(q['bid' if p['side']=='BUY' else 'ask']);sign=1 if p['side']=='BUY' else -1
                if sign*(price-number(p['sl']))<=0:self._close(p,'SL',price)
                elif sign*(price-number(p['tp']))>=0:self._close(p,'TP',p['tp'])  # no favorable gap windfall at TP
        close_crossed()
        for o in list(self.p['orders']):
            price=number(q['ask' if o['side']=='BUY' else 'bid']);sign=1 if o['side']=='BUY' else -1
            if sign*(price-number(o['price']))>=0:self._fill(o,price)
        close_crossed()  # gap may cross a newly filled ticket's unchanged SL
        self.persist()
    def snapshot(self):
        if not self.available or self.market is None or not -2<=self.clock()-self.market['timestamp']<=5:raise OSError('Paper waits for verified fresh MT5 data')
        return dict(account=self.account,hedging=True,history_complete=True,spec=deepcopy(self.market['spec']),now=self.clock(),quote=deepcopy(self.market['quote']),candle=deepcopy(self.market['candle']),
            **{k:deepcopy(self.p[k]) for k in ('orders','positions','deals','order_history')})
    def result(self,cid):return deepcopy(self.p['results'].get(cid,{'status':'UNKNOWN'}))
    def submit(self,cmd):
        if cmd['id'] in self.p['results']:return  # durable idempotency, no second paper effect
        kind=cmd['kind'];result={'status':'REJECTED','never_sent':True}
        try:
            snap=self.snapshot();spec=Spec.load(snap['spec']);q=snap['quote']
            if self.p['paused_gap'] and kind not in {'CANCEL'}:raise ValueError('Paper gap needs confirmation')
            if kind in {'STOP','MARKET'}:
                if cmd['side'] not in {'BUY','SELL'}:raise ValueError('side')
                size=number(cmd['size']);price=spec.price(q['ask' if cmd['side']=='BUY' else 'bid'] if kind=='MARKET' else cmd['price']);sign=1 if cmd['side']=='BUY' else -1
                if not spec.volume_min<=size<=spec.volume_max or size%spec.volume_step:raise ValueError('volume')
                if kind=='STOP' and sign*(price-number(q['ask' if sign==1 else 'bid']))<max(spec.tick_size,spec.point*spec.stops_level):raise ValueError('crossed STOP')
                spec.protection(cmd['side'],price,number(cmd['sl']),number(cmd['tp']),q if kind=='MARKET' else None)
                oid='paper-o-'+cmd['id'];self.p['orders'].append(dict(id=oid,command_id=cmd['id'],side=cmd['side'],status='PENDING',size=str(size),price=str(price),sl=cmd['sl'],tp=cmd['tp'],owned=True))
                result=dict(status='ACCEPTED',order_id=oid)
                if kind=='MARKET':self._fill(self.p['orders'][-1],price)
            elif kind=='CANCEL':
                o=next((o for o in self.p['orders'] if o['id']==cmd['order_id']),None)
                if o:
                    reference=number(q['ask' if o['side']=='BUY' else 'bid'])
                    if abs(number(o['price'])-reference)<spec.point*spec.freeze_level:raise ValueError('freeze')
                    self.p['orders'].remove(o);o['status']='CANCELLED';self.p['order_history'].append(o)
                result=dict(status='DONE')
            elif kind in {'MODIFY','CLOSE'}:
                p=next(p for p in self.p['positions'] if p['id']==cmd['position_id'])
                if kind=='MODIFY':
                    spec.protection(p['side'],number(p['entry']),number(cmd['sl']),number(cmd['tp']),q,modify=True);p.update(sl=cmd['sl'],tp=cmd['tp']);result=dict(status='DONE')
                elif number(cmd['size'])==number(p['size']):
                    self._close(p,cmd['purpose'],q['bid' if p['side']=='BUY' else 'ask'],cmd['id']);result=dict(status='ACCEPTED')
        except (ValueError,StopIteration):pass
        self.p['results'][cmd['id']]=result;self.persist()

class PaperSession:
    def __init__(self,path,reader,*,clock,monotonic):
        self.store=Store(path);self.reader=reader;self.clock=clock;self.monotonic=monotonic
        existing=self.store.load()
        if existing and existing.get('backend')!=PaperExchange.backend_id:
            self.store.close();raise DurabilityError('Paper cannot adopt another controller database')
        self.exchange=PaperExchange(self.store,clock=clock)
        self.controller=ControlledRecoveryController(self.store,self.exchange,clock=clock,monotonic=monotonic)
        self.scope=self.exchange.account;self.gateway=ActionGateway(self.controller,execute=self.action);self.mailbox=OwnerMailbox();self.snapshot=None
        self.presentation=None
    def _fresh(self):
        try:return self.exchange.snapshot()
        except OSError as exc:raise ActionRejected('Ожидание данных MT5',public_message='Ожидаются проверенные свежие котировки MT5; бумажное действие не выполнено.') from exc
    def action(self,a):
        if not isinstance(a,dict):raise ActionRejected('Неверная бумажная команда')
        if self.controller.state.get('migration_block'):raise ActionRejected(self.controller.state['migration_block'])
        if a.get('type')=='paper_costs' and set(a)=={'type','commission_per_lot_side'}:
            try:value=number(a['commission_per_lot_side']);assert value>=0
            except (ValueError,AssertionError):raise ActionRejected('Нужна неотрицательная модель комиссии')
            self.exchange.p['commission_per_lot_side']=str(value);self.exchange.persist();return
        if a==dict(type='paper_resume'):
            self._fresh()
            if self.controller._deadline_guard():raise ActionRejected('Нужна ручная сверка: цикл ограничен временем или уже остановлен защитой')
            self.exchange.p['paused_gap']=False;self.exchange.persist();return
        if a==dict(type='paper_flatten',confirmed=True):
            self._fresh()
            for o in list(self.exchange.p['orders']):self.exchange.p['orders'].remove(o);o['status']='CANCELLED';self.exchange.p['order_history'].append(o)
            for p in list(self.exchange.p['positions']):self.exchange._close(p,'MANUAL',self.exchange.market['quote']['bid' if p['side']=='BUY' else 'ask'])
            self.exchange.persist();self.controller.state['stop']=True;self.controller.save('paper_flatten');return
        if a==dict(type='command',text='/reset_manual'):
            c=self.controller.state.get('cycle')
            if not c or not c.get('manual'):raise ActionRejected('Нет бумажного цикла для сверки')
            snap=self._fresh()
            if snap['orders'] or snap['positions']:raise ActionRejected('Сначала закройте бумажные обязательства')
            if any(cmd['status']=='UNKNOWN' and self.exchange.result(cid)['status']=='UNKNOWN' for cid,cmd in self.controller.state['commands'].items()):raise ActionRejected('Неизвестный исход сохранён')
            ids=set(c['entries']);pids={d['position_id'] for d in snap['deals'] if d['entry']=='IN' and d['command_id'] in ids}
            net=sum((sum((number(d[k]) for k in ('profit','commission','swap','fee')),D(0)) for d in snap['deals'] if d['position_id'] in pids),D(0))
            s=self.controller.state;carry=max(D(0),number(s.get('basket_carry','0'))-net);s['basket_carry']=str(carry)
            s['completed'].append(dict(id=c['id'],currency='UST',contract='ticket_basket_v1',recovery_retained=str(carry),completion='PAPER_MANUAL_RESET'))
            for cmd in s['commands'].values():
                if cmd['status'] not in TERMINAL:cmd['status']='OBSOLETE'
            s.update(cycle=None,phase='WAIT_START',stop=True);self.controller.save('paper_manual_reset');return
        if a==dict(type='command',text='/resume_time'):
            if self.reader.safety.check():raise ActionRejected('Ожидаются проверенные свежие данные MT5')
            return
        if self.exchange.p['paused_gap'] and a==dict(type='command',text='/stop'):
            self.controller.command('/stop')
            for o in list(self.exchange.p['orders']):
                self.exchange.p['orders'].remove(o);o['status']='CANCELLED';self.exchange.p['order_history'].append(o)
            self.exchange.persist();return
        if self.exchange.p['paused_gap'] and a!=dict(type='command',text='/stop'):raise ActionRejected('После перерыва подтвердите продолжение симуляции без восстановления пропущенных тиков')
        if a!=dict(type='command',text='/stop'):self._fresh()
        return self.controller.action(a)
    def step(self):
        try:
            market=self.reader.read();self.exchange.observe(market)
            if not self.exchange.p['paused_gap']:self.controller.tick()
            self.snapshot=self.exchange.snapshot()
        except Exception:self.exchange.available=False;self.reader.reason=self.reader.reason or 'WAIT_FRESH_MT5'
        self.mailbox.drain(self.gateway,self.view)
    def view(self):
        from .web_control import NativePanel
        if self.presentation is None:
            self.presentation=object.__new__(NativePanel);self.presentation.controller=self.controller;self.presentation.scope=self.scope
        s=self.controller.state;c=s.get('cycle') or {};snap=self.snapshot or {k:deepcopy(self.exchange.p[k]) for k in ('orders','positions')}
        v=self.presentation.basket_view(s,c,snap,self.reader.view())
        v.update(mode='PAPER',source='PAPER_ON_MT5_READ_ONLY',paper=dict(paused_gap=self.exchange.p['paused_gap'],commission_per_lot_side=self.exchange.p['commission_per_lot_side'],cost_model='modeled commission per lot per side; swap/fee=0',history=deepcopy(self.exchange.p['deals'][-100:]),cursor=self.exchange.p['cursor']))
        return v
    def close(self):self.store.close()
