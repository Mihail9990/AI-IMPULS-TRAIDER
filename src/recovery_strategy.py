"""Durable local RECOVERY owner. External callers supply the already connected MT5 API."""

import copy
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from local_settings import DEFAULT_SETTINGS, validate_settings
from recovery_book import Broker, CheckRejected, Wait, checked, units
from recovery_helpers import HelperUnavailable


def opposite(side):
    return "SELL" if side == "BUY" else "BUY"


def sign(side):
    return 1 if side == "BUY" else -1


class RecoveryStrategy:
    def __init__(self, api, helpers, *, account, server, symbol, magic, database, settings=None):
        self.lock = threading.RLock()
        self.b = Broker(api, helpers, account, server, symbol, magic)
        path = Path(database)
        if not path.is_absolute() or not path.parent.is_dir():
            raise ValueError("An existing private database directory is required")
        self.ownership = sqlite3.connect(str(path) + ".owner", timeout=0, check_same_thread=False)
        try:
            self.ownership.execute("BEGIN EXCLUSIVE")
            self.db = sqlite3.connect(path, timeout=3, check_same_thread=False)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS owner_state(k INTEGER PRIMARY KEY, wire TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, wire TEXT NOT NULL, result TEXT NOT NULL, ticket TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS confirmed_exits(position TEXT PRIMARY KEY, evidence TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS modify_receipts(id TEXT PRIMARY KEY, retcode INTEGER NOT NULL,"
                " received REAL NOT NULL, quote TEXT NOT NULL, phase TEXT NOT NULL)"
            )
            row = self.db.execute("SELECT wire FROM owner_state WHERE k=1").fetchone()
            binding = [int(account), server, symbol, int(magic), helpers.scope]
            if row:
                self.s = json.loads(row[0])
                if self.s["binding"] != binding:
                    raise ValueError("Database binding differs from the selected MT5 scope")
            else:
                self.s = dict(
                    binding=binding,
                    settings=validate_settings(settings or DEFAULT_SETTINGS),
                    mode="ready",
                    armed=False,
                    epoch=uuid4().hex,
                    pending=None,
                    leg=None,
                    detached=[],
                    floor=0,
                )
                self.save()
            self.running = False
            self.available = False
            self.reason = "starting"
            self.last_read = 0.0
            self.last_lease = 0.0
            self.lease_signature = None
            self.bank = []
            self.distance = self.threshold = 0
            self.unresolved = False
            self.move_wait = ""
            self.foreign = False
            self.run_id = self.s.setdefault("lease_id", uuid4().hex)
            self.save()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.ownership.close()
            raise

    def save(self, state=None):
        state = self.s if state is None else state
        wire = json.dumps(state, sort_keys=True, separators=(",", ":"))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO owner_state(k,wire) VALUES(1,?)", (wire,))
        self.s = state

    def _grid(self, settings):
        b = self.b
        distance = units(settings["initial_sl_distance"], b.tick)
        threshold = units(settings["support_threshold"], b.tick)
        bank = [units(value, b.step) for value in settings["lot_schedule"]]
        checked(
            distance > 0
            and threshold > 0
            and len(bank) == 24
            and all(b.min_vol <= vol <= b.max_vol for vol in bank),
            "settings_off_grid",
        )
        return distance, threshold, bank

    def _known_orders(self):
        leg = self.s["leg"]
        out = {self.s["pending"]["ticket"]} if self.s["pending"] else set()
        if leg:
            out.update(item["ticket"] for item in leg["orders"].values() if item.get("ticket"))
        return out

    def _foreign_managed(self):
        leg = self.s["leg"]
        known = {q["position_id"] for q in self.s["detached"]}
        orders = self._known_orders()
        if leg:
            known.add(leg["position_id"])
        for p in self.b.verified_positions.values():
            if p["magic"] == self.b.magic and p["position_id"] not in known:
                if not any(
                    d.order in orders
                    for d in self.b.deals(p["position_id"])
                    if d.entry == self.b.api.DEAL_ENTRY_IN
                ):
                    return True
        return any(
            o["magic"] == self.b.magic and o["ticket"] not in orders for o in self.b.verified_orders.values()
        )

    def _fresh(self):
        return self.available and time.monotonic() - self.last_read < 5

    def _can_start(self):
        return (
            not self.running
            and self._fresh()
            and not self.unresolved
            and not self.foreign
            and not self.s.get("interrupted_move")
        )

    def _can_pause(self):
        return (
            self.running
            and self._fresh()
            and self.s["mode"] in ("ready", "manual_wait", "pending_wait")
            and not self.s["leg"]
            and not self.s["pending"]
            and not self.s["detached"]
            and not self.unresolved
            and not self.b.positions
            and not self.b.orders
        )

    def snapshot(self):
        with self.lock:
            leg = self.s["leg"] or {}
            return dict(
                running=self.running,
                mode=self.s["mode"],
                market_rearm_enabled=self.s["armed"],
                available=self._fresh(),
                mt5_connected=self.b.connected,
                can_start=self._can_start(),
                can_pause=self._can_pause(),
                reason=self.reason,
                account="***" + str(self.b.account)[-3:],
                server=self.b.server,
                symbol=self.b.symbol,
                strategy_epoch=self.s["epoch"],
                settings=copy.deepcopy(self.s["settings"]),
                step=leg.get("rung", 0),
                position_ticket=leg.get("ticket", 0),
                opposite_ticket=(leg.get("orders", {}).get("reverse") or {}).get("ticket", 0),
                support_applied=bool(leg.get("be_proven")),
            )

    def update_settings(self, settings):
        with self.lock:
            if (
                self.running
                or not self._fresh()
                or self.s["mode"] != "ready"
                or self.s["leg"]
                or self.s["pending"]
            ):
                return False
            try:
                self.b.refresh()
                if self.b.orders or self.b.positions or self.unresolved:
                    return False
                checked_settings = validate_settings(settings)
                self._grid(checked_settings)
            except (ValueError, Wait, HelperUnavailable):
                return False
            self.s["settings"] = checked_settings
            self.save()
            return True

    def control(self, action):
        with self.lock:
            if action == "start" and self._can_start():
                self.running = True
                self.reason = ""
                return True
            if action == "pause" and self._can_pause():
                self.s.update(mode="ready", armed=False, floor=0)
                self.save()
                self.running = False
                self.reason = ""
                return True
            if not self.running or not self._fresh():
                return False
            if (
                action in ("manual_wait", "manual_pending_wait")
                and self.s["mode"] == "ready"
                and not self.foreign
                and not self.unresolved
            ):
                self.s.update(
                    mode="manual_wait" if action == "manual_wait" else "pending_wait",
                    armed=action == "manual_wait",
                    floor=0,
                )
            elif action == "manual_cancel":
                if self.s["mode"] == "active" and self.s["armed"]:
                    self.s["armed"] = False
                elif self.s["mode"] in ("manual_wait", "pending_wait"):
                    # A starting position owns its protection even while a delayed receipt is pending.
                    if self.s["leg"]:
                        self.s["armed"] = False
                    elif self.s["pending"]:
                        p = self.s["pending"]
                        o = self.b.orders.get(p["ticket"])
                        if o is None or o.state != self.b.api.ORDER_STATE_PLACED:
                            return False
                        self.s.update(mode="ready", armed=False, pending=None)
                    else:
                        self.s.update(mode="ready", armed=False)
                else:
                    return False
            else:
                return False
            self.reason = ""
            self.save()
            return True

    def _record(self, key):
        return self.db.execute("SELECT wire,result,ticket FROM actions WHERE id=?", (key,)).fetchone()

    def _effect(self, key, request, guard=None, move_side=None):
        """Persist intent before broker dispatch. An uncertain call can only be observed."""
        wire = json.dumps(request, sort_keys=True, separators=(",", ":"))
        row = self._record(key)
        if row:
            checked(row[0] == wire, "action_identity_changed")
            checked(row[1] == "accepted", "uncertain_broker_action")
            return int(row[2])
        quote = self.b.move_quote(request, move_side) if move_side else ""
        try:
            self.b.preflight(request, guard)
        except CheckRejected as exc:
            if not move_side or exc.retcode != self.b.api.TRADE_RETCODE_INVALID_PRICE:
                raise
            with self.db:
                self.db.execute("INSERT INTO actions VALUES(?,?,?,?)", (key, wire, "rejected_price", "0"))
                self.db.execute(
                    "INSERT INTO modify_receipts VALUES(?,?,?,?,?)",
                    (key, int(exc.retcode), time.time(), quote, "check"),
                )
            raise Wait("move_rejection_recorded") from exc
        with self.db:
            self.db.execute("INSERT INTO actions VALUES(?,?,?,?)", (key, wire, "submitted", "0"))
        if guard:
            try:
                guard()
            except Wait:
                # This process owns the journal and has not entered order_send.
                with self.db:
                    self.db.execute("DELETE FROM actions WHERE id=? AND result='submitted'", (key,))
                raise
        try:
            result = self.b.api.order_send(request)
        except Exception as exc:
            raise Wait("uncertain_broker_action") from exc
        accepted = result is not None and result.retcode in (
            self.b.api.TRADE_RETCODE_DONE,
            self.b.api.TRADE_RETCODE_PLACED,
        )
        ticket = int(result.order or 0) if result is not None else 0
        if request["action"] == self.b.api.TRADE_ACTION_PENDING:
            accepted = accepted and ticket > 0
        invalid = (
            move_side and result is not None and result.retcode == self.b.api.TRADE_RETCODE_INVALID_PRICE
        )
        with self.db:
            if move_side and result is not None:
                self.db.execute(
                    "INSERT INTO modify_receipts VALUES(?,?,?,?,?)",
                    (key, int(result.retcode), time.time(), quote, "send"),
                )
            if accepted or invalid:
                self.db.execute(
                    "UPDATE actions SET result=?,ticket=? WHERE id=?",
                    ("accepted" if accepted else "rejected_price", str(ticket), key),
                )
        self.b.session()
        if invalid:
            raise Wait("move_rejection_recorded")
        checked(accepted, "uncertain_broker_action")
        return ticket

    def _move_context(self, key, request):
        leg = self.s["leg"]
        item = (leg or {}).get("orders", {}).get("reverse")
        if item and (item.get("move") or {}).get("key") == key:
            creation = self._record(item["key"])
            checked(
                creation and creation[1] == "accepted" and int(creation[2]) == item["ticket"],
                "move_binding_changed",
            )
            original = json.loads(creation[0])
            checked(
                original["action"] == self.b.api.TRADE_ACTION_PENDING
                and original["symbol"] == self.b.symbol
                and original["magic"] == self.b.magic
                and self.b.side(original["type"]) == item["side"]
                and units(original["volume"], self.b.step) == item["volume"],
                "move_binding_changed",
            )
            return item, False
        item = self.s["pending"]
        if (
            item
            and request["order"] == item["ticket"]
            and key == (item.get("move") or {}).get("key", f"{self.s['epoch']}:pending:{item['ticket']}")
        ):
            return item, True
        raise Wait("move_binding_changed")

    def _reconcile_move(self, key, request, prior):
        b = self.b
        item, manual = self._move_context(key, request)
        move = item.get("move") or {"entry": item["entry"], "sl": item["sl"]}
        checked(
            (units(request["price"], b.tick), units(request["sl"], b.tick)) == (move["entry"], move["sl"])
            and units(request["tp"], b.tick) == (item["tp"] if manual else 0),
            "move_binding_changed",
        )
        state, moved, o = b.observe_move(request, item, manual)
        if state == "working" and not moved:
            if prior == "submitted":
                raise Wait("awaiting_move_evidence")
            return
        # If a retry was definitively rejected, a final ticket can still prove
        # its effective geometry. Preserve the actual server receipt separately.
        result = "observed_target" if moved else "terminal_original"
        if state == "filled" and not manual:
            leg = self.s["leg"]
            item["recovered_fill"] = o.position_id
            live_ids = {p["position_id"] for p in b.verified_positions.values()}
            # Once both positions have already disappeared while the MODIFY was
            # unresolved, ordinary recovery must never create a retrospective STOP.
            if o.position_id not in live_ids and leg["position_id"] not in live_ids:
                self.s["interrupted_move"] = dict(key=key, ticket=o.ticket, successor=o.position_id)
        with self.db:
            self.db.execute("UPDATE actions SET result=?,ticket=? WHERE id=?", (result, str(o.ticket), key))
            self.save()

    def _modify(self, item, base, order, entry, sl, guard=None):
        move = item.get("move")
        if not move:
            move = item["move"] = dict(key=base, entry=entry, sl=sl)
            self.save()
        checked((move["entry"], move["sl"]) == (entry, sl), "action_identity_changed")
        key = move["key"]
        request = self.b.modify(order, entry, sl)
        row = self._record(key)
        if row and row[1] == "rejected_price":
            receipt = self.db.execute(
                "SELECT received,quote FROM modify_receipts WHERE id=?", (key,)
            ).fetchone()
            attempt = move.get("attempt", 0)
            checked(
                receipt and time.time() - receipt[0] >= min(60, 2 ** min(attempt + 1, 6)),
                "move_rejected_price",
            )
            current = self.b.move_quote(request, item["side"], admissible=True)
            checked(current and current != receipt[1], "move_rejected_price")
            move["attempt"] = attempt + 1
            move["key"] = key = f"{base}:retry:{attempt + 1}"
            move["retry_from"] = receipt[1]
            self.save()

        def exact_target():
            if guard:
                guard()
            if move.get("attempt"):
                current = self.b.move_quote(request, item["side"], admissible=True)
                checked(current and current != move["retry_from"], "move_rejected_price")

        self._effect(key, request, exact_target, move_side=item["side"])

    def _reconcile_intents(self):
        b = self.b
        api = b.api
        self.move_wait = ""
        rows = self.db.execute("SELECT id,wire,result FROM actions WHERE result='submitted'").fetchall()
        leg = self.s["leg"]
        tracked = (leg or {}).get("orders", {}).get("reverse") or self.s["pending"] or {}
        move = tracked.get("move")
        if move:
            row = self._record(move["key"])
            if row and row[1] == "rejected_price":
                rows.append((move["key"], row[0], row[1]))
        for key, wire, prior in rows:
            q = json.loads(wire)
            ticket = 0
            ok = False
            if q["action"] == api.TRADE_ACTION_PENDING:

                def matches(o):
                    return (
                        o.comment == q["comment"]
                        and o.symbol == b.symbol
                        and o.magic == b.magic
                        and o.type == q["type"]
                        and units(o.volume_initial, b.step) == units(q["volume"], b.step)
                        and units(o.price_open, b.tick) == units(q["price"], b.tick)
                        and units(o.sl, b.tick) == units(q["sl"], b.tick)
                        and units(o.tp, b.tick) == units(q["tp"], b.tick)
                        and o.price_stoplimit == 0
                    )

                live = [o for o in b.orders.values() if matches(o)]
                if len(live) == 1:
                    ticket = live[0].ticket
                    ok = True
                elif not live:
                    leg = self.s["leg"]
                    item = next((i for i in (leg or {}).get("orders", {}).values() if i["key"] == key), None)
                    if item:
                        # MT5 broker clock can differ from UTC. The UUID-derived comment and
                        # immutable geometry identify the request within this padded interval.
                        now = datetime.now(timezone.utc)
                        start = datetime.fromtimestamp(
                            item.get("created", now.timestamp()), timezone.utc
                        ) - timedelta(days=2)
                        history = api.history_orders_get(start, now + timedelta(days=2), group=b.symbol)
                        checked(history is not None, "order_history_unavailable")
                        matched = [o for o in history if matches(o)]
                        if len(matched) == 1 and b.history_order(matched[0].ticket):
                            ticket = matched[0].ticket
                            ok = True
            elif q["action"] == api.TRADE_ACTION_SLTP:
                p = b.positions.get(q["position"])
                ok = (
                    p is not None
                    and units(p.sl, b.tick) == units(q["sl"], b.tick)
                    and units(p.tp, b.tick) == units(q["tp"], b.tick)
                )
            elif q["action"] == api.TRADE_ACTION_MODIFY:
                try:
                    self._reconcile_move(key, q, prior)
                except (Wait, HelperUnavailable) as exc:
                    self.move_wait = str(exc) if isinstance(exc, Wait) else "awaiting_move_evidence"
            elif q["action"] == api.TRADE_ACTION_REMOVE and q["order"] not in b.orders:
                item = next(
                    (
                        i
                        for i in (self.s["leg"] or {}).get("orders", {}).values()
                        if i["ticket"] == q["order"]
                    ),
                    None,
                )
                if item is not None:
                    state, _ = self._observe(item)
                    # The ticket reached a terminal state. A racing fill still follows
                    # the ordinary successor path and never serves as a cancellation.
                    ok = state in ("canceled", "filled")
            if ok:
                with self.db:
                    self.db.execute(
                        "UPDATE actions SET result='accepted',ticket=? WHERE id=?", (str(ticket), key)
                    )
        self.unresolved = bool(self.move_wait) or bool(
            self.db.execute(
                "SELECT 1 FROM actions WHERE result NOT IN "
                "('accepted','observed_target','terminal_original','rejected_price') LIMIT 1"
            ).fetchone()
        )
        # A crash can occur after the receipt is committed but before the ticket reaches the cycle.
        leg = self.s["leg"]
        if leg:
            for item in leg["orders"].values():
                row = self._record(item["key"])
                if not item.get("ticket") and row and row[1] == "accepted" and int(row[2]) > 0:
                    item["ticket"] = int(row[2])
                    self.save()

    def _lease(self, publish=True):
        b = self.b
        rows = []
        status = b.helpers.sl_status()
        checked(status.get("trade_allowed") is True, "sl_helper_permission")
        current = [self.s["leg"]] if self.s["leg"] else []
        for p in current + self.s["detached"]:
            if status["run_id"] == self.run_id:
                for r in status["rows"]:
                    if (
                        r["position_id"],
                        r["ticket"],
                        r["opened_msc"],
                        r["magic"],
                        r["side"],
                        r["entry_ticks"],
                        r["advance_ticks"],
                    ) == (
                        p["position_id"],
                        p["ticket"],
                        p["opened_msc"],
                        p["magic"],
                        p["side"],
                        p["entry_ticks"],
                        self.threshold,
                    ):
                        if (
                            r["status"] in ("confirmed", "last_confirmed")
                            and r["sl_ticks"] == p["entry_ticks"]
                            and not p.get("be_proven")
                        ):
                            p["be_proven"] = True
                            self.save()
                        if (
                            r["status"] in ("pending", "missing")
                            and r.get("retcode") == b.api.TRADE_RETCODE_DONE
                            and not p.get("be_ack")
                        ):
                            p["be_ack"] = True
                            self.save()
            v = b.verified_positions.get(p["ticket"])
            eligible = v and v["position_id"] == p["position_id"] and v["sl_ticks"] > 0
            # Keep the exact identity registered until the independently proven exit. The
            # SL actor can return its last confirmed readback after the ticket disappears.
            if self.running and not p.get("history_only") and (eligible or (not v and not p.get("close"))):
                rows.append(
                    dict(
                        position_id=p["position_id"],
                        ticket=p["ticket"],
                        opened_msc=p["opened_msc"],
                        magic=p["magic"],
                        side=p["side"],
                        entry_ticks=p["entry_ticks"],
                        advance_ticks=self.threshold,
                    )
                )
        signature = tuple((p["position_id"], p["ticket"], p["advance_ticks"]) for p in rows)
        now = time.monotonic()
        if self.running and publish and (signature != self.lease_signature or now - self.last_lease >= 1.0):
            b.helpers.register(rows, self.run_id)
            self.last_lease = now
            self.lease_signature = signature

    def tick(self):
        with self.lock:
            self.available = False
            try:
                self.b.refresh()
                self.distance, self.threshold, self.bank = self._grid(self.s["settings"])
                self._reconcile_intents()
                hold = self.s.get("interrupted_move")
                if hold:
                    self.running = False
                    self.lease_signature = None
                self.foreign = self._foreign_managed()
                if not hold:
                    self._lease(publish=not (self.move_wait and not self.b.positions and not self.b.orders))
                self.last_read = time.monotonic()
                self.available = True
                checked(not self.unresolved, self.move_wait or "uncertain_broker_action")
                checked(not self.foreign, "unbound_strategy_exposure")
                if hold:
                    # The public method takes the same RLock; it performs read-only
                    # broker reconciliation and a conditional local transaction.
                    if not self.retire_interrupted_move(self.s["epoch"], hold["ticket"]):
                        raise Wait("interrupted_move_retirement")
                else:
                    self.reason = ""
                    if self.running:
                        self._step()
                    else:
                        self._reconcile_paused_close()
            except (Wait, HelperUnavailable) as exc:
                self.reason = str(exc) if isinstance(exc, Wait) else "helper_observation_unavailable"
                if self.reason in (
                    "account_or_connection",
                    "symbol_not_available",
                    "trading_permission",
                    "inventory_unavailable",
                    "sl_helper_permission",
                    "history_helper_scope",
                    "verifier_binding",
                ):
                    self.available = False
            return self.snapshot()

    def _leg(self, p, rung, initial):
        return dict(p, rung=rung, initial=initial, be_proven=False, orders={}, close=None)

    def _protect(self, leg):
        b = self.b
        p = b.positions.get(leg["ticket"])
        checked(p is not None and p.identifier == leg["position_id"], "protect_position_missing")
        sl = units(p.sl, b.tick)
        if sl > 0:
            return
        key = f"{self.s['epoch']}:{leg['position_id']}:initial"
        self._effect(key, b.protect(p, leg["initial"]))
        # The immutable reverse STOP may be staged following acceptance of the SL request.
        # The next matching broker/verifier inventory still has to prove the position effect.

    def _item(self, leg, kind, side, entry, sl, vol):
        b = self.b
        item = leg["orders"].get(kind)
        if not item:
            key = f"{self.s['epoch']}:{leg['position_id']}:{kind}"
            item = dict(
                key=key, side=side, entry=entry, sl=sl, volume=vol, ticket=0, created=int(time.time())
            )
            leg["orders"][kind] = item
            self.save()
        if not item["ticket"]:
            comment = "RC" + hashlib.sha256(item["key"].encode()).hexdigest()[:22]
            req = b.pending(item["side"], item["entry"], item["sl"], item["volume"], comment)

            def guard():
                b.session()
                if not leg.get("close"):
                    b.guard_live(leg)
                if kind == "reentry" and leg["rung"] < 24:
                    b.guard_counter(leg["orders"]["reverse"])

            item["ticket"] = self._effect(item["key"], req, guard)
            self.save()
            raise Wait("awaiting_stop")
        return item

    def _observe(self, item):
        b = self.b
        a = b.api
        if not item["ticket"]:
            return "unsent", None
        ticket = item["ticket"]
        cur = b.orders.get(ticket)
        o = cur or b.history_order(ticket)
        if o is None:
            return "missing", None
        checked(
            o.symbol == b.symbol
            and o.magic == b.magic
            and o.type == (a.ORDER_TYPE_BUY_STOP if item["side"] == "BUY" else a.ORDER_TYPE_SELL_STOP)
            and units(o.volume_initial, b.step) == item["volume"]
            and units(o.tp, b.tick) == 0
            and units(o.price_stoplimit, b.tick) == 0,
            "counterorder_identity",
        )
        geometry = (units(o.price_open, b.tick), units(o.sl, b.tick))
        if geometry != (item["entry"], item["sl"]):
            moved = item.get("move")
            row = self._record(moved["key"]) if moved else None
            checked(
                moved and row and geometry == (moved["entry"], moved["sl"]), "counterorder_geometry_changed"
            )
            item.update(entry=moved["entry"], sl=moved["sl"])
            self.save()
        if cur:
            checked(
                o.state == a.ORDER_STATE_PLACED
                and o.position_id == 0
                and units(o.volume_current, b.step) == item["volume"],
                "partial_or_changed_order",
            )
            return "working", o
        if o.state == a.ORDER_STATE_CANCELED:
            checked(
                o.position_id == 0
                and units(o.volume_current, b.step) == item["volume"]
                and units(o.price_open, b.tick) == item["entry"]
                and units(o.sl, b.tick) == item["sl"],
                "cancel_geometry_unconfirmed",
            )
            return "canceled", o
        if o.state == a.ORDER_STATE_FILLED:
            checked(o.position_id > 0 and o.volume_current == 0, "counterorder_fill_incomplete")
            return "filled", o
        raise Wait("counterorder_history_unresolved")

    def _step(self):
        s = self.s
        b = self.b
        if s["mode"] == "ready":
            return
        if not s["leg"]:
            if s["mode"] == "manual_wait":
                pp = list(b.verified_positions.values())
                if len(pp) != 1:
                    return
                p = pp[0]
                if p["magic"] or not b.human(p["reason"]) or p["opened_msc"] <= s["floor"]:
                    return
                p = b.identify(p, manual=True)
                s["leg"] = self._leg(
                    p, 1, p["sl_ticks"] or p["entry_ticks"] - sign(p["side"]) * self.distance
                )
                s["leg"]["first_manual"] = True
                s["mode"] = "active"
                self.save()
            elif s["mode"] == "pending_wait":
                self._pending()
                if not s["leg"]:
                    return
            else:
                raise Wait("missing_active_binding")
        self._active()

    def _pending(self):
        s = self.s
        b = self.b
        a = b.api
        if not s["pending"]:
            if b.positions:
                return
            choices = [
                o
                for o in b.orders.values()
                if o.magic == 0
                and b.human(o.reason)
                and o.type in (a.ORDER_TYPE_BUY_STOP, a.ORDER_TYPE_SELL_STOP)
                and o.state == a.ORDER_STATE_PLACED
                and o.volume_initial == o.volume_current
                and o.position_id == 0
                and o.price_stoplimit == 0
            ]
            if len(choices) != 1:
                return
            o = choices[0]
            v = b.verified_orders[o.ticket]
            side = b.side(o.type)
            s["pending"] = dict(
                ticket=o.ticket,
                side=side,
                entry=v["price_ticks"],
                volume=v["remaining_steps"],
                original_sl=v["sl_ticks"],
                sl=v["sl_ticks"] or v["price_ticks"] - sign(side) * self.distance,
                tp=v["tp_ticks"],
            )
            self.save()
        p = s["pending"]
        o = b.orders.get(p["ticket"])
        if o:
            checked(
                o.magic == 0
                and b.human(o.reason)
                and o.state == a.ORDER_STATE_PLACED
                and b.side(o.type) == p["side"]
                and units(o.price_open, b.tick) == p["entry"]
                and units(o.volume_current, b.step) == p["volume"]
                and units(o.tp, b.tick) == p["tp"]
                and o.volume_initial == o.volume_current,
                "pending_order_changed",
            )
            current = units(o.sl, b.tick)
            if current == 0:
                self._modify(p, f"{s['epoch']}:pending:{o.ticket}", o, p["entry"], p["sl"])
            else:
                checked(current == p["sl"], "pending_sl_changed")
            return
        o = b.history_order(p["ticket"])
        checked(o is not None and o.magic == 0 and b.human(o.reason), "pending_history_unavailable")
        native_type = a.ORDER_TYPE_BUY_STOP if p["side"] == "BUY" else a.ORDER_TYPE_SELL_STOP
        remaining = units(o.volume_current, b.step)
        checked(
            o.type == native_type
            and units(o.price_open, b.tick) == p["entry"]
            and units(o.tp, b.tick) == p["tp"]
            and units(o.sl, b.tick) in (p["sl"], p.get("original_sl", 0))
            and o.price_stoplimit == 0
            and units(o.volume_initial, b.step) == p["volume"],
            "pending_history_changed",
        )
        if o.state == a.ORDER_STATE_CANCELED and o.position_id == 0 and remaining == p["volume"]:
            s.update(pending=None, mode="ready", armed=False, epoch=uuid4().hex)
            self.save()
            return
        checked(
            o.position_id > 0
            and (
                (o.state == a.ORDER_STATE_FILLED and remaining == 0)
                or (o.state == a.ORDER_STATE_CANCELED and 0 < remaining < p["volume"])
            ),
            "pending_fill_incomplete",
        )
        v = next((r for r in b.verified_positions.values() if r["position_id"] == o.position_id), None)
        checked(v is not None, "filled_pending_position_missing")
        v = b.identify(v, p["ticket"], manual=True, native_stop=True)
        checked(
            v["side"] == p["side"] and v["volume_steps"] == p["volume"] - remaining, "pending_fill_mismatch"
        )
        s["leg"] = self._leg(v, 1, v["sl_ticks"] or v["entry_ticks"] - sign(v["side"]) * self.distance)
        s["pending"] = None
        s["mode"] = "active"
        self.save()

    def _record_exit(self, leg, exits):
        pid = str(leg["position_id"])
        wire = json.dumps(dict(anchor=leg["anchor"], exits=exits), sort_keys=True, separators=(",", ":"))
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO confirmed_exits(position,evidence) VALUES(?,?)", (pid, wire)
            )
            prior = self.db.execute(
                "SELECT evidence FROM confirmed_exits WHERE position=?", (pid,)
            ).fetchone()
            checked(prior[0] == wire, "closed_evidence_changed")

    def _detached(self):
        for p in self.s["detached"][:]:
            try:
                proof = self.b.closed(p, self._record_exit)
            except (Wait, HelperUnavailable):
                continue
            if proof:
                active = self.s["leg"]
                if active and active.get("rung_parent") == p["position_id"]:
                    active["rung"] = p["rung"] if proof["cause"] == "support" else p["rung"] + 1
                    del active["rung_parent"]
                self.s["detached"].remove(p)
                self.save()

    def _successor(self, leg, item, broker_order, keep_rung, remaining):
        b = self.b
        v = next(
            (r for r in b.verified_positions.values() if r["position_id"] == broker_order.position_id), None
        )
        if (
            v is None
            and leg["ticket"] not in b.positions
            and item.get("recovered_fill") == broker_order.position_id
        ):
            self.s["interrupted_move"] = dict(
                key=item["move"]["key"], ticket=item["ticket"], successor=broker_order.position_id
            )
            self.running = False
            self.lease_signature = None
            self.save()
            raise Wait("interrupted_move_retirement")
        if v is None:
            v = b.retired_fill(broker_order, item)
        else:
            v = b.identify(v, broker_order.ticket, native_stop=True)
        checked(
            v["side"] == item["side"] and v["magic"] == b.magic and v["volume_steps"] == item["volume"],
            "next_position_mismatch",
        )
        rung = leg["rung"] if keep_rung else leg["rung"] + 1
        checked(1 <= rung <= 24, "cycle_exhausted")
        if not leg.get("close"):
            old = {k: value for k, value in leg.items() if k not in ("orders", "close")}
            self.s["detached"].append(old)
        nxt = self._leg(v, rung, item["sl"])
        if not leg.get("close") and leg["ticket"] not in b.positions and not keep_rung:
            nxt["rung_parent"] = leg["position_id"]
        if remaining:
            # A surviving ticket may have served on earlier legs. Its observed
            # geometry survives, but the next leg must own a new relocation.
            nxt["orders"]["reverse"] = {
                k: v for k, v in remaining.items() if k not in ("move", "recovered_fill")
            }
            nxt["move_required"] = True
        self.s["leg"] = nxt
        self.save()

    def _reconcile_paused_close(self):
        leg = self.s["leg"]
        if not leg or not leg.get("close") or leg["ticket"] in self.b.positions:
            return
        if any(o["ticket"] in self.b.orders for o in leg["orders"].values()):
            return
        proof = self.b.closed(leg, self._record_exit)
        checked(proof == leg["close"], "closed_evidence_changed")
        self._detached()
        observations = {k: self._observe(item) for k, item in leg["orders"].items()}
        self._finish_closed(leg, observations)

    def _interrupted_exit_time(self, leg, item, hold):
        b = self.b
        action = self._record(hold["key"])
        checked(action and action[1] in ("observed_target", "terminal_original"), "awaiting_move_evidence")
        request = json.loads(action[0])
        self._move_context(hold["key"], request)
        state, moved, order = b.observe_move(request, item, False)
        checked(
            state == "filled"
            and order.position_id == hold["successor"]
            and moved == (action[1] == "observed_target")
            and order.position_id != leg["position_id"],
            "awaiting_move_evidence",
        )
        parent = b.history_order(leg["anchor"])
        checked(
            parent
            and parent.position_id == leg["position_id"]
            and parent.magic == leg["magic"] == b.magic
            and parent.state == b.api.ORDER_STATE_FILLED
            and parent.volume_current == 0
            and parent.type in (b.api.ORDER_TYPE_BUY_STOP, b.api.ORDER_TYPE_SELL_STOP)
            and b.side(parent.type) == leg["side"]
            and units(parent.volume_initial, b.step) == leg["volume_steps"]
            and units(parent.sl, b.tick) == leg["initial"],
            "entry_order_mismatch",
        )
        parent_open = b.retired_fill(
            parent, dict(side=leg["side"], volume=leg["volume_steps"], sl=leg["initial"])
        )
        checked(parent_open["entry_ticks"] == leg["entry_ticks"], "entry_order_mismatch")
        parent_close = b.closed(leg, self._record_exit)
        child_open = b.retired_fill(order, dict(item, sl=units(order.sl, b.tick)))
        child = self._leg(child_open, leg["rung"], units(order.sl, b.tick))
        child_close = b.closed(child, self._record_exit)
        checked(
            parent_close and child_close and (not leg["close"] or leg["close"] == parent_close),
            "closed_evidence_changed",
        )
        return max(parent_close["time"], child_close["time"])

    def retire_interrupted_move(self, expected_epoch, expected_ticket):
        """Explicit paused reconciliation. No broker write or helper lease is issued."""
        with self.lock:
            try:
                s, b = self.s, self.b
                last = s.get("last_interrupted_retirement")
                if (
                    not self.running
                    and s["mode"] == "ready"
                    and not s["armed"]
                    and not s["leg"]
                    and not s["pending"]
                    and not s["detached"]
                    and last == [expected_epoch, expected_ticket, s["epoch"]]
                ):
                    b.refresh()
                    checked(not b.positions and not b.orders, "prior_exposure_still_open")
                    return True
                checked(
                    not self.running
                    and s["mode"] == "active"
                    and s["epoch"] == expected_epoch
                    and not s["pending"]
                    and not s["detached"],
                    "interrupted_move_retirement",
                )
                leg = s["leg"]
                checked(
                    leg
                    and set(leg["orders"]) == {"reverse"}
                    and leg.get("move_required")
                    and leg["orders"]["reverse"]["ticket"] == expected_ticket,
                    "move_binding_changed",
                )
                b.refresh()
                checked(not b.positions and not b.orders, "prior_exposure_still_open")
                self._reconcile_intents()
                checked(not self.unresolved, self.move_wait or "uncertain_broker_action")
                hold = s.get("interrupted_move") or {}
                item = leg["orders"]["reverse"]
                checked(
                    hold.get("ticket") == expected_ticket
                    and hold.get("key") == item.get("move", {}).get("key"),
                    "interrupted_move_retirement",
                )
                last_exit = self._interrupted_exit_time(leg, item, hold)
                b.refresh()
                checked(not b.orders and not b.positions, "prior_exposure_still_open")
                # Release the in-memory hold only after the durable retirement commits.
                ready = dict(s)
                ready.pop("interrupted_move")
                ready.update(
                    leg=None,
                    pending=None,
                    armed=False,
                    mode="ready",
                    epoch=uuid4().hex,
                    floor=last_exit,
                )
                ready["last_interrupted_retirement"] = [expected_epoch, expected_ticket, ready["epoch"]]
                self.save(ready)
                self.lease_signature = None
                self.reason = ""
                return True
            except (Wait, HelperUnavailable) as exc:
                self.reason = str(exc) if isinstance(exc, Wait) else "awaiting_move_evidence"
                return False

    def _finish_closed(self, leg, observations):
        cause = leg["close"]["cause"]
        if any(state != "canceled" for state, _ in observations.values()):
            return False
        # Canceling an unfilled reverse explicitly ends a stopped-out chain.
        # A still-working reverse or its confirmed execution remains part of that chain.
        final_sl = cause == "full" and leg["rung"] == 24 and not observations
        interrupted_sl = cause == "full" and "reverse" in observations
        if cause != "manual" and not (final_sl or interrupted_sl):
            return False
        if (
            self.s["detached"]
            or any(
                v["magic"] == self.b.magic or v["position_id"] == leg["position_id"]
                for v in self.b.verified_positions.values()
            )
            or any(o.magic == self.b.magic for o in self.b.orders.values())
        ):
            raise Wait("prior_exposure_still_open")
        armed = self.s["armed"] and not final_sl
        self.s.update(
            leg=None,
            pending=None,
            armed=armed,
            floor=leg["close"]["time"],
            mode="manual_wait" if armed else "ready",
            epoch=uuid4().hex,
        )
        self.save()
        return True

    def _cancel_closed_counter(self, leg, item):
        row = self._record(item["key"])
        b = self.b
        checked(
            row
            and row[1] == "accepted"
            and int(row[2]) == item["ticket"]
            and json.loads(row[0])["action"] == b.api.TRADE_ACTION_PENDING,
            "unconfirmed_cleanup_owner",
        )

        def guard():
            b.session()
            positions = b.api.positions_get(symbol=b.symbol)
            checked(
                positions is not None and all(p.identifier != leg["position_id"] for p in positions),
                "prior_exposure_still_open",
            )
            b.guard_counter(item)

        key = f"{self.s['epoch']}:{leg['position_id']}:remove:{item['ticket']}"
        self._effect(key, b.remove(item["ticket"]), guard)

    def _active(self):
        s = self.s
        b = self.b
        leg = s["leg"]
        if leg.get("rung_parent"):
            self._detached()
            if leg.get("rung_parent"):
                if leg["ticket"] in b.positions:
                    self._protect(leg)
                raise Wait("awaiting_predecessor_stop_classification")
        close_wait = None
        if not leg["close"] and leg["ticket"] not in b.positions:
            try:
                leg["close"] = b.closed(leg, self._record_exit)
                self.save()
            except (Wait, HelperUnavailable) as exc:
                close_wait = exc
        # The first access can publish a verifier challenge. Ask for the order in
        # the same poll even when the independent predecessor history is pending.
        observations = {k: self._observe(i) for k, i in leg["orders"].items()}
        fills = [k for k, v in observations.items() if v[0] == "filled"]
        if close_wait is not None and not fills:
            raise close_wait
        if fills:
            if len(fills) > 1:
                checked(
                    (leg.get("close") or {}).get("cause") == "support" and len(fills) == 2,
                    "multiple_alternative_fills",
                )
                fills.sort(key=lambda k: observations[k][1].time_done_msc)
                checked(
                    observations[fills[0]][1].time_done_msc < observations[fills[1]][1].time_done_msc,
                    "alternative_fill_order_ambiguous",
                )
            k = fills[0]
            others = [
                x
                for x in observations
                if x != k
                and not (observations[x][0] == "unsent" and not self._record(leg["orders"][x]["key"]))
            ]
            checked(
                all(observations[x][0] in ("working", "filled") for x in others),
                "surviving_order_unconfirmed",
            )
            remaining = leg["orders"][others[0]] if others else None
            support = (leg.get("close") or {}).get("cause") == "support"
            self._successor(leg, leg["orders"][k], observations[k][1], support or k == "reentry", remaining)
            return
        if any(status == "missing" for status, _ in observations.values()):
            raise Wait("waiting_exact_order")
        self._detached()
        if leg["close"]:
            # An intent rejected by preflight has never crossed the send boundary.
            for kind in list(leg["orders"]):
                pending = leg["orders"][kind]
                if not pending["ticket"] and not self._record(pending["key"]):
                    del leg["orders"][kind]
                    observations.pop(kind, None)
                    self.save()
            cause = leg["close"]["cause"]
            if self._finish_closed(leg, observations):
                return
            if cause == "manual":
                for kind, (status, _) in observations.items():
                    if status == "working":
                        self._cancel_closed_counter(leg, leg["orders"][kind])
                        break
                raise Wait("waiting_counterorder_cancellation")
            if leg["rung"] < 24 and "reverse" not in leg["orders"] and cause in ("full", "support"):
                side = opposite(leg["side"])
                entry = (
                    leg["initial"]
                    if leg.get("first_manual")
                    else leg["entry_ticks"] - sign(leg["side"]) * self.distance
                )
                self._item(
                    leg, "reverse", side, entry, entry - sign(side) * self.distance, self.bank[leg["rung"]]
                )
                return
            if cause == "support":
                if any(state == "canceled" for state, _ in observations.values()):
                    raise Wait("counterorder_canceled")
                side = leg["side"]
                entry = leg["entry_ticks"] + sign(side) * self.distance
                self._item(leg, "reentry", side, entry, leg["entry_ticks"], self.bank[leg["rung"] - 1])
            if cause == "full" and any(state == "working" for state, _ in observations.values()):
                raise Wait("waiting_reverse_fill")
            return
        currently = b.verified_positions.get(leg["ticket"])
        checked(
            currently
            and currently["position_id"] == leg["position_id"]
            and currently["volume_steps"] == leg["volume_steps"],
            "active_position_changed",
        )
        if observations.get("reverse", (None, None))[0] == "canceled":
            raise Wait("counterorder_canceled")
        self._protect(leg)
        if leg["rung"] == 24:
            return
        side = opposite(leg["side"])
        entry = (
            leg["initial"]
            if leg.get("first_manual", False)
            else leg["entry_ticks"] - sign(leg["side"]) * self.distance
        )
        sl = entry - sign(side) * self.distance
        item = self._item(leg, "reverse", side, entry, sl, self.bank[leg["rung"]])
        if leg.get("move_required"):
            status, o = self._observe(item)
            checked(status == "working", "survivor_not_working")
            if units(o.price_open, b.tick) != entry or units(o.sl, b.tick) != sl:
                key = f"{s['epoch']}:{leg['position_id']}:move:{o.ticket}"

                def guard():
                    b.guard_live(leg)
                    b.guard_counter(item)

                self._modify(item, key, o, entry, sl, guard)
                raise Wait("awaiting_survivor_move")
            item.update(entry=entry, sl=sl)
            leg["move_required"] = False
            self.save()

    def close(self):
        with self.lock:
            try:
                if self.lease_signature:
                    self.b.helpers.register([], self.run_id)
            finally:
                self.db.close()
                self.ownership.close()
