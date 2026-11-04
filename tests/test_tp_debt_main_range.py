from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_basket_latest_close as fixtures
from tradfi.basket_latest_close import LatestCloseBasketController

class TPDebtMain(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups);self.f=self.case.f
        self.case.first_sl();self.case.fill_basket();self.f.tick(2)
        self.main=deepcopy(self.f.bot.state['cycle']['main_range'])
    def close_all(self,fee='-1',inside=True):
        for p in list(self.f.b.data['positions']):
            self.f.b.data['now']+=1;self.f.b.close('TP',position_id=p['id'],commission=fee)
        if inside:self.f.b.quote('3998','3998.1')
    def test_returns_previous_main_and_preserves_actual_cost_debt(self):
        self.close_all();self.f.tick(5);c=self.f.bot.state['cycle']
        self.assertEqual(c['expanded'],self.main);self.assertEqual(c['tp_debt_resume']['levels'],self.main)
        self.assertGreater(D(c['recovery']),0);self.assertEqual(len(self.f.b.data['orders']),12)
        expected=-sum(sum(D(d.get(k,'0')) for k in ('profit','commission','swap','fee')) for d in self.f.b.data['deals'])
        self.assertEqual(D(c['recovery']),expected)
    def test_mirrored_initial_sell_buy_basket_returns_main(self):
        case=fixtures.LatestClose();case.setUp();self.addCleanup(case.doCleanups)
        case.first_sl('SELL');case.fill_basket('BUY');case.f.tick(2)
        main=deepcopy(case.f.bot.state['cycle']['main_range'])
        for p in list(case.f.b.data['positions']):
            case.f.b.data['now']+=1;case.f.b.close('TP',position_id=p['id'],commission='-1')
        middle=(D(main['BUY'])+D(main['SELL']))/2
        case.f.b.quote(str(middle),str(middle+D('.1')));case.f.tick(5)
        self.assertEqual(case.f.bot.state['cycle']['expanded'],main)
        self.assertGreater(D(case.f.bot.state['cycle']['recovery']),0)
        self.assertEqual(len(case.f.b.data['orders']),12)
    def test_restart_before_observation_and_after_rearm_no_duplicates(self):
        self.close_all();self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(5)
        before=deepcopy(self.f.b.data['orders']);debt=self.f.bot.state['cycle']['recovery']
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(4)
        self.assertEqual(before,self.f.b.data['orders']);self.assertEqual(debt,self.f.bot.state['cycle']['recovery'])
    def test_temporary_chart_range_is_not_the_main_range(self):
        self.f.bot.edit(levels={'BUY':'4002','SELL':'3995'},expected=self.f.bot.levels(),expected_orders=[]);self.f.tick(5)
        self.assertNotEqual(self.f.bot.levels(),self.main);self.close_all();self.f.tick(5)
        self.assertEqual(self.f.bot.levels(),self.main)
    def test_crossed_main_range_halts_no_market_or_partial_pair(self):
        self.close_all(inside=False);self.f.tick(5);c=self.f.bot.state['cycle']
        self.assertEqual(c['manual_reason'],'TP_DEBT_MAIN_RANGE_INVALID');self.assertFalse(self.f.b.data['orders'])
        self.assertGreater(D(c['recovery']),0);self.assertFalse(any(cmd['kind']=='MARKET' for cmd in self.f.b.commands))
    def test_missing_main_range_is_not_guessed(self):
        self.f.bot.state['cycle'].pop('main_range');self.close_all();self.f.tick(5)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'TP_DEBT_MAIN_RANGE_MISSING');self.assertFalse(self.f.b.data['orders'])
    def test_invalid_tick_main_range_halts(self):
        self.f.bot.state['cycle']['main_range']['BUY']='4001.005';self.close_all();self.f.tick(5)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'TP_DEBT_MAIN_RANGE_INVALID');self.assertFalse(self.f.b.data['orders'])
    def test_no_rearm_before_last_ticket_closes(self):
        for p in list(self.f.b.data['positions'])[:-1]:
            self.f.b.close('TP',position_id=p['id'],commission='-1')
        self.f.tick(4);self.assertEqual(len(self.f.b.data['positions']),1);self.assertFalse(self.f.b.data['orders'])
    def test_read_only_never_rearms(self):
        self.close_all();before=len(self.f.b.commands);self.f.b.read_only=True;self.f.tick(4)
        self.assertEqual(before,len(self.f.b.commands));self.assertFalse(self.f.b.data['orders'])
    def test_stop_keeps_debt_and_blocks_new_orders(self):
        self.close_all();self.f.bot.command('/stop');self.f.tick(4)
        self.assertGreater(D(self.f.bot.state['cycle']['recovery']),0);self.assertFalse(self.f.b.data['orders'])
    def test_reopened_pair_preserves_oco_double_fill_safeguard(self):
        self.close_all();self.f.tick(4);self.f.b.fill('BUY');self.f.b.fill('SELL');self.f.tick(4)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'BOTH_RECOVERY_SIDES_FILLED')
    def test_unknown_other_intent_does_not_rearm(self):
        self.close_all();cid=self.f.bot._intent('STOP',side='BUY',price='4002',trigger='4002',size='.01',sl='4000',tp='4003',sl_distance='2',crossed_policy='reject',basket_index=0)
        self.f.bot.state['commands'][cid]['status']='UNKNOWN';self.f.b.results[cid]={'status':'UNKNOWN'};self.f.bot.save('unknown_fixture');self.f.tick(3)
        self.assertFalse(self.f.b.data['orders'])
    def test_zero_debt_still_starts_fresh_candle_cycle(self):
        self.close_all(fee='0');self.f.tick(5)
        self.assertIsNone(self.f.bot.state['cycle']);self.assertFalse(self.f.b.data['orders'])

    def test_once_and_permanent_overrides_keep_their_tp_completion_rules(self):
        from test_manual_range_control import ManualControl
        for duration in ('once','permanent'):
            case=ManualControl();case.setUp();self.addCleanup(case.doCleanups)
            case.close(duration=duration);selected=deepcopy(case.f.bot.state['cycle']['expanded']);case.fill_next()
            for p in list(case.f.b.data['positions']):
                case.f.b.data['now']+=1;case.f.b.close('TP',position_id=p['id'],commission='-1')
            case.f.b.quote('3998','3998.1');case.f.tick(5);c=case.f.bot.state['cycle']
            expected=case.main if duration=='once' else selected
            self.assertEqual(c['expanded'],expected);self.assertEqual(c['main_range'],expected)
            self.assertEqual(len(case.f.b.data['orders']),12);self.assertFalse(c.get('manual'))


