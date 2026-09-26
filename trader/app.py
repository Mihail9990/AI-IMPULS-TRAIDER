from __future__ import annotations

import logging
import time
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

from .capital import CapitalClient, CapitalError
from .config import Settings
from .cycle_continuation import CycleContinuation
from .diagnostics import (
    acknowledge_diagnostic_snapshot,
    begin_diagnostic_cycle,
    configure_diagnostics,
    end_diagnostic_cycle,
    pending_diagnostic_snapshots,
    snapshot_diagnostics,
)
from .engine import Strategy
from .events import (
    BrokerEvent, find_close_event,
    find_trigger_open_event,
    find_working_order_cancellation,
    find_working_order_execution,
    normalize_events, protection_range_diagnostic,
)
from .execution import ExecutionPolicy, is_crossed_level_rejection, trigger_level_passed
from .model import CycleState, Leg, stop_for, target_for
from .notifications import (
    NotificationHistoryWorker, TransactionHistoryWorker, migrate_notification_jobs, split_report,
)
from .reconcile import RemoteSnapshot
from .reporting import (
    cycle_heading, cycle_result_text, leg_details, pnl_text, recovery_change_text,
    recovery_snapshot, scenario_nine_result_text, status_text, transaction_result_fingerprint,
)
from .streaming import PriceWatch, QuoteStream
from .telegram import Telegram


LOG = logging.getLogger(__name__)
D = Decimal


