"""Independent production role, authentication and static HTML contracts."""
import json
import unittest
from unittest.mock import Mock,patch
from tradfi.control import OwnerMailbox
from tradfi.web_control import WebTransport,PaperPanel

PASSWORD='test-only-not-a-real-secret'*2
class PanelIsolationTest(unittest.TestCase):
    def setUp(self):
        self.boxes={role:OwnerMailbox() for role in ('live','paper')}
        self.transports={role:WebTransport(self.boxes[role],role=role,authority='127.0.0.1:'+str(8765+i),password=PASSWORD) for i,role in enumerate(self.boxes)}
        self.headers={role:{'Host':t.authority,'Origin':t.origin,'X-Local-Control':'1','Content-Type':'application/json'} for role,t in self.transports.items()}
        for role,box in self.boxes.items():box._view={'scope':role+'-scope','revision':0}
    def request(self,role,path,payload=None):
        return self.transports[role].request('GET' if payload is None else 'POST',path,self.headers[role],json.dumps(payload).encode())
    def login(self,role):
        code,_,extra=self.request(role,'/api/login',{'password':PASSWORD});self.assertEqual(code,200)
        return extra['Set-Cookie'].split(';')[0]
    def test_same_host_cookies_and_logout_are_independent(self):
        cookies={role:self.login(role) for role in self.boxes}
        self.assertNotEqual(cookies['live'].split('=')[0],cookies['paper'].split('=')[0])
        for role in self.boxes:self.headers[role]['Cookie']='; '.join(cookies.values())
        self.assertEqual(self.request('live','/api/state')[0],200)
        self.assertEqual(self.request('paper','/api/paper/state')[0],200)
        code,_,extra=self.request('live','/api/logout',{})
        self.assertEqual(code,200);self.assertTrue(extra['Set-Cookie'].startswith('tradfi_live_session='))
        self.assertEqual(self.request('live','/api/state')[0],401)
        self.assertEqual(self.request('paper','/api/paper/state')[0],200)
    def test_other_role_cookie_does_not_authenticate(self):
        self.headers['paper']['Cookie']=self.login('live')
        self.assertEqual(self.request('paper','/api/paper/state')[0],401)
    def test_routes_and_scopes_cannot_cross(self):
        req={'id':'request_001','revision':0,'action':{'type':'command','text':'/stop'}}
        for role,other in [('live','paper'),('paper','live')]:
            self.headers[role]['Cookie']=self.login(role)
            own='/api/paper/' if role=='paper' else '/api/'
            foreign='/api/' if role=='paper' else '/api/paper/'
            self.headers[role]['X-Controller-Scope']=other+'-scope'
            self.assertEqual(self.request(role,own+'action',req)[0],409)
            self.headers[role]['X-Controller-Scope']=role+'-scope'
            self.assertEqual(self.request(role,foreign+'action',req)[0],404)
            self.assertEqual(self.request(role,foreign+'state')[0],404)
            self.assertTrue(self.boxes[role].requests.empty())
            self.assertEqual(self.request(role,own+'action',req)[0],202)
    def test_role_immutable_and_log_scopes_filtered(self):
        for role,t in self.transports.items():
            with self.assertRaises(AttributeError):t.role='paper' if role=='live' else 'live'
            t.logs=Mock();t.logs.response.return_value={}
            self.headers[role]['Cookie']=self.login(role)
            other='paper' if role=='live' else 'live'
            self.assertEqual(self.request(role,'/api/logs?scope='+other)[0],400)
            self.assertEqual(self.request(role,'/api/logs/download?scope='+other)[0],400)
            t.logs.response.assert_not_called()
            self.assertEqual(self.request(role,'/api/logs?scope='+role)[0],200)
    def test_paper_panel_does_not_construct_native_chart_feed(self):
        paper=Mock();paper.mailbox=OwnerMailbox();paper.log_file=None
        with patch('tradfi.web_control.ChartFeed',side_effect=AssertionError('native feed forbidden')):
            panel=PaperPanel(paper,password=PASSWORD)
        self.assertEqual(panel.transport.role,'paper');self.assertEqual(panel.authority,'127.0.0.1:8766')
        self.assertIs(panel.view(),paper.view.return_value)
        panel.close()

    def test_html_is_server_role_bound_without_tabs_or_other_log_scope(self):
        from pathlib import Path
        from tradfi.web_control import panel_html
        source=(Path(__file__).resolve().parents[1]/'web/basket-panel/index.html').read_bytes()
        for role in ['live','paper']:
            html=panel_html(source,role).decode()
            self.assertIn('data-role="'+role+'"',html)
            self.assertIn('PAPER · Симулятор' if role=='paper' else 'LIVE · Торговля',html)
            other='paper' if role=='live' else 'live'
            self.assertNotIn('<option value="'+other+'">',html)
            self.assertNotIn('tab-live',html);self.assertNotIn('tab-paper',html)
            self.assertNotIn('__PANEL_ROLE__',html)
