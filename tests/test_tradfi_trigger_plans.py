import unittest
from tradfi.broker import Spec
from tradfi.simulator import ARTIFICIAL_SPEC
from tradfi.strategies.levels import user_triggers,recovery_triggers


class TriggerPlansTest(unittest.TestCase):
    def setUp(self): self.spec=Spec.load(ARTIFICIAL_SPEC)

    def test_ranges_use_ask_for_buy_bid_for_sell_not_mid_or_candle(self):
        quote={'bid':'4133.00','ask':'4133.10'}
        plan=user_triggers(self.spec,quote,ranges={'BUY':'3','SELL':'3'})
        self.assertEqual(plan.prices(),{'BUY':'4136.10','SELL':'4130.00'})
        quote['ask']='9000'
        self.assertEqual(plan.ask,'4133.10')

    def test_single_or_double_exact_prices_are_preserved(self):
        quote={'bid':'4133','ask':'4133.10'}
        self.assertEqual(user_triggers(self.spec,quote,prices={'SELL':'4130'}).prices(),{'SELL':'4130'})
        self.assertEqual(user_triggers(self.spec,quote,prices={'BUY':'4136','SELL':'4130'}).prices(),{'BUY':'4136','SELL':'4130'})

    def test_no_silent_rounding_or_partial_range(self):
        quote={'bid':'4133','ask':'4133.10'}
        for kwargs in ({'ranges':{'BUY':'3'}},{'prices':{'BUY':'4136.001'}},{'prices':{}},
                       {'ranges':{'BUY':'-1','SELL':'3'}},{'prices':{'BUY':'4000','SELL':'4200'}}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):user_triggers(self.spec,quote,**kwargs)

    def test_recovery_example_and_sell_mirror(self):
        args=dict(after_sl='3',continuation='1',opposite='3')
        self.assertEqual(recovery_triggers(self.spec,'4136','SELL','SL',**args),{'BUY':'4139','SELL':'4133'})
        self.assertEqual(recovery_triggers(self.spec,'4140','BUY','TP',**args),{'BUY':'4141','SELL':'4137'})
        self.assertEqual(recovery_triggers(self.spec,'4132','SELL','TP',**args),{'BUY':'4135','SELL':'4131'})
