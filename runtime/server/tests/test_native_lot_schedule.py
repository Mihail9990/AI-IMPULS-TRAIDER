import sqlite3
from decimal import Decimal
import tempfile
import unittest
from types import SimpleNamespace as N
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient

from recovery.native_api import create_native_app
from recovery.native_coordinator import NativeCoordinator
from recovery.native_lot_schedule import DEFAULT_LOT_SCHEDULE
from recovery.native_recovery_control import NativeRecoveryControl
import test_native_recovery_control as controls
from test_native_recovery_control import RANGES
from test_native_support_cycle import CycleFake


class NativeLotScheduleTests(unittest.TestCase):
    session = controls.ControlTests.session

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.path = work.name + '/state.db'
        self.fake = CycleFake(work.name)
        self.headers = {'Authorization': 'Bearer ' + 'x' * 32}

    def build(self, s):
        return NativeCoordinator(NativeRecoveryControl(s, magic=123))

    def client(self, n):
        return TestClient(create_native_app('x' * 32, n))

    def settings(self, n, values=None, *, expected=None, key=None):
        if values is None:
            values = list(DEFAULT_LOT_SCHEDULE)
            values[1] = '0.03'
        data = n.control._read() or {}
        return dict(request_id=str(uuid4()) if key is None else key,
                    expected_context=n.context_id(), expected_generation=data.get('generation'),
                    lot_schedule=values, expected_lot_schedule=n.control.lot_schedule() if expected is None else expected)

    def initial(self, n):
        n.command('ranges', dict(kind='ranges', ranges=RANGES,
                                 initial_sl_distance='.70', reentry_distance='.70'))

    def test_idle_pending_unchanged_save_and_restart_applies_new_reverse_lot(self):
        with self.session(self.fake, self.path) as s:
            n = self.build(s); self.initial(n); client = self.client(n)
            snapshot = client.get('/api/recovery/state', headers=self.headers).json()['widget']
            self.assertIs(snapshot['capabilities']['set_lot_schedule'], True)
            self.assertEqual(snapshot['lot_schedule'], list(DEFAULT_LOT_SCHEDULE))
            old = list(self.fake.sent)
            body = self.settings(n)
            self.assertEqual(client.post('/api/recovery/lot-schedule', json=body).status_code, 401)
            r = client.post('/api/recovery/lot-schedule', headers=self.headers, json=body)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()['widget']['lot_schedule'], body['lot_schedule'])
            self.assertEqual(r.json()['command']['outcome'], 'confirmed')
            self.assertEqual(self.fake.sent, old)
            receipt_path = '/api/recovery/commands/' + body['request_id']
            receipt = client.get(receipt_path, headers=self.headers).json()['command']
            self.assertEqual(receipt['payload'], {k:v for k,v in dict(body, kind='lot_schedule').items() if k!='request_id'})
        with self.session(self.fake, self.path) as s:
            n = self.build(s); client = self.client(n)
            self.assertEqual(n.control.lot_schedule(), body['lot_schedule'])
            self.assertEqual(client.post('/api/recovery/lot-schedule', headers=self.headers, json=body).status_code, 200)
            self.assertEqual(len(self.fake.sent), 1)
            self.assertEqual(self.fake.orders[0].volume_current, .01)
            self.fake.fill_order(51,88); self.fake.ask=4000.21
            self.assertEqual(n.tick()['tick']['status'], 'ok')
            self.assertEqual(self.fake.orders[0].volume_current, .03)

    def test_empty_account_allows_save_but_foreign_position_and_missing_snapshot_block(self):
        with self.session(self.fake,self.path) as s:
            n=self.build(s); client=self.client(n)
            self.assertTrue(client.get('/api/recovery/state',headers=self.headers).json()['widget']['capabilities']['set_lot_schedule'])
            self.fake.positions=[N(ticket=1101,identifier=101,magic=0,symbol='EURUSD',type=0,
                 volume=.01,price_open=1.0,sl=0.0,tp=0.0)]
            self.assertFalse(client.get('/api/recovery/state',headers=self.headers).json()['widget']['capabilities']['set_lot_schedule'])
            body=self.settings(n)
            self.assertEqual(client.post('/api/recovery/lot-schedule', headers=self.headers, json=body).status_code,409)
            self.fake.positions=[]
            self.assertEqual(client.post('/api/recovery/lot-schedule', headers=self.headers, json=body).status_code,409)
            with patch.object(self.fake,'positions_get',return_value=None):
                self.assertEqual(client.post('/api/recovery/lot-schedule',headers=self.headers,json=self.settings(n)).status_code,409)
            self.assertEqual(n.control.lot_schedule(),list(DEFAULT_LOT_SCHEDULE))
            self.assertEqual(self.fake.sent,[])

    def test_flat_live_successor_prevents_save_without_touching_orders(self):
        with self.session(self.fake,self.path) as s:
            n=self.build(s); self.initial(n); self.fake.fill_order(51,88); self.fake.ask=4000.21
            self.assertEqual(n.tick()['tick']['status'],'ok')
            self.fake.close(88,reason=0); n.tick()
            self.assertEqual(self.fake.positions,[])
            self.assertEqual(len(self.fake.orders),1)
            before=list(self.fake.sent)
            client=self.client(n)
            self.assertFalse(client.get('/api/recovery/state',headers=self.headers).json()['widget']['capabilities']['set_lot_schedule'])
            self.assertEqual(client.post('/api/recovery/lot-schedule',headers=self.headers,json=self.settings(n)).status_code,409)
            self.assertEqual(self.fake.sent,before)
            self.assertEqual(n.control.lot_schedule(),list(DEFAULT_LOT_SCHEDULE))

    def test_validate_strict_decimal_broker_grid_and_stale_expected(self):
        with self.session(self.fake,self.path) as s:
            n=self.build(s); client=self.client(n)
            for slot,value,status in [(0,'0.02',409),(1,'0.015',409),(1,'1e-2',409),
                                       (1,'0',409),(1,'100000',409),(1,0.02,422)]:
                values=list(DEFAULT_LOT_SCHEDULE); values[slot]=value
                r=client.post('/api/recovery/lot-schedule',headers=self.headers,json=self.settings(n,values))
                self.assertEqual(r.status_code,status,r.text)
            values=list(DEFAULT_LOT_SCHEDULE); values[1]='0.03'
            body=self.settings(n,values)
            self.assertEqual(client.post('/api/recovery/lot-schedule',headers=self.headers,json=body).status_code,200)
            body['request_id']=str(uuid4())
            body['lot_schedule'][1]='0.04'
            r=client.post('/api/recovery/lot-schedule',headers=self.headers,json=body)
            self.assertEqual(r.status_code,409)
            self.assertIn('changed',r.json()['detail'])
            self.assertEqual(n.control.lot_for_slot(2),Decimal('0.03'))
            self.assertEqual(self.fake.sent,[])

    def test_waiting_after_cancel_can_store_without_restarting_executor(self):
        with self.session(self.fake,self.path) as s:
            n=self.build(s); self.initial(n); n.command('cancel',{'kind':'cancel'})
            self.assertFalse(s.ready)
            client=self.client(n); before=list(self.fake.sent)
            self.assertTrue(client.get('/api/recovery/state',headers=self.headers).json()['widget']['capabilities']['set_lot_schedule'])
            r=client.post('/api/recovery/lot-schedule',headers=self.headers,json=self.settings(n))
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['command']['outcome'],'confirmed')
            self.assertFalse(s.ready)
            self.assertEqual(self.fake.sent,before)

    def test_final_position_read_catches_exposure_after_initial_snapshot(self):
        with self.session(self.fake,self.path) as s:
            n=self.build(s)
            body=self.settings(n)
            foreign=N(ticket=1101,identifier=101,magic=0,symbol='EURUSD',type=0,
                      volume=.01,price_open=1.0,sl=0.0,tp=0.0)
            with patch.object(self.fake,'positions_get',side_effect=[[],[foreign]]):
                with self.assertRaisesRegex(ValueError,'open or unknown'):
                    n.command(body['request_id'],{k:v for k,v in dict(body,kind='lot_schedule').items() if k!='request_id'})
            self.assertEqual(n.control.lot_schedule(),list(DEFAULT_LOT_SCHEDULE))
            self.assertEqual(self.fake.sent,[])

    def test_policy_and_receipt_are_one_transaction(self):
        with self.session(self.fake,self.path) as s:
            n=self.build(s)
            with n.control.db:
                n.control.db.execute(
                    '''CREATE TRIGGER fail_lot_receipt BEFORE INSERT ON native_api_commands
                    BEGIN SELECT RAISE(ABORT, 'simulated receipt failure'); END''')
            body=self.settings(n)
            with self.assertRaises(sqlite3.IntegrityError):
                n.command(body['request_id'],{k:v for k,v in dict(body,kind='lot_schedule').items() if k!='request_id'})
            self.assertEqual(n.control.lot_schedule(),list(DEFAULT_LOT_SCHEDULE))
            self.assertEqual(n.control.db.execute('SELECT COUNT(*) FROM native_api_commands').fetchone(),(0,))
            self.assertEqual(self.fake.sent,[])
