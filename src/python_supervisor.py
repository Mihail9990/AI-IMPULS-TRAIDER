"""One local RECOVERY owner, independent of whether the desktop window is open."""
import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import sys
import time

APP = Path(__file__).resolve().parent
BASE = Path(os.environ["LOCALAPPDATA"]) / "AI-IMPULS-RECOVERY" / "control"
PRIVATE = BASE / "python-v1"
MUTEX_NAME = "Local\\RECOVERY.Python." + hashlib.sha256(str(PRIVATE).casefold().encode()).hexdigest()[:24]
KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL.CreateMutexW.restype = wintypes.HANDLE
KERNEL.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL.OpenMutexW.restype = wintypes.HANDLE
KERNEL.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
KERNEL.CreateFileW.restype = wintypes.HANDLE
KERNEL.CloseHandle.argtypes = [wintypes.HANDLE]
BAD = ctypes.c_void_p(-1).value


def ensure_supervisor():
    existing = KERNEL.OpenMutexW(0x100000, False, MUTEX_NAME)
    if existing:
        KERNEL.CloseHandle(existing)
        return
    subprocess.Popen([sys.executable, "-I", "-B", "-X", "utf8", str(APP / "python_supervisor.py")],
                     cwd=APP, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.NORMAL_PRIORITY_CLASS))


def machine_settings():
    data = json.loads((PRIVATE / "machine.json").read_text(encoding="utf-8"))
    if (data.get("format") != 1 or type(data.get("login")) is not int or data["login"] <= 0
            or type(data.get("magic")) is not int or data["magic"] <= 0
            or not re.fullmatch(r"[0-9a-f]{32}", data.get("helper_scope", ""))
            or not all(isinstance(data.get(k), str) and data[k] for k in ("server","symbol"))
            or not Path(data["terminal_exe"]).is_file() or not (Path(data["data_dir"])/"MQL5/Files").is_dir()):
        raise ValueError("Проверьте локальную привязку RECOVERY к терминалу.")
    return data


@contextlib.contextmanager
def legacy_locks(files):
    # Older executors require one of these exact exclusive MT5 locks before
    # they can trade. Keep them while Python owns this dedicated terminal.
    with contextlib.ExitStack() as stack:
        for relative in ("RECOVERY_CONTROL/owner.lock", "RECOVERY_PANEL/writer.lock"):
            path = files / relative
            if not path.parent.exists():
                path.parent.mkdir()
            handle = KERNEL.CreateFileW(str(path), 0x80000000, 0, None, 4, 128, None)
            if not handle or handle == BAD:
                raise RuntimeError("Другой исполнитель RECOVERY уже занимает терминал.")
            stack.callback(KERNEL.CloseHandle, handle)
        yield


class BoundTerminal:
    def __init__(self, api, cfg):
        self.api = api
        self.cfg = cfg

    def __getattr__(self, name):
        return getattr(self.api, name)

    def problem(self):
        terminal = self.api.terminal_info()
        if terminal is None or not terminal.connected:
            return "Нет соединения MT5 с брокером."
        if Path(terminal.data_path).resolve() != Path(self.cfg["data_dir"]).resolve():
            return "Подключён другой каталог MT5."
        account = self.api.account_info()
        if account is None or (account.login,account.server) != (self.cfg["login"],self.cfg["server"]):
            return "Счёт или сервер MT5 отличается от настроек."
        if account.margin_mode != self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            return "Настроенному счёту необходим хеджинговый режим."
        if not terminal.trade_allowed or terminal.tradeapi_disabled or not account.trade_allowed or not account.trade_expert:
            return "Проверьте разрешения торговли Python, Algo Trading и счёта MT5."
        return ""

    def guard_request(self, request):
        issue = self.problem()
        if issue:
            raise RuntimeError(issue)
        name = request.get("symbol")
        if request.get("action") == self.api.TRADE_ACTION_REMOVE:
            ticket = request.get("order")
            if type(ticket) is not int or ticket <= 0:
                raise RuntimeError("Не указан тикет встречного STOP для отмены.")
            rows = self.api.orders_get(ticket=ticket)
            order = rows[0] if rows is not None and len(rows) == 1 else None
            if (order is None or order.ticket != ticket or order.symbol != self.cfg["symbol"]
                    or order.magic != self.cfg["magic"]
                    or order.type not in (self.api.ORDER_TYPE_BUY_STOP, self.api.ORDER_TYPE_SELL_STOP)):
                raise RuntimeError("MT5 не подтвердил принадлежность встречного STOP для отмены.")
            if name is None:
                name = order.symbol
        if name is None and request.get("order"):
            rows = self.api.orders_get(ticket=int(request["order"]))
            name = rows[0].symbol if rows is not None and len(rows)==1 else None
        if name is None and request.get("position"):
            rows = self.api.positions_get(ticket=int(request["position"]))
            name = rows[0].symbol if rows is not None and len(rows)==1 else None
        if name != self.cfg["symbol"]:
            raise RuntimeError("Инструмент заявки отличается от выбранного в RECOVERY.")

    def order_check(self, request):
        self.guard_request(request)
        return self.api.order_check(request)

    def order_send(self, request):
        self.guard_request(request)
        return self.api.order_send(request)


