"""Native/injected MT5 transport with cycle-scoped evidence and no history-completeness claim."""
import json
from types import ModuleType

from .mt5_adapter import MT5Broker, QuoteFreshnessError, dec
from .settings import number
from .time_profile import TEST_PROFILE, BYBIT_PROFILE, HistoryTimeUnverified, server_to_utc


class MT5EvidenceBroker(MT5Broker):
    evidence_contract='per_obligation_v1'

    def __init__(self, config, store=None, *, api=None, time_safety=None, **kwargs):
        if config.get('time_profile')==TEST_PROFILE and (api is None or isinstance(api,ModuleType)):
            raise HistoryTimeUnverified()
        super().__init__(config,store=store,api=api,**kwargs)
        self.read_only=not self.enable_trading
        from .time_safety import TimeSafety
        self.time_safety = time_safety
        if self.time_profile == BYBIT_PROFILE and self.time_safety is None:
            self.time_safety = TimeSafety(clock=self.clock, monotonic=self.monotonic)

    def _timestamp_semantics_verified(self):
        if self.time_profile == TEST_PROFILE:
            return True
        try:
            return self.time_safety.semantics_verifier() is True
        except Exception:
            return False

    def _check_quote_time(self, tick, stage):
        try:
            return super()._check_quote_time(tick,stage)
        except QuoteFreshnessError as exc:
            if getattr(self,'_manual_readonly',False) and exc.details['reason']=='TICK_OLD':
                return None  # stale market quote is not used for execution in durable MANUAL
            raise

    def _context(self, mutation=False, *, stage='context'):
        old=getattr(self,'_manual_readonly',False)
        if mutation: self._manual_readonly=False
        try:
            return super()._context(mutation=mutation,stage=stage)
        finally:
            self._manual_readonly=old

    def _request(self, command):
        guard=getattr(self,'before_mutation',None)
        if guard is None or not guard(command['kind']):
            raise OSError('Manual/deadline guard forbids automatic mutation')
        request=super()._request(command)
        if not guard(command['kind']):
            raise OSError('Cycle deadline crossed during request preparation')
        return request

    def reconciliation_evidence(self, journal_path=None):
        if journal_path is not None:
            return super().reconciliation_evidence(journal_path)
        from .evidence import collect
        state=self.store.load() if self.store else None
        state=state or dict(backend=self.backend_id,account=self.account,currency='UST',
                            scope_contract='cycle_scope_v1',commands={},completed=[],cycle=None)
        if state.get('account') is None and not state.get('commands') and not state.get('cycle'):
            state=dict(state,account=self.account)
        return collect(self.api,rows=self.rows,state=state,account=self.account,
                       symbol=self.symbol,magic=self.magic,now=self.clock(),cached=self.facts,
                       check_context=lambda:self._context(stage='cycle_evidence'))

    def manual_reset_snapshot(self):
        return self.snapshot(require_fresh=True)

    def snapshot(self, *, require_fresh=False):
        state=self.store.load() if self.store else None
        self._manual_readonly=not require_fresh and bool(((state or {}).get('cycle') or {}).get('manual'))
        _,_,_,tick,spec=self._context(stage='evidence_snapshot_start')
        audit=self.reconciliation_evidence()
        orders=self.api.orders_get(symbol=self.symbol)
        positions=self.api.positions_get(symbol=self.symbol)
        rates=self.api.copy_rates_from_pos(self.symbol,self.api.TIMEFRAME_M1,1,1)
        if orders is None or positions is None or rates is None or len(rates)!=1:
            raise OSError('Incomplete current market observation')
        quote_time=self._check_quote_time(tick,'evidence_market_time')
        candle_time=number(rates[0]['time'])
        semantics_verified = self._timestamp_semantics_verified()
        if not semantics_verified:
            # Unknown MT5 wall-time is display-only, never a UTC candle identity.
            quote_time = None
            candle_time = 0
        elif self.time_profile==BYBIT_PROFILE:
            candle_time=server_to_utc(float(candle_time))
            if candle_time%60 or candle_time+60>self.clock():
                raise ValueError('Invalid closed native M1 candle')
            if quote_time and candle_time!=int(quote_time['utc_time']//60)*60-60:
                raise OSError('Latest closed native M1 candle is unavailable')
        self._context(stage='evidence_snapshot_complete')
        self._check_quote_time(tick,'evidence_snapshot_complete')
        # Re-read current objects must agree with the evidence batch. Never fuse changing snapshots.
        if {str(o.ticket) for o in orders}!=set(audit['current_orders']):
            raise OSError('Orders changed during evidence collection')
        if {str(p.identifier) for p in positions}!=set(audit['current_positions']):
            raise OSError('Positions changed during evidence collection')
        from .evidence import plain
        if any(plain(p)!=audit['current_positions'][str(p.identifier)] for p in positions):
            raise OSError('Position evidence changed during collection')
        if any(plain(o)!=audit['current_orders'][str(o.ticket)] for o in orders):
            raise OSError('Order evidence changed during collection')
        facts=audit['raw_facts']; obligations=audit['obligations']
        owners={r['order_id']:cid for cid,r in obligations.items() if r.get('order_id') and self.rows.get(cid,{}).get('command',{}).get('kind') in {'STOP','MARKET'}}
        position_cycles={r['position_id']:r['cycle_id'] for r in obligations.values() if r.get('position_id')}
        owned_positions=set(position_cycles)
        def order_json(r):
            key=str(r['ticket']); cid=owners.get(key,'')
            status={self.api.ORDER_STATE_FILLED:'FILLED',self.api.ORDER_STATE_CANCELED:'CANCELLED',
                    self.api.ORDER_STATE_EXPIRED:'CANCELLED',self.api.ORDER_STATE_REJECTED:'REJECTED'}.get(r['state'],'PENDING')
            return dict(id=key,command_id=cid,owned=bool(cid),status=status,
                        side='BUY' if r['type'] in (self.api.ORDER_TYPE_BUY,self.api.ORDER_TYPE_BUY_STOP) else 'SELL',
                        price=dec(r['price_open']),size=dec(r['volume_current']),sl=dec(r['sl']),tp=dec(r['tp']))
        deals=[]
        for key,r in facts['deal'].items():
            if str(r['position_id']) not in owned_positions:
                continue
            entry={self.api.DEAL_ENTRY_IN:'IN',self.api.DEAL_ENTRY_OUT:'OUT'}.get(r['entry'],'OTHER')
            reason={self.api.DEAL_REASON_TP:'TP',self.api.DEAL_REASON_SL:'SL'}.get(r['reason'],'EXTERNAL')
            closing=facts['order'].get(str(r['order']),{})
            protection=(entry=='OUT' and str(closing.get('position_id'))==str(r['position_id'])
                        and closing.get('state')==self.api.ORDER_STATE_FILLED and closing.get('type')==r['type'])
            deals.append(dict(cycle_id=position_cycles[str(r['position_id'])],id=key,order_id=str(r['order']),position_id=str(r['position_id']),
                              command_id=owners.get(str(r['order']),'') if entry=='IN' else '',entry=entry,
                              side='BUY' if r['type']==self.api.DEAL_TYPE_BUY else 'SELL',volume=dec(r['volume']),
                              price=dec(r['price']),profit=dec(r['profit']),time=int(r['time_msc']),reason=reason,
                              sl=dec(closing.get('sl',0)) if protection else '0',tp=dec(closing.get('tp',0)) if protection else '0'))
        usable=all(value=='RETURNED' for value in audit['source_status'].values())
        if not usable:
            raise OSError('Required per-obligation API is unavailable')
        if usable and self.store is not None:
            # Facts are durable separately from consumption; they never book money.
            self._transaction(lambda db: db.executemany('INSERT OR REPLACE INTO mt5_facts VALUES(?,?,?)',
                [(kind,key,json.dumps(value,sort_keys=True,allow_nan=False))
                 for kind,group in facts.items() for key,value in group.items()]))
            self.facts=facts
            for cid,evidence in obligations.items():
                row=self.rows.get(cid)
                if not row: continue
                status=evidence['status']
                proven='DONE' if status in {'CANCEL_TERMINAL_READBACK','PROTECTION_READBACK'} else (
                       'ACCEPTED' if status in {'PENDING_OBSERVED','FILLED_OPEN_EVIDENCE','CLOSED_EVIDENCE'} else None)
                if proven and row.get('result',{}).get('status')!=proven:
                    self._save_row(cid,dict(row,result=dict(status=proven,reason='OBLIGATION_EVIDENCE')),'evidence_readback')
        self._context(stage='evidence_snapshot_return')
        self._check_quote_time(tick,'evidence_snapshot_return')
        return dict(protocol=1,backend=self.backend_id,account=self.account,connected=True,hedging=True,
                    reconciliation_contract=self.evidence_contract,history_complete=False,
                    observations_usable=usable,obligations=obligations,ledger_facts=facts['deal'],
                    history_time_status='UNVERIFIED',settlement_basis='cycle_scoped_identity_v1',
                    mt5_timestamp_semantics='VERIFIED' if semantics_verified else 'UNVERIFIED',
                    mt5_display_time=dict(tick_time_msc=getattr(tick,'time_msc',None),
                        candle_time=dec(rates[0]['time'])),
                    now=self.clock(),spec=spec,quote=dict(bid=dec(tick.bid),ask=dec(tick.ask),
                        utc_time=quote_time['utc_time'] if quote_time else None),
                    candle=dict(id=int(candle_time),close=dec(rates[0]['close'])),
                    orders=[order_json(plain(o)) for o in orders],order_history=[order_json(r) for r in facts['order'].values()],
                    positions=[dict(id=str(p.identifier),ticket=str(p.ticket),owned=str(p.identifier) in owned_positions,
                                    side='BUY' if p.type==self.api.POSITION_TYPE_BUY else 'SELL',entry=dec(p.price_open),
                                    size=dec(p.volume),sl=dec(p.sl),tp=dec(p.tp)) for p in positions],deals=deals)
