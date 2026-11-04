import json
import unittest
import test_tradfi_interactive as fixture
from tradfi.local_panel import LocalPanel


class LocalPanelTest(unittest.TestCase):
    setUp=fixture.InteractiveTest.setUp
    tick=fixture.InteractiveTest.tick
    start=fixture.InteractiveTest.start

    def headers(self,**kwargs):
        return {'Host':'127.0.0.1:8765','X-Local-Mock':'1','Content-Type':'application/json',**kwargs}

    def test_mock_source_and_no_frontend_credentials(self):
        self.start();app=LocalPanel(self.bot,clock=lambda:100)
        app.step();code,view=app.request('GET','/api/state',self.headers())
        self.assertEqual(code,200);self.assertEqual(view['source'],'MOCK')
        self.assertEqual(view['symbol'],'XAUUSD.s');self.assertEqual(view['age'],0)
        self.assertNotIn('token',json.dumps(view).lower())

    def test_cross_origin_dns_rebinding_and_missing_header_denied(self):
        app=LocalPanel(self.bot)
        for headers in (self.headers(Origin='https://evil.invalid'),self.headers(Host='evil.invalid'),
                        {'Host':'127.0.0.1:8765'},self.headers(**{'Sec-Fetch-Site':'cross-site'})):
            self.assertEqual(app.request('GET','/api/state',headers)[0],403)

    def test_shared_command_controller_and_duplicate_action(self):
        app=LocalPanel(self.bot)
        req=dict(id='ui-start-0001',revision=0,action=dict(type='start',mode='semi'))
        response=app.request('POST','/api/action',self.headers(),json.dumps(req).encode())
        self.assertEqual(response[1]['status'],'APPLIED')
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.assertEqual(app.request('POST','/api/action',self.headers(),json.dumps(req).encode()),response)
        req['id']='ui-start-0002'
        self.assertEqual(app.request('POST','/api/action',self.headers(),json.dumps(req).encode())[0],409)

    def test_controller_steps_without_browser_requests(self):
        self.start();app=LocalPanel(self.bot)
        self.broker.fill('BUY')
        for _ in range(4):app.step()
        self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertFalse(self.broker.data['orders'])

    def test_only_bounded_json_is_accepted(self):
        app=LocalPanel(self.bot)
        self.assertEqual(app.request('POST','/api/action',self.headers(),b'x'*8193)[0],400)
        self.assertEqual(app.request('POST','/api/action',self.headers(),b'invalid')[0],400)

    def test_crossed_trigger_is_definite_rejection_not_unknown(self):
        self.start();app=LocalPanel(self.bot)
        req=dict(id='ui-crossed-0001',revision=0,action=dict(type='triggers',representation='prices',levels={'BUY':'4000'}))
        code,result=app.request('POST','/api/action',self.headers(),json.dumps(req).encode())
        self.assertEqual(code,200);self.assertEqual(result['status'],'REJECTED')
        self.assertEqual(len(self.broker.data['orders']),2)

    def test_transport_worker_queues_without_touching_store(self):
        import threading
        from tradfi.control import OwnerMailbox
        from tradfi.local_panel import MockTransport
        app=LocalPanel(self.bot);mailbox=OwnerMailbox();transport=MockTransport(mailbox)
        req=dict(id='worker-start-0001',revision=0,action=dict(type='start',mode='semi'))
        responses=[]
        thread=threading.Thread(target=lambda:responses.append(transport.request('POST','/api/action',self.headers(),json.dumps(req).encode())))
        thread.start();thread.join(1)
        self.assertEqual(responses[0][0],202)
        self.assertEqual(self.bot.state['phase'],'IDLE')
        mailbox.drain(app.gateway,app.view)
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.assertEqual(transport.request('GET','/api/state',self.headers())[1]['action_results'][req['id']]['status'],'APPLIED')

    def test_quote_freshness_does_not_advance_from_owner_heartbeat(self):
        self.start()
        timestamp=self.broker.data['quote']['observed_at_utc']
        app=LocalPanel(self.bot,clock=lambda:timestamp+20)
        for _ in range(3):app.step()
        self.assertEqual(app.view()['timestamp'],timestamp)
        self.assertEqual(app.view()['age'],20)

    def test_receipt_readback_survives_transport_recreation_and_eviction(self):
        from tradfi.control import OwnerMailbox
        app=LocalPanel(self.bot);box=OwnerMailbox()
        req=dict(id='retained-start-0001',revision=0,action=dict(type='start',mode='semi'))
        box.submit(req);box.drain(app.gateway,app.view)
        box=OwnerMailbox()
        for i in range(260):box.outcomes['unrelated-'+str(i)]={'status':'CONFLICT'}
        box.drain(app.gateway,app.view)
        self.assertEqual(box.view()['action_results'][req['id']]['status'],'APPLIED')

    def test_conflicting_payload_cannot_clear_durable_unknown(self):
        from tradfi.control import OwnerMailbox,ActionGateway
        app=LocalPanel(self.bot);box=OwnerMailbox()
        def uncertain(action):raise TimeoutError('unknown execution')
        gateway=ActionGateway(self.bot,execute=uncertain)
        req=dict(id='uncertain-id-0001',revision=0,action=dict(type='start',mode='semi'))
        gateway.apply(req)
        req['action']['mode']='auto';box.submit(req);box.drain(gateway,app.view)
        self.assertEqual(box.view()['action_results'][req['id']]['status'],'UNKNOWN')
