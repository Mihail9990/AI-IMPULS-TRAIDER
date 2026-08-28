from __future__ import annotations

import logging
from pathlib import Path
import time
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

from .capital import CapitalClient, CapitalError
from .config import Settings
from .diagnostics import clear_diagnostics, configure_diagnostics
from .engine import Strategy
from .events import (
    find_close_event,
    find_trigger_open_event,
    find_working_order_execution,
    normalize_events,
)
from .execution import ExecutionPolicy, is_crossed_level_rejection, trigger_level_passed
from .model import CycleState, Leg, stop_for
from .reconcile import RemoteSnapshot
from .reporting import cycle_result_text, pnl_text, scenario_nine_result_text, status_text
from .telegram import Telegram


LOG = logging.getLogger(__name__)
D = Decimal


class Bot:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.state = CycleState.load(cfg.state_file)
        self.strategy = Strategy(cfg, self.state)
        self.capital = CapitalClient(cfg)
        self.telegram = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.telegram.offset = self.state.telegram_offset
        self.reconciled = False
        self._flat_checks = 0
        self._missing_exit_since: float | None = None
        self._initial_entry_close: tuple[Leg, str, Decimal] | None = None
        self.execution_policy = ExecutionPolicy()
        # Recover a cleanup that was missed because Pydroid stopped immediately after the tenth
        # completion, and migrate older state files whose marker remained at zero.
        self._clear_diagnostics_if_due()

    def _complete_cycle(self, direction: str, fill: Decimal | None) -> None:
        """Complete one cycle and keep diagnostics only for the latest ten-cycle window."""
        self.strategy.complete(direction, fill)
        self._clear_diagnostics_if_due()

    def _clear_diagnostics_if_due(self) -> None:
        """Truncate all diagnostic logs after every ten cycles since the last cleanup."""
        count = self.state.completed_cycles
        last_cleanup = self.state.diagnostic_cleanup_cycle
        # Use a distance instead of ``count % 10``. This also repairs a missed boundary after a
        # crash/update: completed=11, cleanup=0 is cleaned immediately rather than waiting for 20.
        if count - last_cleanup >= 10:
            self.state.diagnostic_cleanup_cycle = count
            # Persist the marker before truncating so a crash cannot repeatedly clear the log.
            self.state.save(self.cfg.state_file)
            clear_diagnostics(self.cfg.diagnostic_log_file)
            LOG.info(
                "Diagnostic history cleared after completed cycle %s; new 10-cycle window started",
                count,
            )

    def run(self) -> None:
        if self.telegram.offset == 0:
            self.telegram.discard_pending()
            self.state.telegram_offset = self.telegram.offset
            self.state.save(self.cfg.state_file)
        self.telegram.install_commands()
        self.telegram.send(
            f"🤖 Бот запущен\nРежим: {'DEMO' if self.cfg.demo else 'REAL'}\n"
            f"Dry run: {self.cfg.dry_run}\nEpic: {self.cfg.epic}\n"
            f"Размер: {self.cfg.size}\nSL distance: {self.cfg.stop_distance}\n"
            f"Target profit: {self.cfg.target_profit}",
            show_menu=True,
        )
        while True:
            try:
                # Reconcile before processing queued Telegram commands. A /start message may
                # already be waiting when Pydroid launches the process.
                if not self.reconciled:
                    self.reconcile_startup()
                commands = self.telegram.commands()
                # Persist consumed update IDs before a broker-mutating command can run. If
                # Android kills Pydroid immediately after that command, Telegram will not replay
                # the same /settrigger, /start, or /recover on restart.
                self.state.telegram_offset = self.telegram.offset
                self.state.save(self.cfg.state_file)
                self._process_commands(commands)
                self.telegram.flush_pending()
                if not self.state.manual:
                    self.tick()
                self.telegram.flush_pending()
                self.state.save(self.cfg.state_file)
            except Exception as exc:
                LOG.exception("Loop error")
                self.telegram.send(f"⚠️ Ошибка цикла: {exc}")
            # During an active cycle poll at least twice per second even when an older preserved
            # config still contains POLL_SECONDS=1.
            delay = min(self.cfg.poll_seconds, 0.5) if self.state.active else self.cfg.poll_seconds
            time.sleep(delay)

    def _process_commands(self, commands: list[str]) -> None:
        for command in commands:
            try:
                self.command(command)
            except (RuntimeError, ValueError) as exc:
                self.telegram.send(f"⚠️ Команда не выполнена: {exc}")

    def command(self, text: str) -> None:
        command, *args = text.strip().lower().split()
        if command == "/help":
            self.telegram.send(
                "/status /start /startcycle /pause /stop /resume /positions /orders /pnl /cycleinfo\n"
                "/menu — показать клавиатуру /hidemenu — свернуть клавиатуру\n"
                "/recover — повторно связать позиции и установить точные SL/TP\n"
                "/automode — безопасно выйти из ручного режима\n"
                "/profit200 VALUE — личный profit следующих 200 завершённых циклов\n"
                "/dealhistory [DEAL_ID] — история сохранённых сделок или точного ID\n"
                "/sendlog — прислать текущий диагностический файл\n"
                "/setsl long|short PRICE /settp long|short PRICE\n"
                "/settrigger long|short PRICE /canceltrigger long|short\n"
                "/removesl long|short /removetp long|short\n"
                "/abort confirm"
            )
        elif command == "/menu":
            self.telegram.send("⌨️ Командная клавиатура открыта.", show_menu=True)
        elif command == "/hidemenu":
            self.telegram.send(
                "⌨️ Командная клавиатура свернута. Вернуть: /menu.", hide_menu=True
            )
        elif command == "/status":
            self.telegram.send(self.status())
        elif command == "/cycleinfo":
            self.telegram.send("\n".join(self.state.events[-15:]) or "Событий пока нет")
        elif command == "/positions":
            self.telegram.send(str(self.capital.positions()))
        elif command == "/orders":
            self.telegram.send(str(self.capital.working_orders()))
        elif command == "/pnl":
            self.telegram.send(pnl_text(
                self.state, self.capital.positions(), self.capital.transactions()
            ))
        elif command == "/dealhistory":
            self._deal_history(args)
        elif command == "/recover":
            self._recover_manual_positions()
        elif command == "/automode":
            self._exit_manual_mode()
        elif command == "/sendlog":
            self._send_diagnostic_log()
        elif command == "/profit200":
            if len(args) != 1:
                raise RuntimeError("Используйте /profit200 0.4")
            value = D(args[0])
            if value < 0:
                raise RuntimeError("Значение profit не может быть отрицательным")
            self.state.profit_override = value
            self.state.profit_override_remaining = 200
            self.state.save(self.cfg.state_file)
            self.telegram.send(
                f"✅ Для следующих 200 завершённых циклов личный profit установлен: {value}\n"
                f"Обычное значение после них: {self.cfg.target_profit}\n"
                "Текущий уже открытый цикл не пересчитывается."
            )
        elif command in {"/pause", "/stop"}:
            self.state.paused = True
            if not self.state.active:
                self.state.armed = False
                self.state.waiting_current_candle = False
                self.state.phase = "PAUSED"
                self.telegram.send("Пауза: новый цикл не откроется до команды /start.")
            else:
                self.telegram.send(
                    "Пауза принята: текущий цикл продолжится до TP, но следующий цикл не начнётся."
                )
        elif command == "/resume":
            if self.state.manual:
                raise RuntimeError("Ручной режим нельзя снять командой /resume")
            self.arm_cycle()
        elif command == "/abort" and args == ["confirm"]:
            self._manual("Пользователь отключил автоматику")
        elif command in {"/start", "/startcycle"}:
            self.arm_cycle()
        elif command in {"/setsl", "/settp", "/settrigger", "/removesl", "/removetp", "/canceltrigger"}:
            self._manual_command(command, args)
        else:
            self.telegram.send("Неизвестная или неполная команда. /help")

    def _deal_history(self, args: list[str]) -> None:
        """Show the durable local ledger or query Capital activity for one exact dealId."""
        if len(args) > 1:
            raise RuntimeError("Используйте /dealhistory или /dealhistory DEAL_ID")
        if not args:
            records = self.state.deal_history[-20:]
            if not records:
                self.telegram.send("История dealId бота пока пуста.")
                return
            lines = ["🧾 Последние dealId бота:"]
            for item in records:
                lines.append(
                    f"scenario={item.get('scenario', '?')} {item.get('direction', '?')} "
                    f"id={item.get('deal_id', '')}\n"
                    f"entry={item.get('entry', '?')} close={item.get('close_source') or '-'} "
                    f"{item.get('close_level') if item.get('close_level') is not None else ''}"
                )
            self.telegram.send("\n".join(lines))
            return
        deal_id = args[0]
        activity = self.capital.activity(deal_id)
        events = normalize_events(activity)
        if not events:
            self.telegram.send(f"Capital.com не вернул историю для dealId={deal_id}")
            return
        lines = [f"🧾 История dealId={deal_id}"]
        for event in events[-12:]:
            level = event.level if event.level is not None else "-"
            lines.append(
                f"{event.timestamp.isoformat()} {event.event_type} "
                f"source={event.source or '-'} status={event.status or '-'} level={level}"
            )
            if event.source in {"SL", "TP"} and event.level is not None:
                self.state.remember_close(deal_id, event.source, event.level)
        self.state.save(self.cfg.state_file)
        self.telegram.send("\n".join(lines))

    def status(self) -> str:
        return status_text(self.state)

    def arm_cycle(self) -> None:
        if not self.reconciled:
            self.reconcile_startup()
        if not self.reconciled:
            raise RuntimeError("Не удалось завершить сверку с Capital.com")
        if self.state.manual:
            raise RuntimeError("Автоматика в ручном режиме; проверьте /status и /cycleinfo")
        if self.cfg.dry_run:
            raise RuntimeError("BOT_DRY_RUN=true: торговые заявки заблокированы")
        self.state.paused = False
        if self.state.active:
            self.telegram.send("Текущий цикл активен; автоматический запуск следующего цикла включён.")
            return
        self.state.armed = True
        self.state.waiting_current_candle = False
        self.state.phase = "FILTER"
        self.telegram.send(
            f"🔎 Свечной фильтр включён\nТаймфрейм: {self.cfg.candle_minutes} мин\n"
            f"Минимальный диапазон: {self.cfg.entry_range}\nОжидаю условие входа."
        )

    def tick(self) -> None:
        if self.state.armed and not self.state.active and not self.state.paused:
            self._tick_filter()
        elif self.state.active:
            self._tick_cycle()

    def _tick_filter(self) -> None:
        closed, current = self.capital.candle_ranges(self.cfg.epic, self.cfg.candle_minutes)
        if not self.state.waiting_current_candle and closed >= self.cfg.entry_range:
            self._start_cycle(f"закрытая свеча: {closed}")
        elif current >= self.cfg.entry_range:
            self._start_cycle(f"текущая свеча: {current}")
        else:
            self.state.waiting_current_candle = True

    def _start_cycle(self, filter_reason: str = "условие фильтра выполнено") -> None:
        positions = self._cycle_positions()
        orders = [item for item in self.capital.working_orders()
                  if self._order_epic(item) == self.cfg.epic]
        if positions or orders:
            # Capital may keep the just-closed cycle in list endpoints briefly. Starting during
            # that window can bind a new confirmation to an old same-direction position.
            LOG.warning(
                "New cycle delayed until broker is flat: position_ids=%s order_count=%s",
                sorted(positions), len(orders),
            )
            self._flat_checks = 0
            return
        self._flat_checks = getattr(self, "_flat_checks", 0) + 1
        if self._flat_checks < 3:
            LOG.info("Broker flat check %s/3 before new cycle", self._flat_checks)
            return
        self._flat_checks = 0
        bid, ask = self.capital.quote(self.cfg.epic)
        self.strategy.begin(ask, bid)
        assert self.state.long and self.state.short
        self.telegram.send(
            f"🚦 Начинаю сценарий 1\nФильтр: {filter_reason}\nBID: {bid}\nASK: {ask}\n"
            f"Предварительный spread: {ask - bid}\nРазмер каждой стороны: {self.cfg.size}\n"
            f"Предварительный SL BUY: {self.state.long.stop}\n"
            f"Предварительный SL SELL: {self.state.short.stop}"
        )
        opened: list[Leg] = []
        for leg in (self.state.long, self.state.short):
            assert leg
            if opened and not self._position_still_open(opened[0]):
                lost = opened[0]
                self._capture_failure_context(
                    "first initial leg disappeared before opposite entry",
                    RuntimeError(f"{lost.direction} {lost.deal_id} already closed"),
                )
                self.state.reset()
                self.state.paused = True
                self.state.phase = "PAUSED"
                self.state.save(self.cfg.state_file)
                self.telegram.send(
                    f"🚨 Начальный hedge отменён: первая сторона {lost.direction} закрылась "
                    "до открытия противоположной стороны. Вторая заявка не отправлена. "
                    "Проверьте /pnl и запустите новый цикл вручную командой /start."
                )
                return
            error = self._open_initial_leg(leg)
            if error:
                early_close = getattr(self, "_initial_entry_close", None)
                if opened and early_close and early_close[0] is leg:
                    _, source, fill = early_close
                    self._initial_entry_close = None
                    if self._continue_after_second_initial_close(leg, source, fill):
                        return
                if not opened and early_close and early_close[0] is leg:
                    _, source, fill = early_close
                    entry = leg.current_entry
                    result = (fill - entry) if leg.direction == "BUY" else (entry - fill)
                    self.state.reset()
                    self.state.armed = True
                    self.state.phase = "FILTER"
                    self._initial_entry_close = None
                    self.state.save(self.cfg.state_file)
                    self.telegram.send(
                        f"⚠️ Первая сторона {leg.direction} была принята, но закрылась по {source} "
                        "до открытия второй стороны.\n"
                        f"Вход: {entry}\nФактическое закрытие: {fill}\n"
                        f"Результат движения цены: {result}\n"
                        "Вторая заявка не отправлена. Цикл не начат; бот снова ожидает свечной фильтр."
                    )
                    return
                if opened or leg.deal_reference:
                    self._manual(f"Неполный или несинхронизированный hedge {leg.direction}: {error}")
                else:
                    self.state.reset()
                    self.state.armed = True
                    self.state.phase = "FILTER"
                    self.telegram.send(f"⚠️ Первая сторона не открыта после 4 попыток: {error}. Цикл не начат.")
                return
            opened.append(leg)
        assert self.state.long and self.state.short
        self.strategy.confirm_initial_fills(self.state.long.current_entry, self.state.short.current_entry)
        # The first MARKET leg can hit its broker-side stop in the very small window between
        # opening the opposite leg and replacing the provisional distance-based protection with
        # the exact strategy levels.  That is a real scenario-1 stop, not a broken hedge.  Check
        # the broker snapshot before PUT /positions/{dealId} so a legitimate close is replayed by
        # the normal cycle state machine instead of being mislabeled as a 404/manual-mode error.
        if self._continue_after_early_initial_close():
            return
        try:
            # Establish and verify both exact stops first. Only positions that are still active
            # after that safety barrier receive their take profits.
            for leg in (self.state.long, self.state.short):
                self._apply_stop_only(leg)
                if self._continue_after_early_initial_close():
                    return
            for leg in (self.state.long, self.state.short):
                self._apply_take_profit_only(leg)
                if self._continue_after_early_initial_close():
                    return
        except Exception as exc:
            # A position may also close after the snapshot above but while exact protection is
            # being applied.  Reconcile that race once more before stopping automation.
            if self._continue_after_early_initial_close():
                return
            self._capture_failure_context("initial protection failed", exc)
            self._manual(f"Обе стороны открыты, но точные SL/TP не подтверждены: {exc}")
            return
        self.state.save(self.cfg.state_file)
        self.telegram.send(
            f"✅ Цикл полностью открыт\nСценарий: 1\n"
            f"BUY entry: {self.state.long.current_entry}\nSELL entry: {self.state.short.current_entry}\n"
            f"Фактический spread: {self.state.entry_spread}\nRecovery: {self.state.recovery}\n"
            f"BUY SL/TP: {self.state.long.stop} / {self.state.long.take_profit}\n"
            f"SELL SL/TP: {self.state.short.stop} / {self.state.short.take_profit}"
        )

    def _continue_after_second_initial_close(
        self, closed: Leg, source: str, fill: Decimal
    ) -> bool:
        """Continue scenario 1 when the second accepted leg closes before list sync.

        Capital can accept the second MARKET leg, attach its distance-based stop, execute that
        stop, and publish only the surviving first leg in ``/positions``.  Both fills are still
        authoritative, so this is a normal scenario-1 stop rather than an incomplete hedge.
        """
        assert self.state.long and self.state.short
        survivor = self.state.short if closed.direction == "BUY" else self.state.long
        positions = self._cycle_positions()
        if survivor.deal_id not in positions:
            # The same eventual-consistency gap that hid the accepted second leg can briefly
            # hide the first leg as well.  Do not turn a confirmed SL into an incomplete hedge
            # from one empty snapshot: give /positions the normal synchronization window before
            # deciding that the survivor is absent.
            positions = self._retry_missing_positions()
        if survivor.deal_id not in positions:
            LOG.info(
                "Second initial %s closed by %s, but survivor %s is not visible yet; "
                "leaving classification to normal reconciliation",
                closed.direction,
                source,
                survivor.deal_id,
            )
            return False
        self.strategy.confirm_initial_fills(
            self.state.long.current_entry, self.state.short.current_entry
        )
        if source == "TP":
            # Initial entries are submitted with an SL only.  Do not reinterpret an unexpected
            # TP-labelled close while the opposite position is still open.
            return False
        if source != "SL":
            return False
        # _open_initial_leg marks the history-resolved leg closed. Strategy.stopped() deliberately
        # requires an open protected leg, so restore the pre-event state and replay the SL once.
        closed.open = True
        stopped = self.strategy.stopped(
            closed.direction, fill, f"stop:{closed.deal_id}:{fill}"
        )
        if not self._apply_protection(survivor):
            return True
        self._create_trigger(stopped)
        self.state.save(self.cfg.state_file)
        self.telegram.send(
            "🛑 Вторая сторона открылась и сразу закрылась по SL\n"
            f"Сторона: {closed.direction}\n"
            f"Вход: {closed.current_entry}\n"
            f"Плановый SL: {closed.stop}\n"
            f"Фактическое закрытие: {fill}\n"
            f"Recovery: {self.state.recovery}\n"
            f"Осталась сторона: {survivor.direction}\n"
            f"Новый TP: {survivor.take_profit}\n"
            f"Trigger {closed.direction}: {closed.original_trigger_level}\n"
            "Автоматика продолжает сценарий 1."
        )
        return True

    def _continue_after_early_initial_close(self) -> bool:
        """Replay a stop/TP that happened while the sequential hedge was being finalized."""
        positions = self._cycle_positions()
        expected = {
            leg.deal_id for leg in (self.state.long, self.state.short)
            if leg and leg.open and leg.deal_id
        }
        missing = expected.difference(positions)
        if not missing:
            return False
        LOG.warning(
            "Initial hedge changed before exact protection; replaying broker events: "
            "missing=%s present=%s",
            sorted(missing), sorted(positions),
        )
        self.state.save(self.cfg.state_file)
        self._tick_cycle()
        return True

    def _open_initial_leg(self, leg: Leg) -> str | None:
        last_error = "заявка отклонена"
        for _attempt in range(self.execution_policy.attempts):
            try:
                # The previous cycle can remain briefly visible in /positions. Remember every
                # pre-existing id so wait_position cannot bind the new leg to a stale position
                # merely because it has the same direction.
                preexisting_ids = set(self._cycle_positions())
                reference = self.capital.open_position(
                    self.cfg.epic, leg.direction, self.cfg.size,
                    stop_distance=self.cfg.stop_distance,
                )
                confirmation = self.capital.wait_confirmation(reference)
                if confirmation.get("dealStatus") != "ACCEPTED":
                    last_error = confirmation.get("reason") or last_error
                    continue
                # From this point a broker position may exist. Never submit another MARKET order
                # just because /positions has not synchronized yet.
                leg.deal_reference = reference
                deal_id = str(confirmation.get("dealId", ""))
                affected = confirmation.get("affectedDeals") or []
                if affected and isinstance(affected[0], dict):
                    deal_id = str(affected[0].get("dealId") or deal_id)
                # Preserve the accepted fill before waiting for /positions.  A tight broker-side
                # stop can execute before the list endpoint ever exposes the position.
                leg.deal_id = deal_id
                if confirmation.get("level") is not None:
                    leg.current_entry = D(str(confirmation["level"]))
                    leg.original_trigger_level = leg.current_entry
                    leg.stop = stop_for(leg.direction, leg.current_entry, self.cfg.stop_distance)
                position = self.capital.wait_position(
                    deal_id, reference, leg.direction, excluded_ids=preexisting_ids,
                    epic=self.cfg.epic,
                )
                leg.deal_id = str(position["dealId"])
                fill = position.get("level", confirmation.get("level"))
                if fill is None:
                    raise CapitalError("Позиция появилась без фактической цены входа")
                leg.current_entry = D(str(fill))
                self.telegram.send(
                    f"✅ {leg.direction} открыта\nDeal ID: {leg.deal_id}\n"
                    f"Фактический вход: {leg.current_entry}\nПопытка: {_attempt + 1}/4"
                )
                return None
            except Exception as exc:
                last_error = str(exc)
                if leg.deal_reference:
                    close = self._wait_accepted_initial_close(leg)
                    if close is not None:
                        source, fill = close
                        leg.open = False
                        self._initial_entry_close = (leg, source, fill)
                        LOG.warning(
                            "Accepted initial %s closed by %s before /positions synchronized: "
                            "dealId=%s entry=%s fill=%s",
                            leg.direction, source, leg.deal_id, leg.current_entry, fill,
                        )
                        return f"позиция закрылась по {source} до синхронизации /positions"
                    return f"заявка принята, но постоянная позиция не синхронизирована: {last_error}"
        return last_error

    def _wait_accepted_initial_close(
        self, leg: Leg, attempts: int = 120, delay: float = 0.5
    ) -> tuple[str, Decimal] | None:
        """Resolve an accepted first leg that opened and closed between list snapshots.

        Capital.com's position list and detailed activity are eventually consistent.  The demo
        trace showed an ACCEPTED confirmation followed by an empty ``/positions`` response; that
        is not enough to call the position lost.  Keep asking the deal-specific activity endpoint
        for up to roughly one minute so the broker can publish the authoritative SL/TP reason.
        """
        if not leg.deal_id:
            return None
        for attempt in range(attempts):
            try:
                activity = self.capital.activity(leg.deal_id)
                for source in ("SL", "TP"):
                    event = find_close_event(activity, leg.deal_id, source)
                    if event is not None and event.level is not None:
                        LOG.info(
                            "Accepted initial deal resolved from history: dealId=%s "
                            "source=%s fill=%s activity_attempt=%s/%s",
                            leg.deal_id, source, event.level, attempt + 1, attempts,
                        )
                        return source, event.level
            except Exception:
                LOG.warning(
                    "Could not inspect activity for accepted initial deal %s",
                    leg.deal_id,
                    exc_info=True,
                )
            if attempt + 1 < attempts:
                time.sleep(delay)
        return None

    def _position_still_open(self, leg: Leg, attempts: int = 3) -> bool:
        """Confirm the first sequential entry still exists before sending the second one."""
        for attempt in range(attempts):
            try:
                payload = self.capital.position(leg.deal_id)
                position = payload.get("position", payload)
                if str(position.get("dealId", leg.deal_id)) == leg.deal_id:
                    return True
            except CapitalError as exc:
                if "404" not in str(exc):
                    raise
            if attempt + 1 < attempts:
                time.sleep(0.25)
        return False

    def _tick_cycle(self) -> None:
        positions = self._cycle_positions()
        if self.state.phase in {"LONG_ONLY", "SHORT_ONLY"}:
            survivor = self.state.long if self.state.phase == "LONG_ONLY" else self.state.short
            stopped = self.state.short if self.state.phase == "LONG_ONLY" else self.state.long
            assert survivor and stopped
            if survivor.deal_id not in positions:
                # A fast reversal can fill the pending trigger and stop the old survivor before
                # two polling ticks have completed.  In that case the snapshot contains only the
                # newly reopened leg.  Classify/replay both broker events instead of assuming that
                # the old survivor must have reached TP.
                if self._recover_trigger_fill_then_stop(positions, survivor, stopped):
                    return
                if self._recover_trigger_round_trip_from_activity(survivor, stopped):
                    return
                fill = self._closing_fill(survivor, "TP")
                if fill is None:
                    positions = self._retry_missing_positions()
                    if survivor.deal_id in positions:
                        return
                    # The trigger-created position may become visible only during the retry
                    # window.  Re-evaluate the two-event sequence against the newest snapshot;
                    # otherwise a perfectly normal ``trigger fill -> survivor SL`` race is
                    # incorrectly reported as a lost position.
                    if self._recover_trigger_fill_then_stop(positions, survivor, stopped):
                        return
                    if self._recover_trigger_round_trip_from_activity(survivor, stopped):
                        return
                    # A close is never inferred merely from absence in /positions.  Ask the
                    # broker for both possible protected exits after its activity feed has had
                    # time to synchronize.  Only an explicit TP completes the cycle here; an SL
                    # remains pending until its matching trigger fill can be linked.
                    fill = self._wait_closing_fill(survivor, "TP")
                    if fill is None and self._wait_closing_fill(survivor, "SL") is not None:
                        LOG.info(
                            "SL %s confirmed while trigger-created position is still synchronizing",
                            survivor.deal_id,
                        )
                        return
                    if fill is not None:
                        # Continue through the normal TP completion path below.
                        pass
                    else:
                        self._manual(
                            f"Позиция {survivor.direction} исчезла, но Capital.com не подтвердил "
                            "ни SL, ни TP"
                        )
                        return
                if stopped.trigger_id:
                    cancelled = self.capital.delete_working_order(stopped.trigger_id)
                    if not cancelled:
                        # The diagnostic from 2026-08-26 17:30 proves this exact race: TP was
                        # published for the survivor, then the saved trigger executed before our
                        # DELETE and Capital returned error.not-found.dealId.  Resolve the order
                        # from durable activity by its ID; never call the cycle complete while an
                        # untracked trigger-created position may exist.
                        race_loss = self._close_trigger_that_raced_with_tp(stopped)
                        if race_loss is None:
                            self.state.save(self.cfg.state_file)
                            self._manual(
                                "TP и trigger исполнились почти одновременно, но Capital.com "
                                f"не опубликовал однозначный итог workingOrderId={stopped.trigger_id}. "
                                "Trigger сохранён; проверьте /positions и /dealhistory."
                            )
                            return
                        self.state.realized_losses += race_loss
                        LOG.info(
                            "Working order %s already absent; activity has no accepted trigger "
                            "position, treating cancellation as idempotent",
                            stopped.trigger_id,
                        )
                    stopped.trigger_id = stopped.trigger_reference = ""
                self._complete_cycle(survivor.direction, fill)
                if self.state.paused:
                    self.state.armed = False
                    self.state.phase = "PAUSED"
                    suffix = "Следующий цикл ожидает /start."
                else:
                    self.state.armed = True
                    self.state.waiting_current_candle = False
                    self.state.phase = "FILTER"
                    suffix = "Перехожу к фильтру следующего цикла."
                self.telegram.send(
                    f"✅ Цикл завершён по TP {survivor.direction}. {suffix}\n"
                    f"{cycle_result_text(self.state, survivor.direction, fill, self.cfg.size)}\n"
                    f"{self.status()}"
                )
                return
            self._detect_trigger_fill(positions)
            if (
                self.state.phase in {"LONG_ONLY", "SHORT_ONLY"}
                and survivor.deal_id in positions
                and not self._protection_matches(positions[survivor.deal_id], survivor)
            ):
                if not self._apply_protection(survivor):
                    return
            self._ensure_expected_trigger()
            return

        known = {leg.deal_id: leg for leg in (self.state.long, self.state.short) if leg and leg.open and leg.deal_id}
        missing = [leg for deal_id, leg in known.items() if deal_id not in positions]
        if not missing:
            self._missing_exit_since = None
            # A previous PUT/confirmation may have timed out after Capital accepted it.  State
            # already contains the new scenario math, so verify broker protection on every
            # stable snapshot and repair only mismatched levels.  This makes protection updates
            # self-healing instead of being a one-shot side effect of the transition tick.
            for deal_id, leg in known.items():
                if not self._protection_matches(positions[deal_id], leg):
                    if not self._apply_protection(leg):
                        return
            self._detect_trigger_fill(positions)
            self._ensure_expected_trigger()
            return
        # `/positions` is eventually consistent and can omit exactly one still-open leg. Always
        # stabilize the snapshot briefly before interpreting absence as an exit.
        positions = self._retry_missing_positions(attempts=3, delay=0.1)
        missing = [leg for deal_id, leg in known.items() if deal_id not in positions]
        if not missing:
            return
        if len(missing) != 1:
            if len(missing) == 1:
                # Continue below using the stable broker snapshot.
                self._missing_exit_since = None
                pass
            else:
                # Capital's deal-filtered activity endpoint can remain empty even though the
                # mobile application already shows the close.  Fetch the global durable feed
                # once and use it as a second index for both deal IDs.
                try:
                    global_activity = self.capital.activity()
                except CapitalError:
                    LOG.warning("Global activity unavailable while both legs are absent",
                                exc_info=True)
                    global_activity = []
                if not isinstance(global_activity, list):
                    global_activity = []
                tp_closes = [
                    (leg, self._closing_fill_any_index(leg, "TP", global_activity))
                    for leg in missing
                ]
                confirmed = [(leg, fill) for leg, fill in tp_closes if fill is not None]
                if len(confirmed) == 1:
                    self._missing_exit_since = None
                    winner, fill = confirmed[0]
                    for loser in missing:
                        if loser is winner:
                            continue
                        stop_fill = self._closing_fill_any_index(
                            loser, "SL", global_activity
                        )
                        if stop_fill is not None and loser.open:
                            self.strategy.stopped(
                                loser.direction, stop_fill,
                                f"stop:{loser.deal_id}:{stop_fill}",
                            )
                    self._complete_cycle(winner.direction, fill)
                    self.state.armed = not self.state.paused
                    self.state.phase = "FILTER" if self.state.armed else "PAUSED"
                    self.telegram.send(
                        "✅ Обе позиции исчезли из списка, но Capital.com подтвердил TP.\n"
                        + cycle_result_text(self.state, winner.direction, fill, self.cfg.size)
                    )
                    return
                # Capital.com commonly publishes the SL activity first and the opposite TP
                # several seconds later.  The diagnostic log showed exactly that ordering:
                # both positions were already absent, BUY had source=SL, while SELL activity
                # was still empty.  This is a synchronisation delay, not an ambiguous loss.
                stop_confirmed = any(
                    self._closing_fill_any_index(leg, "SL", global_activity) is not None
                    for leg in missing
                )
                now = time.monotonic()
                if stop_confirmed and self._missing_exit_since is None:
                    self._missing_exit_since = now
                pending_for = now - self._missing_exit_since if self._missing_exit_since else 0
                if stop_confirmed:
                    LOG.info(
                        "Both positions absent; SL confirmed and TP activity pending "
                        "(elapsed=%.1fs; continuing durable reconciliation)", pending_for,
                    )
                    return
                self._missing_exit_since = None
                # No protected close has been indexed yet. Staying in reconciliation is safer
                # than a false manual takeover: no broker positions are open and no new order is
                # submitted while the authoritative history catches up.
                LOG.info(
                    "Both positions absent and broker exit sources are not indexed yet; "
                    "continuing durable reconciliation"
                )
                return
        if len(missing) != 1:
            return
        lost = missing[0]
        survivor = self.state.short if lost.direction == "BUY" else self.state.long
        assert survivor
        # During initial protection finalization a newly submitted TP can execute before the
        # read-back request sees it.  A missing leg is therefore not necessarily a stop.  Check
        # the broker event source before applying stop bookkeeping.  Because every strategy TP
        # lies beyond the opposite SL, wait for the opposite position/activity to catch up, book
        # that SL, and then complete the cycle normally.
        tp_fill = self._closing_fill(lost, "TP")
        if tp_fill is not None:
            if survivor.deal_id in positions:
                positions = self._retry_missing_positions()
            if survivor.deal_id in positions:
                LOG.info(
                    "TP %s confirmed while opposite position %s is still synchronizing",
                    lost.deal_id,
                    survivor.deal_id,
                )
                return
            stop_fill = self._wait_closing_fill(survivor, "SL")
            if stop_fill is None:
                self._manual(
                    f"TP {lost.direction} исполнен, но закрытие противоположной "
                    f"стороны {survivor.direction} по SL ещё не подтверждено"
                )
                return
            self.strategy.stopped(
                survivor.direction,
                stop_fill,
                f"stop:{survivor.deal_id}:{stop_fill}",
            )
            self._complete_cycle(lost.direction, tp_fill)
            self.state.armed = not self.state.paused
            self.state.phase = "FILTER" if self.state.armed else "PAUSED"
            self.state.save(self.cfg.state_file)
            suffix = (
                "Перехожу к фильтру следующего цикла."
                if self.state.armed else "Следующий цикл ожидает /start."
            )
            self.telegram.send(
                f"✅ Take Profit {lost.direction} исполнился во время установки защиты.\n"
                f"Противоположный SL {survivor.direction}: {stop_fill}\n{suffix}\n"
                f"{cycle_result_text(self.state, lost.direction, tp_fill, self.cfg.size)}"
            )
            return
        if not survivor.open or survivor.deal_id not in positions:
            self._manual("Сверка: невозможно однозначно определить закрытую сторону")
            return
        fill = self._wait_closing_fill(lost)
        if fill is None:
            self._manual(f"Позиция {lost.direction} исчезла, но цена исполнения не найдена")
            return
        # A missing leg while both were open is a stop. A missing survivor while a trigger
        # is pending is handled by the branch below before this state can be mutated.
        stopped = self.strategy.stopped(lost.direction, fill, f"stop:{lost.deal_id}:{fill}")
        # Queue the broker event before follow-up actions. Previously protection/trigger helpers
        # queued their messages first, making Telegram appear to show trigger before the SL.
        self.telegram.send(
            f"🛑 Stop Loss исполнен — сценарий {self.state.scenario} продолжается\n"
            f"Сторона закрыта: {lost.direction}\nDeal ID: {lost.deal_id}\n"
            f"Плановый SL: {lost.stop}\nФактическое закрытие: {fill}\n"
            f"SL slippage: {abs(lost.stop - fill) if lost.stop is not None else '-'}\n"
            f"Recovery после SL: {self.state.recovery}\n"
            f"Осталась сторона: {survivor.direction}\nНовый TP: {survivor.take_profit}\n"
            f"Следующее действие: подтвердить защиту {survivor.direction}, затем поставить "
            f"trigger {stopped.direction} на {stopped.original_trigger_level}.\n"
            "Номер сценария пока НЕ меняется."
        )
        if not self._apply_protection(survivor):
            # If TP completion did not finish the cycle, the survivor itself closed during its
            # protection PUT. Preserve strategy order by creating the already-required trigger
            # for the first stopped side; the next tick will classify the survivor's SL/TP.
            if self.state.active and not stopped.trigger_id:
                self._create_trigger(stopped)
                self.state.save(self.cfg.state_file)
            return
        self._create_trigger(stopped)
        self.state.save(self.cfg.state_file)

    def _close_trigger_that_raced_with_tp(self, leg: Leg) -> Decimal | None:
        """Resolve TP/trigger race automatically and return the additional realized loss."""
        trigger_id = leg.trigger_id
        for attempt in range(120):
            activity = self.capital.activity()
            opened = find_trigger_open_event(activity, trigger_id, leg.direction)
            positions = self._cycle_positions()
            position = next(
                (item for item in positions.values()
                 if str(item.get("workingOrderId", "")) == trigger_id),
                None,
            )
            if opened is None and position is not None:
                deal_id = str(position.get("dealId", ""))
                entry = D(str(position.get("level", leg.original_trigger_level)))
            elif opened is not None and opened.deal_id and opened.level is not None:
                deal_id, entry = opened.deal_id, opened.level
            else:
                executed = find_working_order_execution(activity, trigger_id)
                if executed is None:
                    # 404 can also mean an already-cancelled order. Three quiet reads are enough.
                    if attempt >= 2:
                        return D("0")
                    time.sleep(0.5)
                    continue
                if attempt + 1 < 120:
                    time.sleep(0.5)
                    continue
                return None

            leg.deal_id = deal_id
            leg.current_entry = entry
            leg.open = True
            self.state.remember_deal(leg, self.state.scenario + 1)
            position = positions.get(deal_id, position)
            if position is not None:
                reference = self.capital.close_position(deal_id)
                result = self.capital.wait_confirmation(reference)
                if result.get("dealStatus") != "ACCEPTED" or result.get("level") is None:
                    raise CapitalError(result.get("reason") or "Поздняя trigger-позиция не закрыта")
                close = D(str(result["level"]))
            else:
                close_event = (
                    find_close_event(activity, deal_id, "SL")
                    or find_close_event(activity, deal_id, "TP")
                )
                if close_event is None or close_event.level is None:
                    time.sleep(0.5)
                    continue
                close = close_event.level
            loss = max(D("0"), entry - close) if leg.direction == "BUY" else max(
                D("0"), close - entry
            )
            leg.open = False
            self.state.remember_close(deal_id, "TP_TRIGGER_RACE", close)
            self.state.save(self.cfg.state_file)
            self.telegram.send(
                "⚡ TP и trigger исполнились почти одновременно\n"
                f"Поздняя сторона: {leg.direction}\nDeal ID: {deal_id}\n"
                f"Trigger fill: {entry}\nФактическое закрытие: {close}\n"
                f"Дополнительный убыток: {loss}\nЦикл завершается автоматически."
            )
            return loss
        return None

    @staticmethod
    def _protection_matches(position: dict, leg: Leg) -> bool:
        """Return whether the broker snapshot contains the strategy's current SL and TP."""
        stop = position.get("stopLevel")
        target = position.get("profitLevel")
        actual_stop = D(str(stop)) if stop is not None else None
        actual_target = D(str(target)) if target is not None else None
        return actual_stop == leg.stop and actual_target == leg.take_profit

    def _retry_missing_positions(self, attempts: int = 10, delay: float = 0.5) -> dict[str, dict]:
        """Protect against short-lived empty /positions responses from the broker."""
        positions: dict[str, dict] = {}
        for _ in range(attempts):
            time.sleep(delay)
            positions = self._cycle_positions()
            expected = [leg.deal_id for leg in (self.state.long, self.state.short)
                        if leg and leg.open and leg.deal_id]
            if any(deal_id in positions for deal_id in expected):
                return positions
        return positions

    def _detect_trigger_fill(self, positions: dict[str, dict]) -> None:
        for leg in (self.state.long, self.state.short):
            if not leg or leg.open or not leg.trigger_id:
                continue
            candidate = next((data for data in positions.values()
                              if data.get("direction") == leg.direction and data.get("workingOrderId") == leg.trigger_id), None)
            if not candidate:
                continue
            fill = D(str(candidate["level"]))
            self.strategy.reopened(leg.direction, fill, str(candidate["dealId"]), f"reopen:{candidate['dealId']}")
            if self.state.scenario == self.cfg.max_scenarios:
                self._enter_manual_nine()
            else:
                self.telegram.send(
                    f"🔄 Trigger исполнен — переход в сценарий {self.state.scenario}\n"
                    f"Переоткрыта сторона: {leg.direction}\nDeal ID: {candidate['dealId']}\n"
                    f"Сохранённый trigger: {leg.original_trigger_level}\n"
                    f"Фактический вход: {fill}\n"
                    f"Trigger slippage: {abs(leg.original_trigger_level - fill)}\n"
                    f"Recovery нового сценария: {self.state.recovery}\n"
                    f"Новый SL {leg.direction}: {leg.stop}\nНовый TP {leg.direction}: "
                    f"{leg.take_profit}\nСледующее действие: подтвердить защиту обеих сторон."
                )
                if not self._apply_protection(self.state.long):
                    self.state.save(self.cfg.state_file)
                    return
                if not self._apply_protection(self.state.short):
                    self.state.save(self.cfg.state_file)
                    return

    def _trigger_fill_candidate(self, positions: dict[str, dict], leg: Leg) -> dict | None:
        """Return the position opened by this leg's saved working order, if visible."""
        if leg.open or not leg.trigger_id:
            return None
        return next(
            (
                data for data in positions.values()
                if data.get("direction") == leg.direction
                and data.get("workingOrderId") == leg.trigger_id
            ),
            None,
        )

    def _recover_trigger_fill_then_stop(
        self, positions: dict[str, dict], survivor: Leg, stopped: Leg
    ) -> bool:
        """Replay a trigger fill immediately followed by the previous survivor's SL.

        Capital.com can expose only the new trigger-created position in ``/positions`` while the
        local state is still in LONG_ONLY/SHORT_ONLY.  The disappeared survivor is not "lost": its
        activity source determines whether it closed by SL or TP.
        """
        candidate = self._trigger_fill_candidate(positions, stopped)
        if candidate is None:
            return False
        stop_fill = self._wait_closing_fill(survivor, "SL")
        if stop_fill is None:
            return False
        reopened_fill = D(str(candidate["level"]))
        reopened_id = str(candidate["dealId"])
        self.strategy.reopened(
            stopped.direction,
            reopened_fill,
            reopened_id,
            f"reopen:{reopened_id}",
        )
        stopped.deal_reference = str(candidate.get("dealReference") or stopped.deal_reference)
        self.strategy.stopped(
            survivor.direction,
            stop_fill,
            f"stop:{survivor.deal_id}:{stop_fill}",
        )
        if self.state.scenario == self.cfg.max_scenarios:
            self._enter_manual_nine()
            return True
        if not self._apply_protection(stopped):
            return True
        self._create_trigger(survivor)
        self.state.save(self.cfg.state_file)
        self.telegram.send(
            "🔁 Восстановлена быстрая последовательность событий\n"
            f"Trigger {stopped.direction}: {stopped.original_trigger_level}\n"
            f"Фактический вход: {reopened_fill}\n"
            f"SL {survivor.direction}: {stop_fill}\n"
            f"Сценарий: {self.state.scenario}\n"
            f"Recovery: {self.state.recovery}\n"
            f"Осталась сторона: {stopped.direction}\n"
            f"Новый TP: {stopped.take_profit}\n"
            f"Новый trigger {survivor.direction}: {survivor.original_trigger_level}"
        )
        return True

    def _recover_trigger_round_trip_from_activity(self, survivor: Leg, stopped: Leg) -> bool:
        """Replay a trigger position that opened and closed between two position snapshots."""
        if not stopped.trigger_id:
            return False
        activity = self.capital.activity()
        opened = find_trigger_open_event(activity, stopped.trigger_id, stopped.direction)
        if opened is None or opened.level is None or not opened.deal_id:
            return False

        survivor_sl = find_close_event(activity, survivor.deal_id, "SL")
        survivor_tp = find_close_event(activity, survivor.deal_id, "TP")
        reopened_sl = find_close_event(activity, opened.deal_id, "SL")
        reopened_tp = find_close_event(activity, opened.deal_id, "TP")
        if survivor_tp is not None:
            self._manual(
                "TP surviving-позиции и исполнение trigger произошли между опросами; "
                "требуется проверка неожиданно открытой стороны"
            )
            return True
        if survivor_sl is None or survivor_sl.level is None:
            return False

        reopen_key = f"reopen:{opened.deal_id}"
        if reopen_key not in self.state.processed_events:
            self.strategy.reopened(stopped.direction, opened.level, opened.deal_id, reopen_key)
            stopped.deal_reference = opened.deal_reference or stopped.deal_reference
        survivor_stop_key = f"stop:{survivor.deal_id}:{survivor_sl.level}"
        if survivor_stop_key not in self.state.processed_events:
            self.strategy.stopped(survivor.direction, survivor_sl.level, survivor_stop_key)

        if reopened_tp is not None and reopened_tp.level is not None:
            self._complete_cycle(stopped.direction, reopened_tp.level)
            self.state.armed = not self.state.paused
            self.state.phase = "FILTER" if self.state.armed else "PAUSED"
            self.state.save(self.cfg.state_file)
            self.telegram.send(
                "✅ Полный жизненный цикл trigger восстановлен из Capital.com activity\n"
                f"Trigger {stopped.direction}: {stopped.original_trigger_level}\n"
                f"Фактический вход: {opened.level}\n"
                f"SL {survivor.direction}: {survivor_sl.level}\n"
                f"TP {stopped.direction}: {reopened_tp.level}\n"
                + cycle_result_text(
                    self.state, stopped.direction, reopened_tp.level, self.cfg.size
                )
            )
            return True

        if reopened_sl is not None and reopened_sl.level is not None:
            # Event order is: the saved trigger reopened ``stopped``; the old ``survivor`` then
            # hit SL; its trigger must therefore be created *before* applying the later SL of the
            # reopened leg. This preserves the real strategy order. If Capital immediately uses
            # MARKET fallback, ``survivor.open`` becomes true and the later SL can be applied now.
            # If the working order is still pending, leave the later SL for the next tick, where
            # the normal trigger-fill+stop reconciliation will apply both without inventing a
            # non-existent surviving position.
            self._create_trigger(survivor)
            if survivor.open:
                reopened_stop_key = f"stop:{stopped.deal_id}:{reopened_sl.level}"
                if reopened_stop_key not in self.state.processed_events:
                    self.strategy.stopped(stopped.direction, reopened_sl.level, reopened_stop_key)
                if not self._apply_protection(survivor):
                    return True
            self.state.save(self.cfg.state_file)
            self.telegram.send(
                "🔁 Trigger-позиция открылась и закрылась по SL между опросами\n"
                f"Вход: {opened.level}\nSL: {reopened_sl.level}\n"
                f"Сценарий: {self.state.scenario}\nRecovery: {self.state.recovery}\n"
                f"Следующий trigger: {survivor.direction} "
                f"на {survivor.original_trigger_level}\n"
                "Поздний SL будет применён после подтверждения следующего trigger-входа."
            )
            return True

        LOG.info(
            "Trigger fill %s recovered from activity; close event is still synchronizing",
            opened.deal_id,
        )
        self.state.save(self.cfg.state_file)
        return True

    def _ensure_expected_trigger(self) -> None:
        # Scenario 9 can finish inside _detect_trigger_fill().  The caller then continues in the
        # same Python frame with stale local Leg references.  Never create another trigger after
        # the cycle has become inactive or left a one-sided phase.
        if not self.state.active or self.state.phase not in {"LONG_ONLY", "SHORT_ONLY"}:
            return
        for leg in (self.state.long, self.state.short):
            if leg and not leg.open and not leg.trigger_id:
                self._create_trigger(leg)

    def _create_trigger(self, leg: Leg) -> None:
        last_error = "trigger отклонён"
        projected_stop = stop_for(leg.direction, leg.original_trigger_level, self.cfg.stop_distance)
        projected_recovery = self.state.recovery + self.cfg.stop_distance
        opposite = self.state.short if leg.direction == "BUY" else self.state.long
        if not opposite or opposite.stop is None:
            raise RuntimeError("Нельзя рассчитать защиту trigger без противоположного SL")
        projected_target = (
            opposite.stop + projected_recovery
            if leg.direction == "BUY"
            else opposite.stop - projected_recovery
        )
        for _ in range(self.execution_policy.attempts):
            existing = self._find_order(leg)
            if existing:
                leg.trigger_id = str(existing["dealId"])
                if leg.trigger_id not in self.state.cycle_trigger_ids:
                    self.state.cycle_trigger_ids.append(leg.trigger_id)
                return
            try:
                reference = self.capital.working_stop(
                    self.cfg.epic, leg.direction, self.cfg.size, leg.original_trigger_level,
                    projected_stop, projected_target
                )
                result = self.capital.wait_confirmation(reference)
                if result.get("dealStatus") == "ACCEPTED" and result.get("dealId"):
                    leg.trigger_reference = reference
                    leg.trigger_id = str(result["dealId"])
                    if leg.trigger_id not in self.state.cycle_trigger_ids:
                        self.state.cycle_trigger_ids.append(leg.trigger_id)
                    self.telegram.send(
                        f"📌 Trigger установлен\nСторона: {leg.direction}\n"
                        f"Уровень: {leg.original_trigger_level}\nOrder ID: {leg.trigger_id}\n"
                        f"Projected SL: {projected_stop}\nProjected TP: {projected_target}\n"
                        f"Recovery после исполнения без slippage: {projected_recovery}"
                    )
                    return
                last_error = result.get("reason") or last_error
                if self._trigger_level_passed(leg) and self._is_crossed_level_rejection(last_error):
                    self._open_passed_trigger_at_market(leg, projected_target)
                    return
            except Exception as exc:
                last_error = str(exc)
                if self._trigger_level_passed(leg) and self._is_crossed_level_rejection(last_error):
                    self._open_passed_trigger_at_market(leg, projected_target)
                    return
        self._manual(f"Trigger {leg.direction} не создан после 4 попыток: {last_error}")

    def _trigger_level_passed(self, leg: Leg) -> bool:
        bid, ask = self.capital.quote(self.cfg.epic)
        return trigger_level_passed(leg.direction, leg.original_trigger_level, bid, ask)

    @staticmethod
    def _is_crossed_level_rejection(reason: str) -> bool:
        return is_crossed_level_rejection(reason)

    def _open_passed_trigger_at_market(self, leg: Leg, projected_target: D) -> None:
        """Reopen immediately when Capital rejects a STOP whose level is already crossed."""
        last_error = "MARKET fallback отклонён"
        accepted = None
        reference = ""
        for _ in range(self.execution_policy.attempts):
            try:
                projected_stop = stop_for(
                    leg.direction, leg.original_trigger_level, self.cfg.stop_distance
                )
                reference = self.capital.open_position(
                    self.cfg.epic, leg.direction, self.cfg.size, projected_stop, projected_target
                )
                result = self.capital.wait_confirmation(reference)
                if result.get("dealStatus") != "ACCEPTED" or not result.get("dealId"):
                    last_error = result.get("reason") or last_error
                    continue
                if result.get("level") is None:
                    raise CapitalError("MARKET fallback принят без фактической цены")
                accepted = result
                break
            except Exception as exc:
                last_error = str(exc)
        if accepted is None:
            raise CapitalError(last_error)
        fill = D(str(accepted["level"]))
        self.strategy.reopened(
            leg.direction, fill, str(accepted["dealId"]), f"reopen:{accepted['dealId']}"
        )
        leg.deal_reference = reference
        try:
            if self.state.scenario == self.cfg.max_scenarios:
                self._enter_manual_nine()
            else:
                self._apply_protection(self.state.long)
                self._apply_protection(self.state.short)
        except Exception as exc:
            self._manual(f"MARKET trigger исполнен, но защита не подтверждена: {exc}")
            return
        self.state.save(self.cfg.state_file)
        self.telegram.send(
            f"Сценарий {self.state.scenario}: пройденный trigger {leg.direction} "
            f"переоткрыт MARKET по {fill}"
        )

    def reconcile_startup(self) -> None:
        positions = self._cycle_positions()
        if self.state.active and not positions:
            # Immediately after creates/updates Capital.com can briefly return an empty list.
            # Never discard or manualize an active local cycle from a single such snapshot.
            positions = self._retry_missing_positions()
        orders = self.capital.working_orders()
        if not self.state.active:
            unknown = list(positions.values()) or [self._order_data(item) for item in orders if self._order_epic(item) == self.cfg.epic]
            if unknown:
                self._manual("На Capital.com есть Gold позиции/ордера, но локально активного цикла нет")
            self.reconciled = True
            return
        gold_orders = [item for item in orders if self._order_epic(item) == self.cfg.epic]
        if self.state.manual and not positions and not gold_orders:
            self._clear_stale_cycle(
                "На Capital.com нет открытых Gold-позиций и trigger-ордеров"
            )
            self.reconciled = True
            return
        try:
            self._recover_active_cycle(positions, orders)
            self.reconciled = True
            self.state.save(self.cfg.state_file)
            self.telegram.send(f"✅ Состояние автоматически восстановлено. {self.status()}")
        except Exception as exc:
            self._manual(f"Невозможно однозначно восстановить цикл: {exc}")
            self.reconciled = True

    def _recover_active_cycle(self, positions: dict[str, dict], orders: list[dict]) -> None:
        """Replay unambiguous stop/TP/trigger events that happened while the bot was offline."""
        gold_orders = {
            str(data.get("dealId")): data
            for item in orders
            if self._order_epic(item) == self.cfg.epic
            for data in [self._order_data(item)]
            if data.get("dealId")
        }
        known_ids = {leg.deal_id for leg in (self.state.long, self.state.short) if leg and leg.deal_id}
        snapshot = RemoteSnapshot(positions, gold_orders)
        unknown = snapshot.unknown_position_ids(known_ids, {
            leg.trigger_id for leg in (self.state.long, self.state.short) if leg and leg.trigger_id
        })
        if unknown:
            raise RuntimeError(f"неизвестные Gold позиции: {sorted(unknown)}")

        # Re-run the same deterministic transition logic used during normal polling. Repeating
        # permits recovery of trigger-fill followed by the next stop while the phone was offline.
        for _ in range(6):
            before = (self.state.scenario, self.state.phase,
                      tuple((leg.open, leg.deal_id, leg.trigger_id) for leg in (self.state.long, self.state.short) if leg))
            if self.state.phase in {"LONG_ONLY", "SHORT_ONLY"}:
                survivor = self.state.long if self.state.phase == "LONG_ONLY" else self.state.short
                stopped = self.state.short if self.state.phase == "LONG_ONLY" else self.state.long
                assert survivor and stopped
                self._detect_trigger_fill(positions)
                if self.state.phase in {"LONG_ONLY", "SHORT_ONLY"}:
                    if self._recover_trigger_round_trip_from_activity(survivor, stopped):
                        positions = self._cycle_positions()
                        continue
                    if survivor.deal_id not in positions:
                        fill = self._closing_fill(survivor, "TP")
                        if fill is None:
                            raise RuntimeError(
                                f"закрытие {survivor.deal_id} по TP не подтверждено"
                            )
                        if stopped.trigger_id and stopped.trigger_id in gold_orders:
                            self.capital.delete_working_order(stopped.trigger_id)
                        self._complete_cycle(survivor.direction, fill)
                        self.state.armed = not self.state.paused
                        self.state.phase = "FILTER" if self.state.armed else "PAUSED"
                    elif not stopped.trigger_id or stopped.trigger_id not in gold_orders:
                        stopped.trigger_id = ""
                        self._create_trigger(stopped)
                        if stopped.trigger_id:
                            gold_orders[stopped.trigger_id] = {"dealId": stopped.trigger_id}
            elif self.state.phase == "BOTH_OPEN":
                missing = [leg for leg in (self.state.long, self.state.short)
                           if leg and leg.open and leg.deal_id not in positions]
                if len(missing) == 1:
                    lost = missing[0]
                    survivor = self.state.short if lost.direction == "BUY" else self.state.long
                    assert survivor
                    if survivor.deal_id not in positions:
                        raise RuntimeError("обе ожидаемые позиции отсутствуют")
                    fill = self._closing_fill(lost)
                    if fill is None:
                        raise RuntimeError(f"не найдена цена закрытия {lost.deal_id}")
                    self.strategy.stopped(lost.direction, fill, f"stop:{lost.deal_id}:{fill}")
                    if not self._apply_protection(survivor):
                        break
                    self._create_trigger(lost)
                    if lost.trigger_id:
                        gold_orders[lost.trigger_id] = {"dealId": lost.trigger_id}
                elif len(missing) > 1:
                    raise RuntimeError("одновременно отсутствуют обе ожидаемые позиции")
            after = (self.state.scenario, self.state.phase,
                     tuple((leg.open, leg.deal_id, leg.trigger_id) for leg in (self.state.long, self.state.short) if leg))
            if after == before:
                break

    def _enter_manual_nine(self) -> None:
        """Automatically flatten scenario 9 using actual fills from both sides.

        The historical method name is retained to keep all transition call sites small. Scenario
        9 is no longer manual: protection is removed and both legs are closed as concurrently as
        the REST API permits.
        """
        self.state.phase = "SCENARIO_9_CLOSING"
        self.state.manual = False
        prior_losses = self.state.realized_losses
        self.state.scenario_nine_prior_losses = prior_losses
        self.state.save(self.cfg.state_file)

        trigger_ids = self._cancel_and_verify_scenario_nine_triggers()

        # Remove both sets of protection first. A leg may execute its old SL during this narrow
        # window; that is a normal scenario-9 close and its authoritative activity fill is used.
        for leg in (self.state.long, self.state.short):
            if leg and leg.open:
                try:
                    self._confirm_update(self.capital.update_position(leg.deal_id, None, None))
                    leg.stop = leg.take_profit = None
                except CapitalError as exc:
                    if "error.not-found.dealId" not in str(exc):
                        raise
                leg.trigger_id = leg.trigger_reference = ""

        positions = self._retry_missing_positions(attempts=4, delay=0.1)
        legs = [leg for leg in (self.state.long, self.state.short) if leg]
        # Persist each confirmed fill independently. If Android kills Pydroid after one side has
        # closed, the next launch can resume scenario 9 without closing either side twice.
        fills: dict[str, Decimal] = {}
        if self.state.scenario_nine_long_fill is not None:
            fills["BUY"] = self.state.scenario_nine_long_fill
        if self.state.scenario_nine_short_fill is not None:
            fills["SELL"] = self.state.scenario_nine_short_fill
        open_legs = [leg for leg in legs if leg.deal_id in positions]
        open_legs = [leg for leg in open_legs if leg.direction not in fills]

        # DELETE requests are issued from two workers so neither side intentionally waits for the
        # other's HTTP round trip. Confirmations provide the actual execution prices.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self.capital.close_position, leg.deal_id): leg for leg in open_legs}
            references: dict[str, str] = {}
            for future in as_completed(futures):
                leg = futures[future]
                try:
                    references[leg.direction] = future.result()
                except CapitalError as exc:
                    if "error.not-found.dealId" not in str(exc):
                        raise
            confirmation_futures = {
                pool.submit(self.capital.wait_confirmation, reference): direction
                for direction, reference in references.items()
            }
            for future in as_completed(confirmation_futures):
                direction = confirmation_futures[future]
                result = future.result()
                if result.get("dealStatus") != "ACCEPTED" or result.get("level") is None:
                    raise CapitalError(result.get("reason") or f"Закрытие {direction} не подтверждено")
                fills[direction] = D(str(result["level"]))
                if direction == "BUY":
                    self.state.scenario_nine_long_fill = fills[direction]
                else:
                    self.state.scenario_nine_short_fill = fills[direction]
                self.state.save(self.cfg.state_file)

        # A side that vanished while protections were being removed is resolved from durable
        # activity. It may have closed by SL or TP; either actual fill participates in the gap.
        for leg in legs:
            if leg.direction not in fills:
                fill = self._wait_any_closing_fill(leg, attempts=120, delay=0.5)
                if fill is None:
                    raise CapitalError(
                        f"Сценарий 9: не найдена фактическая цена закрытия {leg.direction}"
                    )
                fills[leg.direction] = fill
                if leg.direction == "BUY":
                    self.state.scenario_nine_long_fill = fill
                else:
                    self.state.scenario_nine_short_fill = fill
                self.state.save(self.cfg.state_file)

        # A trigger can execute in the narrow interval between the cancellation snapshot and the
        # broker processing DELETE.  Discover any resulting position by workingOrderId, close it
        # immediately, and add only its actual loss to the scenario-9 total.
        base_ids = {leg.deal_id for leg in legs if leg.deal_id}
        extra_loss = self._close_scenario_nine_trigger_races(trigger_ids, base_ids)
        long_fill, short_fill = fills["BUY"], fills["SELL"]
        # Keep the pre-close loss snapshot even if another helper touched state while resolving
        # broker history; scenario 9 adds only the absolute gap between its two actual fills.
        self.state.realized_losses = prior_losses
        self.strategy.complete_scenario_nine(long_fill, short_fill, extra_loss)
        for leg in legs:
            leg.open = False
            leg.stop = leg.take_profit = None
            leg.trigger_id = leg.trigger_reference = ""
        self._clear_diagnostics_if_due()
        self.state.armed = not self.state.paused
        self.state.phase = "FILTER" if self.state.armed else "PAUSED"
        self.state.save(self.cfg.state_file)
        self.telegram.send(scenario_nine_result_text(self.state, long_fill, short_fill))

    def _cancel_and_verify_scenario_nine_triggers(self) -> set[str]:
        """Cancel all Gold working orders and verify broker-side absence before flattening."""
        current_ids: set[str] = {
            leg.trigger_id for leg in (self.state.long, self.state.short)
            if leg and leg.trigger_id
        }
        owned_ids = set(self.state.cycle_trigger_ids) | current_ids
        uncertain_ids = set(current_ids)
        consecutive_empty = 0
        for attempt in range(8):
            orders = [
                self._order_data(item) for item in self.capital.working_orders()
                if self._order_epic(item) == self.cfg.epic
                and str(self._order_data(item).get("dealId", "")) in owned_ids
            ]
            if not orders:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    self.state.scenario_nine_triggers_verified = True
                    self.state.save(self.cfg.state_file)
                    self.telegram.send(
                        "✅ Сценарий 9: trigger-ордера отменены\n"
                        "Capital.com три последовательных раза подтвердил: "
                        "working orders текущего цикла = 0."
                    )
                    return uncertain_ids
            else:
                consecutive_empty = 0
                for order in orders:
                    order_id = str(order.get("dealId", ""))
                    if not order_id:
                        continue
                    owned_ids.add(order_id)
                    if self.capital.delete_working_order(order_id):
                        uncertain_ids.discard(order_id)
                    else:
                        uncertain_ids.add(order_id)
            if attempt + 1 < 8:
                time.sleep(0.2)
        remaining = [
            self._order_data(item) for item in self.capital.working_orders()
            if self._order_epic(item) == self.cfg.epic
            and str(self._order_data(item).get("dealId", "")) in owned_ids
        ]
        if remaining:
            raise CapitalError(
                "Сценарий 9: Capital.com не подтвердил отмену trigger: "
                + ", ".join(str(item.get("dealId", "")) for item in remaining)
            )
        self.state.scenario_nine_triggers_verified = True
        return uncertain_ids

    def _close_scenario_nine_trigger_races(
        self, trigger_ids: set[str], base_deal_ids: set[str]
    ) -> Decimal:
        """Close positions created by scenario-9 triggers that raced with cancellation."""
        if not trigger_ids:
            return D("0")
        total_loss = self.state.scenario_nine_extra_loss
        closed_ids: set[str] = {
            str(item.get("deal_id", "")) for item in self.state.deal_history
            if item.get("close_source") == "SCENARIO_9_TRIGGER_RACE"
        }
        resolved_trigger_ids: set[str] = set()
        for attempt in range(120):
            activity = self.capital.activity()
            executed_ids = {
                trigger_id for trigger_id in trigger_ids
                if find_working_order_execution(activity, trigger_id) is not None
            }
            positions = [
                self._position_data(item) for item in self.capital.positions()
                if self._position_epic(item) == self.cfg.epic
            ]
            raced = [
                position for position in positions
                if str(position.get("workingOrderId", "")) in trigger_ids
                and str(position.get("dealId", "")) not in base_deal_ids
                and str(position.get("dealId", "")) not in closed_ids
            ]
            # The trigger-created position may open and close entirely between snapshots.
            # Reconstruct that round trip from durable activity instead of waiting forever.
            for trigger_id in executed_ids:
                opened = find_trigger_open_event(activity, trigger_id)
                if (
                    opened is None or not opened.deal_id or opened.level is None
                    or opened.deal_id in base_deal_ids or opened.deal_id in closed_ids
                ):
                    continue
                if any(str(item.get("dealId", "")) == opened.deal_id for item in raced):
                    continue
                close_event = (
                    find_close_event(activity, opened.deal_id, "SL")
                    or find_close_event(activity, opened.deal_id, "TP")
                )
                if close_event is None or close_event.level is None:
                    continue
                direction = opened.direction
                loss = (
                    max(D("0"), opened.level - close_event.level)
                    if direction == "BUY"
                    else max(D("0"), close_event.level - opened.level)
                )
                total_loss += loss
                self.state.scenario_nine_extra_loss = total_loss
                closed_ids.add(opened.deal_id)
                resolved_trigger_ids.add(trigger_id)
                self.state.remember_close(
                    opened.deal_id, "SCENARIO_9_TRIGGER_RACE", close_event.level
                )
                self.telegram.send(
                    "⚡ Сценарий 9: полный trigger round-trip восстановлен из history\n"
                    f"workingOrderId: {trigger_id}\nDeal ID: {opened.deal_id}\n"
                    f"Сторона: {direction}\nВход: {opened.level}\n"
                    f"Закрытие: {close_event.level}\nДополнительный убыток: {loss}"
                )
                self.state.save(self.cfg.state_file)
            for position in raced:
                deal_id = str(position["dealId"])
                direction = str(position.get("direction", ""))
                entry = D(str(position.get("level", "0")))
                reference = self.capital.close_position(deal_id)
                result = self.capital.wait_confirmation(reference)
                if result.get("dealStatus") != "ACCEPTED" or result.get("level") is None:
                    raise CapitalError(
                        result.get("reason") or f"Сценарий 9: trigger-позиция {deal_id} не закрыта"
                    )
                close = D(str(result["level"]))
                loss = max(D("0"), entry - close) if direction == "BUY" else max(
                    D("0"), close - entry
                )
                total_loss += loss
                self.state.scenario_nine_extra_loss = total_loss
                closed_ids.add(deal_id)
                resolved_trigger_ids.add(str(position.get("workingOrderId", "")))
                self.state.remember_close(deal_id, "SCENARIO_9_TRIGGER_RACE", close)
                self.telegram.send(
                    "⚡ Сценарий 9: trigger исполнился во время отмены и закрыт MARKET\n"
                    f"workingOrderId: {position.get('workingOrderId')}\nDeal ID: {deal_id}\n"
                    f"Сторона: {direction}\nВход: {entry}\nЗакрытие: {close}\n"
                    f"Дополнительный убыток: {loss}"
                )
            if raced:
                self.state.save(self.cfg.state_file)
            # Three quiet reads cover ordinary eventual consistency without delaying every
            # scenario 9 for a full minute.
            if not raced and not (executed_ids - resolved_trigger_ids) and attempt >= 2:
                break
            time.sleep(0.2)

        remaining_orders = [
            item for item in self.capital.working_orders()
            if self._order_epic(item) == self.cfg.epic
            and str(self._order_data(item).get("dealId", "")) in trigger_ids
        ]
        if remaining_orders:
            raise CapitalError("Сценарий 9: после закрытия остались working orders Gold")
        return total_loss

    def _wait_any_closing_fill(
        self, leg: Leg, attempts: int = 20, delay: float = 0.5
    ) -> Decimal | None:
        for attempt in range(attempts):
            for source in ("SL", "TP"):
                fill = self._closing_fill(leg, source)
                if fill is not None:
                    return fill
            if attempt + 1 < attempts:
                time.sleep(delay)
        return None

    def _manual_command(self, command: str, args: list[str]) -> None:
        if not args or args[0] not in {"long", "short"}:
            raise RuntimeError("Укажите long или short")
        leg = self.state.long if args[0] == "long" else self.state.short
        if not leg:
            raise RuntimeError("Указанная сторона отсутствует в состоянии цикла")
        if command in {"/canceltrigger", "/settrigger"}:
            if leg.open:
                raise RuntimeError("Trigger можно изменить только для закрытой стороны")
        elif not leg.open:
            raise RuntimeError("Указанная позиция не открыта")
        if command == "/canceltrigger":
            if leg.trigger_id:
                self.capital.delete_working_order(leg.trigger_id)
                leg.trigger_id = leg.trigger_reference = ""
        elif command == "/settrigger":
            if len(args) != 2:
                raise RuntimeError("Укажите цену trigger")
            level = D(args[1])
            if leg.trigger_id:
                self.capital.delete_working_order(leg.trigger_id)
            reference = self.capital.working_stop(
                self.cfg.epic, leg.direction, self.cfg.size, level
            )
            result = self.capital.wait_confirmation(reference)
            if result.get("dealStatus") != "ACCEPTED" or not result.get("dealId"):
                raise CapitalError(result.get("reason") or "Ручной trigger отклонён")
            leg.trigger_reference = reference
            leg.trigger_id = str(result["dealId"])
            if leg.trigger_id not in self.state.cycle_trigger_ids:
                self.state.cycle_trigger_ids.append(leg.trigger_id)
        else:
            if command not in {"/removesl", "/removetp"} and len(args) != 2:
                raise RuntimeError("Укажите цену")
            value = None if command in {"/removesl", "/removetp"} else D(args[1])
            stop = value if command == "/setsl" else leg.stop
            target = value if command == "/settp" else leg.take_profit
            if command == "/removesl":
                stop = None
            if command == "/removetp":
                target = None
            self._confirm_update(self.capital.update_position(leg.deal_id, stop, target))
            leg.stop, leg.take_profit = stop, target
        self.state.save(self.cfg.state_file)
        self.telegram.send("Команда выполнена и подтверждена Capital.com")

    def _recover_manual_positions(self) -> None:
        if not self.state.active or not self.state.long or not self.state.short:
            raise RuntimeError("Нет сохранённого активного цикла для восстановления")
        positions = list(self._cycle_positions().values())
        buys = [position for position in positions if position.get("direction") == "BUY"]
        sells = [position for position in positions if position.get("direction") == "SELL"]
        if len(buys) != 1 or len(sells) != 1:
            raise RuntimeError(
                f"Ожидалась одна BUY и одна SELL позиция; найдено BUY={len(buys)}, SELL={len(sells)}"
            )
        for leg, position in ((self.state.long, buys[0]), (self.state.short, sells[0])):
            leg.deal_id = str(position["dealId"])
            if position.get("dealReference"):
                leg.deal_reference = str(position["dealReference"])
            if position.get("level") is not None:
                leg.current_entry = D(str(position["level"]))
            leg.open = True
        if self.state.scenario == 1:
            self.strategy.confirm_initial_fills(
                self.state.long.current_entry, self.state.short.current_entry
            )
        self._apply_protection(self.state.long)
        self._apply_protection(self.state.short)
        self.state.manual = False
        self.state.paused = True
        self.state.phase = "BOTH_OPEN"
        self.state.save(self.cfg.state_file)
        self.telegram.send(
            f"✅ Цикл восстановлен\nСценарий: {self.state.scenario}\n"
            f"BUY dealId: {self.state.long.deal_id}\nSELL dealId: {self.state.short.deal_id}\n"
            f"Recovery: {self.state.recovery}\nТекущий цикл продолжает контролироваться. "
            f"Следующий цикл на паузе до /start."
        )

    def _exit_manual_mode(self) -> None:
        if not self.state.manual:
            self.telegram.send("ℹ️ Автоматика уже не находится в ручном режиме.")
            return
        positions = self._cycle_positions()
        orders = [item for item in self.capital.working_orders()
                  if self._order_epic(item) == self.cfg.epic]
        if positions or orders:
            raise RuntimeError(
                f"Нельзя выйти из ручного режима: позиции={len(positions)}, ордера={len(orders)}. "
                "Используйте /recover или сначала разберите их вручную."
            )
        self._clear_stale_cycle("Пользователь подтвердил выход командой /automode")

    def _clear_stale_cycle(self, reason: str) -> None:
        previous_scenario = self.state.scenario
        self.state.events.append(f"stale cycle cleared: {reason}")
        self.state.reset()
        self.state.paused = True
        self.state.phase = "PAUSED"
        self.state.save(self.cfg.state_file)
        self.telegram.send(
            f"✅ Ручной режим сброшен\nПричина: {reason}\n"
            f"Предыдущий сценарий: {previous_scenario}\n"
            "Открытых позиций и ордеров нет. Новый цикл ожидает /start."
        )

    def _send_diagnostic_log(self) -> None:
        for handler in logging.getLogger().handlers:
            handler.flush()
        path = Path(self.cfg.diagnostic_log_file)
        if not path.exists():
            raise RuntimeError(f"Диагностический файл ещё не создан: {path}")
        if self.telegram.send_document(str(path)):
            self.telegram.send(
                f"✅ Диагностический файл отправлен: {path.name}, размер {path.stat().st_size} байт"
            )
        else:
            LOG.warning("Diagnostic file was not delivered to Telegram: %s", path)

    def _capture_failure_context(self, reason: str, error: Exception) -> None:
        LOG.exception("FAILURE CONTEXT reason=%s state=%s error=%s", reason, self.status(), error)
        for label, getter in (
            ("positions", self.capital.positions),
            ("working_orders", self.capital.working_orders),
            ("activity", self.capital.activity),
            ("transactions", self.capital.transactions),
        ):
            try:
                LOG.error("FAILURE SNAPSHOT %s=%s", label, getter())
            except Exception:
                LOG.exception("FAILURE SNAPSHOT %s unavailable", label)

    def _apply_protection(self, leg: Leg | None) -> bool:
        if leg and leg.open and leg.deal_id:
            if self._take_profit_already_reached(leg):
                self._close_reached_take_profit(leg)
                return False
            try:
                reference = self.capital.update_position(
                    leg.deal_id, leg.stop, leg.take_profit
                )
            except CapitalError as exc:
                # The quote can cross the target between the pre-check and PUT. Capital then
                # rejects the now-stale absolute TP with minvalue/maxvalue. Treat that as the
                # strategy target having been reached and close the surviving leg at market.
                text = str(exc).lower()
                if "error.not-found.dealid" in text:
                    LOG.info(
                        "Protection skipped because deal closed before PUT: direction=%s "
                        "dealId=%s expectedSL=%s expectedTP=%s",
                        leg.direction, leg.deal_id, leg.stop, leg.take_profit,
                    )
                    self.state.save(self.cfg.state_file)
                    return False
                if "capital transport error put" in text:
                    # PUT may have reached Capital before the connection timed out. Never resend
                    # blindly. A fresh idempotent snapshot determines whether it was accepted,
                    # rejected, or the position closed during the uncertain request.
                    positions = self._cycle_positions()
                    remote = positions.get(leg.deal_id)
                    if remote is None:
                        LOG.info(
                            "Protection PUT outcome resolved as closed position: dealId=%s",
                            leg.deal_id,
                        )
                        return False
                    if self._protection_matches(remote, leg):
                        LOG.info(
                            "Protection PUT accepted despite transport error: dealId=%s",
                            leg.deal_id,
                        )
                        self.telegram.send(
                            f"🛡 Защита подтверждена повторным чтением\n"
                            f"Сторона: {leg.direction}\nDeal ID: {leg.deal_id}\n"
                            f"SL: {leg.stop}\nTP: {leg.take_profit}"
                        )
                        return True
                if "error.invalid.takeprofit." not in text or not self._take_profit_already_reached(leg):
                    raise
                self._close_reached_take_profit(leg)
                return False
            self._confirm_update(reference)
            self.telegram.send(
                f"🛡 Защита подтверждена\nСторона: {leg.direction}\nDeal ID: {leg.deal_id}\n"
                f"Entry: {leg.current_entry}\nSL: {leg.stop}\nTP: {leg.take_profit}\n"
                f"Сценарий: {self.state.scenario}\nRecovery: {self.state.recovery}"
            )
        return True

    def _take_profit_already_reached(self, leg: Leg) -> bool:
        """Check the executable quote, not the midpoint, against an unapplied TP."""
        # Automatic market realization is only valid after the opposite leg has a confirmed SL
        # and this is the sole surviving position. With both legs open, event replay must first
        # establish which broker-side SL actually executed.
        if leg.take_profit is None or self.state.phase not in {"LONG_ONLY", "SHORT_ONLY"}:
            return False
        quote = self.capital.quote(self.cfg.epic)
        # Test doubles and partially initialized clients may not expose a usable quote. In that
        # case let Capital validate the requested level; the API-error branch below remains the
        # authoritative race detector.
        if not isinstance(quote, tuple) or len(quote) != 2:
            return False
        bid, offer = quote
        return bid >= leg.take_profit if leg.direction == "BUY" else offer <= leg.take_profit

    def _close_reached_take_profit(self, leg: Leg) -> None:
        """Realize a target crossed before Capital accepted the absolute TP level."""
        reference = self.capital.close_position(leg.deal_id)
        result = self.capital.wait_confirmation(reference)
        if result.get("dealStatus") != "ACCEPTED":
            raise CapitalError(result.get("reason") or "Закрытие достигнутого TP отклонено")
        if result.get("level") is None:
            raise CapitalError("Закрытие достигнутого TP принято без фактической цены")
        fill = D(str(result["level"]))
        self._complete_cycle(leg.direction, fill)
        self.state.armed = not self.state.paused
        self.state.phase = "FILTER" if self.state.armed else "PAUSED"
        self.state.save(self.cfg.state_file)
        suffix = (
            "Перехожу к фильтру следующего цикла."
            if self.state.armed else "Следующий цикл ожидает /start."
        )
        self.telegram.send(
            "✅ Целевая цена достигнута до установки TP\n"
            f"Сторона: {leg.direction}\nРасчётный TP: {leg.take_profit}\n"
            f"Фактическое MARKET-закрытие: {fill}\n{suffix}\n"
            f"{cycle_result_text(self.state, leg.direction, fill, self.cfg.size)}"
        )

    def _apply_stop_only(self, leg: Leg | None) -> None:
        """Install an exact SL, verify its broker level, and deliberately leave TP unset."""
        if not leg or not leg.open or not leg.deal_id or leg.stop is None:
            return
        self._confirm_update(self.capital.update_position(leg.deal_id, leg.stop, None))
        actual_stop = self._wait_position_stop(leg.deal_id, leg.stop)
        if actual_stop != leg.stop:
            raise CapitalError(
                f"Capital.com не подтвердил точный SL {leg.stop} для {leg.deal_id}; "
                f"фактический stopLevel={actual_stop}"
            )
        self.telegram.send(
            f"🛡 Stop Loss подтверждён\nСторона: {leg.direction}\nDeal ID: {leg.deal_id}\n"
            f"Entry: {leg.current_entry}\nSL: {actual_stop}\nTP пока не установлен"
        )

    def _apply_take_profit_only(self, leg: Leg | None) -> None:
        """Add TP after the SL barrier and verify both broker-side protection levels."""
        if (
            not leg or not leg.open or not leg.deal_id
            or leg.stop is None or leg.take_profit is None
        ):
            return
        self._confirm_update(
            self.capital.update_position(leg.deal_id, leg.stop, leg.take_profit)
        )
        actual_stop, actual_tp = self._wait_position_protection(
            leg.deal_id, leg.stop, leg.take_profit
        )
        if actual_stop != leg.stop or actual_tp != leg.take_profit:
            raise CapitalError(
                "Capital.com не подтвердил точную защиту "
                f"для {leg.deal_id}; ожидались SL={leg.stop}, TP={leg.take_profit}; "
                f"фактически SL={actual_stop}, TP={actual_tp}"
            )
        self.telegram.send(
            f"🎯 Take Profit подтверждён\nСторона: {leg.direction}\n"
            f"Deal ID: {leg.deal_id}\nEntry: {leg.current_entry}\n"
            f"SL: {actual_stop}\nTP: {actual_tp}\n"
            f"Сценарий: {self.state.scenario}\nRecovery: {self.state.recovery}"
        )

    def _wait_position_stop(
        self, deal_id: str, expected: Decimal, attempts: int = 5, delay: float = 0.25
    ) -> Decimal | None:
        """Read the position back until Capital.com exposes the requested exact stop level."""
        actual = None
        for attempt in range(attempts):
            try:
                payload = self.capital.position(deal_id)
            except CapitalError as exc:
                # A protected position can close between the successful PUT/confirmation and
                # this read-back.  A 404 is therefore an exit signal to be reconciled from
                # activity, not a loop error.
                if "error.not-found.dealid" in str(exc).lower():
                    LOG.info("Position %s closed before SL read-back", deal_id)
                    return None
                raise
            position = payload.get("position", payload)
            value = position.get("stopLevel")
            actual = D(str(value)) if value is not None else None
            if actual == expected:
                return actual
            if attempt + 1 < attempts:
                time.sleep(delay)
        return actual

    def _wait_position_protection(
        self,
        deal_id: str,
        expected_stop: Decimal,
        expected_tp: Decimal,
        attempts: int = 5,
        delay: float = 0.25,
    ) -> tuple[Decimal | None, Decimal | None]:
        """Read a position until the exact SL and TP are visible at the broker."""
        actual_stop = actual_tp = None
        for attempt in range(attempts):
            try:
                payload = self.capital.position(deal_id)
            except CapitalError as exc:
                if "error.not-found.dealid" in str(exc).lower():
                    LOG.info("Position %s closed before protection read-back", deal_id)
                    return None, None
                raise
            position = payload.get("position", payload)
            stop_value = position.get("stopLevel")
            tp_value = position.get("profitLevel")
            actual_stop = D(str(stop_value)) if stop_value is not None else None
            actual_tp = D(str(tp_value)) if tp_value is not None else None
            if actual_stop == expected_stop and actual_tp == expected_tp:
                return actual_stop, actual_tp
            if attempt + 1 < attempts:
                time.sleep(delay)
        return actual_stop, actual_tp

    def _confirm_update(self, reference: str) -> None:
        result = self.capital.wait_confirmation(reference)
        if result.get("dealStatus") != "ACCEPTED":
            raise CapitalError(result.get("reason") or "Изменение позиции отклонено")

    def _cycle_positions(self) -> dict[str, dict]:
        result = {}
        for item in self.capital.positions():
            data = self._position_data(item)
            if self._position_epic(item) == self.cfg.epic and data.get("dealId"):
                result[str(data["dealId"])] = data
        return result

    def _find_order(self, leg: Leg) -> dict | None:
        matches = []
        for item in self.capital.working_orders():
            data = self._order_data(item)
            level = data.get("orderLevel", data.get("level"))
            size = data.get("orderSize", data.get("size"))
            if level is None or size is None:
                continue
            if (self._order_epic(item) == self.cfg.epic
                    and data.get("direction") == leg.direction
                    and D(str(level)) == leg.original_trigger_level
                    and D(str(size)) == self.cfg.size):
                matches.append(data)
        if len(matches) > 1:
            raise CapitalError(
                f"Найдено несколько одинаковых trigger {leg.direction} на "
                f"{leg.original_trigger_level}; автоматическое связывание небезопасно"
            )
        return matches[0] if matches else None

    def _closing_fill(self, leg: Leg, expected_source: str = "SL") -> Decimal | None:
        event = find_close_event(self.capital.activity(leg.deal_id), leg.deal_id, expected_source)
        if event and event.level is not None:
            self.state.remember_deal(leg)
            self.state.remember_close(leg.deal_id, expected_source, event.level)
            return event.level
        return None

    def _closing_fill_any_index(
        self, leg: Leg, expected_source: str, global_activity: list[dict]
    ) -> Decimal | None:
        """Resolve a close from deal-specific history, then from the global activity index."""
        try:
            fill = self._closing_fill(leg, expected_source)
        except CapitalError:
            LOG.warning(
                "Deal activity unavailable; falling back to global activity: dealId=%s source=%s",
                leg.deal_id, expected_source, exc_info=True,
            )
            fill = None
        if fill is not None:
            return fill
        event = find_close_event(global_activity, leg.deal_id, expected_source)
        if event and event.level is not None:
            self.state.remember_deal(leg)
            self.state.remember_close(leg.deal_id, expected_source, event.level)
            LOG.info(
                "Close resolved from global activity: dealId=%s source=%s fill=%s",
                leg.deal_id, expected_source, event.level,
            )
            return event.level
        return None

    def _wait_closing_fill(
        self, leg: Leg, expected_source: str = "SL", attempts: int = 16, delay: float = 0.5
    ) -> Decimal | None:
        """Wait for an authoritative close event across both Capital activity views.

        Capital.com can remove a deal from ``/positions`` before its deal-filtered activity is
        indexed.  The unfiltered activity feed has also been observed to publish first.  Retry
        both sources and tolerate transient GET failures; never infer SL/TP from absence alone.
        """
        for attempt in range(attempts):
            fill = None
            try:
                fill = self._closing_fill(leg, expected_source)
            except CapitalError:
                LOG.warning(
                    "Deal activity unavailable while resolving close: dealId=%s source=%s "
                    "attempt=%s/%s",
                    leg.deal_id, expected_source, attempt + 1, attempts,
                    exc_info=True,
                )
            if fill is not None:
                return fill
            # Every fourth pass also consult the durable unfiltered feed. This catches the
            # broker's indexing race without doubling API traffic on every 0.5-second poll.
            if attempt % 4 == 3:
                try:
                    event = find_close_event(
                        self.capital.activity(), leg.deal_id, expected_source
                    )
                    if event and event.level is not None:
                        self.state.remember_deal(leg)
                        self.state.remember_close(
                            leg.deal_id, expected_source, event.level
                        )
                        LOG.info(
                            "Close resolved from global activity: dealId=%s source=%s "
                            "fill=%s attempt=%s/%s",
                            leg.deal_id, expected_source, event.level,
                            attempt + 1, attempts,
                        )
                        return event.level
                except CapitalError:
                    LOG.warning(
                        "Global activity unavailable while resolving close: dealId=%s "
                        "source=%s attempt=%s/%s",
                        leg.deal_id, expected_source, attempt + 1, attempts,
                        exc_info=True,
                    )
            if attempt + 1 < attempts:
                time.sleep(delay)
        return None

    @staticmethod
    def _position_data(item: dict) -> dict:
        return item.get("position", item)

    @staticmethod
    def _position_epic(item: dict) -> str:
        return str(item.get("market", {}).get("epic") or item.get("position", item).get("epic", ""))

    @staticmethod
    def _order_data(item: dict) -> dict:
        return item.get("workingOrderData", item.get("workingOrder", item))

    @classmethod
    def _order_epic(cls, item: dict) -> str:
        return str(item.get("marketData", {}).get("epic") or cls._order_data(item).get("epic", ""))

    def _manual(self, reason: str) -> None:
        self.state.manual = self.state.paused = True
        self.state.phase = "MANUAL"
        self.state.events.append(f"manual: {reason}")
        self.state.save(self.cfg.state_file)
        self.telegram.send(f"🚨 Автоматика остановлена: {reason}")


def main() -> None:
    settings = Settings.from_env()
    configure_diagnostics(settings.diagnostic_log_file)
    LOG.info("Bot process starting; demo=%s epic=%s state=%s", settings.demo,
             settings.epic, settings.state_file)
    Bot(settings).run()