class RuntimeAdapter:
    def __init__(self, api, cfg):
        self.cfg = cfg
        self.api = api
        self.bound = BoundTerminal(api,cfg)
        self.strategy = None
        self.connection_due = 0.
        self.issue = "Подключение к настроенному MT5."
        self.last_log = None

    def tick(self):
        from recovery_helpers import Helpers
        from recovery_strategy import RecoveryStrategy
        now = time.monotonic()
        try:
            terminal = self.api.terminal_info()
            if terminal is None or not terminal.connected:
                self.issue = "Нет соединения MT5 с брокером."
                if now < self.connection_due:
                    return
                self.connection_due = now + 5
                if not self.api.initialize(self.cfg["terminal_exe"], timeout=3500):
                    return
            self.issue = self.bound.problem()
            if self.issue:
                return
            if self.strategy is None:
                files = Path(self.cfg["data_dir"]) / "MQL5/Files"
                helpers = Helpers(files,self.cfg["login"],self.cfg["server"],self.cfg["symbol"],self.cfg["helper_scope"])
                self.strategy = RecoveryStrategy(self.bound,helpers,account=self.cfg["login"],server=self.cfg["server"],
                        symbol=self.cfg["symbol"],magic=self.cfg["magic"],database=PRIVATE/"recovery.sqlite3")
            self.strategy.tick()
            self.last_log = None
        except Exception as error:
            self.issue = "Проверка Python/MT5 приостановлена. Подробности в журнале пульта."
            tag = type(error).__name__
            if tag != self.last_log:
                logging.exception("Strategy poll")
                self.last_log = tag

    def snapshot(self):
        if self.strategy is None:
            result = {"running":False, "mode":"ready","market_rearm_enabled":False,"available":False,
                    "can_start":False,"can_pause":False,"mt5_connected":False,
                    "server":self.cfg["server"],"symbol":self.cfg["symbol"],
                    "account":"••••"+str(self.cfg["login"])[-4:]}
        else:
            result = dict(self.strategy.snapshot())
        if self.issue:
            result.update(available=False, can_start=False, reason=self.issue, mt5_connected=False)
        return result

    def control(self, action):
        if self.strategy is None or self.issue or self.bound.problem():
            return False
        return self.strategy.control(action)

    def update_settings(self, settings):
        if self.strategy is None or self.issue:
            return False
        return self.strategy.update_settings(settings)

    def close(self):
        if self.strategy is not None:
            self.strategy.close()
        self.api.shutdown()


def main():
    mutex = KERNEL.CreateMutexW(None,False,MUTEX_NAME)
    duplicate = ctypes.get_last_error() == 183
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if duplicate:
            return
        cfg = machine_settings()
        root_log = logging.getLogger()
        root_log.setLevel(logging.INFO)
        logfile = RotatingFileHandler(PRIVATE/"python-runtime.log",maxBytes=750000,backupCount=2,encoding="utf-8")
        logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root_log.addHandler(logfile)
        sys.path[:0] = [str(APP),str(APP/"lib")]
        import MetaTrader5 as mt
        from control_process import DesktopControlProcess
        from trade_wakeup import TradeWakeup
        files = Path(cfg["data_dir"]) / "MQL5/Files"
        with (legacy_locks(files), TradeWakeup(files / "RECOVERY_PY" / cfg["helper_scope"]) as wakeup,
              DesktopControlProcess(PRIVATE) as control):
            worker = RuntimeAdapter(mt,cfg)
            first_pulse = True
            next_control_log = 0.0
            try:
                while True:
                    worker.tick()
                    try:
                        control.poll(worker)
                    except Exception:
                        if time.monotonic() >= next_control_log:
                            logging.exception("Desktop control request deferred")
                            next_control_log = time.monotonic() + 30
                    if wakeup.wait(.20) and first_pulse:
                        first_pulse = False
                        logging.info("MT5 verifier notification received")
            finally:
                worker.close()
    except Exception:
        logging.exception("Local supervisor stopped")
        raise
    finally:
        KERNEL.CloseHandle(mutex)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

