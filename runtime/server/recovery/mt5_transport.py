"""Concrete MT5-shaped transport, HARD LOCKED to injected test doubles.

No initialize/login/shutdown calls. Real MetaTrader5 module is rejected. This
implements request conversion, fresh preflight and snapshot normalization for
executor tests. Removing the test lock is NOT a supported deployment procedure.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from .mt5_plan import Symbol, Quote, pending, protect, cancel, close_position


class MT5Transport:
    def __init__(self, api):
        self.api = api
        self._approved = None
        self._binding = None
        self._test_lock()

    def _test_lock(self):
        if getattr(self.api, '_RECOVERY_TEST_DOUBLE', False) is not True:
            raise RuntimeError('Live MT5 transport is disabled; explicit test double required')

    def _account(self, expected):
        self._test_lock()
        a, t = self.api.account_info(), self.api.terminal_info()
        if a is None or t is None or not t.connected:
            raise ValueError('MT5 account/terminal unavailable')
        if a.login != expected['account'] or a.server != expected['server']:
            raise ValueError('Account/server changed')
        if a.margin_mode != self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise ValueError('Hedging account required')
        if not a.trade_allowed or not t.trade_allowed:
            raise ValueError('Trading disabled')
        return a

    def _symbol(self, name):
        s = self.api.symbol_info(name); q = self.api.symbol_info_tick(name)
        if s is None or q is None:
            raise ValueError('Symbol metadata/quote unavailable')
        symbol=Symbol(name,str(s.trade_tick_size),str(s.point),s.trade_stops_level,
                      s.trade_freeze_level,str(s.volume_min),str(s.volume_max),str(s.volume_step))
        return symbol, Quote(str(q.bid),str(q.ask))

    def _normalize_order(self, o):
        types={self.api.ORDER_TYPE_BUY_STOP:'ORDER_TYPE_BUY_STOP',self.api.ORDER_TYPE_SELL_STOP:'ORDER_TYPE_SELL_STOP'}
        states={self.api.ORDER_STATE_CANCELED:'cancelled',self.api.ORDER_STATE_FILLED:'filled'}
        return {'ticket':o.ticket,'magic':o.magic,'symbol':o.symbol,
                'type':types.get(o.type,'unsupported'),'price':str(o.price_open),
                'sl':str(o.sl),'tp':str(o.tp),'volume':str(o.volume_current),
                'volume_initial':str(getattr(o,'volume_initial',o.volume_current)),
                'state':states.get(o.state,'other')}

    def _normalize_position(self,p):
        if p.type not in (self.api.POSITION_TYPE_BUY,self.api.POSITION_TYPE_SELL):
            raise ValueError('Unsupported position side')
        return {'ticket':p.ticket,'identifier':p.identifier,'magic':p.magic,'symbol':p.symbol,
                'side':'BUY' if p.type==self.api.POSITION_TYPE_BUY else 'SELL',
                'entry':str(p.price_open),'volume':str(p.volume),'sl':str(p.sl),'tp':str(p.tp)}

    def _convert(self,request):
        result=deepcopy(request)
        for k in ('action','type','type_time','type_filling'):
            if k in result:
                if not isinstance(result[k],str) or not hasattr(self.api,result[k]):
                    raise ValueError('Unknown symbolic MT5 constant')
                result[k]=getattr(self.api,result[k])
        for k in ('price','sl','tp','volume'):
            if k in result:result[k]=float(result[k])
        return result

    def _order_check(self,request):
        return self.api.order_check(self._convert(request))

    def _order_send(self,request):
        return self.api.order_send(self._convert(request))

    def preflight(self,request,expected):
        self._approved=None
        self._account(expected)
        action=request['action']
        for field in ('symbol','magic'):
            if field in request and (not expected.get(field) or request[field]!=expected[field]):
                raise ValueError('Request scope differs from expected '+field)
        if action=='TRADE_ACTION_DEAL' and any(not request.get(k) for k in ('symbol','magic')):
            raise ValueError('Close request requires symbol and magic')
        if action=='TRADE_ACTION_PENDING':
            if any(not expected.get(k) or request.get(k)!=expected[k] for k in ('symbol','magic')):
                raise ValueError('Pending request scope differs from expected binding')
            s,q=self._symbol(request['symbol'])
            side='BUY' if request['type']=='ORDER_TYPE_BUY_STOP' else 'SELL' if request['type']=='ORDER_TYPE_SELL_STOP' else None
            if side is None:raise ValueError('Only stop orders supported')
            pending(dict(request,id='preflight',side=side),s,q,request['magic'])
            if request.get('tp') not in (0,0.0,'0'):
                raise ValueError('RECOVERY has no automatic TP')
        elif action=='TRADE_ACTION_SLTP':
            rows=self.api.positions_get(ticket=request['position'])
            if rows is None or len(rows)!=1:raise ValueError('Position absent or ambiguous')
            p=self._normalize_position(rows[0]);p['stop_stage']=expected.get('confirmed_stop_stage',0)
            s,q=self._symbol(expected['symbol'])
            validated=protect(p,request['sl'],s,q,expected_ticket=request['position'],
                expected_identifier=expected['identifier'],expected_magic=expected['magic'])
            # Unchanged TP must be read freshly, never overwritten from stale intent.
            if float(validated['tp'])!=float(request['tp']):raise ValueError('TP changed; refresh intent')
        elif action=='TRADE_ACTION_DEAL':
            rows=self.api.positions_get(ticket=request['position'])
            if rows is None or len(rows)!=1:raise ValueError('Close position absent or ambiguous')
            p=self._normalize_position(rows[0]);s,q=self._symbol(expected['symbol'])
            verified=close_position(p,s,q,expected_ticket=request['position'],expected_identifier=expected['identifier'],expected_magic=expected['magic'],filling=request['type_filling'])
            if any(str(verified[f])!=str(request[f]) for f in ('type','volume','price')):
                raise ValueError('Close snapshot changed; refresh explicit request')
        elif action=='TRADE_ACTION_REMOVE':
            rows=self.api.orders_get(ticket=request['order'])
            if rows is None or len(rows)!=1:raise ValueError('Order absent or ambiguous')
            cancel(self._normalize_order(rows[0]),expected_ticket=request['order'],
                   expected_magic=expected['magic'],expected_symbol=expected['symbol'])
        else:raise ValueError('Unsupported trade action')
        result=self._order_check(request)
        if result is None or result.retcode!=0:raise ValueError('MT5 order_check rejected or unavailable')
        self._approved=deepcopy(request);self._binding=deepcopy(expected)

    def send(self,request):
        self._test_lock()
        if self._approved is None or request!=self._approved:
            raise ValueError('Fresh preflight for this exact request required')
        approved=self._approved;binding=self._binding
        # Consume even if the account guard or send fails. Never reusable capability.
        self._approved=None;self._binding=None
        # Revalidate quote/ownership immediately before send as well. This still
        # cannot eliminate the final broker race, so reconcile afterwards.
        self.preflight(approved,binding)
        self._approved=None;self._binding=None
        result=self._order_send(approved)
        if result is None:return {'status':'unknown'}
        if result.retcode in (self.api.TRADE_RETCODE_DONE,self.api.TRADE_RETCODE_PLACED):
            return {'status':'accepted','order':result.order,'deal':result.deal,'retcode':result.retcode}
        # Partial completion and timeout are not safe definitive rejections.
        if result.retcode in (self.api.TRADE_RETCODE_DONE_PARTIAL,self.api.TRADE_RETCODE_TIMEOUT):
            return {'status':'unknown','order':result.order,'deal':result.deal,'retcode':result.retcode}
        # Unknown retcodes must not become definitive failure/retry permission.
        rejected_names=('TRADE_RETCODE_REJECT','TRADE_RETCODE_INVALID',
            'TRADE_RETCODE_INVALID_VOLUME','TRADE_RETCODE_INVALID_PRICE',
            'TRADE_RETCODE_INVALID_STOPS','TRADE_RETCODE_TRADE_DISABLED',
            'TRADE_RETCODE_MARKET_CLOSED','TRADE_RETCODE_NO_MONEY')
        rejected={getattr(self.api,n) for n in rejected_names if hasattr(self.api,n)}
        return {'status':'rejected' if result.retcode in rejected else 'unknown','retcode':result.retcode}

    def snapshot(self,expected):
        a=self._account(expected)
        orders=self.api.orders_get();positions=self.api.positions_get()
        # Prototype bounded read; a production cursor must persist overlap and catch
        # history older than this bound after long downtime. Unknown stays unknown.
        now=datetime.now(timezone.utc)
        history=self.api.history_orders_get(now-timedelta(days=30),now)
        if orders is None or positions is None or history is None:
            raise ValueError('Incomplete broker snapshot')
        closes=[]
        if hasattr(self.api,'history_deals_get'):
            deals=self.api.history_deals_get(now-timedelta(days=30),now)
            if deals is None:raise ValueError('Deal history unavailable')
            closes=[{'ticket':d.ticket,'order':d.order,'position_identifier':d.position_id,
                     'symbol':d.symbol,'entry':'OUT','volume':str(d.volume)}
                    for d in deals if d.entry==self.api.DEAL_ENTRY_OUT]
        return {'account':a.login,'server':a.server,'close_deals':closes,
                'orders':[self._normalize_order(o) for o in orders],
                'positions':[self._normalize_position(p) for p in positions],
                'history_orders':[self._normalize_order(o) for o in history]}
