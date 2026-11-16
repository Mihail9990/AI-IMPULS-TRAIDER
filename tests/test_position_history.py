from copy import deepcopy
import unittest
from tradfi.position_history import project_history

class PositionHistory(unittest.TestCase):
    def setup_data(self):
        state=dict(backend='mt5',account='a',currency='UST',seen_deals=['2'],completed=[dict(id='c',realized='-50')],cycle=None)
        base=dict(symbol='XAUUSD.s',magic=123,position_id=10,volume='.1',commission='-.2',swap='0',fee='-.05')
        entry=dict(base,ticket=1,entry=0,type=0,price='2000',profit='0',time_msc=100000,reason=0)
        exit=dict(base,ticket=2,entry=1,type=1,price='2001',profit='10',time_msc=200000,reason=5)
        record=dict(backend='mt5',account='a',currency='UST',cycle_id='c',position_id='10',ticket='2',raw=exit,net_accounting=dict(opening_net='-.25',closing_net='9.75'))
        return state,[record],[entry,exit]
    def project(self,data,**kw):return project_history(*data,symbol='XAUUSD.s',magic=123,**kw)
    def test_exact_costs_prices_times_side_and_no_carry_or_writeoff(self):
        data=self.setup_data();before=deepcopy(data);view=self.project(data,to_utc=lambda t:float(t)-10)
        row=view['position_history'][0];self.assertEqual(row['net'],'9.50');self.assertEqual(row['reason'],'TP')
        self.assertEqual(row['side'],'BUY');self.assertEqual(row['entry_price'],'2000');self.assertEqual(row['exit_price'],'2001')
        self.assertEqual((row['opened_at'],row['closed_at']),(90,190));self.assertEqual(view['history_metrics']['all_closed_net'],'9.50');self.assertEqual(data,before)
    def test_missing_entry_does_not_invent_values_or_total(self):
        state,rows,facts=self.setup_data();view=self.project((state,rows,facts[1:]))
        self.assertIsNone(view['position_history'][0]['net']);self.assertIsNone(view['history_metrics']['all_closed_net'])
        self.assertFalse(view['history_metrics']['history_complete'])
    def test_unknown_reason_not_inferred_from_profit(self):
        data=self.setup_data();data[1][0]['raw']['reason']=3
        row=self.project(data)['position_history'][0];self.assertIsNone(row['reason']);self.assertEqual(row['net'],'9.50')
    def test_missing_fee_or_mismatched_volume_or_identity_blocks_total(self):
        for key,value in [('fee',None),('volume','.05'),('magic',999),('position_id',42)]:
            data=self.setup_data();data[1][0]['raw'][key]=value
            self.assertIsNone(self.project(data)['history_metrics']['all_closed_net'])
    def test_old_aggregate_without_deals_not_added_to_total(self):
        data=self.setup_data();data[0]['completed'].append(dict(id='old',realized='999'))
        view=self.project(data);self.assertFalse(view['history_metrics']['history_complete']);self.assertIsNone(view['history_metrics']['all_closed_net'])
        self.assertEqual(view['position_history'][0]['net'],'9.50')
    def test_missing_time_verification_hides_times_not_invents_utc(self):
        row=self.project(self.setup_data())['position_history'][0]
        self.assertIsNone(row['opened_at']);self.assertIsNone(row['closed_at']);self.assertEqual(row['net'],'9.50')
    def test_open_position_kept_separate_from_closed_total(self):
        data=self.setup_data();data[0]['cycle']=dict(tickets={'other':dict(side='SELL',entry='1900',opening_net='-8',closed=False)})
        view=self.project(data);row=next(r for r in view['position_history'] if r['status']=='open')
        self.assertEqual(row['side'],'SELL');self.assertIsNone(row['net']);self.assertEqual(view['history_metrics']['all_closed_net'],'9.50')
    def test_no_deals_no_cycles_is_zero_and_order_events_do_not_enter_history(self):
        state=dict(backend='mt5',account='a',currency='UST',seen_deals=[],completed=[],commands={'cancel':{},'modify':{}})
        view=self.project((state,[],[]));self.assertEqual(view['position_history'],[]);self.assertEqual(view['history_metrics']['all_closed_net'],'0')
    def test_missing_consumed_record_does_not_claim_complete_history(self):
        data=self.setup_data();data[0]['seen_deals'].append('3');self.assertFalse(self.project(data)['history_metrics']['history_complete'])

    def test_closed_current_ticket_without_ledger_is_incomplete_not_zero(self):
        state, rows, facts = self.setup_data()
        state.update(seen_deals=[], completed=[], cycle=dict(tickets={
            '10': dict(closed=True, side='BUY', entry='2000', opening_net='-.25', exit_net='9.75')}))
        view = self.project((state, [], facts))
        self.assertFalse(view['history_metrics']['history_complete'])
        self.assertIsNone(view['history_metrics']['all_closed_net'])
        self.assertEqual(view['position_history'][0]['status'], 'incomplete')
        self.assertIsNone(view['position_history'][0]['net'])

    def split_close_data(self):
        state, rows, facts = self.setup_data()
        first = rows[0]
        first['raw'].update(volume='.04', profit='4', commission='-.08', fee='-.02')
        first['net_accounting']['closing_net'] = '3.90'
        second = deepcopy(first); second['ticket'] = '3'
        second['raw'].update(ticket=3, volume='.06', profit='6', commission='-.12',
            fee='-.03', price='2002', time_msc=300000)
        second['net_accounting']['closing_net'] = '5.85'
        rows.append(second); facts.append(deepcopy(second['raw'])); state['seen_deals'].append('3')
        return state, rows, facts

    def test_split_close_counts_opening_cost_once_and_weights_exit_price(self):
        view = self.project(self.split_close_data())
        self.assertEqual(len(view['position_history']), 1)
        self.assertEqual(view['position_history'][0]['exit_price'], '2001.6')
        self.assertEqual(view['position_history'][0]['net'], '9.50')
        self.assertEqual(view['history_metrics']['all_closed_net'], '9.50')

    def test_partial_volume_and_duplicate_closes_block_total(self):
        for mode in ('partial', 'duplicate'):
            with self.subTest(mode=mode):
                state, rows, facts = self.split_close_data()
                if mode == 'partial':
                    rows.pop(); state['seen_deals'].pop()
                else:
                    rows.append(deepcopy(rows[-1]))
                view = self.project((state, rows, facts))
                self.assertFalse(view['history_metrics']['history_complete'])
                self.assertIsNone(view['history_metrics']['all_closed_net'])

    def test_sell_loss_uses_signed_broker_money_not_side_or_reason(self):
        state, rows, facts = self.setup_data()
        facts[0]['type'] = 1
        rows[0]['raw'].update(type=0, reason=4, profit='-10')
        rows[0]['net_accounting']['closing_net'] = '-10.25'
        view = self.project((state, rows, facts))
        self.assertEqual(view['position_history'][0]['side'], 'SELL')
        self.assertEqual(view['position_history'][0]['reason'], 'SL')
        self.assertEqual(view['history_metrics']['all_closed_net'], '-10.50')

