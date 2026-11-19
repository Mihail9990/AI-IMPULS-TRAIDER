import unittest
from types import SimpleNamespace as N
from recovery.closing_order_evidence import resolve_closing_order, EvidenceUnavailable

class Api:
    DEAL_ENTRY_OUT=1;DEAL_REASON_SL=4;DEAL_TYPE_BUY=0;DEAL_TYPE_SELL=1
    ORDER_TYPE_BUY=0;ORDER_TYPE_SELL=1;ORDER_REASON_SL=4;ORDER_STATE_FILLED=4
    def __init__(self):
        self.login=1;self.calls=[]
        self.rows=[N(ticket=99,position_id=700,symbol='XAUUSD.s',reason=4,type=1,state=4,sl=3999.3,price_open=3999.3)]
    def account_info(self):return N(login=self.login,server='demo')
    def terminal_info(self):return N(connected=True)
    def history_orders_get(self,**kwargs):self.calls.append(kwargs);return self.rows

class ClosingEvidenceTests(unittest.TestCase):
    def resolve(self,a,**updates):
        d=N(order=99,entry=1,reason=4,position_id=700,symbol='XAUUSD.s',type=1,price=3999.1)
        vars(d).update(updates)
        return resolve_closing_order(a,d,account=1,server='demo',symbol='XAUUSD.s',position_identifier=700,position_side='BUY')
    def test_exact_link_is_not_proof_of_stop_price(self):
        a=Api();r=self.resolve(a)
        self.assertEqual(a.calls,[{'ticket':99}])
        self.assertEqual(r['status'],'STOP_LEVEL_UNRESOLVED')
        self.assertIsNone(r['close_classification']);self.assertIsNone(r['stop_level'])
    def test_missing_ambiguous_history(self):
        for rows in (None,[],[Api().rows[0],Api().rows[0]]):
            a=Api();a.rows=rows
            with self.assertRaises(EvidenceUnavailable):self.resolve(a)
    def test_mismatched_order_evidence(self):
        for key,val in [('ticket',100),('position_id',701),('symbol','OTHER'),('reason',0),('type',0),('state',2)]:
            a=Api();setattr(a.rows[0],key,val)
            with self.subTest(field=key),self.assertRaises(EvidenceUnavailable):self.resolve(a)
    def test_mismatched_deal_rejected_before_read(self):
        a=Api()
        with self.assertRaises(EvidenceUnavailable):self.resolve(a,position_id=701)
        self.assertEqual(a.calls,[])
    def test_account_change_during_read(self):
        a=Api()
        def read(**kwargs):a.login=2;return a.rows
        a.history_orders_get=read
        with self.assertRaisesRegex(EvidenceUnavailable,'Account/server changed'):self.resolve(a)
