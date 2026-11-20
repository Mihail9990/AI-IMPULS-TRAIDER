"""Account-bound MT5 reads and checked native commands for the RECOVERY strategy."""

from decimal import Decimal


class Wait(RuntimeError):
    """The requested observation is not yet complete."""


class CheckRejected(Wait):
    def __init__(self, retcode):
        super().__init__("order_check_rejected")
        self.retcode = retcode


def checked(ok, reason):
    if not ok:
        raise Wait(reason)


def units(value, quantum):
    value, quantum = Decimal(str(value)), Decimal(str(quantum))
    checked(value.is_finite() and quantum.is_finite() and quantum > 0 and value >= 0, "invalid_grid")
    rounded = (value / quantum).to_integral_value()
    # MT5 exposes IEEE doubles. Permit representation error, never half a tick.
    tolerance = min(quantum / 100, Decimal("1e-12") * max(value, quantum))
    checked(abs(rounded * quantum - value) <= tolerance, "off_grid")
    return int(rounded)


class Broker:
    def __init__(self, api, helpers, account, server, symbol, magic):
        self.api, self.helpers = api, helpers
        self.account, self.server, self.symbol, self.magic = int(account), server, symbol, int(magic)
        self.connected = False
        self.tick = self.step = None
        self.positions, self.orders = {}, {}

    def session(self):
        a, t = self.api.account_info(), self.api.terminal_info()
        self.connected = bool(a and t and t.connected and a.login == self.account and a.server == self.server)
        checked(self.connected, "account_or_connection")
        checked(
            a.margin_mode == self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
            and a.trade_allowed
            and a.trade_expert
            and t.trade_allowed
            and not t.tradeapi_disabled,
            "trading_permission",
        )

    def refresh(self):
        self.session()
        info = self.api.symbol_info(self.symbol)
        checked(
            info is not None and info.trade_tick_size > 0 and info.volume_step > 0, "symbol_not_available"
        )
        self.tick, self.step = Decimal(str(info.trade_tick_size)), Decimal(str(info.volume_step))
        self.min_vol, self.max_vol = units(info.volume_min, self.step), units(info.volume_max, self.step)
        self.facts = self.helpers.snapshot()
        checked(
            (self.facts["login"], self.facts["server"], self.facts["symbol"])
            == (self.account, self.server, self.symbol)
            and Decimal(self.facts["tick_size"]) == self.tick
            and Decimal(self.facts["volume_step"]) == self.step,
            "verifier_binding",
        )
        pp = self.api.positions_get(symbol=self.symbol)
        oo = self.api.orders_get(symbol=self.symbol)
        checked(pp is not None and oo is not None, "inventory_unavailable")
        self.positions = {p.ticket: p for p in pp}
        self.orders = {o.ticket: o for o in oo}
        self.verified_positions = {p["ticket"]: p for p in self.facts["positions"]}
        self.verified_orders = {o["ticket"]: o for o in self.facts["orders"]}
        checked(
            self.positions.keys() == self.verified_positions.keys()
            and self.orders.keys() == self.verified_orders.keys(),
            "inventory_changing",
        )
        for p in pp:
            v = self.verified_positions[p.ticket]
            checked(
                (
                    p.identifier,
                    p.magic,
                    p.reason,
                    p.time_msc,
                    self.side(p.type),
                    units(p.volume, self.step),
                    units(p.price_open, self.tick),
                    units(p.sl, self.tick),
                    units(p.tp, self.tick),
                )
                == tuple(
                    v[k]
                    for k in (
                        "position_id",
                        "magic",
                        "reason",
                        "opened_msc",
                        "side",
                        "volume_steps",
                        "entry_ticks",
                        "sl_ticks",
                        "tp_ticks",
                    )
                ),
                "position_changing",
            )
        for o in oo:
            v = self.verified_orders[o.ticket]
            checked(
                (
                    o.position_id,
                    o.magic,
                    o.type,
                    o.state,
                    o.reason,
                    units(o.volume_initial, self.step),
                    units(o.volume_current, self.step),
                    units(o.price_open, self.tick),
                    units(o.sl, self.tick),
                    units(o.tp, self.tick),
                    units(o.price_stoplimit, self.tick),
                )
                == tuple(
                    v[k]
                    for k in (
                        "position_id",
                        "magic",
                        "type",
                        "state",
                        "reason",
                        "initial_steps",
                        "remaining_steps",
                        "price_ticks",
                        "sl_ticks",
                        "tp_ticks",
                        "stoplimit_ticks",
                    )
                ),
                "order_changing",
            )
        self.session()

    def side(self, typ):
        if typ in (self.api.ORDER_TYPE_BUY, self.api.ORDER_TYPE_BUY_STOP):
            return "BUY"
        if typ in (self.api.ORDER_TYPE_SELL, self.api.ORDER_TYPE_SELL_STOP):
            return "SELL"
        raise Wait("unexpected_order_type")

    def human(self, reason):
        return reason in (
            self.api.ORDER_REASON_CLIENT,
            self.api.ORDER_REASON_MOBILE,
            self.api.ORDER_REASON_WEB,
        )

    def history_order(self, ticket):
        orders = self.api.history_orders_get(ticket=ticket)
        checked(orders is not None, "order_history_unavailable")
        if len(orders) != 1:
            return None
        o = orders[0]
        checked(o.ticket == ticket and o.symbol == self.symbol, "order_history_scope")
        report = self.helpers.order(ticket)
        checked(
            (report["login"], report["server"], report["symbol"]) == (self.account, self.server, self.symbol),
            "order_history_scope",
        )
        row = report["order"]
        checked(
            (
                o.ticket,
                o.position_id,
                o.magic,
                o.type,
                o.state,
                o.reason,
                o.time_setup_msc,
                o.time_done_msc,
                units(o.volume_initial, self.step),
                units(o.volume_current, self.step),
                units(o.price_open, self.tick),
                units(o.sl, self.tick),
                units(o.tp, self.tick),
                units(o.price_stoplimit, self.tick),
            )
            == tuple(
                row[k]
                for k in (
                    "ticket",
                    "position_id",
                    "magic",
                    "type",
                    "state",
                    "reason",
                    "time_msc",
                    "done_msc",
                    "initial_steps",
                    "remaining_steps",
                    "price_ticks",
                    "sl_ticks",
                    "tp_ticks",
                    "stoplimit_ticks",
                )
            ),
            "order_history_disagrees",
        )
        return o

    def deals(self, position):
        result = self.api.history_deals_get(position=position)
        checked(result is not None, "deal_history_unavailable")
        values = {}
        for d in result:
            if d.type not in (self.api.DEAL_TYPE_BUY, self.api.DEAL_TYPE_SELL):
                continue
            checked(
                d.symbol == self.symbol
                and d.position_id == position
                and d.ticket > 0
                and d.order > 0
                and d.ticket not in values,
                "deal_history_scope",
            )
            values[d.ticket] = d
        return sorted(values.values(), key=lambda d: (d.time_msc, d.ticket))

    def identify(self, p, ticket=None, manual=False, native_stop=False):
        ds = self.deals(p["position_id"])
        ins = [d for d in ds if d.entry == self.api.DEAL_ENTRY_IN]
        checked(
            ins
            and len(ins) == len(ds)
            and len({d.order for d in ins}) == 1
            and all(self.side(d.type) == p["side"] and d.magic == p["magic"] for d in ins)
            and sum(units(d.volume, self.step) for d in ins) == p["volume_steps"],
            "entry_incomplete",
        )
        anchor = ins[0].order
        checked(not ticket or anchor == ticket, "entry_order_mismatch")
        o = self.history_order(anchor)
        checked(
            o
            and o.magic == p["magic"]
            and o.position_id == p["position_id"]
            and self.side(o.type) == p["side"],
            "entry_order_unconfirmed",
        )
        initial = units(o.volume_initial, self.step)
        residual = units(o.volume_current, self.step)
        complete = o.state == self.api.ORDER_STATE_FILLED and residual == 0
        partial = (
            manual and native_stop and o.state == self.api.ORDER_STATE_CANCELED and 0 < residual < initial
        )
        checked((complete or partial) and initial - residual == p["volume_steps"], "entry_order_unconfirmed")
        if native_stop:
            checked(
                o.type in (self.api.ORDER_TYPE_BUY_STOP, self.api.ORDER_TYPE_SELL_STOP), "not_native_stop"
            )
        if manual:
            checked(
                p["magic"] == 0
                and self.human(p["reason"])
                and self.human(o.reason)
                and all(self.human(d.reason) for d in ins),
                "entry_not_manual",
            )
            if not native_stop:
                checked(o.type in (self.api.ORDER_TYPE_BUY, self.api.ORDER_TYPE_SELL), "not_market_entry")
        return dict(p, anchor=anchor)

    def retired_fill(self, o, item):
        # A complete round-trip may occur between inventories. The broker order and
        # two deal histories identify the retired position without fabricating a live ticket.
        helper = self.helpers.history(o.position_id)
        checked(
            (helper["login"], helper["server"], helper["symbol"], helper["position_id"])
            == (self.account, self.server, self.symbol, o.position_id),
            "history_helper_scope",
        )
        ds = self.deals(o.position_id)
        facts = {r["ticket"]: r for r in helper["deals"]}
        checked(set(facts) == {d.ticket for d in ds}, "history_sources_disagree")
        ins = [d for d in ds if d.entry == self.api.DEAL_ENTRY_IN]
        outs = [d for d in ds if d.entry in (self.api.DEAL_ENTRY_OUT, self.api.DEAL_ENTRY_OUT_BY)]
        checked(
            ins
            and outs
            and len(ins) + len(outs) == len(ds)
            and all(
                d.order == o.ticket and d.magic == o.magic and self.side(d.type) == item["side"] for d in ins
            )
            and all(self.side(d.type) != item["side"] for d in outs)
            and sum(units(d.volume, self.step) for d in ins) == item["volume"]
            and sum(units(d.volume, self.step) for d in outs) == item["volume"],
            "retired_fill_incomplete",
        )
        weighted = 0
        for d in ds:
            r = facts[d.ticket]
            checked(
                (
                    r["order"],
                    r["position_id"],
                    r["magic"],
                    r["type"],
                    r["entry"],
                    r["reason"],
                    r["time_msc"],
                    r["price_ticks"],
                    r["volume_steps"],
                )
                == (
                    d.order,
                    o.position_id,
                    d.magic,
                    d.type,
                    d.entry,
                    d.reason,
                    d.time_msc,
                    units(d.price, self.tick),
                    units(d.volume, self.step),
                ),
                "deal_sources_disagree",
            )
            if d.entry == self.api.DEAL_ENTRY_IN:
                weighted += r["price_ticks"] * r["volume_steps"]
        entry, rem = divmod(weighted, item["volume"])
        checked(rem == 0, "filled_price_off_grid")
        return dict(
            position_id=o.position_id,
            ticket=0,
            anchor=o.ticket,
            magic=o.magic,
            side=item["side"],
            opened_msc=min(d.time_msc for d in ins),
            volume_steps=item["volume"],
            entry_ticks=entry,
            sl_ticks=item["sl"],
            tp_ticks=0,
            history_only=True,
        )

    def closed(self, p, record_exit):
        if any(v["position_id"] == p["position_id"] for v in self.verified_positions.values()):
            return None
        ds = self.deals(p["position_id"])
        ins = [d for d in ds if d.entry == self.api.DEAL_ENTRY_IN]
        outs = [d for d in ds if d.entry in (self.api.DEAL_ENTRY_OUT, self.api.DEAL_ENTRY_OUT_BY)]
        checked(
            ins
            and outs
            and len(ins) + len(outs) == len(ds)
            and all(d.order == p["anchor"] and self.side(d.type) == p["side"] for d in ins)
            and all(self.side(d.type) != p["side"] for d in outs)
            and sum(units(d.volume, self.step) for d in ins) == p["volume_steps"]
            and sum(units(d.volume, self.step) for d in outs) == p["volume_steps"],
            "full_exit_unconfirmed",
        )
        report = self.helpers.history(p["position_id"])
        checked(
            report["position_id"] == p["position_id"]
            and (report["login"], report["server"], report["symbol"])
            == (self.account, self.server, self.symbol),
            "history_helper_scope",
        )
        facts = {
            d["ticket"]: d
            for d in report["deals"]
            if d["type"] in (self.api.DEAL_TYPE_BUY, self.api.DEAL_TYPE_SELL)
        }
        checked(set(facts) == {d.ticket for d in ds}, "history_sources_disagree")
        for d in ds:
            row = facts[d.ticket]
            checked(
                (
                    row["order"],
                    row["position_id"],
                    row["magic"],
                    row["entry"],
                    row["type"],
                    row["reason"],
                    row["time_msc"],
                    row["volume_steps"],
                    row["price_ticks"],
                )
                == (
                    d.order,
                    d.position_id,
                    d.magic,
                    d.entry,
                    d.type,
                    d.reason,
                    d.time_msc,
                    units(d.volume, self.step),
                    units(d.price, self.tick),
                ),
                "deal_sources_disagree",
            )
        cause = None
        reasons = {d.reason for d in outs}
        if len(reasons) == 1 and self.human(next(iter(reasons))):
            cause = "manual"
        elif reasons == {self.api.DEAL_REASON_SL}:
            levels = {facts[d.ticket]["sl_ticks"] for d in outs}
            # A synchronous DONE alone never establishes support. For a rapid exit,
            # the scoped helper acknowledgement and independently reported DEAL_SL
            # together identify the move away from the original risk level.
            supported = p.get("be_proven") or (p.get("be_ack") and p["initial"] != p["entry_ticks"])
            if supported and levels == {p["entry_ticks"]}:
                cause = "support"
            elif levels == {p["initial"]} and p["initial"] > 0:
                cause = "full"
            else:
                raise Wait("unconfirmed_stop_level")
        record_exit(
            p,
            [
                dict(
                    ticket=d.ticket,
                    order=d.order,
                    side=d.type,
                    steps=units(d.volume, self.step),
                    reason=d.reason,
                    time_msc=d.time_msc,
                    price_ticks=facts[d.ticket]["price_ticks"],
                    sl_ticks=facts[d.ticket]["sl_ticks"],
                )
                for d in outs
            ],
        )
        checked(cause is not None, "unclassified_exit")
        return dict(cause=cause, time=max(d.time_msc for d in outs))

    def pending(self, side, entry, sl, volume, comment):
        checked(self.min_vol <= volume <= self.max_vol and entry > 0 and sl > 0, "invalid_order_grid")
        return dict(
            action=self.api.TRADE_ACTION_PENDING,
            symbol=self.symbol,
            magic=self.magic,
            type=self.api.ORDER_TYPE_BUY_STOP if side == "BUY" else self.api.ORDER_TYPE_SELL_STOP,
            price=float(self.tick * entry),
            sl=float(self.tick * sl),
            tp=0.0,
            volume=float(self.step * volume),
            type_time=self.api.ORDER_TIME_GTC,
            type_filling=self.api.ORDER_FILLING_RETURN,
            comment=comment,
        )

    def modify(self, o, entry, sl):
        return dict(
            action=self.api.TRADE_ACTION_MODIFY,
            order=o.ticket,
            symbol=self.symbol,
            price=float(self.tick * entry),
            sl=float(self.tick * sl),
            tp=o.tp,
            stoplimit=o.price_stoplimit,
            type_time=o.type_time,
            expiration=o.time_expiration,
            magic=self.magic,
        )

    def move_quote(self, request, side, *, admissible=False):
        quote = self.api.symbol_info_tick(self.symbol)
        if not quote:
            return ""
        market = Decimal(str(quote.ask if side == "BUY" else quote.bid))
        if not market.is_finite() or market <= 0:
            return ""
        if admissible:
            info = self.api.symbol_info(self.symbol)
            old = self.orders.get(request["order"])
            if not info or not old:
                return ""
            point = Decimal(str(info.point))
            minimum = point * info.trade_stops_level
            freeze = point * info.trade_freeze_level
            entry, sl = Decimal(str(request["price"])), Decimal(str(request["sl"]))
            direction = 1 if side == "BUY" else -1
            distance = direction * (entry - market)
            if (
                distance <= 0
                or distance < minimum
                or direction * (entry - sl) < minimum
                or (
                    freeze > 0
                    and (direction * (Decimal(str(old.price_open)) - market) <= freeze or distance <= freeze)
                )
            ):
                return ""
        return str(market.normalize())

    def observe_move(self, request, item, manual):
        ticket = item["ticket"]
        magic = 0 if manual else self.magic
        checked(
            request["order"] == ticket
            and request["symbol"] == self.symbol
            and request["magic"] == self.magic,
            "move_binding_changed",
        )
        live = self.orders.get(ticket)
        o = live or self.history_order(ticket)
        checked(o is not None, "awaiting_move_evidence")
        kind = self.api.ORDER_TYPE_BUY_STOP if item["side"] == "BUY" else self.api.ORDER_TYPE_SELL_STOP
        checked(
            (
                o.symbol,
                o.ticket,
                o.magic,
                o.type,
                units(o.volume_initial, self.step),
                units(o.tp, self.tick),
                units(o.price_stoplimit, self.tick),
            )
            == (self.symbol, ticket, magic, kind, item["volume"], units(request["tp"], self.tick), 0)
            and (not manual or self.human(o.reason)),
            "counterorder_identity",
        )
        original = item.get("original_sl", 0) if manual else item["sl"]
        geometry = (units(o.price_open, self.tick), units(o.sl, self.tick))
        target = (units(request["price"], self.tick), units(request["sl"], self.tick))
        moved = geometry == target
        checked(moved or geometry == (item["entry"], original), "counterorder_geometry_changed")
        remaining = units(o.volume_current, self.step)
        if live:
            checked(
                o.state == self.api.ORDER_STATE_PLACED and o.position_id == 0 and remaining == item["volume"],
                "partial_or_changed_order",
            )
            return "working", moved, o
        if o.state == self.api.ORDER_STATE_CANCELED and o.position_id == 0 and remaining == item["volume"]:
            return "canceled", moved, o
        checked(
            o.state == self.api.ORDER_STATE_FILLED and o.position_id > 0 and remaining == 0,
            "counterorder_fill_incomplete",
        )
        ds = [d for d in self.deals(o.position_id) if d.entry == self.api.DEAL_ENTRY_IN]
        report = self.helpers.history(o.position_id)
        checked(
            (report["login"], report["server"], report["symbol"], report["position_id"])
            == (self.account, self.server, self.symbol, o.position_id),
            "history_helper_scope",
        )
        entries = [r for r in report["deals"] if r["entry"] == self.api.DEAL_ENTRY_IN]
        rows = {r["ticket"]: r for r in entries}
        checked(
            ds
            and len(rows) == len(entries)
            and set(rows) == {d.ticket for d in ds}
            and sum(units(d.volume, self.step) for d in ds) == item["volume"],
            "entry_incomplete",
        )
        for d in ds:
            r = rows[d.ticket]
            checked(
                d.order == ticket
                and d.magic == magic
                and self.side(d.type) == item["side"]
                and (
                    d.order,
                    d.position_id,
                    d.magic,
                    d.type,
                    d.reason,
                    d.time_msc,
                    units(d.volume, self.step),
                    units(d.price, self.tick),
                )
                == tuple(
                    r[k]
                    for k in (
                        "order",
                        "position_id",
                        "magic",
                        "type",
                        "reason",
                        "time_msc",
                        "volume_steps",
                        "price_ticks",
                    )
                ),
                "entry_order_mismatch",
            )
        return "filled", moved, o

    def protect(self, p, sl):
        return dict(
            action=self.api.TRADE_ACTION_SLTP,
            position=p.ticket,
            symbol=self.symbol,
            sl=float(self.tick * sl),
            tp=p.tp,
            magic=self.magic,
        )

    def remove(self, ticket):
        return dict(action=self.api.TRADE_ACTION_REMOVE, order=ticket, symbol=self.symbol, magic=self.magic)

    def validate_target(self, request):
        self.session()
        if request["action"] == self.api.TRADE_ACTION_SLTP:
            ticket = request["position"]
            fresh = self.api.positions_get(ticket=ticket)
            prior = self.positions.get(ticket)
            checked(
                prior
                and fresh
                and len(fresh) == 1
                and fresh[0].symbol == self.symbol
                and (fresh[0].identifier, fresh[0].magic, fresh[0].volume, fresh[0].sl, fresh[0].tp)
                == (prior.identifier, prior.magic, prior.volume, prior.sl, prior.tp),
                "stale_position_action",
            )
        elif request["action"] == self.api.TRADE_ACTION_MODIFY:
            ticket = request["order"]
            fresh = self.api.orders_get(ticket=ticket)
            prior = self.orders.get(ticket)
            checked(
                prior
                and fresh
                and len(fresh) == 1
                and fresh[0].symbol == self.symbol
                and (
                    fresh[0].type,
                    fresh[0].state,
                    fresh[0].magic,
                    fresh[0].volume_current,
                    fresh[0].price_open,
                    fresh[0].sl,
                    fresh[0].tp,
                    fresh[0].type_time,
                    fresh[0].time_expiration,
                )
                == (
                    prior.type,
                    prior.state,
                    prior.magic,
                    prior.volume_current,
                    prior.price_open,
                    prior.sl,
                    prior.tp,
                    prior.type_time,
                    prior.time_expiration,
                ),
                "stale_pending_action",
            )

    def guard_live(self, leg):
        pp = self.api.positions_get(ticket=leg["ticket"])
        checked(pp and len(pp) == 1, "active_position_changed")
        p = pp[0]
        checked(
            (
                p.symbol,
                p.identifier,
                p.ticket,
                p.magic,
                p.time_msc,
                self.side(p.type),
                units(p.volume, self.step),
                units(p.price_open, self.tick),
            )
            == (
                self.symbol,
                leg["position_id"],
                leg["ticket"],
                leg["magic"],
                leg["opened_msc"],
                leg["side"],
                leg["volume_steps"],
                leg["entry_ticks"],
            ),
            "active_position_changed",
        )

    def guard_counter(self, item):
        oo = self.api.orders_get(ticket=item["ticket"])
        checked(oo and len(oo) == 1, "support_counter_changed")
        o = oo[0]
        checked(
            (
                o.symbol,
                o.ticket,
                o.magic,
                o.type,
                o.state,
                o.position_id,
                units(o.volume_initial, self.step),
                units(o.volume_current, self.step),
                units(o.price_open, self.tick),
                units(o.sl, self.tick),
                units(o.tp, self.tick),
                units(o.price_stoplimit, self.tick),
            )
            == (
                self.symbol,
                item["ticket"],
                self.magic,
                self.api.ORDER_TYPE_BUY_STOP if item["side"] == "BUY" else self.api.ORDER_TYPE_SELL_STOP,
                self.api.ORDER_STATE_PLACED,
                0,
                item["volume"],
                item["volume"],
                item["entry"],
                item["sl"],
                0,
                0,
            ),
            "support_counter_changed",
        )

    def preflight(self, request, guard=None):
        self.validate_target(request)
        if guard:
            guard()
        check = self.api.order_check(request)
        if check is None or check.retcode != 0:
            raise CheckRejected(check.retcode if check else None)
        self.validate_target(request)
        if guard:
            guard()
