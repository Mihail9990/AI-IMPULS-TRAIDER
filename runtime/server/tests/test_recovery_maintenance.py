import unittest
from types import SimpleNamespace as N
from recovery.engine import Engine
from recovery.controller import Controller
from recovery.pipeline import MockPipeline
from recovery.maintenance import Maintenance
from test_recovery_integrated_api import CancelFake

class CloseFake(CancelFake):
    TRADE_ACTION_DEAL=1;ORDER_TYPE_BUY=0;ORDER_TYPE_SELL=1
    ORDER_FILLING_FOK=0;ORDER_FILLING_IOC=1
    def __init__(self):super().__init__();self.close_deals=[];self.partial=False
    def history_deals_get(self,start,end):return self.close_deals
    def order_send(self,r):
        if r['action']!=1:return super().order_send(r)
        self.sent.append(r);p=next(p for p in self.positions if p.ticket==r['position'])
        volume=r['volume']/2 if self.partial else r['volume']
        self.close_deals.append(N(ticket=900,order=901,position_id=p.identifier,symbol=p.symbol,
                                  entry=1,volume=volume))
        if self.partial:p.volume-=volume
        else:self.positions.remove(p)
        return N(retcode=self.TRADE_RETCODE_DONE_PARTIAL if self.partial else self.TRADE_RETCODE_DONE,order=901,deal=900)

