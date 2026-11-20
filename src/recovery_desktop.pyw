"""Local RECOVERY desktop. Its only write path is the private control mailbox."""
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from control_host import LocalClient, REQUEST_TTL, allowed
from local_settings import validate_settings


MODES = {"ready": "Готов к выбору режима", "manual_wait": "Ждёт ручную рыночную позицию",
         "pending_wait": "Ждёт ручной отложенный STOP", "active": "Сопровождает цикл RECOVERY",
         "unknown": "Состояние не подтверждено"}
RESULTS = {"applied": "Команда выполнена.", "rejected": "Контроллер отклонил команду.",
           "unknown": "Выполнение не подтверждено. Проверьте текущее состояние."}
REASONS = {"not_available": "Команда недоступна в текущем состоянии.",
           "session_changed": "Сеанс или состояние контроллера изменились.",
           "expired": "Истекло время ожидания команды.", "invalid_settings": "Некорректные параметры.",
           "owner_rejected": "Контроллер отклонил действие при проверке условий.",
           "starting": "Контроллер проверяет привязку и подключение.",
           "account_or_connection": "Проверьте выбранный счёт и соединение с MT5.",
           "symbol_not_available": "Символ недоступен в MT5.",
           "trading_permission": "MT5 не подтвердил разрешение на торговлю.",
           "helper_observation_unavailable": "Ожидаю показания помощников на двух графиках MT5.",
           "uncertain_broker_action": "Результат операции в MT5 ещё не подтверждён.",
           "awaiting_predecessor_stop_classification": "Ожидаю подтверждение причины выхода предыдущей позиции.",
           "awaiting_stop": "Ожидаю подтверждение STOP в MT5.",
           "awaiting_survivor_move": "Ожидаю подтверждение изменения оставшегося ордера.",
           "counterorder_canceled": "Встречный ордер отменён.",
           "counterorder_history_unresolved": "Ожидаю историю встречного ордера.",
           "missing_active_binding": "Проверяется принадлежность позиции текущему циклу.",
           "old_working_order": "Проверяется ранее выставленный ордер.",
           "waiting_counterorder_cancellation": "Ожидаю подтверждение отмены встречного ордера.",
           "unconfirmed_cleanup_owner": "Принадлежность ордера текущему циклу ещё не подтверждена. Отмена отложена.",
           "waiting_reverse_fill": "Полный SL подтверждён. Встречный STOP остаётся выставленным.",
           "prior_exposure_still_open": "Ожидаю закрытие прежней позиции или завершение связанной заявки.",
           "waiting_exact_order": "Ожидаю данные выбранного ордера в MT5.",
           "money_component_pending": "Ожидаю полную историю для денежного результата.",
           "unclassified_exit": "Причина выхода уточняется по истории MT5.",
           "unconfirmed_stop_level": "Уровень SL ещё не подтверждён.",
           "unexpected_order_type": "Тип ордера отличается от ожидаемого.",
           "sl_helper_permission": "Помощник SL не подтвердил разрешение. Проверьте его график и разрешения MT5.",
           "verifier_binding": "Проверяется привязка графика помощника истории.",
           "inventory_changing": "Список позиций и ордеров меняется. Идёт повторная сверка.",
           "inventory_unavailable": "MT5 пока не вернул список позиций и ордеров.",
           "unbound_strategy_exposure": "Найдена позиция или заявка RECOVERY с неподтверждённой привязкой к циклу.",
           "action_identity_changed": "Привязка операции изменилась. Проверяется состояние в MT5.",
           "active_position_changed": "Данные сопровождаемой позиции изменились.",
           "alternative_fill_order_ambiguous": "Не удалось однозначно связать исполнение с ордером.",
           "cancel_geometry_unconfirmed": "Параметры отменяемого ордера ещё не подтверждены.",
           "closed_evidence_changed": "Изменились данные закрытия. Идёт сверка истории.",
           "counterorder_fill_incomplete": "Ожидаю полное подтверждение исполнения встречного ордера.",
           "counterorder_geometry_changed": "Параметры встречного ордера изменились.",
           "counterorder_identity": "Проверяется тикет встречного ордера.",
           "cycle_exhausted": "Все 24 ступени цикла использованы.",
           "filled_pending_position_missing": "Исполненный отложенный ордер пока не связан с позицией.",
           "multiple_alternative_fills": "Обнаружено несколько возможных исполнений. Идёт сверка.",
           "next_position_mismatch": "Следующая позиция отличается от данных исполненного ордера.",
           "order_history_unavailable": "Ожидаю историю ордера из MT5.",
           "partial_or_changed_order": "Объём ордера изменился или ордер исполнен частично.",
           "pending_fill_incomplete": "Исполнение отложенного ордера подтверждено не полностью.",
           "pending_fill_mismatch": "Исполнение отложенного ордера расходится с ожидаемыми данными.",
           "pending_history_changed": "История отложенного ордера изменилась.",
           "pending_history_unavailable": "Ожидаю историю отложенного ордера.",
           "pending_order_changed": "Параметры выбранного отложенного ордера изменились.",
           "pending_sl_changed": "Изменился SL отложенного ордера.",
           "protect_position_missing": "Позиция для установки SL пока не подтверждена.",
           "settings_off_grid": "Проверьте шаг цены и объёма в настройках выбранного символа.",
           "surviving_order_unconfirmed": "Оставшийся ордер пока не подтверждён.",
           "survivor_not_working": "Оставшийся ордер уже не числится активным. Проверяется история.",
           "deal_history_scope": "Сделки проверяются на соответствие текущей позиции.",
           "deal_history_unavailable": "Ожидаю историю сделок из MT5.",
           "deal_sources_disagree": "Источники расходятся в данных сделок. Ожидаю сверку.",
           "entry_incomplete": "Фактический вход подтверждён не полностью.",
           "entry_not_manual": "Для этого входа не подтверждено ручное происхождение.",
           "entry_order_mismatch": "Сделка входа не совпадает с ожидаемым ордером.",
           "entry_order_unconfirmed": "Ордер открытия позиции ещё не подтверждён.",
           "filled_price_off_grid": "Цена исполнения расходится с шагом цены символа.",
           "full_exit_unconfirmed": "Полное закрытие позиции ещё не подтверждено.",
           "history_helper_scope": "Проверяется привязка ответа помощника истории.",
           "history_sources_disagree": "Данные истории MT5 и помощника расходятся.",
           "invalid_grid": "MT5 не подтвердил шаг цены или объёма инструмента.",
           "invalid_order_grid": "Параметры ордера не совпадают с шагом цены или объёма.",
           "money_currency_unconfirmed": "Валюта денежного результата пока не подтверждена.",
           "money_exit_changed": "Состав сделок закрытия изменился. Денежный итог проверяется.",
           "money_exits_unconfirmed": "Ожидаю подтверждение всех закрывающих сделок для расчёта итога.",
           "not_market_entry": "Сделка не соответствует ожидаемому рыночному входу.",
           "not_native_stop": "Не подтверждён подходящий отложенный STOP в MT5.",
           "off_grid": "Значение расходится с шагом цены или объёма символа.",
           "order_changing": "Данные ордера обновляются. Идёт сверка.",
           "order_check_rejected": "MT5 отклонил предварительную проверку операции.",
           "order_history_disagrees": "Источники расходятся в данных ордера.",
           "order_history_scope": "Проверяется принадлежность записи истории нужному ордеру.",
           "position_changing": "Данные позиции меняются. Идёт повторная сверка.",
           "retired_fill_incomplete": "Жду полных данных исполнения прежнего ордера.",
           "stale_pending_action": "Отложенный ордер изменился до подтверждения операции.",
           "stale_position_action": "Позиция изменилась до подтверждения операции.",
           "support_counter_changed": "Встречный ордер изменился во время проверки поддержки.",
           "snapshot_unavailable": "Не удалось получить состояние стратегии."}


