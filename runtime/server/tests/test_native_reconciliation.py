import json
import tempfile
import unittest
from types import SimpleNamespace as N
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.native_api import create_native_app
from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_support_cycle import CycleFake
import test_native_recovery_control as controls


class RejectReverse(CycleFake):
    TRADE_RETCODE_REJECT=10006
    def order_send(self,r):
        if r['action']==self.TRADE_ACTION_PENDING and self.positions:
            self.sent.append(r)
            return N(retcode=self.TRADE_RETCODE_REJECT,order=0,deal=0)
        return super().order_send(r)


class RecoveryTests(unittest.TestCase):
    session=controls.ControlTests.session
    def test_rejected_reverse_keeps_support_but_never_retries_entry(self):
        with tempfile.TemporaryDirectory() as d:
            a=RejectReverse(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                n.command('start',dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88);a.ask=4000.21
                result=n.tick()
                self.assertEqual(result['tick']['status'],'reconciliation_required')
                pending_sends=sum(r['action']==a.TRADE_ACTION_PENDING for r in a.sent)
                from copy import deepcopy
                unknown=deepcopy(a.positions[0]);unknown.ticket=unknown.identifier=999
                a.positions.append(unknown);a.ask=4000.46
                before=len(a.sent)
                self.assertEqual(n.tick()['tick']['status'],'reconciliation_required')
                self.assertEqual(len(a.sent),before)
                a.positions.remove(unknown)
                a.ask=4000.46
                result=n.tick()
                self.assertEqual(result['tick']['status'],'degraded_protection_only')
                self.assertEqual(a.positions[0].sl,3999.75)
                self.assertFalse(result['executor_ready'])
                a.ask=4000.71
                result=n.tick()
                self.assertEqual(a.positions[0].sl,4000)
                self.assertEqual(sum(r['action']==a.TRADE_ACTION_PENDING for r in a.sent),pending_sends)
                self.assertEqual(result['tick']['status'],'degraded_protection_only')
                count=len(a.sent)
                n.reconcile(expected_context=n.context_id(),expected_generation=n.control._read()['generation'])
                self.assertEqual(len(a.sent),count)
                self.assertEqual(next(iter(n.control._read()['reverse_commands'].values()))['phase'],'rejected')
                a.close(88,reason=0)
                n.tick()
                self.assertEqual(n.ledger.totals()['1']['closed_positions'],1)
                self.assertEqual(n.ledger.totals()['1']['realized_net'],'-0.26')
                from recovery.native_api import native_widget_projection
                projected=native_widget_projection(n)
                self.assertTrue(projected['capabilities']['set_ranges'])
                self.assertFalse(projected['reconciliation_required'])
                rejected_key=next(iter(n.control._read()['reverse_commands']))
                with n.control.db:n.control.db.execute("UPDATE recovery_outbox SET state='UNKNOWN' WHERE id=?",(rejected_key,))
                self.assertFalse(native_widget_projection(n)['capabilities']['set_ranges'])
                with self.assertRaises(RuntimeError): n.command('unknown-reset',{'kind':'cancel'})
                with n.control.db:n.control.db.execute("UPDATE recovery_outbox SET state='REJECTED' WHERE id=?",(rejected_key,))
                # Flat explicit cancellation acknowledges only terminal reject;
                # it never retries the rejected pending order.
                n.command('reset',dict(kind='cancel'))
                self.assertEqual(sum(r['action']==a.TRADE_ACTION_PENDING for r in a.sent),pending_sends)
                self.assertEqual(n.control._read()['range_management']['phase'],'waiting')
                a.ask=3999
                n.command('fresh',dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                self.assertEqual(n.ledger.totals(),{})
                self.assertEqual(len(n.ranges.history()),1)

    def test_reconcile_http_completed_effect_and_unknown_missing_journal(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                key=str(uuid4())
                payload=dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70',
                             expected_context=n.context_id(),expected_generation=None)
                n.command(key,payload)
                # Model crash after concrete effects, before HTTP completion.
                with n.control.db:n.control.db.execute("UPDATE native_api_commands SET phase='dispatching',result=NULL WHERE id=?",(key,))
                client=TestClient(create_native_app('x'*32,n));headers={'Authorization':'Bearer '+'x'*32}
                body=dict(request_id=str(uuid4()),expected_context=n.context_id(),expected_generation=n.control._read()['generation'])
                count=len(a.sent)
                self.assertEqual(client.post('/api/recovery/reconcile',json=body).status_code,401)
                result=client.post('/api/recovery/reconcile',json=body,headers=headers)
                self.assertEqual(result.status_code,200,result.text)
                status=client.get('/api/recovery/commands/'+key,headers=headers).json()['command']
                self.assertEqual(status['phase'],'completed');self.assertEqual(status['outcome'],'confirmed')
                self.assertEqual(len(a.sent),count)
                # Missing concrete checkpoint cannot be invented or replayed.
                unknown=str(uuid4())
                with n.control.db:
                    n.control.db.execute('INSERT INTO native_api_commands VALUES(?,?,?,NULL)',(unknown,json.dumps(dict(kind='cancel',expected_context=n.context_id(),expected_generation=body['expected_generation'])),'dispatching'))
                result=client.post('/api/recovery/reconcile',json=body,headers=headers)
                self.assertFalse(result.json()['executor_ready'])
                status=client.get('/api/recovery/commands/'+unknown,headers=headers).json()['command']
                self.assertEqual(status['phase'],'dispatching');self.assertEqual(len(a.sent),count)

    def test_degraded_protects_later_sibling_fill_without_new_entries(self):
        with tempfile.TemporaryDirectory() as d:
            a=RejectReverse(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                ranges=[dict(id='1',side='BUY',price='4000',volume='.01'),
                        dict(id='2',side='SELL',price='3950',volume='.01')]
                n.command('start',dict(kind='ranges',ranges=ranges,initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88);a.ask=4000.21;n.tick()
                # Range 2's original pending remains live and later executes.
                a.close(88,reason=0)
                a.fill_order(52,89,price=3949.98);a.ask=3949.70
                pending_before=sum(r['action']==a.TRADE_ACTION_PENDING for r in a.sent)
                result=n.tick()
                self.assertEqual(result['tick']['status'],'degraded_protection_only')
                position=next(p for p in a.positions if p.identifier==89)
                self.assertAlmostEqual(position.sl,3950.23)
                self.assertEqual(n.control._read()['confirmed_protection']['89']['stage'],1)
                self.assertEqual(sum(r['action']==a.TRADE_ACTION_PENDING for r in a.sent),pending_before)

    def test_degraded_partial_sibling_cancel_late_fill_and_restart_no_replay(self):
        class LatePartial(RejectReverse):
            late_fill=False
            def order_send(self,r):
                if r['action']==self.TRADE_ACTION_REMOVE and r['order']==52 and self.late_fill:
                    self.late_fill=False
                    order=next(o for o in self.orders if o.ticket==52)
                    p=next(p for p in self.positions if p.identifier==89)
                    order.volume_current=.004;p.volume=.006
                    self.deals.append(N(ticket=self.next_deal,order=52,position_id=89,magic=123,symbol=order.symbol,
                        entry=0,type=1,volume=.002,price=3950,time_msc=self.next_deal,reason=3,
                        profit=0,commission=0,swap=0,fee=0));self.next_deal+=1
                return super().order_send(r)
        with tempfile.TemporaryDirectory() as d:
            a=LatePartial(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                ranges=[dict(id='1',side='BUY',price='4000',volume='.01'),dict(id='2',side='SELL',price='3950',volume='.01')]
                n.command('start',dict(kind='ranges',ranges=ranges,initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88);a.ask=4000.21;n.tick();a.close(88,reason=0)
                order=next(o for o in a.orders if o.ticket==52)
                order.volume_current=.006;order.position_id=89
                a.deals.append(N(ticket=a.next_deal,order=52,position_id=89,magic=123,symbol=order.symbol,
                    entry=0,type=1,volume=.004,price=3950,time_msc=a.next_deal,reason=3,
                    profit=0,commission=0,swap=0,fee=0));a.next_deal+=1
                a.positions.append(N(ticket=189,identifier=89,magic=123,symbol=order.symbol,type=1,
                    volume=.004,price_open=3950,sl=order.sl,tp=0))
                a.ask=3949.70;a.late_fill=True
                before=sum(r['action']==a.TRADE_ACTION_PENDING for r in a.sent)
                result=n.tick()
                self.assertEqual(result['tick']['status'],'degraded_protection_only',result['tick'])
                self.assertFalse(any(o.ticket==52 for o in a.orders))
                self.assertAlmostEqual(a.positions[0].volume,.006)
                self.assertAlmostEqual(a.positions[0].sl,3950.25)
                self.assertEqual(sum(r['action']==a.TRADE_ACTION_PENDING for r in a.sent),before)
                self.assertEqual(sum(r['action']==a.TRADE_ACTION_REMOVE for r in a.sent),1)
                count=len(a.sent)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                self.assertEqual(n.tick()['tick']['status'],'degraded_protection_only')
                self.assertEqual(len(a.sent),count)
