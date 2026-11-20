"""Local Windows setup and foreground launcher for the packaged RECOVERY roles."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from uuid import UUID

sys.dont_write_bytecode = True
APP = Path(__file__).resolve().parent
DEPLOY = APP / "design" / "recovery-ea" / "deployment"
sys.path.insert(0, str(DEPLOY))
from fresh_profile import initialize_fresh_profile
from role_launch import RoleLaunch, prepare_role_launch
from run_panel import ConfigError, parse_origin

SCHEDULE = "0.01,0.01,0.01,0.01,0.01,0.02,0.02,0.02,0.03,0.03,0.04,0.04,0.05,0.05,0.06,0.06,0.07,0.07,0.08,0.08,0.09,0.09,0.10,0.10"
READY = {"verifier": "RECOVERY verifier ready to observe; chart ID ",
         "watcher": "RECOVERY price watcher ready to observe; chart ID "}


class SetupError(ValueError):
    pass


def label(value: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value)
            or re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", value, re.I)):
        raise SetupError("Выберите имя из латинских букв, цифр, дефиса или подчёркивания.")
    return value


def local(path: Path) -> Path:
    """Never follow redirected profile/session files or use network/device paths."""
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive) or ".." in path.parts:
        raise SetupError("Нужен абсолютный локальный путь на диске Windows.")
    for item in (*reversed(path.parents), path):
        try:
            attrs = item.lstat().st_file_attributes
        except FileNotFoundError:
            continue
        if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise SetupError("В выбранном пути есть ссылка или точка повторного анализа.")
    return path


def profiles() -> Path:
    return local(Path(os.environ["LOCALAPPDATA"]) / "AI-IMPULS-RECOVERY" / "profiles")


def private_acl(directory: Path, mode: str) -> None:
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    args = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(APP / "private_acl.ps1"), "-Directory", str(local(directory)), "-Mode", mode]
    if subprocess.run(args, capture_output=True).returncode:
        raise SetupError("Не удалось подтвердить приватные права NTFS каталога профиля.")


def read_json(path: Path) -> dict:
    with local(path).open("rb") as stream:
        raw = stream.read(16_385)
    if len(raw) > 16_384:
        raise SetupError("Слишком большой файл конфигурации.")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise SetupError("Повреждена конфигурация.")
    return value


def save_json(path: Path, data: dict) -> None:
    # Called only inside an unpublished, newly created private directory.
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def mt5_root(value: str) -> Path:
    path = local(Path(value))
    if not local(path / "MQL5").is_dir():
        raise SetupError("В указанном каталоге данных MT5 отсутствует MQL5.")
    return path


def matches(profile: dict) -> None:
    if (profile.get("recovery_launcher") != 1 or profile.get("schema") != 1
            or str(UUID(profile["install_id"])) != profile["install_id"]
            or not isinstance(profile["panel_key"], str) or len(profile["panel_key"]) < 32):
        raise SetupError("Выбранная папка не содержит приватный профиль этого мастера.")


def account_args(mt5: dict) -> dict:
    return {k: mt5[k] for k in ("login", "server", "symbol", "magic")}


def profile_at(name: str) -> tuple[Path, dict]:
    root = local(profiles() / label(name))
    private_acl(root, "Verify")
    doc = read_json(root / "recovery-profile.json")
    matches(doc)
    return root, doc


def set_bytes(values: dict) -> bytes:
    lines = []
    for key, value in values.items():
        if any(char in str(value) for char in "\r\n\x00") or "||" in str(value):
            raise SetupError("Параметр MT5 содержит недопустимый разделитель файла .set.")
        lines.append(f"{key}={value}\r\n")
    return b"\xff\xfe" + "".join(lines).encode("utf-16-le")


def observer_sets(plan: RoleLaunch) -> dict[str, bytes]:
    return {"RecoveryVerifier.set": set_bytes(plan.verifier_inputs()),
            "RecoveryPriceWatcher.set": set_bytes(plan.watcher_inputs())}


def prepare_profile(name: str, data_dir: str, login: int, server: str, symbol: str,
                    magic: int, origin: str) -> tuple[Path, dict]:
    root = local(profiles() / label(name))
    mt5 = dict(login=login, server=server, symbol=symbol, magic=magic, data_dir=str(mt5_root(data_dir)))
    origin = parse_origin(origin)
    if root.exists():
        root, doc = profile_at(name)
        if doc.get("mt5") != mt5 or doc.get("panel_origin") != origin:
            raise SetupError("У этого имени уже другая привязка. Выберите новое имя профиля.")
        return root, doc
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".new-profile-", dir=root.parent) as staging:
        private = Path(staging) / "profile"
        private.mkdir(mode=0o700)
        private_acl(private, "Create")
        target = initialize_fresh_profile(private)
        doc = read_json(target)
        # Validate with the same rules that determine the actual MT5 presets.
        observer_sets(prepare_role_launch(doc["install_id"], **account_args(mt5)))
        doc.update(mt5=mt5, panel_origin=origin, recovery_launcher=1)
        save_json(target, doc)
        (private / "sessions").mkdir()
        try:
            private.rename(root)  # Windows fails if another owner has already published it.
        except FileExistsError:
            return prepare_profile(name, data_dir, login, server, symbol, magic, origin)
    return root, doc


def load_session(directory: Path, doc: dict) -> RoleLaunch:
    session = read_json(directory / "role-session.json")
    if session.get("schema") != 1 or session.get("install_id") != doc["install_id"]:
        raise SetupError("Сеанс не соответствует выбранному профилю.")
    plan = RoleLaunch(doc["install_id"], session["instance_seed"], *(
        doc["mt5"][k] for k in ("login", "server", "symbol", "magic")))
    for filename, expected in observer_sets(plan).items():
        if local(directory / filename).read_bytes() != expected:
            raise SetupError("Пресеты выбранного сеанса повреждены. Подготовьте новый сеанс.")
    return plan


def prepare_session(root: Path, doc: dict, name: str) -> Path:
    directory = local(root / "sessions" / label(name))
    if not directory.exists():
        plan = prepare_role_launch(doc["install_id"], **account_args(doc["mt5"]))
        with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".new-session-") as staging:
            new = Path(staging) / "session"
            new.mkdir()
            save_json(new / "role-session.json", {"schema": 1, "install_id": doc["install_id"],
                                                  "instance_seed": plan.instance_seed})
            for filename, data in observer_sets(plan).items():
                (new / filename).write_bytes(data)
            try:
                new.rename(directory)
            except FileExistsError:
                pass
    load_session(directory, doc)
    return directory


def ready_id(role: str, line: str, data_dir: Path) -> int:
    prefix = READY[role]
    evidence = re.search(re.escape(prefix) + r"([1-9][0-9]{0,18})\s*$", line)
    if evidence:
        value = int(evidence[1])
    elif re.fullmatch(r"[1-9][0-9]{0,18}", line):
        value = int(line)
        # MT5 sometimes buffers today's Experts file. Its exact ready line can
        # also be copied straight from the terminal; stale time cutoffs are unnecessary.
        logs = local(data_dir / "MQL5" / "Logs")
        expected = prefix + line
        found = False
        if logs.is_dir():
            for path in sorted(logs.glob("*.log"), reverse=True):
                with local(path).open("rb") as raw:
                    head = raw.read(4)
                encoding = ("utf-16" if head.startswith((b"\xff\xfe", b"\xfe\xff"))
                            else "utf-16-le" if head[1:2] == b"\0" else "utf-8-sig")
                with path.open(encoding=encoding, errors="replace") as log:
                    if any(entry.rstrip().endswith(expected) for entry in log):
                        found = True
                        break
        if not found:
            raise SetupError("Для этого номера нет строки готовности. Скопируйте полную строку из «Эксперты».")
    else:
        raise SetupError("Нужна строка готовности указанной роли из журнала «Эксперты».")
    if value > 2**63 - 1:
        raise SetupError("ID графика вне диапазона MT5.")
    return value


def make_trader(root: Path, doc: dict, name: str, verifier: str, watcher: str) -> Path:
    directory = local(root / "sessions" / label(name))
    plan = load_session(directory, doc)
    data_dir = mt5_root(doc["mt5"]["data_dir"])
    v, w = ready_id("verifier", verifier.strip(), data_dir), ready_id("watcher", watcher.strip(), data_dir)
    if v == w:
        raise SetupError("Требуются два разных графика наблюдателей.")
    target = local(directory / "RecoveryTrader.set")
    payload = set_bytes({"ExpectedLogin": plan.expected_login, "ExpectedServer": plan.expected_server,
                         "ExpectedSymbol": plan.expected_symbol, "FixedTimeframe": 1, "StrategyMagic": plan.strategy_magic,
                         "ProfileScope": plan.profile_scope, "InstanceSeed": plan.instance_seed,
                         "VerifierChart": v, "WatcherChart": w, "InitialSLDistance": "0.80",
                         "SupportThreshold": "0.40", "LotSchedule": SCHEDULE})
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=directory, prefix=".new-binding-") as temp:
            new = Path(temp) / target.name
            new.write_bytes(payload)
            try:
                new.rename(target)
            except FileExistsError:
                pass
    if target.read_bytes() != payload:
        raise SetupError("Этот сеанс уже связан с другими ID. Для нового запуска подготовьте новый сеанс.")
    return target


def panel(data_dir: str, origin: str, profile: Path, start: bool) -> int:
    profile = local(profile)
    if not profile.is_file():
        raise SetupError("Файл приватного профиля не найден.")
    args = [str(APP / "python/python.exe"), "-I", "-B", "-X", "utf8", str(DEPLOY / "run_panel.py"),
            "--mt5-data-dir", str(mt5_root(data_dir)), "--origin", parse_origin(origin), "--profile", str(profile)]
    check = subprocess.run([*args, "--check"]).returncode
    if check or not start:
        return check
    print("Панель в этом окне: http://127.0.0.1:8767/recovery. Для остановки нажмите Ctrl+C.", flush=True)
    return subprocess.run(args).returncode


def ask(message: str) -> str:
    result = input(message + ": ").strip()
    if not result:
        raise SetupError("Пустое значение. Повторите действие с нужными параметрами.")
    return result


def path_input(message: str) -> str:
    return ask(message).strip('"')


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальная подготовка RECOVERY")
    parser.add_argument("action", nargs="?", choices=["prepare", "bind", "check", "start", "existing-check", "existing-start"])
    action = parser.parse_args().action
    if action is None:
        print("1  Подготовить профиль и пресеты наблюдателей\n2  Подготовить пресет RecoveryTrader\n"
              "3  Проверить панель\n4  Запустить панель\n"
              "5  Проверить панель с существующим приватным JSON\n6  Запустить панель с существующим приватным JSON")
        choices = ("prepare", "bind", "check", "start", "existing-check", "existing-start")
        index = ask("Номер действия")
        if index not in ("1", "2", "3", "4", "5", "6"):
            raise SetupError("Выберите действие от 1 до 6.")
        action = choices[int(index) - 1]
    if action.startswith("existing-"):
        old = Path(path_input("Абсолютный путь к имеющемуся recovery-profile.json"))
        return panel(path_input("Каталог данных именно этого MT5"), ask("HTTPS origin"), old, action == "existing-start")
    name = label(ask("Имя приватного профиля"))
    if action == "prepare":
        if local(profiles() / name).exists():
            root, doc = profile_at(name)
            print("Используется имеющаяся привязка к серверу", doc["mt5"]["server"], "и символу", doc["mt5"]["symbol"])
        else:
            root, doc = prepare_profile(name, path_input("Каталог данных (MT5 → Файл → Открыть каталог данных)"),
                int(ask("Номер выбранного счёта MT5")), ask("Точное имя сервера MT5"), ask("Точный символ"),
                int(ask("Положительный Magic для RECOVERY")), ask("Публичный HTTPS origin или https://localhost для диагностики"))
        session = prepare_session(root, doc, ask("Имя сеанса (новое имя для нового запуска всех ролей)"))
        print("Два пресета для наблюдателей:", session)
        print("Ключ панели находится в приватном файле:", root / "recovery-profile.json")
        return 0
    root, doc = profile_at(name)
    if action == "bind":
        name = ask("Имя ранее подготовленного сеанса")
        v = ask("Строка готовности RecoveryVerifier из «Эксперты» или подтверждённый номер")
        w = ask("Строка готовности RecoveryPriceWatcher того же сеанса или подтверждённый номер")
        print("Готов пресет для ручного подключения:", make_trader(root, doc, name, v, w))
        return 0
    return panel(doc["mt5"]["data_dir"], doc["panel_origin"], root / "recovery-profile.json", action == "start")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
    except (OSError, KeyError, ValueError, TypeError, RuntimeError) as error:
        # JSON/config exceptions can contain user-supplied data. Never echo a key.
        if isinstance(error, (SetupError, ConfigError)):
            print(str(error), file=sys.stderr)
        elif isinstance(error, (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError)):
            print("Не удалось прочитать или сохранить выбранную конфигурацию. Проверьте путь и права NTFS.", file=sys.stderr)
        else:
            print("Проверьте значения параметров счёта, символа, Magic и сеанса.", file=sys.stderr)
        raise SystemExit(2)
