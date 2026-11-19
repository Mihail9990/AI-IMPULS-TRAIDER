import unittest
from types import SimpleNamespace as N
from recovery.engine import Engine
from recovery.controller import Controller
from recovery.pipeline import MockPipeline
from test_recovery_mt5_transport import FakeMT5

class TradeFake(FakeMT5):
    DEAL_ENTRY_IN=0;DEAL_ENTRY_OUT=1;DEAL_TYPE_BUY=0;DEAL_TYPE_SELL=1
    DEAL_REASON_SL=4;DEAL_REASON_CLIENT=0;DEAL_REASON_MOBILE=1;DEAL_REASON_WEB=2
    def __init__(self):super().__init__();self.counter=50;self.reject_sl=False;self.lose_sl_response=False
    def order_send(self,r):
        self.sent.append(r)
        if r['action']==5:
            self.counter+=1
            self.orders.append(N(ticket=self.counter,magic=r['magic'],symbol=r['symbol'],type=r['type'],price_open=r['price'],sl=r['sl'],tp=r['tp'],volume_current=r['volume'],state=1))
            return N(retcode=self.TRADE_RETCODE_PLACED,order=self.counter,deal=0)
        if r['action']==6:
            if self.reject_sl:return N(retcode=self.TRADE_RETCODE_TIMEOUT,order=0,deal=0)
            p=next(p for p in self.positions if p.ticket==r['position']);p.sl=r['sl'];p.tp=r['tp']
            if self.lose_sl_response:raise TimeoutError('response lost after effect')
            return N(retcode=self.TRADE_RETCODE_DONE,order=0,deal=0)
        raise ValueError('Unsupported fake send')
    def fill(self,ticket=51):
        o=next(o for o in self.orders if o.ticket==ticket);self.orders.remove(o)
        o.state=self.ORDER_STATE_FILLED;self.history.append(o)
        self.positions=[N(ticket=70,identifier=700,magic=o.magic,symbol=o.symbol,type=0,price_open=4000,volume=.01,sl=o.sl,tp=0)]
        self.ask=4000.2
    def deal(self,ticket=100,entry=0,reason=0):
        return N(ticket=ticket,order=51 if entry==0 else 99,position_id=700,entry=entry,type=0 if entry==0 else 1,
                 reason=reason,symbol='XAUUSD.s',magic=123 if entry==0 else 0,volume=.01,price=4000,
                 profit=0,commission=-.01,swap=0,fee=0,time_msc=ticket)

