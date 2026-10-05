"""Durable, exclusive first-pair entry via a STOP/LIMIT pair.

Only the Bot owner mutates state. Workers return POST results; both may-send intents are
committed before either worker starts. Unknown submissions without broker identity stay blocked:
Capital supplies no client idempotency key, so price/direction are never ownership evidence.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
import time

from .capital import CapitalError
from .diagnostics import begin_diagnostic_cycle
from .events import normalize_events

D = Decimal
SIDES = ("BUY", "SELL")
TERMINAL = {"REJECTED", "CANCELLED"}


def entry_plan(open_bid: D, market: dict, threshold: D, offset: D, size: D):
    """Return the exact agreed level/types or None inside the direction band.

    Do not reinterpret a protection-distance rule as an entry-distance rule. Public Capital
    metadata exposes quote precision, minimum step and size rules; remaining broker validation
    is authoritative and follows the explicit rejection workflow, never a moved price.
    """
    snapshot = market["snapshot"]
    bid, ask = D(str(snapshot["bid"])), D(str(snapshot["offer"]))
    if not all(x.is_finite() and x > 0 for x in (open_bid, bid, ask, threshold, offset, size)):
        raise ValueError("Initial entry: invalid quote/settings")
    if ask < bid:
        raise ValueError("Initial entry: ASK below BID")
    move = bid - open_bid
    if -threshold < move < threshold:
        return None
    if snapshot.get("marketStatus") != "TRADEABLE" or snapshot.get("delayTime", 0) != 0:
        raise ValueError("Initial entry: market is closed or quotes are delayed")
    if D(str(snapshot.get("scalingFactor", 1))) != 1:
        raise ValueError("Initial entry: non-unit scalingFactor needs an explicit price convention")
    level = bid + offset if move >= threshold else bid - offset
    if level > ask:
        types = {"BUY": "STOP", "SELL": "LIMIT"}
    elif 0 < level < bid:
        types = {"BUY": "LIMIT", "SELL": "STOP"}
    else:
        raise ValueError("Initial entry: BID offset places level inside/on BID-ASK spread; level unchanged")
    rules = market.get("dealingRules", {})
    def points(name):
        rule = rules.get(name, {})
        if rule.get("unit") != "POINTS":
            raise ValueError(f"Initial entry: missing/unsupported {name} unit")
        value = D(str(rule.get("value")))
        if not value.is_finite() or value <= 0:
            raise ValueError(f"Initial entry: invalid {name}")
        return value
    step = points("minStepDistance")
    minimum, maximum, increment = (points(name) for name in
                                   ("minDealSize", "maxDealSize", "minSizeIncrement"))
    precision = int(snapshot["decimalPlacesFactor"])
    if precision < 0 or precision > 12:
        raise ValueError("Initial entry: unsupported quote precision")
    quantum = D(1).scaleb(-precision)
    if level % quantum or level % step or min(abs(level-bid), abs(level-ask)) < step:
        raise ValueError("Initial entry: exact level violates broker precision/minStepDistance; no rounding")
    if not minimum <= size <= maximum or size % increment:
        raise ValueError("Initial entry: requested size violates broker size limits/increment")
    return {"level": str(level), "bid": str(bid), "ask": str(ask), "types": types}


class InitialTriggerEntry:
    def __init__(self, bot):
        self.bot = bot

    @property
    def state(self):
        return self.bot.state

    @property
    def entry(self):
        return self.state.initial_entry

    def save(self):
        self.state.save(self.bot.cfg.state_file)

    def notice(self, text):
        if self.entry.get("notice") != text:
            self.entry["notice"] = text
            self.save()
            self.bot._send_report(text)

    def filter_tick(self):
        if self.state.active or self.state.continuation_managed or self.bot.cfg.dry_run:
            return
        closed, current = self.bot.capital.entry_candles(self.bot.cfg.epic, self.bot.cfg.candle_minutes)
        chosen = None
        if not self.state.waiting_current_candle and D(closed["range"]) >= self.bot.cfg.entry_range:
            chosen, source = closed, "closed"
        elif D(current["range"]) >= self.bot.cfg.entry_range:
            chosen, source = current, "current"
        if chosen is None:
            self.state.waiting_current_candle = True
            return
        opening = D(chosen["open"])
        if not opening.is_finite() or opening <= 0:
            raise ValueError("Reference candle has invalid BID-open")
        number = max(self.state.attempt_counter, self.state.diagnostic_cycle_number) + 1
        self.state.reset()
        self.state.attempt_counter = self.state.active_attempt_id = number
        self.state.diagnostic_cycle_number = self.state.cycle_id = number
        self.state.cycle_attempt = 1
        self.state.active = True
        self.state.armed = False
        self.state.phase = "INITIAL_TRIGGER_ENTRY"
        self.state.initial_entry = {
            "cycle_id": number, "attempt_id": number, "stage": "DIRECTION",
            "candle": dict(chosen), "filter_source": source, "filter_approved": True,
            "threshold": str(self.bot.cfg.initial_entry_direction_distance),
            "offset": str(self.bot.cfg.initial_entry_order_offset),
            "size": str(self.bot.cfg.size_for(1)), "stop_requested": False,
            "flat_checks": 0, "round": 0, "orders": {}, "round_history": [],
            "created_at": time.time(), "history_cursor": time.time(), "cancel_reason": "",
        }
        self.save()
        begin_diagnostic_cycle(self.bot.cfg.diagnostic_log_file, number, self.state.completed_cycles)
        self.notice(f"Первоначальный вход: фильтр подтверждён ({source}); "
                    f"BID-open={opening}; ожидаю отклонение ±{self.entry['threshold']}. "
                    "До двух fills SL/TP отсутствуют.")

    def stop(self):
        self.entry["stop_requested"] = True
        self.state.paused = True
        self.save()
        self.notice("/stop сохранён: без fills отменяю начальные ордера; при fill завершаю пару "
                    "и сопровождаю цикл, следующий вход запрещён.")

    def resume(self):
        # Once cancellation starts its outcome must be reconciled before another approval.
        exposed = any(o.get("fill") or o.get("exposure_id") for o in self.entry["orders"].values())
        unresolved_cancel = any(o.get("cancel") and not o.get("fill")
                                and o["status"] not in TERMINAL
                                for o in self.entry["orders"].values())
        if (self.entry.get("cancel_reason") and not exposed) or unresolved_cancel:
            raise RuntimeError("Отмена первоначального входа ещё сверяется; дождитесь её результата")
        self.entry["cancel_reason"] = ""
        self.entry["stop_requested"] = False
        self.state.paused = False
        for order in self.entry["orders"].values():
            fallback = order.get("fallback")
            if fallback and fallback["status"] == "REJECTED":
                order.setdefault("fallback_history", []).append(dict(fallback))
                order.pop("fallback")
        self.save()
        self.notice("Первоначальный вход продолжен; UNKNOWN-заявки не отправляются повторно.")

    def tick(self):
        e = self.entry
        if not e:
            return
        if (e["cycle_id"] != self.state.cycle_id or e["attempt_id"] != self.state.active_attempt_id):
            self.notice("Initial entry: ownership mismatch; broker mutations blocked")
            return
        positions = self.bot._cycle_positions()
        orders = [self.bot._order_data(o) for o in self.bot.capital.working_orders()
                  if self.bot._order_epic(o) == self.bot.cfg.epic]
        if e["stage"] == "DIRECTION":
            if e["stop_requested"]:
                if positions or orders:
                    self.notice("Initial /stop: unexpected broker exposure; ownership retained")
                else:
                    self._pause_flat()
                return
            if positions or orders:
                e["flat_checks"] = 0
                self.save()
                self.notice("Initial entry waits for broker-flat positions/orders")
                return
            e["flat_checks"] += 1
            self.save()
            if e["flat_checks"] < 3 or self.bot.cfg.dry_run:
                return
            try:
                plan = entry_plan(D(e["candle"]["open"]),
                                  self.bot.capital.initial_entry_market(self.bot.cfg.epic),
                                  D(e["threshold"]), D(e["offset"]), D(e["size"]))
            except (CapitalError, ValueError, KeyError, ArithmeticError) as exc:
                self.notice(str(exc))
                return
            if plan:
                self._submit_pair(plan)
            return
        # Read current history plus one bounded older interval per tick after a long restart.
        activity = self.bot.capital.activity()
        if time.time() - e["created_at"] > 86400:
            start = e["history_cursor"]
            end = min(start + 86400, time.time())
            if end > start:
                activity += self.bot.capital.activity(
                    from_date=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                    to_date=datetime.fromtimestamp(end, timezone.utc).isoformat(),
                )
                e["history_cursor"] = end if end < time.time() - 1 else e["created_at"]
        for side, order in e["orders"].items():
            self._reconcile(side, order, positions, orders, activity)
        self.save()
        if e.get("conflict"):
            self.notice(e["conflict"])
            return
        owned_positions = {o["fill"]["id"] for o in e["orders"].values() if o.get("fill")}
        owned_positions.update(o.get("exposure_id") for o in e["orders"].values())
        if set(positions) - owned_positions:
            self.notice("Initial entry: uncorrelated broker position; no mutation until identity is proved")
            return
        filled = [side for side, o in e["orders"].items() if o.get("fill") or o.get("exposure_id")]
        if len(filled) == 2:
            if not all(o.get("fill") for o in e["orders"].values()):
                return
            self._handoff(positions)
            return
        if len(filled) == 1:
            missing = "SELL" if filled[0] == "BUY" else "BUY"
            other = e["orders"][missing]
            if other["status"] in TERMINAL and e["orders"][filled[0]].get("fill"):
                self._fallback(missing, other)
            # Waiting order, UNKNOWN POST, executed-but-unpublished fill or cancellation race:
            # none permits another entry, a timeout close, or premature protection.
            return
        if e["stop_requested"]:
            e["cancel_reason"] = "STOP"
        elif any(o["status"] in TERMINAL for o in e["orders"].values()):
            e["cancel_reason"] = e["cancel_reason"] or "RETRY"
        if not e["cancel_reason"]:
            return
        self.save()
        for order in e["orders"].values():
            if order["status"] == "PENDING" and not order.get("cancel"):
                self._cancel(order)
                return  # next tick re-reads both sides before cancelling another
        if not all(o["status"] in TERMINAL for o in e["orders"].values()):
            return
        # Absence alone was not sufficient: both terminal outcomes are now positively proved.
        if positions or orders:
            e["flat_checks"] = 0
            self.save()
            return
        e["flat_checks"] += 1
        self.save()
        if e["flat_checks"] < 3:
            return
        if e["stop_requested"]:
            self._pause_flat()
        else:
            e["round_history"].append(e["orders"])
            e.update(stage="DIRECTION", orders={}, cancel_reason="", flat_checks=0)
            self.save()  # fresh quote next tick, original filter/open retained

    def _submit_pair(self, plan):
        e = self.entry
        e["round"] += 1
        e.update(stage="ORDERS", level=plan["level"], submission_bid=plan["bid"],
                 submission_ask=plan["ask"], flat_checks=0)
        e["orders"] = {side: {
            "status": "UNKNOWN", "type": plan["types"][side], "reference": "", "order_id": "",
            "intent": f"initial:{e['cycle_id']}:{e['round']}:{side}",
        } for side in SIDES}
        self.save()  # both may-send boundaries precede either POST
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self.bot.capital.initial_working_order,
                                   self.bot.cfg.epic, side, D(e["size"]), D(e["level"]),
                                   e["orders"][side]["type"]): side for side in SIDES}
            for future in as_completed(futures):
                order = e["orders"][futures[future]]
                try:
                    order["reference"] = str(future.result())
                except CapitalError as exc:
                    order["error"] = str(exc)
                    if exc.rejected:
                        order["status"] = "REJECTED"
                except Exception as exc:
                    order["error"] = str(exc)
                self.save()  # owner commits reference before attempting confirmation
        self.notice(f"Начальные STOP/LIMIT отправлены на одном уровне {e['level']}; "
                    "ожидаю подтверждённые fills BUY и SELL, без SL/TP.")

    def _confirmation(self, reference):
        if not reference:
            return {}
        try:
            return self.bot.capital.confirmation(reference)
        except CapitalError:
            return {}  # a failed GET is not a rejection of the original mutation

    @staticmethod
    def _ids(result):
        return {str(result.get("dealId") or ""),
                *(str(x.get("dealId") or "") for x in result.get("affectedDeals", []))} - {""}

    def _reconcile(self, side, order, positions, orders, activity):
        reference = order["reference"]
        if order["status"] == "UNKNOWN" and reference:
            result = self._confirmation(reference)
            if result.get("dealStatus") == "REJECTED":
                order["status"] = "REJECTED"
                order["error"] = str(result.get("reason", "REJECTED"))
            elif result.get("dealStatus") == "ACCEPTED" and result.get("dealId"):
                order["order_id"] = str(result["dealId"])
                order["status"] = "PENDING"
        for item in orders:
            if (reference and item.get("dealReference") == reference
                    and item.get("direction") == side and item.get("dealId")):
                order["order_id"] = str(item["dealId"])
                if order["status"] == "UNKNOWN":
                    order["status"] = "PENDING"
        oid = order["order_id"]
        # Learn an order identity only from the saved broker reference, never quote resemblance.
        events = normalize_events(activity)
        if not oid and reference:
            matches = {x.deal_id for x in events if x.deal_reference == reference
                       and x.event_type == "WORKING_ORDER" and x.deal_id}
            if len(matches) == 1:
                oid = order["order_id"] = matches.pop()
        candidates = [p for p in positions.values() if oid and p.get("workingOrderId") == oid
                      and p.get("direction") == side]
        fallback = order.get("fallback")
        if fallback:
            result = self._confirmation(fallback.get("reference"))
            if result.get("dealStatus") == "REJECTED":
                fallback["status"] = "REJECTED"
                fallback["error"] = str(result.get("reason", "REJECTED"))
            elif result.get("dealStatus") == "ACCEPTED":
                fallback["position_id"] = self.bot._confirmed_position_id(result)
            candidates += [p for p in positions.values() if p.get("direction") == side and (
                (fallback.get("position_id") and p.get("dealId") == fallback["position_id"])
                or (fallback.get("reference") and p.get("dealReference") == fallback["reference"]))]
        for event in events:
            if oid and event.deal_id == oid and event.event_type == "WORKING_ORDER":
                if event.status == "EXECUTED":
                    order["status"] = "EXECUTED"
                elif event.status == "CANCELLED" and order["status"] != "EXECUTED":
                    order["status"] = "CANCELLED"
            if (oid and event.working_order_id == oid and event.event_type == "POSITION"
                    and event.source == "USER" and event.status == "ACCEPTED"
                    and event.direction == side and event.level is not None and event.size is not None
                    and not event.raw.get("details", {}).get("openPrice")):
                candidates.append({"dealId": event.deal_id, "direction": side,
                                   "level": event.level, "size": event.size})
        unique = {str(p.get("dealId")): p for p in candidates if p.get("dealId")}
        if len(unique) > 1:
            self.entry["conflict"] = f"Initial {side}: conflicting owned fills; reconciliation required"
            self.notice(self.entry["conflict"])
            return
        if unique:
            p = next(iter(unique.values()))
            p = positions.get(str(p["dealId"]), p)  # current broker size beats old opening history
            if p.get("level") is not None and p.get("size") is not None:
                fill, size = D(str(p["level"])), D(str(p["size"]))
                if fill.is_finite() and size.is_finite() and fill > 0 and size > 0:
                    # Partial position plus a live residual order must not be treated as two fills.
                    order["exposure_id"] = str(p["dealId"])
                    if any(x.get("dealId") == oid for x in orders):
                        self.notice(f"Initial {side}: position and residual order coexist; awaiting full execution")
                        return
                    order["fill"] = {"id": str(p["dealId"]), "entry": str(fill), "size": str(size)}
                    order["status"] = "FILLED"
        cancel = order.get("cancel")
        if cancel and not order.get("fill"):
            result = self._confirmation(cancel.get("reference"))
            if (result.get("dealStatus") == "ACCEPTED" and oid in self._ids(result)
                    and result.get("status") in {"DELETED", "CANCELLED"}
                    and order["status"] != "EXECUTED"):
                order["status"] = "CANCELLED"
                cancel["status"] = "CONFIRMED"
            elif result.get("dealStatus") == "REJECTED":
                cancel["status"] = "REJECTED"
                self.notice(f"Initial {side}: cancellation rejected; awaiting broker cancellation/fill")
        if order["status"] == "UNKNOWN" and not reference:
            self.notice(f"Initial {side}: POST UNKNOWN without reference; no duplicate, "
                        "broker identity must be reconciled before continuation")

    def _cancel(self, order):
        if self.bot.cfg.dry_run:
            return
        order["cancel"] = {"status": "UNKNOWN", "reference": ""}
        self.save()
        try:
            order["cancel"]["reference"] = self.bot.capital.initial_cancel_order(order["order_id"])
        except Exception as exc:
            # Includes not-found: it could have executed; retain ownership and inspect history.
            order["cancel"]["error"] = str(exc)
        self.save()

    def _fallback(self, side, order):
        if self.bot.cfg.dry_run:
            return
        if order.get("fallback"):
            if order["fallback"]["status"] == "REJECTED":
                self.notice(f"Initial {side}: MARKET fallback rejected; first position retained, "
                            "no automatic retry storm; /start permits one new attempt")
            elif not order["fallback"].get("reference"):
                self.notice(f"Initial {side}: MARKET UNKNOWN without reference; duplicate blocked")
            return
        order["fallback"] = {"status": "UNKNOWN", "reference": ""}
        self.save()
        try:
            order["fallback"]["reference"] = self.bot.capital.open_position(
                self.bot.cfg.epic, side, D(self.entry["size"]))
        except CapitalError as exc:
            order["fallback"]["error"] = str(exc)
            if exc.rejected:
                order["fallback"]["status"] = "REJECTED"
        except Exception as exc:
            order["fallback"]["error"] = str(exc)
        self.save()

    def _handoff(self, positions):
        fills = {side: order["fill"] for side, order in self.entry["orders"].items()}
        if any(f["id"] not in positions for f in fills.values()):
            self.notice("Initial pair fills known but an owned position is absent; reconciliation required")
            return
        if D(fills["BUY"]["size"]) != D(fills["SELL"]["size"]):
            self.notice("Initial pair actual sizes differ; no guessed Recovery or extra order")
            return
        paused = self.state.paused or self.entry["stop_requested"]
        # Both confirmed fills enter ordinary S1 exactly once. Commit handoff before protection
        # I/O; restart then follows the existing protection-repair/replay paths, never opens a pair.
        self.state.active = False
        self.bot.strategy.begin(D(fills["BUY"]["entry"]), D(fills["SELL"]["entry"]))
        for side, leg in (("BUY", self.state.long), ("SELL", self.state.short)):
            leg.deal_id = fills[side]["id"]
            leg.size = D(fills[side]["size"])
            leg.size_confirmation = "broker"
            leg.entry_confirmation = "broker"
        self.bot.strategy.confirm_initial_fills(D(fills["BUY"]["entry"]), D(fills["SELL"]["entry"]))
        self.state.paused = paused
        self.state.initial_submitted_directions = list(SIDES)
        self.state.initial_entry = {}
        self.save()
        self.bot._send_report("Оба первоначальных fills подтверждены. Сценарий 1 и Recovery "
                              "инициализированы; устанавливаю обычную защиту обеих сторон.")
        self.bot._tick_cycle()

    def _pause_flat(self):
        self.state.reset()
        self.state.paused = True
        self.state.phase = "PAUSED"
        self.save()
        self.bot._send_report("Начальные ордера разрешены, позиций нет. PAUSED до /start.")
