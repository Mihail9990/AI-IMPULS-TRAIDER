"""Private, local, once-consumed desktop commands. No network or MT5 imports."""
from contextlib import suppress
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from uuid import uuid4

from local_settings import validate_settings
from ipc_files import open_shared_read, replace_shared, sharing_error


HEX = re.compile(r"[0-9a-f]{32}\Z")
OPS = {"start", "pause", "manual_wait", "manual_pending_wait", "manual_cancel", "update_settings"}
MODES = {"ready", "manual_wait", "pending_wait", "active", "unknown"}
STATUS_TTL = 5
REQUEST_TTL = 15


def default_root():
    return Path(os.environ["LOCALAPPDATA"]) / "AI-IMPULS-RECOVERY/control/python-v1"


def _directory(path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or not path.is_dir():
        raise ValueError("Недоступен приватный каталог управления.")
    return path


def _id(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("Недействительный идентификатор.")
    return value


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def _read(path):
    if path.is_symlink():
        raise ValueError("Недопустимая ссылка.")
    with open_shared_read(path) as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("Слишком большой файл.")
    def unique(pairs):
        row = dict(pairs)
        if len(row) != len(pairs):
            raise ValueError("Повтор поля.")
        return row
    result = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("Некорректный файл управления.")
    return result


def _write(path, value, *, new=False):
    data = _encode(value)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                if new:
                    os.link(tmp, path)
                else:
                    replace_shared(tmp, path)
                break
            except OSError as exc:
                if not sharing_error(exc) or attempt == 5:
                    raise
                time.sleep(.01)
    finally:
        _remove(tmp)


def _remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if not sharing_error(exc):
            raise


def allowed(state, action):
    mode = state["mode"]
    if action == "start":
        return not state["running"] and state["can_start"]
    if action == "pause":
        return state["running"] and state["can_pause"]
    if action == "update_settings":
        return mode == "ready" and state.get("settings_editable", False)
    if not state["running"] or not state["available"]:
        return False
    if action == "manual_cancel":
        return mode == "pending_wait" or (state["market_rearm_enabled"] and mode in {"manual_wait", "active"})
    return action in {"manual_wait", "manual_pending_wait"} and mode == "ready"


def _project(state):
    # The owner supplies a snapshot, never an arbitrary map to expose to the UI.
    public = {}
    for name in ("running", "available", "can_start", "can_pause", "mt5_connected", "market_rearm_enabled"):
        value = state.get(name, False)
        if type(value) is not bool:
            raise ValueError("Недействительный статус контроллера.")
        public[name] = value
    public["mode"] = state.get("mode", "unknown")
    if public["mode"] not in MODES:
        raise ValueError("Недействительный режим.")
    for name in ("reason", "account", "server", "symbol", "strategy_epoch", "step", "position_ticket",
                 "opposite_ticket"):
        value = state.get(name)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            public[name] = str(value)[:160]
    if public.get("reason", "").startswith("money_"):
        public.pop("reason")  # Previous owners could leave a retired status reason on disk.
    if state.get("settings") is not None:
        public["settings"] = validate_settings(state["settings"])
    if type(state.get("support_applied")) is bool:
        public["support_applied"] = state["support_applied"]
    editable = state.get("settings_editable", public["can_start"])
    public["settings_editable"] = (editable is True and not public["running"] and public["available"]
                                   and public["mode"] == "ready" and "settings" in public)
    return public


def intent_binding(state):
    fields = ("strategy_epoch", "running", "available", "mode", "market_rearm_enabled", "can_start",
              "can_pause", "settings_editable", "account", "server", "symbol", "mt5_connected", "settings")
    return hashlib.sha256(_encode([state.get(k) for k in fields])).hexdigest()


class ControlHost:
    """One OS-locked owner. Caller serializes poll() with its strategy tick."""
    def __init__(self, private_root=None):
        self.root = Path(private_root) if private_root is not None else default_root()
        self.lock = None
        self.boot = uuid4().hex
        self.generation = uuid4().hex
        self.key = secrets.token_bytes(32)
        self._previous_binding = None
        self._previous = None
        self._last_write = 0
        self._publish_retry = 0
        self._pending_receipts = {}

    def __enter__(self):
        _directory(self.root)
        name = self.root / "controller.lock"
        if name.is_symlink():
            raise ValueError("Недопустимая ссылка.")
        lock = name.open("a+b")
        try:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("Контроллер Python уже запущен.") from None
        self.lock = lock
        try:
            self.session = self.root / self.boot
            self.session.mkdir()
            self.commands = self.session / "requests"
            self.receipts = self.session / "receipts"
            self.commands.mkdir()
            self.receipts.mkdir()
            _write(self.session / "session-key.json", {"key": self.key.hex()}, new=True)
            _remove(self.root / "status.json")
            return self
        except BaseException:
            self.lock.close()
            self.lock = None
            raise

    def _state(self, strategy):
        try:
            state = _project(strategy.snapshot())
        except (OSError, TypeError, ValueError, KeyError, RuntimeError):
            state = _project(dict(reason="snapshot_unavailable"))
        binding = intent_binding(state)
        if binding != self._previous_binding:
            self.generation = uuid4().hex
            self._previous_binding = binding
        return dict(state, version=1, boot=self.boot, generation=self.generation)

    def _publish(self, state):
        now = time.monotonic()
        if now < self._publish_retry or (self._previous == state and now - self._last_write < .75):
            return
        try:
            _write(self.root / "status.json", dict(state, written=time.time()))
        except OSError as exc:
            if not sharing_error(exc):
                raise
            if not self._publish_retry:
                logging.getLogger(__name__).warning("Desktop heartbeat file is busy; publication deferred")
            self._publish_retry = time.monotonic() + .15
            return
        self._previous, self._last_write, self._publish_retry = state, now, 0

    def _consume(self, path, strategy):
        command_id = _id(path.stem)
        receipt = self.receipts / path.name
        if receipt.exists():
            _remove(path)
            return
        row = _read(path)
        mac = row.pop("mac", None)
        if not isinstance(mac, str) or not hmac.compare_digest(hmac.digest(self.key, _encode(row), "sha256").hex(), mac):
            _remove(path)
            return
        state = self._state(strategy)
        request = dict(id=command_id, boot=self.boot, generation=row.get("generation"))
        action = row.get("action")
        if isinstance(action, str) and action in OPS:
            request["action"] = action
        reason = "invalid"
        valid = False
        if (set(row) == {"version", "id", "boot", "generation", "action", "issued", "settings"}
                and type(row["version"]) is int and row["version"] == 1 and row["id"] == command_id
                and row["boot"] == self.boot and isinstance(action, str) and action in OPS
                and type(row["issued"]) in (float, int)):
            reason = "session_changed"
            if row["generation"] == state["generation"]:
                reason = "expired"
                if -.75 <= time.time() - row["issued"] <= REQUEST_TTL:
                    reason = "not_available"
                    valid = allowed(state, action) and (action == "update_settings" or row["settings"] is None)
        if valid and action == "update_settings":
            try:
                settings = validate_settings(row["settings"])
            except ValueError:
                valid, reason = False, "invalid_settings"
        if not valid:
            _write(receipt, dict(request, status="rejected", reason=reason), new=True)
        else:
            # Durable claim before the adapter: a crash leaves an explicit uncertain
            # command and a new owner starts a disjoint session without replaying it.
            _write(receipt, dict(request, status="accepted", reason="in_progress"), new=True)
            try:
                if hasattr(strategy, "dispatch_control"):
                    ok = strategy.dispatch_control(action, settings if action == "update_settings" else None,
                                                   intent_binding(state), row["issued"] + REQUEST_TTL)
                else:
                    ok = strategy.update_settings(settings) if action == "update_settings" else strategy.control(action)
                outcome = dict(request, status="applied" if ok is True else "rejected",
                               reason="confirmed" if ok is True else "owner_rejected")
            except Exception:
                outcome = dict(request, status="unknown", reason="owner_unconfirmed")
            self._pending_receipts[receipt] = outcome
            self._flush()
        _remove(path)

    def _flush(self):
        for receipt, final in list(self._pending_receipts.items()):
            try:
                _write(receipt, final)
            except OSError:
                continue
            del self._pending_receipts[receipt]

    def poll(self, strategy):
        if self.lock is None:
            raise RuntimeError("Контроллер не запущен.")
        self._flush()
        self._publish(self._state(strategy))
        with os.scandir(self.commands) as entries:
            count = 0
            for entry in entries:
                path = Path(entry.path)
                if path.suffix != ".json" or not HEX.fullmatch(path.stem) or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    self._consume(path, strategy)
                except (ValueError, TypeError, OverflowError):
                    _remove(path)
                except OSError:
                    pass
                count += 1
                if count == 8:
                    break
        self._publish(self._state(strategy))

    def __exit__(self, *exc):
        try:
            self._flush()
            with suppress(OSError):
                (self.root / "status.json").unlink(missing_ok=True)
        finally:
            self.lock.close()
            self.lock = None


class LocalClient:
    def __init__(self, private_root=None):
        self.root = Path(private_root) if private_root is not None else default_root()

    def status(self):
        try:
            _directory(self.root)
            row = _read(self.root / "status.json")
            if type(row["version"]) is not int or row["version"] != 1 or not -.75 <= time.time() - row["written"] <= STATUS_TTL:
                return None
            _id(row["boot"])
            _id(row["generation"])
            return dict(_project(row), boot=row["boot"], generation=row["generation"], written=row["written"])
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def submit(self, action, expected, settings=None):
        current = self.status()
        if not current or not expected or any(current[k] != expected.get(k) for k in ("boot", "generation")):
            raise ValueError("Состояние изменилось. Проверьте данные и повторите выбор.")
        if action not in OPS or not allowed(current, action):
            raise ValueError("Эта команда сейчас недоступна.")
        if action == "update_settings":
            settings = validate_settings(settings)
        elif settings is not None:
            raise ValueError("Команда не принимает параметры.")
        session = _directory(self.root / current["boot"])
        key = bytes.fromhex(_read(session / "session-key.json")["key"])
        if len(key) != 32:
            raise ValueError("Недействительный ключ сеанса.")
        row = dict(version=1, id=uuid4().hex, boot=current["boot"], generation=current["generation"],
                   issued=time.time(), action=action, settings=settings)
        row["mac"] = hmac.digest(key, _encode(row), hashlib.sha256).hex()
        _write(_directory(session / "requests") / (row["id"] + ".json"), row, new=True)
        return (row["boot"], row["generation"], row["id"])

    def receipt(self, handle):
        boot, generation, command = map(_id, handle)
        try:
            session = _directory(self.root / boot)
            result = _read(_directory(session / "receipts") / (command + ".json"))
        except FileNotFoundError:
            return None
        if ((result.get("boot"), result.get("generation"), result.get("id")) != (boot, generation, command)
                or result.get("status") not in {"accepted", "applied", "rejected", "unknown"}):
            raise ValueError("Ответ не соответствует запросу.")
        return result
