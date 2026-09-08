from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import threading
import time
import requests


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Delivery:
    kind: str
    value: str = ""
    menu_action: str = "none"


class Telegram:
    """Telegram transport whose network I/O never runs on the trading thread.

    Worker threads only move immutable command/report values.  The main Bot thread remains the
    sole owner of Capital API calls and CycleState mutations.
    """

    COMMANDS = [
        ("status", "Состояние цикла"), ("start", "Запустить свечной фильтр"),
        ("stop", "Пауза после текущего цикла"), ("positions", "Открытые позиции"),
        ("orders", "Trigger-ордера"), ("pnl", "Прибыль и убыток"),
        ("dealhistory", "История сделок по dealId"),
        ("profit200", "Целевой profit следующих 200 циклов"),
        ("cycleinfo", "Последние события"), ("recover", "Повторить защиту позиций"),
        ("automode", "Выйти из ручного режима"), ("sendlog", "Отправить диагностический файл"),
        ("menu", "Показать кнопочную клавиатуру"),
        ("hidemenu", "Свернуть кнопочную клавиатуру"), ("help", "Все команды"),
    ]
    KEYBOARD = {
        "keyboard": [
            [{"text": "/status"}, {"text": "/start"}, {"text": "/stop"}],
            [{"text": "/positions"}, {"text": "/orders"}, {"text": "/pnl"}],
            [{"text": "/dealhistory"}], [{"text": "/profit200 0.4"}],
            [{"text": "/cycleinfo"}, {"text": "/help"}],
            [{"text": "/recover"}, {"text": "/automode"}],
            [{"text": "/sendlog"}], [{"text": "/hidemenu"}],
        ],
        "resize_keyboard": True, "is_persistent": False,
    }

    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, str(chat_id)
        self.offset = 0
        self.base = f"https://api.telegram.org/bot{token}" if token else ""
        self._lock = threading.Lock()
        self._wake_sender = threading.Event()
        self._stop = threading.Event()
        self._outbox: deque[_Delivery] = deque()
        self._commands: deque[str] = deque()
        self._poll_thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        self._started = False
        self._startup_discard = self.offset == 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def start(self) -> None:
        if not self.enabled or self._started:
            return
        self._started = True
        self._startup_discard = self.offset == 0
        self._stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="telegram-poll", daemon=True
        )
        self._send_thread = threading.Thread(
            target=self._send_loop, name="telegram-send", daemon=True
        )
        self._poll_thread.start()
        self._send_thread.start()
        LOG.info("TELEGRAM workers started offset=%s", self.offset)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake_sender.set()
        for thread in (self._poll_thread, self._send_thread):
            if thread:
                thread.join(timeout)

    def send(self, text: str, show_menu: bool = False, hide_menu: bool = False) -> bool:
        LOG.info("TELEGRAM QUEUE text=%s", text)
        if not self.enabled:
            return False
        action = "show" if show_menu else "hide" if hide_menu else "none"
        self._enqueue(_Delivery("message", text, action))
        return True

    def send_document(self, path: str) -> bool:
        file = Path(path)
        LOG.info("TELEGRAM QUEUE DOCUMENT path=%s size=%s", file, file.stat().st_size)
        if not self.enabled:
            return False
        self._enqueue(_Delivery("document", str(file)))
        return True

    def install_commands(self) -> None:
        if self.enabled:
            self._enqueue(_Delivery("menu"))

    def commands(self) -> list[str]:
        """Drain commands already received by the poll worker; never perform network I/O."""
        with self._lock:
            commands = list(self._commands)
            self._commands.clear()
        return commands

    @property
    def pending_reports(self) -> int:
        with self._lock:
            return len(self._outbox)

    def _enqueue(self, delivery: _Delivery) -> None:
        with self._lock:
            self._outbox.append(delivery)
            pending = len(self._outbox)
        if pending in {100, 500, 1000}:
            LOG.warning("TELEGRAM outbox backlog pending=%s", pending)
        self._wake_sender.set()

    def _poll_loop(self) -> None:
        delay = 0.0
        while not self._stop.wait(delay):
            try:
                with self._lock:
                    offset = self.offset
                discard = self._startup_discard
                params = {"offset": -1 if discard else offset, "timeout": 20}
                started = time.monotonic()
                response = requests.get(self.base + "/getUpdates", params=params, timeout=(5, 25))
                response.raise_for_status()
                updates = response.json().get("result", [])
                received: list[str] = []
                with self._lock:
                    for update in updates:
                        self.offset = max(self.offset, int(update["update_id"]) + 1)
                        # offset=0 means startup discard: never replay a command predating process.
                        if discard:
                            continue
                        message = update.get("message", {})
                        if (str(message.get("chat", {}).get("id")) == self.chat_id
                                and "text" in message):
                            command = str(message["text"]).strip()
                            self._commands.append(command)
                            received.append(command)
                    self._startup_discard = False
                if received:
                    LOG.info("TELEGRAM COMMANDS queued=%s", received)
                LOG.debug("TELEGRAM poll completed elapsed=%.3fs", time.monotonic() - started)
                # Successful short responses should not create a busy loop if Telegram elects not
                # to hold the requested long poll (proxy/server peculiarities on Android).
                delay = 0.1
            except Exception as exc:
                LOG.warning("TELEGRAM poll unavailable; retry in 5s: %s", exc)
                delay = 5.0

    def _send_loop(self) -> None:
        retry = 0.0
        while not self._stop.is_set():
            self._wake_sender.wait(1.0)
            self._wake_sender.clear()
            if retry and self._stop.wait(retry):
                break
            retry = 0.0
            while not self._stop.is_set():
                with self._lock:
                    item = self._outbox[0] if self._outbox else None
                if item is None:
                    break
                try:
                    self._deliver(item)
                except FileNotFoundError as exc:
                    LOG.error("TELEGRAM document no longer exists; dropping delivery: %s", exc)
                    with self._lock:
                        if self._outbox and self._outbox[0] is item:
                            self._outbox.popleft()
                    continue
                except Exception as exc:
                    with self._lock:
                        pending = len(self._outbox)
                    LOG.warning(
                        "TELEGRAM delivery unavailable; retained pending=%s retry in 5s: %s",
                        pending, exc,
                    )
                    retry = 5.0
                    break
                with self._lock:
                    if self._outbox and self._outbox[0] is item:
                        self._outbox.popleft()
                    pending = len(self._outbox)
                LOG.info("TELEGRAM delivered kind=%s pending=%s", item.kind, pending)

    def _deliver(self, item: _Delivery) -> None:
        if item.kind == "message":
            payload = {"chat_id": self.chat_id, "text": item.value}
            if item.menu_action == "show":
                payload["reply_markup"] = self.KEYBOARD
            elif item.menu_action == "hide":
                payload["reply_markup"] = {"remove_keyboard": True}
            response = requests.post(self.base + "/sendMessage", json=payload, timeout=(5, 15))
        elif item.kind == "document":
            with Path(item.value).open("rb") as document:
                response = requests.post(
                    self.base + "/sendDocument", data={"chat_id": self.chat_id},
                    files={"document": document}, timeout=(5, 120),
                )
        else:
            response = requests.post(
                self.base + "/setMyCommands",
                json={"commands": [{"command": command, "description": description}
                                    for command, description in self.COMMANDS]}, timeout=(5, 15),
            )
        response.raise_for_status()
