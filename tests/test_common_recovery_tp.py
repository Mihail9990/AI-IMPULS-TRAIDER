from decimal import Decimal as D
import unittest
from tradfi.basket_math import common_recovery_tp as tp
from tradfi.broker import Spec
from tradfi.interactive_simulator import InteractiveSimulator
from tradfi.simulator import ARTIFICIAL_SPEC

class CommonTP(unittest.TestCase):
    def setUp(self):
        self.b=InteractiveSimulator(dict(ARTIFICIAL_SPEC,volume_min='.01',volume_step='.01',contract_size='100'))
        self.spec=Spec.load(self.b.snapshot()['spec'])
    def profit(self,side,es,vs,p):return sum((self.b.profit(side,D(v),D(e),D(p)) for e,v in zip(es,vs)),D(0))
    def test_buy_sell_debt_only(self):
        for side,expected in [('BUY','4000.50'),('SELL','3999.50')]:
            self.assertEqual(tp(self.b,self.spec,side,['4000']*6,['.01']*6,'3'),expected)
    def test_actual_nonuniform_fills_and_volumes(self):
        es=['4000','4000.20'];vs=['.01','.02']
        for side in ('BUY','SELL'):
            p=D(tp(self.b,self.spec,side,es,vs,'.37'))
            self.assertGreaterEqual(self.profit(side,es,vs,p),D('.37'))
            previous=p-self.spec.tick_size*(1 if side=='BUY' else -1)
            self.assertLess(self.profit(side,es,vs,previous),D('.37'))
    def test_realized_cost_in_debt(self):
        p=tp(self.b,self.spec,'BUY',['4000']*6,['.01']*6,'3.07')
        self.assertEqual(p,'4000.52')
        self.assertEqual(self.profit('BUY',['4000']*6,['.01']*6,p),D('3.12'))
    def test_remaining_volume_only(self):
        self.assertEqual(tp(self.b,self.spec,'SELL',['4000']*2,['.01']*2,'3'),'3998.50')
    def test_unreachable_sell_price(self):
        with self.assertRaises(ValueError):tp(self.b,self.spec,'SELL',['1'],['.01'],'100')
    def test_invalid_values(self):
        for bad in ('NaN','Infinity','-1'):
            with self.assertRaises(ValueError):tp(self.b,self.spec,'BUY',['4000'],['.01'],bad)
        with self.assertRaises(ValueError):tp(self.b,self.spec,'BUY',['4000'],[],'1')
    def test_unknown_profit_rejected(self):
        self.b.profit=lambda *args:None
        with self.assertRaises(ValueError):tp(self.b,self.spec,'BUY',['4000'],['.01'],'1')
