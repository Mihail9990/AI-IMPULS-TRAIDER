"""Protective close orders are observed, not adopted or traded. Offline only."""
from copy import deepcopy
from unittest.mock import patch
import unittest
from tradfi.related_close import protective_close
from tradfi.basket import TransientDataWait
from test_tradfi_mt5 import record
import test_tradfi_basket as fixtures

class RelatedCloseTest(unittest.TestCase):
    setUp=fixtures.NativeBasketTest.setUp
    tearDown=fixtures.NativeBasketTest.tearDown
    restart=fixtures.NativeBasketTest.restart
    tick=fixtures.NativeBasketTest.tick
    initial=fixtures.NativeBasketTest.initial

    def candidate(self,**changes):
        self.api.ORDER_REASON_TP=5;self.api.ORDER_REASON_SL=4
        p=self.api.positions[0]
        self.order=dict(ticket=99999,position_id=p.identifier,position_by_id=0,
            symbol=p.symbol,magic=p.magic,comment='broker TP',reason=5,type=1-p.type,
            state=1,volume_initial=p.volume,volume_current=p.volume,price_open=p.tp,
            sl=p.sl,tp=p.tp,time_setup_msc=self.api.now*1000,time_done_msc=0)
        self.order.update(changes);self.api.orders.append(record(**self.order))
        return self.order

    def test_pending_observed_not_owned_no_pause_or_mutation_then_close_once(self):
        self.initial();self.candidate()
        snap=self.bot.snapshot();r=next(o for o in snap['orders'] if o['id']=='99999')
        self.assertFalse(r['owned']);self.assertEqual(r['related_close']['status'],'PROTECTIVE_CLOSE_PENDING')
        phase=self.bot.state['phase']
        with patch.object(self.api,'order_send',side_effect=AssertionError('no send')),patch.object(self.api,'order_check',side_effect=AssertionError('no check')):
            self.tick(3)
        self.assertEqual(self.bot.state['phase'],phase)
        self.assertEqual(self.bot.reconciliation_diagnostics['reason'],'RELATED_PROTECTIVE_CLOSE_PENDING')
        with self.assertRaises(TransientDataWait):self.bot._guard()
        start=deepcopy(self.bot.state['related_close_wait']);self.restart();self.tick()
        self.assertEqual(self.bot.state['related_close_wait'],start)
        # Broker closes the position. Same position identifier, separate closing order.
        self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        self.api.close_position(reason=self.api.DEAL_REASON_TP)
        self.api.history[-1]=record(**dict(self.api.history[-1]._asdict(),ticket=99999,reason=5,position_by_id=0))
        self.api.deals[-1]=self.api.deals[-1]._replace(order=99999)
        self.tick(5)
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertNotIn('related_close_wait',self.bot.state)
        seen=deepcopy(self.bot.state['seen_deals']);self.restart();self.tick(2)
        self.assertEqual(self.bot.state['seen_deals'],seen)

    def test_unverified_link_still_pauses(self):
        self.initial();self.candidate(reason=0);self.tick()
        self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def test_existing_pause_never_cleared(self):
        self.initial();self.candidate();self.bot.pause('existing')
        self.tick(2);self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def test_bad_shapes_remain_foreign(self):
        self.initial();self.candidate();audit=self.broker.reconciliation_evidence()
        for changes in ({'position_id':123},{'position_by_id':2},{'type':4},{'type':0},
                        {'magic':1},{'symbol':'OTHER'},{'reason':0},{'state':4},
                        {'volume_current':0},{'volume_initial':.02},{'volume_current':.02},
                        {'volume_initial':0},{'time_setup_msc':1},{'ticket':next(iter(audit['obligations'].values())).get('order_id')}):
            with self.subTest(changes=changes):
                self.assertIsNone(protective_close(dict(self.order,**changes),audit,self.broker.rows,self.api,self.broker.symbol,self.broker.magic))
        for field in ('reason','position_id','volume_initial','time_setup_msc'):
            r=dict(self.order);r.pop(field)
            self.assertIsNone(protective_close(r,audit,self.broker.rows,self.api,self.broker.symbol,self.broker.magic))

    def test_duplicate_related_orders_not_ignored(self):
        self.initial();self.candidate();self.api.orders.append(record(**dict(self.order,ticket=99998)))
        self.tick();self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def test_unavailable_history_is_not_emptiness(self):
        self.initial();self.candidate();self.api.position_none=True
        with self.assertRaises(OSError):self.bot.snapshot()

    def test_candidate_cancelled_no_fabricated_close(self):
        self.initial();self.candidate();self.tick()
        self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        self.api.history.append(record(**dict(self.order,state=2,time_done_msc=self.api.now*1000+1)))
        self.tick();self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertFalse(next(iter(self.bot.state['cycle']['tickets'].values()))['closed'])

    def test_unrelated_order_still_pauses_alongside_candidate(self):
        self.initial();self.candidate();self.api.orders.append(record(**dict(self.order,ticket=99998,position_id=678)))
        self.tick();self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def test_partial_exact_order_volume_and_no_booking(self):
        self.initial();self.candidate();audit=self.broker.reconciliation_evidence()
        related=next(p for p in audit['obligations'].values() if str(p.get('position_id'))==str(self.order['position_id']))
        related.update(status='PARTIAL_CLOSE',closed_volume='0.005')
        entry=next(d for d in audit['raw_facts']['deal'].values() if str(d['position_id'])==str(self.order['position_id']))
        audit['raw_facts']['deal']['98765']=dict(entry,ticket=98765,order=99999,entry=1,type=1,volume=.005,reason=5)
        candidate=dict(self.order,volume_current=.005)
        self.assertIsNotNone(protective_close(candidate,audit,self.broker.rows,self.api,self.broker.symbol,self.broker.magic))
        for changes in ({'position_id':678},{'entry':0},{'type':0},{'magic':1}):
            with self.subTest(changes=changes):
                altered=deepcopy(audit);altered['raw_facts']['deal']['98765'].update(changes)
                self.assertIsNone(protective_close(candidate,altered,self.broker.rows,self.api,self.broker.symbol,self.broker.magic))

    def test_ambiguous_position_owner_or_missing_proof_not_classified(self):
        self.initial();self.candidate();audit=self.broker.reconciliation_evidence()
        cid=next(cid for cid,p in audit['obligations'].items() if str(p.get('position_id'))==str(self.order['position_id']))
        for status in ('UNKNOWN','PARTIAL_FILL','EXTERNAL_EXIT','API_EVIDENCE_UNAVAILABLE','UNSUPPORTED_EXIT'):
            a=deepcopy(audit);a['obligations'][cid]['status']=status
            self.assertIsNone(protective_close(self.order,a,self.broker.rows,self.api,self.broker.symbol,self.broker.magic))
        audit['obligations']['duplicate']=deepcopy(audit['obligations'][cid])
        self.assertIsNone(protective_close(self.order,audit,self.broker.rows,self.api,self.broker.symbol,self.broker.magic))

    def test_partial_close_integration_waits_without_booking_or_send(self):
        self.initial();self.candidate();self.tick()
        self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        self.api.close_position(reason=self.api.DEAL_REASON_TP,volume=.005)
        partial=dict(self.order,state=3,volume_current=.005)
        self.api.history[-1]=record(**partial)
        self.api.deals[-1]=self.api.deals[-1]._replace(order=99999)
        self.api.orders.append(record(**partial))
        phase=self.bot.state['phase'];seen=deepcopy(self.bot.state['seen_deals'])
        with patch.object(self.api,'order_send',side_effect=AssertionError('no send')),patch.object(self.api,'order_check',side_effect=AssertionError('no check')):
            self.tick(3)
        self.assertEqual(self.bot.state['phase'],phase)
        self.assertEqual(self.bot.state['seen_deals'],seen)
        self.assertFalse(next(iter(self.bot.state['cycle']['tickets'].values()))['closed'])
        self.assertEqual(self.bot.state['related_close_wait']['order_ids'],['99999'])

    def test_pending_other_ticket_does_not_prevent_passive_closed_accounting(self):
        fixtures.NativeBasketTest.reversal(self)
        self.api.close_position(reason=self.api.DEAL_REASON_TP)
        closed_pid=str(self.api.deals[-1].position_id)
        self.candidate()
        with patch.object(self.api,'order_send',side_effect=AssertionError('no send')),patch.object(self.api,'order_check',side_effect=AssertionError('no check')):
            self.tick(3)
        self.assertTrue(self.bot.state['cycle']['tickets'][closed_pid]['closed'])
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
        booked=deepcopy(self.bot.state['seen_deals'])
        self.restart();self.tick(2)
        self.assertEqual(self.bot.state['seen_deals'],booked)

    def test_unknown_command_not_replayed_while_related_close_pending(self):
        self.initial();self.candidate()
        cid='f'*24
        cmd=dict(id=cid,kind='MARKET',status='UNKNOWN',cycle_id=self.bot.state['cycle']['id'],side='BUY',size='0.01',price='4000',sl='3999',tp='4001')
        self.bot.state['commands'][cid]=cmd
        self.broker.rows[cid]=dict(command=deepcopy(cmd),account=self.broker.account,send_started=True,result={'status':'UNKNOWN'})
        self.bot.save('unknown_fixture')
        with patch.object(self.api,'order_send',side_effect=AssertionError('no send')),patch.object(self.api,'order_check',side_effect=AssertionError('no check')):
            self.tick(3)
        self.assertEqual(self.bot.state['commands'][cid]['status'],'UNKNOWN')
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def queue(self,kind,**kwargs):
        cid=self.bot._intent(kind,**kwargs);self.bot.save('resource_fixture');return cid

    def test_independent_cancel_executes_while_protective_close_pending(self):
        self.initial();self.candidate()
        target=next(o for o in self.api.orders if o.ticket!=99999)
        cid=self.queue('CANCEL',order_id=str(target.ticket));before=len(self.api.sends)
        self.tick()
        self.assertEqual(len(self.api.sends),before+1)
        self.assertFalse(any(o.ticket==target.ticket for o in self.api.orders))
        self.assertTrue(any(o.ticket==99999 for o in self.api.orders))
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.tick(2);self.assertEqual(len(self.api.sends),before+1)

    def test_independent_modify_and_close_execute(self):
        for kind in ('MODIFY','CLOSE'):
            with self.subTest(kind=kind):
                if self.bot.state.get('cycle'):self.tearDown();self.setUp()
                fixtures.NativeBasketTest.reversal(self);self.candidate()
                p=self.api.positions[1]
                args=dict(position_id=str(p.identifier),purpose='MANUAL' if kind=='CLOSE' else 'ANCHOR')
                args.update(size=str(p.volume),independent_owner_request=True) if kind=='CLOSE' else args.update(sl=str(p.sl),tp=str(p.tp))
                cid=self.queue(kind,**args);before=len(self.api.sends)
                self.tick()
                self.assertEqual(len(self.api.sends),before+1)
                self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
                self.assertTrue(any(o.ticket==99999 for o in self.api.orders))

    def test_conflicting_position_command_remains_ready_not_unknown(self):
        self.initial();self.candidate();p=self.api.positions[0]
        cid=self.queue('CLOSE',position_id=str(p.identifier),size=str(p.volume),purpose='MANUAL')
        before=len(self.api.sends);self.tick(3)
        self.assertEqual(self.bot.state['commands'][cid]['status'],'READY')
        self.assertNotIn(cid,self.broker.rows);self.assertEqual(len(self.api.sends),before)

    def test_adapter_rechecks_foreign_race_after_order_check_no_send(self):
        self.initial();self.candidate();target=next(o for o in self.api.orders if o.ticket!=99999)
        cid=self.queue('CANCEL',order_id=str(target.ticket));before=len(self.api.sends)
        original=self.api.order_check
        def check(req):
            result=original(req)
            self.api.orders.append(record(**dict(self.order,ticket=99998,position_id=678)))
            return result
        self.api.order_check=check;self.tick()
        self.assertEqual(len(self.api.sends),before)
        self.assertTrue(self.broker.result(cid)['never_sent'])
        self.assertFalse(self.broker.rows[cid]['send_started'])

    def test_same_resource_unknown_never_allows_second_request(self):
        fixtures.NativeBasketTest.reversal(self);self.candidate();p=self.api.positions[1]
        prior=self.queue('CLOSE',position_id=str(p.identifier),size=str(p.volume),purpose='MANUAL')
        self.bot.state['commands'][prior]['status']='UNKNOWN'
        cmd=deepcopy(self.bot.state['commands'][prior]);cmd.pop('status')
        self.broker.rows[prior]=dict(command=cmd,account=self.broker.account,send_started=True,result={'status':'UNKNOWN'})
        following=self.queue('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        before=len(self.api.sends);self.tick(2)
        self.assertEqual(self.bot.state['commands'][following]['status'],'READY')
        self.assertEqual(len(self.api.sends),before)

    def test_preflight_unavailable_keeps_ready_without_journal(self):
        self.initial();self.candidate();target=next(o for o in self.api.orders if o.ticket!=99999)
        cid=self.queue('CANCEL',order_id=str(target.ticket))
        with patch.object(self.bot,'snapshot',side_effect=OSError('unavailable')):
            self.bot.dispatch({'CANCEL'},ready_filter=self.bot._independent_action_ready)
        self.assertEqual(self.bot.state['commands'][cid]['status'],'READY')
        self.assertNotIn(cid,self.broker.rows)

    def test_final_post_journal_recheck_blocks_conflicting_candidate(self):
        self.initial();self.candidate();target=next(o for o in self.api.orders if o.ticket!=99999)
        cid=self.queue('CANCEL',order_id=str(target.ticket));before=len(self.api.sends)
        original=self.broker._request;calls=[]
        def request(cmd):
            calls.append(1)
            if len(calls)==3:
                self.api.orders.append(record(**dict(self.order,ticket=99998,position_id=678)))
            return original(cmd)
        self.broker._request=request;self.tick()
        self.assertEqual(len(calls),3)
        self.assertEqual(len(self.api.sends),before)
        self.assertTrue(self.broker.result(cid)['never_sent'])
        self.assertFalse(self.broker.rows[cid]['send_started'])

    def test_partial_close_allows_other_position_ready_management(self):
        fixtures.NativeBasketTest.reversal(self);self.candidate()
        self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        self.api.close_position(reason=self.api.DEAL_REASON_TP,volume=.005)
        partial=dict(self.order,state=3,volume_current=.005)
        self.api.history[-1]=record(**partial);self.api.deals[-1]=self.api.deals[-1]._replace(order=99999)
        self.api.orders.append(record(**partial));p=self.api.positions[1]
        cid=self.queue('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        before=len(self.api.sends);self.tick()
        self.assertEqual(len(self.api.sends),before+1)
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')
        self.assertFalse(self.bot.state['cycle']['tickets'][str(self.order['position_id'])]['closed'])

    def test_runtime_v3_dispatch_forwards_resource_filter(self):
        from tradfi.recovery_v3 import ControlledRecoveryController
        self.initial();self.candidate()
        # Bind the fixture's already evidenced tickets to current V3 schema,
        # not a migration test or automatic live-state conversion.
        self.bot.state['cycle'].update(recovery_mode='V3',range_policy='last_actual_close_v2',
            protection_policy='own_entry_v1',tp_distances=[None]*20)
        self.bot.save('v3_schema_fixture')
        self.bot=ControlledRecoveryController(self.store,self.broker,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        target=next(o for o in self.api.orders if o.ticket!=99999)
        self.queue('CANCEL',order_id=str(target.ticket));before=len(self.api.sends)
        self.tick()
        self.assertEqual(len(self.api.sends),before+1)
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def test_stop_cancels_own_pending_entries_not_protective_close(self):
        self.initial();self.candidate();self.bot.command('/stop');self.tick(3)
        self.assertEqual([o.ticket for o in self.api.orders],[99999])
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def test_adapter_rechecks_same_resource_unknown_journal(self):
        fixtures.NativeBasketTest.reversal(self);self.candidate();p=self.api.positions[1]
        cid=self.queue('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        before=len(self.api.sends);original=self.api.order_check
        def check(req):
            result=original(req)
            other=dict(id='d'*24,kind='CLOSE',position_id=str(p.identifier),size=str(p.volume),purpose='MANUAL',cycle_id=self.bot.state['cycle']['id'])
            self.bot.state['commands'][other['id']]=dict(other,status='UNKNOWN')
            self.bot.save('concurrent_unknown_fixture')
            self.broker._save_row(other['id'],dict(command=other,account=self.broker.account,send_started=True,result={'status':'UNKNOWN'}),'unknown_fixture')
            return result
        self.api.order_check=check;self.tick()
        self.assertEqual(len(self.api.sends),before)
        self.assertTrue(self.broker.result(cid)['never_sent'])

    def test_pnl_dependent_tp_and_automatic_close_stay_ready(self):
        fixtures.NativeBasketTest.reversal(self);self.candidate();p=self.api.positions[1]
        modify=self.queue('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp-.1),purpose='ANCHOR')
        close=self.queue('CLOSE',position_id=str(p.identifier),size=str(p.volume),purpose='MANUAL')
        before=len(self.api.sends);self.tick(2)
        self.assertEqual(self.bot.state['commands'][modify]['status'],'READY')
        self.assertEqual(self.bot.state['commands'][close]['status'],'READY')
        self.assertEqual(len(self.api.sends),before)

    def test_resolved_candidate_does_not_bypass_journal_recheck(self):
        fixtures.NativeBasketTest.reversal(self);self.candidate();p=self.api.positions[1]
        cid=self.queue('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        before=len(self.api.sends);original=self.api.order_check
        def check(req):
            result=original(req)
            self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
            self.api.history.append(record(**dict(self.order,state=2,time_done_msc=self.api.now*1000+1)))
            other=dict(id='e'*24,kind='CLOSE',position_id=str(p.identifier),size=str(p.volume),purpose='MANUAL',cycle_id=self.bot.state['cycle']['id'])
            self.bot.state['commands'][other['id']]=dict(other,status='UNKNOWN')
            self.bot.save('concurrent_unknown_fixture')
            self.broker._save_row(other['id'],dict(command=other,account=self.broker.account,send_started=True,result={'status':'UNKNOWN'}),'unknown_fixture')
            return result
        self.api.order_check=check;self.tick()
        self.assertEqual(len(self.api.sends),before)
        self.assertTrue(self.broker.result(cid)['never_sent'])

    def recovery_target(self):
        from tradfi.related_close import financial_basis
        fixtures.NativeBasketTest.reversal(self);self.candidate();p=self.api.positions[1]
        snap=self.bot.snapshot()
        cid=self.queue('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp-.1),
            purpose='RECOVERY_TARGET',recovery_target_basis=financial_basis(snap,self.bot.state))
        return cid

    def test_fresh_financial_basis_allows_independent_last_tp(self):
        cid=self.recovery_target();before=len(self.api.sends);self.tick()
        self.assertEqual(len(self.api.sends),before+1)
        self.assertNotEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def test_stale_financial_basis_remains_ready_before_submit(self):
        cid=self.recovery_target();self.bot.state['cycle']['recovery_revision']=1;self.bot.save('changed_basis')
        before=len(self.api.sends);self.tick()
        self.assertEqual(self.bot.state['commands'][cid]['status'],'READY')
        self.assertNotIn(cid,self.broker.rows);self.assertEqual(len(self.api.sends),before)

    def test_final_financial_recheck_rejects_changed_ledger_no_send(self):
        cid=self.recovery_target();before=len(self.api.sends);original=self.api.order_check
        def check(req):
            result=original(req)
            self.bot.state['cycle']['realized']='-0.10';self.bot.save('ledger_changed_fixture')
            return result
        self.api.order_check=check;self.tick()
        self.assertEqual(len(self.api.sends),before)
        self.assertTrue(self.broker.result(cid)['never_sent'])
        self.assertEqual(self.broker.result(cid)['reason'],'RECOVERY_TARGET_BASIS_CHANGED')

    def test_financial_basis_changes_when_source_closes_before_booked(self):
        from tradfi.related_close import financial_basis
        cid=self.recovery_target();before=self.bot.state['commands'][cid]['recovery_target_basis']
        self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
        self.api.close_position(reason=self.api.DEAL_REASON_TP)
        self.assertNotEqual(financial_basis(self.bot.snapshot(),self.bot.state),before)

    def test_candidate_disappears_and_external_tp_change_never_overwritten(self):
        fixtures.NativeBasketTest.reversal(self);self.candidate();p=self.api.positions[1]
        cid=self.queue('MODIFY',position_id=str(p.identifier),sl=str(p.sl),tp=str(p.tp),purpose='ANCHOR')
        before=len(self.api.sends);original=self.api.order_check
        def check(req):
            result=original(req)
            self.api.orders=[o for o in self.api.orders if o.ticket!=99999]
            self.api.history.append(record(**dict(self.order,state=2,time_done_msc=self.api.now*1000+1)))
            self.api.positions=[x._replace(tp=x.tp-.2) if x.identifier==p.identifier else x for x in self.api.positions]
            return result
        self.api.order_check=check;self.tick()
        self.assertEqual(len(self.api.sends),before)
        self.assertTrue(self.broker.result(cid)['never_sent'])
        self.assertEqual(self.broker.result(cid)['reason'],'INDEPENDENT_RESOURCE_CHANGED')
