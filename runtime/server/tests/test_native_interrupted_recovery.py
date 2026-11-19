import tempfile,unittest
from copy import deepcopy
from decimal import Decimal
from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
from recovery.volume_schedule import volume_for_slot
from test_native_support_cycle import CycleFake,ea_response
import test_native_recovery_control as controls


class InterruptedTests(unittest.TestCase):
    session=controls.ControlTests.session
    def setup_chain(self,a,s,d,count=2,final_evidence=True):
        n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
        n.command('start',dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70'))
        current=n.control._read()['orders'][0];pid=88;a.fill_order(current['broker_order'],pid)
        for slot in range(1,count+1):
            p=a.positions[0];a.ask=p.price_open+.21 if p.type==0 else p.price_open-.20
            if slot<count:
                self.assertEqual(n.tick()['tick']['status'],'ok')
                following=n.control._read()['reverse_commands'][current['key']+':initial-reverse']
            stop=str(p.sl);a.close(pid)
            n.observer.observe('1',order_key=current['key']);n.ledger.record(current['broker_order'])
            if slot<count or final_evidence:
                ea_response(n.ledger,current['broker_order'],a.deals[-1].ticket,d,stop)
                n.ledger.resolve_sl(current['broker_order'])
            if slot<count:
                pid+=1;a.fill_order(following['broker_order'],pid);current=following
        return n,current,stop

    def test_exact_rearm_count_lot_and_limit(self):
        for count in (2,5,23,24):
            with self.subTest(count=count),tempfile.TemporaryDirectory() as d:
                a=CycleFake(d)
                with self.session(a,d+'/s.db') as s:
                    n,last,stop=self.setup_chain(a,s,d,count)
                    next_side='SELL' if last['intent']['side']=='BUY' else 'BUY'
                    a.ask=float(stop)+.4 if next_side=='SELL' else float(stop)-.4
                    before=len(a.sent);result=n.tick()
                    if count==24:
                        self.assertEqual(result['data']['range_modes']['1'],'manual');self.assertEqual(len(a.sent),before)
                    else:
                        self.assertEqual(result['tick']['status'],'ok',result['tick'])
                        order=a.orders[0]
                        self.assertEqual(Decimal(str(order.volume_initial)),volume_for_slot(count+1))
                        self.assertEqual(Decimal(str(order.price_open)),Decimal(stop))
                        self.assertEqual(len(a.sent),before+1)
                        key=last['key']+':initial-reverse'
                        self.assertEqual(n.control._read()['reverse_commands'][key]['broker_order'],order.ticket)
                        n.tick();self.assertEqual(len(a.sent),before+1)

    def test_crossed_level_waits_restart_then_one_exact_pending(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d)
                a.ask=float(stop)+.1;before=len(a.sent)
                self.assertEqual(n.tick()['tick']['status'],'waiting_exact_level')
                key=last['key']+':initial-reverse'
                self.assertEqual(Decimal(n.control._read()['interrupted_recovery'][key]['intent']['price']),Decimal(stop))
                self.assertEqual(len(a.sent),before)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                self.assertEqual(n.tick()['tick']['status'],'waiting_exact_level');self.assertEqual(len(a.sent),before)
                a.ask=float(stop)-.4
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertEqual(len(a.sent),before+1)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123));n.tick()
                self.assertEqual(len(a.sent),before+1)

    def test_unknown_send_after_broker_accept_never_duplicates(self):
        class Lost(CycleFake):
            lose=False
            def order_send(self,r):
                result=super().order_send(r)
                if self.lose and r['action']==self.TRADE_ACTION_PENDING:raise TimeoutError('lost receipt')
                return result
        with tempfile.TemporaryDirectory() as d:
            a=Lost(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d);a.ask=float(stop)-.4;a.lose=True
                result=n.tick();self.assertEqual(result['tick']['status'],'reconciliation_required')
                count=len(a.sent);self.assertEqual(len(a.orders),1)
                n.tick();self.assertEqual(len(a.sent),count)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                n.reconcile(expected_context=n.context_id(),expected_generation=n.control._read()['generation'])
                n.tick();self.assertEqual(len(a.sent),count)

    def test_unknown_owned_exposure_or_changed_proof_prevents_rearm(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d);a.ask=float(stop)+.1;n.tick()
                before=len(a.sent)
                # Changed config invalidates a saved exact recovery plan.
                data=n.control._read();data['strategy_config']['settings']['reentry_distance']='.80';n.control._save(data)
                a.ask=float(stop)-.4
                self.assertEqual(n.tick()['tick']['status'],'reconciliation_required')
                self.assertEqual(len(a.sent),before)

    def test_existing_successor_key_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d);a.ask=float(stop)-.4
                n.tick();key=last['key']+':initial-reverse';created=n.control._read()['reverse_commands'][key]
                # Exact mapped successor is cancelled externally. A later tick
                # must not restore it using the same or a newly invented key.
                order=a.orders.pop();order.state=a.ORDER_STATE_CANCELED;a.history.append(order)
                data=n.control._read();data['range_modes']['1']='interrupted';n.control._save(data)
                count=len(a.sent);result=n.tick()
                self.assertEqual(result['tick']['status'],'interrupted_chain')
                self.assertEqual(len(a.sent),count)
                self.assertEqual(n.control._read()['reverse_commands'][key]['broker_order'],created['broker_order'])

    def test_delayed_evidence_duplicate_reordered_history_rearms_once(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d,final_evidence=False)
                a.ask=float(stop)-.4;before=len(a.sent)
                self.assertEqual(n.tick()['tick']['status'],'waiting_evidence')
                self.assertEqual(len(a.sent),before)
                ea_response(n.ledger,last['broker_order'],a.deals[-1].ticket,d,stop)
                original=a.history_deals_get
                def duplicates(*args,**kwargs):
                    rows=original(*args,**kwargs)
                    return list(reversed(rows))+list(rows)
                a.history_deals_get=duplicates
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertEqual(n.ledger.totals()['1']['full_sl_count'],2)
                self.assertEqual(len(a.sent),before+1);n.tick();self.assertEqual(len(a.sent),before+1)

    def test_crash_before_send_and_after_verified_send_do_not_duplicate(self):
        for phase in ('before_send','after_verified'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as d:
                a=CycleFake(d)
                with self.session(a,d+'/s.db') as s:
                    n,last,stop=self.setup_chain(a,s,d);a.ask=float(stop)-.4;before=len(a.sent)
                    if phase=='before_send':
                        def crash(*args,**kwargs):raise RuntimeError('crash before outbox')
                        s.execute=crash
                    else:
                        saved=n.control._save
                        def crash(data):
                            if any(p['phase']=='verified' for p in data.get('interrupted_recovery',{}).values()):
                                raise RuntimeError('crash after broker proof before plan commit')
                            return saved(data)
                        n.control._save=crash
                    self.assertEqual(n.tick()['tick']['status'],'reconciliation_required')
                    self.assertEqual(len(a.sent),before+(phase=='after_verified'))
                    count=len(a.sent)
                with self.session(a,d+'/s.db') as s:
                    n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                    n.reconcile(expected_context=n.context_id(),expected_generation=n.control._read()['generation'])
                    result=n.tick()
                    self.assertEqual(len(a.sent),count)
                    self.assertEqual(result['tick']['status'],'reconciliation_required' if phase=='before_send' else 'ok')

    def test_generation_reset_discards_waiting_plan_and_never_replays_it(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d);a.ask=float(stop)+.1;n.tick()
                key=last['key']+':initial-reverse';old=n.control._read()['generation']
                a.ask=3999
                n.command('new',dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                self.assertNotEqual(n.control._read()['generation'],old)
                self.assertNotIn('interrupted_recovery',n.control._read())
                self.assertIsNone(n.control.db.execute('SELECT id FROM recovery_outbox WHERE id=?',(key,)).fetchone())
                before=len(a.sent);n.tick();self.assertEqual(len(a.sent),before)

    def test_new_owned_order_during_final_check_blocks_send(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d);a.ask=float(stop)-.4;before=len(a.sent)
                original=s.transport.snapshot;injected=False
                def race(scope):
                    nonlocal injected
                    if n.control._read().get('interrupted_recovery') and not injected:
                        injected=True
                        order=deepcopy(a.history[0]);order.ticket=999;order.volume_current=order.volume_initial;order.state=1;order.position_id=0
                        a.orders.append(order)
                    return original(scope)
                s.transport.snapshot=race
                self.assertEqual(n.tick()['tick']['status'],'reconciliation_required')
                self.assertEqual(len(a.sent),before)

    def test_two_filled_sibling_branches_are_not_selected_by_time(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                n.command('start',dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88);a.ask=4000.21;n.tick();a.ask=4000.46;n.tick();a.close(88);a.ask=3999.8
                n.tick();ea_response(n.ledger,51,602,d,'3999.75');n.tick()
                data=n.control._read();children=list(data['reverse_commands'].values())+list(data['reentry_commands'].values())
                for index,item in enumerate(children):
                    pid=89+index;a.fill_order(item['broker_order'],pid)
                    stop=str(next(p for p in a.positions if p.identifier==pid).sl)
                    a.close(pid)
                    n.observer.observe('1',order_key=item['key']);n.ledger.record(item['broker_order'])
                    ea_response(n.ledger,item['broker_order'],a.deals[-1].ticket,d,stop);n.ledger.resolve_sl(item['broker_order'])
                before=len(a.sent);result=n.tick()
                self.assertEqual(result['tick']['status'],'reconciliation_required')
                self.assertIn('Multiple terminal branches',' '.join(result['tick']['issues']))
                self.assertEqual(len(a.sent),before)

    def test_rejected_rearm_is_terminal_and_other_rejected_range_is_isolated(self):
        from types import SimpleNamespace as N
        class Reject(CycleFake):
            TRADE_RETCODE_REJECT=10006;reject_rearm=False
            def order_send(self,r):
                if r['action']==self.TRADE_ACTION_PENDING and (float(r['price'])==3950 or self.reject_rearm):
                    self.sent.append(r);return N(retcode=10006,order=0,deal=0)
                return super().order_send(r)
        for reject_rearm in (False,True):
            with self.subTest(reject_rearm=reject_rearm),tempfile.TemporaryDirectory() as d:
                a=Reject(d)
                with self.session(a,d+'/s.db') as s:
                    n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                    n.command('start',dict(kind='ranges',ranges=controls.RANGES+[dict(id='2',side='SELL',price='3950',volume='.01')],initial_sl_distance='.70',reentry_distance='.70'))
                    a.fill_order(51,88);a.ask=4000.21;n.tick();a.close(88);a.fill_order(52,89);a.close(89)
                    n.tick();ea_response(n.ledger,51,602,d,'3999.3');ea_response(n.ledger,52,604,d,'4000')
                    a.ask=3999.6;a.reject_rearm=reject_rearm;before=len(a.sent)
                    result=n.tick()
                    self.assertEqual(result['tick']['status'],'reconciliation_required' if reject_rearm else 'ok',result['tick'])
                    self.assertEqual(len(a.sent),before+1)
                    n.tick();self.assertEqual(len(a.sent),before+1)

    def test_retained_partial_entry_counts_one_full_stop_and_uses_scheduled_lot(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                n.command('start',dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88);a.ask=4000.21;n.tick();a.close(88)
                first=n.control._read()['orders'][0]
                n.observer.observe('1',order_key=first['key']);n.ledger.record(51)
                ea_response(n.ledger,51,602,d,'3999.3');n.ledger.resolve_sl(51)
                child=next(iter(n.control._read()['reverse_commands'].values()))
                a.fill_order(52,89)
                order=a.history.pop();order.state=1;order.volume_current=.006;a.orders.append(order)
                a.deals[-1].volume=.004;a.positions[0].volume=.004
                n.observer.observe('1',order_key=child['key'])
                n.observer.cancel_partial_remainder('1',order_key=child['key'])
                a.close(89)
                n.observer.observe('1',order_key=child['key']);n.ledger.record(52)
                ea_response(n.ledger,52,604,d,'4000');n.ledger.resolve_sl(52)
                a.ask=3999.6;before=len(a.sent)
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertEqual(n.ledger.totals()['1']['full_sl_count'],2)
                self.assertEqual(len(a.sent),before+1)
                self.assertAlmostEqual(a.orders[0].volume_current,.01)
                self.assertEqual(n.control._read()['close_ledger']['52']['filled_volume'],'0.004')

    def test_recovered_pending_fill_rejoins_normal_protection_and_reverse(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d);a.ask=float(stop)-.4;n.tick()
                recovered=n.control._read()['reverse_commands'][last['key']+':initial-reverse']
                a.fill_order(recovered['broker_order'],90);a.ask=float(stop)+.46
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertAlmostEqual(a.positions[0].sl,float(stop)-.25)
                successor=n.control._read()['reverse_commands'][recovered['key']+':initial-reverse']
                self.assertEqual(Decimal(successor['intent']['volume']),volume_for_slot(4))
                self.assertEqual(n.ledger.totals()['1']['full_sl_count'],2)
                count=len(a.sent);n.tick();self.assertEqual(len(a.sent),count)

    def test_corrupted_parent_identity_never_selects_a_successor(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n,last,stop=self.setup_chain(a,s,d);a.ask=float(stop)-.4
                data=n.control._read();data['reverse_commands'][last['key']]['origin_position_identifier']=999
                n.control._save(data);count=len(a.sent)
                self.assertEqual(n.tick()['tick']['status'],'reconciliation_required')
                self.assertEqual(len(a.sent),count)
