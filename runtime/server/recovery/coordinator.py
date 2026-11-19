"""Mock-only API/pipeline coordinator. Single controller is authoritative.

Cancellation is a verified multi-step workflow, not an atomic broker transaction.
Unknown/partial outcomes never commit a range replacement or auto-retry requests.
"""
import json
from copy import deepcopy
from .engine import Engine


class Coordinator:
    def __init__(self,pipeline,market_filling=None):
        pipeline.transport._test_lock()
        self.pipeline=pipeline
        self.controller=pipeline.controller
        self.market_filling=market_filling

    def _require_owner(self):
        if not getattr(self.controller,'_service_owned',False):
            raise ValueError('Active service ownership required')

    def state(self):
        with self.controller.lock:
            self._require_owner()
            result=self.controller.engine().state()
            result['startup_ready']=getattr(self.controller,'_startup_ready',False)
            result['blocked']=deepcopy(self.controller.data['blocked'])
            result['execution_environment']='test_double_only'
            result['live_trading']=False
            return result

    def _persist(self,update):
        old=deepcopy(self.controller.data)
        try:update(self.controller.data);self.controller._save()
        except Exception:self.controller.data=old;raise

    def command(self,request_id,payload):
        """Dedup API command across restart; running/unknown cannot be replayed."""
        self.pipeline.transport._test_lock()
        encoded=json.dumps(payload,sort_keys=True)
        with self.controller.lock:
            self._require_owner()
            if not getattr(self.controller,'_startup_ready',False):
                raise ValueError('Startup reconciliation unresolved; commands blocked')
            old=self.controller.data.get('api_requests',{}).get(request_id)
            if old:
                if old['payload']!=encoded:raise ValueError('Request ID reused with another payload')
                if old['status']=='complete':return old['result']
                raise ValueError('Command incomplete/failed; inspect state, do not repeat execution')
            self._persist(lambda d:d.setdefault('api_requests',{}).update({request_id:{'payload':encoded,'status':'running'}}))
            try:
                result=self._execute(request_id,payload)
                self._persist(lambda d:d['api_requests'][request_id].update(status='complete',result=result))
                return result
            except Exception as exc:
                # If persistence itself fails, durable running remains and cannot
                # replay side effects on restart. Original exception stays visible.
                try:self._persist(lambda d:d['api_requests'][request_id].update(status='failed',error=str(exc)))
                except Exception:pass
                raise

    def _execute(self,request_id,payload):
        kind=payload['kind']
        if kind=='close_cancel':return self._close_cancel(request_id,payload['range_id'])
        if kind not in ('ranges','cancel'):raise ValueError('Unsupported command')
        if self.controller.data['blocked']:
            raise ValueError('Unresolved broker state blocks range replacement/cancellation')
        candidate=Engine(self.controller.data['engine'])
        if kind=='ranges':
            candidate.replace_ranges(payload['ranges'],payload.get('initial_sl_distance',.70),payload.get('reentry_distance',.70))
        else:candidate.cancel_ranges()
        # Never remove live orders merely because local position array is empty.
        snapshot=self.pipeline.transport.snapshot(self.pipeline.scope)
        if snapshot['positions']:raise ValueError('Broker has open positions; range cancellation disabled')
        owned=[o for o in snapshot['orders'] if o['symbol']==self.pipeline.scope['symbol'] and o['magic']==self.pipeline.scope['magic']]
        bindings=self.controller.data['bindings']
        for o in owned:
            b=bindings.get(str(o['ticket']))
            if b is None:raise ValueError('Unmapped owned broker order; reconcile before changing ranges')
            request={'action':'TRADE_ACTION_REMOVE','order':o['ticket']}
            key='cancel:'+request_id+':'+str(o['ticket'])
            self.pipeline.executor.prepare(key,request,self.pipeline.scope)
            self.pipeline.executor.submit_once(key)
            result=self.pipeline.executor.reconcile(key,refresh=True)
            if result['state']!='VERIFIED':
                self.pipeline._blocked(b['range_id'],'Cancellation not confirmed: '+result['state'])
                raise ValueError('Cancellation unconfirmed; old range state retained')
        final=self.pipeline.transport.snapshot(self.pipeline.scope)
        if final['positions']:raise ValueError('Position appeared during cancellation; reconcile before commit')
        if any(o['symbol']==self.pipeline.scope['symbol'] and o['magic']==self.pipeline.scope['magic'] for o in final['orders']):
            raise ValueError('Owned order remains; cancellation incomplete')
        def commit(d):
            d['engine']=candidate.data
            for b in d['bindings'].values():
                if b['phase']=='pending':b['phase']='cancelled'
            d['blocked']={}
        self._persist(commit)
        if kind=='ranges':
            self.pipeline.sync_pending()
            if self.controller.data['blocked']:
                raise ValueError('New ranges only partly reconciled; inspect actual state')
        return self.state()

    def _close_cancel(self,request_id,rid):
        from .maintenance import Maintenance
        if self.market_filling not in ('ORDER_FILLING_IOC','ORDER_FILLING_FOK'):
            raise ValueError('Explicit tested market filling policy not configured')
        if self.controller.data['blocked']:
            raise ValueError('Unresolved broker state blocks automatic close/cancel workflow')
        engine=self.controller.engine()
        r=engine._range(rid)
        positions=[p for p in engine.data['positions'] if p['range_id']==rid]
        if not r or len(positions)!=1:raise ValueError('Expected one open position in selected range')
        matches=[b for b in self.controller.data['bindings'].values() if b['engine_position_id']==positions[0]['id']]
        if len(matches)!=1 or matches[0]['phase']!='open':
            raise ValueError('Partial/ambiguous position cannot use full close workflow')
        binding=matches[0];identifier=binding['position_identifier']
        # Disarm strategy first; unknown result cannot restart the range automatically.
        def disarm(d):
            range_state=next(x for x in d['engine']['ranges'] if x['id']==rid)
            range_state['run_state']='closing'
        self._persist(disarm)
        try:
            snapshot=self.pipeline.transport.snapshot(self.pipeline.scope)
            for o in snapshot['orders']:
                if o['magic']!=self.pipeline.scope['magic'] or o['symbol']!=self.pipeline.scope['symbol']:continue
                mapped=self.controller.data['bindings'].get(str(o['ticket']))
                if mapped is None:raise ValueError('Unmapped owned order prevents close/cancel')
                if mapped['range_id']!=rid:continue
                Maintenance(self.pipeline)._cancel('close-range-cancel:'+request_id+':'+str(o['ticket']),o['ticket'])
            proof=Maintenance(self.pipeline).close_verified_position(identifier,request_id,filling=self.market_filling)
            if proof['state']!='VERIFIED':raise ValueError('Close not verified')
            # Ingest real-shaped confirmed exit history for actual realized P/L.
            # No fabricated close/P&L is applied to Engine from request prices.
            self.pipeline.poll_history()
            current=self.controller.engine()
            if any(p['range_id']==rid for p in current.data['positions']):
                raise ValueError('Exit history did not finalize strategy position')
            if binding['phase']!='closed':raise ValueError('Closing deal aggregation incomplete')
            final=self.pipeline.transport.snapshot(self.pipeline.scope)
            if any(p['identifier']==identifier for p in final['positions']):
                raise ValueError('Position remains after close')
            for o in final['orders']:
                b=self.controller.data['bindings'].get(str(o['ticket']))
                if b and b['range_id']==rid:raise ValueError('Range pending order remains')
            ranged=current._range(rid)
            current._archive(ranged,'closed_and_cancelled')
            current.data['ranges'].remove(ranged)
            current.data['orders']=[o for o in current.data['orders'] if o['range_id']!=rid]
            if current.data['active_range']==rid:current.data['active_range']=None
            def commit(d):
                d['engine']=current.data
                for b in d['bindings'].values():
                    if b['range_id']==rid and b['phase']=='pending':b['phase']='cancelled'
            self._persist(commit)
            return self.state()
        except Exception as exc:
            self.pipeline._blocked(rid,'Close/cancel incomplete: '+str(exc))
            raise
