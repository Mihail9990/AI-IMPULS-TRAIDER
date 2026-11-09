"""Offline runtime/MT5 and authenticated transport contracts; no socket binding."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_tradfi_native_interactive import NativeAPI
import test_tradfi_native_interactive as fixtures
from test_tradfi_mt5 import record
from time_fixtures import verified_time
from tradfi.web_control import NativePanel,WebTransport
from tradfi.control import OwnerMailbox
from tradfi.runtime import _run_mt5
from tradfi.time_profile import BYBIT_PROFILE,offset_at

class TransportTest(unittest.TestCase):
    def setUp(self):
        self.box=OwnerMailbox();self.now=1
        self.transport=WebTransport(self.box,password='test-only-not-a-real-secret'*2,clock=lambda:self.now)
        self.headers={'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
    def request(self,path,payload=None):return self.transport.request('GET' if payload is None else 'POST',path,self.headers,json.dumps(payload).encode())
    def login(self):
        code,_,extra=self.request('/api/login',{'password':'test-only-not-a-real-secret'*2});self.assertEqual(code,200)
        self.headers['Cookie']=extra['Set-Cookie'].split(';')[0]
    def test_auth_missing_and_bad_origin_cannot_queue(self):
        self.assertEqual(WebTransport(self.box).request('GET','/api/state',self.headers)[1]['error'],'AUTH_NOT_CONFIGURED')
        self.assertEqual(self.request('/api/state')[0],401)
        self.login();self.headers['Origin']='http://attacker.invalid'
        self.assertEqual(self.request('/api/action',{})[0],403);self.assertTrue(self.box.requests.empty())
    def test_session_expiry_scope_and_queue_only(self):
        self.login();self.box._view={'scope':'account-a'}
        request={'id':'request_01','revision':0,'action':{'type':'start','mode':'semi'}}
        self.assertEqual(self.request('/api/action',request)[0],409)
        self.headers['X-Controller-Scope']='account-a'
        self.assertEqual(self.request('/api/action',request)[0],202);self.assertEqual(self.box.requests.qsize(),1)
        self.now=3602;self.assertEqual(self.request('/api/state')[0],401)
    def test_unicode_password_is_auth_failure_not_transport_exception(self):
        self.assertEqual(self.request('/api/login',{'password':'неправильный пароль'})[0],403)
        self.assertEqual(self.request('/api/login',{'password':'\ud800'})[0],400)

    def test_login_rate_bound_and_invalid_json(self):
        for _ in range(10):self.assertEqual(self.request('/api/login',{'password':'wrong'})[0],403)
        self.assertEqual(self.request('/api/login',{'password':'wrong'})[0],429)

class FeedTest(unittest.TestCase):
    setUp=fixtures.NativeInteractiveTest.setUp
    tearDown=fixtures.NativeInteractiveTest.tearDown
    restart=fixtures.NativeInteractiveTest.restart
    def test_feed_exact_symbol_bid_ask_raw_time_and_age(self):
        mono=[self.api.now]
        panel=NativePanel(self.bot,clock=lambda:self.api.now,monotonic=lambda:mono[0]);panel.step()
        view=panel.mailbox.view();feed=view['feed']
        self.assertEqual(feed['source'],'INJECTED_MT5');self.assertEqual(feed['status'],'FRESH')
        self.assertEqual(feed['symbol'],'XAUUSD.s');self.assertEqual(len(feed['candles']),120)
        self.assertNotEqual(feed['quote']['bid'],feed['quote']['ask'])
        seq=feed['sequence'];panel.step();self.assertEqual(panel.feed.view()['sequence'],seq)
        mono[0]+=10;self.assertEqual(panel.feed.view()['status'],'STALE')
        with patch.object(self.api,'copy_rates_from_pos',return_value=None):panel.step()
        self.assertEqual(panel.feed.view()['status'],'DISCONNECTED')
        self.assertEqual(panel.feed.view()['quote'],feed['quote'])
    def test_unverified_feed_never_labels_server_time_utc(self):
        panel=NativePanel(self.bot,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        snap=self.bot.snapshot();snap['quote']['utc_time']=None
        with patch.object(self.broker,'_timestamp_semantics_verified',return_value=False):feed=panel.feed.capture(snap)
        self.assertEqual(feed['status'],'UNVERIFIED');self.assertIsNone(feed['timestamp'])
        self.assertTrue(all(x['utc_time'] is None for x in feed['candles']))

    def test_future_candles_rejected_and_old_candles_explicit_stale(self):
        panel=NativePanel(self.bot,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        snap=self.bot.snapshot();bars=self.api.copy_rates_from_pos('XAUUSD.s',1,0,120)
        with patch.object(self.api,'copy_rates_from_pos',return_value=[dict(b,time=b['time']+86400) for b in bars]):
            with self.assertRaisesRegex(ValueError,'Future'):panel.feed.capture(snap)
        with patch.object(self.api,'copy_rates_from_pos',return_value=[dict(b,time=b['time']-86400) for b in bars]):
            feed=panel.feed.capture(snap)
        self.assertEqual(feed['status'],'STALE');self.assertEqual(feed['candle_status'],'STALE')

class RuntimeAPI(NativeAPI):
    def __init__(self):
        super().__init__();self.now=int(self.now//60)*60+10
        self.rates=[dict(time=int(self.now//60)*60-60,open=3999.,close=4000.)]
    def symbol_info_tick(self,symbol):
        stamp=self.now
        return record(bid=self.tick.bid,ask=self.tick.ask,time=int(stamp),time_msc=int(stamp*1000))
    def copy_rates_from_pos(self,*args):
        return super().copy_rates_from_pos(*args)

class RuntimePipelineTest(unittest.TestCase):
    def test_runtime_web_controller_adapter_fill_modify_close_and_feed(self):
        api=RuntimeAPI();panels=[];bots=[];stages=[]
        class Stop:
            done=False
            def is_set(self):return self.done
            def wait(self,*args):pass
        stop=Stop()
        class Panel(NativePanel):
            n=0
            def start(self):panels.append(self) # no real sockets
            def step(self):
                super().step()
                bot=self.controller
                bots.append(bot);self.n+=1
                if self.n>20:raise AssertionError('runtime failed to settle')
                if self.n==1:
                    bot.action(dict(type='tp_distances',cycle_id=None,distances=['.20']+[None]*19));bot.command('/start');api.now+=60;api.rates[0]['time']+=60;stages.append('candle')
                elif self.n==3:api.fill();stages.append('fill')
                elif self.n==6:
                    p=bot.snapshot()['positions'][0]
                    bot.action(dict(type='protection',cycle_id=bot.state['cycle']['id'],position_id=p['id'],old={'sl':p['sl'],'tp':p['tp']},new={'sl':p['sl'],'tp':str(float(p['tp'])+.25)}))
                    stages.append('modify')
                elif self.n==9:
                    api.close_position(reason=api.DEAL_REASON_TP);stages.append('close')
                elif self.n==12:
                    assert bot.state['phase'] in {'COOLDOWN','WAIT_CANDLE'};stop.done=True
        with tempfile.TemporaryDirectory() as tmp:
            config=dict(backend='mt5_direct_v1',time_profile=BYBIT_PROFILE,expected_server='Bybit-Live-6',expected_login=111111111,
                symbol='XAUUSD.s',magic=25101102,terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',enable_trading=True,
                execution_contract='cycle_scoped_identity_v1',legacy_ea_removed=True,legacy_state_reconciled=True,
                state_file=str(Path(tmp)/'state.db'),log_file=str(Path(tmp)/'log'),web={'enabled':True})
            with patch.dict('os.environ',{'LOCALAPPDATA':tmp}),patch('tradfi.time_safety.TimeSafety',side_effect=verified_time):
                _run_mt5(config,False,service=True,execute=True,api=api,panel_factory=Panel,
                    stop_event=stop,clock=lambda:api.now,monotonic=lambda:api.now)
            self.assertEqual(stages,['candle','fill','modify','close']);self.assertFalse(api.positions)
            self.assertEqual(panels[0].mailbox.view()['source'],'INJECTED_MT5')
            self.assertEqual(panels[0].mailbox.view()['feed']['status'],'FRESH')
            self.assertEqual(len([s for s in api.sends if s['action']==api.TRADE_ACTION_DEAL]),0)
            self.assertTrue(any(s['action']==api.TRADE_ACTION_SLTP for s in api.sends))

class WebServiceSecurityTest(unittest.TestCase):
    def test_public_https_origin_secure_cookie_and_reject_cross_origin(self):
        box=OwnerMailbox();t=WebTransport(box,password='offline-test-credential-not-real-123',public_origin='https://bot.example.org')
        headers={'Host':'127.0.0.1:8765','Origin':'https://bot.example.org','X-Local-Control':'1','Content-Type':'application/json'}
        code,_,extra=t.request('POST','/api/login',headers,json.dumps({'password':'offline-test-credential-not-real-123'}).encode())
        self.assertEqual(code,200);self.assertIn('; Secure',extra['Set-Cookie']);self.assertIn('HttpOnly',extra['Set-Cookie'])
        headers['Cookie']=extra['Set-Cookie'].split(';')[0];headers['Origin']='https://attacker.example.org'
        self.assertEqual(t.request('GET','/api/state',headers)[0],403)
    def test_public_origin_rejects_paths_credentials_http_and_remote_bind(self):
        for origin in ['http://example.org','https://example.org/','https://user:pass@example.org','https://example.org?x=1']:
            with self.subTest(origin=origin),self.assertRaises(ValueError):WebTransport(OwnerMailbox(),public_origin=origin)
        with self.assertRaises(ValueError):WebTransport(OwnerMailbox(),authority='0.0.0.0:8765')
    def test_missing_password_stops_service_before_mt5_or_store(self):
        config=dict(expected_login=111111111,expected_server='Bybit-Live-6',symbol='XAUUSD.s')
        with patch.dict('os.environ',{},clear=True),patch('tradfi.runtime.Store') as store,patch('tradfi.mt5_adapter.MT5Broker.connect') as connect:
            with self.assertRaisesRegex(ValueError,'TRADFI_WEB_PASSWORD'):_run_mt5(config,False,service=True)
            store.assert_not_called();connect.assert_not_called()
    def test_no_cookie_or_csrf_header_cannot_submit(self):
        t=WebTransport(OwnerMailbox(),password='offline-test-credential-not-real-123')
        headers={'Host':'127.0.0.1:8765','Content-Type':'application/json'}
        self.assertEqual(t.request('POST','/api/action',headers,b'{}')[0],403)
        headers['X-Local-Control']='1'
        self.assertEqual(t.request('POST','/api/action',headers,b'{}')[0],401)
