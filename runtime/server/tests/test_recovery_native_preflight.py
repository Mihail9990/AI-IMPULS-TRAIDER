import unittest
from types import SimpleNamespace as N
from copy import deepcopy
from recovery.native_preflight import NativeMT5Preflight, TerminalBinding
from test_recovery_mt5_transport import FakeMT5, REQUEST, EXPECTED


class NativeFake(FakeMT5):
    _RECOVERY_TEST_DOUBLE = False
    SYMBOL_TRADE_MODE_DISABLED=0;SYMBOL_TRADE_MODE_LONGONLY=1;SYMBOL_TRADE_MODE_SHORTONLY=2
    SYMBOL_TRADE_MODE_CLOSEONLY=3;SYMBOL_TRADE_MODE_FULL=4
    SYMBOL_ORDER_MARKET=1;SYMBOL_ORDER_STOP=4;SYMBOL_ORDER_SL=16
    TRADE_ACTION_DEAL=1;ORDER_TYPE_BUY=0;ORDER_TYPE_SELL=1
    ORDER_FILLING_FOK=0;ORDER_FILLING_IOC=1
    def __init__(self):
        super().__init__()
        self.mode=4;self.flags=21;self.api_disabled=False;self.expert=True
        self.execution=2;self.filling=3;self.switch_during_read=False;self.checks=[]
    def account_info(self):
        a=super().account_info();a.trade_expert=self.expert;return a
    def terminal_info(self):
        t=super().terminal_info();t.tradeapi_disabled=self.api_disabled;return t
    def symbol_info(self,name):
        s=super().symbol_info(name);s.select=True;s.trade_mode=self.mode;s.order_mode=self.flags
        s.trade_exemode=self.execution;s.filling_mode=self.filling;return s
    def positions_get(self,ticket=None):
        if self.switch_during_read:self.login=2
        return super().positions_get(ticket)
    def order_check(self,r):self.checks.append(r);return N(retcode=0)
    def order_send(self,r):raise AssertionError('Native preflight must never send')


class NativePreflightTests(unittest.TestCase):
    def setUp(self):
        self.api=NativeFake()
        self.bound=TerminalBinding(1,'demo','XAUUSD.s',frozenset({123,124}))
        self.p=NativeMT5Preflight(self.api,self.bound)
        self.expected=dict(EXPECTED,identifier=88)
        self.api.positions=[N(ticket=77,identifier=88,magic=123,symbol='XAUUSD.s',type=0,
                             price_open=3998,volume=.01,sl=3997.9,tp=0)]
    def protect_request(self):
        return dict(action='TRADE_ACTION_SLTP',symbol='XAUUSD.s',position=77,sl='3998',tp='0')
    def close_request(self):
        return dict(action='TRADE_ACTION_DEAL',symbol='XAUUSD.s',position=77,magic=123,
                    type='ORDER_TYPE_SELL',volume='.01',type_filling='ORDER_FILLING_IOC')
    def test_real_marker_not_needed_for_validation(self):
        r=self.p.check(REQUEST,EXPECTED)
        self.assertTrue(r['validated']);self.assertFalse(r['safe_to_trade']);self.assertFalse(r['live_trading'])
        self.assertEqual(r['request']['action'],5)
        self.assertEqual(self.api.sent,[])
    def test_send_always_locked(self):
        self.p.check(REQUEST,EXPECTED)
        with self.assertRaises(RuntimeError):self.p.send(REQUEST)
    def test_flags_fail_closed(self):
        for field,value in [('api_disabled',True),('expert',False),('expert',None)]:
            with self.subTest(field=field,value=value):
                api=NativeFake();setattr(api,field,value)
                with self.assertRaises(ValueError):NativeMT5Preflight(api,self.bound).check(REQUEST,EXPECTED)
                self.assertEqual(api.checks,[])
    def test_bad_scope_and_oversize(self):
        for field,value in [('account',2),('server','other'),('symbol','EURUSD'),('magic',125),('magic',True)]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):self.p.validate(REQUEST,dict(EXPECTED,**{field:value}))
        with self.assertRaises(ValueError):self.p.validate(dict(REQUEST,volume='.11'),EXPECTED)
    def test_longonly_shortonly(self):
        self.api.mode=1;self.p.validate(REQUEST,EXPECTED)
        sell=dict(REQUEST,type='ORDER_TYPE_SELL_STOP',price='3998',sl='3998.7')
        with self.assertRaises(ValueError):self.p.validate(sell,EXPECTED)
        self.api.mode=2;self.p.validate(sell,EXPECTED)
        with self.assertRaises(ValueError):self.p.validate(REQUEST,EXPECTED)
    def test_longonly_allows_sell_close(self):
        self.api.mode=1
        native=self.p.validate(self.close_request(),self.expected)
        self.assertEqual(native['type'],1);self.assertNotIn('price',native)
    def test_closeonly_allows_protect_and_close_not_entry(self):
        self.api.mode=3
        self.p.validate(self.protect_request(),self.expected)
        self.p.validate(self.close_request(),self.expected)
        with self.assertRaises(ValueError):self.p.validate(REQUEST,EXPECTED)
    def test_stop_and_sl_permission_required(self):
        for flag in (self.api.SYMBOL_ORDER_STOP,self.api.SYMBOL_ORDER_SL):
            self.api.flags=21 & ~flag
            with self.assertRaises(ValueError):self.p.validate(REQUEST,EXPECTED)
    def test_filling_bitmask_not_order_enum(self):
        self.api.filling=1 # FOK allowed; IOC enum is also 1, but its flag is 2
        with self.assertRaises(ValueError):self.p.validate(self.close_request(),self.expected)
        self.p.validate(dict(self.close_request(),type_filling='ORDER_FILLING_FOK'),self.expected)
    def test_external_stop_never_weakened_at_stage_zero(self):
        req=dict(self.protect_request(),sl='3997.5')
        with self.assertRaises(ValueError):self.p.validate(req,dict(self.expected,confirmed_stop_stage=0))
    def test_account_switch_during_position_read(self):
        self.api.switch_during_read=True
        with self.assertRaises(ValueError):self.p.check(self.protect_request(),self.expected)
        self.assertEqual(self.api.checks,[])
    def test_foreign_position(self):
        for field,value in [('magic',125),('symbol','EURUSD'),('identifier',89)]:
            original=deepcopy(self.api.positions[0]);setattr(self.api.positions[0],field,value)
            with self.assertRaises(ValueError):self.p.validate(self.protect_request(),self.expected)
            self.api.positions[0]=original
    def test_no_permission_carryover_after_error(self):
        self.p.check(REQUEST,EXPECTED)
        self.api.expert=False
        with self.assertRaises(ValueError):self.p.check(REQUEST,EXPECTED)
        with self.assertRaises(RuntimeError):self.p.send(REQUEST)
        self.assertEqual(len(self.api.checks),1)
    def test_scope_configuration_rejects_mutable_magics(self):
        with self.assertRaises(ValueError):TerminalBinding(1,'demo','XAUUSD.s',{123})
        with self.assertRaises(ValueError):TerminalBinding(True,'demo','XAUUSD.s',frozenset({123}))
