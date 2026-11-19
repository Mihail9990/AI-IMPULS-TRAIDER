"""End-to-end MOCK pipeline: intent sync, verified SL, normalized deal observation.

No background loop, scheduler, credentials or real terminal initialization. Every
cycle requires an explicit call with an MT5 test double. Production deployment is
unsupported. Unknown broker outcomes stop synchronization for that intent.
"""
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from .engine import Engine, number
from .executor import Executor
from .mt5_transport import MT5Transport
from .mt5_plan import pending, protect


class MockPipeline:
    def __init__(self, controller, fake_mt5, account, server, symbol, magic, database=':memory:', *, require_stop_evidence=False):
        if type(require_stop_evidence) is not bool:raise ValueError('Explicit boolean evidence mode required')
        self.controller=controller
        self.transport=MT5Transport(fake_mt5)
        self.scope={'account':account,'server':server,'symbol':symbol,'magic':magic}
        if any(any(b.get(k)!=v for k,v in self.scope.items()) for b in controller.data['bindings'].values()):
            raise ValueError('Persisted controller binding belongs to another account/symbol scope')
        # Validate before allocating an outbox connection: a constructor failure
        # cannot otherwise hand ownership of that resource back to its caller.
        self.executor=Executor(self.transport,database)
        self.events=[]
        from .stop_evidence_ledger import StopEvidenceLedger
        self.stop_evidence=StopEvidenceLedger(controller,self.scope) if require_stop_evidence else None

    def _blocked(self,rid,message):
        old=deepcopy(self.controller.data)
        try:
            self.controller.data['blocked'][rid]=message
            self.controller._save()
        except Exception:
            self.controller.data=old;raise

    def sync_pending(self, range_id=None):
        """Create missing intents and verify tickets. Never replace volume silently.

        Removed/changed orders currently require explicit reconciliation rather than
        cancel/recreate automatically: that race can open two positions.
        """
        self.transport._test_lock()
        self.controller.engine().validate_distance_policy()
        engine=self.controller.engine()
        desired={o['id']:o for o in engine.data['orders'] if range_id is None or o['range_id']==range_id}
        bindings=self.controller.data['bindings']
        for ticket,b in list(bindings.items()):
            if range_id is not None and b['range_id']!=range_id:continue
            if b['range_id'] in self.controller.data['blocked']:continue
            if b['phase']!='pending':continue
            o=desired.get(b['logical_order_id'])
            if o is None:
                # Strategy retired an old reverse after verified reentry. Remove
                # its exact mapped broker ticket BEFORE creating the replacement.
                request={'action':'TRADE_ACTION_REMOVE','order':int(ticket)}
                key='retire:'+b['logical_order_id']+':'+ticket
                self.executor.prepare(key,request,self.scope)
                self.executor.submit_once(key)
                result=self.executor.reconcile(key,refresh=True)
                if result['state']=='VERIFIED':
                    before=deepcopy(self.controller.data)
                    try:
                        b['phase']='cancelled';self.controller._save()
                    except Exception:self.controller.data=before;raise
                else:self._blocked(b['range_id'],'Retired pending cancellation not confirmed: '+result['state'])
            elif number(o['volume'])!=number(b['expected_volume']):
                self._blocked(b['range_id'],'Pending broker volume differs; cancel/replace reconciliation required')
        known={b['logical_order_id'] for b in bindings.values()}
        for oid,o in desired.items():
            if oid in known or o['range_id'] in self.controller.data['blocked']:continue
            if o['status']!='simulated_pending':continue
            s,q=self.transport._symbol(self.scope['symbol'])
            request=pending(o,s,q,self.scope['magic'])
            key='create:'+oid
            self.executor.prepare(key,request,self.scope)
            self.executor.submit_once(key)
            result=self.executor.reconcile(key,refresh=True)
            if result['state']=='VERIFIED' and result['evidence']['kind'] in ('pending_verified','order_filled_before_verification'):
                ticket=result['evidence']['order']
                self.controller.bind_verified_order(oid,ticket,**self.scope)
                if result['evidence']['kind']=='order_filled_before_verification':
                    before=deepcopy(self.controller.data)
                    try:
                        self.controller.data['bindings'][str(ticket)]['phase']='awaiting_fill_history'
                        self.controller._save()
                    except Exception:self.controller.data=before;raise
            else:
                self._blocked(o['range_id'],'Create pending not fully reconciled: '+result['state'])
        return self.controller.state()

    def observe(self, normalized_deals):
        """Consume normalized, confirmed fake history; Controller dedupes durably."""
        self.transport._test_lock()
        for event in normalized_deals:self.controller.deal(event)
        return self.controller.state()

    def observe_mt5_deals(self, deals):
        """Normalize a supplied MT5-shaped history list, IN and OUT only.

        History fetching/cursor is deliberately outside this method. OUT links by
        persistent position identifier, never by its distinct closing-order ticket.
        Partial/mixed policies remain Controller's responsibility.
        """
        api=self.transport.api;self.transport._test_lock()
        for deal in sorted(deals,key=lambda d:(d.time_msc,d.ticket)):
            if str(deal.ticket) in self.controller.data['deals']:continue
            if deal.symbol!=self.scope['symbol']:continue
            bindings=self.controller.data['bindings']
            if deal.entry==api.DEAL_ENTRY_IN:
                b=bindings.get(str(deal.order));entry_ticket=deal.order;entry='IN'
            elif deal.entry==api.DEAL_ENTRY_OUT:
                matches=[(ticket,b) for ticket,b in bindings.items() if b['position_identifier']==deal.position_id]
                if not matches:
                    # Unknown/manual positions are not this strategy's authority.
                    continue
                if len(matches)!=1:raise ValueError('Ambiguous position identifier')
                entry_ticket,b=matches[0];entry_ticket=int(entry_ticket);entry='OUT'
            else:
                raise ValueError('INOUT/OUT_BY deal unsupported; explicit reconciliation required')
            if b is None:
                if entry=='IN' and deal.magic==self.scope['magic']:
                    raise ValueError('Owned entry has no verified ticket mapping; observer cursor must not advance')
                continue
            if entry=='IN':
                expected_type=api.DEAL_TYPE_BUY if b['side']=='BUY' else api.DEAL_TYPE_SELL
                if deal.type!=expected_type:raise ValueError('Entry deal side mismatch')
            # Entry magic must match owned pending. Manual OUT deals may legitimately
            # have magic 0; provenance is position_identifier, not closing magic.
            if entry=='IN' and deal.magic!=self.scope['magic']:
                raise ValueError('Entry deal magic mismatch')
            reason='manual'
            if entry=='OUT' and deal.reason==api.DEAL_REASON_SL:
                if b['range_id'] in self.controller.data['blocked']:
                    raise ValueError('SL classification blocked pending reconciliation')
                p=next((p for p in self.controller.data['engine']['positions'] if p['id']==b['engine_position_id']),None)
                if p is None:raise ValueError('SL reason without known position state')
                if p.get('stop_classification_pending'):
                    raise ValueError('SL classification unresolved at equal support target')
                # DEAL_SL is the position's stop at closure, not the execution
                # price (which can slip). Never infer an external SL change from
                # profit/price or silently advance the volume schedule.
                # Legacy test doubles omit sl; production ingestion must supply
                # this evidence or explicitly reconcile it before enabling sends.
                if self.stop_evidence is not None or hasattr(deal,'sl'):
                    try:
                        if self.stop_evidence is not None and deal.type!=(api.DEAL_TYPE_SELL if b['side']=='BUY' else api.DEAL_TYPE_BUY):
                            raise ValueError('SL exit direction mismatch')
                        observed_sl=(self.stop_evidence.resolve(deal,b) if self.stop_evidence is not None else number(deal.sl))
                        if self.stop_evidence is not None:
                            from .price_evidence import canonical_exported_price
                            symbol,_=self.transport._symbol(self.scope['symbol'])
                            observed_sl=canonical_exported_price(observed_sl,symbol.tick_size)
                        if observed_sl<=0 or observed_sl!=number(p['sl']):
                            raise ValueError('Stop differs from confirmed strategy protection')
                    except (ValueError,TypeError,ArithmeticError):
                        self._blocked(b['range_id'],'SL evidence missing/changed; explicit reconciliation required')
                        raise ValueError('SL evidence does not match confirmed protection')
                reason='support_sl' if p['stop_stage']>0 else 'initial_sl'
            elif entry=='OUT' and deal.reason not in (api.DEAL_REASON_CLIENT,api.DEAL_REASON_MOBILE,api.DEAL_REASON_WEB):
                # A bot-generated market close is authorized only by our durable
                # matching close request + broker receipt, not magic/comment alone.
                authorized=False
                for payload_json,receipt_json in self.executor.db.execute(
                        "SELECT payload,receipt FROM recovery_outbox WHERE state IN ('ACKNOWLEDGED','VERIFIED','UNKNOWN')"):
                    import json
                    intent=json.loads(payload_json);receipt=json.loads(receipt_json) if receipt_json else {}
                    if (intent['request']['action']=='TRADE_ACTION_DEAL'
                            and receipt.get('order')==deal.order
                            and intent['expected'].get('identifier')==deal.position_id
                            and all(intent['expected'].get(k)==v for k,v in self.scope.items())
                            and getattr(api,'DEAL_REASON_EXPERT',None)==deal.reason):
                        authorized=True;break
                if not authorized:raise ValueError('Unsupported close reason; do not classify TP/stopout as manual')
            pnl=sum((number(getattr(deal,f)) for f in ('profit','commission','swap','fee')),Decimal(0))
            event=dict(deal_id=deal.ticket,entry_order_ticket=entry_ticket,**self.scope,
                       position_identifier=deal.position_id,entry=entry,side=b['side'],
                       volume=str(deal.volume),price=str(deal.price),realized_net=str(pnl),close_reason=reason)
            self.controller.deal(event)
        return self.controller.state()

    def poll_history(self, now=None):
        """One explicit fake-history polling cycle with durable overlapping cursor.

        No timer/loop. Empty/error reads never advance cursor. Overlap is five
        minutes; events older than overlap and late fees still need reconciliation.
        """
        self.transport._test_lock();self.transport._account(self.scope)
        now=now or datetime.now(timezone.utc)
        if now.tzinfo is None:raise ValueError('Timezone-aware poll time required')
        cursor=self.controller.data.get('observer_cursor_msc')
        start=(datetime.fromtimestamp(cursor/1000,timezone.utc)-timedelta(minutes=5)
               if cursor is not None else now-timedelta(days=30))
        rows=self.transport.api.history_deals_get(start,now)
        if rows is None:raise ValueError('Deal history unavailable; cursor unchanged')
        # A terminal can switch accounts while the blocking history call runs.
        # Reject the entire result before touching the durable ledger/cursor.
        self.transport._account(self.scope)
        # Validate the WHOLE batch before any durable deal/strategy mutation.
        for deal in rows:
            timestamp=getattr(deal,'time_msc',None)
            if isinstance(timestamp,bool) or not isinstance(timestamp,int) or timestamp<0:
                raise ValueError('Invalid history timestamp; batch not ingested')
            if timestamp>int(now.timestamp()*1000):
                raise ValueError('Future history timestamp; batch not ingested')
        self.observe_mt5_deals(rows)
        if rows:
            newest=max(d.time_msc for d in rows)
            before=deepcopy(self.controller.data)
            try:
                self.controller.data['observer_cursor_msc']=max(cursor or 0,newest)
                self.controller._save()
            except Exception:
                self.controller.data=before;raise
        return self.controller.state()

    def protect_partial(self, range_id=None):
        """Verify protection of observed partial exposure without inventing a full fill.

        Partial IN uses actual broker average entry and configured initial distance;
        already tighter broker SL is preserved. Partial OUT preserves the strategy's
        confirmed protection. No support-stage/count transition is synthesized.
        """
        self.transport._test_lock()
        self.controller.engine().validate_distance_policy()
        for b in self.controller.data['bindings'].values():
            if range_id is not None and b['range_id']!=range_id:continue
            if b['phase'] not in ('partial_entry','partial_exit'):continue
            rid=b['range_id']
            if rid in self.controller.data['blocked']:continue
            snapshot=self.transport.snapshot(self.scope)
            rows=[p for p in snapshot['positions'] if p['identifier']==b['position_identifier']]
            if len(rows)!=1:
                self._blocked(rid,'Partial exposure missing/ambiguous in broker snapshot');continue
            actual=rows[0];remaining=number(b['entry_volume'])-number(b['exit_volume'])
            if number(actual['volume'])!=remaining or remaining<=0:
                self._blocked(rid,'Partial exposure volume differs from deal ledger');continue
            r=self.controller.engine()._range(rid)
            direction=Decimal(1) if actual['side']=='BUY' else Decimal(-1)
            stage=0
            if b['phase']=='partial_entry':
                desired=number(actual['entry'])-direction*number(r['initial_sl_distance'])
            else:
                p=next((p for p in self.controller.data['engine']['positions'] if p['id']==b['engine_position_id']),None)
                if p is None:
                    self._blocked(rid,'Partial close without known logical protection');continue
                desired=number(p['sl']);stage=p['stop_stage']
            old=number(actual['sl'])
            if old>0 and (old-desired)*direction>0:desired=old
            s,q=self.transport._symbol(self.scope['symbol'])
            actual['stop_stage']=stage
            expected=dict(self.scope,identifier=b['position_identifier'],confirmed_stop_stage=stage)
            request=protect(actual,str(desired),s,q,expected_ticket=actual['ticket'],
                            expected_identifier=b['position_identifier'],expected_magic=self.scope['magic'])
            key='partial-protect:'+b['logical_order_id']+':'+str(remaining)+':'+str(desired)
            self.executor.prepare(key,request,expected);self.executor.submit_once(key)
            result=self.executor.reconcile(key,refresh=True)
            if result['state']!='VERIFIED':
                self._blocked(rid,'Partial protection unverified: '+result['state']);continue
            before=deepcopy(self.controller.data)
            try:
                b['protection_status']='verified'
                b['protected_volume']=str(remaining)
                b['verified_sl']=str(desired)
                self.controller._save()
            except Exception:
                self.controller.data=before;raise
        return self.controller.state()

    def protect_quote(self,bid,ask,range_id=None):
        """Advance support stage ONLY after exact broker SL/TP verification.

        Also reconciles initial actual-fill SL before accepting it as protected.
        This method does not call Controller.quote (which is simulation-only).
        """
        self.transport._test_lock()
        current=self.controller.engine();proposed=Engine(current.data)
        if range_id is not None:
            proposed.data['positions']=[p for p in proposed.data['positions'] if p['range_id']==range_id]
        proposed.quote(bid,ask)
        for wanted in proposed.data['positions']:
            rid=wanted['range_id']
            if rid in self.controller.data['blocked']:continue
            matches=[b for b in self.controller.data['bindings'].values() if b['engine_position_id']==wanted['id']]
            if len(matches)!=1:raise ValueError('Position binding unresolved')
            b=matches[0]
            if b['phase']!='open':
                # Partial lifecycle uses its separate observed-volume protection path.
                continue
            snapshot=self.transport.snapshot(self.scope)
            rows=[p for p in snapshot['positions'] if p['identifier']==b['position_identifier']]
            if len(rows)!=1:
                self._blocked(rid,'Position absent/ambiguous during protection; inspect deal history');continue
            actual=rows[0]
            old=next(p for p in current.data['positions'] if p['id']==wanted['id'])
            s,q=self.transport._symbol(self.scope['symbol'])
            expected=dict(self.scope,identifier=b['position_identifier'],confirmed_stop_stage=old['stop_stage'])
            actual['stop_stage']=old['stop_stage']
            request=protect(actual,wanted['sl'],s,q,expected_ticket=actual['ticket'],
                            expected_identifier=b['position_identifier'],expected_magic=self.scope['magic'])
            key='protect:'+wanted['id']+':'+str(wanted['stop_stage'])+':'+wanted['sl']
            self.executor.prepare(key,request,expected);self.executor.submit_once(key)
            result=self.executor.reconcile(key,refresh=True)
            if result['state']!='VERIFIED':
                self._blocked(rid,'Protection outcome '+result['state']+'; do not classify as support');continue
            # Re-fetch in-memory identity: processing remains serial in this mock.
            before=deepcopy(self.controller.data)
            try:
                p=next(p for p in self.controller.data['engine']['positions'] if p['id']==wanted['id'])
                p['sl']=wanted['sl'];p['stop_stage']=wanted['stop_stage']
                if wanted.get('stop_classification_pending'):
                    p['stop_classification_pending']=wanted['stop_classification_pending']
                else:p.pop('stop_classification_pending',None)
                self.controller._save()
            except Exception:
                self.controller.data=before;raise
        return self.controller.state()
