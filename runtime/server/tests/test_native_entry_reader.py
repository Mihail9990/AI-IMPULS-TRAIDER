import tempfile
import unittest
from types import SimpleNamespace as N
from copy import deepcopy
from recovery.native_entry_reader import NativeEntryReader
from recovery.native_executor_session import NativeExecutorSession
from recovery.native_preflight import TerminalBinding
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_recovery_control import FillingFake, RANGES


class EntryFake(FillingFake):
    DEAL_TYPE_BUY=0; DEAL_TYPE_SELL=1; DEAL_ENTRY_IN=0; DEAL_ENTRY_OUT=1
    POSITION_TYPE_BUY=0; POSITION_TYPE_SELL=1
    def __init__(self):
        super().__init__(); self.entry_deals=[]; self.exit_deals=[]; self.hist=[]; self.switch_history=False
    def history_orders_get(self,*args,**kwargs):
        return [o for o in self.hist if not kwargs or o.ticket==kwargs.get('ticket')]
    def history_deals_get(self,*args,**kwargs):
        if self.switch_history:self.login=2
        if 'ticket' in kwargs:return [d for d in self.entry_deals if d.order==kwargs['ticket']]
        return self.entry_deals+self.exit_deals
    def fill(self,volume=.01):
        o=self.orders[0];o.position_id=88;o.volume_current=round(.01-volume,8)
        self.entry_deals=[N(ticket=101,order=51,position_id=88,symbol='XAUUSD.s',magic=123,
             entry=0,type=0,time_msc=100,volume=volume,price=4000.1,
             profit=0,commission=-.01,swap=0,fee=0,reason=3)]
        self.positions=[N(ticket=77,identifier=88,magic=123,symbol='XAUUSD.s',type=0,
                          price_open=4000.1,volume=volume,sl=3999.3,tp=0)]
        if volume==.01:
            o.state=self.ORDER_STATE_FILLED;self.hist=[o];self.orders=[]


class NativeEntryReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.a=EntryFake()
        self.s=NativeExecutorSession(self.a,TerminalBinding(1,'demo','XAUUSD.s',frozenset({123})),database=self.tmp.name+'/state.db')
        self.s.__enter__();self.addCleanup(self.s.close)
        c=NativeRecoveryControl(self.s,magic=123);c.stage_initial_ranges(RANGES)
        self.item=c.submit_initial('1')['data']['orders'][0]
        self.reader=NativeEntryReader(self.s)
    def read(self):return self.reader.read(self.item,magic=123)
    def test_full_fill_and_duplicate_normalization(self):
        self.a.fill();self.a.entry_deals.append(deepcopy(self.a.entry_deals[0]))
        r=self.read()
        self.assertEqual(r['filled_volume'],'0.01');self.assertEqual(r['average_price'],'4000.1')
        self.assertEqual(r['position']['ticket'],77);self.assertEqual(r['position_identifier'],88)
        self.assertTrue(r['terminal_order']);self.assertEqual(len(r['deals']),1)
        self.assertEqual(len(self.a.sent),1)
    def test_partial_live_and_cancelled_remainder(self):
        self.a.fill(.004);r=self.read()
        self.assertFalse(r['terminal_order']);self.assertEqual(r['pending_order']['volume'],'0.006')
        o=self.a.orders.pop();o.state=self.a.ORDER_STATE_CANCELED;self.a.hist=[o]
        r=self.read();self.assertTrue(r['terminal_order']);self.assertEqual(r['filled_volume'],'0.004')
    def test_closed_before_protection_requires_exit_evidence(self):
        self.a.fill();self.a.positions=[]
        with self.assertRaises(ValueError):self.read()
        d=deepcopy(self.a.entry_deals[0]);d.ticket=102;d.order=52;d.entry=1;d.type=1;d.magic=0
        self.a.exit_deals=[d]
        r=self.read();self.assertIsNone(r['position']);self.assertEqual(len(r['exit_deals']),1)
    def test_missing_order_or_history_fails(self):
        self.a.fill();self.a.hist=[]
        with self.assertRaises(ValueError):self.read()
    def test_account_switch_and_wrong_scope(self):
        self.a.fill();self.a.switch_history=True
        with self.assertRaises(ValueError):self.read()
        self.a.switch_history=False;self.a.login=1;self.a.entry_deals[0].magic=456
        with self.assertRaises(ValueError):self.read()
    def test_conflicting_deal_and_late_volume_fails(self):
        self.a.fill(.004)
        d=deepcopy(self.a.entry_deals[0]);d.price=4001;self.a.entry_deals.append(d)
        with self.assertRaises(ValueError):self.read()
        self.a.entry_deals.pop();self.a.orders[0].volume_current=.005
        with self.assertRaises(ValueError):self.read()

    def test_invalid_position_identity_and_prices_rejected(self):
        self.a.fill()
        for field,value in [('ticket',0),('ticket',True),('price_open',float('nan')),
                            ('price_open',0),('sl',float('nan')),('sl',-1),
                            ('tp',float('inf')),('tp',-1)]:
            with self.subTest(field=field,value=value):
                old=getattr(self.a.positions[0],field)
                setattr(self.a.positions[0],field,value)
                with self.assertRaises(ValueError):self.read()
                setattr(self.a.positions[0],field,old)
        self.assertEqual(len(self.a.sent),1)

    def test_multiple_fills_weighted_entry(self):
        self.a.fill()
        first=self.a.entry_deals[0];first.volume=.004;first.price=4000
        second=deepcopy(first);second.ticket=102;second.volume=.006;second.price=4001
        self.a.entry_deals.append(second);self.a.positions[0].price_open=4000.6
        r=self.read()
        self.assertEqual(r['filled_volume'],'0.010')
        self.assertEqual(r['average_price'],'4000.6')
        self.assertEqual(len(r['deals']),2)

    def test_sell_fill_direction(self):
        # A separate durable session verifies a SELL mapping rather than mutating
        # a BUY receipt into evidence for a different side.
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake()
            with NativeExecutorSession(a,TerminalBinding(1,'demo','XAUUSD.s',frozenset({123})),database=d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123)
                c.stage_initial_ranges([{'id':'1','side':'SELL','price':'3990','volume':'.01'}])
                item=c.submit_initial('1')['data']['orders'][0]
                a.fill();a.entry_deals[0].type=a.DEAL_TYPE_SELL
                a.entry_deals[0].price=3989.9;a.positions[0].type=a.POSITION_TYPE_SELL
                a.positions[0].price_open=3989.9;a.positions[0].sl=3990.7
                r=NativeEntryReader(s).read(item,magic=123)
                self.assertEqual(r['position']['side'],'SELL')
                self.assertEqual(r['average_price'],'3989.9')
                a.entry_deals[0].type=a.DEAL_TYPE_BUY
                with self.assertRaises(ValueError):NativeEntryReader(s).read(item,magic=123)

    def receipt_item(self, receipt=None, state='UNKNOWN'):
        self.s.executor._update(self.item['key'],state,receipt=receipt)
        return self.item

    def test_partial_receipt_evidence_keeps_default_reader_strict(self):
        self.a.fill(.004)
        item=self.receipt_item({'order':51,'status':'unknown'})
        with self.assertRaises(ValueError):self.read()
        r=self.reader.read_receipt(item,magic=123)
        self.assertEqual(r['filled_volume'],'0.004')
        self.assertEqual(self.s.executor.read(item['key'])['state'],'UNKNOWN')
        self.assertEqual(len(self.a.sent),1)
        self.s.executor._update(item['key'],'VERIFIED',evidence={'kind':'partial_entry_verified','order':51})
        self.assertEqual(self.read()['filled_volume'],'0.004')

    def test_receipt_rejects_missing_wrong_or_terminal_receipt(self):
        self.a.fill(.004)
        for receipt,state in [(None,'UNKNOWN'),({'order':0},'UNKNOWN'),({'order':True},'UNKNOWN'),
                              ({'order':52},'UNKNOWN'),({'order':51},'REJECTED')]:
            with self.subTest(receipt=receipt,state=state):
                item=self.receipt_item(receipt,state)
                with self.assertRaises(ValueError):self.reader.read_receipt(item,magic=123)

    def test_receipt_requires_exact_original_order_fields(self):
        self.a.fill(.004);item=self.receipt_item({'order':51})
        for field,value in [('price_open',4001),('sl',3998),('tp',4002),
                            ('type',self.a.ORDER_TYPE_SELL_STOP),('volume_initial',.02)]:
            with self.subTest(field=field):
                order=self.a.orders[0];old=getattr(order,field);setattr(order,field,value)
                with self.assertRaises(ValueError):self.reader.read_receipt(item,magic=123)
                setattr(order,field,old)
        changed=deepcopy(item);changed['request']['price']='4001'
        with self.assertRaises(ValueError):self.reader.read_receipt(changed,magic=123)
        changed=deepcopy(item);changed['intent']['side']='SELL'
        with self.assertRaises(ValueError):self.reader.read_receipt(changed,magic=123)
        changed=deepcopy(item);changed['intent']['volume']='.02'
        with self.assertRaises(ValueError):self.reader.read_receipt(changed,magic=123)

    def test_receipt_requires_nonzero_strict_partial(self):
        item=self.receipt_item({'order':51})
        with self.assertRaises(ValueError):self.reader.read_receipt(item,magic=123)
        self.a.fill()
        with self.assertRaises(ValueError):self.reader.read_receipt(item,magic=123)

    def test_partial_at_initial_submit_uses_actual_unknown_receipt(self):
        class PartialAtSubmitFake(EntryFake):
            def order_send(self, request):
                result=super().order_send(request)
                self.fill(.004)
                result.retcode=self.TRADE_RETCODE_DONE_PARTIAL
                return result
        with tempfile.TemporaryDirectory() as d:
            a=PartialAtSubmitFake()
            with NativeExecutorSession(a,TerminalBinding(1,'demo','XAUUSD.s',frozenset({123})),database=d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
                item=c.submit_initial('1')['data']['orders'][0]
                row=s.executor.read(item['key'])
                self.assertEqual(row['state'],'UNKNOWN')
                self.assertEqual(row['receipt']['order'],51)
                item['broker_order']=row['receipt']['order']
                r=NativeEntryReader(s).read_receipt(item,magic=123)
                self.assertEqual(r['filled_volume'],'0.004')
                self.assertEqual(len(a.sent),1)
