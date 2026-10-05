"""Mock-only initial-entry regressions: no broker, account or Telegram network I/O."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from trader.app import Bot
from trader.capital import CapitalClient, CapitalError
from trader.config import Settings
from trader.engine import Strategy
from trader.initial_entry import InitialTriggerEntry, entry_plan
from trader.model import CycleState
from trader.storage import StorageFailure, database_path


def market(bid="4001.50", ask="4001.70"):
    return {"snapshot": {"bid": bid, "offer": ask, "marketStatus": "TRADEABLE",
                         "decimalPlacesFactor": 2, "scalingFactor": 1, "delayTime": 0},
            "dealingRules": {name: {"unit": "POINTS", "value": value} for name, value in
                             (("minStepDistance", "0.01"), ("minDealSize", "0.1"),
                              ("maxDealSize", "100"), ("minSizeIncrement", "0.1"))}}


class Broker:
    def __init__(self):
        self.market = market()
        self.candles = ({"id": "2026-10-05T10:00:00+00:00", "open": "4000", "range": "3"},
                        {"id": "2026-10-05T10:01:00+00:00", "open": "4010", "range": "0.5"})
        self.candle_calls = 0
        self.live_orders = {}
        self.live_positions = {}
        self.confirms = {}
        self.history = []
        self.posts = []
        self.cancels = []
        self.markets = []
        self.post_failure = {}
        self.market_failure = None
        self.cancel_failure = None
        self.cancel_race = False
        self.confirm_timeout = False
        self.empty_snapshot = False
        self.barrier = None
        self.on_post = None
        self.lock = threading.Lock()

    def entry_candles(self, *_):
        self.candle_calls += 1
        return deepcopy(self.candles)

    def initial_entry_market(self, *_):
        return deepcopy(self.market)

    def positions(self):
        return [] if self.empty_snapshot else deepcopy(list(self.live_positions.values()))

    def working_orders(self):
        return [] if self.empty_snapshot else deepcopy(list(self.live_orders.values()))

    def activity(self, *args, **kwargs):
        return deepcopy(self.history)

    def initial_working_order(self, epic, side, size, level, order_type):
        if self.on_post:
            self.on_post()
        if self.barrier:
            self.barrier.wait(timeout=3)
        with self.lock:
            index = len(self.posts) + 1
            oid, reference = f"order-{index}", f"ref-{index}"
            self.posts.append((side, size, level, order_type))
            failure = self.post_failure.get(side)
            if failure:
                raise failure
            self.live_orders[oid] = {"epic": epic, "direction": side, "dealId": oid,
                                     "dealReference": reference, "orderSize": size,
                                     "orderLevel": level, "type": order_type}
            self.confirms[reference] = {"dealStatus": "ACCEPTED", "dealId": oid, "status": "OPEN"}
            return reference

    def confirmation(self, reference):
        if self.confirm_timeout:
            raise CapitalError("mock GET timeout")
        return deepcopy(self.confirms.get(reference, {}))

    def fill(self, side, price="4003.50", size="10", *, keep_order=False):
        oid, order = next((k, o) for k, o in self.live_orders.items() if o["direction"] == side)
        pid = "position-" + oid
        self.live_positions[pid] = {"dealId": pid, "epic": "GOLD", "direction": side,
                                    "workingOrderId": oid, "level": D(price), "size": D(size)}
        if not keep_order:
            self.live_orders.pop(oid)
        return pid

    def initial_cancel_order(self, oid):
        self.cancels.append(oid)
        if self.cancel_race:
            self.cancel_race = False
            self.fill(self.live_orders[oid]["direction"])
            raise CapitalError("mock not found", http_status=404, error_code="error.not-found.dealId")
        if self.cancel_failure:
            raise self.cancel_failure
        self.live_orders.pop(oid, None)
        reference = "cancel-" + oid
        self.confirms[reference] = {"dealStatus": "ACCEPTED", "status": "DELETED", "dealId": oid}
        return reference

    def cancel_externally(self, side):
        oid, order = next((k, o) for k, o in self.live_orders.items() if o["direction"] == side)
        self.live_orders.pop(oid)
        self.history.append({"dealId": oid, "type": "WORKING_ORDER", "status": "CANCELLED",
                             "dateUTC": "2026-10-05T10:02:00Z"})

    def open_position(self, epic, side, size, *args, **kwargs):
        self.markets.append((side, size, args, kwargs))
        if self.market_failure:
            raise self.market_failure
        pid, ref = "market-position", "market-reference"
        self.live_positions[pid] = {"dealId": pid, "dealReference": ref, "epic": epic,
                                    "direction": side, "level": D("4003.75"), "size": size}
        self.confirms[ref] = {"dealId": pid, "dealStatus": "ACCEPTED", "status": "OPEN"}
        return ref


class InitialEntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Settings(initial_trigger_entry_enabled=True, dry_run=False,
                            size=D("10"), state_file=str(Path(self.tmp.name)/"state.json"),
                            diagnostic_log_file=str(Path(self.tmp.name)/"diagnostic.log"))
        self.broker = Broker()
        self.protections = []
        self.bot = self.make_bot(CycleState(armed=True))
        self.addCleanup(patch.stopall)
        patch("trader.initial_entry.begin_diagnostic_cycle").start()

    def make_bot(self, state):
        bot = Bot.__new__(Bot)
        bot.cfg, bot.state, bot.capital = self.cfg, state, self.broker
        bot.strategy = Strategy(self.cfg, state)
        bot.reconciled = True
        bot._send_report = Mock()
        bot.telegram = Mock()
        def protection(leg):
            self.assertTrue(bot.state.long.deal_id and bot.state.short.deal_id)
            self.assertFalse(bot.state.initial_entry)
            self.protections.append((leg.direction, leg.stop, leg.take_profit))
            p = self.broker.live_positions[leg.deal_id]
            p.update(stopLevel=leg.stop, profitLevel=leg.take_profit)
            return True
        bot._apply_protection = protection
        return bot

    def restart(self, *, flag=None):
        if flag is not None:
            self.cfg = replace(self.cfg, initial_trigger_entry_enabled=flag)
        self.bot = self.make_bot(CycleState.load(self.cfg.state_file))
        self.bot.reconciled = False
        self.bot.reconcile_startup()

    def ticks(self, number=1):
        for _ in range(number):
            self.bot.tick()

    def submit(self):
        self.bot._tick_filter()
        self.ticks(4)
        self.assertEqual(len(self.broker.posts), 2)

    def test_disabled_and_absent_flag_keep_legacy_market_route(self):
        for cfg in (Settings(), replace(self.cfg, initial_trigger_entry_enabled=False)):
            self.bot.cfg = cfg
            self.broker.candle_ranges = Mock(return_value=(D(3), D(0)))
            self.bot._start_cycle = Mock()
            self.bot._tick_filter()
            self.bot._start_cycle.assert_called_once_with("закрытая свеча: 3")
            self.assertEqual(self.broker.candle_calls, 0)
            self.assertFalse(self.bot.state.initial_entry)

    def test_enabled_flag_does_not_intercept_continuation_filter(self):
        self.bot.state.continuation_managed = True
        self.broker.candle_ranges = Mock(return_value=(D(3), D(0)))
        starter = Mock()
        self.bot._tick_filter(starter)
        starter.assert_called_once()
        self.assertEqual(self.broker.candle_calls, 0)

    def test_direction_boundaries_and_exact_same_level_types(self):
        for bid, ask, level, types in [
            ("4001", "4001.2", "4003", {"BUY": "STOP", "SELL": "LIMIT"}),
            ("4001.50", "4001.70", "4003.50", {"BUY": "STOP", "SELL": "LIMIT"}),
            ("3999", "3999.2", "3997", {"BUY": "LIMIT", "SELL": "STOP"}),
            ("3998.50", "3998.70", "3996.50", {"BUY": "LIMIT", "SELL": "STOP"}),
        ]:
            with self.subTest(bid=bid):
                plan = entry_plan(D(4000), market(bid, ask), D(1), D(2), D(10))
                self.assertEqual(D(plan["level"]), D(level))
                self.assertEqual(plan["types"], types)
        for bid in ("3999.01", "4000", "4000.99"):
            self.assertIsNone(entry_plan(D(4000), market(bid, str(D(bid)+D('.2'))), D(1), D(2), D(10)))

    def test_rollover_keeps_approved_open_and_never_rechecks_filter(self):
        self.broker.market = market("4000.50", "4000.70")
        self.bot._tick_filter()
        self.ticks(5)
        self.assertFalse(self.broker.posts)
        self.broker.candles = ({"id": "next", "open": "4500", "range": "0"},)*2
        self.broker.market = market("3998.50", "3998.70")
        self.restart()
        self.ticks(2)
        self.assertEqual(self.broker.candle_calls, 1)
        self.assertEqual(self.bot.state.initial_entry["candle"]["open"], "4000")
        self.assertEqual({p[2] for p in self.broker.posts}, {D("3996.50")})
        self.assertEqual({p[0]:p[3] for p in self.broker.posts}, {"BUY":"LIMIT", "SELL":"STOP"})

    def test_current_candle_approval_uses_current_open(self):
        self.bot.state.waiting_current_candle = True
        self.broker.candles = (self.broker.candles[0], {"id": "current", "open":"3990", "range":"3"})
        self.bot._tick_filter()
        self.assertEqual(self.bot.state.initial_entry["candle"]["open"], "3990")
        self.assertEqual(self.bot.state.initial_entry["filter_source"], "current")

    def test_parallel_posts_have_both_durable_intents_and_no_protection(self):
        self.broker.barrier = threading.Barrier(2)
        snapshots = []
        def read_intents():
            with sqlite3.connect(database_path(self.cfg.state_file)) as db:
                snapshots.append(json.loads(db.execute("SELECT payload FROM state_snapshot").fetchone()[0]))
        self.broker.on_post = read_intents
        self.submit()
        self.assertEqual(len(snapshots), 2)
        for snapshot in snapshots:
            self.assertEqual(set(snapshot["initial_entry"]["orders"]), {"BUY", "SELL"})
            self.assertTrue(all(o["status"] == "UNKNOWN" for o in snapshot["initial_entry"]["orders"].values()))
        self.assertFalse(self.protections)
        self.assertEqual({p[2] for p in self.broker.posts}, {D("4003.50")})

    def test_both_fill_orders_wait_unprotected_then_enter_s1_once(self):
        for first in ("BUY", "SELL"):
            with self.subTest(first=first):
                self.broker = Broker()
                self.bot = self.make_bot(CycleState(armed=True))
                self.protections.clear()
                self.submit()
                self.broker.fill(first, "4003.55")
                self.ticks(5)
                self.assertFalse(self.protections)
                self.assertFalse(self.broker.markets)
                self.assertEqual(self.bot.state.general_recovery, 0)
                self.broker.market = market("4010", "4010.2")
                self.restart(flag=False)  # persisted owner survives flag change
                self.assertFalse(self.protections)
                other = "SELL" if first == "BUY" else "BUY"
                self.broker.fill(other, "4003.45")
                self.ticks()
                self.assertEqual(self.bot.state.scenario, 1)
                self.assertEqual(self.bot.state.general_recovery, D("4"))
                self.assertEqual(len(self.protections), 2)
                self.assertFalse(self.bot.state.initial_entry)
                self.assertFalse(self.bot.state.scenario_transitions)
                self.ticks(3)
                self.assertEqual(len(self.broker.posts), 2)
                self.assertEqual(len(self.bot.state.recovery_events), 1)
                self.cfg = replace(self.cfg, initial_trigger_entry_enabled=True)

    def test_rejected_before_fills_cancels_other_and_requotes_original_anchor(self):
        self.broker.post_failure["SELL"] = CapitalError("no", outcome="REJECTED")
        self.submit()
        for _ in range(6):
            self.ticks()
            if self.bot.state.initial_entry["stage"] == "DIRECTION":
                break
        self.assertEqual(len(self.broker.cancels), 1)
        self.assertEqual(len(self.broker.posts), 2)
        self.broker.post_failure.clear()
        self.broker.market = market("3998.5", "3998.7")
        self.ticks(5)
        self.assertEqual(len(self.broker.posts), 4)
        self.assertEqual(self.broker.posts[-1][2], D("3996.5"))
        self.assertEqual(self.broker.candle_calls, 1)
        self.assertFalse(self.broker.markets)

    def test_cancel_races_fill_fallback_only_missing_side(self):
        self.broker.post_failure["SELL"] = CapitalError("no", outcome="REJECTED")
        self.broker.cancel_race = True
        self.submit()
        self.ticks(3)
        self.assertEqual([m[0] for m in self.broker.markets], ["SELL"])
        self.assertEqual(self.broker.markets[0][2:], ((), {}))
        self.assertEqual(len(self.protections), 2)
        self.assertEqual(self.bot.state.scenario, 1)
        self.assertEqual(len(self.broker.posts), 2)

    def test_waiting_order_never_permits_market_but_confirmed_cancel_does(self):
        self.submit()
        self.broker.fill("SELL")
        self.ticks(8)
        self.assertFalse(self.broker.markets)
        self.broker.cancel_externally("BUY")
        self.ticks(3)
        self.assertEqual([m[0] for m in self.broker.markets], ["BUY"])
        self.assertEqual(self.bot.state.scenario, 1)

    def test_explicit_fallback_rejection_requires_start_no_recursive_storm(self):
        self.submit()
        self.broker.fill("BUY")
        self.broker.cancel_externally("SELL")
        self.broker.market_failure = CapitalError("invalid", outcome="REJECTED")
        self.ticks(10)
        self.assertEqual(len(self.broker.markets), 1)
        self.restart()
        self.ticks(5)
        self.assertEqual(len(self.broker.markets), 1)
        self.broker.market_failure = None
        self.bot.command('/start')
        self.ticks(2)
        self.assertEqual(len(self.broker.markets), 2)
        self.assertEqual(self.bot.state.scenario, 1)

    def test_stop_cancel_race_rejected_fallback_can_resume_after_restart(self):
        self.submit()
        self.bot.command('/stop')
        self.ticks()  # accepted cancellation of first order
        self.broker.cancel_race = True
        self.broker.market_failure = CapitalError('rejected', outcome='REJECTED')
        self.ticks(5)
        self.assertEqual(len(self.broker.markets), 1)
        self.assertFalse(self.protections)
        self.restart()
        self.broker.market_failure = None
        self.bot.command('/start')
        self.ticks(3)
        self.assertEqual(len(self.broker.markets), 2)
        self.assertEqual(self.bot.state.scenario, 1)
        self.assertFalse(self.bot.state.paused)

    def test_current_position_size_overrides_old_opening_history(self):
        self.submit()
        buy = self.broker.fill('BUY', size='5')
        self.broker.fill('SELL', size='5')
        oid = self.broker.live_positions[buy]['workingOrderId']
        self.broker.history.append({
            'dealId': buy, 'type': 'POSITION', 'status': 'ACCEPTED', 'source': 'USER',
            'dateUTC': '2026-10-05T10:02:00Z',
            'details': {'workingOrderId': oid, 'direction': 'BUY',
                        'level': '4003.50', 'size': '10'},
        })
        self.ticks()
        self.assertEqual(self.bot.state.initial_position_size, D('5'))
        self.assertEqual(len(self.protections), 2)
        self.assertFalse(self.broker.markets)

    def test_unknown_initial_post_without_reference_never_reposts_after_restart(self):
        self.broker.post_failure["BUY"] = CapitalError("timeout")
        self.submit()
        self.ticks(5)
        self.restart()
        self.bot.command('/stop')
        self.ticks(8)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertTrue(self.bot.state.initial_entry)
        self.assertFalse(self.broker.markets)

    def test_confirmation_timeout_and_empty_snapshot_are_not_rejection(self):
        self.broker.confirm_timeout = True
        self.submit()
        self.broker.empty_snapshot = True
        self.ticks(5)
        self.restart()
        self.assertEqual(len(self.broker.posts), 2)
        self.assertFalse(self.broker.markets)
        self.assertFalse(self.broker.cancels)
        self.broker.empty_snapshot = False
        self.broker.confirm_timeout = False
        self.ticks()
        self.assertTrue(all(o['status']=='PENDING' for o in self.bot.state.initial_entry['orders'].values()))

    def test_cancel_timeout_survives_restart_and_history_resolves_it(self):
        self.submit()
        self.broker.cancel_failure = CapitalError("timeout")
        self.bot.command('/stop')
        self.ticks(3)
        self.restart()
        self.ticks(3)
        self.assertEqual(len(self.broker.cancels), 2)
        self.assertTrue(self.bot.state.initial_entry)
        self.broker.cancel_externally('BUY')
        self.broker.cancel_externally('SELL')
        self.ticks(4)
        self.assertFalse(self.bot.state.initial_entry)
        self.assertEqual(self.bot.state.phase, 'PAUSED')

    def test_market_timeout_does_not_duplicate(self):
        self.submit()
        self.broker.fill('BUY')
        self.broker.cancel_externally('SELL')
        self.broker.market_failure = CapitalError('timeout')
        self.ticks(5)
        self.restart()
        self.ticks(5)
        self.assertEqual(len(self.broker.markets), 1)
        self.assertFalse(self.protections)

    def test_stop_before_submission_requires_new_start_and_filter(self):
        self.bot._tick_filter()
        self.bot.command('/stop')
        self.restart()
        self.assertEqual(self.bot.state.phase, 'PAUSED')
        self.assertFalse(self.broker.posts)
        self.bot.command('/start')
        self.ticks()
        self.assertEqual(self.broker.candle_calls, 2)

    def test_stop_before_fills_cancels_both_and_stays_paused_after_restart(self):
        self.submit()
        self.bot.command('/stop')
        self.restart()
        self.ticks(8)
        self.assertEqual(len(self.broker.cancels), 2)
        self.assertEqual(self.bot.state.phase, 'PAUSED')
        self.assertFalse(self.bot.state.active)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertFalse(self.broker.markets)

    def test_stop_after_first_fill_waits_other_then_handoff_keeps_pause(self):
        self.submit()
        self.broker.fill('BUY')
        self.bot.command('/stop')
        self.restart()
        self.ticks(3)
        self.assertFalse(self.broker.cancels)
        self.assertFalse(self.protections)
        self.broker.fill('SELL')
        self.ticks()
        self.assertTrue(self.bot.state.paused)
        self.assertEqual(self.bot.state.scenario, 1)

    def test_stop_cancel_race_keeps_other_waiting(self):
        self.submit()
        self.broker.cancel_race = True
        self.bot.command('/stop')
        self.ticks(5)
        self.assertEqual(len(self.broker.cancels), 1)
        self.assertFalse(self.broker.markets)
        remaining_side = next(iter(self.broker.live_orders.values()))['direction']
        self.broker.fill(remaining_side)
        self.ticks()
        self.assertTrue(self.bot.state.paused)
        self.assertEqual(self.bot.state.scenario, 1)

    def test_stop_race_with_previously_cancelled_other_uses_missing_market(self):
        self.submit()
        self.bot.command('/stop')
        self.ticks()  # first DELETE accepted, reference persisted
        self.broker.cancel_race = True
        self.ticks(4)
        self.assertEqual(len(self.broker.markets), 1)
        self.assertTrue(self.bot.state.paused)
        self.assertEqual(self.bot.state.scenario, 1)

    def test_invalid_market_level_is_reported_without_rounding_or_post(self):
        self.bot._tick_filter()
        self.broker.market = market('4001.5', '4004')
        self.ticks(6)
        self.assertFalse(self.broker.posts)
        self.assertIn('spread', self.bot.state.initial_entry['notice'])
        self.assertEqual(self.broker.candle_calls, 1)

    def test_partial_fill_does_not_trigger_cancellation_or_premature_protection(self):
        self.submit()
        self.broker.fill('BUY', size='5', keep_order=True)
        self.bot.command('/stop')
        self.ticks(5)
        self.assertFalse(self.broker.cancels)
        self.assertFalse(self.protections)
        self.assertFalse(self.broker.markets)

    def test_storage_failure_before_pair_prevents_both_posts(self):
        self.bot._tick_filter()
        self.ticks(2)
        original = self.bot.state.save
        def save(path):
            if self.bot.state.initial_entry.get('orders'):
                raise StorageFailure('mock commit failure')
            original(path)
        self.bot.state.save = save
        with self.assertRaises(StorageFailure):
            self.ticks()
        self.assertFalse(self.broker.posts)

    def test_reset_does_not_erase_unknown_initial_owner(self):
        self.submit()
        with self.assertRaises(RuntimeError):
            self.bot.command('/resetcycle')
        self.assertTrue(self.bot.state.initial_entry)


class InitialEntryApiTest(unittest.TestCase):
    def test_payloads_have_types_and_never_sl_tp(self):
        client = CapitalClient.__new__(CapitalClient)
        client.request = Mock(return_value={'dealReference':'reference'})
        for side, kind in [('BUY','STOP'),('SELL','LIMIT'),('BUY','LIMIT'),('SELL','STOP')]:
            client.initial_working_order('GOLD', side, D(10), D('4003.5'), kind)
            body = client.request.call_args.kwargs['json']
            self.assertEqual(body['type'], kind)
            self.assertEqual(set(body), {'epic','direction','size','level','type','guaranteedStop'})
        client.initial_cancel_order('owned-order')
        self.assertEqual(client.request.call_args.args, ('DELETE','/workingorders/owned-order'))

    def test_candle_context_aggregates_bid_open_in_same_snapshot(self):
        client = CapitalClient.__new__(CapitalClient)
        prices = []
        for minute, opening in [(0, '4000'),(1,'4001'),(2,'4002')]:
            prices.append({'snapshotTimeUTC':f'2026-10-05T10:0{minute}:00',
                           'openPrice':{'bid':opening}, 'highPrice':{'bid':str(D(opening)+2)},
                           'lowPrice':{'bid':opening}})
        client.request = Mock(return_value={'prices':prices[::-1]})
        closed,current = client.entry_candles('GOLD',2)
        self.assertEqual(closed['open'],'4000')
        self.assertEqual(closed['range'],'3')
        self.assertEqual(current['open'],'4002')
        client.request.assert_called_once()

    def test_invalid_precision_size_rules_and_closed_market_block(self):
        cases = []
        bad=market(); bad['snapshot']['marketStatus']='CLOSED';cases.append(bad)
        bad=market();bad['snapshot']['scalingFactor']=100;cases.append(bad)
        bad=market();bad['dealingRules']['minStepDistance']['unit']='UNKNOWN';cases.append(bad)
        bad=market();bad['dealingRules']['maxDealSize']['value']='5';cases.append(bad)
        bad=market('4001.501','4001.701');cases.append(bad)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                entry_plan(D(4000),value,D(1),D(2),D(10))

    def test_hedging_preferences_are_read_only_and_required(self):
        client=CapitalClient.__new__(CapitalClient)
        client.request=Mock(return_value={'hedgingMode':False})
        with self.assertRaises(CapitalError):client.initial_entry_market('GOLD')
        client.request.assert_called_once_with('GET','/accounts/preferences')

    def test_config_flag_defaults_and_environment_values(self):
        with patch.dict('os.environ', {'BOT_CONFIG_FILE':'/nonexistent/mock-config'}, clear=True):
            self.assertFalse(Settings.from_env().initial_trigger_entry_enabled)
        with patch.dict('os.environ', {'BOT_CONFIG_FILE':'/nonexistent/mock-config',
                                      'INITIAL_TRIGGER_ENTRY_ENABLED':'true',
                                      'INITIAL_ENTRY_DIRECTION_DISTANCE':'1.5',
                                      'INITIAL_ENTRY_ORDER_OFFSET':'2.5'}, clear=True):
            cfg=Settings.from_env()
            self.assertTrue(cfg.initial_trigger_entry_enabled)
            self.assertEqual(cfg.initial_entry_direction_distance,D('1.5'))
            self.assertEqual(cfg.initial_entry_order_offset,D('2.5'))
        for value in (D(0),D(-1),D('NaN'),D('Infinity')):
            with self.assertRaises(ValueError):replace(Settings(),initial_entry_order_offset=value).validate()