class Bot:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.state = CycleState.load(cfg.state_file)
        if migrate_notification_jobs(self.state.pending_notification_jobs):
            # Only the main Bot thread owns and persists CycleState.  The history worker receives
            # copies and never writes bot_state.json.
            self.state.save(cfg.state_file)
        self.strategy = Strategy(cfg, self.state)
        self.capital = CapitalClient(cfg)
        self.quotes = QuoteStream(
            cfg.epic,
            self.capital.streaming_tokens,
            enabled=cfg.websocket_enabled,
            stale_seconds=cfg.websocket_stale_seconds,
        )
        self.telegram = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.telegram.offset = self.state.telegram_offset
        self.reconciled = False
        self._flat_checks = 0
        self._missing_exit_since: float | None = None
        self._initial_entry_close: tuple[Leg, str, Decimal] | None = None
        self._stream_signal = False
        self._last_cycle_rest_check = 0.0
        self.execution_policy = ExecutionPolicy()
        self.continuation = CycleContinuation(self)
        self.notification_worker = NotificationHistoryWorker(cfg)
        self.transaction_worker = TransactionHistoryWorker(cfg)
        self._queued_report_parts: set[tuple[str, int]] = set()
        self._queued_log_parts: set[tuple[str, int]] = set()
        if (not self.state.continuation_managed and self.state.active
                and self.state.cycle_attempt > 1):
            self.state.continuation_managed = True
            if any(leg and leg.pending_market_kind for leg in (self.state.long, self.state.short)):
                self.state.continuation_stage = "FORMING_PAIR"
            elif self.state.phase == "CONTINUATION_FILTER":
                self.state.continuation_stage = "FILTER"
            elif self.state.phase == "DOUBLE_SL_RECONCILING":
                self.state.continuation_stage = "RECONCILING"
            else:
                self.state.continuation_stage = "ACTIVE"
        if self.state.phase == "PAUSED_DOUBLE_SL":
            # Compatible migration from the one-release safety pause. No broker mutation is
            # performed here; startup reconciliation still runs before the timer can release.
            self.state.active = True
            self.state.paused = True
            self.state.phase = "DOUBLE_SL_PAUSE"
            self.state.continuation_pause_until = time.time() + 300
            self.state.continuation_managed = True
            self.state.continuation_stage = "PAUSE"
        if self.state.active:
            attempt = (
                self.state.active_attempt_id or self.state.diagnostic_cycle_number
                or self.state.attempt_counter + 1
            )
            self.state.active_attempt_id = self.state.diagnostic_cycle_number = attempt
            self.state.cycle_id = self.state.cycle_id or attempt
            self.state.cycle_attempt = self.state.cycle_attempt or 1
            self.state.attempt_counter = max(self.state.attempt_counter, attempt)
            begin_diagnostic_cycle(
                self.cfg.diagnostic_log_file, attempt, self.state.completed_cycles
            )
        else:
            end_diagnostic_cycle(
                self.cfg.diagnostic_log_file,
                max(self.state.attempt_counter, self.state.diagnostic_cycle_number) + 1,
                self.state.completed_cycles,
            )

    def _leg_details(self, leg: Leg, **kwargs) -> str:
        return leg_details(
            leg, general_recovery=self.state.general_recovery,
            scenario=self.state.scenario, **kwargs
        )

    def _get_continuation(self) -> CycleContinuation:
        controller = getattr(self, "continuation", None)
        if controller is None:
            controller = self.continuation = CycleContinuation(self)
        return controller

    def _send_report(self, text: str, *, key: str = "") -> None:
        # Test and third-party transports historically implement only ``send``.  Do not let a
        # dynamically-created Mock attribute swallow reports: chunking belongs to our concrete
        # asynchronous Telegram transport, while compatible transports receive the same text.
        if isinstance(self.telegram, Telegram) and self.telegram.enabled:
            if "🏁 Итог завершённого цикла" in text:
                existing_completion = next((item for item in self.state.report_outbox
                    if str(item.get("key", "")).startswith(
                        f"cycle-complete:{self.state.cycle_id}:{self.state.completed_cycles}"
                    )), None)
                if existing_completion is not None:
                    self._queue_pending_reports()
                    return
            identity = key or hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
            existing = self._store_report(text, identity)
            if existing is not None:
                self._queue_pending_reports()
                return
            self.state.save(self.cfg.state_file)
            self._queue_pending_reports()
        else:
            self.telegram.send(text)

    def _store_report(self, text: str, identity: str) -> dict | None:
        """Put a report in state without saving, for atomic trading-event commits.

        ``None`` means a new report was stored; an existing report is returned for idempotency.
        The main thread remains the only owner of CycleState and its file.
        """
        existing = next(
            (item for item in self.state.report_outbox if item.get("key") == identity), None
        )
        if existing is not None:
            return existing
        report_id = f"{self.state.next_report_id}:{identity}"
        self.state.next_report_id += 1
        self.state.report_outbox.append({
            "id": report_id, "key": identity,
            "parts": [{"number": index, "text": value, "status": "pending"}
                      for index, value in enumerate(split_report(text), 1)],
        })
        return None

    def _queue_pending_reports(self) -> None:
        if not isinstance(self.telegram, Telegram) or not self.telegram.enabled:
            return
        for report in self.state.report_outbox:
            total = len(report.get("parts", []))
            for part in report.get("parts", []):
                marker = (str(report["id"]), int(part["number"]))
                if part.get("status") != "pending" or marker in self._queued_report_parts:
                    continue
                if self.telegram.send_report_part(
                    str(part["text"]), marker[0], marker[1], total
                ):
                    self._queued_report_parts.add(marker)

    def _tick_notifications(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        changed = False
        worker = getattr(self, "notification_worker", None)
        if worker is not None:
            for completed in worker.results():
                job = next((item for item in self.state.pending_notification_jobs
                            if item.get("key") == completed["key"]), None)
                if job is None:
                    if isinstance(worker, NotificationHistoryWorker):
                        worker.acknowledge(completed["key"])
                    continue
                if (int(job.get("generation", self.state.transaction_generation))
                        != self.state.transaction_generation
                        or (job.get("cycle_id") and self.state.active
                            and int(job["cycle_id"]) != self.state.cycle_id)):
                    if isinstance(worker, NotificationHistoryWorker):
                        worker.acknowledge(completed["key"])
                    continue
                waiting = dict(job["waiting"])
                waiting.update(completed["result"])
                self._send_report(
                    self._initial_pair_report_text(job["closed"], waiting, final=True, meta=job),
                    key=f"{job['key']}:final",
                )
                self.state.pending_notification_jobs.remove(job)
                self.state.save(self.cfg.state_file)
                if isinstance(worker, NotificationHistoryWorker):
                    worker.acknowledge(completed["key"])
            for job in self.state.pending_notification_jobs:
                worker.submit(job)
        transaction_worker = getattr(self, "transaction_worker", None)
        if transaction_worker is not None:
            for completed in transaction_worker.results():
                job = next((item for item in self.state.pending_transaction_jobs
                            if item.get("key") == completed["key"]), None)
                if job is None:
                    if isinstance(transaction_worker, TransactionHistoryWorker):
                        transaction_worker.acknowledge(completed["key"])
                    continue
                if (int(job.get("generation", self.state.transaction_generation))
                        != self.state.transaction_generation
                        or int(job.get("cycle_id", self.state.cycle_id) or 0)
                        != self.state.cycle_id):
                    if isinstance(transaction_worker, TransactionHistoryWorker):
                        transaction_worker.acknowledge(completed["key"])
                    continue
                if completed.get("error"):
                    job["retry_count"] = int(job.get("retry_count", 0) or 0) + 1
                    job["next_check_at"] = now + min(300, 5 * 2 ** (job["retry_count"] - 1))
                    if isinstance(transaction_worker, TransactionHistoryWorker):
                        transaction_worker.acknowledge(completed["key"])
                    changed = True
                    continue
                result = completed["result"]
                observed = result.get("observed_to_epoch")
                previous_observed = float(job.get("observed_to_epoch", 0) or 0)
                if observed is not None and float(observed) < previous_observed:
                    if isinstance(transaction_worker, TransactionHistoryWorker):
                        transaction_worker.acknowledge(completed["key"])
                    continue
                fingerprint = transaction_result_fingerprint(result)
                if fingerprint != job.get("result_fingerprint"):
                    job["status"] = result["status"]
                    job["amount"] = (str(result["amount"])
                                     if result.get("amount") is not None else None)
                    job["currency"] = result.get("currency", "")
                    job["components"] = result.get("components", [])
                    job["result_fingerprint"] = fingerprint
                    attempt = next((item for item in self.state.attempt_history
                                    if item.get("attempt_id") == job.get("attempt_id")), None)
                    if attempt is not None:
                        attempt["broker_transaction_status"] = job["status"]
                        attempt["broker_transaction_pnl"] = job["amount"]
                        attempt["broker_transaction_currency"] = job["currency"]
                        attempt["broker_transaction_components"] = list(job["components"])
                    if job.get("attempt_id") == max(
                        (int(item.get("attempt_id", 0) or 0)
                         for item in self.state.pending_transaction_jobs), default=0
                    ):
                        self.state.broker_transaction_status = job["status"]
                        self.state.broker_transaction_pnl = (
                            D(job["amount"]) if job["amount"] is not None else None
                        )
                        self.state.broker_transaction_currency = job["currency"]
                        self.state.broker_transaction_components = list(job["components"])
                    changed = True
                if observed is not None:
                    job["observed_to_epoch"] = max(previous_observed, float(observed))
                job["retry_count"] = 0
                job["next_check_at"] = now + (
                    3600 if result.get("status") == "COMPLETE_SNAPSHOT" else 30
                )
                if isinstance(transaction_worker, TransactionHistoryWorker):
                    transaction_worker.acknowledge(completed["key"])
                changed = True
            for job in self.state.pending_transaction_jobs:
                job.setdefault("next_check_at", 0)
                job.setdefault("retry_count", 0)
                if float(job["next_check_at"] or 0) <= now:
                    transaction_worker.submit(job)
        if isinstance(self.telegram, Telegram):
            for ack in self.telegram.delivery_acks():
                marker = (str(ack["report_id"]), int(ack["part"]))
                self._queued_report_parts.discard(marker)
                for report in self.state.report_outbox:
                    if str(report.get("id")) != marker[0]:
                        continue
                    for part in report.get("parts", []):
                        if int(part.get("number", 0)) == marker[1]:
                            part["status"] = ack["status"]
                            changed = True
            self.state.report_outbox[:] = [
                report for report in self.state.report_outbox
                if not all(part.get("status") == "delivered" for part in report.get("parts", []))
            ]
            for ack in self.telegram.document_acks():
                marker = (str(ack["snapshot_id"]), int(ack["part"]))
                self._queued_log_parts.discard(marker)
                completed = acknowledge_diagnostic_snapshot(
                    self.cfg.diagnostic_log_file, marker[0], marker[1], str(ack["status"])
                )
                self.telegram.confirm_document_ack(marker[0], marker[1])
                if completed:
                    self._send_report(
                        f"✅ /sendlog полностью доставлен: снимок {marker[0]}",
                        key=f"sendlog-complete:{marker[0]}",
                    )
            for snapshot in pending_diagnostic_snapshots(self.cfg.diagnostic_log_file):
                total = len(snapshot.get("parts", []))
                self.telegram.restore_document_group(
                    str(snapshot["id"]), total,
                    [int(part["number"]) for part in snapshot.get("parts", [])
                     if part.get("status") == "delivered"],
                )
                already_queued = self.telegram.queued_document_parts()
                for part in snapshot.get("parts", []):
                    marker = (str(snapshot["id"]), int(part["number"]))
                    if (part.get("status") != "pending" or marker in self._queued_log_parts
                            or marker in already_queued):
                        continue
                    if self.telegram.send_document_part(
                        str(part["path"]), marker[0], marker[1], total
                    ):
                        self._queued_log_parts.add(marker)
        if changed:
            # Delivery acknowledgement is durable immediately; it must not wait for a broker
            # tick or any later state mutation.
            self.state.save(self.cfg.state_file)
        self._queue_pending_reports()

    def _dispatch_owned_cycle(self) -> None:
        """Return shared-operation follow-up to the exclusive current owner."""
        if self.state.continuation_managed:
            self._get_continuation().handle_active_scenario()
        else:
            self._tick_cycle()

    def _complete_cycle(self, direction: str, fill: Decimal | None) -> None:
        """Complete one cycle and move subsequent startup/gap records outside its boundary."""
        attempt_id = self.state.active_attempt_id
        self.strategy.complete(direction, fill)
        self._get_continuation().release()
        # Attempt history is incremental; the cycle aggregate already includes earlier attempts.
        # Recording the full cycle result here would count previous double-SL attempts twice.
        current_attempt_losses = (
            self.state.realized_loss_money - self.state.cycle_attempt_start_loss_money
        )
        winner = self.state.long if direction == "BUY" else self.state.short
        current_attempt_profit = D("0")
        if fill is not None and winner is not None:
            move = (fill - winner.current_entry if direction == "BUY"
                    else winner.current_entry - fill)
            current_attempt_profit = max(D("0"), move) * winner.size
        current_attempt_result = current_attempt_profit - current_attempt_losses
        self.state.remember_attempt(
            "COMPLETED_CYCLE", current_attempt_result,
            scenario=self.state.scenario, completed_cycle=self.state.completed_cycles,
        )
        LOG.info(
            "TRADING ATTEMPT %s COMPLETED direction=%s fill=%s result=%s",
            attempt_id, direction, fill, self.state.net_cycle_result,
        )
        self.state.active_attempt_id = 0
        if fill is not None:
            next_mode = (
                "PAUSED: пользовательский /stop запрещает новый цикл до /start."
                if self.state.paused else "FILTER: после завершения будет разрешён фильтр нового цикла."
            )
            text = cycle_result_text(self.state, direction, fill, self.cfg.size) + (
                f"\nДальнейший режим: {next_mode}"
            )
            self._store_report(
                text, f"cycle-complete:{self.state.cycle_id}:{self.state.completed_cycles}"
            )
            self._archive_and_clear_completed_cycle(text)
        # Completion counters and a recoverable final report are committed by one atomic replace.
        self.state.save(self.cfg.state_file)
        end_diagnostic_cycle(
            self.cfg.diagnostic_log_file, self.state.attempt_counter + 1,
            self.state.completed_cycles,
        )

    def _archive_and_clear_completed_cycle(self, report: str) -> None:
        """Write one self-contained final ledger, then remove cycle-detail working state."""
        archive = {
            "cycle_id": self.state.cycle_id, "completed_cycles": self.state.completed_cycles,
            "report": report, "deal_history": self.state.deal_history,
            "attempt_history": self.state.attempt_history,
            "recovery_events": self.state.recovery_events,
            "scenario_transitions": self.state.scenario_transitions,
            "trigger_race_results": self.state.trigger_race_results,
            "broker_transaction_status": self.state.broker_transaction_status,
            "broker_transaction_pnl": (str(self.state.broker_transaction_pnl)
                                       if self.state.broker_transaction_pnl is not None else None),
            "broker_transaction_currency": self.state.broker_transaction_currency,
            "broker_transaction_components": self.state.broker_transaction_components,
        }
        LOG.info("CYCLE_LEDGER_FINAL %s", json.dumps(archive, ensure_ascii=False, sort_keys=True))
        self.state.completed_cycle_report = report
        completed_cycle_id = self.state.cycle_id
        self.state.deal_history.clear()
        self.state.attempt_history.clear()
        self.state.recovery_events.clear()
        self.state.pending_recovery.clear()
        self.state.scenario_transitions.clear()
        self.state.trigger_race_results.clear()
        self.state.attempt_deal_ids.clear()
        self.state.cycle_trigger_ids.clear()
        self.state.processed_events.clear()
        self.state.pending_actual_attempt_id = 0
        self.state.pending_actual_deal_ids.clear()
        self.state.pending_notification_jobs[:] = [
            job for job in self.state.pending_notification_jobs
            if int(job.get("cycle_id", 0) or 0) != completed_cycle_id
        ]
        self.state.pending_transaction_jobs[:] = [
            job for job in self.state.pending_transaction_jobs
            if int(job.get("cycle_id", 0) or 0) != completed_cycle_id
        ]
        self.state.broker_transaction_pnl = None
        self.state.broker_transaction_currency = ""
        self.state.broker_transaction_status = "UNAVAILABLE"
        self.state.broker_transaction_components.clear()
        self.state.transaction_generation += 1
        self.state.long = self.state.short = None

    def _finish_failed_initial_attempt(
        self, leg: Leg, source: str, fill: Decimal, *, opposite_sent: bool
    ) -> None:
        """Account for a resolved one-sided initial attempt and require an explicit /start."""
        if self.state.continuation_managed:
            self._get_continuation().finish_single_leg(leg, source, fill)
            return
        entry = leg.current_entry
        points = fill - entry if leg.direction == "BUY" else entry - fill
        size = leg.size if self.cfg.scenario_sizes else self.cfg.size
        money = points * size
        attempt_id = self.state.active_attempt_id or self.state.diagnostic_cycle_number
        self.state.remember_deal(leg, scenario=1)
        self.state.remember_close(leg.deal_id, source, fill)
        self.state.remember_attempt(
            "INITIAL_PAIR_NOT_FORMED", money,
            direction=leg.direction, entry=entry, close=fill, close_source=source,
            deal_id=leg.deal_id, deal_reference=leg.deal_reference, planned_stop=leg.stop,
            opposite_order_sent=opposite_sent, completed_cycle=None,
        )
        LOG.warning(
            "TRADING ATTEMPT %s FAILED_INITIAL direction=%s entry=%s close=%s source=%s "
            "result=%s opposite_sent=%s",
            attempt_id, leg.direction, entry, fill, source, money, opposite_sent,
        )
        LOG.info("FAILED_INITIAL_LEDGER %s", json.dumps({
            "attempt_id": attempt_id, "deal_history": self.state.deal_history,
            "attempt_history": self.state.attempt_history,
        }, ensure_ascii=False, sort_keys=True))
        total = self.state.attempt_result_total
        self.state.reset()
        self.state.paused = True
        self.state.armed = False
        self.state.phase = "PAUSED"
        self.state.save(self.cfg.state_file)
        end_diagnostic_cycle(
            self.cfg.diagnostic_log_file, self.state.attempt_counter + 1,
            self.state.completed_cycles,
        )
        self._send_report(
            f"⏸ Начальная торговая попытка №{attempt_id} завершена без пары\n"
            f"Сторона: {leg.direction}\nПричина закрытия: {source}\n"
            f"Фактический вход: {entry}\nФактическое закрытие: {fill}\n"
            f"Результат попытки: {points} пункта / {money} при размере {size}\n"
            f"Противоположная заявка отправлялась: {'ДА' if opposite_sent else 'НЕТ'}\n"
            f"Общий результат сохранённых попыток: {total}\n"
            "Открытых связанных позиций и ордеров не осталось. Автоматика на паузе; "
            "следующий вход только после /start."
        )

    def run(self) -> None:
        self.telegram.start()
        self.notification_worker.start()
        self.transaction_worker.start()
        # Durable Telegram reports are independent of Capital.com startup reconciliation.
        self._tick_notifications()
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
                # Outbox/history/log delivery is independent of Capital.com availability.
                self._tick_notifications()
                # Reconcile before processing queued Telegram commands. A /start message may
                # already be waiting when Pydroid launches the process.
                if not self.reconciled:
                    self.reconcile_startup()
                if self.reconciled:
                    self.quotes.start()
                commands = self.telegram.commands()
                # Persist consumed update IDs before a broker-mutating command can run. If
                # Android kills Pydroid immediately after that command, Telegram will not replay
                # the same broker-mutating command or /start on restart.
                self.state.telegram_offset = self.telegram.offset
                self.state.save(self.cfg.state_file)
                self._process_commands(commands)
                self._tick_notifications()
                if not self.state.manual:
                    self.tick()
                self.quotes.watch(self._price_watches())
                self.state.save(self.cfg.state_file)
            except Exception as exc:
                LOG.exception("Loop error")
                self.telegram.send(f"⚠️ Ошибка цикла: {exc}")
            # During an active cycle poll at least twice per second even when an older preserved
            # config still contains POLL_SECONDS=1.
            delay = min(self.cfg.poll_seconds, 0.5) if self.state.active else self.cfg.poll_seconds
            # A streaming quote that reaches SL, TP or trigger wakes this wait immediately. The
            # following loop still performs REST /positions and History checks before changing
            # state. A timeout preserves the existing polling fallback while streaming is stale,
            # disconnected or unavailable.
            self._stream_signal = self.quotes.wait(delay)

    def _price_watches(self) -> list[PriceWatch]:
        if not self.state.active or self.state.manual:
            return []
        watches: list[PriceWatch] = []
        for leg in (self.state.long, self.state.short):
            if not leg:
                continue
            if leg.open:
                if leg.stop is not None:
                    watches.append(PriceWatch("SL", leg.direction, leg.stop))
                if leg.take_profit is not None:
                    watches.append(PriceWatch("TP", leg.direction, leg.take_profit))
            elif leg.trigger_id:
                watches.append(PriceWatch("TRIGGER", leg.direction, leg.original_trigger_level))
        return watches

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
                "/automode — безопасно выйти из ручного режима\n"
                "/profit200 VALUE — личный profit следующих 200 завершённых циклов\n"
                "/dealhistory [DEAL_ID] — история сохранённых сделок или точного ID\n"
                "/sendlog — прислать текущий диагностический файл\n"
                "/setsl long|short PRICE /settp long|short PRICE\n"
                "/canceltrigger long|short\n"
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
            if (self.state.continuation_managed
                    or self.state.phase in {"DOUBLE_SL_PAUSE", "CONTINUATION_FILTER"}):
                self.state.continuation_stopped_by_user = True
            if not self.state.active:
                self.state.armed = False
                self.state.waiting_current_candle = False
                self.state.phase = "PAUSED"
                self.telegram.send("Пауза: новый цикл не откроется до команды /start.")
            else:
                if (self.state.continuation_managed
                        or self.state.phase in {"DOUBLE_SL_PAUSE", "CONTINUATION_FILTER"}):
                    self.telegram.send(
                        "⛔ /stop принят: автоматическое продолжение цикла после двух SL "
                        "заблокировано. Состояние и таймер сохранены."
                    )
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
        elif command in {"/setsl", "/settp", "/removesl", "/removetp", "/canceltrigger"}:
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
        if self.state.phase == "DOUBLE_SL_RECONCILING":
            raise RuntimeError("Сначала должна завершиться сверка двух SL и связанных trigger")
        if self.state.phase == "DOUBLE_SL_PAUSE":
            remaining = max(0, int(self.state.continuation_pause_until - time.time()))
            if remaining:
                raise RuntimeError(f"Продолжение цикла станет доступно через {remaining} сек.")
            self.state.continuation_stopped_by_user = False
            self.state.paused = False
            self.state.phase = "CONTINUATION_FILTER"
            self.state.continuation_managed = True
            self.state.continuation_stage = "FILTER"
            self.state.save(self.cfg.state_file)
            return
        if self.state.phase == "CONTINUATION_MANUAL_PAIR_PAUSE":
            if self._cycle_positions() or any(
                self._order_epic(item) == self.cfg.epic for item in self.capital.working_orders()
            ):
                raise RuntimeError("Связанные позиции/ордера ещё не разрешены; /start заблокирован")
            self.state.paused = False
            self.state.armed = True
            self.state.phase = "CONTINUATION_FILTER"
            self.state.continuation_stage = "FILTER"
            self.state.save(self.cfg.state_file)
            self.telegram.send("🔎 Ручная пауза снята; ожидаю фильтр продолжения того же цикла.")
            return
        if self.state.continuation_managed:
            if self.state.continuation_stage in {"RECONCILING", "FORMING_PAIR"} and any(
                leg and leg.pending_market_kind for leg in (self.state.long, self.state.short)
            ):
                raise RuntimeError("Незавершённая заявка ещё сверяется; /start не меняет её исход")
            self.state.continuation_stopped_by_user = False
            self.state.paused = False
            self.state.save(self.cfg.state_file)
            self.telegram.send(
                f"▶️ Продолжение цикла разрешено; этап {self.state.continuation_stage}."
            )
            return
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
        if self.state.pending_actual_attempt_id:
            self._refresh_actual_attempt_result()
        if self.state.continuation_managed:
            self._get_continuation().tick()
        elif self.state.armed and not self.state.active and not self.state.paused:
            self._tick_filter()
        elif self.state.active:
            if self._should_check_cycle_rest():
                self._tick_cycle()

    def _should_check_cycle_rest(self) -> bool:
        """Use stream crossings for immediacy, with periodic REST as an independent fallback."""
        quotes = getattr(self, "quotes", None)
        signalled = bool(getattr(self, "_stream_signal", False))
        self._stream_signal = False
        now = time.monotonic()
        last_check = getattr(self, "_last_cycle_rest_check", 0.0)
        fallback = getattr(self.cfg, "websocket_rest_fallback_seconds", 2.0)
        stream_available = bool(quotes is not None and quotes.connected and quotes.latest() is not None)
        if not stream_available or signalled or now - last_check >= fallback:
            self._last_cycle_rest_check = now
            if quotes is not None:
                reason = quotes.consume_signal() if signalled else (
                    "stream_unavailable" if not stream_available else "periodic_fallback"
                )
                quotes.log_rest_check(reason)
            return True
        return False

    def _tick_filter(self, starter=None) -> None:
        starter = starter or self._start_cycle
        closed, current = self.capital.candle_ranges(self.cfg.epic, self.cfg.candle_minutes)
        if not self.state.waiting_current_candle and closed >= self.cfg.entry_range:
            starter(f"закрытая свеча: {closed}")
        elif current >= self.cfg.entry_range:
            starter(f"текущая свеча: {current}")
        else:
            self.state.waiting_current_candle = True

    def _start_cycle(self, filter_reason: str = "условие фильтра выполнено") -> None:
        self._start_pair_common(filter_reason, continuation=False)

    def _start_pair_common(self, filter_reason: str, *, continuation: bool,
                           preflight_done: bool = False) -> None:
        positions = self._cycle_positions() if not preflight_done else {}
        orders = ([item for item in self.capital.working_orders()
                   if self._order_epic(item) == self.cfg.epic]
                  if not preflight_done else [])
        if not preflight_done and (positions or orders):
            # Capital may keep the just-closed cycle in list endpoints briefly. Starting during
            # that window can bind a new confirmation to an old same-direction position.
            LOG.warning(
                "New cycle delayed until broker is flat: position_ids=%s order_count=%s",
                sorted(positions), len(orders),
            )
            self._flat_checks = 0
            return
        self._flat_checks = 3 if preflight_done else getattr(self, "_flat_checks", 0) + 1
        if not preflight_done and self._flat_checks < 3:
            LOG.info("Broker flat check %s/3 before new cycle", self._flat_checks)
            return
        self._flat_checks = 0
        if continuation and not self.state.active_attempt_id:
            self.state.attempt_counter = max(
                self.state.attempt_counter, self.state.diagnostic_cycle_number
            ) + 1
            cycle_number = self.state.attempt_counter
            self.state.active_attempt_id = cycle_number
            self.state.cycle_attempt += 1
            self.state.attempt_deal_ids.clear()
            self.state.initial_submitted_directions.clear()
            self.state.cycle_attempt_start_losses = self.state.realized_losses
            self.state.cycle_attempt_start_loss_money = self.state.realized_loss_money
        elif self.state.active_attempt_id:
            cycle_number = self.state.active_attempt_id
        else:
            self.state.attempt_counter = max(
                self.state.attempt_counter, self.state.diagnostic_cycle_number
            ) + 1
            cycle_number = self.state.attempt_counter
            self.state.attempt_deal_ids.clear()
            self.state.initial_submitted_directions.clear()
        self.state.active_attempt_id = cycle_number
        if not continuation:
            self.state.cycle_id = cycle_number
            self.state.cycle_attempt = 1
            self.state.cycle_attempt_start_losses = D("0")
            self.state.cycle_attempt_start_loss_money = D("0")
        self.state.diagnostic_cycle_number = cycle_number
        self.state.save(self.cfg.state_file)
        begin_diagnostic_cycle(
            self.cfg.diagnostic_log_file, cycle_number, self.state.completed_cycles
        )
        LOG.info(
            "TRADING ATTEMPT %s START completed_cycles=%s filter=%s",
            cycle_number, self.state.completed_cycles, filter_reason,
        )
        bid, ask = self.capital.quote(self.cfg.epic)
        if continuation:
            self.strategy.begin_continuation(ask, bid)
        else:
            self.strategy.begin(ask, bid)
        assert self.state.long and self.state.short
        self._send_report(
            f"🚦 {'Продолжение цикла №' + str(self.state.cycle_id) if continuation else 'Начинаю цикл'}\n"
            f"Сценарий: {self.state.scenario}; попытка: {self.state.cycle_attempt}\n"
            f"Фильтр: {filter_reason}\nBID: {bid}\nASK: {ask}\n"
            f"Предварительный spread: {ask - bid}\nРазмер каждой стороны: {self.state.long.size}\n"
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
                close = self._wait_accepted_initial_close(lost)
                if close is not None:
                    source, fill = close
                    self._finish_failed_initial_attempt(
                        lost, source, fill, opposite_sent=False
                    )
                    return
                self._set_pending_market(
                    lost, "INITIAL", set(),
                    reason="first initial leg disappeared before opposite submission",
                    unknown_post=False,
                )
                lost.pending_market_reference = lost.deal_reference
                self.state.save(self.cfg.state_file)
                self._send_report(
                    f"⏳ Первая сторона {lost.direction} исчезла до открытия противоположной. "
                    "Вторая заявка не отправлена; новый вход заблокирован до появления связанного "
                    "SL/TP в истории."
                )
                return
            error = self._open_initial_leg(leg)
            if error:
                if self.state.manual or leg.pending_market_kind:
                    # An unknown POST outcome must survive across restarts/ticks. In particular,
                    # do not reset and re-arm the filter merely because no dealReference arrived.
                    self.state.save(self.cfg.state_file)
                    return
                early_close = getattr(self, "_initial_entry_close", None)
                if opened and early_close and early_close[0] is leg:
                    _, source, fill = early_close
                    self._initial_entry_close = None
                    handler = (self._get_continuation().handle_fast_second_close
                               if continuation else self._continue_after_second_initial_close)
                    if handler(leg, source, fill):
                        return
                if not opened and early_close and early_close[0] is leg:
                    _, source, fill = early_close
                    self._initial_entry_close = None
                    self._finish_failed_initial_attempt(leg, source, fill, opposite_sent=False)
                    return
                if opened or leg.deal_reference:
                    self._manual(f"Неполный или несинхронизированный hedge {leg.direction}: {error}")
                else:
                    if continuation:
                        self.state.long = self.state.short = None
                        self.state.active_attempt_id = 0
                        self.state.armed = True
                        self.state.phase = "CONTINUATION_FILTER"
                        self.state.continuation_stage = "FILTER"
                        self.state.save(self.cfg.state_file)
                        self._send_report(
                            f"⚠️ Продолжение цикла №{self.state.cycle_id}: первая сторона не "
                            f"открыта ({error}). Убытки и сценарий сохранены; снова ожидаю фильтр."
                        )
                    else:
                        self.state.reset()
                        self.state.armed = True
                        self.state.phase = "FILTER"
                        self._send_report(
                            f"⚠️ Первая сторона не открыта после 4 попыток: {error}. Цикл не начат."
                        )
                return
            opened.append(leg)
        assert self.state.long and self.state.short
        try:
            if continuation:
                self._get_continuation().confirm_pair_fills()
            else:
                self.strategy.confirm_initial_fills(
                    self.state.long.current_entry, self.state.short.current_entry
                )
        except RuntimeError as exc:
            # Both real positions remain protected by their MARKET stopDistance. An asymmetric
            # or insufficiently confirmed pair has no user-defined monetary spread formula.
            self._manual(f"Пара требует безопасной сверки; Recovery не изменён: {exc}")
            return
        # The first MARKET leg can hit its broker-side stop in the very small window between
        # opening the opposite leg and replacing the provisional distance-based protection with
        # the exact strategy levels.  That is a real scenario-1 stop, not a broken hedge.  Check
        # the broker snapshot before PUT /positions/{dealId} so a legitimate close is replayed by
        # the normal cycle state machine instead of being mislabeled as a 404/manual-mode error.
        if self._continue_after_early_initial_close(continuation):
            return
        try:
            # Establish and verify both exact stops first. Only positions that are still active
            # after that safety barrier receive their take profits.
            for leg in (self.state.long, self.state.short):
                self._apply_stop_only(leg)
                if self._continue_after_early_initial_close(continuation):
                    return
            for leg in (self.state.long, self.state.short):
                self._apply_take_profit_only(leg)
                if self._continue_after_early_initial_close(continuation):
                    return
        except Exception as exc:
            # A position may also close after the snapshot above but while exact protection is
            # being applied.  Reconcile that race once more before stopping automation.
            if self._continue_after_early_initial_close(continuation):
                return
            self._capture_failure_context("initial protection failed", exc)
            self._manual(f"Обе стороны открыты, но точные SL/TP не подтверждены: {exc}")
            return
        self.state.save(self.cfg.state_file)
        if continuation:
            pair_key = f"continuation-pair:{self.state.cycle_id}:{self.state.cycle_attempt}"
            component = next(
                item for item in self.state.recovery_events if item.get("key") == pair_key
            )
            recovery_summary = (
                f"GENERAL_RECOVERY: {component['before']} + подтверждённый continuation "
                f"spread {component['spread_distance']} × {component['size']} = "
                f"{self.state.general_recovery}"
            )
        else:
            recovery_summary = (
                f"GENERAL_RECOVERY={self.state.entry_spread} × "
                f"{self.state.initial_position_size} (spread value) + "
                f"{self.state.target_value} (зафиксированная денежная цель) "
                f"= {self.state.general_recovery}"
            )
        self._send_report(
            f"✅ {cycle_heading(self.state, 'повторная пара подтверждена' if continuation else 'начальная пара подтверждена')}\n"
            "Подтверждение: обе MARKET-позиции имеют broker dealId; точные SL и TP "
            "установлены и прочитаны обратно из Capital.com.\n"
            f"Фактический spread=|{self.state.long.current_entry} − "
            f"{self.state.short.current_entry}|={self.state.entry_spread}\n"
            f"{recovery_summary}\n\n"
            f"{self._leg_details(self.state.long, broker_stop=self.state.long.stop, broker_target=self.state.long.take_profit)}\n\n"
            f"{self._leg_details(self.state.short, broker_stop=self.state.short.stop, broker_target=self.state.short.take_profit)}\n\n"
            "Следующее действие: сопровождать обе позиции; изменение сценария возможно только "
            "после подтверждённого исполнения Trigger."
        )
        if continuation:
            self.state.continuation_stage = "ACTIVE"
            self.state.save(self.cfg.state_file)

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
            self._schedule_initial_pair_report(closed, source, fill, survivor)
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
        self._send_report(
            "🛑 Вторая сторона открылась и сразу закрылась по SL\n"
            f"Сторона: {closed.direction}\n"
            f"Вход: {closed.current_entry}\n"
            f"Плановый SL: {closed.stop}\n"
            f"Фактическое закрытие: {fill}\n"
            f"GENERAL_RECOVERY={self.state.general_recovery} денег; pending D закрытой сделки "
            "перенесётся только после подтверждённого переоткрытия.\n"
            f"Осталась сторона: {survivor.direction}\n"
            f"Новый TP: {survivor.take_profit}\n"
            f"Trigger {closed.direction}: {closed.original_trigger_level}\n"
            "Автоматика продолжает сценарий 1.\n"
            f"Текущее состояние survivor:\n{self._leg_details(survivor)}"
        )
        return True

    def _report_initial_pair_closures(
        self, closed: Leg, source: str, fill: Decimal, other: Leg,
        other_close: tuple[str, Decimal] | None,
    ) -> None:
        """Report the initial-pair race without changing routing or attempt totals."""
        if other_close is None:
            key = f"initial-pair-close-pending:{closed.deal_id}:{fill}"
            if key in self.state.processed_events:
                return
            self._send_report(self._initial_pair_report_text(
                self._leg_report_snapshot(closed, source, fill),
                self._leg_report_snapshot(other), final=False,
            ), key=key)
            self.state.processed_events.append(key)
            self.state.save(self.cfg.state_file)
            return
        other_source, other_fill = other_close
        key = f"initial-pair-double-close:{closed.deal_id}:{fill}:{other.deal_id}:{other_fill}"
        if key in self.state.processed_events:
            return
        self._send_report(self._initial_pair_report_text(
            self._leg_report_snapshot(closed, source, fill),
            self._leg_report_snapshot(other, other_source, other_fill), final=True,
        ), key=key)
        self.state.processed_events.append(key)
        self.state.save(self.cfg.state_file)

    @staticmethod
    def _leg_report_snapshot(
        leg: Leg, source: str = "", fill: Decimal | None = None,
    ) -> dict:
        return {
            "direction": leg.direction, "deal_id": leg.deal_id,
            "deal_reference": leg.deal_reference,
            "working_order_id": leg.trigger_id,
            "trigger_reference": leg.trigger_reference,
            "entry": str(leg.current_entry), "size": str(leg.size),
            "stop": str(leg.stop) if leg.stop is not None else None,
            "take_profit": str(leg.take_profit) if leg.take_profit is not None else None,
            "source": source, "fill": str(fill) if fill is not None else None,
        }

    def _schedule_initial_pair_report(
        self, closed: Leg, source: str, fill: Decimal, waiting: Leg,
    ) -> None:
        key = f"initial-pair-history:{closed.deal_id}:{waiting.deal_id}:{fill}"
        if not any(item.get("key") == key for item in self.state.pending_notification_jobs):
            self.state.pending_notification_jobs.append({
                "key": key, "cycle_id": self.state.cycle_id,
                "cycle_attempt": self.state.cycle_attempt, "scenario": self.state.scenario,
                "closed": self._leg_report_snapshot(closed, source, fill),
                "waiting": self._leg_report_snapshot(waiting),
                "created_at": time.time(), "search_from_epoch": time.time() - 3600,
                "search_to_epoch": time.time() + 86400,
                "original_decision": "MANUAL: начальная пара не была подтверждена",
            })
        self.state.save(self.cfg.state_file)
        self._send_report(
            self._initial_pair_report_text(
                self._leg_report_snapshot(closed, source, fill),
                self._leg_report_snapshot(waiting), final=False,
            ), key=f"{key}:pending",
        )

    def _initial_pair_report_text(
        self, first: dict, second: dict, *, final: bool, meta: dict | None = None,
    ) -> str:
        meta = meta or {}
        heading = (
            f"Цикл №{meta.get('cycle_id') or self.state.cycle_id or self.state.diagnostic_cycle_number or '-'}; "
            f"попытка {meta.get('cycle_attempt') or self.state.cycle_attempt or '-'}; "
            f"сценарий {meta.get('scenario', self.state.scenario)}"
        )
        if not final:
            return (
                f"⏳ {heading}\nСобытие: раннее закрытие начальной пары уточняется\n"
                f"Подтверждено history/activity: {first['direction']} dealId={first['deal_id']}; "
                f"объём={first['size']}; entry={first['entry']}; причина={first['source']}; "
                f"fill={first['fill']}.\n"
                f"{second['direction']} dealId={second['deal_id']}: отсутствует в /positions, но "
                "это не доказательство закрытия. Фоновый read-only обработчик продолжает "
                "deal-specific/global history после перехода в manual; итог пары пока не объявлен. "
                "Новые заявки заблокированы."
            )
        def line(item: dict) -> tuple[str, Decimal]:
            entry, close, size = D(item["entry"]), D(item["fill"]), D(item["size"])
            value = (close - entry) * size if item["direction"] == "BUY" else (entry - close) * size
            formula = (f"({close} − {entry}) × {size}" if item["direction"] == "BUY" else
                       f"({entry} − {close}) × {size}")
            stop = D(item["stop"]) if item.get("stop") is not None else None
            target = D(item["take_profit"]) if item.get("take_profit") is not None else None
            reference = stop if item["source"] == "SL" else target if item["source"] == "TP" else None
            slip = abs(reference - close) if reference is not None else "не определено: исторический уровень неизвестен"
            return (
                f"{item['direction']} dealId={item['deal_id']}: объём={size}; entry={entry}; "
                f"исторический SL={stop}; исторический TP={target}; причина={item['source']}; fill={close}; "
                f"slippage={slip}; результат={formula}={value}", value,
            )
        first_line, first_value = line(first)
        second_line, second_value = line(second)
        return (
            f"⛔ {heading}\nСобытие: обе стороны начальной пары закрылись при формировании\n"
            "Подтверждение: отдельные broker history/activity события для каждой позиции.\n"
            f"{first_line}\n{second_line}\n"
            f"Итог двух позиций={first_value} + {second_value} = {first_value + second_value}.\n"
            "Этот информационный итог НЕ добавлен в attempt_result_total и Recovery.\n"
            f"Решение в момент события: {meta.get('original_decision', 'MANUAL: initial-пара не сформирована')}. "
            f"Это отложенный отчёт старой попытки; текущее состояние бота: phase={self.state.phase}, "
            f"active={self.state.active}, manual={self.state.manual}. Оно могло измениться после события."
        )

    def _continue_after_early_initial_close(self, continuation: bool = False) -> bool:
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
        if continuation:
            self.state.continuation_stage = "ACTIVE"
            self._get_continuation().handle_active_scenario()
        else:
            self._tick_cycle()
        return True

    def _open_initial_leg(self, leg: Leg) -> str | None:
        last_error = "заявка отклонена"
        for _attempt in range(self.execution_policy.attempts):
            preexisting_ids = set(self._cycle_positions())
            if leg.direction not in self.state.initial_submitted_directions:
                self.state.initial_submitted_directions.append(leg.direction)
            self._set_pending_market(
                leg, "INITIAL", preexisting_ids,
                reason=f"initial {leg.direction} POST outcome pending",
                unknown_post=True,
            )
            try:
                # The previous cycle can remain briefly visible in /positions. Remember every
                # pre-existing id so wait_position cannot bind the new leg to a stale position
                # merely because it has the same direction.
                reference = self.capital.open_position(
                    self.cfg.epic, leg.direction, leg.size,
                    stop_distance=leg.stop_distance,
                )
            except Exception as exc:
                # Capital generates dealReference in the response. If that response is lost there
                # is no idempotency key, but a newly visible position can still prove execution.
                try:
                    position = self._resolve_unknown_market_position(
                        leg.direction, preexisting_ids, attempts=20
                    )
                except Exception as reconcile_exc:
                    leg.pending_market_reason = str(reconcile_exc)
                    self.state.save(self.cfg.state_file)
                    LOG.warning(
                        "Initial MARKET POST and position reconciliation are unavailable: %s",
                        reconcile_exc,
                    )
                    return (
                        "результат MARKET-заявки не установлен; повторное открытие заблокировано: "
                        f"{reconcile_exc}"
                    )
                if position is not None:
                    leg.deal_id = str(position["dealId"])
                    if position.get("size") is not None:
                        leg.size = D(str(position["size"]))
                        leg.size_confirmation = "positions"
                    fill = position.get("level")
                    if fill is None:
                        return "MARKET-позиция найдена без фактической цены входа"
                    leg.current_entry = leg.original_trigger_level = D(str(fill))
                    leg.entry_confirmation = "positions"
                    leg.stop = stop_for(leg.direction, leg.current_entry, leg.stop_distance)
                    self._clear_pending_market(leg)
                    self._send_report(
                        f"⚠️ Ответ начального MARKET POST потерян, но позиция подтверждена "
                        f"через /positions\nСторона: {leg.direction}\nDeal ID: {leg.deal_id}\n"
                        f"Фактический вход: {leg.current_entry}"
                    )
                    return None
                # Preserve the unknown submission for later network reconciliation. The active
                # cycle blocks the entry filter, and _tick_cycle resumes this state before exits.
                leg.pending_market_reason = str(exc)
                self.state.save(self.cfg.state_file)
                return (
                    "результат MARKET-заявки не установлен; повторное открытие заблокировано: "
                    f"{exc}"
                )
            try:
                leg.pending_market_reference = reference
                leg.pending_market_unknown_post = False
                self.state.save(self.cfg.state_file)
                confirmation = self._wait_market_submission(reference, rounds=3)
            except Exception as exc:
                # A missing confirmation is an unknown result, not permission to submit again.
                # A newly visible position can nevertheless prove that this exact sequential slot
                # was filled; otherwise persist/manualize the reference so the filter cannot reset.
                try:
                    position = self._resolve_unknown_market_position(
                        leg.direction, preexisting_ids, attempts=20
                    )
                except Exception as reconcile_exc:
                    leg.pending_market_reason = str(reconcile_exc)
                    self.state.save(self.cfg.state_file)
                    return (
                        "результат MARKET-заявки не установлен; повторное открытие заблокировано: "
                        f"{reconcile_exc}"
                    )
                if position is not None and position.get("level") is not None:
                    leg.deal_reference = reference
                    leg.deal_id = str(position["dealId"])
                    if position.get("size") is not None:
                        leg.size = D(str(position["size"]))
                        leg.size_confirmation = "positions"
                    leg.current_entry = leg.original_trigger_level = D(str(position["level"]))
                    leg.entry_confirmation = "positions"
                    leg.stop = stop_for(leg.direction, leg.current_entry, leg.stop_distance)
                    self._clear_pending_market(leg)
                    return None
                leg.pending_market_reason = "initial confirmation unavailable"
                self.state.save(self.cfg.state_file)
                return (
                    "результат MARKET-заявки не установлен; повторное открытие заблокировано: "
                    f"{exc}"
                )
            if confirmation.get("dealStatus") != "ACCEPTED":
                last_error = confirmation.get("reason") or last_error
                self._clear_pending_market(leg)
                continue
            # From this point a broker position may exist. Never submit another MARKET order just
            # because /positions has not synchronized yet.
            leg.deal_reference = reference
            leg.deal_id = self._confirmed_position_id(confirmation)
            if confirmation.get("level") is not None:
                leg.current_entry = D(str(confirmation["level"]))
                leg.original_trigger_level = leg.current_entry
                leg.stop = stop_for(leg.direction, leg.current_entry, leg.stop_distance)
                leg.entry_confirmation = "confirmation"
            if confirmation.get("size") is not None:
                leg.size = D(str(confirmation["size"]))
                leg.size_confirmation = "confirmation"
            try:
                position = self.capital.wait_position(
                    leg.deal_id, reference, leg.direction, excluded_ids=preexisting_ids,
                    epic=self.cfg.epic,
                )
                leg.deal_id = str(position["dealId"])
                if position.get("size") is not None:
                    leg.size = D(str(position["size"]))
                    leg.size_confirmation = "positions"
                fill = position.get("level", confirmation.get("level"))
                if fill is None:
                    raise CapitalError("Позиция появилась без фактической цены входа")
                leg.current_entry = D(str(fill))
                leg.entry_confirmation = "positions"
                self._clear_pending_market(leg)
                both_confirmed = all(
                    candidate and candidate.deal_id
                    for candidate in (self.state.long, self.state.short)
                )
                next_action = (
                    "установить и проверить точные SL/TP обеих сторон"
                    if both_confirmed else
                    "открыть вторую сторону либо сверить раннее закрытие первой"
                )
                self._send_report(
                    f"✅ {cycle_heading(self.state, f'{leg.direction} MARKET-позиция подтверждена')}\n"
                    f"Broker confirmation и /positions: dealId={leg.deal_id}; объём={leg.size}; "
                    f"actual entry={leg.current_entry}. Запрос {_attempt + 1}/4.\n"
                    f"Сохранённый original trigger установлен равным первому fill: "
                    f"{leg.original_trigger_level}.\n"
                    f"Предварительный broker-side SL от MARKET distance={leg.stop}; точный SL/TP "
                    f"ещё не подтверждены. Следующее действие: {next_action}."
                )
                return None
            except Exception as exc:
                last_error = str(exc)
                close = self._wait_accepted_initial_close(leg)
                if close is not None:
                    source, fill = close
                    self._clear_pending_market(leg)
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

    def _set_pending_market(
        self, leg: Leg, kind: str, preexisting_ids: set[str], *, reason: str,
        unknown_post: bool,
    ) -> None:
        leg.pending_market_kind = kind
        leg.pending_market_reason = reason
        leg.pending_market_unknown_post = unknown_post
        leg.pending_market_preexisting_ids = sorted(preexisting_ids)
        if unknown_post:
            leg.pending_market_reference = ""
        self.state.save(self.cfg.state_file)

    def _clear_pending_market(self, leg: Leg) -> None:
        leg.pending_market_reference = ""
        leg.pending_market_reason = ""
        leg.pending_market_kind = ""
        leg.pending_market_unknown_post = False
        leg.pending_market_preexisting_ids.clear()
        self.state.save(self.cfg.state_file)

    def _wait_market_submission(self, reference: str, rounds: int = 2) -> dict:
        """Wait longer for one MARKET submission without ever repeating its POST."""
        last_error: Exception | None = None
        for _ in range(rounds):
            try:
                return self.capital.wait_confirmation(reference)
            except Exception as exc:
                last_error = exc
        raise CapitalError(f"Нет окончательного confirmation для {reference}: {last_error}")

    @staticmethod
    def _confirmed_position_id(confirmation: dict) -> str:
        """Prefer the position ID from affectedDeals over an execution/working-order ID."""
        affected = confirmation.get("affectedDeals") or []
        for item in affected:
            if isinstance(item, dict) and str(item.get("status", "")).upper() in {
                "OPEN", "OPENED"
            } and item.get("dealId"):
                return str(item["dealId"])
        for item in affected:
            if isinstance(item, dict) and item.get("dealId"):
                return str(item["dealId"])
        return str(confirmation.get("dealId", ""))

    @staticmethod
    def _confirmation_close_level(confirmation: dict, expected_deal_id: str) -> Decimal | None:
        """Use a close price only when confirmation explicitly belongs to the expected deal."""
        identifiers = {
            str(confirmation.get("dealId", "")),
            *(str(item.get("dealId", "")) for item in confirmation.get("affectedDeals") or []
              if isinstance(item, dict)),
        }
        identifiers.discard("")
        # A level without any identifier is not proof that this particular position closed.
        if expected_deal_id not in identifiers or confirmation.get("level") is None:
            return None
        return D(str(confirmation["level"]))

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
        execution_id = leg.deal_id
        for attempt in range(attempts):
            try:
                activity = self.capital.activity(leg.deal_id)
                direct_close = next(
                    (event for source in ("SL", "TP")
                     if (event := find_close_event(activity, leg.deal_id, source)) is not None
                     and event.level is not None),
                    None,
                )
                if direct_close is not None:
                    self._remember_close_event(leg, direct_close)
                    return direct_close.source, direct_close.level
                global_activity = self.capital.activity()
                opened = find_trigger_open_event(global_activity, execution_id, leg.direction)
                if opened is not None and opened.deal_id:
                    leg.deal_id = opened.deal_id
                    if opened.level is not None:
                        leg.current_entry = leg.original_trigger_level = opened.level
                    activity = global_activity
                for source in ("SL", "TP"):
                    event = find_close_event(activity, leg.deal_id, source)
                    if event is not None and event.level is not None:
                        self._remember_close_event(leg, event)
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
        self._resume_pending_trigger_cancel()
        if self.state.phase == "DOUBLE_SL_RECONCILING":
            self._tick_double_sl_reconciling()
            return
        if self.state.pending_close_reference:
            self._resume_pending_close()
            return
        if self.state.pending_tp_direction and self.state.pending_tp_fill is not None:
            self._finish_reached_take_profit()
            return
        if self._resume_pending_market():
            return
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
                    survivor_sl = self._wait_closing_fill(survivor, "SL") if fill is None else None
                    if fill is None and survivor_sl is not None:
                        if self._resolve_trigger_for_double_stop(stopped, positions):
                            self._begin_double_sl_pause([(survivor, survivor_sl)])
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
                            self._send_report(
                                "⏳ TP подтверждён, но результат отмены trigger ещё неизвестен\n"
                                f"workingOrderId: {stopped.trigger_id}\n"
                                "Цикл и следующий вход заблокированы; сверка продолжится автоматически."
                            )
                            return
                        self.state.last_trigger_resolution = (
                            f"workingOrderId={stopped.trigger_id} исполнился одновременно с TP; "
                            f"позиция разрешена и закрыта, отдельный signed result={race_loss}."
                        )
                        LOG.info(
                            "Working order %s already absent; activity has no accepted trigger "
                            "position, treating cancellation as idempotent",
                            stopped.trigger_id,
                        )
                    else:
                        self.state.last_trigger_resolution = (
                            f"workingOrderId={stopped.trigger_id}: DELETE confirmation ACCEPTED."
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
                self._send_report(
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
                            stop_event = self._close_event_any_index(
                                loser, "SL", global_activity
                            )
                            if self._apply_confirmed_stop_event(
                                loser, stop_event, global_activity
                            ) is None:
                                self._manual(
                                    f"TP/SL chronology недостаточна для {loser.deal_id}"
                                )
                                return
                    self._complete_cycle(winner.direction, fill)
                    self.state.armed = not self.state.paused
                    self.state.phase = "FILTER" if self.state.armed else "PAUSED"
                    self._send_report(
                        "✅ Обе позиции исчезли из списка, но Capital.com подтвердил TP.\n"
                        + cycle_result_text(self.state, winner.direction, fill, self.cfg.size)
                    )
                    return
                sl_closes = [
                    (leg, self._closing_fill_any_index(leg, "SL", global_activity))
                    for leg in missing
                ]
                confirmed_stops = [(leg, fill) for leg, fill in sl_closes if fill is not None]
                if len(confirmed_stops) == len(missing) == 2:
                    for trigger_leg in missing:
                        if (trigger_leg.trigger_id
                                and not self._resolve_trigger_for_double_stop(
                                    trigger_leg, positions
                                )):
                            return
                    self._begin_double_sl_pause(confirmed_stops)
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
            chronology = self.capital.activity()
            stop_event = self._close_event_any_index(survivor, "SL", chronology)
            if (stop_event is None or stop_event.level != stop_fill
                    or self._apply_confirmed_stop_event(
                        survivor, stop_event, chronology
                    ) is None):
                self._manual(
                    f"TP/SL chronology недостаточна для {survivor.deal_id}"
                )
                return
            self._complete_cycle(lost.direction, tp_fill)
            self.state.armed = not self.state.paused
            self.state.phase = "FILTER" if self.state.armed else "PAUSED"
            self.state.save(self.cfg.state_file)
            suffix = (
                "Перехожу к фильтру следующего цикла."
                if self.state.armed else "Следующий цикл ожидает /start."
            )
            self._send_report(
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
        recovery_before = recovery_snapshot(self.state)
        scenario_at_close = self.state.scenario
        broker_execution_time = ""
        opened_record = next((item for item in reversed(self.state.deal_history)
                              if item.get("deal_id") == lost.deal_id), None)
        if opened_record and int(opened_record.get("scenario", self.state.scenario)) < self.state.scenario:
            chronology = self.capital.activity()
            close_event = self._close_event_any_index(lost, "SL", chronology)
            if close_event is None or close_event.level != fill:
                self._manual("Поздний SL не связан с broker chronology")
                return
            resolved = self._scenario_at_broker_close(lost, close_event, chronology)
            if resolved is None:
                self._manual(
                    "Broker chronology позднего SL/reentry недостаточна; "
                    "scenario_at_close не угадан"
                )
                return
            scenario_at_close = resolved
            broker_execution_time = close_event.timestamp.isoformat()
        stopped = self.strategy.stopped(
            lost.direction, fill, f"stop:{lost.deal_id}:{fill}",
            scenario_at_close=scenario_at_close,
            broker_execution_time=broker_execution_time,
        )
        # Queue the broker event before follow-up actions. Previously protection/trigger helpers
        # queued their messages first, making Telegram appear to show trigger before the SL.
        slippage = abs(lost.stop - fill) if lost.stop is not None else D("0")
        loss_points = (lost.current_entry - fill if lost.direction == "BUY"
                       else fill - lost.current_entry)
        result_formula = (
            f"({fill} − {lost.current_entry}) × {lost.size}"
            if lost.direction == "BUY" else
            f"({lost.current_entry} − {fill}) × {lost.size}"
        )
        self._send_report(
            f"🛑 {cycle_heading(self.state, 'SL подтверждён брокерской history/activity')}\n"
            f"Закрыта {lost.direction}: объём={lost.size}; entry={lost.current_entry}; "
            f"подтверждённый SL={lost.stop}; фактический fill={fill}.\n"
            f"SL slippage=|{lost.stop} − {fill}|={slippage}.\n"
            f"Результат позиции={result_formula} = {-max(D('0'), loss_points) * lost.size}.\n"
            f"Сценарий остаётся {self.state.scenario}; SL сам сценарий не увеличивает.\n\n"
            f"{recovery_change_text(self.state, recovery_before, event='пересчёт после SL', direction=lost.direction, stop_slippage=slippage)}\n\n"
            f"Состояние surviving-стороны:\n{self._leg_details(survivor)}\n\n"
            f"План: сначала подтвердить новый TP {survivor.direction}, затем создать Trigger "
            f"{stopped.direction} на сохранённом уровне {stopped.original_trigger_level}. "
            "До broker confirmation Trigger считается только запланированным."
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

    def _resume_pending_market(self) -> bool:
        """Resolve every submitted MARKET before ordinary SL/TP logic can complete a cycle."""
        leg = next(
            (item for item in (self.state.long, self.state.short)
             if item and item.pending_market_kind),
            None,
        )
        if leg is None:
            return False
        preexisting_ids = set(leg.pending_market_preexisting_ids)
        if leg.pending_market_kind == "FALLBACK":
            _, projected_distance, projected_recovery = self.strategy.projected_reopen(leg.direction)
            projected_target = target_for(
                leg.direction, leg.original_trigger_level,
                projected_distance, projected_recovery,
            )
            self._open_passed_trigger_at_market(
                leg, projected_target, leg.pending_market_reason or "pending MARKET reconciliation"
            )
            return True

        # Initial POST without a conclusive result must never fall through to BOTH_OPEN handling:
        # the opposite side may not have been submitted at all.
        try:
            continuation_rounds = 1 if self.state.continuation_managed else 20
            position = self._resolve_unknown_market_position(
                leg.direction, preexisting_ids, attempts=continuation_rounds
            ) if leg.pending_market_unknown_post else None
            if position is None and leg.pending_market_reference:
                confirmation = self._wait_market_submission(
                    leg.pending_market_reference,
                    rounds=1 if self.state.continuation_managed else 3,
                )
                if confirmation.get("dealStatus") == "REJECTED":
                    self._clear_pending_market(leg)
                    opposite = self.state.short if leg.direction == "BUY" else self.state.long
                    if (opposite and opposite.deal_id and self.state.scenario == 1):
                        close = self._wait_accepted_initial_close(opposite, attempts=20)
                        if close is not None:
                            self._finish_failed_initial_attempt(
                                opposite, close[0], close[1], opposite_sent=True
                            )
                            return True
                        positions = self._cycle_positions()
                        if opposite.deal_id in positions:
                            self._manual(
                                f"Вторая начальная заявка {leg.direction} REJECTED; первая "
                                f"позиция {opposite.deal_id} ещё открыта и требует контроля"
                            )
                            return True
                        self._set_pending_market(
                            opposite, "INITIAL", set(),
                            reason=f"opposite {leg.direction} rejected; first close pending",
                            unknown_post=False,
                        )
                        opposite.pending_market_reference = opposite.deal_reference
                        self.state.save(self.cfg.state_file)
                        return True
                    self._manual(
                        f"Отложенный начальный MARKET {leg.direction} подтверждён как REJECTED"
                    )
                    return True
                position = self._resolve_market_position(
                    confirmation, leg.pending_market_reference, leg.direction, preexisting_ids,
                    attempts=continuation_rounds,
                )
        except Exception as exc:
            leg.pending_market_reason = str(exc)
            self.state.save(self.cfg.state_file)
            LOG.warning("Pending initial MARKET reconciliation delayed: %s", exc)
            return True
        if position is None:
            return True
        leg.deal_id = str(position["dealId"])
        if position.get("size") is not None:
            leg.size = D(str(position["size"]))
            leg.size_confirmation = "positions"
        if position.get("level") is not None:
            leg.current_entry = leg.original_trigger_level = D(str(position["level"]))
            leg.entry_confirmation = "positions"
            leg.stop = stop_for(leg.direction, leg.current_entry, leg.stop_distance)
        current = self._cycle_positions()
        opposite = self.state.short if leg.direction == "BUY" else self.state.long
        both_submitted = set(self.state.initial_submitted_directions) == {"BUY", "SELL"}
        if (not both_submitted and opposite and opposite.deal_id
                and (self.state.scenario == 1 or self.state.continuation_managed)):
            # Migration from 0d9cc63: that version persisted both Leg objects/references but did
            # not yet persist the submitted-direction list.
            self.state.initial_submitted_directions = ["BUY", "SELL"]
            both_submitted = True
        if (both_submitted and opposite and opposite.deal_id and leg.deal_id
                and not opposite.pending_market_kind):
            # The hedge was formed even if one or both positions disappeared before list
            # synchronization. Hand the complete pair to the ordinary scenario-1 replay, which
            # accounts an opposite SL before TP and is idempotent by broker event key.
            self._clear_pending_market(leg)
            if self.state.continuation_managed:
                self._get_continuation().confirm_pair_fills()
            elif self.state.scenario == 1:
                self.strategy.confirm_initial_fills(
                    self.state.long.current_entry, self.state.short.current_entry
                )
            self.state.save(self.cfg.state_file)
            if self.state.continuation_managed:
                self.state.continuation_stage = "ACTIVE"
                self._get_continuation().handle_active_scenario()
            else:
                self._dispatch_owned_cycle()
            return True
        if leg.deal_id not in current:
            related_orders = [
                item for item in self.capital.working_orders()
                if self._order_epic(item) == self.cfg.epic
            ]
            if current or related_orders:
                leg.pending_market_reason = (
                    "linked initial close pending while other positions/orders are reconciled"
                )
                self.state.save(self.cfg.state_file)
                return True
            try:
                activity = self.capital.activity(leg.deal_id)
                if not activity:
                    activity = self.capital.activity()
                for source in ("SL", "TP"):
                    closed = find_close_event(activity, leg.deal_id, source)
                    if closed is not None and closed.level is not None:
                        self._clear_pending_market(leg)
                        self._finish_failed_initial_attempt(
                            leg, source, closed.level, opposite_sent=False
                        )
                        return True
            except Exception as exc:
                leg.pending_market_reason = str(exc)
                self.state.save(self.cfg.state_file)
                return True
            # A historical opening is not proof that the position is still open. Preserve the
            # submission lock until either /positions or a linked close event proves its outcome.
            leg.pending_market_reason = "historical opening found; linked close still pending"
            self.state.save(self.cfg.state_file)
            return True
        self._clear_pending_market(leg)
        self._manual(
            f"Начальная MARKET-позиция {leg.direction} найдена после неопределённого ответа; "
            f"dealId={leg.deal_id}. Противоположный вход автоматически не отправлен."
        )
        return True

    def _close_trigger_that_raced_with_tp(self, leg: Leg) -> Decimal | None:
        """Close an owned TP/cancel-race position and return its separate signed result."""
        trigger_id = leg.trigger_id
        for attempt in range(16):
            activity = self.capital.activity()
            if find_working_order_cancellation(activity, trigger_id) is not None:
                return D("0")
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
                actual_size = (D(str(position["size"]))
                               if position.get("size") is not None else None)
            elif opened is not None and opened.deal_id and opened.level is not None:
                deal_id, entry = opened.deal_id, opened.level
                actual_size = opened.size
                if actual_size is None and position is not None and position.get("size") is not None:
                    actual_size = D(str(position["size"]))
            else:
                executed = find_working_order_execution(activity, trigger_id)
                if executed is None:
                    if attempt + 1 < 16:
                        time.sleep(0.5)
                    continue
                if attempt + 1 < 16:
                    time.sleep(0.5)
                    continue
                return None

            if actual_size is None or actual_size <= 0:
                LOG.info("Trigger race size is not published yet: dealId=%s", deal_id)
                continue
            leg.deal_id = deal_id
            leg.current_entry = entry
            leg.size = actual_size
            leg.size_confirmation = "broker"
            leg.open = True
            self.state.remember_deal(leg, self.state.scenario + 1)
            race_record = next(item for item in self.state.deal_history
                               if item.get("deal_id") == deal_id)
            race_record["category"] = "TP_TRIGGER_RACE"
            race_record["open_working_order_id"] = trigger_id
            position = positions.get(deal_id, position)
            if position is not None:
                if leg.pending_race_close_unknown and not leg.pending_race_close_reference:
                    # DELETE may already have reached Capital.  Do not submit it again; wait for
                    # the position or activity to prove the outcome.
                    time.sleep(0.5)
                    continue
                reference = leg.pending_race_close_reference
                if not reference:
                    leg.pending_race_close_deal_id = deal_id
                    leg.pending_race_close_unknown = True
                    self.state.save(self.cfg.state_file)
                    try:
                        reference = self.capital.close_position(deal_id)
                    except CapitalError:
                        return None
                    leg.pending_race_close_reference = reference
                    leg.pending_race_close_unknown = False
                    self.state.save(self.cfg.state_file)
                result = self.capital.wait_confirmation(reference)
                close = self._confirmation_close_level(result, deal_id)
                if result.get("dealStatus") != "ACCEPTED" or close is None:
                    raise CapitalError(result.get("reason") or "Поздняя trigger-позиция не закрыта")
            else:
                close_event = (
                    find_close_event(activity, deal_id, "SL")
                    or find_close_event(activity, deal_id, "TP")
                )
                if close_event is None:
                    expected_direction = "SELL" if leg.direction == "BUY" else "BUY"
                    close_event = next((event for event in reversed(normalize_events(activity))
                        if event.deal_id == deal_id and event.source == "USER"
                        and event.event_type == "POSITION" and event.status == "ACCEPTED"
                        and event.direction == expected_direction and event.level is not None), None)
                if close_event is None or close_event.level is None:
                    time.sleep(0.5)
                    continue
                close = close_event.level
            signed = ((close - entry) if leg.direction == "BUY" else (entry - close)) * actual_size
            leg.open = False
            leg.pending_race_close_reference = leg.pending_race_close_deal_id = ""
            leg.pending_race_close_unknown = False
            self.state.remember_close(deal_id, "TP_TRIGGER_RACE", close,
                                      close_size=actual_size, category="TP_TRIGGER_RACE")
            result_key = f"race:{trigger_id}:{deal_id}:{close}"
            if not any(item.get("key") == result_key for item in self.state.trigger_race_results):
                self.state.trigger_race_results.append({
                    "key": result_key, "trigger_id": trigger_id, "deal_id": deal_id,
                    "direction": leg.direction, "entry": str(entry), "close": str(close),
                    "size": str(actual_size), "signed_result": str(signed),
                })
            self.state.save(self.cfg.state_file)
            self._send_report(
                "⚡ TP и trigger исполнились почти одновременно\n"
                f"Поздняя сторона: {leg.direction}\nDeal ID: {deal_id}\n"
                f"Trigger fill: {entry}\nФактическое закрытие: {close}\n"
                f"Отдельный signed result: {signed}\n"
                "Этот результат не изменяет GENERAL_RECOVERY и основной итог цикла."
            )
            return signed
        return None

    def _protection_matches(self, position: dict, leg: Leg) -> bool:
        """Return whether the broker snapshot contains the strategy's current SL and TP."""
        if str(position.get("dealId", "")) != leg.deal_id:
            return False
        stop = position.get("stopLevel")
        target = position.get("profitLevel")
        try:
            actual_stop = D(str(stop)) if stop is not None else None
            actual_target = D(str(target)) if target is not None else None
        except (ArithmeticError, TypeError, ValueError):
            return False
        matches = actual_stop == leg.stop and actual_target == leg.take_profit
        if matches and (
            leg.confirmed_stop != actual_stop
            or leg.confirmed_take_profit != actual_target
            or leg.protection_readback != "ПОДТВЕРЖДЕНО"
        ):
            leg.confirmed_stop, leg.confirmed_take_profit = actual_stop, actual_target
            leg.confirmed_stop_distance = abs(leg.current_entry - actual_stop)
            leg.protection_readback = "ПОДТВЕРЖДЕНО"
            # A GET proves broker state, not that an older/newer PUT confirmation belongs to it.
            if (leg.protection_sent_stop != leg.stop
                    or leg.protection_sent_take_profit != leg.take_profit):
                leg.protection_confirmation = leg.protection_confirmation or "неизвестно"
            self.state.save(self.cfg.state_file)
        return matches

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
            recovery_before = recovery_snapshot(self.state)
            previous_scenario = self.state.scenario
            trigger_level = leg.original_trigger_level
            executed_trigger_id = leg.trigger_id
            actual_size = D(str(candidate["size"])) if candidate.get("size") is not None else None
            execution_time = ""
            try:
                execution = find_working_order_execution(
                    self.capital.activity(), executed_trigger_id
                )
                if execution is not None:
                    execution_time = execution.timestamp.isoformat()
            except (CapitalError, TypeError):
                # The matching position is enough to continue the live path.  Durable ownership is
                # retained so a later close/startup read can obtain WORKING_ORDER execution time.
                LOG.info("Trigger execution history is not published yet: %s", executed_trigger_id)
            self.strategy.reopened(
                leg.direction, fill, str(candidate["dealId"]),
                f"reopen:{candidate['dealId']}", actual_size=actual_size,
                working_order_id=executed_trigger_id,
                broker_execution_time=execution_time,
            )
            if self.state.scenario == self.cfg.max_scenarios:
                self._enter_manual_nine()
            else:
                trigger_slip = abs(trigger_level - fill)
                self._send_report(
                    f"🔄 Trigger исполнен — переход в сценарий {self.state.scenario}\n"
                    f"{cycle_heading(self.state, 'Trigger исполнен и позиция подтверждена')}\n"
                    f"Переход подтверждён: сценарий {previous_scenario} → {self.state.scenario}.\n"
                    f"Источник: /positions связал dealId={candidate['dealId']} с "
                    f"workingOrderId Trigger={executed_trigger_id}.\n"
                    f"{leg.direction}: original trigger={trigger_level}; actual fill={fill}; "
                    f"slippage=|{trigger_level} − {fill}|={trigger_slip}.\n\n"
                    f"{recovery_change_text(self.state, recovery_before, event='пересчёт после Trigger fill', direction=leg.direction, trigger_slippage=trigger_slip)}\n\n"
                    "Расчётная защита ещё не называется установленной. Следующее действие: "
                    "PUT и read-back SL/TP обеих сторон; подтверждение придёт отдельными сообщениями."
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

    def _scenario_at_broker_close(self, leg: Leg, close_event, activity: list[dict]) -> int | None:
        """Resolve a late close against already-accounted reentries using broker UTC only."""
        opened_record = next(
            (item for item in reversed(self.state.deal_history)
             if item.get("deal_id") == leg.deal_id), None,
        )
        opened_scenario = int((opened_record or {}).get("scenario", self.state.scenario))
        if self.state.scenario <= opened_scenario:
            return self.state.scenario
        unknown = datetime.min.replace(tzinfo=timezone.utc)
        if close_event.timestamp == unknown:
            return None
        normalized = normalize_events(activity)
        transitions = [dict(item) for item in self.state.scenario_transitions
                       if int(item.get("scenario", 0) or 0) > opened_scenario
                       and int(item.get("scenario", 0) or 0) <= self.state.scenario
                       and int(item.get("cycle_id", 0) or 0) == self.state.cycle_id]
        known_scenarios = {int(item.get("scenario", 0) or 0) for item in transitions}
        # Migration path for states saved before scenario_transitions existed.  Missing ownership is
        # accepted only when the immutable attempt_id proves the active attempt; it is never
        # silently replaced with the current cycle identifiers.
        for record in self.state.deal_history:
            scenario = int(record.get("scenario", 0) or 0)
            if scenario <= opened_scenario or scenario > self.state.scenario \
                    or scenario in known_scenarios:
                continue
            owned = (record.get("cycle_id") == self.state.cycle_id
                     and record.get("cycle_attempt") == self.state.cycle_attempt)
            if not owned and record.get("cycle_id") is None:
                owned = int(record.get("attempt_id", 0) or 0) == self.state.active_attempt_id
            if not owned:
                continue
            transitions.append({
                "scenario": scenario, "deal_id": record.get("deal_id", ""),
                "direction": record.get("direction", ""),
                "working_order_id": record.get("open_working_order_id", ""),
                "broker_execution_time": record.get("broker_open_execution_time", ""),
                "time_source": record.get("broker_open_time_source", ""),
            })
        reentries: list[tuple[int, object]] = []
        for transition in sorted(transitions, key=lambda item: int(item["scenario"])):
            scenario = int(transition["scenario"])
            deal_id = str(transition.get("deal_id", ""))
            trigger_id = str(transition.get("working_order_id", ""))
            position_events = [event for event in normalized
                               if event.deal_id == deal_id and event.event_type == "POSITION"
                               and event.source == "USER" and event.status == "ACCEPTED"]
            executions = [event for event in normalized
                          if trigger_id and event.deal_id == trigger_id
                          and event.event_type == "WORKING_ORDER" and event.status == "EXECUTED"]
            if executions:
                opened_event = executions[-1]
            elif transition.get("broker_execution_time"):
                try:
                    stamp = datetime.fromisoformat(
                        str(transition["broker_execution_time"]).replace("Z", "+00:00")
                    )
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    opened_event = BrokerEvent(
                        f"saved-transition:{deal_id}", stamp, "USER", "EXECUTED",
                        "WORKING_ORDER", trigger_id, "", trigger_id,
                        str(transition.get("direction", "")), None, None, {},
                    )
                except ValueError:
                    return None
            elif len(position_events) == 1:
                opened_event = position_events[0]
            else:
                return None
            if opened_event.timestamp == unknown or opened_event.timestamp == close_event.timestamp:
                return None
            reentries.append((scenario, opened_event))
        if not reentries:
            return None
        same_time = [event for _, event in reentries if event.timestamp == close_event.timestamp]
        if same_time:
            return None
        later = [scenario for scenario, event in reentries if event.timestamp > close_event.timestamp]
        return min(later) - 1 if later else self.state.scenario

    def _apply_confirmed_stop_event(self, leg: Leg, close_event, activity: list[dict]) -> Leg | None:
        """Apply one exact SL using its deal's immutable opening scenario and broker chronology."""
        if close_event is None or close_event.level is None:
            return None
        scenario = self._scenario_at_broker_close(leg, close_event, activity)
        if scenario is None:
            self._diagnostic_decision_once(
                f"chronology:{leg.deal_id}:{close_event.event_id}",
                f"cycle={self.state.cycle_id}/{self.state.cycle_attempt}; dealId={leg.deal_id}; "
                f"open_scenario={next((r.get('scenario') for r in self.state.deal_history if r.get('deal_id') == leg.deal_id), '?')}; "
                f"local_scenario={self.state.scenario}; close_time={close_event.timestamp.isoformat()}; "
                "scenario_at_close unresolved: missing/conflicting owned transition execution time",
            )
            return None
        details = close_event.raw.get("details", {}) if isinstance(close_event.raw, dict) else {}
        effective_stop = details.get("stopLevel")
        evidence_status = self._remember_close_event(leg, close_event, scenario_at_close=scenario)
        if evidence_status == "CONFLICT":
            self._manual(
                f"Конфликт broker evidence для dealId={leg.deal_id}: сохранённые и новые "
                f"source/fill/size/time различаются; accounting не выполнен"
            )
            return None
        if effective_stop is not None:
            leg.confirmation_stop = leg.confirmed_stop = D(str(effective_stop))
            leg.confirmed_stop_distance = abs(leg.current_entry - leg.confirmed_stop)
            leg.protection_confirmation = "ACCEPTED"
        if close_event.size is None or close_event.size <= 0 or close_event.size > leg.size:
            return None
        fully_closed = close_event.size == leg.size
        return self.strategy.stopped(
            leg.direction, close_event.level,
            f"stop:{leg.deal_id}:{close_event.level}",
            scenario_at_close=scenario,
            broker_execution_time=close_event.timestamp.isoformat(),
            actual_closed_size=close_event.size, fully_closed=fully_closed,
        )

    def _remember_close_event(self, leg: Leg, event: BrokerEvent,
                              *, scenario_at_close: int | None = None) -> str:
        details = event.raw.get("details", {}) if isinstance(event.raw, dict) else {}
        broker_stop = details.get("stopLevel")
        broker_target = details.get("profitLevel")
        stop = D(str(broker_stop)) if broker_stop is not None else leg.confirmed_stop
        target = D(str(broker_target)) if broker_target is not None else leg.confirmed_take_profit
        diagnostic = protection_range_diagnostic(
            event, confirmed_stop=stop, confirmed_take_profit=target
        )
        self.state.remember_deal(leg)
        if event.size is not None and event.size < leg.size:
            record = next((item for item in self.state.deal_history
                           if item.get("deal_id") == leg.deal_id), None)
            if record is None:
                return "CONFLICT"
            partial = {
                "event_id": event.event_id, "source": event.source, "type": event.event_type,
                "status": event.status, "fill": str(event.level), "size": str(event.size),
                "execution_time": event.timestamp.isoformat(),
                "effective_stop": str(stop) if stop is not None else None,
                "effective_take_profit": str(target) if target is not None else None,
                "scenario_at_close": scenario_at_close,
            }
            partials = record.setdefault("partial_closes", [])
            existing = next((item for item in partials
                             if item.get("event_id") == event.event_id), None)
            if existing is not None:
                return "SAME" if existing == partial else "CONFLICT"
            if event.event_id.startswith("synthetic:"):
                semantic = {key: partial.get(key) for key in (
                    "source", "type", "status", "fill", "size", "execution_time",
                    "effective_stop", "effective_take_profit", "scenario_at_close",
                )}
                for item in partials:
                    if str(item.get("event_id", "")).startswith("synthetic:") and all(
                        (D(str(item.get(key))) == D(str(value))
                         if key in {"fill", "size", "effective_stop", "effective_take_profit"}
                         and item.get(key) is not None and value is not None
                         else item.get(key) == value)
                        for key, value in semantic.items()
                    ):
                        return "SAME"
            partials.append(partial)
            return "NEW_PARTIAL"
        return self.state.remember_close(
            leg.deal_id, event.source, event.level,
            close_event_id=event.event_id, close_event_type=event.event_type,
            close_event_status=event.status, close_execution_time=event.timestamp.isoformat(),
            close_size=event.size if event.size is not None else leg.size,
            effective_stop=stop, effective_take_profit=target,
            scenario_at_close=scenario_at_close,
            close_cycle_id=self.state.cycle_id, close_cycle_attempt=self.state.cycle_attempt,
            protection_range=diagnostic,
        )

    def _saved_close_event(self, leg: Leg, source: str) -> BrokerEvent | None:
        record = next((item for item in reversed(self.state.deal_history)
                       if item.get("deal_id") == leg.deal_id
                       and item.get("close_source") == source.upper()
                       and item.get("close_level") is not None), None)
        if record is None or record.get("close_evidence_conflict"):
            return None
        try:
            timestamp = datetime.fromisoformat(
                str(record.get("close_execution_time", "")).replace("Z", "+00:00")
            )
        except ValueError:
            timestamp = datetime.min.replace(tzinfo=timezone.utc)
        return BrokerEvent(
            str(record.get("close_event_id") or f"saved:{leg.deal_id}:{source}:{record['close_level']}"),
            timestamp, source.upper(), str(record.get("close_event_status", "ACCEPTED")),
            str(record.get("close_event_type", "POSITION")), leg.deal_id, "", "",
            leg.direction, D(str(record["close_level"])),
            D(str(record.get("close_size", leg.size))), {"saved": True, "details": {
                "stopLevel": record.get("effective_stop"),
                "profitLevel": record.get("effective_take_profit"),
                "size": record.get("close_size"),
            }},
        )

    def _diagnostic_decision_once(self, key: str, message: str) -> None:
        marker = f"decision:{key}:{hashlib.sha256(message.encode()).hexdigest()[:12]}"
        if marker in self.state.processed_events:
            return
        self.state.processed_events.append(marker)
        self.state.events.append(message)
        LOG.warning("BROKER DECISION %s", message)
        self.state.save(self.cfg.state_file)

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
        activity = self.capital.activity()
        survivor_sl = find_close_event(activity, survivor.deal_id, "SL")
        if survivor_sl is None or survivor_sl.level is None:
            return False
        opened = find_trigger_open_event(activity, stopped.trigger_id, stopped.direction)
        if opened is not None and opened.deal_id == str(candidate.get("dealId", "")):
            opened_time = opened.timestamp
        else:
            executed = find_working_order_execution(activity, stopped.trigger_id)
            if (executed is None
                    or str(candidate.get("workingOrderId", "")) != stopped.trigger_id
                    or str(candidate.get("direction", "")) != stopped.direction
                    or not str(candidate.get("dealId", ""))):
                LOG.info("Trigger execution activity is still synchronizing: %s", stopped.trigger_id)
                return False
            # WORKING_ORDER/EXECUTED is the broker execution clock. Position.createdDateUTC is a
            # later publication/creation field and is deliberately not used as a substitute.
            opened_time = executed.timestamp
        if (survivor_sl.timestamp == datetime.min.replace(tzinfo=timezone.utc)
                or opened_time == datetime.min.replace(tzinfo=timezone.utc)
                or survivor_sl.timestamp == opened_time):
            self._manual(
                "Broker chronology Trigger/reentry и survivor SL недостаточна; "
                "scenario_at_close не угадан"
            )
            return True
        stop_fill = survivor_sl.level
        reopened_fill = D(str(candidate["level"]))
        reopened_id = str(candidate["dealId"])
        previous_scenario = self.state.scenario
        close_scenario = (
            previous_scenario if survivor_sl.timestamp < opened_time
            else previous_scenario + 1
        )
        self.strategy.reopened(
            stopped.direction,
            reopened_fill,
            reopened_id,
            f"reopen:{reopened_id}",
            actual_size=(D(str(candidate["size"])) if candidate.get("size") is not None else None),
            working_order_id=stopped.trigger_id,
        )
        stopped.deal_reference = str(candidate.get("dealReference") or stopped.deal_reference)
        self.strategy.stopped(
            survivor.direction,
            stop_fill,
            f"stop:{survivor.deal_id}:{stop_fill}",
            scenario_at_close=close_scenario,
            broker_execution_time=survivor_sl.timestamp.isoformat(),
        )
        if self.state.scenario == self.cfg.max_scenarios:
            self._enter_manual_nine()
            return True
        if not self._apply_protection(stopped):
            return True
        self._create_trigger(survivor)
        self.state.save(self.cfg.state_file)
        self._send_report(
            "🔁 Восстановлена быстрая последовательность событий\n"
            f"Trigger {stopped.direction}: {stopped.original_trigger_level}\n"
            f"Фактический вход: {reopened_fill}\n"
            f"SL {survivor.direction}: {stop_fill}\n"
            f"Сценарий: {self.state.scenario}\n"
            f"GENERAL_RECOVERY={self.state.general_recovery}; recovery_distance оставшейся "
            f"{stopped.direction}={self.strategy.recovery_distance_for(stopped)}\n"
            f"Осталась сторона: {stopped.direction}\n"
            f"Новый TP: {stopped.take_profit}\n"
            f"Новый trigger {survivor.direction}: {survivor.original_trigger_level}\n"
            f"{self._leg_details(stopped)}"
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
        if survivor_tp is not None and survivor_tp.level is not None:
            # The API already gives us everything required to resolve this race. Ask positions
            # and durable activity for the trigger-created deal; close it at market if it is
            # still open, or use its recorded SL/TP fill if it completed between polls.
            race_loss = self._close_trigger_that_raced_with_tp(stopped)
            if race_loss is None:
                LOG.info(
                    "Survivor TP is confirmed but trigger result is still indexing: "
                    "workingOrderId=%s; continuing reconciliation",
                    stopped.trigger_id,
                )
                self.state.save(self.cfg.state_file)
                return True
            stopped.trigger_id = stopped.trigger_reference = ""
            self._complete_cycle(survivor.direction, survivor_tp.level)
            self.state.armed = not self.state.paused
            self.state.phase = "FILTER" if self.state.armed else "PAUSED"
            self.state.save(self.cfg.state_file)
            suffix = (
                "Перехожу к фильтру следующего цикла."
                if self.state.armed else "Следующий цикл ожидает /start."
            )
            self._send_report(
                "✅ TP surviving-позиции и trigger-fill проверены через Capital.com API\n"
                f"TP {survivor.direction}: {survivor_tp.level}\n"
                f"Trigger {stopped.direction}: {opened.level}\n"
                f"Отдельный signed result trigger-позиции: {race_loss}\n{suffix}\n"
                + cycle_result_text(
                    self.state, survivor.direction, survivor_tp.level, self.cfg.size
                )
            )
            return True
        if survivor_sl is None or survivor_sl.level is None:
            return False

        unknown_time = datetime.min.replace(tzinfo=timezone.utc)
        if (survivor_sl.timestamp == unknown_time or opened.timestamp == unknown_time
                or survivor_sl.timestamp == opened.timestamp):
            self._manual(
                "Broker chronology Trigger/reentry и survivor SL неоднозначна; "
                "scenario_at_close не угадан"
            )
            return True
        reopen_key = f"reopen:{opened.deal_id}"
        if reopen_key not in self.state.processed_events:
            self.strategy.reopened(
                stopped.direction, opened.level, opened.deal_id, reopen_key,
                actual_size=opened.size, working_order_id=stopped.trigger_id,
            )
            stopped.deal_reference = opened.deal_reference or stopped.deal_reference
        survivor_stop_key = f"stop:{survivor.deal_id}:{survivor_sl.level}"
        if survivor_stop_key not in self.state.processed_events:
            close_scenario = (
                self.state.scenario - 1
                if survivor_sl.timestamp < opened.timestamp else self.state.scenario
            )
            self.strategy.stopped(
                survivor.direction, survivor_sl.level, survivor_stop_key,
                scenario_at_close=close_scenario,
                broker_execution_time=survivor_sl.timestamp.isoformat(),
            )

        if reopened_tp is not None and reopened_tp.level is not None:
            self._complete_cycle(stopped.direction, reopened_tp.level)
            self.state.armed = not self.state.paused
            self.state.phase = "FILTER" if self.state.armed else "PAUSED"
            self.state.save(self.cfg.state_file)
            self._send_report(
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
            self._send_report(
                "🔁 Trigger-позиция открылась и закрылась по SL между опросами\n"
                f"Вход: {opened.level}\nSL: {reopened_sl.level}\n"
                f"Сценарий: {self.state.scenario}\n"
                f"GENERAL_RECOVERY={self.state.general_recovery}; recovery_distance survivor "
                f"{survivor.direction}={self.strategy.recovery_distance_for(survivor)}\n"
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
            if (leg and not leg.open and not leg.trigger_id
                    and not leg.pending_trigger_action and not leg.pending_trigger_cancel_unknown
                    and not leg.trigger_recreation_suppressed):
                self._create_trigger(leg)

    def _create_trigger(self, leg: Leg) -> None:
        if any(item and item.pending_trigger_cancel_unknown
               for item in (self.state.long, self.state.short)):
            LOG.info("Trigger mutation deferred while a manual trigger mutation is unresolved")
            return
        last_error = "trigger отклонён"
        projected_size, projected_distance, projected_recovery = self.strategy.projected_reopen(
            leg.direction
        )
        projected_stop = stop_for(leg.direction, leg.original_trigger_level, projected_distance)
        projected_target = target_for(
            leg.direction, leg.original_trigger_level,
            projected_distance, projected_recovery,
        )
        if leg.pending_market_kind == "FALLBACK":
            self._open_passed_trigger_at_market(
                leg, projected_target, leg.pending_market_reason or "ожидание confirmation"
            )
            return
        for _ in range(self.execution_policy.attempts):
            existing = self._find_order(leg)
            if existing:
                leg.trigger_id = str(existing["dealId"])
                self._link_pending_closure_trigger(leg)
                if leg.trigger_id not in self.state.cycle_trigger_ids:
                    self.state.cycle_trigger_ids.append(leg.trigger_id)
                return
            try:
                reference = self.capital.working_stop(
                    self.cfg.epic, leg.direction, projected_size, leg.original_trigger_level,
                    projected_stop, projected_target
                )
                result = self.capital.wait_confirmation(reference)
                if result.get("dealStatus") == "ACCEPTED" and result.get("dealId"):
                    leg.trigger_reference = reference
                    leg.trigger_id = str(result["dealId"])
                    self._link_pending_closure_trigger(leg)
                    if leg.trigger_id not in self.state.cycle_trigger_ids:
                        self.state.cycle_trigger_ids.append(leg.trigger_id)
                    self._send_report(
                        f"📌 {cycle_heading(self.state, 'Trigger создан и подтверждён брокером', next_scenario=self.state.scenario + 1)}\n"
                        f"Сторона={leg.direction}; orderId={leg.trigger_id}; объём будущей позиции="
                        f"{projected_size}; сохранённый уровень={leg.original_trigger_level}.\n"
                        f"Предварительный Recovery без будущего slippage={projected_recovery}.\n"
                        f"Предварительная защита рабочего ордера: SL={projected_stop}; "
                        f"TP={projected_target}; SL distance={projected_distance}.\n"
                        "Подтверждено только создание working STOP. Позиция ещё НЕ открыта; "
                        "actual fill, Trigger slippage, итоговый Recovery и точная защита будут "
                        "уточнены только после исполнения.\n"
                        f"Текущее состояние другой стороны:\n"
                        f"{self._leg_details(self.state.short if leg.direction == 'BUY' else self.state.long)}"
                    )
                    return
                last_error = result.get("reason") or last_error
                if self._trigger_level_passed(leg) and self._is_crossed_level_rejection(last_error):
                    self._open_passed_trigger_at_market(leg, projected_target, last_error)
                    return
            except Exception as exc:
                last_error = str(exc)
                if self._trigger_level_passed(leg) and self._is_crossed_level_rejection(last_error):
                    self._open_passed_trigger_at_market(leg, projected_target, last_error)
                    return
        self._manual(f"Trigger {leg.direction} не создан после 4 попыток: {last_error}")

    def _link_pending_closure_trigger(self, leg: Leg) -> None:
        """Attach broker order ownership to its one unresolved closure."""
        matches = [
            item for item in self.state.pending_recovery
            if item.get("deal_id") == leg.deal_id
            and item.get("direction") == leg.direction
            and not item.get("reentry_accounted", bool(item.get("reopen_event_id")))
        ]
        if len(matches) != 1:
            if len(matches) > 1:
                raise RuntimeError(
                    f"Ambiguous closure ownership for trigger {leg.trigger_id}"
                )
            return
        matches[0]["trigger_id"] = leg.trigger_id
        matches[0]["trigger_reference"] = leg.trigger_reference
        self.state.save(self.cfg.state_file)

    def _trigger_level_passed(self, leg: Leg) -> bool:
        bid, ask = self.capital.quote(self.cfg.epic)
        return trigger_level_passed(leg.direction, leg.original_trigger_level, bid, ask)

    @staticmethod
    def _is_crossed_level_rejection(reason: str) -> bool:
        return is_crossed_level_rejection(reason)

    def _open_passed_trigger_at_market(
        self, leg: Leg, projected_target: D, rejection_reason: str = "trigger level crossed"
    ) -> None:
        """Reopen immediately when Capital rejects a STOP whose level is already crossed."""
        last_error = "MARKET fallback отклонён"
        accepted: dict | None = None
        reference = ""
        preexisting_ids = (
            set(leg.pending_market_preexisting_ids)
            if leg.pending_market_kind else set(self._cycle_positions())
        )
        resolved_position: dict | None = None
        for _ in range(self.execution_policy.attempts):
            if leg.pending_market_unknown_post and not leg.pending_market_reference:
                try:
                    resolved_position = self._resolve_unknown_market_position(
                        leg.direction, preexisting_ids, attempts=20
                    )
                except Exception as exc:
                    leg.pending_market_reason = str(exc)
                    self.state.save(self.cfg.state_file)
                    LOG.warning("Unknown MARKET fallback reconciliation delayed: %s", exc)
                    return
                if resolved_position is None:
                    return
                accepted = {
                    "dealStatus": "ACCEPTED",
                    "dealId": str(resolved_position["dealId"]),
                    "affectedDeals": [{
                        "dealId": str(resolved_position["dealId"]), "status": "OPENED",
                    }],
                    "level": resolved_position.get("level"),
                }
                break
            if leg.pending_market_reference:
                reference = leg.pending_market_reference
            else:
                self._set_pending_market(
                    leg, "FALLBACK", preexisting_ids, reason=rejection_reason,
                    unknown_post=True,
                )
                try:
                    projected_size, projected_distance, _ = self.strategy.projected_reopen(
                        leg.direction
                    )
                    projected_stop = stop_for(
                        leg.direction, leg.original_trigger_level, projected_distance
                    )
                    reference = self.capital.open_position(
                        self.cfg.epic, leg.direction, projected_size,
                        projected_stop, projected_target,
                    )
                    leg.pending_market_reference = reference
                    leg.pending_market_unknown_post = False
                    self.state.save(self.cfg.state_file)
                except Exception as exc:
                    try:
                        resolved_position = self._resolve_unknown_market_position(
                            leg.direction, preexisting_ids, attempts=20
                        )
                    except Exception as reconcile_exc:
                        leg.pending_market_reason = str(reconcile_exc)
                        self.state.save(self.cfg.state_file)
                        LOG.warning(
                            "MARKET POST and follow-up position reconciliation are unavailable: %s",
                            reconcile_exc,
                        )
                        return
                    if resolved_position is None:
                        leg.pending_market_reason = str(exc)
                        self.state.save(self.cfg.state_file)
                        return
                    accepted = {
                        "dealStatus": "ACCEPTED",
                        "dealId": str(resolved_position["dealId"]),
                        "affectedDeals": [{
                            "dealId": str(resolved_position["dealId"]), "status": "OPENED",
                        }],
                        "level": resolved_position.get("level"),
                    }
                    break
            try:
                result = self._wait_market_submission(reference, rounds=3)
            except Exception as exc:
                self._send_report(
                    "⏳ MARKET fallback ожидает окончательный confirmation\n"
                    f"Сторона: {leg.direction}\nReference: {reference}\n"
                    "Повторный MARKET POST заблокирован; следующий tick продолжит только сверку.\n"
                    f"Последняя ошибка: {exc}"
                )
                return
            if result.get("dealStatus") != "ACCEPTED":
                last_error = result.get("reason") or last_error
                self._clear_pending_market(leg)
                continue
            accepted = result
            break
        if accepted is None:
            raise CapitalError(last_error)
        confirmation_id = str(accepted.get("dealId", ""))
        expected_id = self._confirmed_position_id(accepted)
        self._send_report(
            "⚠️ Trigger отклонён — MARKET fallback принят брокером\n"
            f"Причина STOP-отказа: {rejection_reason}\nСторона: {leg.direction}\n"
            f"Следующий сценарий: {self.state.scenario + 1}\n"
            f"Сохранённый trigger: {leg.original_trigger_level}\n"
            f"Confirmation dealId: {confirmation_id}\n"
            f"affectedDeals position ID: {expected_id or '-'}\n"
            "Ожидаю появление и проверку фактической позиции."
        )
        if resolved_position is None:
            try:
                position = self._resolve_market_position(
                    accepted, reference, leg.direction, preexisting_ids
                )
            except CapitalError as exc:
                # ACCEPTED without a level is still an accepted order. Keep its reference durable
                # and continue resolving it on later ticks instead of submitting another MARKET.
                self._send_report(
                    "⏳ MARKET принят, фактическая позиция/цена ещё синхронизируется\n"
                    f"Сторона: {leg.direction}\nReference: {reference}\n"
                    f"Confirmation dealId: {confirmation_id or '-'}\nОшибка сверки: {exc}"
                )
                return
        else:
            position = resolved_position
        actual_id = str(position["dealId"])
        fill_value = position.get("level", accepted.get("level"))
        if fill_value is None:
            raise CapitalError("MARKET fallback position has no actual fill level")
        fill = D(str(fill_value))
        recovery_before = recovery_snapshot(self.state)
        previous_scenario = self.state.scenario
        trigger_level = leg.original_trigger_level
        self.strategy.reopened(
            leg.direction, fill, actual_id, f"reopen:{actual_id}",
            actual_size=(D(str(position["size"])) if position.get("size") is not None else None),
        )
        self._clear_pending_market(leg)
        leg.deal_reference = reference
        slippage = abs(leg.original_trigger_level - fill)
        self._send_report(
            f"✅ {cycle_heading(self.state, 'MARKET fallback подтверждён')}\n"
            f"Причина: Trigger STOP был отклонён/пройден; повторный MARKET POST не выполнялся "
            f"после сохранения reference. Сценарий {previous_scenario} → {self.state.scenario}.\n"
            f"{leg.direction}: original trigger={trigger_level}; actual MARKET fill={fill}; "
            f"Фактический Deal ID: {actual_id}; "
            f"slippage=|{trigger_level} − {fill}|={slippage}.\n\n"
            f"{recovery_change_text(self.state, recovery_before, event='пересчёт MARKET-переоткрытия', direction=leg.direction, trigger_slippage=slippage)}\n\n"
            "Фактическая позиция подтверждена; рассчитанные SL/TP ещё требуют PUT и read-back."
        )
        # Re-read both positions before any PUT. A close can be indexed while MARKET is being
        # confirmed; never delay protection of the new leg by updating a confirmed-closed one.
        positions = self._cycle_positions()
        opposite = self.state.short if leg.direction == "BUY" else self.state.long
        if opposite and opposite.deal_id not in positions:
            try:
                if self._resolve_opposite_tp_after_market(leg):
                    return
            except TypeError:
                # Test doubles and temporarily malformed history are not closure evidence.
                LOG.warning("Opposite TP history is not iterable; reconciliation deferred")
            try:
                opposite_sl = self._closing_fill(opposite, "SL")
            except (CapitalError, TypeError):
                opposite_sl = None
            if opposite_sl is not None:
                self._dispatch_owned_cycle()
                return
        if actual_id not in positions:
            try:
                reopened_closed = (
                    self._closing_fill(leg, "SL") is not None
                    or self._closing_fill(leg, "TP") is not None
                )
            except (CapitalError, TypeError):
                reopened_closed = False
            if reopened_closed:
                self._dispatch_owned_cycle()
                return
        try:
            if self.state.scenario == self.cfg.max_scenarios:
                self._enter_manual_nine()
            else:
                reopened_ok = self._apply_protection(leg)
                opposite_ok = (
                    self._apply_protection(opposite)
                    if opposite and opposite.deal_id in positions else False
                )
                long_ok = reopened_ok if leg.direction == "BUY" else opposite_ok
                short_ok = reopened_ok if leg.direction == "SELL" else opposite_ok
        except Exception as exc:
            self._manual(f"MARKET trigger исполнен, но защита не подтверждена: {exc}")
            return
        self.state.save(self.cfg.state_file)
        if not self.state.active:
            return
        actual = positions.get(actual_id)
        opposite = self.state.short if leg.direction == "BUY" else self.state.long
        self._send_report(
            "🔎 Сверка защиты после MARKET-переоткрытия\n"
            f"Сторона: {leg.direction}; Deal ID: {actual_id}\n"
            f"Расчётные SL/TP: {leg.stop} / {leg.take_profit}\n"
            f"Брокерские SL/TP: "
            f"{actual.get('stopLevel') if actual else '-'} / "
            f"{actual.get('profitLevel') if actual else '-'}\n"
            f"Защита BUY/SELL применена: {long_ok}/{short_ok}\n"
            f"Противоположная сторона: {opposite.direction if opposite else '-'}; "
            f"позиция видна: {bool(opposite and opposite.deal_id in positions)}\n"
            "Исчезнувшая сторона будет учтена только после подтверждения SL/TP из history."
        )
        # Process an opposite SL that happened while the MARKET position was being resolved and
        # protected; event keys in Strategy keep this immediate reconciliation idempotent.
        if opposite and opposite.open and opposite.deal_id and opposite.deal_id not in positions:
            self._dispatch_owned_cycle()

    def _resolve_trigger_for_double_stop(self, trigger_leg: Leg, positions: dict[str, dict]) -> bool:
        """Cancel a pending trigger with positive evidence, or resume an executed trigger."""
        if not trigger_leg.trigger_id:
            return True
        self._detect_trigger_fill(positions)
        if trigger_leg.open:
            return False
        order_id = trigger_leg.trigger_id
        self.capital.working_orders()  # keep the snapshot in diagnostics before mutation
        try:
            cancelled = self.capital.delete_working_order(order_id)
        except CapitalError as exc:
            self.state.phase = "DOUBLE_SL_RECONCILING"
            self.state.continuation_managed = True
            self.state.continuation_stage = "RECONCILING"
            self.state.save(self.cfg.state_file)
            self._send_report(
                f"⏳ Отмена trigger {order_id} пока не подтверждена: {exc}. "
                "Новый вход заблокирован; сверка продолжится."
            )
            return False
        if cancelled:
            trigger_leg.trigger_id = trigger_leg.trigger_reference = ""
            return True
        activity = self.capital.activity()
        executed = find_working_order_execution(activity, order_id)
        opened = find_trigger_open_event(activity, order_id, trigger_leg.direction)
        if executed or opened:
            self._send_report(
                f"⏳ Trigger {order_id} исполнился при сверке двух SL; "
                "ожидаю фактическую позицию и продолжаю текущий сценарий."
            )
            return False
        if find_working_order_cancellation(activity, order_id):
            trigger_leg.trigger_id = trigger_leg.trigger_reference = ""
            return True
        self.state.phase = "DOUBLE_SL_RECONCILING"
        self.state.continuation_managed = True
        self.state.continuation_stage = "RECONCILING"
        self.state.save(self.cfg.state_file)
        self._send_report(
            f"⏳ Результат trigger {order_id} пока неизвестен. Новый вход и /start "
            "заблокированы до подтверждения исполнения либо отмены."
        )
        return False

    def _begin_double_sl_pause(self, closes: list[tuple[Leg, Decimal]]) -> None:
        """Account flat stops and start a durable non-blocking continuation pause."""
        activity = self.capital.activity()
        attempt_id = self.state.active_attempt_id or self.state.diagnostic_cycle_number
        details = []
        for leg, fill in closes:
            if leg.open:
                event = self._close_event_any_index(leg, "SL", activity)
                if event is None or event.level != fill \
                        or self._apply_confirmed_stop_event(leg, event, activity) is None:
                    self._manual(
                        f"Double-SL chronology недостаточна для {leg.deal_id}"
                    )
                    return
            points = fill - leg.current_entry if leg.direction == "BUY" else leg.current_entry - fill
            size = leg.size if leg.size else self.cfg.size
            details.append((leg, fill, points * size))
        close_money = sum((item[2] for item in details), D("0"))
        money = -(
            self.state.realized_loss_money - self.state.cycle_attempt_start_loss_money
        )
        self.state.remember_attempt(
            "DOUBLE_SL_CONTINUATION", money, scenario=self.state.scenario,
            closes=[{"direction": leg.direction, "deal_id": leg.deal_id,
                     "fill": str(fill), "result": str(result)}
                    for leg, fill, result in details],
            completed_cycle=None,
        )
        pending_d_added = self.strategy.account_double_sl_pending()
        self.state.active = True
        self.state.armed = False
        self.state.paused = True
        self.state.manual = False
        self.state.phase = "DOUBLE_SL_PAUSE"
        self.state.continuation_managed = True
        self.state.continuation_stage = "PAUSE"
        self.state.continuation_pause_until = time.time() + 300
        # A /stop issued at any earlier continuation stage remains authoritative.
        self.state.active_attempt_id = 0
        self.state.save(self.cfg.state_file)
        lines = "\n".join(
            f"{leg.direction}: вход {leg.current_entry}, установленный SL {leg.stop}, "
            f"SL fill {fill}, результат закрытия {result}"
            for leg, fill, result in details
        )
        self._send_report(
            f"⏸ Цикл №{self.state.cycle_id}; сценарий {self.state.scenario}; "
            f"попытка {self.state.cycle_attempt}\n{lines}\n"
            f"Последние закрытия: {close_money}; результат попытки: {money}; "
            f"накопленные убытки цикла: {self.state.realized_loss_money}\n"
            f"GENERAL_RECOVERY: {self.state.general_recovery}; перенос pending D после "
            f"подтверждённого flat: +{pending_d_added}\n"
            "Trigger: отменён или отсутствует; связанных позиций и ордеров нет.\n"
            f"Пауза до {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.state.continuation_pause_until))}. "
            "Затем бот перейдёт к обычному входному фильтру этого же цикла."
        )

    def _tick_continuation_pause(self) -> None:
        if time.time() < self.state.continuation_pause_until:
            return
        if self.state.continuation_stopped_by_user:
            return
        if any(leg and leg.pending_market_kind for leg in (self.state.long, self.state.short)):
            return
        positions = self._cycle_positions()
        orders = [item for item in self.capital.working_orders()
                  if self._order_epic(item) == self.cfg.epic]
        if positions or orders:
            self.state.phase = "DOUBLE_SL_RECONCILING"
            self.state.save(self.cfg.state_file)
            return
        self.state.paused = False
        self.state.armed = True
        self.state.waiting_current_candle = False
        self.state.phase = "CONTINUATION_FILTER"
        self.state.continuation_stage = "FILTER"
        self.state.save(self.cfg.state_file)
        self._send_report(
            f"🔎 Продолжение цикла №{self.state.cycle_id}: сценарий {self.state.scenario}, "
            f"следующая попытка {self.state.cycle_attempt + 1}. Пятиминутная пауза завершена; "
            "ожидаю обычный входной фильтр."
        )

    def _tick_double_sl_reconciling(self) -> None:
        positions = self._cycle_positions()
        self._detect_trigger_fill(positions)
        if self.state.phase != "DOUBLE_SL_RECONCILING":
            return
        open_legs = [leg for leg in (self.state.long, self.state.short) if leg and leg.open]
        closes = []
        for leg in open_legs:
            if leg.deal_id in positions:
                return
            fill = self._closing_fill(leg, "SL")
            if fill is None:
                return
            closes.append((leg, fill))
        trigger_leg = next(
            (leg for leg in (self.state.long, self.state.short)
             if leg and not leg.open and leg.trigger_id), None
        )
        if trigger_leg and not self._resolve_trigger_for_double_stop(trigger_leg, positions):
            return
        self._begin_double_sl_pause(closes)

    def _resolve_opposite_tp_after_market(self, reopened: Leg) -> bool:
        """Close a resolved MARKET leg if the former survivor already completed by TP."""
        opposite = self.state.short if reopened.direction == "BUY" else self.state.long
        if not opposite or not opposite.deal_id:
            return False
        positions = self._cycle_positions()
        if opposite.deal_id in positions:
            return False
        tp_fill = self._closing_fill(opposite, "TP")
        if tp_fill is None:
            return False
        current = positions.get(reopened.deal_id)
        if current is None:
            # It may already have closed between snapshots; use the same durable race resolver on
            # the next tick rather than declaring the cycle flat from absence.
            LOG.info(
                "Opposite TP confirmed while resolved MARKET leg is not visible yet: %s",
                reopened.deal_id,
            )
            return False
        reference = self.capital.close_position(reopened.deal_id)
        result = self.capital.wait_confirmation(reference)
        close = self._confirmation_close_level(result, reopened.deal_id)
        if result.get("dealStatus") != "ACCEPTED" or close is None:
            raise CapitalError(
                result.get("reason") or "MARKET-позиция после TP противоположной стороны не закрыта"
            )
        loss = max(D("0"), reopened.current_entry - close) if reopened.direction == "BUY" else max(
            D("0"), close - reopened.current_entry
        )
        self.state.realized_losses += loss
        self.state.realized_loss_money += loss * reopened.size
        reopened.open = False
        self.state.remember_close(reopened.deal_id, "TP_MARKET_RACE", close)
        self._complete_cycle(opposite.direction, tp_fill)
        self.state.armed = not self.state.paused
        self.state.phase = "FILTER" if self.state.armed else "PAUSED"
        self.state.save(self.cfg.state_file)
        self._send_report(
            "⚡ TP противоположной стороны исполнен во время pending MARKET\n"
            f"TP {opposite.direction}: {tp_fill}\n"
            f"Связанная MARKET-позиция {reopened.direction} закрыта: {close}\n"
            f"Дополнительный убыток: {loss}\n"
            + cycle_result_text(self.state, opposite.direction, tp_fill, self.cfg.size)
        )
        return True

    def _resolve_unknown_market_position(
        self, direction: str, preexisting_ids: set[str], *, attempts: int = 20,
    ) -> dict | None:
        """Look for one newly appeared position after a POST response was lost."""
        for attempt in range(attempts):
            positions = self._cycle_positions()
            candidates = [
                position for deal_id, position in positions.items()
                if deal_id not in preexisting_ids and position.get("direction") == direction
            ]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise CapitalError(
                    f"После неизвестного MARKET POST найдено несколько новых {direction} позиций"
                )
            if attempt + 1 < attempts:
                time.sleep(0.5)
        return None

    def _resolve_market_position(
        self, confirmation: dict, reference: str, direction: str,
        preexisting_ids: set[str], *, attempts: int = 20,
    ) -> dict:
        """Resolve Capital's actual position ID, including the affectedDeals/workingOrder split."""
        confirmation_id = str(confirmation.get("dealId", ""))
        expected_id = self._confirmed_position_id(confirmation)
        try:
            return self.capital.wait_position(
                expected_id, reference, direction, excluded_ids=preexisting_ids,
                epic=self.cfg.epic, attempts=attempts,
            )
        except CapitalError as position_error:
            # The position can close before /positions publishes it.  The global activity entry
            # links the real position to Capital's execution ID through workingOrderId.
            for attempt in range(attempts):
                try:
                    activity = self.capital.activity()
                    opened = find_trigger_open_event(activity, confirmation_id, direction)
                    if opened and opened.deal_id and opened.level is not None:
                        return {
                            "dealId": opened.deal_id,
                            "dealReference": opened.deal_reference,
                            "workingOrderId": confirmation_id,
                            "direction": direction,
                            "level": opened.level,
                        }
                except CapitalError:
                    LOG.warning("MARKET position activity is not available yet", exc_info=True)
                if attempt + 1 < attempts:
                    time.sleep(0.5)
            raise CapitalError(
                "Принятый MARKET fallback не связан с фактической позицией: "
                f"confirmation dealId={confirmation_id}, affected dealId={expected_id}: "
                f"{position_error}"
            ) from position_error

    def reconcile_startup(self) -> None:
        positions = self._cycle_positions()
        if self.state.active and not positions:
            # Immediately after creates/updates Capital.com can briefly return an empty list.
            # Never discard or manualize an active local cycle from a single such snapshot.
            positions = self._retry_missing_positions()
        orders = self.capital.working_orders()
        if self.state.active and any(
            leg and leg.pending_market_kind for leg in (self.state.long, self.state.short)
        ):
            # Pending submissions have their own correlation rules and must be resumed before the
            # ordinary unknown-position startup checks can misclassify their new position.
            self.reconciled = True
            self.state.save(self.cfg.state_file)
            self._send_report("⏳ Восстановлена незавершённая MARKET-сверка; новые заявки запрещены.")
            return
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
            if (self.state.active and not self.state.manual
                    and self.state.scenario < self.cfg.max_scenarios
                    and self.state.long and self.state.short):
                # Migration of active state saved with the former opposite-stop TP formula.
                self.strategy.refresh_targets()
            self.reconciled = True
            self.state.save(self.cfg.state_file)
            self._send_report(f"✅ Состояние автоматически восстановлено. {self.status()}")
        except Exception as exc:
            self._manual(f"Невозможно однозначно восстановить цикл: {exc}")
            self.reconciled = True

    def _recover_active_cycle(self, positions: dict[str, dict], orders: list[dict]) -> None:
        """Replay unambiguous stop/TP/trigger events that happened while the bot was offline."""
        self._migrate_recovery_model()
        if self.state.pending_tp_direction and self.state.pending_tp_fill is not None:
            self._finish_reached_take_profit()
            return
        gold_orders = {
            str(data.get("dealId")): data
            for item in orders
            if self._order_epic(item) == self.cfg.epic
            for data in [self._order_data(item)]
            if data.get("dealId")
        }
        self._migrate_legacy_legs(positions)
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
                    activity = self.capital.activity()
                    close_event = self._close_event_any_index(lost, "SL", activity)
                    if close_event is None or close_event.level is None:
                        raise RuntimeError(f"не найдена цена закрытия {lost.deal_id}")
                    if self._apply_confirmed_stop_event(lost, close_event, activity) is None:
                        raise RuntimeError(
                            f"broker chronology закрытия {lost.deal_id} недостаточна"
                        )
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

    def _migrate_legacy_legs(self, positions: dict[str, dict]) -> None:
        """Recover only position-specific legacy values that broker evidence proves."""
        unresolved = []
        for leg in (self.state.long, self.state.short):
            if leg is None or not leg.legacy_missing_fields:
                continue
            missing = set(leg.legacy_missing_fields)
            remote = positions.get(leg.deal_id) if leg.open else None
            if "size" in missing and remote and remote.get("size") is not None:
                leg.size = D(str(remote["size"])); missing.remove("size")
            if "stop_distance" in missing and remote and remote.get("stopLevel") is not None:
                try:
                    leg.stop_distance = abs(leg.current_entry - D(str(remote["stopLevel"])))
                    missing.remove("stop_distance")
                except (ArithmeticError, TypeError, ValueError):
                    pass
            # Recovery and temporary components are strategy history, not broker position fields.
            # A numeric zero saved explicitly was never added to legacy_missing_fields.
            leg.legacy_missing_fields = sorted(missing)
            if missing:
                unresolved.append(f"{leg.direction} dealId={leg.deal_id}: {sorted(missing)}")
        self.state.save(self.cfg.state_file)
        if unresolved:
            raise RuntimeError(
                "старое состояние не содержит однозначных параметров позиции; "
                "автоматика заблокирована: " + "; ".join(unresolved)
            )

    def _migrate_recovery_model(self) -> None:
        """Refuse an ambiguous active per-leg Recovery conversion instead of guessing money."""
        if self.state.recovery_model_version >= self.strategy.MODEL_VERSION:
            return
        if not self.state.active:
            self.state.recovery_model_version = self.strategy.MODEL_VERSION
            self.state.general_recovery = self.state.target_value = D("0")
            self.state.initial_position_size = D("0")
            self.state.recovery_migration_error = ""
            self.state.save(self.cfg.state_file)
            return
        self.state.recovery_migration_error = (
            "Активное состояние использует прежнюю Recovery-модель. В нём нет "
            "однозначных денежных снимков spread/target/pending D/slippage; автоматический "
            "пересчёт запрещён. Исходные значения сохранены."
        )
        self.state.manual = True
        self.state.paused = True
        self.state.save(self.cfg.state_file)
        raise RuntimeError(self.state.recovery_migration_error)

    def _enter_manual_nine(self) -> None:
        """Automatically flatten scenario 9 using actual fills from both sides.

        The historical method name is retained to keep all transition call sites small. Scenario
        9 is no longer manual: protection is removed and both legs are closed as concurrently as
        the REST API permits.
        """
        if not self.state.active:
            LOG.info(
                "Scenario 9 completion already finalized; duplicate entry ignored "
                "completed_cycles=%s attempt=%s",
                self.state.completed_cycles, self.state.active_attempt_id,
            )
            return
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

        # Establish one fresh session before concurrent DELETEs. CapitalClient.login is also
        # serialized as a second line of defence against any future parallel request path.
        self.capital.login()

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
                expected_leg = next(leg for leg in open_legs if leg.direction == direction)
                confirmed_fill = self._confirmation_close_level(result, expected_leg.deal_id)
                if result.get("dealStatus") != "ACCEPTED":
                    raise CapitalError(result.get("reason") or f"Закрытие {direction} не подтверждено")
                if confirmed_fill is None:
                    LOG.warning(
                        "Ignoring mismatched close confirmation direction=%s expected=%s "
                        "confirmation_deal=%s affected=%s",
                        direction, expected_leg.deal_id, result.get("dealId"),
                        result.get("affectedDeals"),
                    )
                    continue
                fills[direction] = confirmed_fill
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
        for leg in legs:
            self.state.remember_deal(leg)
            self.state.remember_close(leg.deal_id, "SCENARIO_9_MARKET", fills[leg.direction])
        scenario_nine_deals = list(dict.fromkeys(self.state.attempt_deal_ids))
        attempt_id = self.state.active_attempt_id
        self.strategy.complete_scenario_nine(long_fill, short_fill, extra_loss)
        self._get_continuation().release()
        self.state.remember_attempt(
            "COMPLETED_SCENARIO_9", self.state.net_cycle_result,
            include_in_total=False, result_kind="STRATEGY_CALCULATED_NOT_BROKER_PNL",
            completed_cycle=self.state.completed_cycles,
        )
        self.state.pending_actual_attempt_id = attempt_id
        self.state.pending_actual_deal_ids = scenario_nine_deals
        self.state.broker_transaction_pnl = None
        self.state.broker_transaction_currency = ""
        self.state.broker_transaction_status = "PENDING"
        self.state.broker_transaction_components.clear()
        transaction_key = f"transactions:{self.state.cycle_id}:{attempt_id}"
        if not any(job.get("key") == transaction_key
                   for job in self.state.pending_transaction_jobs):
            self.state.pending_transaction_jobs.append({
                "key": transaction_key, "cycle_id": self.state.cycle_id,
                "attempt_id": attempt_id, "deal_ids": list(scenario_nine_deals),
                "search_from_epoch": time.time() - 86400,
                "status": "PENDING", "amount": None, "currency": "",
                "components": [],
                "next_check_at": 0, "retry_count": 0,
                "generation": self.state.transaction_generation,
            })
        self._refresh_actual_attempt_result()
        for leg in legs:
            leg.open = False
            leg.stop = leg.take_profit = None
            leg.trigger_id = leg.trigger_reference = ""
        self.state.armed = not self.state.paused
        self.state.phase = "FILTER" if self.state.armed else "PAUSED"
        LOG.info(
            "TRADING ATTEMPT %s COMPLETED scenario=9 calculated_result=%s",
            attempt_id, self.state.net_cycle_result,
        )
        self.state.active_attempt_id = 0
        scenario_report = scenario_nine_result_text(self.state, long_fill, short_fill) + (
            "\nДальнейший режим: PAUSED до /start из-за /stop."
            if self.state.paused else "\nДальнейший режим: FILTER нового цикла."
        )
        self._store_report(scenario_report, f"scenario-9-complete:{attempt_id}")
        self._archive_and_clear_completed_cycle(scenario_report)
        self.state.save(self.cfg.state_file)
        end_diagnostic_cycle(
            self.cfg.diagnostic_log_file, self.state.attempt_counter + 1,
            self.state.completed_cycles,
        )
        self._send_report(scenario_report, key=f"scenario-9-complete:{attempt_id}")

    def _refresh_actual_attempt_result(self) -> bool:
        """Finalize scenario-9 statistics only from complete per-deal entry/close evidence."""
        attempt_id = self.state.pending_actual_attempt_id
        if not attempt_id:
            return True
        records = {
            str(item.get("deal_id")): item for item in self.state.deal_history
            if str(item.get("deal_id", "")) in self.state.pending_actual_deal_ids
        }
        missing = []
        total = D("0")
        for deal_id in self.state.pending_actual_deal_ids:
            item = records.get(deal_id)
            if item and item.get("close_level") is None:
                try:
                    activity = self.capital.activity(deal_id)
                    close_event = next(
                        (event for source in ("SL", "TP")
                         if (event := find_close_event(activity, deal_id, source)) is not None),
                        None,
                    )
                    if close_event is None:
                        direction = str(item.get("direction", ""))
                        close_direction = "SELL" if direction == "BUY" else "BUY"
                        close_event = next((event for event in reversed(normalize_events(activity))
                                            if event.deal_id == deal_id
                                            and event.event_type == "POSITION"
                                            and event.source == "USER"
                                            and event.direction == close_direction
                                            and event.level is not None), None)
                    if close_event is not None and close_event.level is not None:
                        self.state.remember_close(
                            deal_id, close_event.source or "USER", close_event.level
                        )
                except Exception:
                    LOG.warning("Actual attempt result history delayed for %s", deal_id)
                item = next((value for value in self.state.deal_history
                             if value.get("deal_id") == deal_id), item)
            if not item or item.get("entry") is None or item.get("close_level") is None \
                    or item.get("direction") not in {"BUY", "SELL"}:
                missing.append(deal_id)
                continue
            entry, close = D(str(item["entry"])), D(str(item["close_level"]))
            points = close - entry if item["direction"] == "BUY" else entry - close
            size = D(str(item.get("size", self.cfg.size))) if self.cfg.scenario_sizes else self.cfg.size
            total += points * size
        attempt = next(
            (item for item in self.state.attempt_history if item.get("attempt_id") == attempt_id),
            None,
        )
        if missing:
            if attempt is not None:
                attempt["actual_result_status"] = "PENDING"
                attempt["missing_deal_ids"] = missing
            self.state.save(self.cfg.state_file)
            return False
        if attempt is not None and not attempt.get("actual_result_recorded"):
            attempt["actual_result"] = str(total)
            attempt["actual_result_status"] = "CONFIRMED"
            attempt["actual_result_recorded"] = True
            attempt.pop("missing_deal_ids", None)
            self.state.attempt_result_total += total
        self.state.pending_actual_attempt_id = 0
        self.state.pending_actual_deal_ids.clear()
        self.state.save(self.cfg.state_file)
        return True

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
                    self._send_report(
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
                raced_leg = Leg(
                    direction, opened.level, opened.level, deal_id=opened.deal_id,
                    open=False, size=self.cfg.size_for(9), stop_distance=self.cfg.stop_for(9),
                )
                self.state.remember_deal(raced_leg, 9)
                self.state.remember_close(
                    opened.deal_id, "SCENARIO_9_TRIGGER_RACE", close_event.level
                )
                self._send_report(
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
                close = self._confirmation_close_level(result, deal_id)
                if result.get("dealStatus") != "ACCEPTED" or close is None:
                    raise CapitalError(
                        result.get("reason") or f"Сценарий 9: trigger-позиция {deal_id} не закрыта"
                    )
                loss = max(D("0"), entry - close) if direction == "BUY" else max(
                    D("0"), close - entry
                )
                total_loss += loss
                self.state.scenario_nine_extra_loss = total_loss
                closed_ids.add(deal_id)
                resolved_trigger_ids.add(str(position.get("workingOrderId", "")))
                raced_leg = Leg(
                    direction, entry, entry, deal_id=deal_id, open=False,
                    size=self.cfg.size_for(9), stop_distance=self.cfg.stop_for(9),
                )
                self.state.remember_deal(raced_leg, 9)
                self.state.remember_close(deal_id, "SCENARIO_9_TRIGGER_RACE", close)
                self._send_report(
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
            try:
                expected_close_direction = "SELL" if leg.direction == "BUY" else "BUY"
                manual_closes = [
                    event for event in normalize_events(self.capital.activity(leg.deal_id))
                    if event.deal_id == leg.deal_id and event.source == "USER"
                    and event.event_type == "POSITION" and event.status != "REJECTED"
                    and event.direction == expected_close_direction and event.level is not None
                ]
                if manual_closes:
                    return manual_closes[-1].level
            except Exception:
                LOG.warning("Manual close history is not available for %s", leg.deal_id)
            if attempt + 1 < attempts:
                time.sleep(delay)
        return None

    def _resume_pending_trigger_cancel(self) -> None:
        leg = next((item for item in (self.state.long, self.state.short)
                    if item and item.pending_trigger_cancel_unknown), None)
        if leg is None:
            return
        positions = self._cycle_positions()
        self._detect_trigger_fill(positions)
        if leg.open:
            leg.pending_trigger_cancel_unknown = False
            leg.pending_trigger_action = ""
            self.state.save(self.cfg.state_file)
            return
        if leg.pending_trigger_cancel_unknown and leg.trigger_id:
            activity = self.capital.activity()
            if find_working_order_cancellation(activity, leg.trigger_id) is not None:
                leg.trigger_id = leg.trigger_reference = ""
                leg.pending_trigger_cancel_unknown = False
                if leg.pending_trigger_action == "cancel":
                    leg.pending_trigger_action = ""
                    leg.trigger_recreation_suppressed = True
                    self.state.save(self.cfg.state_file)
                    return
            else:
                # Keep the old order binding and mutation lock, but never stop read-only survivor
                # SL/TP/protection processing while cancellation history is delayed.
                return
        if leg.pending_trigger_action == "cancel":
            leg.pending_trigger_action = ""
            leg.trigger_recreation_suppressed = True
            self.state.save(self.cfg.state_file)
            return

    def _manual_command(self, command: str, args: list[str]) -> None:
        if not args or args[0] not in {"long", "short"}:
            raise RuntimeError("Укажите long или short")
        leg = self.state.long if args[0] == "long" else self.state.short
        if not leg:
            raise RuntimeError("Указанная сторона отсутствует в состоянии цикла")
        if command == "/canceltrigger":
            if not self.state.active:
                raise RuntimeError("Trigger можно изменить только во время активного цикла")
            if leg.open:
                raise RuntimeError("Trigger можно изменить только для закрытой стороны")
        elif not leg.open:
            raise RuntimeError("Указанная позиция не открыта")
        if command == "/canceltrigger":
            if leg.trigger_id:
                leg.pending_trigger_action = "cancel"
                self.state.save(self.cfg.state_file)
                try:
                    cancelled = self.capital.delete_working_order(leg.trigger_id)
                except CapitalError:
                    cancelled = False
                if not cancelled:
                    leg.pending_trigger_cancel_unknown = True
                    self.state.save(self.cfg.state_file)
                    raise RuntimeError(
                        f"Исход отмены Trigger {leg.trigger_id} ещё не подтверждён; "
                        "ownership сохранён, survivor остаётся под автосопровождением"
                    )
                leg.trigger_id = leg.trigger_reference = ""
                leg.pending_trigger_action = ""
                leg.trigger_recreation_suppressed = True
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
                "Сначала сверьте и разберите их вручную."
            )
        self._clear_stale_cycle("Пользователь подтвердил выход командой /automode")

    def _clear_stale_cycle(self, reason: str) -> None:
        previous_scenario = self.state.scenario
        self.state.events.append(f"stale cycle cleared: {reason}")
        self.state.reset()
        self.state.paused = True
        self.state.phase = "PAUSED"
        self.state.save(self.cfg.state_file)
        self._send_report(
            f"✅ Ручной режим сброшен\nПричина: {reason}\n"
            f"Предыдущий сценарий: {previous_scenario}\n"
            "Открытых позиций и ордеров нет. Новый цикл ожидает /start."
        )

    def _send_diagnostic_log(self) -> None:
        if self.telegram.send_log_snapshot(
            lambda: snapshot_diagnostics(self.cfg.diagnostic_log_file)
        ):
            self.telegram.send(
                "⏳ Согласованный снимок диагностики поставлен в отдельную очередь документов. "
                "Будет отправлено необходимое количество последовательно пронумерованных "
                "частей .log без обрезания содержимого."
            )
        else:
            LOG.warning("Diagnostic snapshot was not queued for Telegram")

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
            submitted_revision = (
                leg.protection_sent_stop == leg.stop
                and leg.protection_sent_take_profit == leg.take_profit
                and leg.protection_confirmation in {"ACCEPTED", "исход неизвестен"}
            )
            if submitted_revision and leg.protection_readback != "ПОДТВЕРЖДЕНО":
                # Confirmation belongs to this exact revision.  Retry only the safe GET
                # read-back; never repeat PUT because its verification response was malformed or
                # temporarily unavailable.
                actual_stop, actual_tp = self._wait_position_protection(
                    leg.deal_id, leg.stop, leg.take_profit
                )
                if actual_stop != leg.stop or actual_tp != leg.take_profit:
                    leg.protection_readback = (
                        f"НЕ ПОДТВЕРЖДЕНО: прочитано {actual_stop}/{actual_tp}"
                    )
                    self.state.save(self.cfg.state_file)
                    return False
                leg.confirmed_stop, leg.confirmed_take_profit = actual_stop, actual_tp
                leg.confirmed_stop_distance = abs(leg.current_entry - actual_stop)
                leg.protection_readback = "ПОДТВЕРЖДЕНО"
                self.state.save(self.cfg.state_file)
                self._send_report(
                    f"🛡 {cycle_heading(self.state, 'отложенная проверка защиты завершена')}\n"
                    f"{self._leg_details(leg)}\nПовторён только GET /positions; PUT не отправлялся."
                )
                return True
            try:
                # Record exactly which calculated revision was submitted.  Previous confirmed
                # levels remain historical until this revision has its own confirmation/readback.
                leg.protection_sent_stop = leg.stop
                leg.protection_sent_take_profit = leg.take_profit
                leg.confirmation_stop = leg.confirmation_take_profit = None
                leg.protection_confirmation = "ожидается"
                leg.protection_readback = "не выполнено"
                self.state.save(self.cfg.state_file)
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
                        "Protection deferred because dealId is not currently visible; "
                        "closure is not inferred from 404: direction=%s "
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
                        leg.confirmed_stop, leg.confirmed_take_profit = leg.stop, leg.take_profit
                        leg.confirmed_stop_distance = abs(leg.current_entry - leg.stop)
                        leg.protection_confirmation = "исход неизвестен"
                        leg.protection_readback = "ПОДТВЕРЖДЕНО"
                        LOG.info(
                            "Protection PUT accepted despite transport error: dealId=%s",
                            leg.deal_id,
                        )
                        details = self._leg_details(
                            leg, broker_stop=leg.stop, broker_target=leg.take_profit,
                            confirmation="исход неизвестен", readback="ПОДТВЕРЖДЕНО",
                        )
                        self._send_report(
                            f"🛡 {cycle_heading(self.state, 'защита подтверждена после неопределённого PUT')}\n"
                            f"Confirmation PUT был недоступен; источник подтверждения — повторное "
                            f"повторным чтением /positions.\n{details}"
                        )
                        return True
                if "error.invalid.takeprofit." not in text or not self._take_profit_already_reached(leg):
                    raise
                self._close_reached_take_profit(leg)
                return False
            self._confirm_update(reference)
            leg.confirmation_stop, leg.confirmation_take_profit = leg.stop, leg.take_profit
            leg.protection_confirmation = "ACCEPTED"
            leg.protection_readback = "ожидается"
            self.state.save(self.cfg.state_file)
            actual_stop, actual_tp = self._wait_position_protection(
                leg.deal_id, leg.stop, leg.take_profit
            )
            if actual_stop is None and actual_tp is None:
                leg.protection_readback = "НЕ ПОДТВЕРЖДЕНО: позиция/уровни не прочитаны"
                self.state.save(self.cfg.state_file)
                return False
            if actual_stop != leg.stop or actual_tp != leg.take_profit:
                leg.protection_readback = (
                    f"НЕ ПОДТВЕРЖДЕНО: прочитано {actual_stop}/{actual_tp}"
                )
                self.state.save(self.cfg.state_file)
                raise CapitalError(
                    f"Protection read-back mismatch {leg.deal_id}: "
                    f"expected {leg.stop}/{leg.take_profit}, actual {actual_stop}/{actual_tp}"
                )
            leg.confirmed_stop, leg.confirmed_take_profit = actual_stop, actual_tp
            leg.confirmed_stop_distance = abs(leg.current_entry - actual_stop)
            leg.protection_readback = "ПОДТВЕРЖДЕНО"
            self.state.save(self.cfg.state_file)
            details = self._leg_details(
                leg, broker_stop=actual_stop, broker_target=actual_tp,
                confirmation="ACCEPTED", readback="ПОДТВЕРЖДЕНО",
            )
            self._send_report(
                f"🛡 {cycle_heading(self.state, 'защита позиции подтверждена')}\n"
                "Этапы: PUT отправлен → confirmation ACCEPTED → уровни повторно прочитаны "
                "из /positions.\n"
                f"{details}"
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
        self.state.pending_close_direction = leg.direction
        self.state.pending_close_reference = reference
        self.state.pending_close_reason = "TP_REACHED_MARKET_CLOSE"
        self.state.save(self.cfg.state_file)
        result = self.capital.wait_confirmation(reference)
        if result.get("dealStatus") != "ACCEPTED":
            self.state.pending_close_direction = self.state.pending_close_reference = ""
            self.state.pending_close_reason = ""
            self.state.save(self.cfg.state_file)
            raise CapitalError(result.get("reason") or "Закрытие достигнутого TP отклонено")
        fill = self._confirmation_close_level(result, leg.deal_id)
        if fill is None:
            fill = self._wait_any_closing_fill(leg, attempts=20, delay=0.5)
        if fill is None:
            self._send_report(
                "⏳ MARKET-закрытие принято без подтверждённой связи с позицией. "
                "Повторный DELETE запрещён; сверка продолжится по сохранённому reference."
            )
            return
        self.state.pending_close_direction = self.state.pending_close_reference = ""
        self.state.pending_close_reason = ""
        # Persist the confirmed close before resolving the trigger race. If Android stops here, the
        # next tick/restart must not send a second DELETE or lose the actual fill.
        self.state.pending_tp_direction = leg.direction
        self.state.pending_tp_fill = fill
        self.state.save(self.cfg.state_file)
        self._finish_reached_take_profit()

    def _resume_pending_close(self) -> None:
        direction = self.state.pending_close_direction
        leg = self.state.long if direction == "BUY" else self.state.short
        if not leg or not leg.deal_id:
            self._manual("Pending close не связан с локальной позицией")
            return
        try:
            result = self.capital.wait_confirmation(self.state.pending_close_reference)
            if result.get("dealStatus") == "REJECTED":
                self.state.pending_close_direction = self.state.pending_close_reference = ""
                self.state.pending_close_reason = ""
                self.state.save(self.cfg.state_file)
                return
            fill = self._confirmation_close_level(result, leg.deal_id)
            if fill is None:
                fill = self._wait_any_closing_fill(leg, attempts=20, delay=0.5)
        except Exception as exc:
            self.state.pending_close_reason = str(exc)
            self.state.save(self.cfg.state_file)
            return
        if fill is None:
            return
        self.state.pending_close_direction = self.state.pending_close_reference = ""
        self.state.pending_close_reason = ""
        self.state.pending_tp_direction = direction
        self.state.pending_tp_fill = fill
        self.state.save(self.cfg.state_file)
        self._finish_reached_take_profit()

    def _finish_reached_take_profit(self) -> None:
        """Resume/finalize a MARKET TP close only after its recovery trigger is resolved."""
        direction = self.state.pending_tp_direction
        fill = self.state.pending_tp_fill
        if not direction or fill is None:
            return
        leg = self.state.long if direction == "BUY" else self.state.short
        if leg is None:
            raise CapitalError(f"Не найдена сторона ожидающего TP-завершения: {direction}")
        try:
            self._cancel_pending_trigger_for_completion(leg)
        except CapitalError as exc:
            LOG.info(
                "Reached TP close is confirmed but trigger outcome remains pending: %s", exc
            )
            self._send_report(
                "⏳ MARKET-закрытие по достигнутому TP подтверждено, но trigger ещё сверяется\n"
                f"Сторона: {direction}\nФактическое закрытие: {fill}\n"
                "Цикл остаётся активным; новый вход заблокирован."
            )
            return
        self._complete_cycle(direction, fill)
        self.state.pending_tp_direction = ""
        self.state.pending_tp_fill = None
        self.state.armed = not self.state.paused
        self.state.phase = "FILTER" if self.state.armed else "PAUSED"
        self.state.save(self.cfg.state_file)
        suffix = (
            "Перехожу к фильтру следующего цикла."
            if self.state.armed else "Следующий цикл ожидает /start."
        )
        self._send_report(
            "✅ Целевая цена достигнута до установки TP\n"
            f"Сторона: {direction}\nРасчётный TP: {leg.take_profit}\n"
            f"Фактическое MARKET-закрытие: {fill}\n{suffix}\n"
            f"{cycle_result_text(self.state, leg.direction, fill, self.cfg.size)}"
        )

    def _cancel_pending_trigger_for_completion(self, winner: Leg) -> None:
        """Prove the opposite recovery trigger harmless before declaring TP completion."""
        pending = self.state.short if winner.direction == "BUY" else self.state.long
        if not pending or not pending.trigger_id:
            return
        trigger_id = pending.trigger_id
        cancelled = self.capital.delete_working_order(trigger_id)
        if not cancelled:
            race_loss = self._close_trigger_that_raced_with_tp(pending)
            if race_loss is None:
                raise CapitalError(
                    "Достигнут TP, но результат одновременного trigger-fill не подтверждён: "
                    f"workingOrderId={trigger_id}"
                )
            self.state.last_trigger_resolution = (
                f"workingOrderId={trigger_id} исполнился около TP; позиция закрыта, "
                f"отдельный signed result={race_loss}."
            )
        else:
            self.state.last_trigger_resolution = (
                f"workingOrderId={trigger_id}: DELETE confirmation ACCEPTED."
            )
        pending.trigger_id = pending.trigger_reference = ""
        self.state.save(self.cfg.state_file)

    def _apply_stop_only(self, leg: Leg | None) -> None:
        """Install an exact SL, verify its broker level, and deliberately leave TP unset."""
        if not leg or not leg.open or not leg.deal_id or leg.stop is None:
            return
        leg.protection_sent_stop, leg.protection_sent_take_profit = leg.stop, None
        leg.confirmation_stop = leg.confirmation_take_profit = None
        self._confirm_update(self.capital.update_position(leg.deal_id, leg.stop, None))
        leg.confirmation_stop, leg.confirmation_take_profit = leg.stop, None
        leg.protection_confirmation, leg.protection_readback = "ACCEPTED", "ожидается"
        self.state.save(self.cfg.state_file)
        actual_stop = self._wait_position_stop(leg.deal_id, leg.stop)
        if actual_stop != leg.stop:
            leg.protection_readback = f"НЕ ПОДТВЕРЖДЕНО: прочитано {actual_stop}"
            self.state.save(self.cfg.state_file)
            raise CapitalError(
                f"Capital.com не подтвердил точный SL {leg.stop} для {leg.deal_id}; "
                f"фактический stopLevel={actual_stop}"
            )
        leg.confirmed_stop, leg.confirmed_take_profit = actual_stop, None
        leg.confirmed_stop_distance = abs(leg.current_entry - actual_stop)
        leg.protection_readback = "ПОДТВЕРЖДЕНО"
        self.state.save(self.cfg.state_file)
        self._send_report(
            f"🛡 {cycle_heading(self.state, 'SL позиции подтверждён брокером')}\n"
            f"{self._leg_details(leg, broker_stop=actual_stop, confirmation='ACCEPTED', readback='ПОДТВЕРЖДЕНО')}\n"
            "TP пока не установлен и не подтверждён: бот установит его только после прохождения SL-барьера."
        )

    def _apply_take_profit_only(self, leg: Leg | None) -> None:
        """Add TP after the SL barrier and verify both broker-side protection levels."""
        if (
            not leg or not leg.open or not leg.deal_id
            or leg.stop is None or leg.take_profit is None
        ):
            return
        leg.protection_sent_stop = leg.stop
        leg.protection_sent_take_profit = leg.take_profit
        leg.confirmation_stop = leg.confirmation_take_profit = None
        self._confirm_update(
            self.capital.update_position(leg.deal_id, leg.stop, leg.take_profit)
        )
        leg.confirmation_stop, leg.confirmation_take_profit = leg.stop, leg.take_profit
        leg.protection_confirmation, leg.protection_readback = "ACCEPTED", "ожидается"
        self.state.save(self.cfg.state_file)
        actual_stop, actual_tp = self._wait_position_protection(
            leg.deal_id, leg.stop, leg.take_profit
        )
        if actual_stop != leg.stop or actual_tp != leg.take_profit:
            leg.protection_readback = f"НЕ ПОДТВЕРЖДЕНО: прочитано {actual_stop}/{actual_tp}"
            self.state.save(self.cfg.state_file)
            raise CapitalError(
                "Capital.com не подтвердил точную защиту "
                f"для {leg.deal_id}; ожидались SL={leg.stop}, TP={leg.take_profit}; "
                f"фактически SL={actual_stop}, TP={actual_tp}"
            )
        leg.confirmed_stop, leg.confirmed_take_profit = actual_stop, actual_tp
        leg.confirmed_stop_distance = abs(leg.current_entry - actual_stop)
        leg.protection_readback = "ПОДТВЕРЖДЕНО"
        self.state.save(self.cfg.state_file)
        self._send_report(
            f"🎯 {cycle_heading(self.state, 'Take Profit подтверждён; полная защита позиции подтверждена брокером')}\n"
            f"{self._leg_details(leg, broker_stop=actual_stop, broker_target=actual_tp, confirmation='ACCEPTED', readback='ПОДТВЕРЖДЕНО')}\n"
            "Подтверждение: PUT принят и уровни повторно прочитаны из /positions."
        )

    def _wait_position_stop(
        self, deal_id: str, expected: Decimal, attempts: int = 5, delay: float = 0.25
    ) -> Decimal | None:
        """Read the position back until Capital.com exposes the requested exact stop level."""
        actual = None
        for attempt in range(attempts):
            try:
                if hasattr(self.capital, "position"):
                    payload = self.capital.position(deal_id)
                else:  # compatible lightweight broker adapters
                    payload = next(
                        (item for item in self.capital.positions()
                         if str(self._position_data(item).get("dealId", "")) == deal_id),
                        {},
                    )
            except CapitalError as exc:
                # A protected position can close between the successful PUT/confirmation and
                # this read-back.  A 404 is therefore an exit signal to be reconciled from
                # activity, not a loop error.
                if "error.not-found.dealid" in str(exc).lower():
                    LOG.info("Position %s closed before SL read-back", deal_id)
                    return None
                raise
            position = payload.get("position", payload) if isinstance(payload, dict) else None
            if not isinstance(position, dict):
                actual = None
            else:
                value = position.get("stopLevel")
                try:
                    actual = D(str(value)) if value is not None else None
                except (ArithmeticError, TypeError, ValueError):
                    actual = None
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
                if hasattr(self.capital, "position"):
                    payload = self.capital.position(deal_id)
                else:  # compatible lightweight broker adapters
                    payload = next(
                        (item for item in self.capital.positions()
                         if str(self._position_data(item).get("dealId", "")) == deal_id),
                        {},
                    )
            except CapitalError as exc:
                if "error.not-found.dealid" in str(exc).lower():
                    LOG.info("Position %s closed before protection read-back", deal_id)
                    return None, None
                raise
            if not isinstance(payload, dict):
                LOG.warning("Invalid /positions payload for %s: %r", deal_id, payload)
                position = None
            else:
                position = payload.get("position", payload)
            if not isinstance(position, dict):
                actual_stop = actual_tp = None
                if attempt + 1 < attempts:
                    time.sleep(delay)
                continue
            stop_value = position.get("stopLevel")
            tp_value = position.get("profitLevel")
            try:
                actual_stop = D(str(stop_value)) if stop_value is not None else None
                actual_tp = D(str(tp_value)) if tp_value is not None else None
            except (ArithmeticError, TypeError, ValueError):
                LOG.warning(
                    "Unreadable protection for %s: stopLevel=%r profitLevel=%r",
                    deal_id, stop_value, tp_value,
                )
                actual_stop = actual_tp = None
                if attempt + 1 < attempts:
                    time.sleep(delay)
                continue
            if actual_stop == expected_stop and actual_tp == expected_tp:
                return actual_stop, actual_tp
            if attempt + 1 < attempts:
                time.sleep(delay)
        return actual_stop, actual_tp

    def _confirm_update(self, reference: str) -> dict:
        result = self.capital.wait_confirmation(reference)
        if result.get("dealStatus") != "ACCEPTED":
            raise CapitalError(result.get("reason") or "Изменение позиции отклонено")
        return result

    def _cycle_positions(self) -> dict[str, dict]:
        result = {}
        for item in self.capital.positions():
            data = self._position_data(item)
            if self._position_epic(item) == self.cfg.epic and data.get("dealId"):
                result[str(data["dealId"])] = data
        return result

    def _find_order(self, leg: Leg) -> dict | None:
        expected_size, _, _ = self.strategy.projected_reopen(leg.direction)
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
                    and D(str(size)) == expected_size):
                matches.append(data)
        if len(matches) > 1:
            raise CapitalError(
                f"Найдено несколько одинаковых trigger {leg.direction} на "
                f"{leg.original_trigger_level}; автоматическое связывание небезопасно"
            )
        return matches[0] if matches else None

    def _closing_fill(self, leg: Leg, expected_source: str = "SL") -> Decimal | None:
        try:
            event = find_close_event(
                self.capital.activity(leg.deal_id), leg.deal_id, expected_source
            )
        except CapitalError:
            event = None
        if event and event.level is not None:
            self._remember_close_event(leg, event)
            return event.level
        saved = self._saved_close_event(leg, expected_source)
        if saved is not None:
            return saved.level
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
            self._remember_close_event(leg, event)
            LOG.info(
                "Close resolved from global activity: dealId=%s source=%s fill=%s",
                leg.deal_id, expected_source, event.level,
            )
            return event.level
        return None

    def _close_event_any_index(self, leg: Leg, expected_source: str,
                               global_activity: list[dict]):
        event = find_close_event(global_activity, leg.deal_id, expected_source)
        if event is not None:
            self._remember_close_event(leg, event)
            return event
        try:
            event = find_close_event(
                self.capital.activity(leg.deal_id), leg.deal_id, expected_source
            )
            if event is not None:
                self._remember_close_event(leg, event)
                return event
            return self._saved_close_event(leg, expected_source)
        except CapitalError:
            return self._saved_close_event(leg, expected_source)

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
        self._send_report(
            f"🚨 {cycle_heading(self.state, 'переход в ручной режим')}\n"
            f"Причина: {reason}\n"
            f"Состояние: phase=MANUAL; active={self.state.active}; новые заявки запрещены.\n"
            "Следующее действие: проверить /positions, /orders и /dealhistory; затем использовать "
            "/automode только после однозначной сверки брокера."
        )


def main() -> None:
    settings = Settings.from_env()
    configure_diagnostics(settings.diagnostic_log_file)
    LOG.info("Bot process starting; demo=%s epic=%s state=%s", settings.demo,
             settings.epic, settings.state_file)
    Bot(settings).run()
