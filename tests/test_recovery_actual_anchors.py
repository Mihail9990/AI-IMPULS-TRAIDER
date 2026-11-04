from copy import deepcopy
from decimal import Decimal as D
import unittest
from tradfi.basket_math import confirmed_stop_anchors as anchors,recovery_range_after_sl as levels

class ActualAnchors(unittest.TestCase):
    def setUp(self):
        self.t=[];self.d=[];self.p={}
        for i,(v,price) in enumerate([('.01','4000'),('.02','4000.3')]):
            pid=f'p{i}';cid=f'c{i}'
            self.t.append(dict(id=pid,command_id=cid,side='BUY',size=v,entry='9999',sl='9998'))
            self.d.extend([dict(id=f'in{i}',position_id=pid,command_id=cid,entry='IN',side='BUY',volume=v,price=price),dict(id=f'out{i}',position_id=pid,entry='OUT',reason='SL',volume=v,price='3996.7')])
            self.p[cid]={'status':'CLOSED_EVIDENCE'}
    def calc(self):return anchors(self.t,self.d,self.p)
    def test_weighted_actual_entry_and_gap_exit(self):
        a=self.calc();self.assertEqual(D(a['entry']),D('4000.2'));self.assertEqual(a['sl_exit'],'3996.7')
        self.assertEqual(levels(a['side'],a['entry'],a['sl_exit'],'1',initial=True),{'BUY':'4000.2','SELL':'3995.7'})
    def test_mirrored_sell(self):
        for t in self.t:t['side']='SELL'
        for d in self.d:
            if d['entry']=='IN':d['side']='SELL'
            else:d['price']='4002.8'
        a=self.calc();self.assertEqual(levels(a['side'],a['entry'],a['sl_exit'],'1',initial=False),{'BUY':'4003.8','SELL':'3999.2'})
    def test_distinct_exit_prices_require_clarification(self):
        self.d[-1]['price']='3996.6'
        with self.assertRaisesRegex(ValueError,'разным ценам'):self.calc()
    def test_mixed_tp_sl_requires_clarification(self):
        self.d[-1]['reason']='TP'
        with self.assertRaisesRegex(ValueError,'смешанных'):self.calc()
    def test_unknown_evidence(self):
        for status in ('UNKNOWN','FILLED_OPEN_EVIDENCE','PARTIAL_FILL','PARTIAL_CLOSE'):
            self.p['c1']['status']=status
            with self.subTest(status=status),self.assertRaises(ValueError):self.calc()
    def test_missing_proof(self):
        self.p.pop('c1')
        with self.assertRaises(ValueError):self.calc()
    def test_missing_close(self):
        self.d.pop()
        with self.assertRaises(ValueError):self.calc()
    def test_incomplete_close(self):
        self.d[-1]['volume']='.01'
        with self.assertRaises(ValueError):self.calc()
    def test_duplicate_deal(self):
        self.d.append(deepcopy(self.d[-1]))
        with self.assertRaises(ValueError):self.calc()
    def test_mismatched_command(self):
        self.d[0]['command_id']='unowned'
        with self.assertRaises(ValueError):self.calc()
    def test_zero_nonfinite(self):
        for field in ('volume','price'):
            for value in ('0','-1','NaN','Infinity'):
                rows=deepcopy(self.d);rows[0][field]=value
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):anchors(self.t,rows,self.p)
    def test_split_fill_same_ticket_is_weighted(self):
        self.d[2]['volume']='.01';extra=deepcopy(self.d[2]);extra.update(id='in-extra',price='4000.6');self.d.append(extra)
        self.assertEqual(D(self.calc()['entry']),D('4000.3'))
    def test_same_price_split_close_complete(self):
        self.d[-1]['volume']='.01';extra=deepcopy(self.d[-1]);extra['id']='out-extra';self.d.append(extra)
        self.assertEqual(D(self.calc()['volume']),D('.03'))
    def test_mixed_sides_rejected(self):
        self.t[-1]['side']='SELL'
        with self.assertRaises(ValueError):self.calc()
    def test_unrelated_position_ignored(self):
        self.d.append(dict(id='unrelated',position_id='other'))
        self.assertEqual(D(self.calc()['entry']),D('4000.2'))
    def test_restart_deterministic_and_input_unchanged(self):
        import json
        before=json.dumps([self.t,self.d,self.p],sort_keys=True)
        a=self.calc();t,d,p=json.loads(before)
        self.assertEqual(a,anchors(t,d,p));self.assertEqual(before,json.dumps([self.t,self.d,self.p],sort_keys=True))
