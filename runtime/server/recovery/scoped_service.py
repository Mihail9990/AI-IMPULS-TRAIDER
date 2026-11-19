"""Range-isolated MOCK service. Broker-history/account failures remain global."""
from datetime import datetime, timezone, timedelta
from .service_loop import MockService
from .engine import number


class ScopedMockService(MockService):
    def reconcile_startup(self):
        report=super().reconcile_startup()
        # Quarantine alone must survive restart without disabling an unrelated
        # healthy range. Any broker/ledger mismatch or unknown intent is global.
        if not any(report[k] for k in ('unknown_owned_orders','unknown_owned_positions',
                'snapshot_mismatches','unresolved_intents','missing_mapped_orders',
                'missing_mapped_positions')):
            self.controller._startup_ready=True
        report['scoped_ready']=self.controller._startup_ready
        report['quarantined_ranges']=sorted(self.controller.data['blocked'])
        return report

    def _history(self, now):
        self.p.transport._account(self.p.scope)
        cursor=self.controller.data.get('observer_cursor_msc')
        start=(datetime.fromtimestamp(cursor/1000,timezone.utc)-timedelta(minutes=5)
               if cursor is not None else now-timedelta(days=30))
        rows=self.p.transport.api.history_deals_get(start,now)
        self.p.transport._account(self.p.scope)
        if rows is None:raise ValueError('Deal history unavailable')
        for row in rows:
            stamp=getattr(row,'time_msc',None)
            if isinstance(stamp,bool) or not isinstance(stamp,int) or not 0<=stamp<=int(now.timestamp()*1000):
                raise ValueError('Invalid history timestamp')
        api=self.p.transport.api
        # Resolve ownership for the complete batch before any mutation, including
        # an IN followed by OUT in the same history response.
        bindings=self.controller.data['bindings']
        positions={}
        for b in bindings.values():
            if b['position_identifier'] is not None:
                positions.setdefault(b['position_identifier'],set()).add(b['range_id'])
        for row in rows:
            if row.symbol==self.p.scope['symbol'] and row.entry==api.DEAL_ENTRY_IN:
                b=bindings.get(str(row.order))
                if b:positions.setdefault(row.position_id,set()).add(b['range_id'])
                elif row.magic==self.p.scope['magic']:
                    raise ValueError('Owned history entry lacks binding')
        routed={}
        for row in rows:
            if row.symbol!=self.p.scope['symbol']:continue
            if row.entry==api.DEAL_ENTRY_IN:
                b=bindings.get(str(row.order));ids={b['range_id']} if b else set()
            else:
                ids=positions.get(row.position_id,set())
            if len(ids)>1:raise ValueError('Ambiguous history range identity')
            if ids:routed.setdefault(next(iter(ids)),[]).append(row)
        return rows,routed

    def prepare_stop_evidence_requests(self, *, now=None):
        """Local test-driver handshake; reads history but does not ingest/clear blocks."""
        with self.controller.lock:
            if self.closed or not self.controller._startup_ready:
                raise RuntimeError('Service not ready')
            if self.p.stop_evidence is None:raise ValueError('Strict evidence mode is disabled')
            _,routed=self._history(now or datetime.now(timezone.utc))
            api=self.p.transport.api
            requests=[]
            for rows in routed.values():
                for deal in rows:
                    if deal.entry!=api.DEAL_ENTRY_OUT or deal.reason!=api.DEAL_REASON_SL:continue
                    if str(deal.ticket) in self.controller.data['deals']:continue
                    matches=[b for b in self.controller.data['bindings'].values()
                             if b['position_identifier']==deal.position_id]
                    if len(matches)!=1:raise ValueError('Stop evidence position unresolved')
                    b=matches[0]
                    if deal.type!=(api.DEAL_TYPE_SELL if b['side']=='BUY' else api.DEAL_TYPE_BUY):
                        raise ValueError('SL exit direction mismatch')
                    requests.append(self.p.stop_evidence.request(deal,b))
            return requests

    def step(self,bid,ask,*,now=None):
        with self.controller.lock:
            if self.closed:raise RuntimeError('Service closed')
            self.p.transport._test_lock()
            if not self.controller._startup_ready:raise RuntimeError('Startup reconciliation unresolved')
            bid,ask=number(bid),number(ask)
            if bid<=0 or ask<bid:raise ValueError('Invalid BID/ASK')
            now=now or datetime.now(timezone.utc)
            if now.tzinfo is None:raise ValueError('Timezone-aware poll time required')
            try:rows,routed=self._history(now)
            except Exception:
                self.controller._startup_ready=False
                raise
            failures=[]
            for rid in [r['id'] for r in self.controller.data['engine']['ranges']]:
                if rid in self.controller.data['blocked']:continue
                try:
                    self.p.transport._account(self.p.scope)
                    self.p.observe_mt5_deals(routed.get(rid,[]))
                    self.p.protect_partial(range_id=rid)
                    from .maintenance import Maintenance
                    for ticket,binding in list(self.controller.data['bindings'].items()):
                        if (binding['range_id']==rid and binding['phase']=='partial_entry'
                                and rid not in self.controller.data['blocked']):
                            def poll_range():
                                try:_,fresh=self._history(datetime.now(timezone.utc))
                                except Exception:
                                    self.controller._startup_ready=False
                                    raise
                                self.p.observe_mt5_deals(fresh.get(rid,[]))
                            Maintenance(self.p).cancel_partial_remainder(
                                int(ticket),'accept-partial:'+ticket,history_poll=poll_range)
                    self.p.protect_quote(bid,ask,range_id=rid)
                    self.p.sync_pending(range_id=rid)
                    self.p.transport._account(self.p.scope)
                except Exception as exc:
                    if not self.controller._startup_ready:raise
                    # Account faults are never reduced to per-range failures.
                    try:self.p.transport._account(self.p.scope)
                    except Exception:
                        self.controller._startup_ready=False
                        raise
                    try:self.p._blocked(rid,'Scoped cycle failed: '+str(exc))
                    except Exception:
                        self.close();raise
                    failures.append(rid)
            # Deliberately retain overlap cursor while quarantined history exists.
            # Healthy events are deduplicated by durable deal IDs on every read.
            if rows and not self.controller.data['blocked']:
                from copy import deepcopy
                before=deepcopy(self.controller.data)
                try:
                    self.controller.data['observer_cursor_msc']=max(
                        self.controller.data.get('observer_cursor_msc') or 0,max(r.time_msc for r in rows))
                    self.controller._save()
                except Exception:
                    self.controller.data=before;self.close();raise
            return self.controller.state()
