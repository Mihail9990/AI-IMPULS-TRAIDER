from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
import gzip
from pathlib import Path
import shutil
import tempfile
import threading
import time
import requests


LOG = logging.getLogger(__name__)


@dataclass
class _Delivery:
    kind: str
    value: str = ""
    menu_action: str = "none"
    compress: bool = False
    attempts: int = 0
    upload_path: str = ""
    delete_upload: bool = False


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
        self._wake_messages = threading.Event()
        self._wake_documents = threading.Event()
        self._stop = threading.Event()
        self._messages: deque[_Delivery] = deque()
        self._documents: deque[_Delivery] = deque()
        self._commands: deque[str] = deque()
        self._poll_thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        self._document_thread: threading.Thread | None = None
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
        self._document_thread = threading.Thread(
            target=self._document_loop, name="telegram-document", daemon=True
        )
        self._poll_thread.start()
        self._send_thread.start()
        self._document_thread.start()
        LOG.info("TELEGRAM workers started offset=%s", self.offset)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake_messages.set()
        self._wake_documents.set()
        for thread in (self._poll_thread, self._send_thread, self._document_thread):
            if thread:
                thread.join(timeout)

    def send(self, text: str, show_menu: bool = False, hide_menu: bool = False) -> bool:
        LOG.info("TELEGRAM QUEUE text=%s", text)
        if not self.enabled:
            return False
        action = "show" if show_menu else "hide" if hide_menu else "none"
        self._enqueue(_Delivery("message", text, action))
        return True

    def send_document(self, path: str, *, compress: bool = False) -> bool:
        file = Path(path)
        LOG.info("TELEGRAM QUEUE DOCUMENT path=%s size=%s", file, file.stat().st_size)
        if not self.enabled:
            return False
        self._enqueue(_Delivery("document", str(file), compress=compress))
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
            return len(self._messages) + len(self._documents)

    def _enqueue(self, delivery: _Delivery) -> None:
        with self._lock:
            queue = self._documents if delivery.kind == "document" else self._messages
            queue.append(delivery)
            pending = len(self._messages) + len(self._documents)
        if pending in {100, 500, 1000}:
            LOG.warning("TELEGRAM outbox backlog pending=%s", pending)
        (self._wake_documents if delivery.kind == "document" else self._wake_messages).set()

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
                LOG.warning(
                    "TELEGRAM poll unavailable; retry in 5s: %s", self._safe_error(exc)
                )
                delay = 5.0

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            self._wake_messages.wait(1.0)
            self._wake_messages.clear()
            item = self._peek(self._messages)
            if item is None:
                continue
            started = time.monotonic()
            item.attempts += 1
            LOG.info("TELEGRAM delivery started kind=%s attempt=%s", item.kind, item.attempts)
            try:
                self._deliver_message(item)
            except Exception as exc:
                permanent, delay = self._failure_policy(exc, item.attempts, document=False)
                LOG.warning(
                    "TELEGRAM delivery failed kind=%s attempt=%s elapsed=%.3fs permanent=%s "
                    "next_retry=%ss error=%s",
                    item.kind, item.attempts, time.monotonic() - started, permanent,
                    0 if permanent else delay, self._safe_error(exc),
                )
                if permanent:
                    self._remove(self._messages, item)
                elif not self._stop.wait(delay):
                    self._wake_messages.set()
                continue
            self._remove(self._messages, item)
            LOG.info(
                "TELEGRAM delivery succeeded kind=%s attempt=%s elapsed=%.3fs pending=%s",
                item.kind, item.attempts, time.monotonic() - started, self.pending_reports,
            )

    def _document_loop(self) -> None:
        while not self._stop.is_set():
            self._wake_documents.wait(1.0)
            self._wake_documents.clear()
            item = self._peek(self._documents)
            if item is None:
                continue
            try:
                self._prepare_document(item)
            except Exception as exc:
                LOG.error("TELEGRAM document preparation failed permanent=true error=%s", self._safe_error(exc))
                self._finish_document(item)
                self.send("⚠️ Не удалось подготовить диагностический файл к отправке.")
                continue
            item.attempts += 1
            upload = Path(item.upload_path)
            started = time.monotonic()
            LOG.info(
                "TELEGRAM document upload started name=%s size=%s attempt=%s",
                upload.name, upload.stat().st_size, item.attempts,
            )
            try:
                self._deliver_document(item)
            except Exception as exc:
                permanent, delay = self._failure_policy(exc, item.attempts, document=True)
                LOG.warning(
                    "TELEGRAM document upload failed name=%s size=%s attempt=%s elapsed=%.3fs "
                    "permanent=%s next_retry=%ss error=%s",
                    upload.name, upload.stat().st_size, item.attempts,
                    time.monotonic() - started, permanent, 0 if permanent else delay,
                    self._safe_error(exc),
                )
                if permanent:
                    self._finish_document(item)
                    self.send(
                        f"⚠️ Файл {Path(item.value).name} не отправлен после "
                        f"{item.attempts} попыток. Получите его вручную из папки проекта."
                    )
                elif not self._stop.wait(delay):
                    self._wake_documents.set()
                continue
            self._finish_document(item)
            LOG.info(
                "TELEGRAM document upload succeeded name=%s attempt=%s elapsed=%.3fs pending=%s",
                upload.name, item.attempts, time.monotonic() - started, self.pending_reports,
            )
            self.send(f"✅ Диагностический файл отправлен: {Path(item.value).name}")

    def _peek(self, queue: deque[_Delivery]) -> _Delivery | None:
        with self._lock:
            return queue[0] if queue else None

    def _remove(self, queue: deque[_Delivery], item: _Delivery) -> None:
        with self._lock:
            if queue and queue[0] is item:
                queue.popleft()

    def _finish_document(self, item: _Delivery) -> None:
        self._remove(self._documents, item)
        if item.delete_upload and item.upload_path:
            Path(item.upload_path).unlink(missing_ok=True)

    def _prepare_document(self, item: _Delivery) -> None:
        if item.upload_path:
            return
        source = Path(item.value)
        if not source.is_file():
            raise FileNotFoundError(source)
        if not item.compress:
            item.upload_path = str(source)
            return
        descriptor, name = tempfile.mkstemp(
            prefix="telegram-", suffix=f"-{source.name}.gz", dir=source.parent
        )
        try:
            with open(descriptor, "wb", closefd=True) as raw, gzip.GzipFile(
                    filename=source.name, mode="wb", fileobj=raw, compresslevel=6) as compressed, \
                    source.open("rb") as original:
                shutil.copyfileobj(original, compressed, length=256 * 1024)
        except Exception:
            Path(name).unlink(missing_ok=True)
            raise
        item.upload_path = name
        item.delete_upload = True
        LOG.info(
            "TELEGRAM document compressed source=%s source_size=%s upload=%s upload_size=%s",
            source.name, source.stat().st_size, Path(name).name, Path(name).stat().st_size,
        )

    def _failure_policy(self, exc: Exception, attempt: int, *, document: bool) -> tuple[bool, float]:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            response = exc.response
            try:
                recommended = float(response.json().get("parameters", {}).get("retry_after", 0))
            except (AttributeError, TypeError, ValueError, requests.RequestException):
                recommended = 0
            if not recommended:
                try:
                    recommended = float(response.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    recommended = 0
            return bool(document and attempt >= 5), max(1.0, recommended)
        permanent_status = status is not None and status not in {408, 425, 429} and status < 500
        max_attempts = 5 if document else 0
        permanent = permanent_status or bool(max_attempts and attempt >= max_attempts)
        return permanent, min(300.0, 5.0 * (2 ** min(attempt - 1, 6)))

    def _safe_error(self, exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}"
        if self.token:
            text = text.replace(self.token, "<redacted>")
        return text[:500]

    def _deliver_message(self, item: _Delivery) -> None:
        if item.kind == "message":
            payload = {"chat_id": self.chat_id, "text": item.value}
            if item.menu_action == "show":
                payload["reply_markup"] = self.KEYBOARD
            elif item.menu_action == "hide":
                payload["reply_markup"] = {"remove_keyboard": True}
            response = requests.post(self.base + "/sendMessage", json=payload, timeout=(5, 15))
        else:
            response = requests.post(
                self.base + "/setMyCommands",
                json={"commands": [{"command": command, "description": description}
                                    for command, description in self.COMMANDS]}, timeout=(5, 15),
            )
        response.raise_for_status()

    def _deliver_document(self, item: _Delivery) -> None:
        with Path(item.upload_path).open("rb") as document:
            response = requests.post(
                self.base + "/sendDocument", data={"chat_id": self.chat_id},
                # During multipart upload urllib3 still uses the socket's connect timeout for
                # writes. A tuple such as (5, 180) therefore aborts a slow Android upload after
                # roughly five seconds before Telegram can send a response. One generous socket
                # timeout covers connect, request-body writes and response reads. This worker is
                # independent from both trading and ordinary Telegram messages.
                files={"document": document}, timeout=180,
            )
        response.raise_for_status()
