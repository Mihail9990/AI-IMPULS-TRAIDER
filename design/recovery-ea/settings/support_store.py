"""Server-owned settings for the next RECOVERY generation; no broker client."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

if __package__:
    from .support_settings import SupportSettings, _positive_decimal, validate_support_edit
else:
    from support_settings import SupportSettings, _positive_decimal, validate_support_edit


class SettingsUnavailable(Exception):
    """Saved settings or authoritative state could not be established."""


class SettingsConflict(Exception):
    """The owner is not idle or the panel's revision is stale."""


@dataclass(frozen=True)
class SettingsContext:
    symbol_tick_size: str
    generation_active: bool | None


@dataclass(frozen=True)
class SettingsSnapshot:
    revision: int
    settings: SupportSettings


def unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _fields(settings):
    return {"support_threshold": settings.support_threshold}


class SupportStore:
    """Single process only: inject the generation owner's same shared RLock.

    State and tick size come from the runtime owner, not the browser. Merely
    reading a command-spool state file does not meet the ownership contract.
    """

    def __init__(self, path, owner_lock, observe):
        self.path = Path(path)
        self._lock = owner_lock
        self._observe = observe

    def _context(self):
        try:
            context = self._observe()
        except (OSError, ValueError) as exc:
            raise SettingsUnavailable("runtime context unavailable") from exc
        if not isinstance(context, SettingsContext):
            raise SettingsUnavailable("runtime context unavailable")
        return context

    def _load(self):
        try:
            with self.path.open("rb") as stream:
                payload = stream.read(4097)
        except FileNotFoundError:
            # A missing parent isn't a confirmed fresh profile.
            if not self.path.parent.is_dir():
                raise SettingsUnavailable("profile directory unavailable") from None
            return SettingsSnapshot(0, SupportSettings())
        except OSError as exc:
            raise SettingsUnavailable("saved settings unavailable") from exc
        try:
            if len(payload) > 4096:
                raise ValueError("oversized file")
            doc = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_json_pairs)
            if (not isinstance(doc, dict) or set(doc) != {"version", "revision", "settings"}
                    or type(doc["version"]) is not int
                    or type(doc["revision"]) is not int or doc["revision"] <= 0
                    or not isinstance(doc["settings"], dict)):
                raise ValueError("invalid settings file")
            values = doc["settings"]
            if doc["version"] == 2 and values.keys() == {"support_threshold"}:
                threshold = values["support_threshold"]
            elif doc["version"] == 1 and values.keys() == {
                "support_first", "support_second", "support_first_enabled"
            }:
                # Retain the prior breakeven preference only; the removed
                # first-stage fields no longer contribute to any projection.
                threshold = values["support_second"]
            else:
                raise ValueError("invalid settings file format")
            _positive_decimal(threshold, "support_threshold")
            return SettingsSnapshot(doc["revision"], SupportSettings(threshold))
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise SettingsUnavailable("saved settings are invalid") from exc

    def _write(self, snapshot):
        payload = {"version": 2, "revision": snapshot.revision,
                   "settings": _fields(snapshot.settings)}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                    prefix=".support-", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise SettingsUnavailable("could not save settings") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def projection(self):
        with self._lock:
            snapshot = self._load()
            try:
                editable = self._context().generation_active is False
            except SettingsUnavailable:
                editable = False
            return self._projection(snapshot, editable)

    @staticmethod
    def _projection(snapshot, editable):
        return {"revision": snapshot.revision, "editable": editable,
                **_fields(snapshot.settings)}

    def save(self, body):
        if not isinstance(body, dict) or set(body) != {
            "revision", "support_threshold"
        } or type(body["revision"]) is not int or body["revision"] < 0:
            raise ValueError("invalid settings edit")
        with self._lock:
            context = self._context()
            if context.generation_active is not False:
                raise SettingsConflict("RECOVERY is not known idle")
            current = self._load()
            if current.revision != body["revision"]:
                raise SettingsConflict("settings revision changed")
            candidate = validate_support_edit(
                {k: v for k, v in body.items() if k != "revision"},
                generation_active=False, symbol_tick_size=context.symbol_tick_size)
            updated = current if candidate == current.settings else SettingsSnapshot(
                current.revision + 1, candidate)
            if updated is not current:
                self._write(updated)
            return self._projection(updated, True)

    @contextmanager
    def locked_next_generation(self):
        """Owner reserves generation inside this lock, using the yielded snapshot."""
        with self._lock:
            context = self._context()
            if context.generation_active is not False:
                raise SettingsConflict("RECOVERY is not known idle")
            snapshot = self._load()
            yield snapshot.revision, snapshot.settings.to_ticks(context.symbol_tick_size)
