"""Production native GET + doubles; no broker initialization or scheduler."""
import sys
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'server'));sys.path.insert(0,str(ROOT/'server'/'tests'))
from fastapi.testclient import TestClient
from recovery.native_api import create_native_app
from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
import test_native_recovery_control as controls
from test_native_support_cycle import CycleFake
from native_state_freshness import StateFreshness,RawAnchor
class Integration(unittest.TestCase):
 session=controls.ControlTests.session
 def test_real_native_get_auth_no_db_write_no_trade_and_legacy(self):
  with tempfile.TemporaryDirectory() as d:
   a=CycleFake(d)
   with self.session(a,d+'/s.db') as s:
    n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
    app=StateFreshness(create_native_app('t'*32,n),RawAnchor(NS(time=123,time_msc=123456)), 't'*32)
    client=TestClient(app);headers={'Authorization':'Bearer '+'t'*32,'X-Recovery-State-Nonce':'b'*32}
    changes=s.executor.db.total_changes
    r=client.get('/api/recovery/state',headers=headers)
    self.assertEqual(r.status_code,200);self.assertEqual(r.json()['state_freshness']['nonce'],'b'*32)
    self.assertEqual(r.json()['widget']['snapshot']['status'],'fresh')
    self.assertIs(r.json()['web_trading'],False)
    legacy=client.get('/api/recovery/state',headers={'Authorization':headers['Authorization']})
    self.assertEqual(legacy.status_code,200);self.assertNotIn('state_freshness',legacy.json())
    bad=client.get('/api/recovery/state',headers={'X-Recovery-State-Nonce':'b'*32})
    self.assertEqual(bad.status_code,401);self.assertNotIn('state_freshness',bad.json())
    self.assertEqual(s.executor.db.total_changes,changes);self.assertEqual(a.sent,[])
    self.assertEqual(r.headers['cache-control'],'no-store')
 def test_web_trading_only_authenticated_live_native_arm_on_fresh_nonce_reply(self):
  with tempfile.TemporaryDirectory() as d:
   a=CycleFake(d)
   with self.session(a,d+'/s.db') as s:
    n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
    arm={'value':False}
    def armed():
     value=arm['value']
     if isinstance(value,Exception):raise value
     return value
    app=StateFreshness(create_native_app('t'*32,n),RawAnchor(NS(time=123,time_msc=123456)), 't'*32,web_armed=armed)
    client=TestClient(app)
    auth='Bearer '+'t'*32; headers={'Authorization':auth,'X-Recovery-State-Nonce':'b'*32}
    for value,expected in ((False,False),(1,False),('true',False),(RuntimeError(),False),(True,True),(False,False)):
     arm['value']=value
     r=client.get('/api/recovery/state',headers=headers);self.assertEqual(r.status_code,200)
     self.assertIs(r.json()['web_trading'],expected)
     self.assertEqual(r.json()['state_freshness']['nonce'],'b'*32)
    arm['value']=True
    for headers_ in ({'Authorization':auth},{'Authorization':auth,'X-Recovery-State-Nonce':'BAD'},
                     {'Authorization':'wrong','X-Recovery-State-Nonce':'b'*32}):
     self.assertNotIn('web_trading',client.get('/api/recovery/state',headers=headers_).json())
    self.assertEqual(a.sent,[])
if __name__=='__main__':unittest.main()
