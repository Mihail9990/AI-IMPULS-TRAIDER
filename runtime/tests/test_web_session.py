import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from web_session import BrowserSessions, COOKIE

class Sessions(unittest.TestCase):
 def setUp(self):
  self.calls=[];self.now=0;self.context='account-A'
  app=FastAPI()
  @app.api_route('/{rest:path}',methods=['GET','POST'])
  async def endpoint(request:Request,rest:str):
   self.calls.append(dict(request.headers));return {'ok':True,'authorization':request.headers.get('authorization')}
  self.app=app
  self.session=BrowserSessions(app,'a'*32,'https://vps.example',mode='native',context=lambda:self.context,clock=lambda:self.now,ttl=10)
  self.client=TestClient(self.session,base_url='https://vps.example')
  self.headers={'X-Recovery-Session':'1','Origin':'https://vps.example','Content-Type':'application/json','Sec-Fetch-Site':'same-origin'}
 def login(self,client=None):
  r=(client or self.client).post('/api/recovery/session/login',headers={**self.headers,'Authorization':'Bearer '+'a'*32},json={})
  self.assertEqual(r.status_code,200);return r
 def test_reload_cookie_and_no_key(self):
  r=self.login();cookie=r.headers['set-cookie']
  for x in ['Secure','HttpOnly','SameSite=Strict','Path=/','Max-Age=10']:self.assertIn(x,cookie)
  self.assertNotIn('a'*32,cookie);self.assertNotIn('Domain=',cookie)
  status=self.client.get('/api/recovery/session/status',headers={'X-Recovery-Session':'1'})
  self.assertTrue(status.json()['authenticated']);self.assertEqual(status.json()['csrf'],r.json()['csrf'])
  out=self.client.get('/api/recovery/state',headers={'X-Recovery-Session':'1'})
  self.assertEqual(out.status_code,200);self.assertEqual(self.calls[-1]['authorization'],'Bearer '+'a'*32)
  self.assertNotIn('cookie',self.calls[-1])
 def test_csrf_origin_and_methods(self):
  token=self.login().json()['csrf'];url='/api/recovery/cancel'
  for patch in ({},{'X-Recovery-CSRF':'wrong'},{'X-Recovery-CSRF':token,'Origin':'null'},{'X-Recovery-CSRF':token,'Origin':'https://evil.example'},{'X-Recovery-CSRF':token,'Host':'evil.example'},{'X-Recovery-CSRF':token,'Sec-Fetch-Site':'same-site'}):
   self.assertEqual(self.client.post(url,headers={**self.headers,**patch},json={}).status_code,403)
  h={**self.headers,'X-Recovery-CSRF':token};h.pop('Origin')
  self.assertEqual(self.client.post(url,headers=h,json={}).status_code,403)
  self.assertFalse(self.calls)
  self.assertEqual(self.client.post(url,headers={**self.headers,'X-Recovery-CSRF':token},json={}).status_code,200)
 def test_connectivity_does_not_grant_writes_or_receipts(self):
  self.session.mode='connectivity';csrf=self.login().json()['csrf']
  for method,path in [('POST','/api/recovery/ranges'),('GET','/api/recovery/commands/'+'a'*36),('POST','/_local_arm'),('GET','/_local_ready')]:
   r=self.client.request(method,path,headers={**self.headers,'X-Recovery-CSRF':csrf})
   self.assertEqual(r.status_code,403)
  self.assertFalse(self.calls)
 def test_context_token_expiry_restart_revoke(self):
  self.login();self.context='account-B'
  self.assertEqual(self.client.get('/api/recovery/session/status',headers=self.headers).status_code,401)
  self.login();self.session.token='b'*32
  self.assertEqual(self.client.get('/api/recovery/session/status',headers=self.headers).status_code,401)
  self.session.token='a'*32;self.login();self.now=11
  self.assertEqual(self.client.get('/api/recovery/session/status',headers=self.headers).status_code,401)
  csrf=self.login().json()['csrf']
  out=self.client.post('/api/recovery/session/logout',headers={**self.headers,'X-Recovery-CSRF':csrf},json={})
  self.assertEqual(out.status_code,200);self.assertIn('Max-Age=0',out.headers['set-cookie'])
  self.assertFalse(self.session.sessions)
  self.login();new=TestClient(BrowserSessions(self.app,'a'*32,'https://vps.example',mode='native',context=lambda:self.context),base_url='https://vps.example');new.cookies.update(self.client.cookies)
  self.assertEqual(new.get('/api/recovery/session/status',headers=self.headers).status_code,401)
 def test_explicit_bearer_never_cookie_fallback(self):
  self.login()
  self.client.get('/api/recovery/state',headers={'Authorization':'Bearer wrong'})
  self.assertEqual(self.calls[-1]['authorization'],'Bearer wrong')
  self.client.get('/_local_ready',headers={'Authorization':'Bearer '+'a'*32})
  self.assertEqual(self.calls[-1]['authorization'],'Bearer '+'a'*32)
 def test_login_cross_site_no_auth_change(self):
  for origin in ['null','https://vps.example.evil','http://vps.example','https://vps.example:8443']:
   r=self.client.post('/api/recovery/session/login',headers={**self.headers,'Origin':origin,'Authorization':'Bearer '+'a'*32},json={})
   self.assertEqual(r.status_code,403)
  self.assertFalse(self.session.sessions)
 def test_unknown_routes_and_duplicate_headers(self):
  csrf=self.login().json()['csrf']
  for path in ['/api/recovery/reconcile','/api/recovery/new','/_local_arm']:
   self.assertEqual(self.client.post(path,headers={**self.headers,'X-Recovery-CSRF':csrf},json={}).status_code,403)
  self.assertEqual(self.client.get('/api/recovery/state',headers=[('X-Recovery-Session','1'),('X-Recovery-Session','1')]).status_code,403)
 def test_plain_http_cookie_login_denied_bearer_unchanged(self):
  client=TestClient(self.session,base_url='http://vps.example')
  self.assertEqual(client.post('/api/recovery/session/login',headers={**self.headers,'Authorization':'Bearer '+'a'*32},json={}).status_code,403)
  self.assertEqual(client.get('/_local_ready',headers={'Authorization':'Bearer '+'a'*32}).status_code,200)
 def test_cookie_malformed_and_scope_switch(self):
  self.login()
  self.assertEqual(self.client.get('/api/recovery/state',headers={**self.headers,'Cookie':COOKIE+'=bad; '+COOKIE+'=other'}).status_code,401)
  self.session.mode='connectivity'
  self.assertEqual(self.client.get('/api/recovery/session/status',headers=self.headers).status_code,401)
 def test_caddy_session_routes_are_explicit_and_proxied(self):
  from native_web_panel import connectivity_panel_config
  from connectivity_core import caddy_config
  from native_launch import native_caddy_config
  import re
  for config in [connectivity_panel_config(caddy_config('8.8.8.8',Path('/tmp/caddy-test'))),native_caddy_config('8.8.8.8',Path('/tmp/caddy-test'))]:
   self.assertEqual(config.count('@sessionRead {'),2)
   self.assertEqual(config.count('@sessionWrite {'),2)
   for name in ['sessionRead','sessionWrite']:
    block=re.search(r'handle @'+name+r' \{([^}]+)\}',config).group(1)
    self.assertIn('reverse_proxy 127.0.0.1:8767',block)
    self.assertNotIn('method',block);self.assertNotIn('path',block)
   self.assertIn('/panel/session.js',config)
  connectivity=connectivity_panel_config(caddy_config('8.8.8.8',Path('/tmp/caddy-test')))
  for forbidden in ['/api/recovery/cancel','/api/recovery/ranges','/api/recovery/reconcile','/_local_arm']:
   self.assertNotIn(forbidden,connectivity)
  self.assertIn('handle @state {\n  reverse_proxy',connectivity)
 def test_unadmitted_requests_never_read_broker_context(self):
  requests=[]
  self.session.context=lambda:requests.append(True) or self.context
  bogus={**self.headers,'Cookie':COOKIE+'='+'z'*43}
  for path in ('/api/recovery/state','/api/recovery/session/status'):
   self.assertEqual(self.client.get(path,headers=bogus).status_code,401)
  self.assertEqual(self.client.post('/api/recovery/session/login',headers=self.headers,json={}).status_code,401)
  self.assertEqual(requests,[])
  self.login()
  self.assertEqual(requests,[True])
  self.assertEqual(self.client.post('/api/recovery/ranges',headers=self.headers,json={}).status_code,403)
  self.assertEqual(requests,[True])
  self.assertEqual(self.client.get('/api/recovery/state',headers=self.headers).status_code,200)
  self.assertEqual(requests,[True,True])
 def test_rate_bound_and_config(self):
  for _ in range(10):self.assertEqual(self.client.post('/api/recovery/session/login',headers=self.headers,json={}).status_code,401)
  self.assertEqual(self.client.post('/api/recovery/session/login',headers=self.headers,json={}).status_code,429)
  self.login() # correct high-entropy key is not blocked by invalid-login quota
  for origin in ['http://vps.example','https://vps.example/path','https://user@vps.example']:
   with self.assertRaises(ValueError):BrowserSessions(self.app,'a'*32,origin,mode='native',context=lambda:'x')

if __name__=='__main__':unittest.main()
