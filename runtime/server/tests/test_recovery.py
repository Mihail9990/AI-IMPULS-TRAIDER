import unittest
from decimal import Decimal as D
from recovery.engine import Engine


def ranges(two=False):
    rs = [{'id':'1','price':'4000','side':'BUY','volume':'.01'}]
    if two:
        rs.append({'id':'2','price':'3950','side':'SELL','volume':'.01'})
    return rs


class RecoveryTests(unittest.TestCase):
    def engine(self, two=False):
        e = Engine()
        e.replace_ranges(ranges(two))
        return e

    def fill(self, e, event='fill1', entry='4000', index=0):
        oid = e.data['orders'][index]['id']
        e.fill(event, oid, entry)
        return e.data['positions'][0]

    def test_initial_and_actual_sl(self):
        e = self.engine()
        self.assertEqual(D(e.data['orders'][0]['sl']), D('3999.3'))
        p = self.fill(e, entry='4000.1')
        self.assertEqual(D(p['sl']), D('3999.4'))
        self.assertEqual(D(e.data['orders'][0]['price']), D('3999.4'))

    def test_buy_bid_support_and_breakeven_no_backwards(self):
        e = self.engine(); p = self.fill(e)
        e.quote('4000.24','4000.50'); self.assertEqual(p['stop_stage'], 0)
        e.quote('4000.25','4000.60'); self.assertEqual(D(p['sl']),D('3999.75'))
        e.quote('4000.5','4000.7'); self.assertEqual(D(p['sl']),D('4000'))
        e.quote('4000.1','4000.3'); self.assertEqual(p['stop_stage'],2)
        self.assertEqual(D(e.data['orders'][0]['price']), D('3999.3'))

    def test_sell_ask_mirror(self):
        e = Engine(); e.replace_ranges([{'id':'2','price':'3950','side':'SELL','volume':'.01'}])
        p = self.fill(e, entry='3950')
        e.quote('3949.5','3949.76'); self.assertEqual(p['stop_stage'],0)
        e.quote('3949.5','3949.75'); self.assertEqual(D(p['sl']),D('3950.25'))
        e.quote('3949.2','3949.50'); self.assertEqual(D(p['sl']),D('3950'))
        e.close('c',p['id'],'support_sl','-0.1')
        reentry=next(o for o in e.data['orders'] if o['kind']=='reentry')
        self.assertEqual(D(reentry['price']),D('3949.3'))

    def test_reentry_retains_reverse_then_moves_on_fill(self):
        e = self.engine(); p=self.fill(e)
        reverse=e.data['orders'][0].copy()
        e.quote('4000.25','4000.45')
        e.close('close1',p['id'],'support_sl','-0.30')
        self.assertIn(reverse,e.data['orders'])
        reentry=next(o for o in e.data['orders'] if o['kind']=='reentry')
        self.assertEqual(D(reentry['price']),D('4000.45'))
        e.fill('fill2',reentry['id'],'4000.50')
        self.assertEqual(len(e.data['orders']),1)
        self.assertEqual(D(e.data['orders'][0]['price']),D('3999.80'))

    def test_reverse_fills_first_cancels_stale_reentry(self):
        e=self.engine(); p=self.fill(e); rev=e.data['orders'][0]
        e.quote('4000.5','4000.7'); e.close('c',p['id'],'support_sl','0')
        e.fill('f2',rev['id'],'3999.3')
        self.assertEqual(len(e.data['orders']),1)
        self.assertEqual(e.data['orders'][0]['side'],'BUY')

    def test_manual_close_retains_trigger(self):
        e=self.engine(); p=self.fill(e); orders=e.data['orders'].copy()
        e.close('c',p['id'],'manual','12')
        self.assertEqual(e.data['orders'],orders)
        e.fill('f2',orders[0]['id'],orders[0]['price'])
        self.assertEqual(e.data['positions'][0]['side'],'SELL')

    def test_cancel_guard_and_replace_guard(self):
        e=self.engine(); p=self.fill(e)
        with self.assertRaises(ValueError): e.cancel_ranges()
        with self.assertRaises(ValueError): e.replace_ranges(ranges())
        e.close('c',p['id'],'manual','-1'); e.cancel_ranges()
        self.assertEqual(e.data['orders'],[])
        self.assertEqual(e.data['loss_history'][0]['loss'],'1')

    def test_independent_ranges_history_switch(self):
        e=self.engine(True); p=self.fill(e)
        e.close('c',p['id'],'initial_sl','-2')
        self.assertEqual(e._range('1')['loss'],'2')
        o=next(o for o in e.data['orders'] if o['range_id']=='2')
        e.fill('f2',o['id'],'3950')
        self.assertEqual(e._range('2')['loss'],'0')
        self.assertEqual(e._range('1')['loss'],'2')
        self.assertEqual(e.data['loss_history'],[])

    def test_duplicate_events_and_already_closed(self):
        e=self.engine(); oid=e.data['orders'][0]['id']
        e.fill('f',oid,'4000'); before=e.state(); e.fill('f',oid,'4000')
        self.assertEqual(before,e.state())
        p=e.data['positions'][0]; e.close('c',p['id'],'manual','-1')
        e.close('c',p['id'],'manual','-1'); self.assertEqual(e._range('1')['loss'],'1')
        with self.assertRaises(ValueError): e.close('c2',p['id'],'manual','-1')
        with self.assertRaises(ValueError): e.fill('f',oid,'4000.1')

    def test_close_cancel_one_side_only(self):
        e=self.engine(True); p=self.fill(e)
        e.close_cancel('cc','1','3')
        self.assertEqual([r['id'] for r in e.data['ranges']],['2'])
        self.assertTrue(all(o['range_id']=='2' for o in e.data['orders']))
        self.assertEqual(e.data['positions'],[])

    def test_nonfinite_invalid_input_rejected(self):
        e=self.engine(); before=e.state()
        for bad in ('NaN','Infinity','-1'):
            with self.assertRaises(ValueError): e.replace_ranges([dict(ranges()[0],price=bad)])
        self.assertEqual(before,e.state())
        with self.assertRaises(ValueError): e.quote(4000,3999)

    def test_two_open_ranges_are_independent(self):
        e=self.engine(True); self.fill(e)
        o=next(o for o in e.data['orders'] if o['range_id']=='2')
        e.fill('f2',o['id'],'3950')
        self.assertEqual(len(e.data['positions']),2)
        p=next(p for p in e.data['positions'] if p['range_id']=='1')
        e.close('c',p['id'],'initial_sl','-1')
        self.assertEqual(e._range('1')['full_sl_count'],1)
        self.assertEqual(e._range('2')['full_sl_count'],0)
        self.assertEqual(len(e.data['positions']),1)

    def test_profitable_close_does_not_erase_gross_loss(self):
        e=self.engine(); p=self.fill(e); e.close('c',p['id'],'initial_sl','-2')
        p=self.fill(e,'f2','3999.3'); e.close('c2',p['id'],'manual','3')
        self.assertEqual(e._range('1')['loss'],'2')
        self.assertEqual(e._range('1')['realized_net'],'1')
