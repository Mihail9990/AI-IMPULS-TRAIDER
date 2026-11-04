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
    def test_confirmed_mixed_example_preserves_manual_and_interpolates(self):
        values=['.50',None,'1.50',None,None,None];before=list(values)
        prices=list(map(D,self.calc(values,debt='10')))
        self.assertEqual(prices[:3],[D('4000.50'),D('4001.00'),D('4001.50')])
        self.assertEqual(prices[3]-prices[2],prices[4]-prices[3]);self.assertEqual(prices[4]-prices[3],prices[5]-prices[4])
        self.assertGreaterEqual(sum(p-D(4000) for p in prices),10)
        self.assertLess(sum(p-D(4000) for p in prices)-D('.01')*6,10)
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

    def test_mixed_mirror_and_unequal_actual_fills_volumes(self):
        entries=['4000','4000.02','4000.01','4000.04','4000.03','4000.05'];sizes=['.01','.02','.01','.03','.01','.02']
        for side in ('BUY','SELL'):
            sign=1 if side=='BUY' else -1
            prices=list(map(D,configured_ladder(self.b,self.spec,side,entries,sizes,'20',['.50',None,'1.50',None,None,None])))
            distances=[sign*(p-D(e)) for p,e in zip(prices,entries)]
            self.assertEqual(distances[:3],[D('.50'),D('1.00'),D('1.50')])
            self.assertEqual(distances[3]-distances[2],distances[4]-distances[3]);self.assertEqual(distances[4]-distances[3],distances[5]-distances[4])
            self.assertGreaterEqual(sum(self.b.profit(side,D(v),D(e),p) for e,v,p in zip(entries,sizes,prices)),20)
    def test_multiple_interpolation_segments(self):
        p=self.calc(['.10',None,'.50',None,None,'1.10'],debt='1')
        self.assertEqual([D(x)-D(4000) for x in p],list(map(D,['.10','.30','.50','.70','.90','1.10'])))
    def test_exact_interpolation_without_tail_cannot_be_stretched_for_debt(self):
        with self.assertRaisesRegex(ValueError,'свободного продолжения нет'):self.calc(['.10',None,'.30'],debt='10')
    def test_already_covered_prefix_smallest_positive_tail_step(self):
        self.assertEqual(self.calc(['.50',None,'1.50',None],debt='.1'),['4000.50','4001.00','4001.50','4001.51'])
    def test_quote_freeze_refuses_fixed_tp_without_moving_it(self):
        from dataclasses import replace
        spec=replace(self.spec,freeze_level=D('20'))
        with self.assertRaisesRegex(ValueError,'stop/freeze'):
            configured_ladder(self.b,spec,'BUY',['4000']*3,['.01']*3,'1',['.50',None,'1.50'],quote={'bid':'4000.40','ask':'4000.50'},modify=True)
    def test_positive_sell_tail_unreachable(self):
        with self.assertRaises(ValueError):configured_ladder(self.b,self.spec,'SELL',['1']*4,['.01']*4,'100',['.10',None,'.30',None])
    def test_modify_requires_quote(self):
        with self.assertRaises(ValueError):configured_ladder(self.b,self.spec,'BUY',['4000'],['.01'],'1',['1'],modify=True)

    def test_closed_mask_excludes_profit_and_quote_checks_preserves_indices(self):
        prices=configured_ladder(self.b,self.spec,'BUY',['4000']*3,['.01','.02','.03'],'2.50',['.20','.40',None],quote={'bid':'4000.25','ask':'4000.35'},modify=True,active_mask=[False,True,True])
        self.assertEqual(prices[:2],['4000.20','4000.40'])
        profit=sum(self.b.profit('BUY',D(v),D(4000),D(p)) for v,p in zip(['.02','.03'],prices[1:]))
        self.assertGreaterEqual(profit,D('2.50'))
        self.assertLess(profit-D('.03')*100*self.spec.tick_size,D('2.50'))
    def test_closed_mask_requires_boolean_slots_and_live_remainder(self):
        for mask in ([False,False],[True],[1,False]):
            with self.assertRaises(ValueError):configured_ladder(self.b,self.spec,'BUY',['4000']*2,['.01']*2,'1',['.20',None],active_mask=mask)
