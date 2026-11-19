import unittest
from decimal import Decimal, localcontext
from recovery.price_evidence import canonical_exported_price as canonical

class PriceEvidenceTests(unittest.TestCase):
    def test_exact_grid_and_real_binary_tail(self):
        self.assertEqual(canonical('3999.3','.01'),Decimal('3999.3'))
        self.assertEqual(canonical('3999.3000000000001819','.01'),Decimal('3999.3'))
        self.assertEqual(canonical('4000.6999999999998181','.01'),Decimal('4000.7'))
        self.assertEqual(canonical(format(float('1.23456'),'.16f'),'.00001'),Decimal('1.23456'))
    def test_no_broad_tolerance_or_arbitrary_rounding(self):
        for v in ['3999.3000000000001000','3999.30001','3999.305','3999.2999999999999999']:
            with self.subTest(v=v),self.assertRaises(ValueError):canonical(v,'.01')
    def test_bad_and_ambiguous_metadata(self):
        for v,t in [('NaN','.01'),('Infinity','.01'),('0','.01'),('1','0'),
                    ('1','-1'),('1','NaN'),('4000','.000000000000001'),('1e100','.01')]:
            with self.subTest(v=v,t=t),self.assertRaises(ValueError):canonical(v,t)
    def test_exact_rendered_values_on_several_tick_grids(self):
        for tick,prices in [('.01',['3999.3','4000.7']),('.00001',['1.23456','0.98765']),
                            ('.25',['100.25','999.75']),('.1',['0.1','10.3'])]:
            for p in prices:
                self.assertEqual(canonical(format(float(p),'.16f'),tick),Decimal(p))
    def test_caller_decimal_context_cannot_relax_validation(self):
        with localcontext() as ctx:
            ctx.prec=4
            self.assertEqual(canonical('3999.3000000000001819','.01'),Decimal('3999.3'))
            with self.assertRaises(ValueError):canonical('3999.3000000000001000','.01')

    def test_fixed16_collision_rejected_even_for_exact_grid_input(self):
        with self.assertRaisesRegex(ValueError,'fixed16'):
            canonical('0.0000000000000001','0.000000000000000001')
