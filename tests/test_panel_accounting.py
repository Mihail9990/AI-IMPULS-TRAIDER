"""Panel metrics reuse booked ticket facts; views cannot mutate trading state."""
from copy import deepcopy
from decimal import Decimal
import unittest
from tradfi.panel_accounting import accounting_view

class PanelAccounting(unittest.TestCase):
    def state(self):
        return dict(currency='UST',basket_carry='8',cycle=dict(id='c',currency='UST',recovery='10.25',realized='-10.25',tickets={
            'closed':dict(opening_net='-.10',exit_net='-2',closed=True),
            'open':dict(opening_net='-.15',closed=False,unrealized='999',exit_net='777')}),completed=[])
    def test_existing_total_and_ticket_net_exclude_carry_and_floating(self):
        state=self.state();before=deepcopy(state);view=accounting_view(state)
        self.assertEqual(view['metrics']['recovery_total'],'10.25')
        self.assertEqual(Decimal(view['metrics']['cycle_net']),Decimal('-2.25'))
        self.assertEqual(view['metrics']['currency'],'UST');self.assertEqual(state,before)
    def test_no_cycle_zero_carry_and_missing_history_remain_distinct(self):
        state=self.state();state['cycle']=None
        self.assertEqual(accounting_view(state)['metrics']['recovery_total'],'8')
        self.assertIsNone(accounting_view(state)['metrics']['cycle_net'])
        state['cycle']=dict(tickets={},recovery='0')
        self.assertEqual(accounting_view(state)['metrics']['recovery_total'],'0')
        self.assertEqual(accounting_view(state)['metrics']['cycle_net'],'0')
    def test_missing_or_nonfinite_booked_fields_are_unknown(self):
        for field,value in [('opening_net',None),('opening_net','NaN'),('exit_net','Infinity')]:
            state=self.state();state['cycle']['tickets']['closed'][field]=value
            self.assertIsNone(accounting_view(state)['metrics']['cycle_net'])
        state=self.state();del state['cycle']['tickets']['closed']['exit_net']
        self.assertIsNone(accounting_view(state)['metrics']['cycle_net'])
    def test_history_is_bounded_copied_and_not_reinterpreted(self):
        state=self.state();state['completed']=[dict(id=str(i),realized='-10',completion='RECOVERY_WAIVED',written_off_recovery='10',private='secret') for i in range(105)]
        view=accounting_view(state);rows=view['cycle_history'];self.assertEqual(len(rows),100)
        self.assertEqual(rows[0]['id'],'5');self.assertEqual(rows[-1]['realized'],'-10')
        self.assertNotIn('private',rows[-1]);self.assertNotIn('cycle_net',rows[-1]);self.assertNotIn('at_utc',rows[-1])
        rows[-1]['realized']='999';self.assertEqual(state['completed'][-1]['realized'],'-10')
    def test_manual_reset_has_retained_debt_but_no_invented_result(self):
        state=self.state();state['completed']=[dict(id='old',completion='MANUAL_RESET_RETAINED',recovery_retained='6')]
        row=accounting_view(state)['cycle_history'][0]
        self.assertEqual(row['recovery_retained'],'6');self.assertNotIn('realized',row)

class NativePanelAccounting(unittest.TestCase):
    def test_readonly_native_view_exposes_metrics_and_preserves_store(self):
        import test_tradfi_basket as fixtures
        from tradfi.recovery_v3 import ControlledRecoveryController
        from tradfi.web_control import NativePanel
        f=fixtures.NativeBasketTest();f.setUp()
        self.addCleanup(f.doCleanups);self.addCleanup(f.tearDown)
        f.bot=ControlledRecoveryController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
        panel=NativePanel(f.bot,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
        panel.snapshot=f.bot.snapshot();panel.feed.capture(panel.snapshot)
        before=deepcopy(f.bot.state);sends=deepcopy(f.api.sends)
        first=panel.view();second=panel.view()
        self.assertEqual(first['metrics']['recovery_total'],first['debt'])
        self.assertIsNone(first['metrics']['cycle_net'])
        self.assertEqual(first['cycle_history'],[])
        self.assertEqual(first['metrics'],second['metrics'])
        self.assertEqual(f.bot.state,before);self.assertEqual(f.api.sends,sends)
