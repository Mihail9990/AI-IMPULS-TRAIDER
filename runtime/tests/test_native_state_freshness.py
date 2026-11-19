import unittest,asyncio,json
from types import SimpleNamespace as NS
import native_state_freshness as f
class Tests(unittest.TestCase):
 def run_response(self,nonce=b'a'*32,auth=b'Bearer '+b't'*32,status=200,fresh=True,delay=.0011,tick=True,parts=1,path='/api/recovery/state',method='GET',extra=(),limit=99999):
  clock=[100.0];anchor=f.RawAnchor(NS(time=1000,time_msc=1000123) if tick else None,lambda:clock[0]);original={'widget':{'schema_version':1,'mode':'native','snapshot':{'status':'fresh' if fresh else 'unavailable','observed_at':'UNCHANGED_UTC'}}};calls=[]
  async def app(scope,recv,send):
   calls.append(scope['method']);clock[0]+=delay
   body=json.dumps(original).encode()
   await send({'type':'http.response.start','status':status,'headers':[(b'content-length',str(len(body)).encode()),(b'cache-control',b'public')]})
   if parts==2:await send({'type':'http.response.body','body':body[:10],'more_body':True});body=body[10:]
   await send({'type':'http.response.body','body':body,'more_body':False})
  output=[]
  async def send(m):output.append(m)
  headers=list(extra)
  if nonce is not None:headers.append((b'x-recovery-state-nonce',nonce))
  if auth is not None:headers.append((b'authorization',auth))
  asyncio.run(f.StateFreshness(app,anchor,'t'*32,max_body=limit)({'type':'http','method':method,'path':path,'headers':headers},None,send))
  body=b''.join(x.get('body',b'') for x in output)
  return json.loads(body),output,calls
 def test_valid_ceil_raw_coordinate_and_unchanged_snapshot(self):
  d,out,calls=self.run_response(parts=2);p=d['state_freshness']
  self.assertEqual(p,dict(version=1,nonce='a'*32,age_ms=2,mt5_now_ms=1000124))
  self.assertEqual(d['widget']['snapshot']['observed_at'],'UNCHANGED_UTC');self.assertEqual(calls,['GET'])
  self.assertIn((b'cache-control',b'no-store'),out[0]['headers'])
 def test_missing_invalid_duplicate_nonce(self):
  for nonce,extra in [(None,()),(b'A'*32,()),(b'a'*31,()),(b'a'*32,((b'x-recovery-state-nonce',b'b'*32),))]:
   self.assertNotIn('state_freshness',self.run_response(nonce=nonce,extra=extra)[0])
 def test_auth_status_and_stale(self):
  for kwargs in [dict(auth=b'Bearer wrong'),dict(auth=None),dict(status=401),dict(status=503),dict(fresh=False),dict(extra=((b'authorization',b'Bearer '+b't'*32),))]:
   self.assertNotIn('state_freshness',self.run_response(**kwargs)[0])
 def test_commands_and_readiness_not_changed(self):
  for kwargs in [dict(method='POST'),dict(path='/_local_ready')]:
   d,o,c=self.run_response(**kwargs);self.assertNotIn('state_freshness',d);self.assertIn((b'cache-control',b'public'),o[0]['headers'])
 def test_bounds_and_chunk_limit(self):
  self.assertNotIn('state_freshness',self.run_response(limit=2,parts=2)[0])
  self.assertEqual(self.run_response(delay=15)[0]['state_freshness']['age_ms'],15000)
  self.assertIsNone(self.run_response(delay=-1)[0]['state_freshness']['age_ms'])
  self.assertIsNone(self.run_response(delay=-1)[0]['state_freshness']['mt5_now_ms'])
 def test_long_elapsed_and_no_anchor_are_diagnostics_not_gates(self):
  self.assertGreater(self.run_response(delay=30)[0]['state_freshness']['age_ms'],15000)
  p=self.run_response(tick=False)[0]['state_freshness']
  self.assertEqual(p['nonce'],'a'*32);self.assertIsNone(p['mt5_now_ms']);self.assertEqual(p['age_ms'],2)
 def test_anchor_never_recalibrates_and_invalid_stays_unavailable(self):
  clock=[10];a=f.RawAnchor(NS(time=12,time_msc=12345),lambda:clock[0]);clock[0]=11
  self.assertEqual(a.raw_now_ms(a.monotonic()),13345);clock[0]=9;self.assertIsNone(a.monotonic());clock[0]=20;self.assertIsNone(a.raw_now_ms(a.monotonic()))
  for tick in (None,NS(time=12,time_msc=0),NS(time=12,time_msc=13999),NS(time=12,time_msc=True)):
   a=f.RawAnchor(tick,lambda:10);self.assertIsNone(a.raw_now_ms(a.monotonic()))
 def test_capture_only_one_read_and_two_identity_checks(self):
  calls=[];api=NS(symbol_info_tick=lambda s:calls.append(s) or NS(time=12,time_msc=12345))
  a=f.capture_anchor(api,{'symbol':'X'},lambda a,s:calls.append('verify'),lambda:10)
  self.assertEqual(calls,['verify','X','verify']);self.assertEqual(a.raw_now_ms(a.monotonic()),12345)
 def test_capture_failure_no_proof_no_retry(self):
  def bad(s):raise OSError()
  a=f.capture_anchor(NS(symbol_info_tick=bad),{'symbol':'X'},lambda a,s:None,lambda:10)
  self.assertIsNone(a.raw_now_ms(a.monotonic()))
if __name__=='__main__':unittest.main()
