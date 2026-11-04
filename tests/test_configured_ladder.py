from decimal import Decimal as D
import unittest
from tradfi.basket_math import configured_ladder
from tradfi.broker import Spec
from tradfi.interactive_simulator import InteractiveSimulator
from tradfi.simulator import ARTIFICIAL_SPEC

class ConfiguredLadder(unittest.TestCase):
    def setUp(self):
        self.b=InteractiveSimulator(dict(ARTIFICIAL_SPEC,volume_min='.01',volume_step='.01',contract_size='100'))
        self.spec=Spec.load(self.b.snapshot()['spec'])
    def calc(self,distances,debt='3',side='BUY'):
        return configured_ladder(self.b,self.spec,side,'4000',['4000']*len(distances),['.01']*len(distances),debt,distances)
    def test_first_mandatory(self):
        with self.assertRaises(ValueError):self.calc([None,None])
    def test_only_first_equal_spacing_minimal_cover(self):
        p=list(map(D,self.calc(['.20',None,None,None,None,None])))
        steps=[b-a for a,b in zip(p,p[1:])]
        self.assertEqual(len(set(steps)),1);self.assertEqual(p[0],D('4000.20'))
        self.assertGreaterEqual(sum((x-D(4000) for x in p)),D(3))
        self.assertLess(sum((x-D(4000)-D(i)*self.spec.tick_size for i,x in enumerate(p))),D(3))
    def test_sell_mirror(self):
        buy=list(map(D,self.calc(['.20',None,None])))
        sell=list(map(D,self.calc(['.20',None,None],side='SELL')))
        self.assertEqual([x-D(4000) for x in buy],[D(4000)-x for x in sell])
    def test_explicit_slots_never_moved(self):
        p=self.calc(['.20',None,'.80',None,None,None])
        self.assertEqual(p[0],'4000.20');self.assertEqual(p[2],'4000.80')
        self.assertTrue(all(D(b)>D(a) for a,b in zip(p,p[1:])))
    def test_incompatible_constraints_rejected(self):
        with self.assertRaises(ValueError):self.calc(['.20',None,'.22',None,'.23'])
    def test_fixed_all_insufficient_rejected(self):
        with self.assertRaises(ValueError):self.calc(['.10','.20'])
    def test_fixed_all_sufficient_exact(self):
        self.assertEqual(self.calc(['1','2']),'4001 4002'.split())
    def test_extra_cost_debt_changes_only_blank_slots(self):
        a=self.calc(['.20',None,None]);b=self.calc(['.20',None,None],debt='4')
        self.assertEqual(a[0],b[0]);self.assertGreater(D(b[-1]),D(a[-1]))
    def test_tick_precision_not_rounded(self):
        with self.assertRaises(ValueError):self.calc(['.205',None])
    def test_unordered_fixed_rejected(self):
        with self.assertRaises(ValueError):self.calc(['.50','.25'])
