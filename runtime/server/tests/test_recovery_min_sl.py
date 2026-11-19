import json
import sqlite3
import tempfile
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.api import create_app
from recovery.application import MockApplication
from recovery.mt5_plan import pending,Symbol,Quote
from test_recovery_service_loop import LifecycleFake

class MinimumSLTests(unittest.TestCase):
    def body(self,distance):
        return {'request_id':str(uuid4()),'initial_sl_distance':distance,'reentry_distance':.01,
                'ranges':[{'id':'1','price':4000,'side':'BUY','volume':.01}]}
    def test_both_api_factories_enforce_boundary(self):
        a=MockApplication(LifecycleFake(),token='t'*40,account=1,server='demo',symbol='XAUUSD.s',magic=123)
        self.addCleanup(a.close)
        for app in (create_app('t'*40,':memory:'),a.app):
            with TestClient(app) as c:
                h={'Authorization':'Bearer '+'t'*40}
                for value in (.599999,.59,.25,'0.5999999999999999999999'):
                    self.assertEqual(c.post('/api/recovery/ranges',headers=h,json=self.body(value)).status_code,422)
                self.assertEqual(c.post('/api/recovery/ranges',headers=h,json=self.body(.60)).status_code,200)
    def test_legacy_startup_refuses_without_rewriting_state_or_sends(self):
        with tempfile.TemporaryDirectory() as d:
            path=d+'/state.db';f=LifecycleFake()
            a=MockApplication(f,token='t'*40,account=1,server='demo',symbol='XAUUSD.s',magic=123,database=path)
            with TestClient(a.app) as c:
                c.post('/api/recovery/ranges',headers={'Authorization':'Bearer '+'t'*40},json=self.body(.60))
                f.fill_at(51,100);a.step('4000','4000.2')
            db=sqlite3.connect(path)
            state=json.loads(db.execute('SELECT data FROM recovery_controller WHERE id=1').fetchone()[0])
            state['engine']['settings']['initial_sl_distance']='.25'
            state['engine']['ranges'][0]['initial_sl_distance']='.25'
            raw=json.dumps(state);db.execute('UPDATE recovery_controller SET data=?',(raw,));db.commit()
            before_positions=[vars(p).copy() for p in f.positions];sent=len(f.sent)
            b=MockApplication(f,token='t'*40,account=1,server='demo',symbol='XAUUSD.s',magic=123,database=path)
            self.addCleanup(b.close)
            with self.assertRaisesRegex(ValueError,'at least 0.60'):
                with TestClient(b.app):pass
            self.assertEqual(db.execute('SELECT data FROM recovery_controller WHERE id=1').fetchone()[0],raw)
            self.assertEqual([vars(p).copy() for p in f.positions],before_positions)
            self.assertEqual(len(f.sent),sent);db.close()
    def test_pending_planner_cannot_bypass_minimum(self):
        s=Symbol('XAUUSD.s','.01','.01',0,0,'.01','100','.01')
        q=Quote('3999','3999.2')
        order={'id':'x','price':'4000','sl':'3999.41','volume':'.01','side':'BUY'}
        with self.assertRaisesRegex(ValueError,'at least 0.60'):pending(order,s,q,123)
        order['sl']='3999.40';self.assertEqual(pending(order,s,q,123)['sl'],'3999.40')
