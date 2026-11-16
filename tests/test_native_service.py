import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from tradfi.native_service import NativeMailbox
from tradfi.web_control import WebTransport

class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.identity=dict(installation=str(uuid.uuid4()),login='123',server='Mock',symbol='XAUUSD.s')
        self.box=NativeMailbox(self.temp.name,self.identity,clock=lambda:100)
        self.addCleanup(self.box.close)
    def snapshot(self,**changes):
        value=dict(protocol='native_state_v1',**self.identity,revision=5,phase='PAUSED',read_only=False,positions=[],orders=[],settings={})
        value.update(changes);p=Path(self.temp.name)/'native-view.json';p.write_text(json.dumps(value));os.utime(p,(100,100))
    def test_state_requires_fresh_exact_identity(self):
        self.assertTrue(self.box.view()['read_only'])
        self.snapshot();self.assertEqual(self.box.view()['phase'],'PAUSED')
        self.snapshot(login='wrong');self.assertEqual(self.box.view()['phase'],'DISCONNECTED')
        self.snapshot();self.box.clock=lambda:106;self.assertEqual(self.box.view()['phase'],'DISCONNECTED')
    def test_authenticated_transport_queues_without_terminal_calls(self):
        self.snapshot();web=WebTransport(self.box,password='a'*32)
        headers={'host':'127.0.0.1:8765','x-local-control':'1','content-type':'application/json'}
        code,_,_=web.request('POST','/api/action',headers,b'{}');self.assertEqual(code,401)
        code,_,extra=web.request('POST','/api/login',headers,json.dumps({'password':'a'*32}).encode());self.assertEqual(code,200)
        headers['cookie']=extra['Set-Cookie'].split(';')[0];headers['x-controller-scope']=self.identity['installation']
        rid=str(uuid.uuid4());body=dict(id=rid,revision=5,action=dict(type='close_all'))
        code,result,_=web.request('POST','/api/action',headers,json.dumps(body).encode());self.assertEqual(code,202);self.assertEqual(result['status'],'AWAITING_EA')
        self.assertTrue((Path(self.temp.name)/'native_requests'/f'{rid}.cmd').exists())
        # Independent stop remains available while close outcome is unresolved.
        body['id']=str(uuid.uuid4());body['action']={'type':'stop_new_entries'}
        self.assertEqual(web.request('POST','/api/action',headers,json.dumps(body).encode())[0],202)
    def test_receipt_readback_updates_pending_without_republication(self):
        self.snapshot();rid=str(uuid.uuid4());self.box.submit(dict(id=rid,revision=5,action={'type':'sl_at_entry'}))
        envelope=json.loads((Path(self.temp.name)/'native_requests'/f'{rid}.json').read_text())
        folder=Path(self.temp.name)/'native_receipts';folder.mkdir()
        receipt=dict(protocol='native_panel_receipt_v1',id=rid,identity=self.identity,digest=envelope['digest'],sequence=1,status='AWAITING_BROKER',reason='mock')
        (folder/f'{rid}.json').write_text(json.dumps(receipt))
        self.assertEqual(self.box.view()['action_results'][rid]['status'],'AWAITING_BROKER')
    def test_runtime_routes_native_before_mt5_broker_creation(self):
        from tradfi.runtime import main
        p=Path(self.temp.name)/'config.json';p.write_text(json.dumps({'backend':'ea_native_v1'}))
        with patch('tradfi.native_service.run_native') as run:
            main(['--config',str(p)])
            run.assert_called_once_with({'backend':'ea_native_v1'})
    def test_native_panel_has_no_manual_order_entry(self):
        root=Path(__file__).resolve().parents[1]
        html=(root/'web/native-panel/index.html').read_text()
        self.assertIn('close_all',html);self.assertIn('cancel_triggers',html)
        self.assertNotIn('v3-entry-panel',html);self.assertNotIn('MARKET',html)
