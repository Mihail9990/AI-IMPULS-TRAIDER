import tempfile
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.api import create_app

TOKEN='t'*40

class RecoveryAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=self.tmp.name+'/sim.sqlite'
        self.app=create_app(TOKEN,self.path)
        self.client=TestClient(self.app)
        self.headers={'Authorization':'Bearer '+TOKEN}

    def tearDown(self):
        self.app.state.recovery.db.close(); self.tmp.cleanup()

    def payload(self):
        return {'request_id':str(uuid4()),'ranges':[{'id':'1','price':4000,'side':'BUY','volume':.01}]}

    def test_auth_and_mode(self):
        self.assertEqual(self.client.get('/api/recovery/state').status_code,401)
        s=self.client.get('/api/recovery/state',headers=self.headers).json()
        self.assertEqual(s['mode'],'simulation'); self.assertFalse(s['live_trading'])

    def test_persistence_and_idempotence(self):
        payload=self.payload()
        r=self.client.post('/api/recovery/ranges',headers=self.headers,json=payload)
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.post('/api/recovery/ranges',headers=self.headers,json=payload).json(),r.json())
        app2=create_app(TOKEN,self.path)
        try:
            self.assertEqual(app2.state.recovery.engine.state(),r.json())
        finally: app2.state.recovery.db.close()
        payload['ranges'][0]['price']=4001
        self.assertEqual(self.client.post('/api/recovery/ranges',headers=self.headers,json=payload).status_code,409)

    def test_reject_mutation_with_open_position(self):
        self.client.post('/api/recovery/ranges',headers=self.headers,json=self.payload())
        e=self.app.state.recovery.engine
        e.fill('event',e.data['orders'][0]['id'],'4000')
        before=e.state()
        r=self.client.post('/api/recovery/cancel',headers=self.headers,json={'request_id':str(uuid4())})
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.app.state.recovery.engine.state(),before)

    def test_close_is_not_faked(self):
        r=self.client.post('/api/recovery/ranges/1/close-cancel',headers=self.headers,json={'request_id':str(uuid4())})
        self.assertEqual(r.status_code,409)

    def test_invalid_payload(self):
        p=self.payload(); p['ranges'][0]['side']='INVALID'
        self.assertEqual(self.client.post('/api/recovery/ranges',headers=self.headers,json=p).status_code,422)
        p=self.payload();p['ranges'].append(p['ranges'][0])
        self.assertEqual(self.client.post('/api/recovery/ranges',headers=self.headers,json=p).status_code,409)
