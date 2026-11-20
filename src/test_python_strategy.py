"""Offline broker and independent report fixtures for the production Python strategy."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N

from recovery_strategy import RecoveryStrategy


class Native:
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TYPE_BUY_STOP_LIMIT = 6
    ORDER_TYPE_SELL_STOP_LIMIT = 7
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    ORDER_REASON_CLIENT = 0
    ORDER_REASON_MOBILE = 1
    ORDER_REASON_WEB = 2
    DEAL_REASON_SL = 4
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3
    ORDER_STATE_PLACED = 1
    ORDER_STATE_CANCELED = 2
    ORDER_STATE_FILLED = 4
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_INVALID_PRICE = 10015
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    TRADE_ACTION_MODIFY = 7
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2

    def __init__(self):
        self.n = 100
        self.time = 1000
        self.p = {}
        self.o = {}
        self.h = {}
        self.d = []
        self.requests = []
        self.lost = False
        self.checkcode = 0
        self.external = True
        self.quote = N(bid=100.00, ask=100.02)
        self.move_result = None

    def account_info(self):
        return N(login=17, server="test", margin_mode=2, trade_allowed=True, trade_expert=True)

    def terminal_info(self):
        return N(connected=True, trade_allowed=True, tradeapi_disabled=not self.external)

    def symbol_info(self, s):
        return N(
            trade_tick_size=0.01,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=5.00,
            point=0.01,
            trade_stops_level=2,
            trade_freeze_level=0,
        )

    def symbol_info_tick(self, symbol):
        return self.quote

    def next(self):
        self.n += 1
        return self.n

    def at(self):
        self.time += 100
        return self.time

    def positions_get(self, symbol=None, ticket=None):
        return [p for p in self.p.values() if not ticket or ticket == p.ticket]

    def orders_get(self, symbol=None, ticket=None):
        return [o for o in self.o.values() if not ticket or ticket == o.ticket]

    def history_orders_get(self, *args, ticket=None, group=None):
        if args:
            return list(self.h.values())
        return [self.h[ticket]] if ticket in self.h else []

    def history_deals_get(self, position):
        return [d for d in self.d if d.position_id == position]

    def order_check(self, q):
        hook = getattr(self, "check_hook", None)
        if hook:
            self.check_hook = None
            hook(q)
        return N(retcode=self.checkcode)

    def pending(self, kind, at, sl=0.0, volume=0.01, magic=0, reason=0, comment=""):
        ticket = self.next()
        o = N(
            ticket=ticket,
            type=kind,
            state=1,
            position_id=0,
            magic=magic,
            symbol="X",
            reason=reason,
            volume_initial=volume,
            volume_current=volume,
            price_open=float(at),
            sl=float(sl),
            tp=0.0,
            price_stoplimit=0.0,
            type_time=0,
            time_expiration=0,
            time_setup_msc=self.at(),
            comment=comment,
        )
        self.o[ticket] = o
        return o

    def fill(self, ticket, at=None):
        o = self.o.pop(ticket)
        o.state = 4
        o.volume_current = 0.0
        o.position_id = self.next()
        o.time_done_msc = self.at()
        self.h[ticket] = o
        at = o.price_open if at is None else float(at)
        side = 0 if o.type in (0, 2, 4, 6) else 1
        p = N(
            identifier=o.position_id,
            ticket=o.position_id + 500,
            magic=o.magic,
            reason=o.reason,
            time_msc=self.at(),
            type=side,
            volume=o.volume_initial,
            price_open=at,
            sl=o.sl,
            tp=o.tp,
            symbol="X",
        )
        self.p[p.ticket] = p
        self.d.append(
            N(
                ticket=self.next(),
                order=o.ticket,
                position_id=p.identifier,
                magic=o.magic,
                symbol="X",
                type=side,
                entry=0,
                reason=o.reason,
                time_msc=p.time_msc,
                volume=p.volume,
                price=at,
                sl=o.sl,
            )
        )
        return p

    def market(self, side=0, entry=100.0, sl=0.0):
        return self.fill(self.pending(side, entry, sl).ticket, entry)

    def closep(self, p, reason, price=None):
        o = self.pending(1 - p.type, p.price_open, volume=p.volume)
        self.o.pop(o.ticket)
        o.state = 4
        o.volume_current = 0.0
        o.position_id = p.identifier
        o.time_done_msc = self.at()
        self.h[o.ticket] = o
        self.d.append(
            N(
                ticket=self.next(),
                order=o.ticket,
                position_id=p.identifier,
                magic=0,
                symbol="X",
                type=1 - p.type,
                entry=1,
                reason=reason,
                time_msc=self.at(),
                volume=p.volume,
                price=price or p.price_open,
                sl=p.sl,
            )
        )
        self.p.pop(p.ticket)

    def cancel(self, ticket):
        o = self.o.pop(ticket)
        o.state = 2
        o.time_done_msc = self.at()
        self.h[ticket] = o

    def order_send(self, q):
        self.requests.append(dict(q))
        ticket = 0
        if q["action"] == 5:
            ticket = self.pending(
                q["type"], q["price"], q["sl"], q["volume"], q["magic"], 3, q["comment"]
            ).ticket
        elif q["action"] == 6:
            self.p[q["position"]].sl = q["sl"]
        elif q["action"] == 7:
            if self.move_result is not None:
                return N(retcode=self.move_result, order=0)
            o = self.o[q["order"]]
            o.price_open = q["price"]
            o.sl = q["sl"]
        elif q["action"] == self.TRADE_ACTION_REMOVE:
            ticket = q["order"]
            self.cancel(ticket)
        else:
            raise AssertionError("unexpected trade operation")
        if self.lost:
            self.lost = False
            raise TimeoutError()
        return N(retcode=self.TRADE_RETCODE_DONE, order=ticket)


class Verifier:
    scope = "a" * 32

    def __init__(self, api):
        self.a = api
        self.run = ""
        self.proven = {}
        self.registry = []
        self.trade_allowed = True
        self.complete = True
        self.disagree = False

    def snapshot(self):
        a = self.a
        pp = [
            dict(
                position_id=p.identifier,
                ticket=p.ticket,
                magic=p.magic,
                reason=p.reason,
                opened_msc=p.time_msc,
                side="BUY" if p.type == 0 else "SELL",
                volume_steps=round(p.volume * 100),
                entry_ticks=round(p.price_open * 100),
                sl_ticks=round(p.sl * 100),
                tp_ticks=round(p.tp * 100),
            )
            for p in a.p.values()
        ]
        oo = [
            dict(
                ticket=o.ticket,
                position_id=o.position_id,
                magic=o.magic,
                type=o.type,
                state=o.state,
                reason=o.reason,
                time_msc=o.time_setup_msc,
                initial_steps=round(o.volume_initial * 100),
                remaining_steps=round(o.volume_current * 100),
                price_ticks=round(o.price_open * 100),
                sl_ticks=round(o.sl * 100),
                tp_ticks=round(o.tp * 100),
                stoplimit_ticks=0,
            )
            for o in a.o.values()
        ]
        return dict(
            login=17, server="test", symbol="X", tick_size="0.01", volume_step="0.01", positions=pp, orders=oo
        )

    def history(self, pid):
        return dict(
            login=17,
            server="test",
            symbol="X",
            position_id=pid,
            deals=[
                dict(
                    ticket=d.ticket,
                    order=d.order,
                    position_id=pid,
                    magic=d.magic,
                    entry=d.entry,
                    type=d.type,
                    reason=d.reason,
                    time_msc=d.time_msc,
                    volume_steps=round(d.volume * 100),
                    price_ticks=round(d.price * 100),
                    sl_ticks=round(d.sl * 100),
                )
                for d in self.a.d
                if d.position_id == pid and (self.complete or d.entry != 1)
            ],
        )

    def order(self, ticket):
        o = self.a.h[ticket]
        row = dict(
            ticket=o.ticket,
            position_id=o.position_id,
            magic=o.magic,
            type=o.type,
            state=o.state,
            reason=o.reason,
            time_msc=o.time_setup_msc,
            done_msc=o.time_done_msc,
            initial_steps=round(o.volume_initial * 100),
            remaining_steps=round(o.volume_current * 100),
            price_ticks=round(o.price_open * 100),
            sl_ticks=round(o.sl * 100),
            tp_ticks=round(o.tp * 100),
            stoplimit_ticks=round(o.price_stoplimit * 100),
        )
        if self.disagree and o.state == 2:
            row["remaining_steps"] += 1
        return dict(login=17, server="test", symbol="X", order=row)

    def sl_status(self):
        return dict(instance='synthetic-sl', run_id=self.run, trade_allowed=self.trade_allowed, rows=list(self.proven.values()))

    def cap_status(self):
        return dict(instance='synthetic-sl', run_id=self.run, rows=[])

    def register(self, rows, run_id):
        self.registry = rows
        self.run = run_id

    def breakeven(self, p):
        reg = next(r for r in self.registry if r["position_id"] == p.identifier)
        p.sl = p.price_open
        self.proven[p.identifier] = dict(reg, sl_ticks=reg["entry_ticks"], status="confirmed")


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "strategy.db"
        self.api = Native()
        self.helper = Verifier(self.api)
        self.open()

    def open(self):
        self.e = RecoveryStrategy(
            self.api, self.helper, account=17, server="test", symbol="X", magic=99, database=self.path
        )
        self.e.tick()

    def tearDown(self):
        self.e.close()
        self.tmp.cleanup()

    def arm(self, mode="manual_wait"):
        self.assertTrue(self.e.control("start"))
        self.assertTrue(self.e.control(mode))

    def tick(self, n=1):
        for _ in range(n):
            self.e.tick()

    def owned(self):
        return [o for o in self.api.o.values() if o.magic == 99]

    def adopt(self, side=0):
        self.arm()
        p = self.api.market(side)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["mode"], "active")
        return p, self.owned()[0]

    def test_cap_initial_market_uses_limit_and_preserves_recovery_distance(self):
        for dist, cap in (("1.20", 80), ("0.50", 50)):
            with self.subTest(distance=dist):
                settings = dict(self.e.s["settings"], initial_sl_distance=dist)
                self.assertTrue(self.e.update_settings(settings))
                p, counter = self.adopt()
                self.assertEqual(p.sl, 100 - cap / 100)
                self.assertEqual(counter.price_open, 100 - float(dist))
                self.assertEqual(self.helper.registry[0]["risk_cap_ticks"], 10000 - cap)
                self.api.closep(p, 0)
                self.tick(4)
                self.assertTrue(self.e.control("manual_cancel"))
                self.assertTrue(self.e.control("pause"))

    def test_cap_lease_for_manual_successor_and_detached(self):
        self.arm()
        p = self.api.market(0, 100, 97.0)
        self.tick(3)
        self.assertEqual(self.helper.registry[0]["risk_cap_ticks"], 9920)
        reverse = self.owned()[0]
        self.assertEqual(reverse.price_open, 97)
        nxt = self.api.fill(reverse.ticket, 96.5)
        self.tick(3)
        self.assertEqual({r["position_id"]: r["risk_cap_ticks"] for r in self.helper.registry},
                         {p.identifier: 9920, nxt.identifier: 9730})
        self.assertFalse(self.e.s["leg"]["be_proven"])
        self.assertFalse(any(q["action"] == self.api.TRADE_ACTION_SLTP for q in self.api.requests))

    def test_cap_market_pending_adopts_actual_fill(self):
        self.arm("manual_pending_wait")
        o = self.api.pending(self.api.ORDER_TYPE_BUY_STOP, 102, sl=101.2)
        self.tick()
        p = self.api.fill(o.ticket, 102.5)
        self.tick(3)
        self.assertEqual(self.e.s["leg"]["initial"], 10120)
        self.assertEqual(self.e.s["leg"]["risk_cap_ticks"], 10170)
        self.assertEqual(self.helper.registry[0]["position_id"], p.identifier)

    def test_replaced_cap_helper_blocks_new_bind_but_preserves_existing_recovery(self):
        self.arm()
        self.helper.sl_status = lambda: dict(instance="another", run_id=self.helper.run, rows=[],
                                              trade_allowed=True)
        self.api.market()
        self.tick()
        self.assertIsNone(self.e.s["leg"])
        self.assertEqual(self.e.reason, "sl_cap_helper_unavailable")
        self.helper.cap_status = lambda: dict(instance="another", rows=[])
        self.tick(3)
        p = next(iter(self.api.p.values()))
        first = self.owned()[0]
        self.helper.sl_status = lambda: dict(instance="third", run_id=self.helper.run, rows=[],
                                              trade_allowed=True)
        self.api.closep(p, 4)
        self.api.fill(first.ticket)
        self.tick(4)
        self.assertEqual(self.e.s["leg"]["rung"], 2)
        self.assertEqual(len(self.owned()), 1)

    def test_cap_rounds_towards_entry(self):
        from decimal import Decimal
        self.e.b.tick = Decimal("0.25")
        cfg = dict(self.e.s["settings"], initial_sl_distance="1.25", support_threshold="0.50")
        self.e._grid(cfg)
        self.assertEqual(self.e.cap_distance, 3)
        self.assertEqual(self.e._risk_cap({"entry_ticks": 400, "side": "BUY"}), 397)

    def test_market_full_sl_reflected_then_manual_close_and_cancel_rearms(self):
        p, reverse = self.adopt(1)
        self.assertEqual(
            (p.sl, reverse.type, reverse.price_open, reverse.sl, reverse.volume_initial),
            (100.8, 4, 100.8, 100.0, 0.01),
        )
        self.api.closep(p, 4, 100.86)
        q = self.api.fill(reverse.ticket, 100.83)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["step"], 2)
        nxt = self.owned()[0]
        self.assertEqual((nxt.type, nxt.price_open, nxt.sl), (5, 100.03, 100.83))
        self.api.closep(q, 0)
        self.tick()
        self.assertEqual(self.e.snapshot()["mode"], "active")
        new = self.api.market(0, 101.0)
        self.assertNotIn(nxt.ticket, self.api.o)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["position_ticket"], new.ticket)
        self.assertEqual(self.e.snapshot()["step"], 1)

    def test_cancel_active_keeps_protection_and_no_future_rearm(self):
        p, o = self.adopt()
        self.assertTrue(self.e.control("manual_cancel"))
        self.tick()
        self.assertFalse(self.e.snapshot()["market_rearm_enabled"])
        self.assertIn(o.ticket, self.api.o)
        self.api.cancel(o.ticket)
        self.api.closep(p, 0)
        self.tick()
        self.assertEqual(self.e.snapshot()["mode"], "ready")
        self.api.market()
        self.tick(3)
        self.assertEqual(len(self.owned()), 0)

    def test_support_successor_preserves_existing_ticket_and_volume(self):
        p, rev = self.adopt(1)
        self.helper.breakeven(p)
        self.tick()
        self.api.closep(p, 4, 100.10)
        self.tick(3)
        orders = self.owned()
        self.assertEqual(len(orders), 2)
        reentry = next(o for o in orders if o.ticket != rev.ticket)
        self.assertEqual((reentry.price_open, reentry.sl), (99.2, 100.0))
        q = self.api.fill(rev.ticket, 100.83)
        self.tick(5)
        self.assertEqual(self.e.snapshot()["step"], 1)
        self.assertEqual(
            [(o.ticket, o.price_open, o.sl, o.volume_current) for o in self.owned()],
            [(reentry.ticket, 100.03, 100.83, 0.01)],
        )
        self.api.closep(q, 0)
        self.api.cancel(reentry.ticket)
        self.tick()
        self.assertEqual(self.e.snapshot()["mode"], "manual_wait")

    def test_repeated_manual_exits_cleanup_only_own_trigger_and_adopt_early_next_market(self):
        pos, stop = self.adopt()
        unrelated = self.api.pending(self.api.ORDER_TYPE_SELL_STOP, 75)
        retired = []
        for n in range(5):
            retired.append(stop.ticket)
            self.api.closep(pos, n % 3)
            pos = self.api.market(n % 2, 101 + n)
            self.tick(5)
            self.assertEqual(self.e.snapshot()["position_ticket"], pos.ticket)
            self.assertEqual(self.e.snapshot()["step"], 1)
            self.assertIn(unrelated.ticket, self.api.o)
            self.assertEqual(len(self.owned()), 1)
            stop = self.owned()[0]
        removes = [q["order"] for q in self.api.requests if q["action"] == self.api.TRADE_ACTION_REMOVE]
        self.assertEqual(removes, retired)
        self.assertEqual(self.e.db.execute("SELECT count(*) FROM confirmed_exits").fetchone()[0], 5)

    def test_closed_full_and_externally_cancelled_reverse_recovers_flat_paused_on_restart(self):
        pos, stop = self.adopt()
        self.assertTrue(self.e.control("manual_cancel"))
        self.e.s["leg"]["rung"] = 3
        self.e.save()
        self.api.closep(pos, self.api.DEAL_REASON_SL)
        self.tick()
        self.assertEqual(self.e.snapshot()["reason"], "waiting_reverse_fill")
        self.assertIn(stop.ticket, self.api.o)
        self.assertFalse(any(q["action"] == self.api.TRADE_ACTION_REMOVE for q in self.api.requests))
        self.e.close()
        self.api.cancel(stop.ticket)
        self.helper.disagree = True
        count = len(self.api.requests)
        self.open()
        self.assertEqual(self.e.snapshot()["mode"], "active")
        self.helper.disagree = False
        self.tick(2)
        self.assertFalse(self.e.running)
        self.assertEqual(self.e.snapshot()["mode"], "ready")
        self.assertFalse(self.e.snapshot()["market_rearm_enabled"])
        self.assertEqual(len(self.api.requests), count)

    def test_cancel_receipt_lost_no_repeat_and_successor_from_fill_race(self):
        pos, stop = self.adopt()
        self.api.closep(pos, 0)
        self.api.lost = True
        self.tick()
        self.assertNotIn(stop.ticket, self.api.o)
        self.helper.disagree = True
        self.tick()
        self.assertEqual(self.e.snapshot()["mode"], "active")
        self.helper.disagree = False
        self.tick(2)
        self.assertEqual(self.e.snapshot()["mode"], "manual_wait")
        self.assertEqual(sum(q["action"] == self.api.TRADE_ACTION_REMOVE for q in self.api.requests), 1)
        pos = self.api.market()
        self.tick(3)
        stop = self.owned()[0]
        self.api.closep(pos, 0)
        self.api.check_hook = lambda _: self.api.fill(stop.ticket)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["step"], 2)
        self.assertEqual(self.e.snapshot()["mode"], "active")
        self.assertEqual(sum(q["action"] == self.api.TRADE_ACTION_REMOVE for q in self.api.requests), 1)

    def test_pending_only_native_and_one_shot(self):
        self.arm("manual_pending_wait")
        limit = self.api.pending(2, 98)
        self.tick()
        self.assertEqual(limit.sl, 0.0)
        stop = self.api.pending(4, 103)
        self.tick(2)
        self.assertEqual(stop.sl, 102.2)
        p = self.api.fill(stop.ticket, 103.05)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["position_ticket"], p.ticket)
        o = self.owned()[0]
        self.assertEqual(o.price_open, 102.25)
        self.assertFalse(self.e.snapshot()["market_rearm_enabled"])
        self.api.closep(p, 0)
        self.api.cancel(o.ticket)
        self.tick()
        self.assertEqual(self.e.snapshot()["mode"], "ready")

    def test_entry_from_limit_does_not_become_market(self):
        self.arm()
        self.api.fill(self.api.pending(2, 99).ticket)
        self.tick(3)
        self.assertEqual(self.e.snapshot()["mode"], "manual_wait")
        self.assertEqual(self.api.requests, [])

    def test_duplicate_manual_stops_and_cancel_pending(self):
        self.arm("manual_pending_wait")
        a = self.api.pending(4, 110)
        b = self.api.pending(5, 90)
        self.tick()
        self.assertEqual(self.api.requests, [])
        self.api.cancel(b.ticket)
        self.tick(2)
        self.assertTrue(self.e.control("manual_cancel"))
        self.assertIn(a.ticket, self.api.o)

    def test_cross_source_exit_disagreement_does_not_rearm(self):
        p, o = self.adopt()
        self.api.closep(p, 0)
        self.api.cancel(o.ticket)
        self.helper.complete = False
        self.tick(3)
        self.assertEqual(self.e.snapshot()["mode"], "active")
        self.helper.complete = True
        self.tick()
        self.assertEqual(self.e.snapshot()["mode"], "manual_wait")

    def test_crash_and_lost_reverse_ack_reconciles_without_duplicate(self):
        self.arm()
        self.api.market(sl=99.2)
        self.api.lost = True
        self.tick()
        self.assertEqual(len(self.owned()), 1)
        self.e.close()
        self.open()
        self.assertFalse(self.e.snapshot()["running"])
        self.assertTrue(self.e.control("start"))
        self.tick(4)
        self.assertEqual(len(self.owned()), 1)
        self.assertEqual(len(self.api.requests), 1)

    def test_lost_ack_and_immediate_fill_recovered_from_independent_history(self):
        self.arm()
        p = self.api.market(sl=99.2)
        self.api.lost = True
        self.tick()
        o = self.owned()[0]
        self.api.closep(p, 4)
        q = self.api.fill(o.ticket)
        self.e.close()
        self.open()
        self.assertTrue(self.e.control("start"))
        self.tick(4)
        self.assertEqual(self.e.snapshot()["position_ticket"], q.ticket)
        self.assertEqual(len(self.owned()), 1)
        self.assertEqual(len(self.api.requests), 2)

    def test_fully_retired_successor_between_two_timers_manual_rearms(self):
        p, o = self.adopt()
        self.api.closep(p, 4)
        gone = self.api.fill(o.ticket)
        self.api.closep(gone, 2)
        new = self.api.market(1, 105)
        self.tick(5)
        self.assertEqual(self.e.snapshot()["position_ticket"], new.ticket)
        self.assertEqual(self.e.snapshot()["step"], 1)
        self.assertEqual(len(self.owned()), 1)
        self.assertNotIn(0, [r["ticket"] for r in self.helper.registry])

    def test_unclassified_exit_records_exact_evidence_once_without_trading(self):
        p, o = self.adopt()
        self.api.closep(p, 5)
        self.api.cancel(o.ticket)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["mode"], "active")
        import json

        rows = self.e.db.execute("SELECT position,evidence FROM confirmed_exits").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], str(p.identifier))
        out = self.api.d[-1]
        self.assertEqual(
            json.loads(rows[0][1])["exits"],
            [
                dict(
                    ticket=out.ticket,
                    order=out.order,
                    side=out.type,
                    steps=1,
                    reason=5,
                    time_msc=out.time_msc,
                    price_ticks=round(out.price * 100),
                    sl_ticks=round(out.sl * 100),
                )
            ],
        )

    def test_exact_be_ack_requires_independent_exit_sl(self):
        p, old = self.adopt()
        reg = self.helper.registry[0]
        self.helper.proven[p.identifier] = dict(reg, status="missing", retcode=10009, sl_ticks=0)
        p.sl = p.price_open
        self.api.closep(p, 4, 98.0)
        self.helper.complete = False
        self.tick(2)
        self.assertEqual(len(self.owned()), 1)
        self.helper.complete = True
        self.tick(3)
        self.assertEqual(len(self.owned()), 2)

    def test_cancel_bound_manual_pending_does_not_adopt_another(self):
        self.arm("manual_pending_wait")
        old = self.api.pending(4, 103)
        self.tick(2)
        new = self.api.pending(5, 97)
        self.api.cancel(old.ticket)
        self.tick(3)
        self.assertEqual(self.e.snapshot()["mode"], "ready")
        self.assertEqual(new.sl, 0.0)

    def test_mid_preflight_reverse_fill_does_not_send_stale_reentry(self):
        p, o = self.adopt(1)
        self.helper.breakeven(p)
        self.tick()
        self.api.closep(p, 4)
        self.api.check_hook = lambda req: self.api.fill(o.ticket)
        self.tick()
        self.assertEqual(len(self.owned()), 0)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["step"], 1)
        self.assertEqual(len(self.owned()), 1)

    def test_late_support_history_never_increments_successor_lot(self):
        p, old = self.adopt(1)
        self.e.s["leg"]["rung"] = 5
        self.e.save()
        self.helper.breakeven(p)
        self.tick()
        self.api.closep(p, 4)
        q = self.api.fill(old.ticket)
        self.helper.complete = False
        self.tick(3)
        self.assertEqual(self.e.snapshot()["position_ticket"], q.ticket)
        self.assertEqual(self.owned(), [])
        self.helper.complete = True
        self.tick(4)
        self.assertEqual(self.e.snapshot()["step"], 5)
        self.assertEqual(self.owned()[0].volume_initial, 0.02)

    def test_both_support_orders_fill_before_next_read_exact_sequence(self):
        p, rev = self.adopt(1)
        self.helper.breakeven(p)
        self.tick()
        self.api.closep(p, 4)
        self.tick(3)
        alternate = next(o for o in self.owned() if o.ticket != rev.ticket)
        self.api.fill(rev.ticket)
        last = self.api.fill(alternate.ticket)
        self.tick(5)
        self.assertEqual(self.e.snapshot()["position_ticket"], last.ticket)
        self.assertEqual(self.e.snapshot()["step"], 2)
        self.assertEqual(len(self.owned()), 1)

    def test_24_steps_full_close_no_25th_order(self):
        p, ordr = self.adopt()
        lots = list(self.e.bank)
        for i in range(1, 24):
            self.assertEqual(round(ordr.volume_initial * 100), lots[i])
            self.api.closep(p, 4)
            p = self.api.fill(ordr.ticket)
            self.tick(4)
            if i < 23:
                ordr = self.owned()[0]
        self.assertEqual(self.e.snapshot()["step"], 24)
        self.assertFalse(self.owned())
        self.api.closep(p, 4)
        self.tick()
        self.assertEqual(self.e.snapshot()["mode"], "ready")
        self.assertFalse(self.owned())

    def test_rung24_support_reentry_is_same_rung(self):
        p, order = self.adopt()
        self.api.cancel(order.ticket)
        # Enter the public twenty-fourth boundary with no further opposing ticket.
        leg = self.e.s["leg"]
        leg["rung"] = 24
        leg["orders"] = {}
        self.e.save()
        self.helper.breakeven(p)
        self.tick()
        self.api.closep(p, 4)
        self.tick(3)
        r = self.owned()[0]
        self.assertEqual((r.type, r.volume_initial), (4, 0.10))
        p2 = self.api.fill(r.ticket)
        self.tick(3)
        self.assertEqual(self.e.snapshot()["position_ticket"], p2.ticket)
        self.assertEqual(self.e.snapshot()["step"], 24)
        self.assertEqual(self.owned(), [])

    def test_guard_settings_only_flat_paused_and_single_owner(self):
        self.assertTrue(self.e.snapshot()["can_start"])
        import sqlite3

        with self.assertRaises(sqlite3.OperationalError):
            self.open()
        cfg = self.e.snapshot()["settings"]
        cfg["initial_sl_distance"] = "1.00"
        self.assertTrue(self.e.update_settings(cfg))
        self.arm()
        self.assertFalse(self.e.update_settings(cfg))
        self.api.external = False
        self.api.market()
        self.tick()
        self.assertEqual(self.api.requests, [])

    def test_helper_permission_refused_before_arm(self):
        self.helper.trade_allowed = False
        self.tick()
        self.assertFalse(self.e.snapshot()["available"])
        self.assertFalse(self.e.control("start"))

    def test_stop_empty_wait_clears_arm_and_restart_is_ready(self):
        for mode in ("manual_wait", "manual_pending_wait"):
            self.arm(mode)
            self.assertTrue(self.e.control("pause"))
            self.assertFalse(self.e.running)
            self.assertFalse(self.e.snapshot()["market_rearm_enabled"])
            self.assertEqual(self.e.snapshot()["mode"], "ready")

    def test_partial_fill_only_after_native_canceled_remainder_and_exact_volume(self):
        self.arm("manual_pending_wait")
        o = self.api.pending(5, 95, volume=0.03)
        self.tick(2)
        p = self.api.fill(o.ticket)
        o.state = 2
        o.volume_current = 0.02
        p.volume = 0.01
        self.api.d[-1].volume = 0.01
        self.helper.disagree = True
        self.tick(2)
        self.assertIsNone(self.e.s["leg"])
        self.helper.disagree = False
        self.tick(3)
        self.assertEqual(self.e.snapshot()["position_ticket"], p.ticket)
        self.assertEqual(self.e.s["leg"]["volume_steps"], 1)
        self.assertEqual(len(self.owned()), 1)

    def test_two_source_cancel_before_manual_exit_then_candidate_already_present(self):
        p, o = self.adopt()
        self.api.cancel(o.ticket)
        self.tick()
        self.assertEqual(self.e.snapshot()["mode"], "active")
        self.api.closep(p, 0)
        q = self.api.market(1, 101)
        self.helper.disagree = True
        self.tick(3)
        self.assertEqual(self.e.snapshot()["position_ticket"], p.ticket)
        self.helper.disagree = False
        self.tick(4)
        self.assertEqual(self.e.snapshot()["position_ticket"], q.ticket)

    def test_lease_survives_fast_exit_while_helper_publishes_last_readback(self):
        p, old = self.adopt()
        p.sl = p.price_open
        self.api.closep(p, 4)
        self.tick()
        self.assertEqual([r["position_id"] for r in self.helper.registry], [p.identifier])
        reg = self.helper.registry[0]
        self.helper.proven[p.identifier] = dict(reg, status="last_confirmed", sl_ticks=reg["entry_ticks"])
        self.tick(3)
        self.assertEqual(len(self.owned()), 2)
        self.assertEqual(self.e.snapshot()["step"], 1)

    def test_legacy_table_is_neither_read_nor_updated_on_idle_ticks(self):
        self.e.db.execute("CREATE TABLE closed_money(position TEXT PRIMARY KEY, evidence TEXT, result TEXT)")
        self.e.db.execute("INSERT INTO closed_money VALUES ('123','archived',NULL)")
        self.e.db.commit()
        self.e.close()
        self.open()

        def no_idle_history(pid):
            self.fail("idle timer requested unrelated position history")

        self.helper.history = no_idle_history
        statements = []
        self.e.db.set_trace_callback(statements.append)
        self.tick(5)
        self.assertFalse(any("closed_money" in sql for sql in statements))
        self.assertTrue(
            {"closed_outcomes", "currency", "realized_total", "pending_outcomes"}.isdisjoint(
                self.e.snapshot()
            )
        )
        self.assertEqual(
            self.e.db.execute("SELECT * FROM closed_money").fetchall(), [("123", "archived", None)]
        )

    def test_disappeared_position_and_filled_order_challenged_together(self):
        from recovery_helpers import HelperUnavailable

        pos, stop = self.adopt()
        self.api.closep(pos, self.api.DEAL_REASON_SL)
        new = self.api.fill(stop.ticket)
        history = self.helper.history
        order = self.helper.order
        requested = set()
        approved = set()

        def challenge(observation, key, read):
            requested.add((observation, key))
            if observation not in approved:
                raise HelperUnavailable("awaiting test verifier")
            return read(key)

        self.helper.history = lambda pid: challenge("history", pid, history)
        self.helper.order = lambda ticket: challenge("order", ticket, order)
        self.tick()
        self.assertEqual(requested, {("history", pos.identifier), ("order", stop.ticket)})
        self.assertEqual(self.owned(), [])
        approved.add("order")
        self.tick(2)
        self.assertEqual(self.e.snapshot()["position_ticket"], new.ticket)
        self.assertEqual(self.owned(), [])
        approved.add("history")
        self.tick(3)
        self.assertEqual(self.e.snapshot()["step"], 2)
        self.assertEqual(len(self.owned()), 1)

    def test_detached_order_fill_does_not_wait_for_previous_position_close(self):
        p, o = self.adopt()
        q = self.api.fill(o.ticket)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["position_ticket"], q.ticket)
        self.assertEqual(len(self.owned()), 1)
        self.assertEqual(len(self.e.s["detached"]), 1)
        self.api.closep(p, 4)
        self.tick()
        self.assertEqual(self.e.s["detached"], [])

    def episode_at_rejected_move(self, legacy=False, lost_after_apply=False, check_reject=False):
        self.arm()
        p = self.api.market(0, 3526.43)
        self.tick(4)
        for _ in range(6):
            reverse = self.owned()[0]
            self.api.closep(p, 4)
            p = self.api.fill(reverse.ticket)
            self.tick(4)
        self.assertEqual(self.e.snapshot()["step"], 7)
        reverse = self.owned()[0]
        self.helper.breakeven(p)
        self.tick()
        self.api.closep(p, 4)
        self.tick(3)
        old = next(o for o in self.owned() if o.ticket != reverse.ticket)
        self.assertEqual((old.price_open, old.sl, old.volume_initial), (3527.23, 3526.43, 0.02))
        parent = self.api.fill(reverse.ticket, 3525.02)
        self.tick()
        self.api.quote = N(bid=3526.03, ask=3526.05)
        self.api.move_result = None if lost_after_apply else self.api.TRADE_RETCODE_INVALID_PRICE
        if check_reject:
            self.api.checkcode = self.api.TRADE_RETCODE_INVALID_PRICE
        self.api.lost = lost_after_apply
        self.tick()
        key = self.e.s["leg"]["orders"]["reverse"]["move"]["key"]
        self.assertEqual(self.e._record(key)[1], "submitted" if lost_after_apply else "rejected_price")
        if legacy:
            with self.e.db:
                self.e.db.execute("UPDATE actions SET result='submitted',ticket='0' WHERE id=?", (key,))
                self.e.db.execute("DELETE FROM modify_receipts WHERE id=?", (key,))
        return parent, old, key

    def test_exact_incident_paused_retirement_only_after_two_complete_histories(self):
        parent, survivor, key = self.episode_at_rejected_move(legacy=True)
        self.api.closep(parent, 4, 3526.52)
        child = self.api.fill(survivor.ticket, 3527.44)
        self.api.closep(child, 4, 3526.45)
        self.e.close()
        writes = len(self.api.requests)
        self.helper.complete = False
        self.open()
        self.assertFalse(self.e.running)
        epoch = self.e.s["epoch"]
        self.assertFalse(self.e.control("start"))
        self.assertFalse(self.e.retire_interrupted_move(epoch, survivor.ticket))
        self.assertEqual(self.e._record(key)[1], "terminal_original")
        self.helper.complete = True
        self.assertFalse(self.e.retire_interrupted_move("other", survivor.ticket))
        self.assertFalse(self.e.retire_interrupted_move(epoch, survivor.ticket + 1))
        foreign = self.api.pending(2, 3500)
        self.assertFalse(self.e.retire_interrupted_move(epoch, survivor.ticket))
        self.api.cancel(foreign.ticket)
        import sqlite3

        self.e.db.execute("""CREATE TRIGGER fail_retirement BEFORE INSERT ON owner_state
            WHEN json_extract(NEW.wire,'$.mode')='ready' BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.e.retire_interrupted_move(epoch, survivor.ticket)
        self.assertEqual(self.e.s["epoch"], epoch)
        self.assertEqual(self.e.s["interrupted_move"]["ticket"], survivor.ticket)
        self.e.db.execute("DROP TRIGGER fail_retirement")
        self.assertTrue(self.e.retire_interrupted_move(epoch, survivor.ticket))
        result = self.e.tick()
        self.assertNotEqual(result["strategy_epoch"], epoch)
        self.assertFalse(result["running"] or result["market_rearm_enabled"])
        self.assertEqual(result["mode"], "ready")
        self.assertTrue(self.e.retire_interrupted_move(epoch, survivor.ticket))
        self.assertEqual(len(self.api.requests), writes)
        for pid in (parent.identifier, child.identifier):
            self.assertTrue(
                self.e.db.execute(
                    "SELECT evidence FROM confirmed_exits WHERE position=?", (str(pid),)
                ).fetchone()
            )

    def test_invalid_price_retry_needs_new_admissible_quote_and_preserves_ticket(self):
        from unittest.mock import patch

        parent, old, key = self.episode_at_rejected_move()
        feedback = self.e.db.execute(
            "SELECT retcode,received,quote FROM modify_receipts WHERE id=?", (key,)
        ).fetchone()
        self.assertEqual(feedback[0], 10015)
        self.assertEqual(self.e.reason, "move_rejection_recorded")
        writes = len(self.api.requests)
        self.tick(3)
        self.assertEqual(self.e.reason, "move_rejected_price")
        with patch("recovery_strategy.time.time", return_value=feedback[1] + 10):
            self.tick(2)
            self.api.quote = N(bid=3525.80, ask=3525.83)
            self.tick(2)
            self.assertEqual(len(self.api.requests), writes)
            self.api.move_result = None
            self.api.quote = N(bid=3525.60, ask=3525.62)
            self.tick(3)
        self.assertEqual(len(self.api.requests), writes + 1)
        self.assertEqual((old.price_open, old.sl, old.volume_current), (3525.82, 3525.02, 0.02))
        self.assertIn(old.ticket, self.api.o)
        self.assertEqual(self.e._record(key)[1], "rejected_price")

    def test_checked_invalid_price_never_sent_and_audited_separately(self):
        parent, old, key = self.episode_at_rejected_move(check_reject=True)
        self.assertEqual(
            self.e.db.execute("SELECT retcode,phase FROM modify_receipts WHERE id=?", (key,)).fetchone(),
            (10015, "check"),
        )
        self.assertFalse(
            any(
                q["action"] == self.api.TRADE_ACTION_MODIFY and q["order"] == old.ticket
                for q in self.api.requests
            )
        )
        self.tick(3)
        self.assertEqual(self.e.reason, "move_rejected_price")

    def test_second_support_reuses_survivor_with_a_new_move_identity(self):
        parent, old, key = self.episode_at_rejected_move(lost_after_apply=True)
        self.tick(3)
        self.helper.breakeven(parent)
        self.tick()
        self.api.closep(parent, 4)
        self.tick(3)
        reentry = next(o for o in self.owned() if o.ticket != old.ticket)
        new = self.api.fill(reentry.ticket, 3524.10)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["position_ticket"], new.ticket)
        self.assertEqual(len(self.owned()), 1)
        self.assertEqual((old.price_open, old.sl, old.volume_current), (3524.90, 3524.10, 0.02))
        self.assertNotEqual(self.e.s["leg"]["orders"]["reverse"]["move"]["key"], key)

    def test_partial_or_different_geometry_cannot_retire_full_interrupted_cycle(self):
        parent, old, key = self.episode_at_rejected_move(legacy=True)
        self.api.closep(parent, 4)
        child = self.api.fill(old.ticket, 3527.44)
        self.api.closep(child, 4)
        counts = len(self.api.requests)
        old.sl = 3526.44
        self.tick(2)
        self.assertEqual(self.e._record(key)[1], "submitted")
        old.sl = 3526.43
        entry = next(d for d in self.api.d if d.position_id == child.identifier and d.entry == 0)
        entry.volume = 0.01
        self.tick(2)
        self.assertEqual(self.e._record(key)[1], "submitted")
        entry.volume = 0.02
        self.helper.complete = False
        self.tick(2)
        self.assertFalse(self.e.running)
        self.assertEqual(self.e.reason, "interrupted_move_retirement")
        self.assertFalse(self.e.control("start"))
        self.assertEqual(len(self.api.requests), counts)

        def no_lease(*args):
            self.fail("flat historical retirement published a trading lease")

        self.helper.register = no_lease
        self.api.order_check = self.api.order_send = no_lease
        self.helper.complete = True
        self.tick(2)
        self.assertEqual(self.e.snapshot()["mode"], "ready")
        self.assertFalse(self.e.running or self.e.s["armed"])
        self.assertIsNone(self.e.s["leg"])
        self.assertEqual(len(self.api.requests), counts)

    def test_old_unacknowledged_move_never_retries_and_exact_cancel_can_resolve(self):
        parent, old, key = self.episode_at_rejected_move(legacy=True)
        self.api.quote = N(bid=3525.60, ask=3525.62)
        writes = len(self.api.requests)
        self.tick(5)
        self.assertEqual(self.e.reason, "awaiting_move_evidence")
        self.assertEqual(len(self.api.requests), writes)
        self.api.cancel(old.ticket)
        self.helper.disagree = True
        self.tick(2)
        self.assertEqual(self.e._record(key)[1], "submitted")
        self.helper.disagree = False
        self.tick()
        self.assertEqual(self.e._record(key)[1], "terminal_original")
        self.assertEqual(self.e.reason, "counterorder_canceled")
        self.assertEqual(len(self.api.requests), writes)

    def test_lost_modify_ack_with_observed_target_is_not_repeated(self):
        parent, old, key = self.episode_at_rejected_move(lost_after_apply=True)
        writes = len(self.api.requests)
        self.tick(3)
        self.assertEqual(self.e._record(key)[1], "observed_target")
        self.assertEqual(len(self.api.requests), writes)
        self.assertFalse(self.e.s["leg"]["move_required"])

    def test_old_geometry_fill_with_live_successor_advances_once(self):
        parent, old, key = self.episode_at_rejected_move(legacy=True)
        self.api.closep(parent, 4, 3526.52)
        child = self.api.fill(old.ticket, 3527.44)
        self.tick(4)
        self.assertEqual(self.e.snapshot()["position_ticket"], child.ticket)
        self.assertEqual(self.e.snapshot()["step"], 8)
        self.assertEqual(self.e._record(key)[1], "terminal_original")
        opposite = self.owned()
        self.assertEqual(len(opposite), 1)
        self.assertEqual((opposite[0].volume_initial, opposite[0].price_open), (0.03, 3526.64))


if __name__ == "__main__":
    unittest.main()
