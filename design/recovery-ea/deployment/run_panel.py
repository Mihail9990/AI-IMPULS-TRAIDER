"""Read-only launcher preflight for the clean RECOVERY VPS web panel."""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from uuid import UUID


HOST = "127.0.0.1"
PORT = 8767
REPO = Path(__file__).resolve().parents[3]
CHANNEL_MODULE = "design.recovery-ea.channel.channel"
PANEL_MODULE = "design.recovery-ea.channel.runtime_panel"
PROFILE_MAX = 16384


class ConfigError(ValueError):
    """Errors use fixed safe messages; no supplied value belongs in logs."""


@dataclass(frozen=True)
class LaunchConfig:
    channel_root: Path
    origin: str
    scope: str
    key: str = field(repr=False)


def _link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def channel_root_from_data_dir(path: Path) -> Path:
    """Resolve one explicitly chosen MT5 data directory with no fallback scan."""
    try:
        selected = path.expanduser()
    except (OSError, RuntimeError):
        raise ConfigError("Не удалось разрешить каталог данных MT5.") from None
    if ".." in selected.parts or not selected.is_absolute() or str(selected).startswith(("\\\\", "//")):
        raise ConfigError("Укажи абсолютный локальный путь к каталогу данных MT5.")
    try:
        candidate = selected
        if _link(candidate) or not candidate.is_dir():
            raise ConfigError("Каталог данных MT5 не найден или является ссылкой.")
        for segment in ("MQL5", "Files", "RECOVERY_PANEL", "sessions"):
            candidate = candidate / segment
            if _link(candidate) or not candidate.is_dir():
                raise ConfigError("Рабочая папка EA не готова. Проверен только указанный каталог MT5.")
        return candidate.parent.resolve(strict=True)
    except OSError:
        raise ConfigError("Не удалось прочитать рабочую папку EA.") from None


def parse_origin(value: str) -> str:
    if not value or not value.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ConfigError("Укажи точный публичный HTTPS origin без пути.")
    try:
        origin = urlsplit(value)
        if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                or origin.password is not None or origin.path or origin.query or origin.fragment
                or origin.port in (0, 443) or "%" in origin.netloc or "\\" in origin.netloc
                or value != "https://" + origin.netloc or origin.netloc != origin.netloc.lower()):
            raise ValueError()
        host = f"[{origin.hostname}]" if ":" in origin.hostname else origin.hostname
        if origin.netloc != host + (f":{origin.port}" if origin.port is not None else ""):
            raise ValueError()
    except ValueError:
        raise ConfigError("Укажи точный публичный HTTPS origin без пути.") from None
    return value


def _unique(items):
    result = {}
    for name, value in items:
        if name in result:
            raise ValueError("duplicate field")
        result[name] = value
    return result


def _profile_credentials(profile: Path):
    try:
        if _link(profile) or not profile.is_file():
            raise ValueError()
        with profile.open("rb") as stream:
            raw = stream.read(PROFILE_MAX + 1)
        if len(raw) > PROFILE_MAX:
            raise ValueError()
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        if not isinstance(doc, dict) or type(doc.get("schema")) is not int or doc["schema"] != 1:
            raise ValueError()
        return doc.get("install_id"), doc.get("panel_key")
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigError("Приватный профиль RECOVERY не найден или некорректен.") from None


def read_config(data_dir: Path, origin: str, *, profile: Path | None,
                env: Mapping[str, str]) -> LaunchConfig:
    root = channel_root_from_data_dir(data_dir)
    origin = parse_origin(origin)
    if profile is not None:
        try:
            profile = profile.expanduser()
        except (OSError, RuntimeError):
            raise ConfigError("Не удалось разрешить путь к приватному профилю.") from None
        scope, key = _profile_credentials(profile)
    else:
        scope, key = env.get("RECOVERY_SCOPE_ID"), env.get("RECOVERY_PANEL_KEY")
    try:
        scope_ok = isinstance(scope, str) and str(UUID(scope)) == scope
    except ValueError:
        scope_ok = False
    if not scope_ok:
        raise ConfigError("Не задан корректный ID профиля RECOVERY.")
    if (not isinstance(key, str) or not 32 <= len(key) <= 256
            or not key.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in key)):
        raise ConfigError("Не задан корректный ключ панели.")
    return LaunchConfig(root, origin, scope, key)


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        # Never echo arbitrary argument text (even if a key was pasted by mistake).
        self.exit(2, "Неверные аргументы RECOVERY. Используй --help.\n")


def parser():
    args = QuietParser(description="Запуск веб-панели RECOVERY только на 127.0.0.1:8767.")
    args.add_argument("--mt5-data-dir", required=True, type=Path,
                      help="Каталог данных MT5 из Файл → Открыть каталог данных")
    args.add_argument("--origin", required=True, help="Точный публичный HTTPS origin Caddy")
    args.add_argument("--profile", type=Path, help="Приватный recovery-profile.json, если не используется окружение")
    args.add_argument("--check", action="store_true",
                      help="Локально проверить профиль и состояние EA, не запуская веб-сервер")
    return args


def repo_modules():
    # When invoked as python path/to/run_panel.py, sys.path[0] is deployment/.
    # Put the checkout itself first; no broker SDK required by either import.
    if not sys.path or sys.path[0] != str(REPO):
        sys.path.insert(0, str(REPO))
    return import_module(CHANNEL_MODULE).Channel, import_module(PANEL_MODULE).create_panel_app


def serve(config: LaunchConfig, *, check=False, modules=repo_modules, run_server=None) -> int:
    try:
        Channel, create_panel = modules()
        channel = Channel(config.channel_root, config.scope)
        app = create_panel(config.key, config.origin, channel)  # no live settings owner inferred
    except (ImportError, OSError, TypeError, ValueError):
        print("Не удалось подготовить локальную панель RECOVERY.", file=sys.stderr)
        return 2
    if check:
        try:
            known = channel.state()["mode"] != "unknown"
        except (KeyError, OSError, TypeError, ValueError):
            known = False
        if not known:
            print("Локальная конфигурация прочитана. Состояние EA не подтверждено.")
            return 3
        print("Локальная конфигурация прочитана. Запись состояния EA доступна.")
        return 0
    if run_server is None:
        try:
            run_server = import_module("uvicorn").run
        except ImportError:
            print("Для запуска локальной панели нужен Uvicorn.", file=sys.stderr)
            return 2
    try:
        run_server(app, host=HOST, port=PORT, workers=1, access_log=False,
                   proxy_headers=True, forwarded_allow_ips=HOST)
    except (OSError, TypeError, ValueError):
        print("Не удалось запустить локальный веб-сервер RECOVERY.", file=sys.stderr)
        return 2
    return 0


def main(argv: Sequence[str] | None = None, *, env=None) -> int:
    args = parser().parse_args(argv)
    try:
        config = read_config(args.mt5_data_dir, args.origin, profile=args.profile,
                             env=os.environ if env is None else env)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    return serve(config, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