class MaintenanceTests(unittest.TestCase):
    def make(self):
        e=Engine();e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}])
        c=Controller(e,test_mode=True);a=CloseFake();p=MockPipeline(c,a,1,'demo','XAUUSD.s',123)
        p.sync_pending();return c,a,p,Maintenance(p)
    def test_volume_replace_new_identity_after_verified_cancel(self):
        c,a,p,m=self.make();old=c.engine().data['orders'][0]['id']
        c.data['engine']['orders'][0]['volume']='.02';c._save()
        result=m.replace_pending_volume(old,'op1')
        new=c.engine().data['orders'][0]['id'];self.assertNotEqual(old,new)
        self.assertEqual(len(a.orders),1);self.assertEqual(a.orders[0].volume_current,.02)
        self.assertEqual(c.data['bindings']['51']['phase'],'cancelled')
    def test_replace_race_no_new_order(self):
        c,a,p,m=self.make();old=c.engine().data['orders'][0]['id']
        c.data['engine']['orders'][0]['volume']='.02';a.race=True
        with self.assertRaises(ValueError):m.replace_pending_volume(old,'op1')
        self.assertEqual(a.orders,[]);self.assertTrue(a.positions);self.assertIn('1',c.data['blocked'])
    def test_cancel_partial_remainder_keeps_position(self):
        c,a,p,m=self.make();d=a.deal();d.volume=.004;p.observe_mt5_deals([d])
        a.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,price_open=4000,volume=.004,sl=3999.3,tp=0)]
        a.orders[0].volume_current=.006
        m.cancel_partial_remainder(51,'op')
        self.assertEqual(a.orders,[]);self.assertEqual(a.positions[0].volume,.004)
        self.assertEqual(c.data['bindings']['51']['phase'],'open')
        self.assertEqual(c.engine().data['positions'][0]['volume'],'0.004')
        self.assertFalse(c.engine()._range('1').get('next_volume_policy_pending',False))
        self.assertEqual(c.engine().data['orders'][0]['volume'],'0.01')
        self.assertNotIn('1',c.data['blocked'])
        a.ask=4000.7;p.protect_quote('4000.5','4000.7')
        self.assertEqual(c.engine().data['positions'][0]['stop_stage'],2)
    def test_market_close_requires_history_proof(self):
        c,a,p,m=self.make();a.fill();p.observe_mt5_deals([a.deal()])
        result=m.close_verified_position(700,'close1',filling='ORDER_FILLING_IOC')
        self.assertEqual(result['state'],'VERIFIED');self.assertEqual(a.positions,[])
        # Strategy closure is intentionally not fabricated until deals ingested.
        self.assertEqual(len(c.engine().data['positions']),1)
    def test_partial_market_close_never_reports_complete(self):
        c,a,p,m=self.make();a.fill();p.observe_mt5_deals([a.deal()]);a.partial=True
        with self.assertRaises(ValueError):m.close_verified_position(700,'close1',filling='ORDER_FILLING_IOC')
        self.assertEqual(a.positions[0].volume,.005);self.assertIn('1',c.data['blocked'])
    def test_startup_missing_mapped_order_not_safe(self):
        c,a,p,m=self.make();a.orders=[]
        report=m.inspect_startup();self.assertFalse(report['safe_to_resume'])
        self.assertEqual(report['missing_mapped_orders'],[51])

    def partial(self):
        c,a,p,m=self.make();deal=a.deal();deal.volume=.004;p.observe_mt5_deals([deal])
        a.positions=[N(ticket=70,identifier=700,magic=123,symbol='XAUUSD.s',type=0,price_open=4000,volume=.004,sl=3999.3,tp=0)]
        a.orders[0].volume_current=.006
        return c,a,p,m

    def test_partial_cancel_racing_fill_not_promoted(self):
        c,a,p,m=self.partial();a.race=True
        with self.assertRaises(ValueError):m.cancel_partial_remainder(51,'race')
        self.assertIn('1',c.data['blocked'])
        self.assertEqual(c.engine().data['positions'],[])
        self.assertEqual(c.data['bindings']['51']['expected_volume'],'0.01')

    def test_partial_cancel_timeout_not_promoted(self):
        c,a,p,m=self.partial();a.cancel_timeout=True
        with self.assertRaises(ValueError):m.cancel_partial_remainder(51,'timeout')
        self.assertEqual(c.engine().data['positions'],[])
        self.assertEqual(c.data['bindings']['51']['phase'],'partial_entry')

    def test_accepted_partial_close_uses_observed_volume_and_schedule_trigger(self):
        c,a,p,m=self.partial();m.cancel_partial_remainder(51,'accept')
        b=c.data['bindings']['51']
        c.deal({'deal_id':999,'entry_order_ticket':51,'account':1,'server':'demo','symbol':'XAUUSD.s','magic':123,
                'position_identifier':700,'entry':'OUT','side':'BUY','volume':'.004','price':'3999.3',
                'realized_net':'-.03','close_reason':'initial_sl'})
        self.assertEqual(c.engine().data['positions'],[])
        self.assertEqual(c.engine().data['orders'][0]['volume'],'0.01')
        self.assertEqual(c.engine()._range('1')['full_sl_count'],1)
        self.assertEqual(c.data['bindings']['51']['phase'],'closed')
        self.assertEqual(b['requested_volume'],'0.01')

    def test_partial_reverse_uses_next_schedule_slot_at_boundary(self):
        c,a,p,m=self.partial()
        c.data['engine']['ranges'][0]['full_sl_count']=4
        c._save()
        m.cancel_partial_remainder(51,'boundary')
        self.assertEqual(c.engine().data['positions'][0]['volume'],'0.004')
        reverse=c.engine().data['orders'][0]
        self.assertEqual(reverse['volume'],'0.02')
        self.assertEqual(reverse['side'],'SELL')
        a.ask=4000.2;p.sync_pending()
        self.assertEqual(a.orders[0].volume_current,.02)

    def test_final_slot_partial_has_no_25th_trigger(self):
        c,a,p,m=self.partial()
        c.data['engine']['ranges'][0]['full_sl_count']=23
        c._save()
        m.cancel_partial_remainder(51,'final')
        self.assertEqual(c.engine().data['positions'][0]['volume'],'0.004')
        self.assertEqual(c.engine().data['orders'],[])

    def test_partial_promotion_rejects_entry_side_scope_mismatch(self):
        for field,value in [('price_open',4001),('type',1),('symbol','OTHER'),('magic',999)]:
            with self.subTest(field=field):
                c,a,p,m=self.partial();setattr(a.positions[0],field,value)
                with self.assertRaises(ValueError):m.cancel_partial_remainder(51,'identity')
                self.assertEqual(c.engine().data['positions'],[])
                self.assertIn('1',c.data['blocked'])
                self.assertNotIn('partial_entry_accepted',c.data['bindings']['51'])

    def test_partial_equal_net_volume_in_out_race_is_not_promoted(self):
        c,a,p,m=self.partial()
        incoming=a.deal(ticket=101);incoming.volume=.002
        outgoing=a.deal(ticket=102,entry=1);outgoing.volume=.002
        a.close_deals=[incoming,outgoing]
        with self.assertRaises(ValueError):m.cancel_partial_remainder(51,'roundtrip')
        self.assertEqual(a.positions[0].volume,.004)
        self.assertIn('101',c.data['deals']);self.assertIn('102',c.data['deals'])
        self.assertIn('1',c.data['blocked'])
        self.assertEqual(c.engine().data['positions'],[])

    def test_partial_history_failure_prevents_promotion(self):
        c,a,p,m=self.partial()
        original=m._cancel
        def cancel_then_lose_history(*args):
            original(*args)
            a.history_deals_get=lambda *args:None
        m._cancel=cancel_then_lose_history
        with self.assertRaises(ValueError):m.cancel_partial_remainder(51,'history-gap')
        self.assertIn('1',c.data['blocked'])
        self.assertEqual(c.engine().data['positions'],[])