class NativePositionHistory(unittest.TestCase):
    def test_native_closed_position_readonly_projection_survives_restart(self):
        import test_tradfi_basket as fixtures
        from tradfi.recovery_v3 import ControlledRecoveryController
        from tradfi.web_control import NativePanel
        f=fixtures.NativeBasketTest();f.setUp();self.addCleanup(f.doCleanups);self.addCleanup(f.tearDown)
        def controller():return ControlledRecoveryController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
        f.bot=controller();f.bot.command('/start');f.api.now+=60
        f.api.tick=f.api.tick._replace(time_msc=f.api.now*1000)
        f.api.rates=[dict(time=int(f.api.now//60)*60-60,open=3999.,close=4000.)]
        f.tick(2);f.api.fill();f.tick(3);f.api.close_position(reason=f.api.DEAL_REASON_SL);f.tick(4)
        panel=NativePanel(f.bot,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
        panel.snapshot=f.bot.snapshot();panel.feed.capture(panel.snapshot)
        state=deepcopy(f.bot.state);sends=deepcopy(f.api.sends)
        view=panel.view();self.assertEqual(len(view['position_history']),1)
        row=view['position_history'][0];self.assertEqual(row['reason'],'SL');self.assertEqual(row['side'],'BUY')
        self.assertIsNotNone(row['entry_price']);self.assertIsNotNone(row['exit_price']);self.assertTrue(view['metrics']['history_complete'])
        self.assertEqual(row['net'],view['metrics']['all_closed_net']);self.assertEqual(f.bot.state,state);self.assertEqual(f.api.sends,sends)
        f.bot=controller();again=NativePanel(f.bot,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
        again.snapshot=panel.snapshot;again.feed.capture(panel.snapshot)
        self.assertEqual(again.view()['position_history'],view['position_history']);self.assertEqual(f.api.sends,sends)
