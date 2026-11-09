"""Real subprocess ownership/admission checks; no MT5, network or trading."""
from contextlib import ExitStack
import json,os,subprocess,sys,tempfile,unittest
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

class SeparateManagedInstances(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.sha='a'*40;self.code=self.root/'releases'/self.sha/'code'
        self.file=self.code/'tradfi'/'managed.py';self.file.parent.mkdir(parents=True)
        installer.atomic_json(self.root/'managed-install.json',{'format':1})
        installer.atomic_json(self.root/'current.json',{'current':self.sha,'previous':None})
        self.patch=patch.object(managed,'__file__',str(self.file));self.patch.start();self.addCleanup(self.patch.stop)
    def child(self,role):
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        p=subprocess.Popen([sys.executable,'-c',CHILD,str(self.file),str(self.root/'data'/(role+'.sqlite3')),role],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        def cleanup():
            if p.poll() is None:p.communicate('\n',timeout=10)
            else:p.communicate(timeout=10)
        self.addCleanup(cleanup)
        return p
    def test_two_processes_coexist_but_update_and_duplicate_are_blocked(self):
        live=self.child('live');self.assertEqual(live.stdout.readline().strip(),'READY')
        paper=self.child('paper');self.assertEqual(paper.stdout.readline().strip(),'READY')
        # Admission is short, so another role can start; update cannot own both.
        with installer.lock(self.root/'operation.lock'):
            for role in ('live','paper'):
                with self.assertRaises(installer.SetupError):
                    with installer.lock(self.root/('instance-'+role+'.lock')):pass
        duplicate=self.child('live');self.assertEqual(duplicate.stdout.readline().strip(),'SetupError')
        live.communicate('\n',timeout=10)
        with installer.lock(self.root/'instance-live.lock'):pass
        with self.assertRaises(installer.SetupError):
            with installer.lock(self.root/'instance-paper.lock'):pass
        paper.communicate('\n',timeout=10)
        with installer.lock(self.root/'operation.lock'),installer.lock(self.root/'instance-live.lock'),installer.lock(self.root/'instance-paper.lock'):pass
    def test_update_admission_blocks_new_instance(self):
        with installer.lock(self.root/'operation.lock'):
            p=self.child('paper');self.assertEqual(p.stdout.readline().strip(),'SetupError')
    def test_separate_bindings_and_role_stop_requests(self):
        live=self.root/'data'/'live.sqlite3';paper=self.root/'data'/'paper.sqlite3';live.parent.mkdir();live.touch();paper.touch()
        managed.bind_state(live,instance='live');managed.bind_state(paper,instance='paper')
        self.assertNotEqual(managed.binding_path(self.root,'live'),managed.binding_path(self.root,'paper'))
        with managed.runtime_guard(live,instance='live'),managed.runtime_guard(paper,instance='paper'):pass
        with self.assertRaises(installer.SetupError):
            with managed.runtime_guard(live,instance='paper'):pass
        events={}
        for role in ('live','paper'):
            with patch.dict(os.environ,{'TRADFI_LAUNCH_SESSION':'b'*32,'TRADFI_LAUNCH_INSTANCE':role}):events[role]=managed.stop_request(instance=role)
        self.assertNotEqual(events['live'].path,events['paper'].path)
        events['live'].path.touch();self.assertTrue(events['live'].is_set());self.assertFalse(events['paper'].is_set())
        paper.unlink()
        with self.assertRaises(installer.SetupError):
            with managed.runtime_guard(paper,instance='paper'):pass
