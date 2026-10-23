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
        self.rates=[dict(time=int(self.now//60)*60-60,close=4000.)]
    def symbol_info_tick(self,symbol):
        stamp=self.now+offset_at(self.now)
        return record(bid=self.tick.bid,ask=self.tick.ask,time=int(stamp),time_msc=int(stamp*1000))
    def copy_rates_from_pos(self,*args):
        return [dict(r,time=r['time']+offset_at(r['time'])) for r in super().copy_rates_from_pos(*args)]

class RuntimePipelineTest(unittest.TestCase):
    def test_runtime_telegram_controller_adapter_fill_modify_close_and_feed(self):
        api=RuntimeAPI();panels=[];bots=[];stages=[]
        class Stop:
            done=False
            def is_set(self):return self.done
            def wait(self,*args):pass
        stop=Stop()
        class Panel(NativePanel):
            def start(self):panels.append(self) # no real sockets
        class Telegram:
            def __init__(self,*args,**kwargs):self.http=self;self.n=0
            def updates(self,*args):return []
            def start(self,*args):pass
            def close(self):pass
            def process(self,bot):
                bots.append(bot);self.n+=1
                if self.n>20:raise AssertionError('runtime failed to settle')
                if self.n==1:
                    bot.command('/start');assert bot.state['notifications'][-1]['reply_markup']['inline_keyboard']
                    bot.command('/start semi');bot.triggers('ranges',{'BUY':'3','SELL':'3'});stages.append('semi')
                elif self.n==3:api.fill();stages.append('fill')
                elif self.n==6:
                    p=bot.snapshot()['positions'][0]
                    bot.protection(p['id'],{'sl':p['sl'],'tp':p['tp']},{'sl':p['sl'],'tp':str(float(p['tp'])+.25)})
                    stages.append('modify')
                elif self.n==9:
                    p=bot.snapshot()['positions'][0];bot.close_position(p['id'],p['size']);stages.append('close')
                elif self.n==12:
                    assert bot.state['phase']=='WAIT_TRIGGER';stop.done=True
        with tempfile.TemporaryDirectory() as tmp:
            config=dict(backend='mt5_direct_v1',time_profile=BYBIT_PROFILE,expected_server='Bybit-Live-6',expected_login=111111111,
                symbol='XAUUSD.s',magic=25101102,terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',enable_trading=True,
                execution_contract='cycle_scoped_identity_v1',legacy_ea_removed=True,legacy_state_reconciled=True,
                state_file=str(Path(tmp)/'state.db'),log_file=str(Path(tmp)/'log'),web={'enabled':True})
            with patch.dict('os.environ',{'TRADFI_TELEGRAM_TOKEN':'fake','TRADFI_TELEGRAM_CHAT_ID':'123','LOCALAPPDATA':tmp}),patch('tradfi.time_safety.TimeSafety',side_effect=verified_time):
                _run_mt5(config,False,service=True,execute=True,api=api,telegram_factory=Telegram,panel_factory=Panel,
                    stop_event=stop,clock=lambda:api.now,monotonic=lambda:api.now)
            self.assertEqual(stages,['semi','fill','modify','close']);self.assertFalse(api.positions)
            self.assertEqual(panels[0].mailbox.view()['source'],'INJECTED_MT5')
            self.assertEqual(panels[0].mailbox.view()['feed']['status'],'FRESH')
            self.assertEqual(len([s for s in api.sends if s['action']==api.TRADE_ACTION_DEAL]),1)
            self.assertTrue(any(s['action']==api.TRADE_ACTION_SLTP for s in api.sends))
