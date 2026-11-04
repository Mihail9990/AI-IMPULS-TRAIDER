"""Confirmed v2 level formulas only. No trading-controller activation yet."""
import unittest
from tradfi.basket_math import recovery_range_after_sl as levels

class RecoveryRangeV2(unittest.TestCase):
    def test_confirmed_initial_buy(self):
        self.assertEqual(levels('BUY','4000','3997','1',initial=True),{'BUY':'4000','SELL':'3996'})
    def test_mirrored_initial_sell(self):
        self.assertEqual(levels('SELL','4000','4003','1',initial=True),{'BUY':'4004','SELL':'4000'})
    def test_next_sell_stop(self):
        self.assertEqual(levels('SELL','3996','3997.5','1',initial=False),{'BUY':'3998.5','SELL':'3996.5'})
    def test_next_buy_stop(self):
        self.assertEqual(levels('BUY','4000','3998.5','1',initial=False),{'BUY':'3999.5','SELL':'3997.5'})
    def test_repeated_sell_recomputes_instead_of_freezing(self):
        first=levels('SELL','3996','3997.5','1',initial=False)
        second=levels('SELL',first['SELL'],'3996.5','1',initial=False)
        self.assertEqual(second,{'BUY':'3997.5','SELL':'3995.5'})
        self.assertNotEqual(first,second)
    def test_repeated_buy_recomputes_instead_of_freezing(self):
        first=levels('BUY','4000','3998.5','1',initial=False)
        second=levels('BUY',first['BUY'],'3999.5','1',initial=False)
        self.assertEqual(second,{'BUY':'4000.5','SELL':'3998.5'})
    def test_side_change_uses_latest_stopped_side(self):
        self.assertEqual(levels('BUY','3998.5','3997.0','1',initial=False),{'BUY':'3998.0','SELL':'3996.0'})
    def test_custom_distance_and_explicit_sl(self):
        self.assertEqual(levels('SELL','3996.25','3998.75','.50',initial=False),{'BUY':'3999.25','SELL':'3998.25'})
    def test_nonfinite_or_nonpositive_rejected(self):
        for bad in ('nan','inf','-inf','0','-1','bad'):
            for index in range(3):
                values=['4000','3997','1'];values[index]=bad
                with self.subTest(bad=bad,index=index),self.assertRaises(ValueError):levels('BUY',*values,initial=True)
    def test_invalid_side_and_phase_rejected(self):
        for side,initial in [('LONG',True),('BUY',1),('SELL',None)]:
            with self.assertRaises(ValueError):levels(side,'4000','3997','1',initial=initial)
    def test_inverted_range_rejected_not_rounded(self):
        with self.assertRaises(ValueError):levels('BUY','4000','4005','1',initial=True)
        with self.assertRaises(ValueError):levels('SELL','.5','.5','1',initial=False)