def work_area(window):
    # Windows returns the work area in this Tk process's DPI coordinate space.
    from ctypes import byref, windll, wintypes
    rect = wintypes.RECT()
    if windll.user32.SystemParametersInfoW(48, 0, byref(rect), 0):
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def fit_window(window, area, desired_width, desired_height):
    left, top, width, height = area
    client_w = min(desired_width, max(1, width - 28))
    client_h = min(desired_height, max(1, height - 62))
    x = left + max(0, (width - client_w - 16) // 2)
    y = top + max(0, (height - client_h - 42) // 2)
    window.geometry(f"{client_w}x{client_h}{x:+}{y:+}")
    return client_w, client_h


def flow_buttons(buttons, width):
    row = col = used = 0
    for button in buttons:
        need = button.winfo_reqwidth() + 7
        if col and used + need > width:
            row, col, used = row + 1, 0, 0
        button.grid(row=row, column=col, sticky="w", padx=(0, 7), pady=2)
        col, used = col + 1, used + need


class Desktop:
    def __init__(self, root, client, ensure_supervisor):
        self.root, self.client, self.ensure_supervisor = root, client, ensure_supervisor
        self.state = self.pending = self.editor = None
        self.booting = self.start_after_boot = False
        self.start_deadline = 0
        self.events = queue.SimpleQueue()
        root.title("RECOVERY — локальное управление")
        area = work_area(root)
        width, height = fit_window(root, area, 770, 545)
        root.minsize(min(width, 420), min(height, 410))
        style = ttk.Style(root)
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Mode.TLabel", font=("Segoe UI", 11, "bold"))
        self.padding = 12 if width < 650 else 22
        shell = ttk.Frame(root, padding=self.padding)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        ttk.Label(shell, text="RECOVERY", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.connection, self.identity, self.mode, self.details, self.message = [tk.StringVar() for _ in range(5)]
        connection = ttk.Label(shell, textvariable=self.connection)
        connection.grid(row=1, column=0, sticky="w", pady=(6, 2))
        identity = ttk.Label(shell, textvariable=self.identity)
        identity.grid(row=2, column=0, sticky="w")
        status_frame = ttk.LabelFrame(shell, text="Состояние стратегии", padding=(14, 9))
        status_frame.grid(row=3, column=0, sticky="ew", pady=(14, 8))
        mode_label = ttk.Label(status_frame, textvariable=self.mode, style="Mode.TLabel")
        mode_label.pack(anchor="w")
        details = ttk.Label(status_frame, textvariable=self.details, justify="left")
        details.pack(anchor="w", pady=(4, 0))
        bar = ttk.Frame(shell)
        bar.grid(row=4, column=0, sticky="ew", pady=6)
        self.buttons = {}
        for action, label in (("start", "Старт"), ("pause", "Стоп")):
            self.buttons[action] = ttk.Button(bar, text=label, command=lambda a=action: self.act(a))
        self.settings_btn = ttk.Button(bar, text="Настройки стратегии…", command=self.edit)
        modes_frame = ttk.LabelFrame(shell, text="Ручной вход в MT5", padding=(14, 9))
        modes_frame.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        for action, label in (("manual_wait", "Ждать рыночную позицию"),
                              ("manual_pending_wait", "Ждать триггерный STOP"),
                              ("manual_cancel", "Отменить ожидание")):
            self.buttons[action] = ttk.Button(modes_frame, text=label, command=lambda a=action: self.act(a))
        feedback = ttk.Label(shell, textvariable=self.message, justify="left")
        feedback.grid(row=6, column=0, sticky="w", pady=(14, 0))
        def layout(client_width):
            inner = max(120, client_width - self.padding * 2 - 8)
            for label in (connection, identity, feedback):
                label.configure(wraplength=inner)
            for label in (mode_label, details):
                label.configure(wraplength=max(100, inner - 34))
            flow_buttons([self.buttons["start"], self.buttons["pause"], self.settings_btn], inner)
            flow_buttons([self.buttons[k] for k in ("manual_wait", "manual_pending_wait", "manual_cancel")], inner - 34)
        self.last_layout_width = width
        layout(width)
        def resized(event):
            if event.widget is root and event.width != self.last_layout_width:
                self.last_layout_width = event.width
                layout(event.width)
        root.bind("<Configure>", resized, add="+")
        self.refresh()
        self.ensure()

    def ensure(self, start=False):
        self.start_after_boot |= start
        if start:
            self.start_deadline = time.monotonic() + 15
        if self.booting:
            return
        self.booting = True
        def run():
            try:
                self.ensure_supervisor()
                self.events.put(True)
            except Exception:
                self.events.put(False)
        threading.Thread(target=run, daemon=True).start()

    def act(self, action, settings=None, expected=None):
        if self.pending:
            return
        if action == "start" and self.state is None:
            self.message.set("Ожидаю локальный контроллер Python…")
            self.ensure(start=True)
            return
        try:
            handle = self.client.submit(action, expected or self.state, settings)
        except ValueError as error:
            self.message.set(str(error))
            return
        except OSError:
            self.message.set("Не удалось записать команду. Проверьте состояние контроллера.")
            return
        self.pending = (handle, time.monotonic())
        self.message.set("Команда передана. Ожидаю подтверждение контроллера…")
        self.render()
        return True

    def refresh(self):
        self.state = self.client.status()
        while not self.events.empty():
            ok = self.events.get()
            self.booting = False
            if not ok:
                self.start_after_boot = False
                self.message.set("Не удалось запустить контроллер Python. Старт повторит подключение.")
        if self.start_after_boot:
            if self.state:
                self.start_after_boot = False
                if allowed(self.state, "start"):
                    self.act("start")
            elif time.monotonic() > self.start_deadline:
                self.start_after_boot = False
                self.message.set("Контроллер не подтвердил запуск.")
        if self.pending:
            ticket, start = self.pending
            try:
                receipt = self.client.receipt(ticket)
            except (ValueError, OSError):
                receipt = None
            if receipt and receipt["status"] in RESULTS:
                self.message.set(REASONS.get(receipt.get("reason"), RESULTS[receipt["status"]]))
                self.pending = None
            elif self.state and self.state["boot"] != ticket[0]:
                self.message.set("Сеанс сменился. Результат прежней команды не подтверждён.")
                self.pending = None
            elif time.monotonic() - start > REQUEST_TTL + 2:
                self.message.set("Подтверждение не получено. Контроллер проверит уже переданную команду.")
                if receipt is None:
                    self.pending = None
        self.render()
        self.root.after(500, self.refresh)

    def render(self):
        state = self.state
        if state is None:
            self.connection.set("Python — нет свежего ответа     MT5 — связь не подтверждена")
            self.identity.set("")
            self.mode.set("Контроллер недоступен")
            self.details.set("")
        else:
            python = "работает" if state["running"] else "на паузе"
            mt5 = "подключён" if state["mt5_connected"] else "нет связи"
            self.connection.set(f"Python — {python}     MT5 — {mt5}")
            self.identity.set("    ".join(f"{label} {state.get(key) or '—'}" for key, label in (
                ("account", "Счёт"), ("server", "Сервер"), ("symbol", "Символ"))))
            self.mode.set(MODES[state["mode"]])
            notes = []
            if state["mode"] == "active":
                if state.get("step") not in (None, "", "0"):
                    notes.append("Ступень " + state["step"] + " из 24.")
                notes.append("Повторное рыночное ожидание " + ("включено." if state["market_rearm_enabled"] else "отключено."))
                for field, label in (("position_ticket", "Позиция"), ("opposite_ticket", "Встречный ордер")):
                    if state.get(field) not in (None, "", "0"):
                        notes.append(f"{label} №{state[field]}.")
                if state.get("support_applied"):
                    notes.append("Перенос SL на цену входа подтверждён.")
            elif state["mode"] == "pending_wait":
                notes.append("Отложенный ордер выставляется вручную в MT5. Проверьте его SL в терминале.")
            elif state["mode"] == "manual_wait":
                notes.append("После ручного закрытия позиции и отмены встречного ордера ожидание возобновится."
                             if state["market_rearm_enabled"] else "Повторное ожидание отключено.")
            if state.get("closed_outcomes") not in (None, "", "0") and state.get("realized_total") not in (None, ""):
                notes.append(f"Подтверждённый итог по {state['closed_outcomes']} закрытиям: {state['realized_total']} {state.get('currency', '')}.")
            if state.get("pending_outcomes") not in (None, "", "0"):
                notes.append("Ожидают полной истории закрытий: " + state["pending_outcomes"] + ".")
            if state.get("reason") and state["reason"] not in {"ok", "ready", "none"}:
                notes.append(REASONS.get(state["reason"], state["reason"]))
            self.details.set(" ".join(notes))
        for action, button in self.buttons.items():
            usable = allowed(state, action) if state else action == "start"
            button.state(["!disabled"] if usable and not self.pending and not self.start_after_boot else ["disabled"])
        self.buttons["manual_cancel"].configure(text="Отключить повтор" if state and state["mode"] == "active" else "Отменить ожидание")
        show_settings = state and state.get("settings")
        self.settings_btn.state(["!disabled"] if show_settings else ["disabled"])
        if self.editor is not None:
            authorized = show_settings and allowed(state, "update_settings") and not self.pending and (
                state["boot"], state["generation"]) == self.edit_binding
            for entry in self.fields:
                entry.state(["!readonly"] if authorized else ["readonly"])
            self.save_btn.state(["!disabled"] if authorized else ["disabled"])

    def edit(self):
        if self.editor is not None:
            self.editor.lift()
            return
        if not self.state or "settings" not in self.state:
            return
        settings = self.state["settings"]
        expected = self.state.copy()
        self.edit_binding = (expected["boot"], expected["generation"])
        window = self.editor = tk.Toplevel(self.root)
        window.title("Настройки RECOVERY")
        window.transient(self.root)
        area = work_area(self.root)
        limit = max(1, area[2] - 28)
        compact = limit < 650
        box = ttk.Frame(window, padding=10 if compact else 18)
        box.pack(fill="both", expand=True)
        fields = self.fields = []
        def field(parent, row, col, label, initial, width):
            ttk.Label(parent, text=label).grid(row=row, column=col, sticky="e", padx=4, pady=3)
            entry = ttk.Entry(parent, width=width)
            entry.insert(0, initial)
            entry.grid(row=row, column=col + 1, sticky="w", padx=4)
            fields.append(entry)
            return entry
        dist = field(box, 0, 0, "Дистанция SL", settings["initial_sl_distance"], 12)
        support = field(box, 1, 0, "Порог поддержки", settings["support_threshold"], 12)
        ladder = ttk.LabelFrame(box, text="Лот каждой ступени", padding=8)
        ladder.grid(row=2, column=0, columnspan=4, pady=10)
        groups = 4 if not compact else (3 if limit >= 435 else 2)
        rows = (24 + groups - 1) // groups
        lots = [field(ladder, i % rows, (i // rows) * 2, str(i + 1), value, 7 if compact else 8)
                for i, value in enumerate(settings["lot_schedule"])]
        error = tk.StringVar()
        ttk.Label(box, textvariable=error, wraplength=min(500,max(120, limit-36))).grid(row=3, column=0, columnspan=4, sticky="w")
        def close():
            self.editor = None
            window.destroy()
        def save():
            try:
                values = validate_settings(dict(initial_sl_distance=dist.get().strip(), support_threshold=support.get().strip(),
                                                lot_schedule=[entry.get().strip() for entry in lots]))
            except ValueError as exc:
                error.set(str(exc))
                return
            if self.act("update_settings", values, expected):
                close()
            else:
                error.set(self.message.get())
        self.save_btn = ttk.Button(box, text="Сохранить", command=save)
        self.save_btn.grid(row=4, column=1, pady=(8, 0))
        ttk.Button(box, text="Закрыть", command=close).grid(row=4, column=2, pady=(8, 0))
        window.protocol("WM_DELETE_WINDOW", close)
        self.render()
        window.update_idletasks()
        w, h = fit_window(window, area, window.winfo_reqwidth()+6, window.winfo_reqheight()+24)
        window.minsize(w, h)


def main():
    from python_supervisor import ensure_supervisor
    root = tk.Tk()
    Desktop(root, LocalClient(), ensure_supervisor)
    root.mainloop()


if __name__ == "__main__":
    main()
