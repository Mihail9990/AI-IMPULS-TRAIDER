import unittest
from decimal import Decimal as D
from manual_policy import plan_adoption

class ManualAdoptionPolicy(unittest.TestCase):
    def plan(self, side, sl=None, *, tp="4010", distance="0.70"):
        return plan_adoption(side=side, entry=D("4000"), existing_sl=None if sl is None else D(sl),
                             existing_tp=None if tp is None else D(tp), distance=D(distance), tick_size=D(".01"))

    def test_keep_original_buy_sl_even_if_different_distance(self):
        p=self.plan("BUY", "3998.53", distance="0.80")
        self.assertIsNone(p.position_sl_write)
        self.assertEqual((p.reverse_side, p.reverse_price, p.reverse_sl),
                         ("SELL", D("3998.53"), D("3999.33")))
        self.assertEqual(p.preserved_tp,D("4010"))

    def test_keep_original_sell_sl(self):
        p=self.plan("SELL", "4000.24", tp="3992")
        self.assertIsNone(p.position_sl_write)
        self.assertEqual((p.reverse_side,p.reverse_price,p.reverse_sl),
                         ("BUY",D("4000.24"),D("3999.54")))
        self.assertEqual(p.preserved_tp,D("3992"))

    def test_breakeven_and_profit_stop_not_replaced(self):
        for side, sl in (("BUY","4000"),("BUY","4001"),("SELL","3999")):
            with self.subTest(side=side, sl=sl):
                p=self.plan(side,sl)
                self.assertIsNone(p.position_sl_write)
                self.assertEqual(p.reverse_price,D(sl))

    def test_no_stop_uses_actual_entry_and_configured_distance(self):
        for side,rev,level in (("BUY","SELL","3999.30"),("SELL","BUY","4000.70")):
            with self.subTest(side=side):
                p=self.plan(side,None, tp=None)
                self.assertIsNone(p.observed_sl)
                self.assertEqual(p.position_sl_write,D(level))
                self.assertEqual((p.reverse_side,p.reverse_price,p.reverse_sl),(rev,D(level),D("4000")))
                self.assertIsNone(p.preserved_tp)

    def test_grid_rejection_does_not_shift_stop(self):
        for sl in ("3998.531", "NaN", "0"):
            with self.subTest(sl=sl), self.assertRaises(ValueError):
                self.plan("BUY", sl)
