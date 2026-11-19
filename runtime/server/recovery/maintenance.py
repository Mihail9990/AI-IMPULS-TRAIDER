"""Explicit mock-only maintenance operations; no autonomous recovery policy."""
from copy import deepcopy
from .engine import Engine, number
from .mt5_plan import pending, close_position


class Maintenance:
    def __init__(self,pipeline):
        pipeline.transport._test_lock()
        self.p=pipeline;self.c=pipeline.controller
        self.c.engine().validate_distance_policy()

    def _save(self,mutate):
        before=deepcopy(self.c.data)
        try:mutate(self.c.data);self.c._save()
        except Exception:self.c.data=before;raise

    def _cancel(self,key,ticket):
        self.p.executor.prepare(key,{'action':'TRADE_ACTION_REMOVE','order':ticket},self.p.scope)
        self.p.executor.submit_once(key)
        result=self.p.executor.reconcile(key,refresh=True)
        if result['state']!='VERIFIED':raise ValueError('Cancellation unconfirmed; reconciliation required')
        return result

    def replace_pending_volume(self,logical_order_id,operation_id):
        """Cancel+new ticket at same desired price; never alter a filled order.

        Currently only when broker has no open positions. This conservative guard
        also blocks replacement while the other range is open; do not relax silently.
        """
        self.p.transport._test_lock()
        with self.c.lock:
            candidate=self.c.engine()
            desired=next((o for o in candidate.data['orders'] if o['id']==logical_order_id),None)
            matches=[(int(t),b) for t,b in self.c.data['bindings'].items() if b['logical_order_id']==logical_order_id and b['phase']=='pending']
            if not desired or len(matches)!=1:raise ValueError('Pending mapping ambiguous or absent')
            ticket,b=matches[0];rid=b['range_id']
            # Validate replacement BEFORE cancellation; crossed target is never
            # silently converted into a market order or moved to another level.
            s,q=self.p.transport._symbol(self.p.scope['symbol'])
            pending(desired,s,q,self.p.scope['magic'])
            snapshot=self.p.transport.snapshot(self.p.scope)
            if snapshot['positions']:raise ValueError('Open exposure blocks pending replacement')
            self._cancel('replace-cancel:'+operation_id+':'+str(ticket),ticket)
            snapshot=self.p.transport.snapshot(self.p.scope)
            if snapshot['positions']:
                self.p._blocked(rid,'Position appeared during replacement');raise ValueError('Fill race; replacement not created')
            # New identity avoids binding two broker tickets to one logical order.
            old_id=desired['id'];desired['id']=candidate._id('order')
            def commit(d):
                d['engine']=candidate.data
                d['bindings'][str(ticket)]['phase']='cancelled'
                reason=d['blocked'].get(rid,'')
                if reason=='Pending broker volume differs; cancel/replace reconciliation required':d['blocked'].pop(rid,None)
                d.setdefault('maintenance',[]).append({'kind':'replace','operation_id':operation_id,'old':old_id,'new':desired['id']})
            self._save(commit)
            self.p.sync_pending()
            return self.c.state()

    def cancel_partial_remainder(self,entry_order_ticket,operation_id,*,history_poll=None):
        """Explicit cancel of unfilled remainder; observed partial exposure is retained.

        User chose to retain and manage actual partial exposure. Subsequent trigger
        volume follows the agreed full-SL schedule, not the partial position size.
        """
        self.p.transport._test_lock()
        with self.c.lock:
            b=self.c.data['bindings'].get(str(entry_order_ticket))
            if not b or b['phase']!='partial_entry':raise ValueError('Expected partial-entry binding')
            rid=b['range_id']
            before=self.p.transport.snapshot(self.p.scope)
            prior=[p for p in before['positions'] if p['identifier']==b['position_identifier']]
            if len(prior)!=1:raise ValueError('Partial position identity unresolved before cancellation')
            self._cancel('partial-remainder:'+operation_id+':'+str(entry_order_ticket),entry_order_ticket)
            # Ingest newly visible fills/exits before accepting an unchanged NET
            # volume as proof. IN+OUT races can otherwise hide behind equal size.
            try:
                (history_poll or self.p.poll_history)()
                b=self.c.data['bindings'][str(entry_order_ticket)]
                snapshot=self.p.transport.snapshot(self.p.scope)
                matches=[p for p in snapshot['positions'] if p['identifier']==b['position_identifier']]
                if len(matches)!=1:raise ValueError('Partial position identity changed')
                actual=matches[0]
                entered=number(b['entry_volume']);remaining=entered-number(b['exit_volume'])
                if b['phase']!='partial_entry' or rid in self.c.data['blocked'] or number(b['exit_volume'])!=0:
                    raise ValueError('Partial entry lifecycle changed during cancellation')
                if (any(b.get(k)!=v for k,v in self.p.scope.items()) or
                    actual['ticket']!=prior[0]['ticket'] or
                    actual['symbol']!=self.p.scope['symbol'] or actual['magic']!=self.p.scope['magic'] or
                    actual['side']!=b['side'] or entered<=0 or remaining<=0 or
                    number(actual['volume'])!=remaining or
                    number(actual['entry'])!=number(b['entry_notional'])/entered):
                    raise ValueError('Partial identity/side/entry/volume differs from confirmed deal ledger')
            except Exception:
                self.p._blocked(rid,'Partial cancellation requires history/identity/entry/volume reconciliation')
                raise
            def commit(d):
                binding=d['bindings'][str(entry_order_ticket)]
                candidate=self.c.engine()
                logical=next((o for o in candidate.data['orders'] if o['id']==binding['logical_order_id']),None)
                if logical is None:raise ValueError('Partial logical intent missing')
                if number(binding['exit_volume'])!=0:
                    raise ValueError('Partial exit during cancellation; reconcile before promotion')
                binding['requested_volume']=binding['expected_volume']
                binding['expected_volume']=str(remaining)
                binding['remainder_cancel_confirmed']=True
                binding['partial_entry_accepted']=True
                logical['volume']=str(remaining)
                candidate._range(rid).pop('next_volume_policy_pending',None)
                candidate.fill('aggregate-fill:'+binding['logical_order_id'],binding['logical_order_id'],
                               str(number(binding['entry_notional'])/remaining))
                pos=next(p for p in candidate.data['positions'] if p['range_id']==rid)
                binding['engine_position_id']=pos['id']
                binding['phase']='open'
                d['engine']=candidate.data
            self._save(commit)
            return self.c.state()

    def close_verified_position(self,position_identifier,operation_id,*,filling):
        """Explicit mock full-close primitive, not automatic range disposal.

        Requires all mapped pending orders of this range already confirmed absent.
        Returns broker proof; caller must ingest close deals before disposing range.
        Partial/unknown result is never retried automatically.
        """
        self.p.transport._test_lock()
        with self.c.lock:
            matches=[b for b in self.c.data['bindings'].values() if b['position_identifier']==position_identifier]
            if len(matches)!=1:raise ValueError('Position binding unresolved')
            b=matches[0];rid=b['range_id']
            snapshot=self.p.transport.snapshot(self.p.scope)
            for o in snapshot['orders']:
                mapped=self.c.data['bindings'].get(str(o['ticket']))
                if mapped is None and o['magic']==self.p.scope['magic'] and o['symbol']==self.p.scope['symbol']:
                    raise ValueError('Unmapped owned pending order blocks close')
                if mapped and mapped['range_id']==rid:
                    raise ValueError('Cancel and verify range pending orders before close')
            rows=[p for p in snapshot['positions'] if p['identifier']==position_identifier]
            if len(rows)!=1:raise ValueError('Position absent or ambiguous')
            actual=rows[0];s,q=self.p.transport._symbol(self.p.scope['symbol'])
            request=close_position(actual,s,q,expected_ticket=actual['ticket'],
                expected_identifier=position_identifier,expected_magic=self.p.scope['magic'],filling=filling)
            expected=dict(self.p.scope,identifier=position_identifier)
            key='market-close:'+operation_id+':'+str(position_identifier)
            self.p.executor.prepare(key,request,expected);self.p.executor.submit_once(key)
            result=self.p.executor.reconcile(key,refresh=True)
            if result['state']!='VERIFIED':
                self.p._blocked(rid,'Market close not fully confirmed: '+result['state'])
                raise ValueError('Close unknown/partial; preserve state and reconcile')
            return result

    def inspect_startup(self):
        """Read-only reconstruction report. Never cancels/closes or replays on restart."""
        self.p.transport._test_lock()
        snapshot=self.p.transport.snapshot(self.p.scope)
        scope=self.p.scope
        orders=[o for o in snapshot['orders'] if o['magic']==scope['magic'] and o['symbol']==scope['symbol']]
        positions=[p for p in snapshot['positions'] if p['magic']==scope['magic'] and p['symbol']==scope['symbol']]
        known_orders={int(t) for t in self.c.data['bindings']}
        known_positions={b['position_identifier'] for b in self.c.data['bindings'].values() if b['position_identifier'] is not None}
        unknown_orders=[o for o in orders if o['ticket'] not in known_orders]
        unknown_positions=[p for p in positions if p['identifier'] not in known_positions]
        observed_orders={o['ticket'] for o in orders}
        observed_positions={p['identifier'] for p in positions}
        missing_orders=[int(t) for t,b in self.c.data['bindings'].items() if b['phase']=='pending' and int(t) not in observed_orders]
        missing_positions=[b['position_identifier'] for b in self.c.data['bindings'].values() if b['phase'] in ('open','partial_entry','partial_exit','partial_remainder_cancelled') and b['position_identifier'] not in observed_positions]
        unresolved=[row[0] for row in self.p.executor.db.execute("SELECT id FROM recovery_outbox WHERE state IN ('PREPARED','SENT','UNKNOWN','ACKNOWLEDGED')")]
        mismatches=[]
        for p in positions:
            bindings=[b for b in self.c.data['bindings'].values() if b['position_identifier']==p['identifier']]
            if len(bindings)!=1:
                mismatches.append('Ambiguous position '+str(p['identifier']));continue
            b=bindings[0]
            label='Position '+str(p['identifier'])
            entered=number(b['entry_volume'])
            remaining=entered-number(b['exit_volume'])
            if (b['phase'] not in ('open','partial_entry','partial_exit','partial_remainder_cancelled')
                or any(b.get(k)!=v for k,v in scope.items()) or remaining<=0 or entered<=0):
                mismatches.append(label+' binding phase/scope/volume');continue
            expected_volume=number(b['expected_volume'])
            if (entered>expected_volume or
                (b['phase']=='open' and (entered!=expected_volume or number(b['exit_volume'])!=0)) or
                (b['phase']=='partial_exit' and (entered!=expected_volume or number(b['exit_volume'])<=0)) or
                (b['phase'] in ('partial_entry','partial_remainder_cancelled') and
                 (entered>=expected_volume or number(b['exit_volume'])!=0))):
                mismatches.append(label+' phase volumes')
            if p['side']!=b['side']:
                mismatches.append(label+' side')
            if number(p['volume'])!=remaining:
                mismatches.append(label+' volume')
            if number(p['entry'])!=number(b['entry_notional'])/entered:
                mismatches.append(label+' entry')
            logical=next((v for v in self.c.data['engine']['positions'] if v['id']==b.get('engine_position_id')),None)
            if logical:
                if (logical['range_id']!=b['range_id'] or logical['side']!=b['side'] or
                    number(logical['entry'])!=number(p['entry'])):
                    mismatches.append(label+' logical identity/entry')
                if number(p['sl'])!=number(logical['sl']):
                    mismatches.append(label+' SL')
            elif b['phase'] in ('partial_entry','partial_remainder_cancelled'):
                # No strategy position yet: require durable proof of protection
                # for this exact observed volume. Never assume SL from initial order.
                if (b.get('verified_sl') is None or b.get('protected_volume') is None or
                    number(b['protected_volume'])!=remaining or
                    number(b['verified_sl'])<=0 or number(p['sl'])!=number(b['verified_sl'])):
                    mismatches.append(label+' partial protection unverified')
            else:
                mismatches.append(label+' missing logical position')
        desired={o['id']:o for o in self.c.data['engine']['orders']}
        for o in orders:
            b=self.c.data['bindings'].get(str(o['ticket']))
            if not b:continue  # already reported unknown above
            logical=desired.get(b['logical_order_id'])
            label='Pending '+str(o['ticket'])
            if (b['phase']!='pending' or not logical or
                any(b.get(k)!=v for k,v in scope.items()) or
                (logical and (logical['range_id']!=b['range_id'] or logical['side']!=b['side'] or
                              logical['status']!='simulated_pending'))):
                mismatches.append(label+' identity/logical binding');continue
            expected={'type':'ORDER_TYPE_BUY_STOP' if logical['side']=='BUY' else 'ORDER_TYPE_SELL_STOP',
                      'price':logical['price'],'sl':logical['sl'],'tp':'0','volume':logical['volume']}
            if o['type']!=expected['type']:
                mismatches.append(label+' type')
            for field in ('price','sl','tp','volume'):
                if number(o[field])!=number(expected[field]):
                    mismatches.append(label+' '+field)
            if number(o['volume'])!=number(b['expected_volume']):
                mismatches.append(label+' binding volume')
        return {'mode':'simulation','unknown_owned_orders':unknown_orders,
                'snapshot_mismatches':mismatches,
                'unknown_owned_positions':unknown_positions,'unresolved_intents':unresolved,
                'missing_mapped_orders':missing_orders,'missing_mapped_positions':missing_positions,
                'safe_to_resume':not (mismatches or unknown_orders or unknown_positions or missing_orders or missing_positions or unresolved or self.c.data['blocked']),
                'warning':'Read-only report, not proof against fills after snapshot'}
