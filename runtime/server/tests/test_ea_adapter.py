import tempfile, unittest, sys, os, time, threading
from pathlib import Path
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ea_adapter import EAAdapter

class Tests(unittest.TestCase):
 def setUp(self):
  self.d=tempfile.TemporaryDirectory(); self.root=Path(self.d.name)
  self.a=EAAdapter(self.root,123,'Demo',timeout=.15)
  self.snap=self.root/'snapshot.tsv'
  self.snap.write_text('H\t123\tDemo\t1\nP\t111\tXAUUSD.s\t0\t0.01\t3000\t2990\t3010\t2\t999\nS\tXAUUSD.s\t0.01\t0.01\t10\t0\t3000.1\t3000.3\n')
 def tearDown(self): self.d.cleanup()
 def request(self): return dict(action=6,request_id=str(uuid4()),position=111,expected_symbol='XAUUSD.s',expected_entry=3000,expected_type=0,expected_identifier=999,expected_volume=.01,change_sl=False,change_tp=True,sl=2990,tp=3010)
 def test_snapshot(self):
  self.assertTrue(self.a.terminal_info().connected)
  self.assertEqual(self.a.positions_get(ticket=111)[0].identifier,999)
  self.assertEqual(self.a.positions_get(ticket=112),())
  self.assertEqual(self.a.symbol_info('XAUUSD.s').bid,3000.1)
 def test_stale_and_wrong_account(self):
  os.utime(self.snap,(time.time()-10,time.time()-10)); self.assertIsNone(self.a.account_info())
  self.snap.write_text('H\t124\tDemo\t1\n'); self.assertIsNone(self.a.account_info())
 def test_no_replay_after_timeout(self):
  r=self.request(); self.assertIsNone(self.a.order_send(r))
  target=self.root/(r['request_id']+'.request'); before=target.read_bytes()
  self.assertIsNone(self.a.order_send(r)); self.assertEqual(target.read_bytes(),before)
  row=target.read_text().strip().split('\t'); self.assertEqual(row[10:12],['0','1'])
 def test_response(self):
  r=self.request()
  def worker():
   for _ in range(100):
    if (self.root/(r['request_id']+'.request')).exists():
     (self.root/(r['request_id']+'.result')).write_text(r['request_id']+'\t10009\n'); return
    time.sleep(.001)
  t=threading.Thread(target=worker); t.start()
  self.assertEqual(self.a.order_send(r).retcode,10009); t.join()
 def test_reject_bad_action(self):
  r=self.request(); r['action']=1
  with self.assertRaises(ValueError): self.a.order_send(r)
if __name__=='__main__': unittest.main()
