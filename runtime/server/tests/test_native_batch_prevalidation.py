import json,tempfile,unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.native_api import create_native_app
from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_support_cycle import CycleFake,ea_response
import test_native_recovery_control as controls

class BatchTests(unittest.TestCase):
    session=controls.ControlTests.session
    def payload(self,n,second='3950'):
        return dict(request_id=str(uuid4()),expected_context=n.context_id(),expected_generation=(n.control._read() or {}).get('generation'),
            ranges=[dict(id='1',side='BUY',price='4000',volume='.01'),dict(id='2',side='SELL',price=second,volume='.01')],
            initial_sl_distance='.70',reentry_distance='.70')
    def test_invalid_second_has_zero_effects_and_preserves_existing_generation(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123));client=TestClient(create_native_app('x'*32,n));h={'Authorization':'Bearer '+'x'*32}
                self.assertEqual(client.post('/api/recovery/ranges',json=self.payload(n,'4010'),headers=h).status_code,409)
                self.assertEqual(a.sent,[]);self.assertIsNone(n.control._read());self.assertFalse(n._unfinished_api_command())
                self.assertEqual(client.post('/api/recovery/ranges',json=self.payload(n),headers=h).status_code,200)
                old=n.control._read();count=len(a.sent)
                self.assertEqual(client.post('/api/recovery/ranges',json=self.payload(n,'4010'),headers=h).status_code,409)
                self.assertEqual(n.control._read(),old);self.assertEqual(len(a.sent),count)

    def test_price_race_partial_terminal_keeps_first_protection_and_recovers(self):
        class Shift(CycleFake):
            def order_send(self,r):
                result=super().order_send(r)
                if r['action']==self.TRADE_ACTION_PENDING and result.order==51:self.ask=3949
                return result
        with tempfile.TemporaryDirectory() as d:
            a=Shift(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123));client=TestClient(create_native_app('x'*32,n));h={'Authorization':'Bearer '+'x'*32}
                body=self.payload(n);result=client.post('/api/recovery/ranges',json=body,headers=h)
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(result.json()['command']['outcome'],'partially_applied')
                self.assertEqual(result.json()['command']['range_outcomes'],[{'range_id':'1','status':'confirmed'},{'range_id':'2','status':'not_dispatched'}])
                self.assertEqual(len(a.sent),1);self.assertFalse(n._unfinished_api_command())
                # A crash before completion write can be reconciled without send.
                with n.control.db:n.control.db.execute("UPDATE native_api_commands SET phase='dispatching',result=NULL WHERE id=?",(body['request_id'],))
                count=len(a.sent)
                n.reconcile(expected_context=n.context_id(),expected_generation=n.control._read()['generation'])
                self.assertEqual(len(a.sent),count);self.assertFalse(n._unfinished_api_command())
                a.fill_order(51,88);a.ask=4000.46
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertAlmostEqual(a.positions[0].sl,3999.75)
                self.assertFalse(any(r.get('price')==3950 for r in a.sent))

    def test_two_full_stops_between_ticks_wait_exact_level_and_allow_new_ranges(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                n.command('start',dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88);a.ask=4000.21;n.tick()
                a.close(88);a.fill_order(52,89);a.close(89);a.ask=4000.21
                n.tick();ea_response(n.ledger,51,602,d,'3999.3');ea_response(n.ledger,52,604,d,'4000')
                count=len(a.sent)
                result=n.tick()
                self.assertEqual(result['tick']['status'],'waiting_exact_level')
                self.assertEqual(result['data']['range_modes']['1'],'waiting_exact_level')
                self.assertEqual(n.ledger.totals()['1']['full_sl_count'],2)
                self.assertEqual(n.tick()['tick']['status'],'waiting_exact_level');self.assertEqual(len(a.sent),count)
                a.ask=3999
                n.command('fresh',dict(kind='ranges',ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                self.assertEqual(n.ledger.totals(),{});self.assertEqual(len(n.ranges.history()),1)

    def test_broker_rejected_initials_have_truthful_terminal_receipts_and_recovery(self):
        from types import SimpleNamespace as N
        for reject_all in (False,True):
            class RejectInitial(CycleFake):
                TRADE_RETCODE_REJECT=10006
                def order_send(self,r):
                    if r['action']==self.TRADE_ACTION_PENDING and (reject_all or r['type']==self.ORDER_TYPE_SELL_STOP):
                        self.sent.append(r)
                        return N(retcode=10006,order=0,deal=0)
                    return super().order_send(r)
            with self.subTest(reject_all=reject_all),tempfile.TemporaryDirectory() as d:
                a=RejectInitial(d)
                with self.session(a,d+'/s.db') as s:
                    n=NativeCoordinator(NativeRecoveryControl(s,magic=123));client=TestClient(create_native_app('x'*32,n));h={'Authorization':'Bearer '+'x'*32}
                    body=self.payload(n)
                    response=client.post('/api/recovery/ranges',json=body,headers=h)
                    self.assertEqual(response.status_code,200,response.text)
                    command=response.json()['command']
                    self.assertEqual(command['outcome'],'rejected' if reject_all else 'partially_applied')
                    self.assertEqual(command['phase'],'rejected' if reject_all else 'completed')
                    self.assertEqual(command['range_outcomes'],[{'range_id':'1','status':'rejected' if reject_all else 'confirmed'},{'range_id':'2','status':'rejected'}])
                    self.assertFalse(n._unfinished_api_command())
                    count=len(a.sent)
                    # Existing terminal broker receipts recover even after HTTP
                    # completion was lost. No untouched stage is required.
                    with n.control.db:n.control.db.execute("UPDATE native_api_commands SET phase='dispatching',result=NULL WHERE id=?",(body['request_id'],))
                    n.reconcile(expected_context=n.context_id(),expected_generation=n.control._read()['generation'])
                    status=client.get('/api/recovery/commands/'+body['request_id'],headers=h).json()['command']
                    self.assertEqual(status['outcome'],command['outcome'])
                    self.assertEqual(status['range_outcomes'],command['range_outcomes'])
                    self.assertEqual(len(a.sent),count);self.assertFalse(n._unfinished_api_command())
