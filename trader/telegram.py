from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
import time
import requests


LOG = logging.getLogger(__name__)


class Telegram:
    COMMANDS = [
        ("status", "Состояние цикла"),
        ("start", "Запустить свечной фильтр"),
        ("stop", "Пауза после текущего цикла"),
        ("positions", "Открытые позиции"),
        ("orders", "Trigger-ордера"),
        ("pnl", "Прибыль и убыток"),
        ("dealhistory", "История сделок по dealId"),
        ("profit200", "Целевой profit следующих 200 циклов"),
        ("cycleinfo", "Последние события"),
        ("recover", "Повторить защиту позиций"),
        ("automode", "Выйти из ручного режима"),
        ("sendlog", "Отправить диагностический файл"),
        ("menu", "Показать кнопочную клавиатуру"),
        ("hidemenu", "Свернуть кнопочную клавиатуру"),
        ("help", "Все команды"),
    ]
    KEYBOARD = {
        "keyboard": [
            [{"text": "/status"}, {"text": "/start"}, {"text": "/stop"}],
            [{"text": "/positions"}, {"text": "/orders"}, {"text": "/pnl"}],
            [{"text": "/dealhistory"}],
            [{"text": "/profit200 0.4"}],
            [{"text": "/cycleinfo"}, {"text": "/help"}],
            [{"text": "/recover"}, {"text": "/automode"}],
            [{"text": "/sendlog"}],
            [{"text": "/hidemenu"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
    }

    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, str(chat_id)
        self.offset = 0
        # Command polling and report delivery use independent backoffs. A getUpdates
        # timeout must never suppress trading reports.
        self.poll_unavailable_until = 0.0
        self.send_unavailable_until = 0.0
        self._outbox: deque[tuple[str, str]] = deque()
        self.base = f"https://api.telegram.org/bot{token}" if token else ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, show_menu: bool = False, hide_menu: bool = False) -> bool:
        """Queue a report without blocking the broker-action path on Telegram I/O."""
        LOG.info("TELEGRAM SEND text=%s", text)
        if not self.enabled:
            return False
        menu_action = "show" if show_menu else "hide" if hide_menu else "none"
        self._outbox.append((text, menu_action))
        return True

    def flush_pending(self, max_messages: int = 10) -> int:
        """Deliver queued reports in order and retain them after network failures."""
        if not self.enabled or time.monotonic() < self.send_unavailable_until:
            return 0
        delivered = 0
        while self._outbox and delivered < max_messages:
            text, menu_action = self._outbox[0]
            payload = {"chat_id": self.chat_id, "text": text}
            if menu_action == "show":
                payload["reply_markup"] = self.KEYBOARD
            elif menu_action == "hide":
                payload["reply_markup"] = {"remove_keyboard": True}
            try:
                requests.post(
                    self.base + "/sendMessage", json=payload, timeout=(3, 5)
                ).raise_for_status()
            except requests.RequestException:
                self.send_unavailable_until = time.monotonic() + 5
                LOG.warning(
                    "Telegram send failed; report retained in outbox (pending=%s)",
                    len(self._outbox), exc_info=True,
                )
                break
            self._outbox.popleft()
            delivered += 1
        if delivered:
            LOG.info("Telegram outbox delivered=%s pending=%s", delivered, len(self._outbox))
        return delivered

    @property
    def pending_reports(self) -> int:
        return len(self._outbox)

    def send_document(self, path: str) -> bool:
        file = Path(path)
        LOG.info("TELEGRAM SEND DOCUMENT path=%s size=%s", file, file.stat().st_size)
        if not self.enabled:
            return False
        if time.monotonic() < self.send_unavailable_until:
            return False
        try:
            with file.open("rb") as document:
                requests.post(
                    self.base + "/sendDocument",
                    data={"chat_id": self.chat_id}, files={"document": document}, timeout=60,
                ).raise_for_status()
            return True
        except requests.RequestException:
            self.send_unavailable_until = time.monotonic() + 5
            LOG.warning("Telegram document upload failed", exc_info=True)
            return False

    def install_commands(self) -> None:
        if not self.enabled:
            return
        if time.monotonic() < self.send_unavailable_until:
            return
        try:
            requests.post(
                self.base + "/setMyCommands",
                json={"commands": [{"command": command, "description": description}
                                   for command, description in self.COMMANDS]},
                timeout=2,
            ).raise_for_status()
        except requests.RequestException:
            self.send_unavailable_until = time.monotonic() + 5
            LOG.warning("Telegram command menu installation failed", exc_info=True)

    def commands(self) -> list[str]:
        if not self.enabled:
            return []
        if time.monotonic() < self.poll_unavailable_until:
            return []
        try:
            response = requests.get(
                self.base + "/getUpdates",
                params={"offset": self.offset, "timeout": 0}, timeout=2,
            )
            response.raise_for_status()
        except requests.RequestException:
            self.poll_unavailable_until = time.monotonic() + 5
            LOG.warning("Telegram polling failed; trading loop will continue", exc_info=True)
            return []
        commands = []
        for update in response.json()["result"]:
            self.offset = max(self.offset, update["update_id"] + 1)
            message = update.get("message", {})
            if str(message.get("chat", {}).get("id")) == self.chat_id and "text" in message:
                commands.append(message["text"].strip())
        if commands:
            LOG.info("TELEGRAM COMMANDS received=%s", commands)
        return commands

    def discard_pending(self) -> None:
        """Forget commands sent before this process started, preventing restart replays."""
        if not self.enabled:
            return
        if time.monotonic() < self.poll_unavailable_until:
            return
        try:
            response = requests.get(
                self.base + "/getUpdates", params={"offset": -1, "timeout": 0}, timeout=2
            )
            response.raise_for_status()
        except requests.RequestException:
            self.poll_unavailable_until = time.monotonic() + 5
            LOG.warning("Could not discard pending Telegram updates", exc_info=True)
            return
        updates = response.json()["result"]
        if updates:
            self.offset = max(update["update_id"] for update in updates) + 1
