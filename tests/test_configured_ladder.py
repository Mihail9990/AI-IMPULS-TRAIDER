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
        return configured_ladder(self.b,self.spec,side,['4000']*len(distances),['.01']*len(distances),debt,distances)
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
    def test_mixed_grid_explicitly_blocked_without_mutating_inputs(self):
        values=['.20',None,'.80',None,None,None];before=list(values)
        with self.assertRaisesRegex(ValueError,'MIXED_GRID_UNCONFIRMED'):self.calc(values)
        self.assertEqual(values,before)
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

    def test_each_fixed_tp_uses_own_actual_entry(self):
        for side,expected in [('BUY',['4000.50','4001.20']),('SELL',['3999.50','3999.20'])]:
            self.assertEqual(configured_ladder(self.b,self.spec,side,['4000','4000.20'],['.01','.02'],'1',['.50','1.00']),expected)
    def test_first_only_own_entries_uniform_distance_increment(self):
        es=['4000','4000.20','4000.10'];vs=['.01','.02','.01']
        for side in ('BUY','SELL'):
            ps=list(map(D,configured_ladder(self.b,self.spec,side,es,vs,'3',['.50',None,None])))
            sign=1 if side=='BUY' else -1
            ds=[sign*(p-D(e)) for p,e in zip(ps,es)]
            self.assertEqual(ds[0],D('.50'));self.assertEqual(ds[1]-ds[0],ds[2]-ds[1])
            self.assertTrue(all(sign*(b-a)>0 for a,b in zip(ps,ps[1:])))
            profit=sum((self.b.profit(side,D(v),D(e),p) for e,v,p in zip(es,vs,ps)),D(0))
            self.assertGreaterEqual(profit,3)
    def test_manual_actual_price_order_conflict_never_repaired(self):
        with self.assertRaises(ValueError):configured_ladder(self.b,self.spec,'BUY',['4002','4000'],['.01','.01'],'1',['.50','1.00'])
    def test_off_tick_actual_average_is_not_silently_rounded(self):
        with self.assertRaises(ValueError):configured_ladder(self.b,self.spec,'BUY',['4000.005','4000'],['.01','.01'],'1',['.50',None])
