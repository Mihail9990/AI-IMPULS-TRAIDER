"""Real subprocess LIVE ownership/admission checks; no MT5, network or trading."""
import os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import install_tradfi as installer
from tradfi import managed

CHILD="""
import sys
from pathlib import Path
from tradfi import managed
managed.__file__=sys.argv[1]
try:
 with managed.runtime_guard(Path(sys.argv[2]),instance=sys.argv[3]):
  print('READY',flush=True)
  sys.stdin.readline()
except Exception as exc:
 print(type(exc).__name__,flush=True)
"""
class ManagedLiveOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.sha='a'*40;self.code=self.root/'releases'/self.sha/'code'
        self.file=self.code/'tradfi'/'managed.py';self.file.parent.mkdir(parents=True)
        installer.atomic_json(self.root/'managed-install.json',{'format':1})
        installer.atomic_json(self.root/'current.json',{'current':self.sha,'previous':None})
        self.patch=patch.object(managed,'__file__',str(self.file));self.patch.start();self.addCleanup(self.patch.stop)
    def child(self,role='live'):
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        p=subprocess.Popen([sys.executable,'-c',CHILD,str(self.file),str(self.root/'data'/'state.sqlite3'),role],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        def cleanup():
            if p.poll() is None:p.communicate('\n',timeout=10)
            else:p.communicate(timeout=10)
        self.addCleanup(cleanup);return p
    def test_live_owner_blocks_update_and_duplicate(self):
        live=self.child();self.assertEqual(live.stdout.readline().strip(),'READY')
        with installer.lock(self.root/'operation.lock'):
            with self.assertRaises(installer.SetupError):
                with installer.lock(self.root/'instance-live.lock'):pass
        duplicate=self.child();self.assertEqual(duplicate.stdout.readline().strip(),'SetupError')
        live.communicate('\n',timeout=10)
        with installer.lock(self.root/'operation.lock'),installer.lock(self.root/'instance-live.lock'):pass
    def test_update_admission_blocks_new_instance(self):
        with installer.lock(self.root/'operation.lock'):
            p=self.child();self.assertEqual(p.stdout.readline().strip(),'SetupError')
    def test_non_live_role_rejected_before_locks(self):
        p=self.child('paper');self.assertEqual(p.stdout.readline().strip(),'ValueError')
        self.assertFalse((self.root/'instance-paper.lock').exists())
    def test_binding_and_stop_request_preserve_live_data(self):
        state=self.root/'data'/'state.sqlite3';state.parent.mkdir();state.touch()
        managed.bind_state(state)
        self.assertEqual(managed.binding_path(self.root,'live'),self.root/'data'/'state-binding.json')
        with managed.runtime_guard(state):pass
        with patch.dict(os.environ,{'TRADFI_LAUNCH_SESSION':'b'*32,'TRADFI_LAUNCH_INSTANCE':'live'}):event=managed.stop_request()
        self.assertEqual(event.path.name,'stop-live-'+'b'*32+'.request')
        event.path.touch();self.assertTrue(event.is_set())
        state.unlink()
        with self.assertRaises(installer.SetupError):
            with managed.runtime_guard(state):pass
