import asyncio
from pathlib import Path
import tempfile
import unittest
from native_web_panel import PanelAssets,ASSETS,CSP
from native_launch import native_caddy_config
class PanelTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name);self.calls=[]
  for name,mime in ASSETS.values():(self.root/name).write_text('sample '+name)
 async def underlying(self,scope,receive,send):
  self.calls.append(scope)
  await send({'type':'http.response.start','status':418,'headers':[]})
  await send({'type':'http.response.body','body':b'UNCHANGED'})
 def request(self,path='/panel/',method='GET',kind='http'):
  out=[]
  async def send(m):out.append(m)
  asyncio.run(PanelAssets(self.underlying,self.root)({'type':kind,'path':path,'method':method},None,send))
  return out[0],out[1]['body']
 def test_whitelist_and_security_headers(self):
  for path in ASSETS:
   h,b=self.request(path);self.assertEqual(h['status'],200);headers=dict(h['headers'])
   self.assertEqual(headers[b'cache-control'],b'no-store');self.assertEqual(headers[b'referrer-policy'],b'no-referrer')
   self.assertEqual(headers[b'content-security-policy'],CSP.encode());self.assertNotIn('unsafe-inline',CSP)
  self.assertEqual(self.calls,[])
 def test_404_no_traversal_listing_or_alias(self):
  for path in ['/panel','/panel/../native_launch.py','/panel/secret','/panel/index.html','/panel/server/']:
   self.assertEqual(self.request(path)[0]['status'],404)
  self.assertEqual(self.calls,[])
 def test_nonget_assets_never_reaches_runtime(self):
  for verb in ('POST','PUT','HEAD','DELETE','OPTIONS'):
   h,b=self.request(method=verb);self.assertEqual(h['status'],405);self.assertEqual(dict(h['headers'])[b'allow'],b'GET')
  self.assertEqual(self.calls,[])
 def test_missing_oversized_redirect_fail_safe(self):
  p=self.root/'index.html';p.unlink();self.assertEqual(self.request()[0]['status'],503)
  p.write_bytes(b'x'*(2*1024*1024+1));self.assertEqual(self.request()[0]['status'],503)
  p.unlink();p.symlink_to(self.root/'model.js');self.assertEqual(self.request()[0]['status'],503)
 def test_api_local_routes_and_lifespan_pass_through(self):
  for path in ('/api/recovery/state','/api/recovery/ranges','/_local_ready','/_local_arm'):
   h,b=self.request(path,method='POST');self.assertEqual(h['status'],418);self.assertEqual(b,b'UNCHANGED')
  self.request(kind='lifespan');self.assertEqual(len(self.calls),5)
 def test_caddy_only_explicit_get_assets_and_original_routes(self):
  config=native_caddy_config('8.8.8.8',self.root)
  panel=config.split(' @panel {')[1].split(' }')[0]
  self.assertIn('method GET',panel)
  self.assertIn('path /panel/ /panel/panel.css /panel/model.js /panel/panel.js',panel)
  self.assertNotIn('*',panel);self.assertIn('path /api/recovery/state',config)
  self.assertIn('method POST',config);self.assertIn('/api/recovery/reconcile',config)
  self.assertIn('reverse_proxy 127.0.0.1:8767',config)
if __name__=='__main__':unittest.main()
