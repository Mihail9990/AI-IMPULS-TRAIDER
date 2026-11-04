from decimal import Decimal as D
from dataclasses import replace
import unittest
from tradfi.manual_range import manual_close_range,validate_manual_stops
from tradfi.broker import Spec
from tradfi.simulator import ARTIFICIAL_SPEC

class ManualRange(unittest.TestCase):
    def setUp(self):self.spec=Spec.load(ARTIFICIAL_SPEC)
    def test_buy_confirmed_asymmetric_anchors(self):
        self.assertEqual(manual_close_range('BUY','3997','1','2'),{'BUY':'3998','SELL':'3995'})
    def test_sell_mirror(self):
        self.assertEqual(manual_close_range('SELL','4003','1','2'),{'BUY':'4005','SELL':'4002'})
    def test_settings_are_independent_and_explicit(self):
        self.assertEqual(manual_close_range('BUY','3997.60','.80','1.70'),{'BUY':'3998.40','SELL':'3995.90'})
    def test_no_implicit_last_exit_selection(self):
        with self.assertRaises((ValueError,TypeError)):manual_close_range('BUY',['4000','4001'],'1','2')
    def test_invalid_nonfinite_zero_negative(self):
        for value in ('NaN','Infinity','0','-1',True):
            for i in range(3):
                args=['3997','1','2'];args[i]=value
                with self.assertRaises((ValueError,TypeError)):manual_close_range('BUY',*args)
    def test_unknown_side_rejected(self):
        with self.assertRaises(ValueError):manual_close_range('BOTH','3997','1','2')
    def test_nonpositive_sell_never_repaired(self):
        with self.assertRaises(ValueError):manual_close_range('BUY','1','1','2')
        with self.assertRaises(ValueError):manual_close_range('SELL','1','1','2')
    def test_both_stops_validate_against_bid_ask(self):
        levels=manual_close_range('BUY','4000','1','2')
        self.assertEqual(validate_manual_stops(self.spec,levels,{'bid':'3999.9','ask':'4000.1'}),levels)
    def test_spread_crosses_buy_or_sell_no_market_or_side_drop(self):
        levels={'BUY':'4001','SELL':'3998'}
        for quote in ({'bid':'4000','ask':'4001'},{'bid':'3998','ask':'3999'},{'bid':'4000','ask':'3999'}):
            with self.assertRaises(ValueError):validate_manual_stops(self.spec,levels,quote)
        self.assertEqual(levels,{'BUY':'4001','SELL':'3998'})
    def test_tick_and_stops_not_rounded(self):
        with self.assertRaises(ValueError):validate_manual_stops(self.spec,{'BUY':'4001.005','SELL':'3998'},{'bid':'4000','ask':'4000.1'})
        spec=replace(self.spec,point=D('.01'),stops_level=D('150'))
        with self.assertRaises(ValueError):validate_manual_stops(spec,{'BUY':'4001','SELL':'3998'},{'bid':'4000','ask':'4000.1'})
    def test_full_pair_required(self):
        with self.assertRaises(ValueError):validate_manual_stops(self.spec,{'BUY':'4001'},{'bid':'4000','ask':'4000.1'})
