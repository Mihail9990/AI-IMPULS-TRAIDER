"""Official MT5 Python transport. No EA, credentials, file mailbox or implicit retries.

The module imports without MetaTrader5 installed. Real API import happens in connect;
Linux tests inject an API double. All money remains in the actual UST account currency.
"""
from datetime import datetime, timezone
from decimal import Decimal as D
import importlib
import json
import ntpath
import re
import time

from .broker import Spec
from .settings import number
from .storage import DurabilityError


class NotSent(ValueError):
    """Definitive local refusal before order_send, never a retry of an uncertain send."""


def plain(value):
    if hasattr(value, '_asdict'):
        return {k: plain(v) for k, v in value._asdict().items()}
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, 'item'):
        return value.item()
    return value


def encoded(value):
    return json.dumps(plain(value), sort_keys=True, allow_nan=False)


def dec(value):
    return str(number(value))


class MT5Broker:
    backend_id = 'mt5_direct_v1'
    max_age = 5

    def __init__(self, config, store=None, *, api=None, clock=time.time):
        self.config, self.store, self.api, self.clock = dict(config), store, api, clock
        self.enable_trading = config.get('enable_trading', False)
        if not isinstance(self.enable_trading, bool):
            raise ValueError('enable_trading must be a boolean')
        if config.get('backend') != self.backend_id:
            raise ValueError('Explicit mt5_direct_v1 backend required')
        self.path = config.get('terminal_path', '')
        self.server = config.get('expected_server', '')
        self.login = config.get('expected_login')
        self.symbol = config.get('symbol', 'XAUUSD.s')
        self.magic = config.get('magic', 25101102)
        if not self.path or not self.server or self.symbol != 'XAUUSD.s':
            raise ValueError('Explicit terminal path, server and XAUUSD.s required')
        if isinstance(self.magic, bool) or not isinstance(self.magic, int) or not 0 < self.magic < 2**63:
            raise ValueError('Invalid strategy magic')
        if self.login is not None and (isinstance(self.login, bool) or not isinstance(self.login, int) or self.login <= 0):
            raise ValueError('expected_login must be a positive integer or null for read-only inspection')
        if self.enable_trading and (self.login is None or config.get('legacy_ea_removed') is not True or config.get('legacy_state_reconciled') is not True or store is None):
            raise ValueError('Execution requires bound login, durable store and explicit legacy EA/state reconciliation acknowledgements')
        self.account = None
        self.connected = False
        self.rows = {}
        self.facts = {'order': {}, 'deal': {}}
        if store is not None:
            saved = store.load()
            if saved is not None:
                if saved.get('backend') != self.backend_id:
                    raise DurabilityError('Legacy/incompatible backend state: preserve and reconcile; no migration or replay')
                money_records=[saved,*saved.get('completed',[])]
                if saved.get('cycle') is not None:
                    money_records.append(saved['cycle'])
                if any(r.get('currency')!='UST' for r in money_records):
                    raise DurabilityError('Incompatible saved monetary currency; no conversion or relabel')
                expected=str(self.login)+'@'+self.server if self.login is not None else None
                if expected and saved.get('account') not in (None,expected):
                    raise DurabilityError('Saved account differs from configured identity')
            store.db.execute('CREATE TABLE IF NOT EXISTS mt5_binding (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            store.db.execute('CREATE TABLE IF NOT EXISTS mt5_commands (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            store.db.execute('CREATE TABLE IF NOT EXISTS mt5_facts (kind TEXT, ticket TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,ticket))')
            store.db.execute('CREATE TABLE IF NOT EXISTS mt5_events (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            self.rows = {cid: json.loads(payload) for cid, payload in store.db.execute('SELECT id,payload FROM mt5_commands')}
            for kind, ticket, payload in store.db.execute('SELECT kind,ticket,payload FROM mt5_facts'):
                self.facts[kind][ticket] = json.loads(payload)
            for cid,row in self.rows.items():
                command=(saved or {}).get('commands',{}).get(cid)
                if (not command or command.get('status')=='READY'
                        or row.get('account')!=(saved or {}).get('account')
                        or any(command.get(k)!=v for k,v in row['command'].items())):
                    raise DurabilityError('Controller snapshot and MT5 journal disagree; preserve and reconcile')

    def _transaction(self, fn):
        try:
            self.store.db.execute('BEGIN IMMEDIATE')
            fn(self.store.db)
            self.store.db.execute('COMMIT')
        except Exception as exc:
            if self.store.db.in_transaction:
                self.store.db.execute('ROLLBACK')
            raise DurabilityError('MT5 journal commit failed; halt and reload') from exc

    def _save_row(self, cid, row, event):
        payload = encoded(row)
        def save(db):
            db.execute('INSERT OR REPLACE INTO mt5_commands VALUES (?,?)', (cid, payload))
            db.execute('INSERT INTO mt5_events(payload) VALUES (?)', (encoded(dict(id=cid,event=event,row=row)),))
        self._transaction(save)
        self.rows[cid] = json.loads(payload)

    def _binding(self):
        return dict(backend=self.backend_id, account=self.account, symbol=self.symbol, magic=self.magic,
                    terminal=ntpath.normcase(ntpath.normpath(self.path)), currency='UST')

    def connect(self):
        if self.connected:
            self._context()
            return
        if self.api is None:
            try:
                self.api = importlib.import_module('MetaTrader5')
            except ImportError as exc:
                raise OSError('Install the Windows MetaTrader5 requirements in this Python environment') from exc
        if not self.api.initialize(self.path, timeout=10000):
            self.api.shutdown()
            raise OSError('MT5 initialize failed')
        self.connected = True
        try:
            self._context()
            if self.store is not None:
                binding = self._binding()
                row = self.store.db.execute('SELECT payload FROM mt5_binding WHERE id=1').fetchone()
                if row and json.loads(row[0]) != binding:
                    raise DurabilityError('MT5 journal identity/backend mismatch; no migration or replay')
                if not row:
                    if self.rows or any(self.facts.values()):
                        raise DurabilityError('MT5 journal binding missing')
                    self._transaction(lambda db: db.execute('INSERT INTO mt5_binding VALUES (1,?)', (encoded(binding),)))
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.connected:
            self.api.shutdown()
            self.connected = False

    def _context(self, mutation=False):
        if not self.connected:
            raise OSError('MT5 adapter is not connected')
        terminal, account = self.api.terminal_info(), self.api.account_info()
        if terminal is None or account is None or not terminal.connected:
            raise OSError('MT5 terminal unavailable/disconnected')
        terminal_dir = ntpath.normcase(ntpath.normpath(terminal.path))
        if terminal_dir != ntpath.normcase(ntpath.dirname(ntpath.normpath(self.path))):
            raise ValueError('Unexpected MT5 terminal path')
        identity = str(account.login) + '@' + account.server
        if account.server != self.server or (self.login is not None and account.login != self.login):
            raise ValueError('Unexpected MT5 account/server')
        if self.account is not None and identity != self.account:
            raise ValueError('MT5 account changed')
        if account.currency != 'UST' or account.margin_mode != self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise ValueError('UST hedging account required')
        self.account = identity
        if mutation and (not self.enable_trading or not terminal.trade_allowed or terminal.tradeapi_disabled
                         or not account.trade_allowed or not account.trade_expert):
            raise NotSent('Trading permissions disabled')
        info = self.api.symbol_info(self.symbol)
        if info is None or not info.visible:
            raise ValueError('XAUUSD.s must already be visible in Market Watch')
        tick = self.api.symbol_info_tick(self.symbol)
        if tick is None or abs(self.clock() - tick.time_msc / 1000) > self.max_age:
            raise TimeoutError('MT5 quote is stale/unavailable')
        if number(tick.bid) <= 0 or number(tick.ask) < number(tick.bid):
            raise ValueError('Invalid executable quote')
        spec = dict(symbol=self.symbol, currency=account.currency, profit_currency=info.currency_profit,
                    calc_mode=str(info.trade_calc_mode), contract_size=dec(info.trade_contract_size),
                    tick_size=dec(info.trade_tick_size), tick_value_profit=dec(info.trade_tick_value_profit),
                    tick_value_loss=dec(info.trade_tick_value_loss), volume_min=dec(info.volume_min),
                    volume_step=dec(info.volume_step), volume_max=dec(info.volume_max), point=dec(info.point),
                    stops_level=dec(info.trade_stops_level), freeze_level=dec(info.trade_freeze_level))
        Spec.load(spec)
        return terminal, account, info, tick, spec

    def _entry_cid(self, record):
        """Magic alone never establishes ownership. Journal and exact correlation required."""
        if record.get('symbol') != self.symbol or record.get('magic') != self.magic:
            return ''
        matches = []
        for cid, row in self.rows.items():
            cmd = row['command']
            if cmd['kind'] not in {'STOP', 'MARKET'} or row.get('send_started') is not True:
                continue
            reply = row.get('raw_result') or {}
            if record.get('comment') == cid or (reply.get('order') and str(reply['order']) == str(record.get('ticket'))):
                side_types = (self.api.ORDER_TYPE_BUY, self.api.ORDER_TYPE_BUY_STOP) if cmd['side']=='BUY' else (self.api.ORDER_TYPE_SELL, self.api.ORDER_TYPE_SELL_STOP)
                if record['type'] not in side_types or number(record['volume_initial']) != number(cmd['size']):
                    raise ValueError('Conflicting order ownership evidence')
                matches.append(cid)
        if len(matches) > 1:
            raise ValueError('Ambiguous order ownership')
        return matches[0] if matches else ''

    def _cache(self, kind, records):
        for r in records:
            ticket = str(r['ticket'])
            old = self.facts[kind].get(ticket)
            if old is not None and old != r:
                # Historical pending/partial orders can advance; final facts are immutable.
                changing = (kind=='order' and old['state'] in (self.api.ORDER_STATE_STARTED,
                            self.api.ORDER_STATE_PLACED, self.api.ORDER_STATE_PARTIAL,
                            self.api.ORDER_STATE_REQUEST_ADD, self.api.ORDER_STATE_REQUEST_MODIFY,
                            self.api.ORDER_STATE_REQUEST_CANCEL))
                if not changing:
                    raise ValueError('Conflicting MT5 historical fact')
            self.facts[kind][ticket] = r
        if self.store is not None:
            self._transaction(lambda db: db.executemany('INSERT OR REPLACE INTO mt5_facts VALUES (?,?,?)',
                [(kind,str(r['ticket']),encoded(r)) for r in records]))

    def snapshot(self):
        _, _, info, tick, spec = self._context()
        start = datetime.fromtimestamp(0, timezone.utc)
        end = datetime.fromtimestamp(self.clock()+60, timezone.utc)
        orders = self.api.orders_get(symbol=self.symbol)
        positions = self.api.positions_get(symbol=self.symbol)
        history = self.api.history_orders_get(start,end)
        deals = self.api.history_deals_get(start,end)
        rates = self.api.copy_rates_from_pos(self.symbol,self.api.TIMEFRAME_M1,1,1)
        if any(x is None for x in (orders,positions,history,deals,rates)) or len(rates)!=1:
            raise OSError('Incomplete MT5 snapshot/history')
        # Recheck identity and freshness after potentially slow reads.
        self._context()
        history = [plain(x) for x in history if x.symbol==self.symbol]
        deals = [plain(x) for x in deals if x.symbol==self.symbol]
        self._cache('order',history); self._cache('deal',deals)
        all_orders = dict(self.facts['order'])
        all_orders.update({str(x.ticket):plain(x) for x in orders})
        mapping = {ticket:self._entry_cid(r) for ticket,r in all_orders.items()}
        seen_cids = {}
        for ticket,cid in mapping.items():
            if cid:
                if cid in seen_cids and seen_cids[cid]!=ticket:
                    raise ValueError('One command correlated with multiple order tickets')
                seen_cids[cid]=ticket
        # Successful send tickets survive truncated broker comments and history lag.
        for cid,row in self.rows.items():
            ticket = (row.get('raw_result') or {}).get('order')
            if row['command']['kind'] in {'STOP','MARKET'} and row.get('send_started') and ticket:
                key=str(ticket)
                if key in mapping and mapping[key]!=cid:
                    raise ValueError('Returned order ticket conflicts with broker ownership')
                mapping[key]=cid
        opening_ids = {}
        converted_deals = []
        for r in self.facts['deal'].values():
            cid = mapping.get(str(r['order']), '')
            entry = {self.api.DEAL_ENTRY_IN:'IN',self.api.DEAL_ENTRY_OUT:'OUT',getattr(self.api,'DEAL_ENTRY_OUT_BY',3):'OUT'}.get(r['entry'],'OTHER')
            if entry=='IN' and cid:
                cmd=self.rows[cid]['command']
                expected=self.api.DEAL_TYPE_BUY if cmd['side']=='BUY' else self.api.DEAL_TYPE_SELL
                if r['magic']!=self.magic or r['type']!=expected:
                    raise ValueError('Conflicting opening deal ownership')
                opening_ids.setdefault(str(r['position_id']),set()).add(cid)
            reason={self.api.DEAL_REASON_TP:'TP',self.api.DEAL_REASON_SL:'SL',self.api.DEAL_REASON_EXPERT:'EXPERT'}.get(r['reason'],'EXTERNAL')
            if r['entry']==getattr(self.api,'DEAL_ENTRY_OUT_BY',3):
                reason='EXTERNAL'  # close-by is not a strategy TP/SL outcome
            exit_order=all_orders.get(str(r['order']),{})
            # Python TradeDeal has no native SL/TP. Only exact closing order evidence may supply them.
            protection=(entry=='OUT' and str(exit_order.get('position_id'))==str(r['position_id'])
                        and exit_order.get('type')==r['type'] and exit_order.get('state')==self.api.ORDER_STATE_FILLED)
            converted_deals.append(dict(id=str(r['ticket']),order_id=str(r['order']),position_id=str(r['position_id']),
                command_id=cid if entry=='IN' else '',entry=entry,
                side='BUY' if r['type']==self.api.DEAL_TYPE_BUY else 'SELL',volume=dec(r['volume']),
                price=dec(r['price']),profit=dec(r['profit']),time=int(r['time_msc']),reason=reason,
                sl=dec(exit_order.get('sl',0)) if protection else '0',
                tp=dec(exit_order.get('tp',0)) if protection else '0'))
        if any(len(cids)!=1 for cids in opening_ids.values()):
            raise ValueError('Multiple entries share a hedging position identifier')
        def order_json(r):
            status={self.api.ORDER_STATE_FILLED:'FILLED',self.api.ORDER_STATE_CANCELED:'CANCELLED',
                    self.api.ORDER_STATE_EXPIRED:'CANCELLED',self.api.ORDER_STATE_REJECTED:'REJECTED'}.get(r['state'],'PENDING')
            cid=mapping.get(str(r['ticket']),'')
            return dict(id=str(r['ticket']),command_id=cid,owned=bool(cid),status=status,
                        side='BUY' if r['type'] in (self.api.ORDER_TYPE_BUY,self.api.ORDER_TYPE_BUY_STOP) else 'SELL',
                        price=dec(r['price_open']),size=dec(r['volume_current']),sl=dec(r['sl']),tp=dec(r['tp']))
        converted_positions=[]
        for p in positions:
            pid=str(p.identifier)
            owned=pid in opening_ids and p.magic==self.magic and p.symbol==self.symbol
            if owned:
                cid=next(iter(opening_ids[pid])); cmd=self.rows[cid]['command']
                expected=self.api.POSITION_TYPE_BUY if cmd['side']=='BUY' else self.api.POSITION_TYPE_SELL
                if p.type!=expected:
                    raise ValueError('Position side conflicts with opening deal')
            converted_positions.append(dict(id=pid,ticket=str(p.ticket),owned=owned,
                side='BUY' if p.type==self.api.POSITION_TYPE_BUY else 'SELL',entry=dec(p.price_open),
                size=dec(p.volume),sl=dec(p.sl),tp=dec(p.tp)))
        snap=dict(protocol=1,backend=self.backend_id,account=self.account,connected=True,hedging=True,
                  history_complete=True,now=self.clock(),spec=spec,
                  quote=dict(bid=dec(tick.bid),ask=dec(tick.ask),utc_time=tick.time_msc/1000),
                  candle=dict(id=int(rates[0]['time']),close=dec(rates[0]['close'])),
                  orders=[order_json(plain(o)) for o in orders],positions=converted_positions,
                  order_history=[order_json(o) for o in self.facts['order'].values()],deals=converted_deals)
        if abs(self.clock()-snap['quote']['utc_time'])>self.max_age:
            raise TimeoutError('Snapshot read exceeded quote freshness')
        # Convert successful mutation replies into DONE only with broker readback evidence.
        for cid,row in list(self.rows.items()):
            cmd=row['command']; result=row.get('result',{})
            if result.get('status') not in {'UNKNOWN','ACCEPTED'}:
                continue
            proven=False
            if cmd['kind']=='MODIFY':
                proven=any(p['owned'] and p['id']==cmd['position_id'] and number(p['sl'])==number(cmd['sl'])
                           and number(p['tp'])==number(cmd['tp']) for p in converted_positions)
            elif cmd['kind']=='CANCEL':
                proven=any(o['owned'] and o['id']==cmd['order_id'] and o['status'] in {'CANCELLED','FILLED'} for o in snap['order_history'])
            if proven:
                row=dict(row,result=dict(status='DONE',reason='BROKER_READBACK'))
                self._save_row(cid,row,'readback')
        return snap

    def profit(self, side, size, entry, close):
        self._context()
        if side not in {'BUY','SELL'} or min(map(number,(size,entry,close)))<=0:
            raise ValueError('Invalid profit calculation')
        kind=self.api.ORDER_TYPE_BUY if side=='BUY' else self.api.ORDER_TYPE_SELL
        result=self.api.order_calc_profit(kind,self.symbol,float(number(size)),float(number(entry)),float(number(close)))
        self._context()
        if result is None:
            raise OSError('MT5 order_calc_profit unavailable')
        return number(result)

    def result(self,cid):
        return dict(self.rows.get(cid,{}).get('result',{'status':'UNKNOWN'}))

    def _request(self,cmd):
        self._context(mutation=True)
        snap=self.snapshot()
        _,_,info,tick,data=self._context(mutation=True)
        spec=Spec.load(data)
        snap['quote']=dict(bid=dec(tick.bid),ask=dec(tick.ask))
        if any(not p['owned'] for p in snap['orders']+snap['positions']):
            raise NotSent('Foreign order/position; ownership conflict')
        kind=cmd['kind']
        req=dict(symbol=self.symbol,magic=self.magic,comment=cmd['id'],deviation=0)
        if kind in {'STOP','MARKET'}:
            if info.trade_mode!=self.api.SYMBOL_TRADE_MODE_FULL:
                raise NotSent('Symbol must allow both trading directions')
            required=16|32|(4 if kind=='STOP' else 1)  # SYMBOL_ORDER_SL/TP and STOP/MARKET
            if info.order_mode & required != required:
                raise NotSent('Required symbol order/protection modes unavailable')
            if cmd['side'] not in {'BUY','SELL'}:
                raise NotSent('Invalid side')
            size=number(cmd['size']); side=cmd['side']; sign=D(1) if side=='BUY' else D(-1)
            if not spec.volume_min<=size<=spec.volume_max or size%spec.volume_step:
                raise NotSent('Invalid exact volume; no rounding')
            price,sl,tp,trigger=(spec.price(cmd[k]) for k in ('price','sl','tp','trigger'))
            quote=number(tick.ask if side=='BUY' else tick.bid)
            if kind=='STOP' and sign*(quote-price)>=0:
                raise NotSent('CROSSED')
            if kind=='MARKET' and sign*(quote-trigger)<0:
                raise NotSent('STOP no longer crossed')
            if kind=='MARKET' and quote!=price:
                raise NotSent('Market quote changed; no implicit repricing')
            spec.protection(side,price,sl,tp,snap['quote'] if kind=='MARKET' else None)
            if kind=='STOP' and sign*(price-quote)<spec.point*spec.stops_level:
                raise NotSent('STOP too close to quote')
            same=[p for p in snap['positions'] if p['side']==side]+[o for o in snap['orders'] if o['side']==side]
            limit=number(info.volume_limit)
            if limit>0 and size+sum((number(x['size']) for x in same),D(0))>limit:
                raise NotSent('Directional volume limit')
            req.update(action=self.api.TRADE_ACTION_PENDING if kind=='STOP' else self.api.TRADE_ACTION_DEAL,
                       type=(self.api.ORDER_TYPE_BUY_STOP if side=='BUY' else self.api.ORDER_TYPE_SELL_STOP) if kind=='STOP'
                       else (self.api.ORDER_TYPE_BUY if side=='BUY' else self.api.ORDER_TYPE_SELL),
                       volume=float(size),sl=float(sl),tp=float(tp))
            if kind=='STOP':
                if not info.expiration_mode & 1 or info.order_gtc_mode!=0:
                    raise NotSent('Persistent GTC orders and SL/TP required')
                req.update(price=float(price),type_time=self.api.ORDER_TIME_GTC,type_filling=self.api.ORDER_FILLING_RETURN)
            else:
                # FOK is unconditional for Request/Instant, capability-gated for Market/Exchange.
                if info.trade_exemode not in (self.api.SYMBOL_TRADE_EXECUTION_REQUEST,self.api.SYMBOL_TRADE_EXECUTION_INSTANT) and not info.filling_mode & 1:
                    raise NotSent('FOK required for MARKET; partial refill is unsupported')
                req['type_filling']=self.api.ORDER_FILLING_FOK
                if info.trade_exemode!=self.api.SYMBOL_TRADE_EXECUTION_MARKET:
                    req['price']=float(price)
        elif kind=='CANCEL':
            matches=[o for o in snap['orders'] if o['id']==cmd['order_id'] and o['owned']]
            if len(matches)!=1:
                raise TimeoutError('Order absent/ambiguous; reconcile history')
            o=matches[0]; quote=number(tick.ask if o['side']=='BUY' else tick.bid)
            if spec.freeze_level>0 and abs(number(o['price'])-quote)<=spec.freeze_level*spec.point:
                raise NotSent('Order inside freeze level')
            req.update(action=self.api.TRADE_ACTION_REMOVE,order=int(o['id']))
        elif kind=='MODIFY':
            matches=[p for p in snap['positions'] if p['id']==cmd['position_id'] and p['owned']]
            if len(matches)!=1:
                raise TimeoutError('Position absent/ambiguous; reconcile deals')
            p=matches[0]; sl=spec.price(cmd['sl']); tp=spec.price(cmd['tp'])
            if cmd.get('purpose') not in {'ANCHOR','STEP'}:
                raise NotSent('Invalid protection purpose')
            if cmd['purpose']=='STEP' and number(p['sl'])>0 and ((p['side']=='BUY' and sl<number(p['sl'])) or (p['side']=='SELL' and sl>number(p['sl']))):
                raise NotSent('Stepped SL cannot worsen')
            spec.protection(p['side'],number(p['entry']),sl,tp,snap['quote'],modify=True)
            freeze=spec.freeze_level*spec.point
            anchor=number(tick.bid if p['side']=='BUY' else tick.ask)
            if freeze>0 and any(abs(level-anchor)<=freeze for level in
                               (sl,tp,number(p['sl']),number(p['tp'])) if level>0):
                raise NotSent('Existing/requested protection inside freeze level')
            req.update(action=self.api.TRADE_ACTION_SLTP,position=int(p['ticket']),sl=float(sl),tp=float(tp))
        else:
            raise NotSent('Unsupported mutation')
        return req

    def submit(self,command):
        if not self.enable_trading or self.store is None:
            raise OSError('MT5 mutation disabled')
        cmd={k:v for k,v in command.items() if k not in {'status','result','order_id'} or k=='order_id' and command['kind']=='CANCEL'}
        cid=cmd.get('id','')
        if not re.fullmatch('[0-9a-f]{24}',cid):
            raise ValueError('Invalid command id')
        if cid in self.rows:
            if self.rows[cid]['command']!=cmd:
                raise DurabilityError('Command id reused with different payload')
            return
        state=self.store.load()
        saved=(state or {}).get('commands',{}).get(cid)
        if not saved or saved.get('status')!='UNKNOWN' or state.get('backend')!=self.backend_id or state.get('account')!=self.account:
            raise DurabilityError('Controller durable may-send/account/backend must precede submit')
        original={k:v for k,v in saved.items() if k not in {'status','result','order_id'} or k=='order_id' and saved['kind']=='CANCEL'}
        if original!=cmd:
            raise DurabilityError('Command differs from durable intent')
        row=dict(command=cmd,account=self.account,send_started=False,result={'status':'UNKNOWN'})
        self._save_row(cid,row,'intent')
        try:
            request=self._request(cmd)
            check=self.api.order_check(request)
            row['check']=plain(check)
            if check is None:
                raise TimeoutError('order_check unavailable; no send')
            if check.retcode!=0:
                raise NotSent('order_check rejected')
            # Rebuild and compare immediately before send; never quietly reprice/reselect a ticket.
            if self._request(cmd)!=request:
                raise NotSent('Request context changed before send')
        except (ValueError,OSError,TimeoutError) as exc:
            row['result']=dict(status='REJECTED' if isinstance(exc,ValueError) else 'UNKNOWN',reason=str(exc))
            self._save_row(cid,row,'not_sent')
            return
        row.update(request=request,send_started=True,result={'status':'UNKNOWN'})
        self._save_row(cid,row,'may_send')
        try:
            if self._request(cmd)!=request:
                raise NotSent('Request context changed after durable may-send')
        except (ValueError,OSError,TimeoutError) as exc:
            row.update(send_started=False,result=dict(
                status='REJECTED' if isinstance(exc,ValueError) else 'UNKNOWN',reason=str(exc)))
            self._save_row(cid,row,'final_guard_not_sent')
            return
        try:
            response=self.api.order_send(request)
            row['raw_result']=plain(response)
            row['last_error']=plain(self.api.last_error())
            # If account changed during the call, retain UNKNOWN and raw response for inspection.
            self._context()
            if response is None:
                row['result']={'status':'UNKNOWN','reason':'No order_send result'}
            else:
                rc=response.retcode
                reject_names=('REJECT','INVALID','INVALID_VOLUME','INVALID_PRICE','INVALID_STOPS',
                              'NO_MONEY','MARKET_CLOSED','TRADE_DISABLED','INVALID_FILL','INVALID_EXPIRATION')
                rejected={getattr(self.api,'TRADE_RETCODE_'+n) for n in reject_names}
                accepted={self.api.TRADE_RETCODE_DONE,self.api.TRADE_RETCODE_PLACED}
                status='REJECTED' if rc in rejected else ('ACCEPTED' if rc in accepted else 'UNKNOWN')
                row['result']=dict(status=status,retcode=rc,order_id=str(response.order),deal_id=str(response.deal),reason='MT5_RESULT')
        except Exception as exc:
            row['result']=dict(status='UNKNOWN',reason='order_send outcome uncertain',error_type=type(exc).__name__)
        self._save_row(cid,row,'send_result')

    def preflight(self):
        terminal,account,info,tick,spec=self._context()
        snap=self.snapshot()
        result=self.profit('BUY',number(spec['volume_min']),number(tick.ask),number(tick.ask)+number(spec['tick_size']))
        return dict(backend=self.backend_id,connected=True,hedging=True,currency=account.currency,
                    symbol=self.symbol,profit_currency=spec['profit_currency'],spec=spec,
                    trade_allowed=bool(terminal.trade_allowed),tradeapi_disabled=bool(terminal.tradeapi_disabled),
                    adapter_trading_enabled=self.enable_trading,orders=len(snap['orders']),positions=len(snap['positions']),
                    order_calc_profit=dec(result),mode='READ_ONLY',account_binding='configured' if self.login else 'session_only')
