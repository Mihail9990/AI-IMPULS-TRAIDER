"""LIVE-only routes and roles reject requests belonging to another product."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock
from tradfi.control import OwnerMailbox
from tradfi.web_control import WebTransport, panel_html

PASSWORD='test-only-not-a-real-secret'*2
class PanelIsolationTest(unittest.TestCase):
    def setUp(self):
        self.box=OwnerMailbox();self.box._view={'scope':'live-scope','revision':0}
        self.web=WebTransport(self.box,password=PASSWORD)
        self.headers={'Host':self.web.authority,'Origin':self.web.origin,'X-Local-Control':'1','Content-Type':'application/json'}
        code,_,extra=self.request('/api/login',{'password':PASSWORD});self.assertEqual(code,200)
        self.headers['Cookie']=extra['Set-Cookie'].split(';')[0]
    def request(self,path,payload=None):
        return self.web.request('GET' if payload is None else 'POST',path,self.headers,json.dumps(payload).encode())
    def test_only_live_role_and_cookie_are_accepted(self):
        self.assertTrue(self.headers['Cookie'].startswith('tradfi_live_session='))
        with self.assertRaises(ValueError):WebTransport(self.box,role='paper',password=PASSWORD)
        with self.assertRaises(AttributeError):self.web.role='paper'
        self.headers['Cookie']=self.headers['Cookie'].replace('tradfi_live_session','tradfi_paper_session')
        self.assertEqual(self.request('/api/state')[0],401)
    def test_foreign_routes_never_reach_mailbox(self):
        req={'id':'request_001','revision':0,'action':{'type':'command','text':'/stop'}}
        self.headers['X-Controller-Scope']='live-scope'
        self.assertEqual(self.request('/api/paper/state')[0],404)
        self.assertEqual(self.request('/api/paper/action',req)[0],404)
        self.assertTrue(self.box.requests.empty())
        self.headers['X-Controller-Scope']='foreign-scope'
        self.assertEqual(self.request('/api/action',req)[0],409)
        self.headers['X-Controller-Scope']='live-scope'
        self.assertEqual(self.request('/api/action',req)[0],202)
    def test_log_scopes_and_logout(self):
        self.web.logs=Mock();self.web.logs.response.return_value={}
        self.assertEqual(self.request('/api/logs?scope=paper')[0],400)
        self.assertEqual(self.request('/api/logs/download?scope=paper')[0],400)
        self.web.logs.response.assert_not_called()
        self.assertEqual(self.request('/api/logs?scope=live')[0],200)
        self.assertEqual(self.request('/api/logout',{})[0],200)
        self.assertEqual(self.request('/api/state')[0],401)
    def test_html_is_fixed_live_with_no_other_product_controls(self):
        source=(Path(__file__).resolve().parents[1]/'web/basket-panel/index.html').read_bytes()
        html=panel_html(source,'live').decode()
        self.assertIn('data-role="live"',html);self.assertIn('LIVE · Торговля',html)
        self.assertNotIn('__PANEL_ROLE__',html)
        for marker in ('paper-controls','paper-history','value="paper"','tab-paper'):self.assertNotIn(marker,html)
        with self.assertRaises(ValueError):panel_html(source,'paper')