class PipelineTests(unittest.TestCase):
    def make(self):
        e=Engine();e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}])
        c=Controller(e,test_mode=True);a=TradeFake();p=MockPipeline(c,a,1,'demo','XAUUSD.s',123)
        return c,a,p
    def enter(self,c,a,p):
        p.sync_pending();a.fill();p.observe_mt5_deals([a.deal()])
    def test_full_pipeline_verified_support_and_close(self):
        c,a,p=self.make();self.enter(c,a,p)
        self.assertEqual(len(c.engine().data['positions']),1)
        p.sync_pending();self.assertEqual(len(a.orders),1)
        a.ask=4000.7;p.protect_quote('4000.5','4000.7')
        self.assertEqual(c.engine().data['positions'][0]['stop_stage'],2)
        self.assertEqual(a.positions[0].sl,4000.)
        a.positions=[];p.observe_mt5_deals([a.deal(101,1,a.DEAL_REASON_SL)])
        self.assertEqual(c.engine()._range('1')['full_sl_count'],0)
        self.assertTrue(any(o['kind']=='reentry' for o in c.engine().data['orders']))
    def test_no_support_stage_on_unverified_sl(self):
        c,a,p=self.make();self.enter(c,a,p);a.reject_sl=True;a.ask=4000.7
        p.protect_quote('4000.5','4000.7')
        self.assertEqual(c.engine().data['positions'][0]['stop_stage'],0)
        self.assertIn('1',c.state()['blocked'])
        with self.assertRaisesRegex(ValueError,'classification blocked'):
            p.observe_mt5_deals([a.deal(101,1,a.DEAL_REASON_SL)])
    def test_lost_sl_response_can_verify_actual_effect(self):
        c,a,p=self.make();self.enter(c,a,p);a.lose_sl_response=True;a.ask=4000.7
        p.protect_quote('4000.5','4000.7')
        self.assertEqual(c.engine().data['positions'][0]['stop_stage'],2)
        self.assertEqual(c.state()['blocked'],{})
    def test_manual_mobile_close_with_zero_magic_maps_by_position(self):
        c,a,p=self.make();self.enter(c,a,p)
        p.observe_mt5_deals([a.deal(101,1,a.DEAL_REASON_MOBILE)])
        self.assertEqual(c.engine().data['positions'],[])
        self.assertEqual(c.engine()._range('1')['full_sl_count'],0)
    def test_wrong_entry_side_rejected(self):
        c,a,p=self.make();p.sync_pending();a.fill();d=a.deal();d.type=1
        with self.assertRaisesRegex(ValueError,'side mismatch'):p.observe_mt5_deals([d])
        self.assertEqual(c.engine().data['positions'],[])
    def test_history_poll_cursor_and_overlap_dedup(self):
        from datetime import datetime, timezone
        c,a,p=self.make();p.sync_pending();a.fill();d=a.deal()
        calls=[]
        def history(start,end):calls.append((start,end));return [d]
        a.history_deals_get=history
        now=datetime.now(timezone.utc)
        p.poll_history(now);before=c.state();p.poll_history(now)
        self.assertEqual(c.state(),before)
        self.assertEqual(c.data['observer_cursor_msc'],d.time_msc)
        self.assertLess(calls[1][0].timestamp()*1000,d.time_msc)
        a.history_deals_get=lambda start,end:None
        with self.assertRaises(ValueError):p.poll_history(now)
        self.assertEqual(c.data['observer_cursor_msc'],d.time_msc)
    def test_owned_unmapped_deal_does_not_advance_cursor(self):
        from datetime import datetime, timezone
        c,a,p=self.make();a.history_deals_get=lambda start,end:[a.deal()]
        with self.assertRaisesRegex(ValueError,'no verified ticket mapping'):p.poll_history(datetime.now(timezone.utc))
        self.assertNotIn('observer_cursor_msc',c.data)
    def test_partial_entry_protected_without_full_strategy_position(self):
        c,a,p=self.make();p.sync_pending()
        a.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,price_open=4000.1,volume=.004,sl=3999.3,tp=0)]
        a.ask=4000.3
        d=a.deal();d.volume=.004;d.price=4000.1
        p.observe_mt5_deals([d]);p.protect_partial()
        self.assertAlmostEqual(a.positions[0].sl,3999.4)
        self.assertEqual(c.engine().data['positions'],[])
        self.assertEqual(c.state()['bindings']['51']['protected_volume'],'0.004')
        self.assertEqual(c.state()['bindings']['51']['protection_status'],'verified')
    def test_partial_protection_preserves_tighter_stop(self):
        c,a,p=self.make();p.sync_pending()
        a.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,price_open=4000,volume=.004,sl=3999.9,tp=0)]
        a.ask=4000.3;d=a.deal();d.volume=.004
        p.observe_mt5_deals([d]);p.protect_partial()
        self.assertAlmostEqual(a.positions[0].sl,3999.9)
    def test_partial_exposure_mismatch_does_not_send_protection(self):
        c,a,p=self.make();p.sync_pending()
        a.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,price_open=4000,volume=.006,sl=3999.3,tp=0)]
        d=a.deal();d.volume=.004;p.observe_mt5_deals([d]);sent=len(a.sent)
        p.protect_partial()
        self.assertEqual(len(a.sent),sent)
        self.assertIn('volume differs',c.state()['blocked']['1'])
    def test_future_history_batch_rejected_before_any_mutation(self):
        from datetime import datetime, timezone
        c,a,p=self.make();p.sync_pending();a.fill();now=datetime.now(timezone.utc)
        d=a.deal();d.time_msc=int(now.timestamp()*1000)+60000
        a.history_deals_get=lambda start,end:[d]
        before=c.state()
        with self.assertRaisesRegex(ValueError,'Future'):p.poll_history(now)
        self.assertEqual(c.state(),before);self.assertEqual(c.data['deals'],{})
        self.assertNotIn('observer_cursor_msc',c.data)
