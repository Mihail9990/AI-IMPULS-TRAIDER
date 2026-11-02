"""All native calls below are mocks. No MetaTrader5 import or network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as N
from tradfi.demo_test import DemoSession,ActionRejected
from tradfi.web_control import WebTransport
from tradfi.control import OwnerMailbox
from tradfi.demo_runtime import DemoPanel,strategy_flat


class Safety:
    def check(self):return None
    def poll_refresh(self):pass
    def request_refresh(self):pass


class API:
    ACCOUNT_TRADE_MODE_DEMO=0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2
    SYMBOL_TRADE_MODE_FULL=4
    ORDER_TYPE_BUY=0;ORDER_TYPE_SELL=1;ORDER_TYPE_BUY_STOP=4;ORDER_TYPE_SELL_STOP=5
    TRADE_ACTION_DEAL=1;TRADE_ACTION_PENDING=5;TRADE_ACTION_SLTP=6;TRADE_ACTION_MODIFY=7;TRADE_ACTION_REMOVE=8
    ORDER_FILLING_FOK=0;ORDER_FILLING_RETURN=2;ORDER_TIME_GTC=0
    TRADE_RETCODE_DONE=10009;TRADE_RETCODE_PLACED=10008;TIMEFRAME_M1=1
    def __init__(self):
        self.a=N(login=123456,server='TEST-DEMO',trade_mode=0,trade_allowed=True,trade_expert=True,margin_mode=2)
        self.t=N(connected=True,trade_allowed=True,tradeapi_disabled=False)
        self.i=N(name='XAUUSDT',visible=True,volume_min=.01,volume_step=.01,volume_max=10,point=.01,trade_tick_size=.01,trade_contract_size=1,trade_stops_level=10,trade_freeze_level=5,filling_mode=1,trade_exemode=2,trade_mode=4,order_mode=1|4|16|32,expiration_mode=1,trade_calc_mode=5,currency_profit='USDT')
        self.q=N(bid=4000.,ask=4000.1,time=1800000000,time_msc=1800000000000)
        self.ps=[];self.os=[];self.sent=[];self.check_hook=lambda:None;self.account_hook=lambda:None;self.send_hook=None;self.counter=0
    def account_info(self):self.account_hook();return self.a
    def terminal_info(self):return self.t
    def symbol_info(self,s):assert s=='XAUUSDT';return self.i
    def symbol_info_tick(self,s):assert s=='XAUUSDT';return self.q
    def positions_get(self,**kw):return self.ps
    def orders_get(self,**kw):return self.os
    def order_calc_margin(self,*args):return 2.
    def order_check(self,r):self.check_hook();return N(retcode=0)
    def order_send(self,r):
        assert self.a.trade_mode==0,'REAL MUTATION ATTEMPT';assert self.a.login==123456
        self.sent.append(copy.deepcopy(r))
        if self.send_hook:return self.send_hook(r)
        self.counter+=1
        if r['action'] in (1,5) and not r.get('position'):
            x=N(symbol=r['symbol'],magic=r['magic'],comment=r['comment'],ticket=self.counter,type=r['type'],volume=r['volume'],volume_current=r['volume'],price_open=r['price'],sl=r['sl'],tp=r['tp'])
            (self.ps if r['action']==1 else self.os).append(x)
        elif r['action']==1:self.ps[:]=[x for x in self.ps if x.ticket!=r['position']]
        elif r['action']==8:self.os[:]=[x for x in self.os if x.ticket!=r['order']]
        else:
            for x in self.ps+self.os:
                if x.ticket==r.get('position',r.get('order')):
                    x.sl=r['sl'];x.tp=r['tp']
                    if 'price' in r:x.price_open=r['price']
        return N(retcode=10009,order=self.counter,deal=self.counter)


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.api=API();self.safe=True;self.s=self.make();self.addCleanup(lambda:self.s.close());self.n=0
    def make(self):return DemoSession(Path(self.tmp.name)/'demo.sqlite3',self.api,expected_login=123456,expected_server='TEST-DEMO',live_safe=lambda:self.safe,clock=lambda:1800000000,monotonic=lambda:1800000000,safety=Safety())
    def apply(self,a):
        self.n+=1
        return self.s.gateway.apply(dict(id='request-'+str(self.n),revision=self.s.state.get('control_revision',0),action=a))
    def enable(self):return self.apply(dict(type='enable',epoch=self.s.epoch))
    def operation(self,op='buy',**kw):
        return dict(inventory_token=self.s.inventory_token(),type='test',operation=op,ticket=0,price='4002' if op.startswith('stop') else '0',sl='3995',tp='4005',epoch=self.s.epoch,**{})|kw
    def test_bounded_lifecycle_and_restart_off(self):
        self.assertFalse(self.s.enabled);self.assertEqual(self.enable()['status'],'APPLIED')
        self.assertEqual(self.apply(self.operation())['status'],'APPLIED');self.assertEqual(self.api.sent[0]['volume'],.01)
        self.assertEqual(self.apply(self.operation('protection',ticket=1,sl='3996',tp='4006'))['status'],'APPLIED')
        self.assertEqual(self.apply(self.operation('close',ticket=1))['status'],'APPLIED');self.assertEqual(self.api.sent[-1]['price'],4000)
        self.assertEqual(self.apply(self.operation('stop_buy'))['status'],'APPLIED')
        ticket=self.api.os[0].ticket
        self.assertEqual(self.apply(self.operation('modify_stop',ticket=ticket,price='4003'))['status'],'APPLIED')
        self.assertEqual(self.apply(self.operation('cancel',ticket=ticket))['status'],'APPLIED')
        scope=self.s.scope;self.s.close();self.s=self.make();self.assertFalse(self.s.enabled);self.assertNotEqual(scope,self.s.scope)
    def test_sell_symmetry(self):
        self.enable();self.assertEqual(self.apply(self.operation('sell',sl='4005',tp='3995'))['status'],'APPLIED')
        self.assertEqual(self.api.sent[-1]['price'],4000)
        self.apply(self.operation('close',ticket=1));self.assertEqual(self.api.sent[-1]['price'],4000.1)
        self.assertEqual(self.apply(self.operation('stop_sell',price='3998',sl='4005',tp='3995'))['status'],'APPLIED')
    def test_real_contest_unknown_missing_permissions(self):
        for mode in (1,2,None,False,'0'):
            with self.subTest(mode=mode):
                self.api.a.trade_mode=mode;self.assertEqual(self.enable()['status'],'REJECTED');self.assertFalse(self.api.sent)
        self.api.a=None;self.assertEqual(self.enable()['status'],'REJECTED')
    def test_switch_after_preflight_before_send(self):
        self.enable();self.api.check_hook=lambda:setattr(self.api.a,'trade_mode',2)
        self.assertEqual(self.apply(self.operation())['status'],'REJECTED');self.assertFalse(self.api.sent);self.assertFalse(self.s.enabled)
    def test_switch_before_cleanup(self):
        self.enable();self.apply(self.operation());self.api.a.login=654321
        self.assertEqual(self.apply(self.operation('close',ticket=1))['status'],'REJECTED');self.assertEqual(len(self.api.sent),1)
    def test_final_account_gate_after_intent(self):
        self.enable();original=self.s.save
        def save(e):
            original(e)
            if e=='demo_intent_before_send':self.api.a.trade_mode=2
        self.s.save=save
        self.assertEqual(self.apply(self.operation())['status'],'REJECTED');self.assertFalse(self.api.sent)
        self.assertEqual(next(iter(self.s.state['commands'].values()))['status'],'NOT_SENT')
    def test_disabled_queued_step_no_send(self):
        self.enable();self.s.mailbox.submit(dict(id='queued-test',revision=self.s.state['control_revision'],action=self.operation()))
        self.s.mailbox.submit(dict(id='disable-now',revision=self.s.state['control_revision'],action={'type':'disable'}))
        self.s.mailbox.drain(self.s.gateway,self.s.view);self.assertFalse(self.api.sent);self.assertTrue(self.s.off.is_set())
    def test_disable_during_order_check(self):
        self.enable();self.api.check_hook=self.s.disarm
        self.assertEqual(self.apply(self.operation())['status'],'REJECTED');self.assertFalse(self.api.sent)
    def test_unknown_is_never_blindly_repeated(self):
        self.enable();self.api.send_hook=lambda r:None
        request=dict(id='unknown-request',revision=self.s.state['control_revision'],action=self.operation())
        self.assertEqual(self.s.gateway.apply(request)['status'],'UNKNOWN')
        self.assertEqual(self.s.gateway.apply(request)['status'],'UNKNOWN');self.assertEqual(len(self.api.sent),1)
        self.assertEqual(self.enable()['status'],'REJECTED');self.apply({'type':'reconcile'});self.assertEqual(self.enable()['status'],'REJECTED')
    def test_positive_reconcile_unknown_entry(self):
        self.enable()
        def uncertain(r):
            self.api.ps.append(N(symbol='XAUUSDT',magic=r['magic'],comment=r['comment'],ticket=42,type=0,volume=.01,price_open=4000.1,sl=3995,tp=4005));return None
        self.api.send_hook=uncertain;self.apply(self.operation());self.apply({'type':'reconcile'})
        self.assertEqual(next(iter(self.s.state['commands'].values()))['status'],'OBSERVED');self.assertEqual(self.enable()['status'],'APPLIED')
    def test_duplicates_stale_and_invalid_values(self):
        self.enable();req=dict(id='same-request',revision=self.s.state['control_revision'],action=self.operation())
        self.s.gateway.apply(req);self.s.gateway.apply(req);self.assertEqual(len(self.api.sent),1)
        from tradfi.control import Conflict
        with self.assertRaises(Conflict):self.s.gateway.apply(dict(id='stale-request',revision=0,action=self.operation()))
        self.assertEqual(self.apply(self.operation('protection',ticket=1,sl='NaN'))['status'],'REJECTED')
        self.assertEqual(len(self.api.sent),1)
    def test_constraints_exact_symbol_and_permissions(self):
        for field,value in [('name','XAUUSDT.s'),('volume_min',.015),('filling_mode',2),('order_mode',1),('expiration_mode',0),('trade_mode',1)]:
            old=getattr(self.api.i,field);setattr(self.api.i,field,value)
            self.enable();self.assertEqual(self.apply(self.operation('stop_buy'))['status'],'REJECTED') if field!='filling_mode' else self.assertEqual(self.apply(self.operation())['status'],'REJECTED')
            setattr(self.api.i,field,old);self.assertFalse(self.api.sent)
        self.enable()
        for patch in ({'sl':'3999.995'},{'sl':'4001.95'},{'tp':'0'},{'price':'4000.1'}):
            self.assertEqual(self.apply(self.operation('stop_buy',**patch))['status'],'REJECTED')
        self.assertFalse(self.api.sent)
    def test_only_owned_and_no_cross_scope_ticket(self):
        self.enable();self.assertEqual(self.apply(self.operation('close',ticket=9876))['status'],'REJECTED')
        self.api.ps=[N(symbol='XAUUSDT',magic=123,comment='manual',ticket=2,type=0,volume=.01,price_open=4000,sl=3995,tp=4005)]
        self.assertEqual(self.apply(self.operation())['status'],'REJECTED');self.assertFalse(self.api.sent)
    def test_active_strategy_or_unknown_blocks_enable(self):
        self.safe=False;self.assertEqual(self.enable()['status'],'REJECTED');self.assertFalse(self.api.sent)
    def test_netting_and_disconnect_fail_closed(self):
        self.api.a.margin_mode=0;self.assertEqual(self.enable()['status'],'REJECTED');self.api.a.margin_mode=2
        self.enable();self.api.t.connected=False;self.s.step();self.assertFalse(self.s.enabled)
        self.api.t.connected=True;self.s.step();self.assertFalse(self.s.enabled)
    def test_routes_auth_scope_and_namespaced_receipts(self):
        self.s.step();live=OwnerMailbox();web=WebTransport(live,password='x'*32,demo_mailbox=self.s.mailbox)
        h={'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        self.assertEqual(web.request('GET','/api/demo/state',h)[0],401)
        _,_,cookie=web.request('POST','/api/login',h,json.dumps({'password':'x'*32}).encode());h['Cookie']=cookie['Set-Cookie'].split(';')[0]
        body=json.dumps(dict(id='route-request',revision=0,action=dict(type='enable',epoch=self.s.epoch))).encode()
        h['X-Controller-Scope']='live';self.assertEqual(web.request('POST','/api/demo/action',h,body)[0],409)
        h['X-Controller-Scope']=self.s.scope;self.assertEqual(web.request('POST','/api/demo/action',h,body)[0],202)
        self.s.step();self.assertTrue(self.s.enabled);self.assertTrue(live.requests.empty())
        h['Origin']='https://evil.invalid';self.assertEqual(web.request('POST','/api/demo/action',h,body)[0],403)
    def test_demo_only_panel_never_loads_strategy(self):
        panel=DemoPanel(self.s,authority='127.0.0.1:8765',password='x'*32);panel.step()
        self.assertEqual(panel.mailbox.view()['phase'],'DEMO ONLY');self.assertIsNone(panel.transport.paper_mailbox)
        self.assertFalse(self.api.sent)
    def test_elapsed_validation_blocks_send(self):
        now=[0.];self.s.monotonic=lambda:now[0];self.enable();self.api.check_hook=lambda:now.__setitem__(0,3.)
        self.assertEqual(self.apply(self.operation())['status'],'REJECTED');self.assertFalse(self.api.sent)
    def test_quote_changed_in_check_no_chase(self):
        self.enable();self.api.check_hook=lambda:setattr(self.api,'q',N(bid=4001.,ask=4001.1,time=1800000000,time_msc=1800000000000))
        self.assertEqual(self.apply(self.operation())['status'],'REJECTED');self.assertFalse(self.api.sent)
    def test_lost_receipt_without_send_intent_can_reconcile(self):
        self.s.state['control_receipts']={'crashed-request':dict(status='UNKNOWN',digest='x',revision=1)};self.s.save('crash_fixture')
        self.assertEqual(self.enable()['status'],'REJECTED');self.apply({'type':'reconcile'});self.assertEqual(self.enable()['status'],'APPLIED');self.assertFalse(self.api.sent)
    def test_guarded_live_panel_rejects_live_commands(self):
        from test_tradfi_basket import BasketTest
        from tradfi.web_control import NativePanel
        live=BasketTest();live.setUp();self.addCleanup(live.doCleanups)
        live.b.account='TEST-LIVE';live.b.symbol='XAUUSD.s'
        panel=NativePanel(live.bot,password='x'*32,demo_session=self.s)
        before=live.store.load();self.enable();self.apply(self.operation())
        self.assertEqual(live.store.load(),before)
        with self.assertRaises(ActionRejected):panel.live_action(dict(type='command',text='/start'))
    def test_dedicated_runtime_no_strategy_or_api_send(self):
        from unittest.mock import patch
        from tradfi.demo_runtime import run_demo_service
        self.api.initialize=lambda path:True;self.api.shutdown=lambda:None
        config=dict(terminal_path='TEST-TERMINAL',expected_login=123456,expected_server='TEST-DEMO',symbol='XAUUSDT',demo_state_file=str(Path(self.tmp.name)/'runtime-demo.db'))
        seen=[]
        class Stop:
            def is_set(self):return bool(seen)
            def wait(self,_):pass
        class Panel(DemoPanel):
            def start(self):pass
            def step(inner):super().step();seen.append(inner)
            def close(self):pass
        with patch.dict('os.environ',{'TRADFI_WEB_PASSWORD':'x'*32}),patch('tradfi.runtime.account_lock_path',return_value=str(Path(self.tmp.name)/'owner.lock')),patch('tradfi.time_safety.TimeSafety',return_value=Safety()),patch('tradfi.basket.BasketController',side_effect=AssertionError('Strategy must not load')):
            run_demo_service(config,api=self.api,stop_event=Stop(),panel_factory=Panel,clock=lambda:1800000000,monotonic=lambda:1800000000)
        self.assertTrue(seen);self.assertFalse(self.api.sent)
    def test_external_edit_and_fill_during_check_reject(self):
        self.enable();self.apply(self.operation());action=self.operation('protection',ticket=1)
        self.api.ps[0].sl=3997
        self.assertEqual(self.apply(action)['status'],'REJECTED');self.assertEqual(len(self.api.sent),1)
        self.s.context();self.api.check_hook=lambda:self.api.ps.clear()
        self.assertEqual(self.apply(self.operation('close',ticket=1))['status'],'REJECTED');self.assertEqual(len(self.api.sent),1)
    def test_logs_authenticated_utf8_events_download_and_paths(self):
        panel=DemoPanel(self.s,authority='127.0.0.1:8765',password='x'*32);self.addCleanup(panel.close)
        panel.step();self.enable();self.apply(self.operation());panel.step()
        h={'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        self.assertEqual(panel.transport.request('GET','/api/logs?scope=demo',h)[0],401)
        _,_,cookie=panel.transport.request('POST','/api/login',h,json.dumps({'password':'x'*32}).encode());h['Cookie']=cookie['Set-Cookie'].split(';')[0]
        code,data,_=panel.transport.request('GET','/api/logs?scope=demo',h);self.assertEqual(code,200);self.assertIn('demo_result',data['text']);self.assertIn('10009',data['text'])
        code,body,headers=panel.transport.request('GET','/api/logs/download?scope=demo',h)
        self.assertEqual(code,200);self.assertIn('Логи',body.decode());self.assertIn('attachment;',headers['Content-Disposition']);self.assertNotIn('123456',body.decode());self.assertLessEqual(len(body),1048576)
        for path in ['/api/logs?scope=../../secret','/api/logs?scope=live&scope=demo','/api/logs?scope=demo&page=-1','/api/logs?scope=demo&path=secret']:
            self.assertEqual(panel.transport.request('GET',path,h)[0],400)
        self.assertEqual(panel.transport.request('GET','/api/logs?scope=paper',h)[0],200)
    def test_log_projection_redacts_existing_sensitive_fields_and_bounds(self):
        from tradfi.web_logs import DiagnosticLogs
        from tradfi.context_diagnostics import project,VERSION
        path=Path(self.tmp.name)/'runtime.log';logs=DiagnosticLogs(path);self.addCleanup(logs.close)
        path.write_text('password=VERY_SECRET\\n'+json.dumps(dict(version=1,utc='2026-11-02T00:00:00Z',level='INFO',event='service_started',password='VERY_SECRET',account=123456))+'\n')
        self.assertNotIn('VERY_SECRET',logs.document('runtime'));self.assertNotIn('123456',logs.document('runtime'))
        self.assertEqual(project(dict(password='VERY_SECRET',account=dict(login=123456,identifier='••56'),request=dict(price=4000,token='VERY_SECRET'))),{'account':{'identifier':'••56'},'request':{'price':'4000'}})
        record=json.dumps(dict(version=VERSION,scope='demo',observed_utc='2026-11-02T00:00:00+00:00',context=dict(password='VERY_SECRET',account=dict(login=123456),quote=dict(bid=4000,ask=4001))))+'\n'
        Path(str(path)+'.demo.diagnostic.jsonl').write_text(record*10000)
        body=logs.response('demo',download=True);self.assertLessEqual(len(body),1048576);self.assertNotIn(b'VERY_SECRET',body);self.assertNotIn(b'123456',body)
        missing=DiagnosticLogs(Path(self.tmp.name)/'absent.log');self.assertIn('files_read=0',missing.document('runtime'))
    def test_no_continuous_tick_logging(self):
        from tradfi.web_logs import DiagnosticLogs
        logs=DiagnosticLogs();self.s.context();view=self.s.view();logs.capture('demo',self.s.store,self.s.state,1800000000,view)
        view['quote']['bid']=4001;logs.capture('demo',self.s.store,self.s.state,1800000001,view)
        self.assertEqual(len(logs.recent['demo']),1)
    def test_account_gate_for_every_operation(self):
        for op in ('buy','sell','stop_buy','stop_sell','modify_stop','cancel','protection','close'):
            self.api.a.trade_mode=0;self.enable();self.api.a.trade_mode=2
            self.assertEqual(self.apply(self.operation(op))['status'],'REJECTED')
        self.assertFalse(self.api.sent)
    def test_ack_without_visible_entry_stays_unknown(self):
        self.enable();self.api.send_hook=lambda r:N(retcode=10009,order=7,deal=8)
        self.assertEqual(self.apply(self.operation())['status'],'UNKNOWN');self.assertEqual(self.enable()['status'],'REJECTED')
    def test_partial_fill_blocks_further_new_entries(self):
        self.enable()
        def partial(r):
            self.api.ps.append(N(symbol='XAUUSDT',magic=r['magic'],comment=r['comment'],ticket=42,type=0,volume=.005,price_open=4000.1,sl=3995,tp=4005));return N(retcode=10010,order=42,deal=43)
        self.api.send_hook=partial;self.assertEqual(self.apply(self.operation())['status'],'UNKNOWN');self.apply({'type':'reconcile'});self.enable()
        self.assertEqual(self.apply(self.operation())['status'],'REJECTED');self.assertEqual(len(self.api.sent),1)
    def test_positive_sltp_and_cancel_history_reconciliation(self):
        self.enable();self.apply(self.operation());self.api.send_hook=lambda r:None
        self.assertEqual(self.apply(self.operation('protection',ticket=1,sl='3996',tp='4006'))['status'],'UNKNOWN')
        self.api.ps[0].sl=3996;self.api.ps[0].tp=4006;self.apply({'type':'reconcile'})
        self.assertTrue(all(c['status']!='UNKNOWN' for c in self.s.state['commands'].values()))
    def test_confirmed_broker_reject_is_not_unknown(self):
        self.enable();self.api.send_hook=lambda r:N(retcode=10018,order=0,deal=0)
        result=self.apply(self.operation());self.assertEqual(result['status'],'REJECTED');self.assertIn('10018',result['reason'])
        self.assertEqual(next(iter(self.s.state['commands'].values()))['status'],'REJECTED')
    def test_close_unknown_requires_positive_deal_evidence(self):
        self.enable();self.apply(self.operation());self.api.DEAL_ENTRY_OUT=1;sent=[]
        def lost(r):sent.append(copy.deepcopy(r));self.api.ps.clear();return None
        self.api.send_hook=lost;self.assertEqual(self.apply(self.operation('close',ticket=1))['status'],'UNKNOWN')
        self.api.history_deals_get=lambda **kw:[];self.apply({'type':'reconcile'});self.assertEqual(self.enable()['status'],'REJECTED')
        r=sent[0];self.api.history_deals_get=lambda **kw:[N(symbol='XAUUSDT',magic=r['magic'],comment=r['comment'],ticket=91,position_id=1,entry=1,volume=.01)]
        self.apply({'type':'reconcile'});self.assertEqual(self.enable()['status'],'APPLIED');self.assertEqual(len(self.api.sent),2)
    def test_cancel_unknown_requires_canceled_history(self):
        self.enable();self.apply(self.operation('stop_buy'));order=copy.deepcopy(self.api.os[0]);self.api.ORDER_STATE_CANCELED=2
        def lost(r):self.api.os.clear();return None
        self.api.send_hook=lost;self.assertEqual(self.apply(self.operation('cancel',ticket=1))['status'],'UNKNOWN')
        order.state=2;self.api.history_orders_get=lambda **kw:[order];self.apply({'type':'reconcile'})
        self.assertEqual(self.enable()['status'],'APPLIED');self.assertEqual(len(self.api.sent),2)
