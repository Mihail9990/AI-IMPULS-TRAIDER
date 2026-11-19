"""Panel alongside the unmodified native API, terminal doubles only."""
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
from native_web_panel import PanelAssets,ASSETS
class Integration(unittest.TestCase):
 session=controls.ControlTests.session
 def test_panel_and_native_auth_compatibility_no_write_no_send(self):
  with tempfile.TemporaryDirectory() as d:
   assets=Path(d)/'web';assets.mkdir()
   for name,mime in ASSETS.values():(assets/name).write_text('test asset')
   a=CycleFake(d)
   with self.session(a,d+'/s.db') as s:
    n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
    app=PanelAssets(StateFreshness(create_native_app('t'*32,n),RawAnchor(NS(time=123,time_msc=123456)),'t'*32),assets)
    client=TestClient(app);changes=s.executor.db.total_changes
    for path in ASSETS:
     r=client.get(path);self.assertEqual(r.status_code,200);self.assertEqual(r.headers['cache-control'],'no-store')
    for path in ['/panel/','/panel/model.js']:
     self.assertEqual(client.post(path).status_code,405)
    self.assertEqual(client.get('/api/recovery/state').status_code,401)
    r=client.get('/api/recovery/state',headers={'Authorization':'Bearer '+'t'*32,'X-Recovery-State-Nonce':'b'*32})
    self.assertEqual(r.status_code,200);self.assertEqual(r.json()['state_freshness']['nonce'],'b'*32)
    self.assertEqual(r.json()['widget']['snapshot']['status'],'fresh')
    self.assertEqual(client.post('/api/recovery/ranges',json=dict(request_id='12345678-1234-1234-1234-123456789abc',expected_context=n.context_id(),expected_generation=None,ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance='.70')).status_code,401)
    self.assertEqual(s.executor.db.total_changes,changes);self.assertEqual(a.sent,[])
if __name__=='__main__':unittest.main()
