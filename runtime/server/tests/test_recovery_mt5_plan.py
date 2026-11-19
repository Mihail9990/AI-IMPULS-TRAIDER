import unittest
from recovery.mt5_plan import Symbol, Quote, pending, protect, cancel

class PlanTests(unittest.TestCase):
    def setUp(self):
        self.symbol=Symbol('XAUUSD.s','.01','.01',5,5,'.01','100','.01')
        self.order={'id':'order-1','side':'BUY','price':'4000','sl':'3999.30','volume':'.01'}
        self.position={'ticket':10,'identifier':11,'magic':123,'symbol':'XAUUSD.s',
                       'side':'BUY','entry':'4000','sl':'3999.3','tp':'4010','stop_stage':0}

    def test_pending_correct_fields(self):
        p=pending(self.order,self.symbol,Quote('3999','3999.2'),123)
        self.assertEqual(p['type'],'ORDER_TYPE_BUY_STOP');self.assertEqual(p['sl'],'3999.30')
        self.assertEqual(p['type_filling'],'ORDER_FILLING_RETURN')

    def test_sell_pending(self):
        p=pending(dict(self.order,side='SELL',price='3950',sl='3950.70'),self.symbol,Quote('3951','3951.2'),123)
        self.assertEqual(p['type'],'ORDER_TYPE_SELL_STOP')

    def test_crossed_or_too_close_rejected(self):
        for q in (Quote('4000','4000.2'), Quote('3999.9','3999.96')):
            with self.assertRaises(ValueError):pending(self.order,self.symbol,q,123)

    def test_no_silent_tick_or_volume_rounding(self):
        for patch in ({'price':'4000.001'}, {'volume':'.015'}, {'sl':'3999.999'}):
            with self.assertRaises(ValueError):pending(dict(self.order,**patch),self.symbol,Quote('3999','3999.2'),123)

    def test_protect_preserves_tp(self):
        p=protect(self.position,'3999.75',self.symbol,Quote('4000.25','4000.45'),expected_ticket=10,expected_identifier=11,expected_magic=123)
        self.assertEqual(p['tp'],'4010'); self.assertEqual(p['position'],10)

    def test_protect_wrong_identity(self):
        with self.assertRaises(ValueError):protect(self.position,'3999.75',self.symbol,Quote('4000.25','4000.45'),expected_ticket=10,expected_identifier=99,expected_magic=123)

    def test_no_support_regression(self):
        with self.assertRaises(ValueError):protect(dict(self.position,stop_stage=2,sl='4000'),'3999.75',self.symbol,Quote('4000.25','4000.45'),expected_ticket=10,expected_identifier=11,expected_magic=123)

    def test_cancel_owned_only(self):
        p=cancel({'ticket':10,'magic':123,'symbol':'XAUUSD.s'},expected_ticket=10,expected_magic=123,expected_symbol='XAUUSD.s')
        self.assertEqual(p['action'],'TRADE_ACTION_REMOVE')
        with self.assertRaises(ValueError):cancel({'ticket':10,'magic':999,'symbol':'XAUUSD.s'},expected_ticket=10,expected_magic=123,expected_symbol='XAUUSD.s')
