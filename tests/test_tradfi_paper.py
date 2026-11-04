"""Paper exchange and authenticated namespaces, with every native write forbidden."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import json,tempfile,unittest
from tradfi.paper import PaperSession
from tradfi.simulator import ARTIFICIAL_SPEC
from tradfi.control import ActionRejected,OwnerMailbox,Conflict
from tradfi.web_control import WebTransport

class Reader:
    def __init__(self):
        self.now=1793620830.;self.ms=int(self.now*1000);self.reason=None;self.safety=SimpleNamespace(check=lambda:None)
        self.data=dict(symbol='XAUUSD.s',source='MT5_READ_ONLY',status='FRESH',timestamp=self.now,tick_msc=self.ms,
            quote=dict(bid='4000',ask='4000.10',utc_time=self.now),candle=dict(id=int(self.now//60)*60-60,open='3999',close='4000'),
            spec=dict(ARTIFICIAL_SPEC,volume_min='.01',volume_step='.01',contract_size='100',tick_value_profit='1',tick_value_loss='1'),candles=[])
    def read(self):
        if self.reason:raise OSError(self.reason)
        return deepcopy(self.data)
    def view(self):return dict(deepcopy(self.data),age_seconds=self.now-self.data['timestamp'],reason=self.reason,status='WAITING' if self.reason else 'FRESH')
    def tick(self,bid,ask,seconds=1,bar=None):
        self.now+=seconds;self.ms=int(self.now*1000)
        self.data.update(timestamp=self.now,tick_msc=self.ms,quote=dict(bid=str(bid),ask=str(ask),utc_time=self.now))
        if bar:self.data['candle']=dict(id=int(self.now//60)*60-60,open=bar[0],close=bar[1])

class PaperTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'paper.db';self.reader=Reader();self.create();self.step()
        self.session.controller.state['tp_distances']=['.20']+[None]*19;self.session.controller.state['basket_settings']['INITIAL_SIZE']='.01';self.session.controller.save('fixture')
    def create(self):self.session=PaperSession(self.path,self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.reader.now)
    def tearDown(self):self.session.close()
    def step(self,n=1):
        for _ in range(n):self.session.step()
    @property
    def p(self):return self.session.exchange.p
    def action(self,a):return self.session.action(a)
    def command(self,text):return self.action(dict(type='command',text=text))
    def start(self,side='BUY'):
        self.command('/start');self.reader.tick('4000','4000.1',seconds=30,bar=('3999' if side=='BUY' else '4001','4000'));self.step(2)
        self.assertEqual(len(self.p['orders']),1)
    def initial(self,side='BUY'):
        self.start(side);self.reader.tick('4000.9','4001' if side=='BUY' else '4000.99') if side=='BUY' else self.reader.tick('3999','3999.1');self.step(3)
        self.assertEqual(len(self.p['positions']),1);self.assertEqual(len(self.p['orders']),0)
    def reversal(self,side='BUY'):
        self.initial(side)
        self.reader.tick('3998','3998.1') if side=='BUY' else self.reader.tick('4001.9','4002')
        self.step(3);self.assertFalse(self.p['positions']);self.assertEqual(len(self.p['orders']),12)
        self.reader.tick('3997','3997.1') if side=='BUY' else self.reader.tick('4002.9','4003')
        self.step(3);self.assertEqual(len(self.p['positions']),6);self.assertFalse(self.p['orders'])
    def test_initial_tp_cancels_opposite_and_new_cycle_fresh_close(self):
        self.initial();self.reader.tick('4001.25','4001.35');self.step(5)
        self.assertFalse(self.p['positions']);self.assertFalse(self.p['orders']);self.assertIsNone(self.session.controller.state['cycle'])
        self.step(3);self.assertFalse(self.p['orders'])
    def test_ask_buy_bid_sell_and_mirror(self):
        self.initial('SELL');self.assertEqual(self.p['positions'][0]['entry'],'3999')
        self.reader.tick('4001.9','4002');self.step(3)
        self.assertFalse(self.p['positions']);self.assertEqual(len(self.p['orders']),12)
        self.reader.tick('4002.9','4003');self.step(3)
        self.assertEqual({p['side'] for p in self.p['positions']},{'BUY'});self.assertEqual({p['entry'] for p in self.p['positions']},{'4003'})
        self.assertEqual(D(self.session.controller.state['cycle']['recovery']),D(3))
    def test_partial_tp_then_sl_reanchors_each_round_and_tp1_moves_sl(self):
        self.reversal();targets=sorted([D(p['tp']) for p in self.p['positions']],reverse=True)
        self.reader.tick(targets[1]-D('.1'),targets[1]);self.step(5);self.assertEqual(len(self.p['positions']),4)
        self.assertEqual({D(p['sl']) for p in self.p['positions']},{D('3997')})
        self.reader.tick('3998.4','3998.5');self.step(4)
        c=self.session.controller.state['cycle'];anchors=deepcopy(c['expanded']);self.assertEqual(anchors,{'BUY':'3999.50','SELL':'3997.50'})
        realized=sum(D(d['profit'])+D(d['commission'])+D(d['swap'])+D(d['fee']) for d in self.p['deals'])
        self.assertEqual(D(c['recovery']),-realized)
        self.reader.tick('3999.4','3999.5');self.step(3);self.assertEqual(len(self.p['positions']),6);self.assertFalse(self.p['orders'])
        self.reader.tick('3998','3998.1');self.step(4);self.assertEqual(c['expanded'],{'BUY':'3999.00','SELL':'3997.00'})
    def test_all_tps_cover_debt_and_stop_has_no_trailing(self):
        self.reversal();sl=[p['sl'] for p in self.p['positions']];target=min(D(p['tp']) for p in self.p['positions'])
        self.reader.tick('3996.95','3997.05');self.step(2);self.assertEqual([p['sl'] for p in self.p['positions']],sl)
        self.reader.tick(target-D('.1'),target);self.step(5);self.assertIsNone(self.session.controller.state['cycle'])
    def test_gap_execution_uses_observed_price_not_trigger_or_favorable_tp(self):
        self.start();self.reader.tick('4001.1','4001.2');self.step(3);self.assertEqual(self.p['positions'][0]['entry'],'4001.2')
        tp=self.p['positions'][0]['tp'];self.reader.tick('4002','4002.1');self.step(4)
        out=[d for d in self.p['deals'] if d['entry']=='OUT'];self.assertEqual(out[0]['price'],tp)
    def test_same_millisecond_distinct_quote_processed_duplicate_not(self):
        self.start();self.reader.tick('4000.8','4000.9');self.step();ms=self.reader.ms
        self.reader.tick('4000.9','4001',seconds=0);self.step(3);self.assertEqual(self.reader.ms,ms);self.assertEqual(len(self.p['positions']),1)
        count=len(self.p['deals']);self.step(3);self.assertEqual(len(self.p['deals']),count)
    def test_out_of_order_stale_and_missing_never_fill(self):
        self.start();self.reader.tick('4002','4002.1',seconds=-1);self.step();self.assertFalse(self.p['positions'])
        self.reader.now+=100;self.step();self.assertFalse(self.p['positions'])
        self.reader.reason='DISCONNECTED';self.step();self.assertFalse(self.p['positions'])
    def test_restart_baseline_no_historical_replay_and_explicit_resume(self):
        self.start();self.session.close();self.reader.tick('4001','4001.1',seconds=60);self.create();self.step(3)
        self.assertTrue(self.p['paused_gap']);self.assertFalse(self.p['positions'])
        self.action(dict(type='paper_resume'));self.step();self.assertFalse(self.p['positions'])
        self.reader.tick('4001','4001.1');self.step(3);self.assertEqual(len(self.p['positions']),1)
    def test_costs_model_and_flatten_reset_retain_debt(self):
        self.action(dict(type='paper_costs',commission_per_lot_side='2'));self.reversal()
        self.assertEqual(D(self.session.controller.state['cycle']['recovery']),D('3.16'))
        self.action(dict(type='paper_flatten',confirmed=True));self.step(2);self.command('/reset_manual')
        self.assertGreater(D(self.session.controller.state['basket_carry']),0);self.assertIsNone(self.session.controller.state['cycle'])
        self.session.close();self.create();self.step();self.assertGreater(D(self.session.controller.state['basket_carry']),0)
    def test_invalid_settings_then_stop_and_foreign_ticket(self):
        self.reversal();s=self.session.controller.state;before=deepcopy(self.p)
        bad=dict(type='protection',cycle_id=s['cycle']['id'],position_id='LIVE-123',old={'sl':'1','tp':'2'},new={'sl':'1','tp':'2'})
        with self.assertRaises(ActionRejected):self.action(bad)
        self.assertEqual(before,self.p)
        bad=dict(type='basket_settings',cycle_id=s['cycle']['id'],settings=dict(s['basket_settings'],TICKET_SIZE='NaN'))
        with self.assertRaises(ActionRejected):self.action(bad)
        self.command('/stop');self.assertTrue(s['stop'])
    def test_paper_receipts_duplicate_and_revision_conflict(self):
        gate=self.session.gateway;r=dict(id='same-id-001',revision=0,action=dict(type='command',text='/stop'))
        self.assertEqual(gate.apply(r)['status'],'APPLIED');self.assertEqual(gate.apply(r)['status'],'APPLIED')
        with self.assertRaises(Conflict):gate.apply(dict(r,id='stale-id-002'))
    def test_gap_resume_cannot_bypass_24h_limit(self):
        self.start();self.reader.tick('4000','4000.1',seconds=90000);self.step()
        self.assertTrue(self.p['paused_gap'])
        with self.assertRaises(ActionRejected):self.action(dict(type='paper_resume'))
        self.assertEqual(self.session.controller.state['cycle']['manual_reason'],'CYCLE_24H_LIMIT')
        self.assertFalse(self.p['positions'])
    def test_stale_start_is_rejected_without_locking_stop(self):
        self.reader.reason='CLOSED_MARKET';self.step()
        gate=self.session.gateway
        first=gate.apply(dict(id='stale-start-001',revision=0,action=dict(type='command',text='/start')))
        self.assertEqual(first['status'],'REJECTED')
        self.assertEqual(gate.apply(dict(id='stop-after-stale',revision=1,action=dict(type='command',text='/stop')))['status'],'APPLIED')
    def test_active_sl_edit_confirmed_without_resizing_and_stale_preview_rejected(self):
        self.reversal();c=self.session.controller.state['cycle'];settings=dict(c['cfg'],RECOVERY_SL_DISTANCE='1.25',BASKET_COUNT=4)
        self.action(dict(type='basket_settings',settings=settings,cycle_id=c['id']));self.step(5)
        self.assertEqual(len(self.p['positions']),6);self.assertEqual({D(p['sl']) for p in self.p['positions']},{D('3998.25')})
        p=self.p['positions'][0]
        with self.assertRaises(ActionRejected):self.action(dict(type='protection',cycle_id='old-cycle',position_id=p['id'],old=dict(sl=p['sl'],tp=p['tp']),new=dict(sl=p['sl'],tp=p['tp'])))
    def test_restart_after_fill_before_controller_adoption_books_once(self):
        self.start();self.reader.tick('4000.9','4001');self.session.exchange.observe(self.reader.read())
        self.assertEqual(len(self.p['positions']),1);self.session.close();self.create();self.step()
        self.assertTrue(self.p['paused_gap']);self.action(dict(type='paper_resume'));self.step(4)
        self.assertEqual(len(self.p['positions']),1);self.assertEqual(len(self.p['orders']),0)
        self.assertEqual(len([d for d in self.p['deals'] if d['entry']=='IN']),1)
    def test_new_oco_both_sides_crossed_in_one_observed_tick_halts(self):
        self.reversal();self.reader.tick('3999.4','3999.5');self.step(3)
        self.assertEqual(len(self.p['orders']),12)
        self.reader.tick('3996','4001');self.step(3)
        self.assertEqual(self.session.controller.state['phase'],'MANUAL')
        self.assertFalse(any(cmd['kind']=='MARKET' for cmd in self.session.controller.state['commands'].values()))
    def test_authenticated_route_namespaces_cannot_cross_live_scope(self):
        live=OwnerMailbox();live._view=dict(scope='LIVE',revision=0)
        t=WebTransport(live,password='x'*32,paper_mailbox=self.session.mailbox);self.step()
        h={'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        self.assertEqual(t.request('GET','/api/paper/state',h)[0],401)
        code,_,extra=t.request('POST','/api/login',h,json.dumps({'password':'x'*32}).encode());h['Cookie']=extra['Set-Cookie'].split(';')[0]
        self.assertEqual(t.request('GET','/api/paper/state',h)[1]['mode'],'PAPER')
        req=json.dumps(dict(id='same-id-001',revision=0,action=dict(type='command',text='/stop'))).encode()
        h['X-Controller-Scope']='LIVE';self.assertEqual(t.request('POST','/api/paper/action',h,req)[0],409)
        h['X-Controller-Scope']=self.session.scope;self.assertEqual(t.request('POST','/api/action',h,req)[0],409)
        self.assertEqual(t.request('POST','/api/paper/action',h,req)[0],202);self.step();self.assertEqual(live.view(),dict(scope='LIVE',revision=0))

class NativePaperIsolationTest(unittest.TestCase):
    def setUp(self):
        from test_tradfi_native_utc import UTCAPI
        from tradfi.paper_market import MarketReader
        self.api=UTCAPI();self.api.info=self.api.info._replace(trade_stops_level=1)
        self.writes=[]
        def forbidden(*a,**k):self.writes.append(True);raise AssertionError('PAPER CALLED NATIVE TRADE METHOD')
        self.api.order_send=forbidden;self.api.order_check=forbidden
        self.api.account=self.api.account._replace(margin_mode=0)  # netting is fine for market reading
        self.reader=MarketReader(self.api,symbol='XAUUSD.s',clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.reader.next_check=float('inf');self.seed()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
    def seed(self):
        self.reader.safety.anchor=(self.api.now,self.api.now);self.reader.safety.result=dict(ok=True,checked_monotonic=self.api.now)
    def test_capability_has_no_trade_or_live_cycle_access_and_accepts_netting_data(self):
        self.assertFalse(hasattr(self.reader,'api'));self.assertFalse(hasattr(self.reader,'order_send'));self.assertFalse(hasattr(self.reader,'broker'))
        market=self.reader.read();self.assertEqual(market['symbol'],'XAUUSD.s');self.assertEqual(market['quote']['utc_time'],self.api.now);self.assertFalse(self.writes)
    def test_market_source_identity_change_does_not_rebind_or_relabel_currency(self):
        self.reader.read();self.api.account=self.api.account._replace(login=222222222)
        with self.assertRaises(OSError):self.reader.read()
        self.assertFalse(self.writes)
    def test_wrong_symbol_stale_wall_time_and_missing_data_wait(self):
        from tradfi.paper_market import MarketReader
        with self.assertRaises(ValueError):MarketReader(self.api,symbol='XAUUSD.S',clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        for shift in (7200,-10):
            self.api.shift=shift
            with self.assertRaises(OSError):self.reader.read()
        self.api.shift=0;self.api.unavailable=True
        with self.assertRaises(OSError):self.reader.read()
        self.assertFalse(self.writes)
    def test_real_transport_paper_actions_leave_live_db_unchanged(self):
        from tradfi.storage import Store
        from tradfi.basket import BasketController
        from tradfi.interactive_simulator import InteractiveSimulator
        from tradfi.web_control import NativePanel
        live_store=Store(Path(self.tmp.name)/'live.db');self.addCleanup(live_store.close)
        live=BasketController(live_store,InteractiveSimulator());live.broker.account='LIVE';live.broker.symbol='XAUUSD.s'
        paper=PaperSession(Path(self.tmp.name)/'paper.db',self.reader,clock=lambda:self.api.now,monotonic=lambda:self.api.now);self.addCleanup(paper.close)
        panel=NativePanel(live,password='x'*32,paper_session=paper);paper.step()
        before=list(live_store.db.iterdump())
        h={'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        _,_,extra=panel.transport.request('POST','/api/login',h,json.dumps({'password':'x'*32}).encode());h['Cookie']=extra['Set-Cookie'].split(';')[0];h['X-Controller-Scope']=paper.scope
        actions=[dict(type='tp_distances',cycle_id=None,distances=['.20']+[None]*19),dict(type='paper_costs',commission_per_lot_side='2'),dict(type='command',text='/start'),dict(type='command',text='/stop'),dict(type='paper_resume'),dict(type='paper_flatten',confirmed=True),dict(type='command',text='/reset_manual')]
        for i,a in enumerate(actions):
            request=dict(id='isolate-%04d'%i,revision=paper.controller.state.get('control_revision',0),action=a)
            self.assertEqual(panel.transport.request('POST','/api/paper/action',h,json.dumps(request).encode())[0],202);paper.step()
        self.assertEqual(list(live_store.db.iterdump()),before);self.assertFalse(self.writes)
        paper.close();paper=PaperSession(Path(self.tmp.name)/'paper.db',self.reader,clock=lambda:self.api.now,monotonic=lambda:self.api.now);paper.step();paper.close()
        self.assertEqual(list(live_store.db.iterdump()),before)
    def _run_runtime(self,execute):
        from unittest.mock import patch
        from tradfi.runtime import _run_mt5
        from tradfi.web_control import NativePanel
        from time_fixtures import verified_time
        self.api.account=self.api.account._replace(margin_mode=self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING if execute else 0)
        config=dict(terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',backend='mt5_direct_v1',time_profile='bybit_live6_time_v1',expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,enable_trading=True,execution_contract='cycle_scoped_identity_v1',legacy_ea_removed=True,legacy_state_reconciled=True,state_file=str(Path(self.tmp.name)/'live.db'),log_file=str(Path(self.tmp.name)/'run.log'))
        observed=[];outer=self
        class Panel(NativePanel):
            def start(self):observed.append(self)
            def close(self):pass
        class Stop:
            count=0
            def is_set(self):return self.count>=5
            def wait(self,seconds):
                panel=observed[0];paper=panel.paper_session
                if self.count==0:
                    paper.action(dict(type='tp_distances',cycle_id=None,distances=['.20']+[None]*19))
                    paper.action(dict(type='command',text='/start'))
                if self.count==3:paper.action(dict(type='command',text='/stop'))
                outer.api.now+=1;self.count+=1
        with patch('tradfi.runtime.account_lock_path',return_value=str(Path(self.tmp.name)/'account.lock')),patch('tradfi.paper_market.MarketReader',return_value=self.reader),patch('tradfi.time_safety.TimeSafety',side_effect=verified_time):
            _run_mt5(config,False,service=True,execute=execute,api=self.api,panel_factory=Panel,stop_event=Stop(),clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.assertFalse(self.writes);self.assertEqual(observed[0].controller.state['phase'],'IDLE')

    def test_runtime_assembly_with_execute_enabled_never_writes_from_paper(self):self._run_runtime(True)
    def test_runtime_netting_account_with_live_disabled_still_serves_paper(self):self._run_runtime(False)
