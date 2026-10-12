"""End-to-end per-obligation transport, currently restricted to an injected offline API.

The native runtime cannot select this class. No global history-completeness claim.
"""
import json
from types import ModuleType

from .mt5_adapter import MT5Broker, QuoteFreshnessError, dec
from .time_profile import TEST_PROFILE, HistoryTimeUnverified


class MT5EvidenceBroker(MT5Broker):
    evidence_contract='per_obligation_v1'

    def __init__(self, config, store=None, *, api=None, **kwargs):
        if api is None or isinstance(api,ModuleType) or config.get('time_profile')!=TEST_PROFILE:
            raise HistoryTimeUnverified()
        super().__init__(config,store=store,api=api,**kwargs)

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
        if guard is None or not guard():
            raise OSError('Manual/deadline guard forbids automatic mutation')
        request=super()._request(command)
        if not guard():
            raise OSError('Cycle deadline crossed during request preparation')
        return request

    def reconciliation_evidence(self, journal_path=None):
        if journal_path is not None:
            return super().reconciliation_evidence(journal_path)
        from .evidence import collect
        state=self.store.load() if self.store else None
        state=state or dict(backend=self.backend_id,account=self.account,currency='UST',
                            scope_contract='cycle_scope_v1',commands={},completed=[],cycle=None)
        return collect(self.api,rows=self.rows,state=state,account=self.account,
                       symbol=self.symbol,magic=self.magic,now=self.clock(),cached=self.facts,
                       check_context=lambda:self._context(stage='cycle_evidence'))

    def snapshot(self):
        state=self.store.load() if self.store else None
        self._manual_readonly=bool(((state or {}).get('cycle') or {}).get('manual'))
        _,_,_,tick,spec=self._context(stage='evidence_snapshot_start')
        audit=self.reconciliation_evidence()
        orders=self.api.orders_get(symbol=self.symbol)
        positions=self.api.positions_get(symbol=self.symbol)
        rates=self.api.copy_rates_from_pos(self.symbol,self.api.TIMEFRAME_M1,1,1)
        if orders is None or positions is None or rates is None or len(rates)!=1:
            raise OSError('Incomplete current market observation')
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
                    now=self.clock(),spec=spec,quote=dict(bid=dec(tick.bid),ask=dec(tick.ask),utc_time=tick.time_msc/1000),
                    candle=dict(id=int(rates[0]['time']),close=dec(rates[0]['close'])),
                    orders=[order_json(plain(o)) for o in orders],order_history=[order_json(r) for r in facts['order'].values()],
                    positions=[dict(id=str(p.identifier),ticket=str(p.ticket),owned=str(p.identifier) in owned_positions,
                                    side='BUY' if p.type==self.api.POSITION_TYPE_BUY else 'SELL',entry=dec(p.price_open),
                                    size=dec(p.volume),sl=dec(p.sl),tp=dec(p.tp)) for p in positions],deals=deals)
