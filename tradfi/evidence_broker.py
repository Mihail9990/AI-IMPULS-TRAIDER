"""Native/injected MT5 transport with cycle-scoped evidence and no history-completeness claim."""
import json
from types import ModuleType

from .mt5_adapter import MT5Broker, QuoteFreshnessError, NotSent, EntryDataWait, dec
from .settings import number
from .native_time import M1_TRANSITION_WAIT
from .time_profile import TEST_PROFILE, BYBIT_PROFILE, RAW_MARKET_MODE, HistoryTimeUnverified, TimeProfileError, market_timestamp_to_utc, market_tick_raw_seconds, validate_market_time_offset, validate_market_time_mode


def _stable_evidence(record, kind):
    # Only MT5 mark-to-market fields can drift without an execution change.
    # Unknown/new fields, swap, execution timestamps and all terms remain strict.
    volatile = {'price_current', 'profit'} if kind == 'position' else {'price_current'}
    return {key: value for key, value in record.items() if key not in volatile}


class MT5EvidenceBroker(MT5Broker):
    evidence_contract='per_obligation_v1'

    def __init__(self, config, store=None, *, api=None, time_safety=None, **kwargs):
        if store is not None:
            from .market_time_state import resolve_market_time_config
            config=resolve_market_time_config(store,config)
        self.market_time_mode=validate_market_time_mode(config)
        self.raw_market=None
        self.market_time_offset_seconds=validate_market_time_offset(config)
        if config.get('time_profile')==TEST_PROFILE and (api is None or isinstance(api,ModuleType)):
            raise HistoryTimeUnverified()
        if store is not None and config.get('time_profile') == BYBIT_PROFILE:
            from .market_time_state import bind_market_time_state
            bind_market_time_state(store,config)
        super().__init__(config,store=store,api=api,**kwargs)
        from .raw_market_time import MT5DisplayClock
        self.display_clock=MT5DisplayClock(self.monotonic)
        if self.market_time_mode == RAW_MARKET_MODE:
            from .raw_market_time import RawMarketTime
            self.raw_market=RawMarketTime(self.monotonic)
        self.read_only=not self.enable_trading
        from .time_safety import TimeSafety
        self.time_safety = time_safety
        if getattr(self,'raw_market',None) is not None:
            if time_safety is not None:
                raise ValueError('Raw market mode cannot use a UTC verifier')
        elif self.time_profile == BYBIT_PROFILE and self.time_safety is None:
            self.time_safety = TimeSafety(clock=self.clock, monotonic=self.monotonic)
            # Only the normal unconfigured safety object receives the native verifier.
            # Explicit injected safety doubles retain their test contract.
            if self.time_safety.semantics_verifier() is not True:
                from .native_time import NativeUTCVerifier
                self.time_safety.semantics_verifier=NativeUTCVerifier(self,self.time_safety)

    def market_time_to_utc(self,stamp):
        if getattr(self,'raw_market',None) is not None:
            raise TimeProfileError('RAW_MARKET_TIME_IS_NOT_VERIFIED_UTC')
        return market_timestamp_to_utc(stamp,self.market_time_offset_seconds)

    def _timestamp_semantics_verified(self):
        if getattr(self,'raw_market',None) is not None:
            return False
        if self.time_profile == TEST_PROFILE:
            return True
        try:
            return self.time_safety.semantics_verifier() is True
        except Exception:
            return False

    def _quote_clock(self):
        if self.time_safety is not None:
            reference = self.time_safety.trusted_utc()
            if reference is not None:
                return reference
        return super()._quote_clock()

    def _check_quote_time(self, tick, stage):
        if getattr(self,'raw_market',None) is not None:
            stamp=market_tick_raw_seconds(tick)
            return dict(raw_time_msc=getattr(tick,'time_msc'),broker_time=stamp,
                        utc_time=None,offset_seconds=None,age_s=None)
        if not self._timestamp_semantics_verified():
            if getattr(self, '_cancel_request', False) and stage != 'cancel_freeze_check':
                return None  # ticket-only REMOVE; a nonzero freeze level is checked separately
            if getattr(self, '_strict_quote', False) or getattr(self, '_require_fresh', False):
                verifier=getattr(getattr(self,'time_safety',None),'semantics_verifier',None)
                if (getattr(self, '_entry_request', False)
                        and getattr(verifier,'reason',None)==M1_TRANSITION_WAIT):
                    self._raise_entry_wait()
                raise NotSent('MT5_TIMESTAMP_SEMANTICS_UNVERIFIED')
            return None  # observation only: never interpret raw broker wall time as UTC
        try:
            quote=super()._check_quote_time(tick,stage)
            # The supplied tick may differ from the verifier's independent read.
            # Enforce the same future tolerance on this exact observation too.
            if quote['age_s'] < -2:
                raise QuoteFreshnessError(stage,'TICK_AHEAD_OF_PC',
                    quote['utc_time']+quote['age_s'],quote['utc_time'],self.max_age,
                    raw_time_msc=quote['raw_time_msc'],time_profile=self.time_profile)
            return quote
        except (QuoteFreshnessError, TimeProfileError):
            if not getattr(self, '_strict_quote', False) and not getattr(self, '_require_fresh', False):
                return None  # identity evidence remains usable; mutations revalidate quote time
            raise

    def _context(self, mutation=False, *, stage='context'):
        old=getattr(self,'_manual_readonly',False)
        if mutation: self._manual_readonly=False
        try:
            value=super()._context(mutation=mutation,stage=stage)
            display=getattr(self,'display_clock',None)
            if display is not None:display.connection(True)
            if getattr(self,'raw_market',None) is not None:self.raw_market.observe(value[3])
            return value
        except (ValueError,OSError,TimeoutError,ArithmeticError) as exc:
            disconnected=isinstance(exc,OSError) and str(exc) in {
                'MT5 adapter is not connected','MT5 terminal unavailable/disconnected'}
            if getattr(self,'raw_market',None) is not None:
                self.raw_market.invalidate(disconnected=disconnected)
            display=getattr(self,'display_clock',None)
            source_changed=isinstance(exc,ValueError) and str(exc) in {
                'Unexpected MT5 account/server','MT5 account changed','Unexpected MT5 terminal path'}
            if (disconnected or source_changed) and display is not None:display.connection(False)
            raise
        finally:
            self._manual_readonly=old

    def _raw_market_pair(self, stage):
        """Bounded read-only retry for a tick/bar rollover, never for bad data."""
        from .raw_market_time import closed_pair
        for attempt in range(3):
            _,_,_,tick,spec=self._context(stage=stage)
            rates=self.api.copy_rates_from_pos(self.symbol,self.api.TIMEFRAME_M1,0,2)
            try:
                closed,current=closed_pair(tick,rates)
                return tick,spec,closed,current
            except OSError as exc:
                if str(exc) != 'Raw tick and M1 observation disagree' or attempt == 2:
                    raise

    def observe_raw_market(self):
        """Read-only eligibility: connection, valid quote and coherent native bars.

        No age/UTC/clock-offset requirement. Reconnection may accept cached data.
        """
        if getattr(self,'raw_market',None) is None:return False
        try:
            self._raw_market_pair('raw_market_observation')
            return True
        except (ValueError,OSError,TimeoutError,ArithmeticError,TypeError,KeyError):
            self.raw_market.invalidate()
            return False

    def close(self):
        if getattr(self,'raw_market',None) is not None:self.raw_market.invalidate(disconnected=True)
        display=getattr(self,'display_clock',None)
        if display is not None:display.connection(False)
        super().close()

    def sync_display_clock(self):
        _,_,_,tick,_=self._context(stage='display_clock_alignment')
        return self.display_clock.align(tick)

    def activate_raw_market_time(self):
        """Only after the durable, explicitly requested migration commits."""
        from .raw_market_time import RawMarketTime
        self.config.update(market_time_mode=RAW_MARKET_MODE,market_time_offset_seconds=0)
        self.market_time_mode=RAW_MARKET_MODE
        self.market_time_offset_seconds=0
        self.time_safety=None
        self.raw_market=RawMarketTime(self.monotonic)
        self.clock_anchor=None
        self.clock_failed=False

    def _raise_entry_wait(self):
        waiting = getattr(self, 'entry_waiting', None)
        if waiting is not None and waiting() is True:
            raise EntryDataWait('MT5_RAW_WAIT_DATA' if getattr(self,'raw_market',None) is not None else 'MT5_UTC_M1_TRANSITION_WAIT')

    def preflight(self):
        if getattr(self,'raw_market',None) is None:return super().preflight()
        snap=self.snapshot()
        return dict(backend=self.backend_id,time_profile=self.time_profile,
                    market_time_mode=self.market_time_mode,mode='READ_ONLY',inspect_status='OK',
                    market_data_time_status='BROKER_RAW',freshness_policy='ACCEPT_LAST_OBSERVED',
                    history_time_status='UNVERIFIED',history_complete=False,execution_ready=False,
                    adapter_trading_enabled=False,connected=True,hedging=True,symbol=self.symbol,
                    currency=snap['spec']['currency'],spec=snap['spec'],quote=snap['quote'],
                    candle=snap['candle'],market_observation=snap['market_observation'],
                    orders=len(snap['orders']),positions=len(snap['positions']))

    def _request(self, command):
        guard=getattr(self,'before_mutation',None)
        if guard is None or not guard(command['kind']):
            if command['kind'] in {'STOP', 'MARKET'}:
                self._raise_entry_wait()
            raise OSError('Manual/deadline guard forbids automatic mutation')
        old = getattr(self, '_strict_quote', False)
        old_entry = getattr(self, '_entry_request', False)
        self._entry_request = command['kind'] in {'STOP', 'MARKET'}
        self._strict_quote = True
        self._protective_request = command['kind'] in {'CANCEL', 'MODIFY', 'CLOSE'}
        self._cancel_request = command['kind'] == 'CANCEL'
        try:
            request=super()._request(command)
        finally:
            self._strict_quote = old
            self._entry_request = old_entry
            self._protective_request = False
            self._cancel_request = False
        if not guard(command['kind']):
            if command['kind'] in {'STOP', 'MARKET'}:
                self._raise_entry_wait()
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
        old = getattr(self, '_require_fresh', False)
        self._require_fresh = require_fresh
        try:
            return self._snapshot(require_fresh=require_fresh)
        finally:
            self._require_fresh = old

    def _snapshot(self, *, require_fresh=False):
        state=self.store.load() if self.store else None
        self._manual_readonly=not require_fresh and bool(((state or {}).get('cycle') or {}).get('manual'))
        _,_,_,tick,spec=self._context(stage='evidence_snapshot_start')
        audit=self.reconciliation_evidence()
        raw_mode=getattr(self,'raw_market',None) is not None
        if raw_mode:
            tick,spec,closed,current_candle=self._raw_market_pair('evidence_market_pair')
            rates=[closed]
            raw_observation=self.raw_market.view()
        else:
            rates=self.api.copy_rates_from_pos(self.symbol,self.api.TIMEFRAME_M1,1,1)
        # Re-read obligations after any market-pair retry as well. A fill/cancel
        # during the rollover must invalidate the earlier evidence batch.
        orders=self.api.orders_get(symbol=self.symbol)
        positions=self.api.positions_get(symbol=self.symbol)
        if orders is None or positions is None or rates is None or len(rates)!=1:
            raise OSError('Incomplete current market observation')
        quote_time=self._check_quote_time(tick,'evidence_market_time')
        semantics_verified = self._timestamp_semantics_verified()
        candle_time=number(rates[0]['time']) if semantics_verified else 0
        if raw_mode:
            candle_time=number(closed['time'])
        elif not semantics_verified or quote_time is None:
            # Unknown MT5 wall-time is display-only, never a UTC candle identity.
            quote_time = None
            candle_time = 0
        elif self.time_profile==BYBIT_PROFILE:
            candle_time=self.market_time_to_utc(float(candle_time))
            if candle_time%60 or candle_time+60>self._quote_clock():
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
        if any(_stable_evidence(plain(p), 'position')!=_stable_evidence(audit['current_positions'][str(p.identifier)], 'position') for p in positions):
            raise OSError('Position evidence changed during collection')
        if any(_stable_evidence(plain(o), 'order')!=_stable_evidence(audit['current_orders'][str(o.ticket)], 'order') for o in orders):
            raise OSError('Order evidence changed during collection')
        facts=audit['raw_facts']; obligations=audit['obligations']
        owners={r['order_id']:cid for cid,r in obligations.items() if r.get('order_id') and self.rows.get(cid,{}).get('command',{}).get('kind') in {'STOP','MARKET'}}
        position_cycles={r['position_id']:r['cycle_id'] for r in obligations.values() if r.get('position_id')}
        owned_positions=set(position_cycles)
        from .reconciliation_diagnostics import obligation_descriptor
        def order_json(r):
            key=str(r['ticket']); cid=owners.get(key,'')
            status={self.api.ORDER_STATE_FILLED:'FILLED',self.api.ORDER_STATE_CANCELED:'CANCELLED',
                    self.api.ORDER_STATE_EXPIRED:'CANCELLED',self.api.ORDER_STATE_REJECTED:'REJECTED'}.get(r['state'],'PENDING')
            return dict(id=key,command_id=cid,owned=bool(cid),status=status,
                        obligation_diagnostic=obligation_descriptor('ORDER',r,owned_positions),
                        side='BUY' if r['type'] in (self.api.ORDER_TYPE_BUY,self.api.ORDER_TYPE_BUY_STOP) else 'SELL',
                        price=dec(r['price_open']),size=dec(r['volume_current']),sl=dec(r['sl']),tp=dec(r['tp']))
        deals=[]
        for key,r in facts['deal'].items():
            if str(r['position_id']) not in owned_positions:
                continue
            entry={self.api.DEAL_ENTRY_IN:'IN',self.api.DEAL_ENTRY_OUT:'OUT'}.get(r['entry'],'OTHER')
            reason={self.api.DEAL_REASON_TP:'TP',self.api.DEAL_REASON_SL:'SL'}.get(r['reason'],'EXTERNAL')
            close_cid=audit.get('controlled_closes',{}).get(key)
            if close_cid:reason=self.rows[close_cid]['command']['purpose']
            closing=facts['order'].get(str(r['order']),{})
            protection=(entry=='OUT' and str(closing.get('position_id'))==str(r['position_id'])
                        and closing.get('state')==self.api.ORDER_STATE_FILLED and closing.get('type')==r['type'])
            deals.append(dict(cycle_id=position_cycles[str(r['position_id'])],id=key,order_id=str(r['order']),position_id=str(r['position_id']),
                              command_id=owners.get(str(r['order']),'') if entry=='IN' else '',entry=entry,
                              side='BUY' if r['type']==self.api.DEAL_TYPE_BUY else 'SELL',volume=dec(r['volume']),
                              price=dec(r['price']),profit=dec(r['profit']),time=int(r['time_msc']),reason=reason,
                              **{k:dec(r[k]) for k in ('commission','swap','fee') if k in r},
                              close_command_id=close_cid,
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
                proven='DONE' if status in {'CANCEL_TERMINAL_READBACK','PROTECTION_READBACK','CLOSE_TERMINAL_READBACK'} else (
                       'ACCEPTED' if status in {'PENDING_OBSERVED','FILLED_OPEN_EVIDENCE','CLOSED_EVIDENCE'} else None)
                if proven and row.get('result',{}).get('status')!=proven:
                    self._save_row(cid,dict(row,result=dict(status=proven,reason='OBLIGATION_EVIDENCE')),'evidence_readback')
        self._context(stage='evidence_snapshot_return')
        quote_time = self._check_quote_time(tick,'evidence_snapshot_return')
        if quote_time is None and not raw_mode:
            candle_time = 0
        # Native rates use NumPy int64. Normalize display metadata only; raw
        # epoch value and the independently verified UTC contract stay unchanged.
        return dict(protocol=1,backend=self.backend_id,account=self.account,connected=True,hedging=True,
                    reconciliation_contract=self.evidence_contract,history_complete=False,
                    observations_usable=usable,obligations=obligations,ledger_facts=facts['deal'],
                    history_time_status='UNVERIFIED',settlement_basis='cycle_scoped_identity_v1',
                    mt5_timestamp_semantics='BROKER_RAW' if raw_mode else ('VERIFIED' if semantics_verified else 'UNVERIFIED'),
                    market_time_mode=self.market_time_mode,
                    market_observation=raw_observation if raw_mode else None,
                    mt5_display_time=dict(tick_time_msc=getattr(tick,'time_msc',None),
                        candle_time=rates[0]['time'].item() if hasattr(rates[0]['time'],'item') else rates[0]['time'],
                        server_display_utc_offset_seconds=self.market_time_offset_seconds if quote_time and not raw_mode else None),
                    now=self.clock(),spec=spec,quote=dict(bid=dec(tick.bid),ask=dec(tick.ask),
                        utc_time=quote_time['utc_time'] if quote_time else None,
                        **(dict(broker_time=quote_time['broker_time'],raw_time_msc=int(tick.time_msc)) if raw_mode else {})),
                    candle=dict(id=int(candle_time),open=dec(rates[0]['open']),close=dec(rates[0]['close']),
                        **(dict(current_id=current_candle,closed=True,time_domain='BROKER_RAW') if raw_mode else {})),
                    orders=[order_json(plain(o)) for o in orders],order_history=[order_json(r) for r in facts['order'].values()],
                    positions=[dict(id=str(p.identifier),ticket=str(p.ticket),owned=str(p.identifier) in owned_positions,
                                    obligation_diagnostic=obligation_descriptor('POSITION',plain(p),owned_positions),
                                    side='BUY' if p.type==self.api.POSITION_TYPE_BUY else 'SELL',entry=dec(p.price_open),
                                    size=dec(p.volume),sl=dec(p.sl),tp=dec(p.tp)) for p in positions],deals=deals)
