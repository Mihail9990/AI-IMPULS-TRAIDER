import tempfile
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from recovery.native_api import create_native_app
from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
import test_native_recovery_control as controls
from test_native_support_cycle import CycleFake


class NativeApiTests(unittest.TestCase):
    session = controls.ControlTests.session

    def test_auth_validation_get_readonly_and_retry(self):
        with tempfile.TemporaryDirectory() as d:
            a = CycleFake(d)
            with self.session(a, d+'/s.db') as s:
                n = NativeCoordinator(NativeRecoveryControl(s, magic=123))
                with self.assertRaises(ValueError): create_native_app('я'*32,n)
                app = create_native_app('x'*32, n)
                client = TestClient(app)
                self.assertEqual(client.get('/api/recovery/state').status_code, 401)
                headers = {'Authorization':'Bearer '+'x'*32}
                r = client.get('/api/recovery/state', headers=headers)
                self.assertEqual(r.json()['mode'], 'native'); self.assertEqual(a.sent, [])
                payload = dict(request_id=str(uuid4()), ranges=controls.RANGES,
                               expected_generation=None, expected_context=n.context_id(),
                               initial_sl_distance='.59', reentry_distance=.7)
                self.assertEqual(client.post('/api/recovery/ranges',json=payload,headers=headers).status_code, 422)
                self.assertEqual(a.sent, [])
                payload['initial_sl_distance']='.70'
                first = client.post('/api/recovery/ranges',json=payload,headers=headers)
                self.assertEqual(first.status_code,200,first.text)
                count = len(a.sent)
                second = client.post('/api/recovery/ranges',json=payload,headers=headers)
                self.assertEqual(first.json()['command'],second.json()['command'])
                self.assertEqual(first.json()['data'],second.json()['data']); self.assertEqual(len(a.sent),count)
                client.get('/api/recovery/state',headers=headers)
                self.assertEqual(len(a.sent),count)

    def test_projection_fresh_actual_inventory_unknown_exposure_and_unavailable(self):
        from copy import deepcopy
        from recovery.native_api import native_widget_projection
        with tempfile.TemporaryDirectory() as d:
            a = CycleFake(d)
            a.ACCOUNT_TRADE_MODE_REAL=0; a.ACCOUNT_TRADE_MODE_DEMO=1
            original_info=a.account_info
            def info():
                value=original_info(); value.trade_mode=1; return value
            a.account_info=info
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                w=native_widget_projection(n)
                self.assertEqual(w['snapshot']['status'],'fresh')
                self.assertEqual(w['account_trade_mode'],'demo')
                self.assertEqual(w['positions'],[]); self.assertTrue(w['capabilities']['set_ranges'])
                n.command('start',dict(kind='ranges', ranges=controls.RANGES,
                                      initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88); a.ask=4000.21
                # Projection must see broker fill even before a coordinator tick.
                count=len(a.sent); w=native_widget_projection(n)
                self.assertEqual(len(w['positions']),1)
                self.assertEqual(w['positions'][0]['range_id'],'1')
                self.assertFalse(w['capabilities']['cancel_ranges'])
                self.assertEqual(w['capabilities']['close_cancel_range_ids'],['1'])
                self.assertEqual(len(a.sent),count)
                unknown=deepcopy(a.positions[0]); unknown.ticket=999; unknown.identifier=999
                a.positions.append(unknown)
                w=native_widget_projection(n)
                self.assertEqual(len(w['positions']),2)
                self.assertEqual(w['positions'][1]['mapping_status'],'unmapped')
                self.assertEqual(w['capabilities']['close_cancel_range_ids'],[])
                a.positions_get=lambda:None
                w=native_widget_projection(n)
                self.assertEqual(w['snapshot']['status'],'unavailable')
                self.assertIsNone(w['positions']); self.assertIsNone(w['orders'])
                self.assertFalse(w['capabilities']['set_ranges'])
                self.assertEqual(len(a.sent),count)

    def test_waiting_projection_and_manual_close_never_resurrect_inventory(self):
        from recovery.native_api import native_widget_projection
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                n.command('start',dict(kind='ranges',ranges=controls.RANGES,
                    initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88); a.ask=4000.21; n.tick()
                a.close(88,reason=0)
                before=len(a.sent); w=native_widget_projection(n)
                self.assertEqual(w['positions'],[])
                self.assertTrue(w['orders'])
                self.assertEqual(len(a.sent),before)
                n.tick()
                n.command('cancel',{'kind':'cancel'})
                w=native_widget_projection(n)
                self.assertFalse(w['executor_ready'])
                self.assertFalse(w['reconciliation_required'])
                self.assertTrue(w['capabilities']['set_ranges'])
                self.assertFalse(w['capabilities']['cancel_ranges'])
                self.assertEqual(w['totals']['1']['gross_negative_closed_net'],'0.26')
                self.assertEqual(len(w['loss_history']),1)
                a.ask=3999
                n.command('new',dict(kind='ranges',ranges=controls.RANGES,
                    initial_sl_distance='.70',reentry_distance='.70'))
                w=native_widget_projection(n)
                self.assertEqual(w['totals']['1']['gross_negative_closed_net'],'0')
                self.assertEqual(len(w['loss_history']),1)

    def test_projection_detects_fill_and_close_during_read(self):
        from recovery.native_api import native_widget_projection
        for transition in ('fill','close'):
            with self.subTest(transition=transition), tempfile.TemporaryDirectory() as d:
                a=CycleFake(d)
                with self.session(a,d+'/s.db') as s:
                    n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                    n.command('start',dict(kind='ranges',ranges=controls.RANGES,
                        initial_sl_distance='.70',reentry_distance='.70'))
                    if transition=='close': a.fill_order(51,88)
                    original=s.transport.snapshot
                    def racing_snapshot(scope):
                        snapshot=original(scope)
                        if transition=='fill': a.fill_order(51,88)
                        else: a.close(88,reason=0)
                        return snapshot
                    s.transport.snapshot=racing_snapshot
                    count=len(a.sent)
                    w=native_widget_projection(n)
                    self.assertEqual(w['snapshot']['status'],'unavailable')
                    self.assertIsNone(w['positions']); self.assertIsNone(w['orders'])
                    self.assertFalse(w['capabilities']['set_ranges'])
                    self.assertFalse(w['capabilities']['cancel_ranges'])
                    self.assertEqual(w['capabilities']['close_cancel_range_ids'],[])
                    self.assertEqual(len(a.sent),count)

    def test_command_status_rejection_context_generation_and_readonly(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                client=TestClient(create_native_app('x'*32,n)); headers={'Authorization':'Bearer '+'x'*32}
                key=str(uuid4()); url='/api/recovery/commands/'+key
                self.assertEqual(client.get(url).status_code,401)
                self.assertEqual(client.get(url,headers=headers).json()['command']['phase'],'not_recorded')
                payload=dict(request_id=key,expected_context=n.context_id(),expected_generation=None,
                             ranges=controls.RANGES,initial_sl_distance='.70',reentry_distance=.70)
                self.assertEqual(client.post('/api/recovery/ranges',json=payload,headers=headers).status_code,200)
                receipt=client.get(url,headers=headers).json()
                self.assertEqual(receipt['context_id'],n.context_id())
                self.assertEqual(receipt['command']['phase'],'completed')
                self.assertEqual(receipt['command']['outcome'],'confirmed')
                count=len(a.sent)
                # Stale displayed generation cannot cancel the replacement.
                rejected=str(uuid4())
                bad=dict(request_id=rejected,expected_context=n.context_id(),expected_generation=None)
                self.assertEqual(client.post('/api/recovery/cancel',json=bad,headers=headers).status_code,409)
                status=client.get('/api/recovery/commands/'+rejected,headers=headers).json()['command']
                self.assertEqual(status['phase'],'rejected'); self.assertEqual(status['outcome'],'rejected')
                self.assertTrue(s.ready); self.assertFalse(n._unfinished_api_command())
                # Same rejected UUID never becomes executable after inputs change.
                bad['expected_generation']=n.control._read()['generation']
                self.assertEqual(client.post('/api/recovery/cancel',json=bad,headers=headers).status_code,409)
                bad['request_id']=str(uuid4()); bad['expected_context']='another-account'
                self.assertEqual(client.post('/api/recovery/cancel',json=bad,headers=headers).status_code,409)
                unknown=str(uuid4())
                with n.control.db:
                    n.control.db.execute('INSERT INTO native_api_commands VALUES(?,?,?,NULL)',
                        (unknown,json.dumps({'kind':'cancel'}),'dispatching'))
                self.assertEqual(client.get('/api/recovery/commands/'+unknown,headers=headers).json()['command']['phase'],'dispatching')
                self.assertEqual(len(a.sent),count)

    def test_predispatch_auth_validation_markers_exact_and_absent_for_known_uuid(self):
        import json, hashlib
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                client=TestClient(create_native_app('x'*32,n))
                headers={'Authorization':'Bearer '+'x'*32,'Content-Type':'application/json'}
                key=str(uuid4())
                payload=dict(request_id=key,expected_generation=None,expected_context=n.context_id())
                raw=json.dumps(payload, separators=(',',':'))
                path='/api/recovery/cancel'
                before=n.control.db.total_changes
                response=client.post(path,content=raw,headers={'Content-Type':'application/json'})
                self.assertEqual(response.status_code,401)
                marker=response.json()['rejection']
                self.assertEqual(marker['request_sha256'],hashlib.sha256(raw.encode()).hexdigest())
                self.assertEqual(marker['scope'],'attempt_only'); self.assertEqual(marker['request_id'],key)
                self.assertEqual(marker['path'],path); self.assertEqual(marker['method'],'POST')
                self.assertEqual(n.control.db.total_changes,before); self.assertFalse(a.sent)
                invalid=json.dumps(dict(payload,extra='invalid'))
                response=client.post(path,content=invalid,headers=headers)
                self.assertEqual(response.status_code,422)
                self.assertEqual(response.json()['rejection']['request_sha256'],hashlib.sha256(invalid.encode()).hexdigest())
                self.assertEqual(response.json()['rejection']['reason'],'validation_failed')
                # A previous uncertain attempt with the UUID forbids a marker,
                # even if the current attempt fails auth/body validation.
                with n.control.db:
                    n.control.db.execute('INSERT INTO native_api_commands VALUES(?,?,?,NULL)',(key,'{}','dispatching'))
                self.assertNotIn('rejection',client.post(path,content=raw,headers={'Content-Type':'application/json'}).json())
                self.assertNotIn('rejection',client.post(path,content=invalid,headers=headers).json())
                self.assertNotIn('rejection',client.post(path,content='{"request_id":false}',headers=headers).json())
                self.assertFalse(a.sent)

    def test_ingress_rejects_streamed_oversize_before_json_parse(self):
        import asyncio
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                app=create_native_app('x'*32,n)
                async def call(headers,chunks):
                    seen=[]; reads=0
                    scope=dict(type='http',asgi={'version':'3.0'},http_version='1.1',method='POST',
                        scheme='https',path='/api/recovery/cancel',raw_path=b'/api/recovery/cancel',
                        query_string=b'',root_path='',headers=headers,server=('test',443),client=('test',1))
                    async def receive():
                        nonlocal reads
                        value=chunks[reads];reads+=1
                        return dict(type='http.request',body=value,more_body=True)
                    async def send(message): seen.append(message)
                    await app(scope,receive,send)
                    return seen,reads
                baseline=n.control.db.total_changes
                # Unknown/chunked length stops at the first overflowing chunk;
                # no third chunk, concatenation of oversized body or JSON parse.
                messages,reads=asyncio.run(call([], [b'a'*32768,b'b'*32769,b'never-read']))
                self.assertEqual(reads,2)
                self.assertEqual(messages[0]['status'],413)
                self.assertNotIn(b'rejection',messages[1]['body'])
                messages,reads=asyncio.run(call([(b'content-length',b'1000000000')], []))
                self.assertEqual(reads,0); self.assertEqual(messages[0]['status'],413)
                # A dishonest small Content-Length cannot bypass streaming count.
                messages,reads=asyncio.run(call([(b'content-length',b'1')], [b'a'*65537]))
                self.assertEqual(reads,1);self.assertEqual(messages[0]['status'],413)
                self.assertEqual(n.control.db.total_changes,baseline);self.assertFalse(a.sent)
