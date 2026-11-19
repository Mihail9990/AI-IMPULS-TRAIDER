import unittest
from decimal import Decimal
from recovery.engine import Engine
from recovery.volume_schedule import volume_for_slot

class IntegratedVolumes(unittest.TestCase):
    def make(self):
        e=Engine();e.replace_ranges([{'id':'1','price':'4000','side':'BUY','volume':'.01'},
                                    {'id':'2','price':'3950','side':'SELL','volume':'.01'}]);return e
    def order(self,e,rid,kind=None):
        return next(o for o in e.data['orders'] if o['range_id']==rid and (kind is None or o['kind']==kind))
    def fill(self,e,rid,tag):
        o=self.order(e,rid);e.fill('f'+tag,o['id'],o['price'])
        return next(p for p in e.data['positions'] if p['range_id']==rid)
    def test_all_24_positions_and_independent_counts(self):
        e=self.make()
        for n in range(1,25):
            p=self.fill(e,'1',str(n))
            self.assertEqual(Decimal(p['volume']),volume_for_slot(n))
            self.assertEqual(e._range('2')['full_sl_count'],0)
            self.assertEqual(Decimal(self.order(e,'2')['volume']),Decimal('.01'))
            e.close('c'+str(n),p['id'],'initial_sl','-1')
        self.assertEqual(e._range('1')['run_state'],'cancellation_pending')
        self.assertEqual(e._range('2')['run_state'],'active')
        p=self.fill(e,'2','other');self.assertEqual(Decimal(p['volume']),Decimal('.01'))
    def test_fifth_support_close_keeps_slot_five_not_six(self):
        e=self.make()
        for n in range(4):
            p=self.fill(e,'1',str(n));e.close('c'+str(n),p['id'],'initial_sl','-1')
        p=self.fill(e,'1','5');self.assertEqual(Decimal(p['volume']),Decimal('.01'))
        # The prearmed reverse anticipates full stop #5, but support exit is not full.
        self.assertEqual(Decimal(self.order(e,'1')['volume']),Decimal('.02'))
        e.quote(str(Decimal(p['entry'])+Decimal('.5')),str(Decimal(p['entry'])+Decimal('.7')))
        e.close('support',p['id'],'support_sl','0')
        self.assertEqual(e._range('1')['full_sl_count'],4)
        self.assertTrue(all(Decimal(o['volume'])==Decimal('.01') for o in e.data['orders'] if o['range_id']=='1'))
        re=self.order(e,'1','reentry');e.fill('reentry',re['id'],re['price'])
        p=next(p for p in e.data['positions'] if p['range_id']=='1')
        e.close('full5',p['id'],'initial_sl','-1')
        self.assertEqual(Decimal(self.order(e,'1')['volume']),Decimal('.02'))
    def test_final_slot_has_no_reverse_even_after_support_reentry(self):
        e=self.make()
        for n in range(23):
            p=self.fill(e,'1',str(n));e.close('c'+str(n),p['id'],'initial_sl','-1')
        p=self.fill(e,'1','24')
        self.assertFalse(any(o['range_id']=='1' for o in e.data['orders']))
        direction=Decimal(1) if p['side']=='BUY' else Decimal(-1)
        target=Decimal(p['entry'])+direction*Decimal('.5')
        bid,ask=(target,target+Decimal('.2')) if p['side']=='BUY' else (target-Decimal('.2'),target)
        e.quote(str(bid),str(ask));e.close('support24',p['id'],'support_sl','0')
        re=self.order(e,'1','reentry');self.assertEqual(Decimal(re['volume']),Decimal('.10'))
        e.fill('repeat24',re['id'],re['price'])
        self.assertFalse(any(o['range_id']=='1' for o in e.data['orders']))
        p=next(p for p in e.data['positions'] if p['range_id']=='1')
        e.close('full24',p['id'],'initial_sl','-1')
        self.assertEqual(e._range('1')['full_sl_count'],24)
        self.assertEqual(e._range('1')['run_state'],'cancellation_pending')
        e.confirm_limit_cancellations('1',[])
        self.assertEqual(e._range('1')['run_state'],'manual')
