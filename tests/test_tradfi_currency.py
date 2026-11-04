"""UST accounting with the reported symbol spec and entirely synthetic executions."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import json
import time
import unittest
from unittest.mock import Mock

from tradfi.broker import Spec
from tradfi.engine import Controller
from tradfi.gateway import FileGateway
from tradfi.settings import Settings
from tradfi.storage import DurabilityError
import test_tradfi as fixture


REPORTED_SPEC = dict(
    symbol='XAUUSD.s', currency='UST', profit_currency='USD', calc_mode='2',
    contract_size='100', tick_size='0.01', tick_value_profit='1', tick_value_loss='1',
    volume_min='0.01', volume_step='0.01', volume_max='100', point='0.01',
    stops_level='20', freeze_level='0')


class USTAccountingTest(unittest.TestCase):
    def make_fixture(self):
        f = fixture.RecoveryTest()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.broker.data['spec'] = deepcopy(REPORTED_SPEC)
        return f

    def test_reported_spec_preserves_distinct_account_and_profit_currencies(self):
        f = self.make_fixture()
        spec = Spec.load(f.bot.snapshot()['spec'])
        spec.validate(Settings())
        self.assertEqual(spec.currency, 'UST')
        self.assertEqual(spec.profit_currency, 'USD')
        self.assertEqual(f.broker.data['spec'], REPORTED_SPEC)
        for currency in ('USD', 'USDT', 'EUR', ''):
            with self.subTest(currency=currency), self.assertRaisesRegex(ValueError, 'UST'):
                Spec.load(dict(REPORTED_SPEC, currency=currency))

    def test_calculator_sets_target_and_actual_deal_profit_drives_completion(self):
        for side in ('BUY', 'SELL'):
            with self.subTest(side=side):
                f = self.make_fixture()
                calculator = f.broker.profit
                # Deliberately differs from contract-size/tick-value estimates.
                f.broker.profit = Mock(side_effect=lambda *args: calculator(*args) * 3)
                f.start(); f.fill(side)
                c = f.bot.state['cycle']
                self.assertEqual(D(c['target']), D('5.4'))
                self.assertEqual(c['currency'], 'UST')
                self.assertTrue(f.broker.profit.called)
                f.broker.close('TP', profit='4.4'); f.tick()
                completed = f.bot.state['completed'][0]
                self.assertEqual(D(completed['realized']), D('4.4'))
                self.assertEqual(D(completed['forgiven_residual']), D('1'))
                self.assertEqual(completed['currency'], 'UST')
                # Simulator includes enormous commission/swap: neither enters recovery.
                self.assertIn(' UST', f.bot.state['notifications'][-1]['text'])
                self.assertNotIn(' USD', f.bot.status())
                restored = Controller(f.store, f.broker)
                self.assertEqual(restored.state['completed'], f.bot.state['completed'])

    def test_ust_one_unit_boundary_only_for_tp_or_stepped_sl(self):
        cases = [('TP', False, '0.99', True), ('TP', False, '1', True),
                 ('TP', False, '1.01', False), ('SL', True, '1', True),
                 ('SL', True, '1.01', False), ('SL', False, '1', False)]
        for reason, stepped, residual, complete in cases:
            with self.subTest(reason=reason, stepped=stepped, residual=residual):
                f = self.make_fixture(); f.recovery()
                c = f.bot.state['cycle']
                if stepped:
                    entry = D(c['position']['entry'])
                    f.broker.quote(entry + D('.8'), entry + D('.9')); f.tick(2)
                    self.assertTrue(c['position']['stepped'])
                amount = D(c['recovery']) - D(residual)
                realized = D(c['realized'])
                f.broker.close(reason, profit=amount); f.tick()
                self.assertEqual(f.bot.state['cycle'] is None, complete)
                record = f.bot.state['completed'][-1] if complete else f.bot.state['cycle']
                self.assertEqual(record['currency'], 'UST')
                self.assertEqual(D(record['realized']), realized + amount)
                field = 'forgiven_residual' if complete else 'recovery'
                self.assertEqual(D(record[field]), D(residual))

    def test_ust_recovery_restart_preserves_money_exactly(self):
        f = self.make_fixture(); f.recovery()
        before = deepcopy(f.bot.state['cycle'])
        restored = Controller(f.store, f.broker)
        self.assertEqual(restored.state['cycle'], before)
        self.assertEqual(restored.state['currency'], 'UST')
        restored.snapshot()
        self.assertEqual(restored.state['cycle'], before)

    def test_incompatible_or_unlabelled_saved_money_is_never_relabelled(self):
        for scope in ('state', 'cycle', 'completed'):
            for currency in ('USD', 'USDT', None):
                with self.subTest(scope=scope, currency=currency):
                    f = self.make_fixture(); f.start(); f.fill()
                    if scope == 'state':
                        record = f.bot.state
                    elif scope == 'cycle':
                        record = f.bot.state['cycle']
                    else:
                        f.broker.close('TP'); f.tick()
                        record = f.bot.state['completed'][0]
                    if currency is None:
                        del record['currency']
                    else:
                        record['currency'] = currency
                    f.store.save(f.bot.state, 'incompatible_fixture')
                    before = f.store.load()
                    f.broker.snapshot = Mock(side_effect=AssertionError('No broker access on restore'))
                    with self.assertRaisesRegex(DurabilityError, 'monetary currency'):
                        Controller(f.store, f.broker)
                    self.assertEqual(f.store.load(), before)
                    f.broker.snapshot.assert_not_called()

    def test_live_currency_change_pauses_before_reconciliation_or_mutation(self):
        f = self.make_fixture(); f.recovery()
        before = deepcopy(f.bot.state['cycle'])
        count = len(f.broker.commands)
        f.broker.data['spec']['currency'] = 'USD'
        f.bot = Controller(f.store, f.broker); f.tick()
        self.assertEqual(f.bot.state['phase'], 'SAFE_PAUSE')
        self.assertEqual(f.bot.state['cycle'], before)
        self.assertEqual(len(f.broker.commands), count)

    def test_reported_snapshot_through_file_gateway_keeps_stale_profit_guard(self):
        f = self.make_fixture()
        root = Path(f.tmp.name)/'mailbox'
        gateway = FileGateway(root)
        data = f.broker.snapshot()
        data.update(protocol=1, connected=True, now=time.time())
        data['quote']['utc_time'] = time.time()
        path = root/'snapshot.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(gateway.snapshot()['spec'], REPORTED_SPEC)
        Spec.load(gateway.snapshot()['spec']).validate(Settings())
        data['quote']['utc_time'] -= 60
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(TimeoutError, 'quote is stale'):
            gateway.profit('BUY', D('.09'), D('4000'), D('4000.2'))
        self.assertFalse(list((root/'commands').iterdir()))
        self.assertFalse(gateway.enable_trading)

    def test_ea_requires_ust_without_replacing_reported_spec_currency(self):
        source = (Path(__file__).resolve().parents[1]/'mt5/TradFiGateway.mq5').read_text()
        self.assertIn('AccountInfoString(ACCOUNT_CURRENCY)!="UST"', source)
        self.assertIn('Q(AccountInfoString(ACCOUNT_CURRENCY))', source)
        self.assertIn('Q(SymbolInfoString(TradeSymbol,SYMBOL_CURRENCY_PROFIT))', source)
        for guard in ('!AllowTrading', '!MQLInfoInteger(MQL_TRADE_ALLOWED)',
                      '!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)',
                      'TimeTradeServer()-tick.time>5'):
            self.assertIn(guard, source)