class NativeTPDebt(unittest.TestCase):
    def test_native_ledger_full_tp_costs_rearms_previous_main(self):
        case=fixtures.NativeLatestClose();case.setUp();self.addCleanup(case.doCleanups);case.first_sl();f=case.f
        for _ in range(6):f.api.fill(index=next(i for i,o in enumerate(f.api.orders) if o.type==f.api.ORDER_TYPE_SELL_STOP))
        f.tick(5);main=deepcopy(f.bot.state['cycle']['main_range'])
        for _ in range(6):
            f.api.now+=1;f.api.close_position(reason=f.api.DEAL_REASON_TP)
            f.api.deals[-1]=f.api.deals[-1]._replace(commission=-1.)
        f.api.tick=f.api.tick._replace(bid=3998.,ask=3998.1,time_msc=f.api.now*1000);f.tick(5)
        self.assertGreater(D(f.bot.state['cycle']['recovery']),0);self.assertEqual(f.bot.state['cycle']['expanded'],main)
        self.assertEqual(len(f.api.orders),12);count=len(f.api.sends)
        f.bot=LatestCloseBasketController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now);f.tick(3)
        self.assertEqual(len(f.api.sends),count)

class PaperTPDebt(unittest.TestCase):
    def test_paper_observed_tp_costs_then_fresh_inside_quote_rearms_main(self):
        from test_tradfi_paper import PaperTest
        case=PaperTest();case.setUp();self.addCleanup(case.doCleanups);self.addCleanup(case.tearDown);case.reversal()
        main=deepcopy(case.session.controller.state['cycle']['main_range'])
        case.action(dict(type='paper_costs',commission_per_lot_side='100'))
        target=min(D(p['tp']) for p in case.p['positions'])
        case.reader.tick(target-D('.1'),target);case.session.exchange.observe(case.reader.read())
        self.assertFalse(case.p['positions'])
        case.reader.tick('3998','3998.1');case.step(6)
        c=case.session.controller.state['cycle'];self.assertEqual(c['expanded'],main)
        self.assertGreater(D(c['recovery']),0);self.assertEqual(len(case.p['orders']),12)
        ids=sorted(o['id'] for o in case.p['orders']);case.session.close();case.create();case.step()
        case.action(dict(type='paper_resume'));case.step(3)
        self.assertEqual(ids,sorted(o['id'] for o in case.p['orders']))
