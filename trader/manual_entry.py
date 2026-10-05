"""One-shot Telegram prices for the first pair; broker mutations share durable initial intents.

The owner thread validates/parses commands and reconciles before replacement. Automatic entry
and continuation keep their own policies; manual-only exposure guards protect this new path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import time

from .capital import CapitalError
from .diagnostics import begin_diagnostic_cycle
from .initial_entry import InitialTriggerEntry, TERMINAL, exact_entry_plan

D = Decimal
TRIGGER_TEMPLATE = "/TRIGGER:\nBUY:\nSELL:"
TRIGGER_HELP = (
    "Нажми кнопку «Скопировать шаблон», вставь шаблон в поле сообщения, "
    "укажи одну цену для обеих сторон и отправь.\n"
    "Пример заполненного сообщения:\n/TRIGGER:\nBUY: 3998.50\nSELL: 3998.50\n"
    "Кнопка копирует текст в буфер; вставить его нужно самостоятельно. "
    "Для входа сначала включи MANUAL_INITIAL_ENTRY_ENABLED и отправь /start."
)


def is_trigger_command(text: str) -> bool:
    return re.match(r"^\s*/trigger(?=\s|:|$)", text, re.IGNORECASE) is not None


def parse_manual_trigger(text: str) -> D:
    """Accept three nonempty lines, case-insensitive labels and dot-decimal prices."""
    if len(text) > 512:
        raise ValueError("Слишком длинная /TRIGGER; нужны BUY и SELL с одной ценой")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 3 or not re.fullmatch(r"/trigger\s*:", lines[0], re.IGNORECASE):
        raise ValueError("Формат: /TRIGGER: затем отдельные строки BUY: цена и SELL: цена")
    values = {}
    for line in lines[1:]:
        match = re.fullmatch(r"(BUY|SELL)\s*:\s*(\S+)", line, re.IGNORECASE)
        if not match or match[1].upper() in values:
            raise ValueError("Нужны ровно по одной строке BUY: цена и SELL: цена")
        raw = match[2]
        try:
            value = D(raw)
        except InvalidOperation as exc:
            raise ValueError("Цена должна быть числом с десятичной точкой") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError("Обе цены должны быть конечными и положительными")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
            raise ValueError("Используй десятичную точку, без запятой, знака и экспоненты")
        values[match[1].upper()] = value
    if values["BUY"] != values["SELL"]:
        raise ValueError("Заданные цены BUY и SELL обязательно должны совпадать")
    return values["BUY"]


class ManualInitialEntry(InitialTriggerEntry):
    def arm(self):
        self.state.manual_initial_mode = True
        self.state.armed = True
        self.state.paused = False
        self.state.waiting_current_candle = False
        self.state.phase = "MANUAL_TRIGGER_WAIT"
        self.save()
        self.bot.telegram.send("Ручной первоначальный вход разрешён. Ожидаю /TRIGGER с "
                               "одинаковой ценой BUY и SELL; свечной фильтр выключен.")

    def _plan(self, level, size):
        try:
            return exact_entry_plan(level, self.bot.capital.initial_entry_market(self.bot.cfg.epic), size)
        except (CapitalError, ValueError, KeyError, ArithmeticError) as exc:
            raise ValueError(f"Цена /TRIGGER не принята; уровень не изменён: {exc}") from exc

    def submit(self, level):
        if not self.bot.cfg.manual_initial_entry_enabled:
            raise RuntimeError("MANUAL_INITIAL_ENTRY_ENABLED=false: ручной вход выключен")
        if self.bot.cfg.dry_run:
            raise RuntimeError("BOT_DRY_RUN=true: торговые заявки заблокированы")
        if not self.bot.reconciled or self.state.pending_finalization:
            raise RuntimeError("Предыдущий цикл или startup ещё сверяется; /TRIGGER заблокирована")
        if self.state.manual or self.state.continuation_managed:
            raise RuntimeError("Идёт ручная сверка или continuation; /TRIGGER не применяется")
        e = self.entry
        if e and e.get("mode") != "MANUAL":
            raise RuntimeError("Первоначальные заявки другого режима ещё сверяются")
        if self.state.active and not e:
            raise RuntimeError("Цикл уже идёт; новая /TRIGGER допустима после его завершения")
        if not e and self.bot._unknown_cycle_mutations():
            raise RuntimeError("Есть незавершённые брокерские операции; /TRIGGER заблокирована")
        if self.state.paused or (not e and not self.state.armed):
            raise RuntimeError("Сначала /start; /TRIGGER до разрешения входа не применяется")
        if e and self._exposed():
            raise RuntimeError("Первый fill уже подтверждён; новый уровень не применяется")
        size = D(e["size"]) if e else self.bot.cfg.size_for(1)
        plan = self._plan(level, size)  # invalid request never cancels the previous pair
        if e:
            positions, orders = self._snapshot()
            if self._exposed():
                self._drop_replacement()
                self.save()
                raise RuntimeError("Обнаружен первый fill; новая /TRIGGER отброшена")
            if positions or e.get("conflict"):
                raise RuntimeError("Неоднозначная позиция/identity; замена заблокирована")
            desired = e.get("replacement_level") or e["level"]
            if D(desired) == level:
                self.notice("Такой уровень уже принят; повторная пара не отправляется.")
                return
            e["replacement_level"] = str(level)
            e["cancel_reason"] = "REPLACE"
            e["flat_checks"] = 0
            self.save()
            self.notice(f"Замена на {level} сохранена; сначала подтверждаю отмену старой пары. "
                        "До её разрешения действует последний принятый новый уровень.")
            self.tick()
            return
        # New ownership only follows repeated flat snapshots, then fresh price/rule validation.
        for _ in range(3):
            if self.bot._cycle_positions() or any(self.bot._order_epic(o) == self.bot.cfg.epic
                                                  for o in self.bot.capital.working_orders()):
                raise RuntimeError("Есть позиции/ордера GOLD; новая ручная пара заблокирована")
        plan = self._plan(level, size)
        number = max(self.state.attempt_counter, self.state.diagnostic_cycle_number) + 1
        self.state.reset()
        self.state.manual_initial_mode = True
        self.state.attempt_counter = self.state.active_attempt_id = number
        self.state.diagnostic_cycle_number = self.state.cycle_id = number
        self.state.cycle_attempt = 1
        self.state.active = True
        self.state.phase = "MANUAL_INITIAL_ENTRY"
        self.state.initial_entry = {
            "mode": "MANUAL", "cycle_id": number, "attempt_id": number, "stage": "ORDERS",
            "size": str(size), "round": 0, "orders": {}, "round_history": [],
            "level": str(level), "replacement_level": "", "cancel_reason": "",
            "stop_requested": False, "flat_checks": 0,
            "created_at": time.time(), "history_cursor": time.time(),
        }
        # _submit_pair atomically saves both UNKNOWN intents before either POST.
        self._submit_pair(plan)
        begin_diagnostic_cycle(self.bot.cfg.diagnostic_log_file, number, self.state.completed_cycles)

    def _snapshot(self):
        e = self.entry
        positions = self.bot._cycle_positions()
        orders = [self.bot._order_data(o) for o in self.bot.capital.working_orders()
                  if self.bot._order_epic(o) == self.bot.cfg.epic]
        activity = self.bot.capital.activity()
        if time.time() - e["created_at"] > 86400:
            start = e["history_cursor"]
            end = min(start + 86400, time.time())
            if end > start:
                activity += self.bot.capital.activity(
                    from_date=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                    to_date=datetime.fromtimestamp(end, timezone.utc).isoformat())
                e["history_cursor"] = end if end < time.time() - 1 else e["created_at"]
        for side, order in e["orders"].items():
            self._reconcile(side, order, positions, orders, activity)
            # Manual replacement cannot relinquish an owned residual using an older full fill.
            if order.get("exposure_id") and any(o.get("dealId") == order["order_id"] for o in orders):
                order.pop("fill", None)
        self.save()
        return positions, orders

    def _exposed(self):
        return any(o.get("fill") or o.get("exposure_id") or o["status"] == "EXECUTED"
                   for o in self.entry["orders"].values())

    def _drop_replacement(self):
        if self.entry.get("replacement_level"):
            self.entry["replacement_level"] = ""
            self.notice("Во время замены появился fill: новая цена отброшена, завершаю прежнюю пару.")
        self.entry["cancel_reason"] = ""

    def stop(self):
        self.entry["replacement_level"] = ""
        super().stop()

    def tick(self):
        e = self.entry
        if not e:
            return
        if e["cycle_id"] != self.state.cycle_id or e["attempt_id"] != self.state.active_attempt_id:
            self.notice("Manual initial entry: ownership mismatch; broker mutations blocked")
            return
        positions, orders = self._snapshot()
        if e.get("conflict"):
            self.notice(e["conflict"])
            return
        owned = {o.get("exposure_id") for o in e["orders"].values()}
        owned.update(o["fill"]["id"] for o in e["orders"].values() if o.get("fill"))
        if set(positions) - owned:
            self.notice("Ручной вход: неизвестная позиция; новые операции заблокированы до сверки identity.")
            return
        if self._exposed():
            self._drop_replacement()
            self.save()
            fills = {side: o["fill"] for side, o in e["orders"].items() if o.get("fill")}
            if len(fills) == 2:
                self._handoff(positions)
                return
            if len(fills) == 1:
                side, fill = next(iter(fills.items()))
                missing = "SELL" if side == "BUY" else "BUY"
                other = e["orders"][missing]
                # Required for this new replacement path: historic/partial exposure is not
                # permission to create an orphan or oversize missing-side MARKET.
                current = positions.get(fill["id"], {})
                if (D(str(current.get("size", "NaN"))) != D(e["size"])
                        or D(fill["size"]) != D(e["size"])):
                    self.notice("Первая позиция отсутствует или исполнена частично; MARKET запрещён до сверки.")
                    return
                if other["status"] in TERMINAL and not other.get("exposure_id"):
                    self._fallback(missing, other)
            return
        if e["stop_requested"]:
            e["cancel_reason"] = "STOP"
        elif any(o["status"] in TERMINAL for o in e["orders"].values()):
            e["cancel_reason"] = e["cancel_reason"] or "REJECT"
        if not e["cancel_reason"]:
            return
        self.save()
        for order in e["orders"].values():
            if order["status"] == "PENDING" and not order.get("cancel"):
                self._cancel(order)
                return
        if not all(o["status"] in TERMINAL for o in e["orders"].values()):
            return
        if positions or orders:
            e["flat_checks"] = 0
            self.save()
            return
        e["flat_checks"] += 1
        self.save()
        if e["flat_checks"] < 3:
            return
        replacement = e.get("replacement_level")
        if e["stop_requested"] or not replacement:
            self._wait_for_command(paused=e["stop_requested"])
            return
        if self.bot.cfg.dry_run:
            return
        try:
            plan = self._plan(D(replacement), D(e["size"]))
        except ValueError as exc:
            self._wait_for_command(paused=False)
            self.bot._send_report(f"Старая пара отменена, новая цена больше недопустима: {exc}. Нужна новая /TRIGGER.")
            return
        e["round_history"].append(e["orders"])
        e.update(replacement_level="", cancel_reason="", flat_checks=0)
        self._submit_pair(plan)

    def _wait_for_command(self, *, paused):
        self.state.reset()
        self.state.manual_initial_mode = True
        self.state.paused = paused
        self.state.armed = not paused
        self.state.phase = "PAUSED" if paused else "MANUAL_TRIGGER_WAIT"
        self.save()
        self.bot._send_report("Старые ордера разрешены, fills нет. " + (
            "PAUSED: нужен новый /start, затем /TRIGGER." if paused else
            "Уровень использован; ожидаю новую /TRIGGER, автоматического повтора нет."))
