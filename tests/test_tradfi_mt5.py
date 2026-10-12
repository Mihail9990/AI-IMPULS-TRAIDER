"""Offline contract regressions. The official terminal module is never imported."""
from collections import namedtuple
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tradfi.engine import Controller
from tradfi.mt5_adapter import MT5Broker
from tradfi.storage import Store, DurabilityError


def record(**fields):
    return namedtuple('Record', fields)(**fields)


class FakeMT5:
    """Explicit in-memory API double: no terminal, network, files or real orders."""
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 0, 1, 4, 5
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    DEAL_REASON_TP, DEAL_REASON_SL, DEAL_REASON_EXPERT = 5, 4, 3
    ORDER_STATE_STARTED, ORDER_STATE_PLACED, ORDER_STATE_PARTIAL = 0, 1, 3
    ORDER_STATE_FILLED, ORDER_STATE_CANCELED, ORDER_STATE_EXPIRED, ORDER_STATE_REJECTED = 4, 2, 6, 5
    ORDER_STATE_REQUEST_ADD, ORDER_STATE_REQUEST_MODIFY, ORDER_STATE_REQUEST_CANCEL = 7, 8, 9
    TRADE_ACTION_PENDING, TRADE_ACTION_DEAL, TRADE_ACTION_REMOVE, TRADE_ACTION_SLTP = 5, 1, 8, 6
    ORDER_TIME_GTC, ORDER_FILLING_RETURN, ORDER_FILLING_FOK = 0, 2, 0
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_TRADE_EXECUTION_REQUEST, SYMBOL_TRADE_EXECUTION_INSTANT, SYMBOL_TRADE_EXECUTION_MARKET = 0, 1, 2
    TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE_PARTIAL = 10009, 10008, 10010
    TRADE_RETCODE_REJECT, TRADE_RETCODE_INVALID = 10006, 10013
    TRADE_RETCODE_INVALID_VOLUME, TRADE_RETCODE_INVALID_PRICE, TRADE_RETCODE_INVALID_STOPS = 10014, 10015, 10016
    TRADE_RETCODE_NO_MONEY, TRADE_RETCODE_MARKET_CLOSED, TRADE_RETCODE_TRADE_DISABLED = 10019, 10018, 10017
    TRADE_RETCODE_INVALID_FILL, TRADE_RETCODE_INVALID_EXPIRATION = 10030, 10022
    TIMEFRAME_M1 = 1

    def __init__(self):
        self.initialized = self.shutdowns = 0
        self.sends, self.checks, self.calcs = [], [], []
        self.now = 1791800000
        self.terminal = record(connected=True, path=r'C:\Program Files\MetaTrader 5', trade_allowed=True, tradeapi_disabled=False)
        self.account = record(login=111111111, server='Bybit-Live-6', currency='UST', margin_mode=2, trade_allowed=True, trade_expert=True)
        self.info = record(visible=True, currency_profit='USD', trade_calc_mode=2, trade_contract_size=100,
            trade_tick_size=.01, trade_tick_value_profit=1, trade_tick_value_loss=1, volume_min=.01,
            volume_step=.01, volume_max=100, point=.01, trade_stops_level=20, trade_freeze_level=0,
            trade_mode=4, order_mode=127, volume_limit=0, expiration_mode=1, order_gtc_mode=0,
            trade_exemode=2, filling_mode=1)
        self.tick = record(bid=4000., ask=4000.1, time_msc=self.now*1000)
        self.orders, self.positions, self.history, self.deals = [], [], [], []
        self.rates = [{'time': self.now-60, 'close': 4000.}]
        self.check = record(retcode=0, comment='ok')
        self.response = record(retcode=self.TRADE_RETCODE_PLACED, order=101, deal=0, comment='ok')
        self.on_send = None

    def initialize(self, path, **kwargs): self.initialized += 1; return True
    def shutdown(self): self.shutdowns += 1
    def terminal_info(self): return self.terminal
    def account_info(self): return self.account
    def symbol_info(self, symbol): return self.info
    def symbol_info_tick(self, symbol): return self.tick
    def orders_get(self, **kwargs): return self.orders
    def positions_get(self, **kwargs): return self.positions
    def history_orders_get(self, *args): return self.history
    def history_deals_get(self, *args): return self.deals
    def copy_rates_from_pos(self, *args): return self.rates
    def order_calc_profit(self, kind, symbol, size, entry, close):
        self.calcs.append((kind, symbol, size, entry, close))
        return round((close-entry)*size*100*(1 if kind==0 else -1), 8)
    def order_check(self, request): self.checks.append(deepcopy(request)); return self.check
    def order_send(self, request):
        self.sends.append(deepcopy(request))
        if self.on_send: self.on_send(request)
        if isinstance(self.response, Exception): raise self.response
        return self.response
    def last_error(self): return (1, 'Success')


