import unittest
from decimal import Decimal as D
from recovery.engine import Engine

class DistanceTests(unittest.TestCase):
    def make(self,side='BUY'):
        e=Engine();e.replace_ranges([{'id':'1','price':'4000','side':side,'volume':'.01'}],'1.20','.90');return e
    def test_independent_buy_distances(self):
        e=self.make();o=e.data['orders'][0];self.assertEqual(D(o['sl']),D('3998.8'))
        e.fill('f',o['id'],'4000.1');p=e.data['positions'][0]
        self.assertEqual(D(p['sl']),D('3998.9'))
        e.quote('4000.35','4000.55');self.assertEqual(D(p['sl']),D('3999.85'))
        e.close('c',p['id'],'support_sl','-1')
        r=next(o for o in e.data['orders'] if o['kind']=='reentry')
        self.assertEqual(D(r['price']),D('4000.75'));self.assertEqual(D(r['sl']),D('3999.55'))
    def test_sell_distances(self):
        e=self.make('SELL');o=e.data['orders'][0];e.fill('f',o['id'],'4000')
        p=e.data['positions'][0];self.assertEqual(D(p['sl']),D('4001.2'))
        e.quote('3999.3','3999.5');e.close('c',p['id'],'support_sl','0')
        r=next(o for o in e.data['orders'] if o['kind']=='reentry')
        self.assertEqual(D(r['price']),D('3999.1'));self.assertEqual(D(r['sl']),D('4000.3'))
    def test_invalid_distance_does_not_change_state(self):
        e=self.make();before=e.state()
        for initial,reentry in ((0,1),(1,-1),('NaN',1),(1,'Infinity')):
            with self.assertRaises(ValueError):e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}],initial,reentry)
        self.assertEqual(before,e.state())
    def test_minimum_initial_sl_inclusive_and_reentry_independent(self):
        for initial in ('.60','.600','.61'):
            e=Engine();e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}],initial,'.01')
            self.assertEqual(D(e.data['settings']['reentry_distance']),D('.01'))
        for initial in ('.599999','.59','.25','.10','0','-1'):
            e=self.make();before=e.state()
            with self.assertRaisesRegex(ValueError,'at least 0.60'):
                e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}],initial,'.01')
            self.assertEqual(e.state(),before)

    def test_legacy_open_position_not_rewritten(self):
        e=self.make();o=e.data['orders'][0];e.fill('f',o['id'],'4000')
        e.data['settings']['initial_sl_distance']='.25'
        e.data['ranges'][0]['initial_sl_distance']='.25'
        before=e.state()
        for operation in (lambda:e.quote('4000.5','4000.7'),
                          lambda:e.close('c',e.data['positions'][0]['id'],'initial_sl','-1')):
            with self.assertRaisesRegex(ValueError,'at least 0.60'):operation()
            self.assertEqual(e.state(),before)

    def test_legacy_empty_ranges_can_be_explicitly_replaced(self):
        e=self.make();e.data['settings']['initial_sl_distance']='.25'
        e.data['ranges'][0]['initial_sl_distance']='.25'
        with self.assertRaises(ValueError):e.validate_distance_policy()
        e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}],'.60','.10')
        e.validate_distance_policy()
