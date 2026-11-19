import io,json,hashlib,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch
from fastapi.testclient import TestClient
import connectivity_core as c
from connectivity_api import create_app
class Stream(io.BytesIO):
    def close(self):pass
class Child:
    def __init__(self):self.stdin=Stream();self.dead=False
    def poll(self):return 0 if self.dead else None
    def terminate(self):self.dead=True
    def kill(self):self.dead=True
    def wait(self,timeout):return 0
class Tests(unittest.TestCase):
    def test_api_auth_readonly(self):
        client=TestClient(create_app('x'*32,'nonce'))
        self.assertEqual(client.get('/api/recovery/state').status_code,401)
        h={'Authorization':'Bearer '+'x'*32};r=client.get('/api/recovery/state',headers=h)
        self.assertEqual(r.json()['mode'],'read_only');self.assertTrue(r.json()['connection_only']);self.assertFalse(r.json()['live_trading'])
        for route in ['ranges','cancel','reconcile','ranges/1/close-cancel']:
            self.assertIn(client.post('/api/recovery/'+route,headers=h,json={}).status_code,(404,405))
        self.assertEqual(client.post('/api/recovery/state',headers=h).status_code,405)
        self.assertEqual(client.get('/_local_ready',headers=h).json(),{'instance':'nonce'})
    def test_profile_persists_protected_only(self):
        with tempfile.TemporaryDirectory() as d:
            def protect(data,decrypt=False):return data[::-1]
            folder,key=c.profile(d,'8.8.8.8',protect=protect)
            self.assertNotIn(key,(folder/'profile.json').read_text())
            self.assertEqual(c.profile(d,'8.8.8.8',protect=protect)[1],key)
            with self.assertRaises(c.ConnectionError):c.profile(d,'1.1.1.1',protect=protect)
    def test_config_least_route(self):
        txt=c.caddy_config('8.8.8.8','/tmp/certs')
        for word in ['path /api/recovery/state','method GET','admin off','disable_http_challenge','profile shortlived']:self.assertIn(word,txt)
        self.assertNotIn('2019',txt);self.assertNotIn('file_server',txt)
    def fixture(self,d):
        root=Path(d);(root/'tools/caddy').mkdir(parents=True);(root/'.venv-native/Scripts').mkdir(parents=True)
        (root/'tools/caddy/caddy.exe').write_bytes(b'fixture');(root/'.venv-native/Scripts/python.exe').write_bytes(b'fixture');return root
    def test_start_readiness_before_caddy_and_no_token_argv(self):
        with tempfile.TemporaryDirectory() as d:
            root=self.fixture(d);calls=[];kids=[]
            def run(args,**kw):return N(returncode=0,stdout=json.dumps({'admin':{'disabled':True}}))
            def popen(args,**kw):calls.append(args);child=Child();kids.append(child);return child
            def ready(child,token,nonce):self.assertEqual(len(kids),1);self.assertIn(token.encode(),child.stdin.getvalue())
            with patch.object(c,'EXE_SHA256',hashlib.sha256(b'fixture').hexdigest()):
                s=c.ConnectionSession(root,root,run=run,popen=popen,readiness=ready);s.start('8.8.8.8','secret-token'*4,root/'certs');s.stop()
            self.assertEqual(len(kids),2);self.assertTrue(all(k.dead for k in kids));self.assertNotIn('secret-token',' '.join(sum(calls,[])))
    def test_readiness_failure_never_caddy(self):
        with tempfile.TemporaryDirectory() as d:
            root=self.fixture(d);calls=[]
            def popen(args,**kw):calls.append(args);return Child()
            def fail(*args):raise c.ConnectionError('not ready')
            with patch.object(c,'EXE_SHA256',hashlib.sha256(b'fixture').hexdigest()):
                s=c.ConnectionSession(root,root,run=lambda *a,**k:N(returncode=0,stdout=json.dumps({'admin':{'disabled':True}})),popen=popen,readiness=fail)
                with self.assertRaises(c.ConnectionError):s.start('8.8.8.8','x'*32,root/'certs')
            self.assertEqual(len(calls),1);self.assertIsNone(s.temp)
if __name__=='__main__':unittest.main()