class MT5Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'state.sqlite3'); self.addCleanup(self.store.close)
        self.api = FakeMT5()
        self.config = dict(backend='mt5_direct_v1', terminal_path=r'C:\Program Files\MetaTrader 5\terminal64.exe',
            expected_login=111111111, expected_server='Bybit-Live-6', symbol='XAUUSD.s',
            enable_trading=True, legacy_ea_removed=True, legacy_state_reconciled=True)
        self.broker = self.make_broker(); self.addCleanup(self.broker.close)
        self.bot = Controller(self.store, self.broker)
        self.bot.state['account'] = self.broker.account

    def make_broker(self):
        broker = MT5Broker(self.config, self.store, api=self.api, clock=lambda:self.api.now)
        broker.connect()
        return broker

    def command(self, **overrides):
        cmd = dict(id='a'*24, kind='STOP', side='BUY', size='.01', price='4003', trigger='4003', sl='4001', tp='4004')
        cmd.update(overrides)
        return cmd

    def submit(self, cmd=None):
        cmd = cmd or self.command()
        self.bot.state['commands'][cmd['id']] = dict(cmd, status='UNKNOWN')
        self.bot.save('test_intent')
        self.broker.submit(cmd)
        return cmd

    def order(self, cmd, **overrides):
        fields = dict(ticket=101, symbol='XAUUSD.s', magic=self.broker.magic, comment=cmd['id'],
            type=self.api.ORDER_TYPE_BUY_STOP, state=self.api.ORDER_STATE_PLACED,
            volume_initial=.01, volume_current=.01, price_open=4003., sl=4001., tp=4004., position_id=501)
        fields.update(overrides)
        return record(**fields)

    def opening(self, cmd=None, *, partial=False):
        cmd = self.submit(cmd)
        volume = .005 if partial else .01
        self.api.history = [self.order(cmd, state=self.api.ORDER_STATE_FILLED, volume_current=0.)]
        self.api.deals = [record(ticket=301, order=101, symbol='XAUUSD.s', magic=self.broker.magic,
            comment=cmd['id'], position_id=501, entry=0, type=0, reason=3,
            volume=volume, price=4003., profit=0., time_msc=self.api.now*1000)]
        self.api.positions = [record(ticket=999, identifier=501, symbol='XAUUSD.s', magic=self.broker.magic,
            type=0, volume=volume, price_open=4003., sl=4001., tp=4004.)]
        self.api.tick = self.api.tick._replace(bid=4003., ask=4003.1)
        return cmd

    def test_normal_submission_durable_before_send_and_duplicate(self):
        def check(_):
            row = json.loads(self.store.db.execute('SELECT payload FROM mt5_commands').fetchone()[0])
            self.assertTrue(row['send_started']); self.assertEqual(row['result']['status'], 'UNKNOWN')
            self.assertEqual(self.store.load()['commands']['a'*24]['status'], 'UNKNOWN')
        self.api.on_send = check
        cmd = self.submit(); self.broker.submit(cmd)
        self.assertEqual(len(self.api.sends), 1)
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'ACCEPTED')
        self.assertEqual(self.api.sends[0]['type_filling'], self.api.ORDER_FILLING_RETURN)

    def test_no_controller_intent_no_send(self):
        with self.assertRaises(DurabilityError): self.broker.submit(self.command())
        self.assertEqual(self.api.sends, [])

    def test_payload_reuse_blocks(self):
        self.submit()
        with self.assertRaises(DurabilityError): self.broker.submit(self.command(size='.02'))
        self.assertEqual(len(self.api.sends), 1)

    def test_rejected_check_never_sends(self):
        self.api.check = record(retcode=10016)
        cmd = self.submit()
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')
        self.assertFalse(self.api.sends)

    def test_send_rejection_recorded(self):
        self.api.response = self.api.response._replace(retcode=self.api.TRADE_RETCODE_NO_MONEY)
        cmd = self.submit()
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')
        self.assertEqual(self.broker.rows[cmd['id']]['raw_result']['retcode'], 10019)

    def test_none_exception_and_partial_are_unknown_without_retry(self):
        for index, response in enumerate((None, TimeoutError('lost reply'), record(retcode=10010, order=101, deal=301))):
            with self.subTest(response=response):
                self.api.response = response
                cmd = self.submit(self.command(id=f'{index:024x}'))
                self.assertEqual(self.broker.result(cmd['id'])['status'], 'UNKNOWN')
                count = len(self.api.sends); self.broker.submit(cmd)
                self.assertEqual(len(self.api.sends), count)

    def test_restart_preserves_unknown_no_blind_replay(self):
        self.api.response = None; cmd = self.submit()
        self.broker.close(); restarted = self.make_broker(); self.addCleanup(restarted.close)
        restarted.submit(cmd)
        self.assertEqual(len(self.api.sends), 1)
        self.assertEqual(restarted.result(cmd['id'])['status'], 'UNKNOWN')
        self.assertTrue(restarted.rows[cmd['id']]['send_started'])

    def test_crash_after_send_before_result_persist_never_resends(self):
        original = self.broker._save_row
        def crash(cid, row, event):
            if event == 'send_result': raise DurabilityError('crash after broker response')
            original(cid, row, event)
        with patch.object(self.broker, '_save_row', side_effect=crash):
            with self.assertRaises(DurabilityError): self.submit()
        self.assertEqual(len(self.api.sends), 1)
        self.broker.close(); restarted = self.make_broker(); self.addCleanup(restarted.close)
        self.assertEqual(restarted.result('a'*24)['status'], 'UNKNOWN')
        restarted.submit(self.command())
        self.assertEqual(len(self.api.sends), 1)
        self.api.orders = [self.order(self.command())]
        snap = restarted.snapshot()
        self.assertTrue(snap['orders'][0]['owned'])
        self.assertEqual(snap['orders'][0]['command_id'], 'a'*24)

    def test_truncated_comment_reconciles_only_exact_success_ticket(self):
        cmd = self.submit()
        self.api.orders = [self.order(cmd, comment='truncated')]
        snap = self.broker.snapshot()
        self.assertTrue(snap['orders'][0]['owned'])
        self.api.orders = [self.order(cmd, ticket=555, comment='truncated')]
        self.assertFalse(self.broker.snapshot()['orders'][0]['owned'])

    def test_account_switch_during_send_retains_unknown_raw_evidence(self):
        self.api.on_send = lambda request: setattr(self.api, 'account', self.api.account._replace(login=222222222))
        cmd = self.submit()
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'UNKNOWN')
        self.assertEqual(self.broker.rows[cmd['id']]['raw_result']['order'], 101)
        self.broker.submit(cmd)
        self.assertEqual(len(self.api.sends), 1)

    def test_journal_failure_before_send_halts(self):
        original = self.broker._save_row
        def fail(cid, row, event):
            if event == 'may_send': raise DurabilityError('disk failure')
            original(cid, row, event)
        with patch.object(self.broker, '_save_row', side_effect=fail):
            with self.assertRaises(DurabilityError): self.submit()
        self.assertFalse(self.api.sends)

    def test_snapshot_requires_all_history_channels(self):
        for name in ('orders', 'positions', 'history', 'deals', 'rates'):
            old = getattr(self.api, name); setattr(self.api, name, None)
            with self.subTest(name=name), self.assertRaises(OSError): self.broker.snapshot()
            setattr(self.api, name, old)

    def test_history_overlap_dedup_and_lag_retains_evidence(self):
        self.opening()
        a = self.broker.snapshot(); b = self.broker.snapshot()
        self.assertEqual(a['deals'], b['deals']); self.assertEqual(len(b['deals']), 1)
        self.api.history = []; self.api.deals = []
        c = self.broker.snapshot()
        self.assertEqual(c['deals'], a['deals']); self.assertTrue(c['positions'][0]['owned'])

    def test_missing_opening_deal_does_not_own_position(self):
        self.opening(); self.api.deals = []
        self.assertFalse(self.broker.snapshot()['positions'][0]['owned'])

    def test_same_magic_foreign_comment_never_owned(self):
        cmd = self.command(); self.api.orders = [self.order(cmd, comment='manual')]
        self.assertFalse(self.broker.snapshot()['orders'][0]['owned'])
        sent = self.submit()
        self.assertEqual(self.broker.result(sent['id'])['status'], 'REJECTED')
        self.assertFalse(self.api.sends)

    def test_conflicting_historical_fact_fails(self):
        self.opening(); self.broker.snapshot()
        self.api.deals[0] = self.api.deals[0]._replace(profit=99.)
        with self.assertRaises(ValueError): self.broker.snapshot()

    def test_hedge_modify_uses_ticket_not_identifier(self):
        self.opening()
        cmd = self.submit(dict(id='b'*24, kind='MODIFY', position_id='501', sl='4002', tp='4004', purpose='STEP'))
        self.assertEqual(self.api.sends[-1]['position'], 999)
        self.assertEqual(self.api.sends[-1]['action'], self.api.TRADE_ACTION_SLTP)
        self.assertNotIn('order', self.api.sends[-1])
        self.api.positions[0] = self.api.positions[0]._replace(sl=4002.)
        self.broker.snapshot()
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'DONE')

    def test_modify_freeze_keeps_protection(self):
        self.opening(); self.api.info = self.api.info._replace(trade_freeze_level=200)
        count = len(self.api.sends)
        cmd = self.submit(dict(id='b'*24, kind='MODIFY', position_id='501', sl='4002', tp='4004', purpose='STEP'))
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')
        self.assertEqual(len(self.api.sends), count)
        self.assertEqual(self.api.positions[0].sl, 4001.)

    def test_step_cannot_worsen_sl(self):
        self.opening(); count = len(self.api.sends)
        cmd = self.submit(dict(id='b'*24, kind='MODIFY', position_id='501', sl='4000', tp='4004', purpose='STEP'))
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')
        self.assertEqual(len(self.api.sends), count)

    def test_partial_volume_preserved_not_refilled(self):
        self.opening(partial=True)
        snap = self.broker.snapshot()
        self.assertEqual(D(snap['positions'][0]['size']), D('.005'))
        self.assertEqual(D(snap['deals'][0]['volume']), D('.005'))
        self.assertEqual(len(self.api.sends), 1)

    def test_market_requires_fok_and_no_implicit_reprice(self):
        self.api.info = self.api.info._replace(filling_mode=2)
        cmd = self.submit(self.command(kind='MARKET', price='4000.10', trigger='4000', sl='3999', tp='4001'))
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')
        self.assertFalse(self.api.sends)

    def test_readonly_preflight_trade_off_no_send_or_check(self):
        self.api.terminal = self.api.terminal._replace(trade_allowed=False)
        broker = MT5Broker(dict(self.config, enable_trading=False), api=self.api, clock=lambda:self.api.now)
        broker.connect(); self.addCleanup(broker.close)
        result = broker.preflight()
        self.assertEqual(result['order_calc_profit'], '0.01')
        self.assertEqual(result['currency'], 'UST'); self.assertEqual(result['profit_currency'], 'USD')
        self.assertFalse(result['adapter_trading_enabled']); self.assertFalse(result['trade_allowed'])
        self.assertFalse(self.api.sends); self.assertFalse(self.api.checks)

    def test_stale_quote_blocks_preflight_and_profit(self):
        self.api.tick = self.api.tick._replace(time_msc=(self.api.now-6)*1000)
        with self.assertRaises(TimeoutError): self.broker.preflight()
        with self.assertRaises(TimeoutError): self.broker.profit('BUY', D('.01'), D(4000), D('4000.01'))
        self.assertFalse(self.api.calcs)

    def test_account_switch_rejected(self):
        self.api.account = self.api.account._replace(login=222222222)
        with self.assertRaises(ValueError): self.broker.snapshot()
        self.assertFalse(self.api.sends)

    def test_currency_rejected(self):
        self.api.account = self.api.account._replace(currency='USD')
        with self.assertRaises(ValueError): self.broker.snapshot()

    def test_legacy_state_blocks_before_initialize(self):
        self.bot.state['backend'] = 'file_gateway_v1'; self.bot.save('legacy')
        count = self.api.initialized
        with self.assertRaises(DurabilityError): self.make_broker()
        self.assertEqual(self.api.initialized, count)

    def test_restore_orphaned_journal_blocks_before_api(self):
        self.submit()
        self.bot.state['commands'] = {}
        self.bot.save('older_snapshot')
        count = self.api.initialized
        with self.assertRaises(DurabilityError): self.make_broker()
        self.assertEqual(self.api.initialized, count)

    def test_restore_ready_snapshot_with_sent_journal_blocks(self):
        cmd = self.submit()
        self.bot.state['commands'][cmd['id']]['status'] = 'READY'
        self.bot.save('older_status')
        with self.assertRaises(DurabilityError): self.make_broker()

    def test_restore_missing_snapshot_with_journal_blocks(self):
        self.submit()
        self.store.db.execute('DELETE FROM snapshot')
        count = self.api.initialized
        with self.assertRaises(DurabilityError): self.make_broker()
        self.assertEqual(self.api.initialized, count)

    def test_restore_wrong_currency_and_account_before_initialize(self):
        self.bot.state['currency'] = 'USD'; self.bot.save('old_currency')
        count = self.api.initialized
        with self.assertRaises(DurabilityError): self.make_broker()
        self.assertEqual(self.api.initialized, count)
        self.bot.state['currency'] = 'UST'; self.bot.state['account'] = '222222222@Bybit-Live-6'
        self.bot.save('wrong_account')
        with self.assertRaises(DurabilityError): self.make_broker()
        self.assertEqual(self.api.initialized, count)

    def test_stop_constraint_changes_during_history_read_prevent_send(self):
        original = self.api.history_deals_get
        def changed(*args):
            self.api.info = self.api.info._replace(trade_stops_level=500)
            return original(*args)
        self.api.history_deals_get = changed
        cmd = self.submit()
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')
        self.assertFalse(self.api.sends)

    def test_quote_changes_after_durable_may_send_prevent_order_send(self):
        original = self.broker._save_row
        def changed(cid, row, event):
            original(cid, row, event)
            if event == 'may_send': self.api.tick = self.api.tick._replace(ask=4004.)
        with patch.object(self.broker, '_save_row', side_effect=changed):
            cmd = self.submit()
        self.assertFalse(self.api.sends)
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')

    def test_conflicting_duplicate_order_correlation_blocks(self):
        cmd = self.submit()
        self.api.orders = [self.order(cmd), self.order(cmd, ticket=102)]
        with self.assertRaises(ValueError): self.broker.snapshot()

    def start_controller_pair(self):
        self.bot.state['settings']['INITIAL_SIZE'] = '.01'
        def accept(request):
            if request['action'] == self.api.TRADE_ACTION_PENDING:
                ticket = 100 + len(self.api.sends)
                cmd = self.bot.state['commands'][request['comment']]
                self.api.orders.append(self.order(cmd, ticket=ticket,
                    type=request['type'], price_open=request['price'],
                    sl=request['sl'], tp=request['tp']))
                self.api.response = record(retcode=10008, order=ticket, deal=0)
            elif request['action'] == self.api.TRADE_ACTION_REMOVE:
                order = next(o for o in self.api.orders if o.ticket == request['order'])
                self.api.orders.remove(order)
                self.api.history.append(order._replace(state=2, volume_current=0.))
                self.api.response = record(retcode=10009, order=order.ticket, deal=0)
            elif request['action'] == self.api.TRADE_ACTION_SLTP:
                self.api.positions = [p._replace(sl=request['sl'], tp=request['tp'])
                    if p.ticket == request['position'] else p for p in self.api.positions]
                self.api.response = record(retcode=10009, order=0, deal=0)
        self.api.on_send = accept
        self.bot.command('/start')
        self.api.rates[0]['time'] += 60
        self.bot.tick()
        self.assertEqual(len(self.api.orders), 2)

    def fill_controller_order(self, side=0, volume=.01):
        order = next(o for o in self.api.orders if o.type == (4 if side==0 else 5))
        self.api.orders.remove(order)
        pid = 500 + order.ticket
        self.api.history.append(order._replace(state=4, volume_current=0., position_id=pid))
        self.api.deals.append(record(ticket=1000+order.ticket, order=order.ticket,
            symbol='XAUUSD.s', magic=self.broker.magic, comment=order.comment,
            position_id=pid, entry=0, type=side, reason=3, volume=volume,
            price=order.price_open, profit=0., time_msc=self.api.now*1000))
        self.api.positions.append(record(ticket=2000+order.ticket, identifier=pid,
            symbol='XAUUSD.s', magic=self.broker.magic, type=side, volume=volume,
            price_open=order.price_open, sl=order.sl, tp=order.tp))
        self.api.tick = self.api.tick._replace(bid=order.price_open, ask=order.price_open+.1)

    def test_controller_full_fill_cancels_opposite_and_anchors_ticket(self):
        self.start_controller_pair(); self.fill_controller_order()
        self.api.deals[0] = self.api.deals[0]._replace(price=4003.01)
        self.api.positions[0] = self.api.positions[0]._replace(price_open=4003.01)
        self.api.tick = self.api.tick._replace(bid=4003.01, ask=4003.11)
        for _ in range(6): self.bot.tick()
        self.assertEqual(self.bot.state['phase'], 'POSITION')
        self.assertTrue(self.bot.state['cycle']['position']['reanchored'])
        self.assertFalse(self.api.orders)
        modifications = [r for r in self.api.sends if r['action']==6]
        self.assertEqual(len(modifications), 1)
        self.assertEqual(modifications[0]['position'], self.api.positions[0].ticket)

    def test_controller_partial_fill_pauses_without_refill(self):
        self.start_controller_pair(); self.fill_controller_order(volume=.005)
        for _ in range(4): self.bot.tick()
        self.assertEqual(self.bot.state['phase'], 'SAFE_PAUSE')
        self.assertEqual(len([r for r in self.api.sends if r['action'] in (1,5)]), 2)

    def test_controller_dual_fill_pauses_without_hedge_close(self):
        self.start_controller_pair(); self.fill_controller_order(0); self.fill_controller_order(1)
        for _ in range(3): self.bot.tick()
        self.assertEqual(self.bot.state['phase'], 'SAFE_PAUSE')
        self.assertEqual(len(self.api.positions), 2)
        self.assertEqual(len(self.api.sends), 2)

    def unknown_pair(self, response):
        self.api.response = response
        self.bot.state['settings']['INITIAL_SIZE'] = '.01'
        self.bot.command('/start'); self.api.rates[0]['time'] += 60
        self.bot.tick()
        commands = list(self.bot.state['commands'].values())
        self.assertEqual([c['status'] for c in commands], ['UNKNOWN', 'READY'])
        self.assertEqual(len(self.api.sends), 1)
        for _ in range(3): self.bot.tick()
        self.broker.close()
        self.broker = self.make_broker(); self.addCleanup(self.broker.close)
        self.bot = Controller(self.store, self.broker)
        for _ in range(3): self.bot.tick()
        self.assertEqual(len(self.api.sends), 1)
        self.assertEqual(self.bot.state['commands'][commands[1]['id']]['status'], 'READY')
        first = commands[0]
        self.api.orders = [self.order(first)]
        self.api.response = record(retcode=10008, order=102, deal=0)
        self.bot.tick()
        self.assertEqual(len(self.api.sends), 2)
        self.assertNotEqual(self.api.sends[0]['comment'], self.api.sends[1]['comment'])
        self.assertEqual(self.bot.state['commands'][first['id']]['status'], 'PENDING')

    def test_unknown_first_pair_reply_blocks_second_until_evidence(self):
        self.unknown_pair(None)

    def test_timeout_first_pair_reply_blocks_second_until_evidence(self):
        self.unknown_pair(TimeoutError('lost reply'))

    def test_modify_exact_freeze_boundary_rejected(self):
        self.opening()
        self.api.info = self.api.info._replace(trade_freeze_level=100)
        count = len(self.api.sends)
        cmd = self.submit(dict(id='b'*24, kind='MODIFY', position_id='501',
            sl='4002', tp='4005', purpose='STEP'))
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')
        self.assertEqual(len(self.api.sends), count)
        self.assertIn('freeze', self.broker.result(cmd['id'])['reason'])

    def test_modify_old_protection_inside_freeze_rejected(self):
        self.opening()
        self.api.info = self.api.info._replace(trade_freeze_level=50)
        self.api.positions[0] = self.api.positions[0]._replace(tp=4003.4)
        count = len(self.api.sends)
        cmd = self.submit(dict(id='b'*24, kind='MODIFY', position_id='501',
            sl='4002', tp='4005', purpose='STEP'))
        self.assertEqual(self.broker.result(cmd['id'])['status'], 'REJECTED')
        self.assertEqual(len(self.api.sends), count)

    def test_close_by_deal_kept_as_external_exit(self):
        self.opening()
        entry = self.api.deals[0]
        self.api.deals.append(entry._replace(ticket=302, order=102, entry=3,
            type=1, reason=self.api.DEAL_REASON_TP, profit=-2.))
        exit = self.broker.snapshot()['deals'][-1]
        self.assertEqual(exit['entry'], 'OUT')
        self.assertEqual(exit['reason'], 'EXTERNAL')
        self.assertEqual(exit['sl'], '0')

    def test_native_tp_actual_deal_profit_counted_once_without_fees(self):
        self.start_controller_pair(); self.fill_controller_order()
        for _ in range(5): self.bot.tick()
        pos = self.api.positions[0]
        opening = self.api.deals[0]
        entry_order = self.api.history[0]
        self.api.history.append(entry_order._replace(ticket=777, type=1,
            position_id=pos.identifier, comment='broker tp', price_open=pos.tp))
        self.api.deals.append(record(ticket=778, order=777, symbol='XAUUSD.s',
            magic=self.broker.magic, comment='broker tp', position_id=pos.identifier,
            entry=1, type=1, reason=5, volume=.01, price=pos.tp, profit=.2,
            commission=-9., swap=-5., time_msc=self.api.now*1000+1))
        self.api.positions = []
        for _ in range(4): self.bot.tick()
        self.assertEqual(self.bot.state['phase'], 'COOLDOWN')
        self.assertEqual(len(self.bot.state['completed']), 1)
        self.assertEqual(D(self.bot.state['completed'][0]['realized']), D('.2'))
        self.assertEqual(len(self.broker.snapshot()['deals']), 2)
        restored = Controller(self.store, self.broker); restored.tick()
        self.assertEqual(len(restored.state['completed']), 1)

    def test_exit_sl_evidence_requires_exact_closing_order(self):
        self.opening()
        entry = self.api.deals[0]
        self.api.deals.append(entry._replace(ticket=302, order=102, entry=1, type=1, reason=4, profit=-2.))
        snap = self.broker.snapshot()
        self.assertEqual(snap['deals'][-1]['sl'], '0')
        self.api.history.append(self.api.history[0]._replace(ticket=102, type=1, comment='broker sl', sl=4002.))
        snap = self.broker.snapshot()
        self.assertEqual(D(snap['deals'][-1]['sl']), D(4002))

    def test_inspect_runtime_forces_readonly_and_closes_on_failure(self):
        from tradfi.runtime import _run_mt5
        with patch('tradfi.mt5_adapter.MT5Broker') as factory:
            factory.return_value.preflight.side_effect = OSError('fake unavailable')
            with self.assertRaises(OSError): _run_mt5(self.config, True)
            self.assertFalse(factory.call_args.args[0]['enable_trading'])
            factory.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
