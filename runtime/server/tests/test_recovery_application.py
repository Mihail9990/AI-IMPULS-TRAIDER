import tempfile
import unittest
from itertools import repeat
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.application import MockApplication
from test_recovery_service_loop import LifecycleFake


class ApplicationTests(unittest.TestCase):
    def make(self, fake=None, database=':memory:'):
        fake = fake or LifecycleFake()
        assembly = MockApplication(fake, token='t'*40, account=1, server='demo',
                                   symbol='XAUUSD.s', magic=123, database=database)
        self.addCleanup(assembly.close)
        return fake, assembly

    def configure(self, client, key=None):
        return client.post('/api/recovery/ranges', headers={'Authorization':'Bearer '+'t'*40},
            json={'request_id':key or str(uuid4()),'ranges':[{'id':'1','price':4000,'side':'BUY','volume':.01}]})

    def test_http_fill_support_reentry_through_owned_lifecycle(self):
        fake, a = self.make()
        with self.assertRaises(RuntimeError): a.step('3999','3999.2')
        with TestClient(a.app) as client:
            self.assertEqual(self.configure(client).status_code,200)
            fake.fill_at(51,100)
            a.step('4000','4000.2')
            fake.ask=4000.7
            a.step('4000.5','4000.7')
            fake.support_close(101); fake.ask=4000.2
            a.step('4000','4000.2')
            self.assertTrue(any(o.price_open==4000.7 for o in fake.orders))
            self.assertEqual(a.controller.engine()._range('1')['full_sl_count'],0)
        self.assertTrue(a.closed)
        with self.assertRaises(RuntimeError): a.step('4000','4000.2')

    def test_restart_persists_controller_and_outbox_no_duplicate_send(self):
        with tempfile.TemporaryDirectory() as d:
            fake,a=self.make(database=d+'/state.db');key=str(uuid4())
            with TestClient(a.app) as client:
                result=self.configure(client,key)
                self.assertEqual(result.status_code,200)
                count=len(fake.sent)
                self.assertTrue(a.pipeline.executor.db.execute('SELECT id FROM recovery_outbox').fetchall())
            _,b=self.make(fake,d+'/state.db')
            with TestClient(b.app) as client:
                self.assertEqual(self.configure(client,key).json(),result.json())
                b.step('3999','3999.2')
                self.assertEqual(len(fake.sent),count)

    def test_duplicate_owner_startup_failure_does_not_close_first(self):
        with tempfile.TemporaryDirectory() as d:
            fake,a=self.make(database=d+'/state.db')
            _,b=self.make(fake,d+'/state.db')
            with TestClient(a.app) as client:
                with self.assertRaises(RuntimeError):
                    with TestClient(b.app): pass
                self.assertTrue(b.closed)
                self.assertEqual(self.configure(client).status_code,200)

    def test_bounded_quotes_and_authenticated_commands(self):
        _,a=self.make()
        with TestClient(a.app) as client:
            self.assertEqual(client.get('/api/recovery/state').status_code,401)
            self.assertEqual(len(a.run_steps(repeat(('3999','3999.2')),max_steps=3)),3)
            for bad in [True,0,-1,10001,1.5]:
                with self.assertRaises(ValueError):a.run_steps([],max_steps=bad)
        with self.assertRaises(RuntimeError):
            with TestClient(a.app): pass

    def test_no_lifespan_no_commands(self):
        fake,a=self.make()
        client=TestClient(a.app)
        self.assertEqual(self.configure(client).status_code,409)
        self.assertEqual(fake.sent,[])

    def test_scope_failure_allocates_no_executor(self):
        from unittest.mock import patch
        from recovery.controller import Controller
        from recovery.engine import Engine
        from recovery.pipeline import MockPipeline
        controller=Controller(Engine(),test_mode=True)
        self.addCleanup(controller.db.close)
        controller.data['bindings']['wrong']={'account':2,'server':'demo',
            'symbol':'XAUUSD.s','magic':123}
        with patch('recovery.pipeline.Executor') as executor:
            with self.assertRaisesRegex(ValueError,'another account/symbol scope'):
                MockPipeline(controller,LifecycleFake(),1,'demo','XAUUSD.s',123)
            executor.assert_not_called()
