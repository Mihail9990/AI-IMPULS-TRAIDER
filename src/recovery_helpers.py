"""Terminal-local RECOVERY helper bridge. No MT5 process control or broker operations."""
import contextlib
import ctypes
from ctypes import wintypes
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from ipc_files import open_shared_read


class HelperUnavailable(RuntimeError):
    pass


HEX = re.compile(r"[0-9a-f]{32}\Z")
NAT = re.compile(r"0|[1-9][0-9]{0,19}\Z")
LEASE_MS = 6000
FRESH_MS = 5000


def _uint(value, positive=False):
    value = str(value)
    if not NAT.fullmatch(value) or int(value) >= 2**64 or (positive and value == "0"):
        raise HelperUnavailable("Некорректное числовое поле помощника")
    return int(value)


def _uptime():
    fn = ctypes.windll.kernel32.GetTickCount64
    fn.restype = ctypes.c_ulonglong
    return int(fn())


def _replace(path, wire):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(wire)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _register_lock(path):
    """Same exclusive Win32 file share contract as FileOpen(...FILE_READ|FILE_WRITE)."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    deadline = time.monotonic() + 0.18
    while True:
        handle = create(str(path), 0xC0000000, 0, None, 4, 0x80, None)
        if handle != ctypes.c_void_p(-1).value:
            break
        if ctypes.get_last_error() not in (32, 33) or time.monotonic() >= deadline:
            raise HelperUnavailable("Помощник SL занят, обновление регистрации не подтверждено")
        time.sleep(0.015)
    try:
        yield
    finally:
        k.CloseHandle(handle)


class Helpers:
    def __init__(self, files_dir, login, server, symbol, scope):
        if not HEX.fullmatch(scope) or any(not isinstance(s, str) or not 0 < len(s) <= 100
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in s) for s in (server, symbol)):
            raise ValueError("Некорректная привязка помощников")
        self.scope, self.login, self.server, self.symbol = scope, _uint(login, True), server, symbol
        self.directory = Path(files_dir) / "RECOVERY_PY" / scope
        self._history_requests = {}

    def _read(self, name, schema, columns):
        try:
            path = self.directory / name
            with open_shared_read(path) as source:
                data = source.read(131073)
                if source.read(1) or len(data) > 131072:
                    raise HelperUnavailable("Слишком большой отчёт помощника")
            wire = data.decode("utf-8", errors="strict")
            if "\r" in wire or not wire.endswith("\nEND\n"):
                raise HelperUnavailable("Неполный отчёт помощника")
            lines = [line.split("\t") for line in wire[:-5].split("\n")]
            header = lines[0]
            if (len(header) != columns or header[:5] !=
                    [schema, self.scope, str(self.login), self.server, self.symbol]
                    or not HEX.fullmatch(header[5])):
                raise HelperUnavailable("Отчёт принадлежит другой привязке")
            sequence, stamp = _uint(header[6], True), _uint(header[7])
            if not 0 <= _uptime() - stamp <= FRESH_MS or time.time() - path.stat().st_mtime > 7:
                raise HelperUnavailable("Нет свежего ответа помощника")
            return (dict(scope=self.scope, login=self.login, server=self.server, symbol=self.symbol,
                         instance=header[5], sequence=sequence, uptime_ms=stamp), header[8:], lines[1:])
        except (OSError, UnicodeError) as exc:
            raise HelperUnavailable("Отчёт помощника пока недоступен") from exc

    def snapshot(self):
        meta, head, rows = self._read("verifier.tsv", "PYINV1", 14)
        if head[0] != "ok" or head[5] not in ("0","1"):
            raise HelperUnavailable("Независимая опись пока не подтверждена")
        try:
            tick, step = Decimal(head[1]), Decimal(head[2])
            if not all(x.is_finite() and x > 0 for x in (tick, step)):
                raise InvalidOperation
        except InvalidOperation as exc:
            raise HelperUnavailable("Некорректный шаг цены или объёма") from exc
        pn, on = _uint(head[3]), _uint(head[4])
        if pn > 128 or on > 128 or len(rows) != pn + on:
            raise HelperUnavailable("Неполная независимая опись")
        positions, orders = [], []
        for i, row in enumerate(rows):
            if i < pn:
                if len(row) != 11 or row[0] != "P" or row[5] not in ("BUY", "SELL"):
                    raise HelperUnavailable("Некорректная строка позиции")
                vals = map(_uint, row[1:5] + row[6:])
                p = dict(zip(("position_id","ticket","magic","reason","opened_msc","volume_steps",
                              "entry_ticks","sl_ticks","tp_ticks"), vals))
                p.update(identifier=p["position_id"], side=row[5], volume=float(step * p["volume_steps"]))
                positions.append(p)
            else:
                if len(row) != 14 or row[0] != "O":
                    raise HelperUnavailable("Некорректная строка ордера")
                o = dict(zip(("ticket","position_id","magic","type","state","reason","time_msc","initial_steps",
                              "remaining_steps","price_ticks","sl_ticks","tp_ticks","stoplimit_ticks"), map(_uint, row[1:])))
                o.update(initial_volume=float(step * o["initial_steps"]),remaining_volume=float(step * o["remaining_steps"]))
                orders.append(o)
        if (len({p["ticket"] for p in positions}) != pn or len({p["position_id"] for p in positions}) != pn
                or len({o["ticket"] for o in orders}) != on):
            raise HelperUnavailable("Повторяющиеся идентификаторы в описи")
        return dict(meta, tick_size=head[1], volume_step=head[2], positions=positions, orders=orders,
                    expert_trade_allowed=head[5]=="1")

    def _evidence(self, key, kind, schema, width):
        witness=self.snapshot()
        now=_uptime()
        slot=(kind,key)
        old=self._history_requests.get(slot)
        stem=f"{kind}-{key}"
        if old and old[0]==witness["instance"] and 0<=now-old[2]<=FRESH_MS:
            meta,head,rows=self._read(stem+".tsv",schema,width)
            if (meta["instance"]!=old[0] or meta["uptime_ms"]<old[2] or head[0]!="ok"
                    or _uint(head[1])!=key or head[2]!=old[1] or head[3]!=witness["tick_size"]
                    or head[4]!=witness["volume_step"]):
                raise HelperUnavailable("Ожидание связанного отчёта сверки")
            return meta,head,rows
        nonce=secrets.token_hex(16)
        stamp="PYHREQ1" if kind=="history" else "PYOQ1"
        fields=(stamp,self.scope,self.login,self.server,self.symbol,witness["instance"],key,nonce,now,"END")
        try:
            _replace(self.directory/(stem+".req"), ("\t".join(map(str,fields))+"\n").encode("utf-8"))
            self._history_requests={k:v for k,v in self._history_requests.items()
                                    if v[0]==witness["instance"] and 0<=now-v[2]<=FRESH_MS}
            self._history_requests[slot]=(witness["instance"],nonce,now)
        except OSError as exc:
            raise HelperUnavailable("Не удалось запросить независимую сверку") from exc
        # A best-effort index makes current keys visible without walking old files.
        # Evidence always comes from the independently verified per-key challenge.
        keys=[str(i) for k,i in self._history_requests if k==kind][-256:]
        hint="\t".join([witness["instance"],str(now),*keys,"END\n"])
        try:
            _replace(self.directory/(kind+"-priority.tsv"),hint.encode("ascii"))
        except OSError:
            pass
        raise HelperUnavailable("Ожидание независимой сверки")

    def order(self,ticket):
        """Return the challenge-bound historical order. An unseen or working order has no final proof."""
        ticket=_uint(ticket,True)
        meta,h,rows=self._evidence(ticket,"order","PYORD1",13)
        if len(rows)!=1 or len(rows[0])!=15 or rows[0][0]!="H":
            raise HelperUnavailable("Неполная точная сверка ордера")
        fields=("ticket","position_id","magic","type","state","reason","time_msc","done_msc","initial_steps",
                "remaining_steps","price_ticks","sl_ticks","tp_ticks","stoplimit_ticks")
        o=dict(zip(fields,map(_uint,rows[0][1:])))
        if o["ticket"]!=ticket or o["done_msc"]==0 or not 0<=o["remaining_steps"]<=o["initial_steps"]:
            raise HelperUnavailable("Недостоверная история ордера")
        return dict(meta,order=o,tick_size=h[3],volume_step=h[4])

    def history(self,position_id):
        """Return independently witnessed structural deals and their actual SL levels."""
        pid=_uint(position_id,True)
        meta,h,rows=self._evidence(pid,"history","PYHIST2",17)
        n,m=_uint(h[5]),_uint(h[8])
        if (n>256 or m>256 or n+m!=len(rows) or h[7] not in ("ok","unavailable")
                or any(row[0]!="M" for row in rows[n:])):
            raise HelperUnavailable("Неполная история позиции")
        deals=[]
        for row in rows[:n]:
            if len(row)!=13 or row[0]!="D":
                raise HelperUnavailable("Некорректная строка сделки")
            fields=("ticket","order","position_id","magic","type","entry","reason","time_msc","volume_steps",
                    "price_ticks","sl_ticks","tp_ticks")
            deal=dict(zip(fields,map(_uint,row[1:])))
            if deal["position_id"]!=pid or not deal["ticket"] or not deal["order"]:
                raise HelperUnavailable("Неверная привязка исторической сделки")
            deals.append(dict(deal,deal_sl_ticks=deal["sl_ticks"]))
        if len({x["ticket"] for x in deals})!=len(deals):
            raise HelperUnavailable("Повтор сделки в истории")
        return dict(meta,position_id=pid,tick_size=h[3],volume_step=h[4],deals=deals)

    def sl_status(self):
        meta, head, rows = self._read("sl-status.tsv", "PYSL1", 13)
        state, run, until, count, allowed = head
        if state != "ok" or allowed not in ("0","1") or (run != "-" and not HEX.fullmatch(run)) or len(rows) != _uint(count) or len(rows)>24:
            raise HelperUnavailable("Помощник SL пока не готов")
        result = []
        for row in rows:
            if (len(row)!=13 or row[0]!="W" or row[5] not in ("BUY","SELL")
                    or row[11] not in {"watching","hit","pending","confirmed","protected","changed","missing",
                                        "invalid","blocked","rejected","unknown","warming","inactive","unavailable",
                                        "last_confirmed","last_protected"}):
                raise HelperUnavailable("Некорректное подтверждение SL")
            vals = map(_uint, row[1:5]+row[6:11]+row[12:13])
            r = dict(zip(("position_id","ticket","magic","opened_msc","entry_ticks","advance_ticks",
                          "sl_ticks","tp_ticks","sample_msc","retcode"), vals))
            result.append(dict(r, side=row[5], status=row[11]))
        return dict(meta, run_id="" if run=="-" else run, lease_until_ms=_uint(until), rows=result,
                    trade_allowed=allowed=="1")

    def cap_status(self):
        meta, head, rows = self._read("sl-cap-status.tsv", "PYSC1", 12)
        state, run, until, count = head
        if (state != "ok" or (run != "-" and not HEX.fullmatch(run))
                or len(rows) != _uint(count) or len(rows) > 24):
            raise HelperUnavailable("Нет подтверждения помощника ограничения SL")
        result = []
        for row in rows:
            if (len(row) != 6 or row[0] != "C" or row[5] not in {
                    "legacy", "missing", "unknown", "within", "waiting", "pending", "rejected",
                    "blocked", "inactive", "no_stop", "be_pending"}):
                raise HelperUnavailable("Повреждена строка ограничения SL")
            pid, ticket, target, observed = map(_uint, row[1:5])
            if not pid or not ticket or (not target and row[5] != "legacy"):
                raise HelperUnavailable("Повреждена привязка ограничения SL")
            result.append(dict(position_id=pid, ticket=ticket, risk_cap_ticks=target,
                               sl_ticks=observed, status=row[5]))
        if len({r['position_id'] for r in result}) != len(result):
            raise HelperUnavailable("Повтор позиции в отчёте ограничения SL")
        self._cap_instance = meta["instance"]
        return dict(meta, run_id="" if run == "-" else run, lease_until_ms=_uint(until), rows=result)

    def register(self, positions, run_id):
        if not HEX.fullmatch(run_id):
            raise ValueError("Процессу требуется идентификатор из 32 hex символов")
        rows, ids, tickets = [], set(), set()
        for p in positions:
            pid,ticket,magic,opened,entry,advance = [_uint(p[k],k!="magic") for k in
                    ("position_id","ticket","magic","opened_msc","entry_ticks","advance_ticks")]
            if p["side"] not in ("BUY","SELL") or pid in ids or ticket in tickets or len(rows)>=24:
                raise ValueError("Некорректный или повторный владелец позиции")
            cap = _uint(p["risk_cap_ticks"], True)
            if not (cap < entry if p["side"] == "BUY" else cap > entry):
                raise ValueError("Стоп должен находиться со стороны исходного риска")
            ids.add(pid); tickets.add(ticket)
            rows.append("\t".join(map(str, ("R",pid,ticket,magic,opened,p["side"],entry,advance,cap))))
        try:
            with _register_lock(self.directory / "register.lock"):
                status = self.sl_status()
                if (getattr(self, "_cap_instance", None) != status["instance"]
                        and self.cap_status()["instance"] != status["instance"]):
                    raise HelperUnavailable("Сменился экземпляр помощника SL")
                now = _uptime()
                current_path = self.directory / "sl-register.tsv"
                if current_path.exists():
                    with open_shared_read(current_path) as current:
                        existing = current.readline(2048).rstrip(b"\n").decode("utf-8").split("\t")
                    if (len(existing)==10 and existing[0] in ("PYREG1","PYREG2") and existing[1:6] == [self.scope,str(self.login),
                            self.server,self.symbol,status["instance"]] and existing[6]!=run_id and
                            HEX.fullmatch(existing[6]) and _uint(existing[7])<=now<_uint(existing[8]) and
                            _uint(existing[8])-_uint(existing[7])<=LEASE_MS and _uint(existing[9])>0):
                        raise HelperUnavailable("Регистрация позиции ещё принадлежит другому процессу")
                header = ("PYREG2",self.scope,self.login,self.server,self.symbol,status["instance"],run_id,now,
                          now+LEASE_MS,len(rows))
                wire = ("\t".join(map(str,header))+"\n"+"\n".join(rows+["END",""])).encode("utf-8")
                with tempfile.NamedTemporaryFile(dir=self.directory,delete=False) as temp:
                    temporary=Path(temp.name)
                    try:
                        temp.write(wire);temp.flush();os.fsync(temp.fileno())
                    except BaseException:
                        temp.close();temporary.unlink(missing_ok=True);raise
                try:
                    os.replace(temporary,current_path)
                finally:
                    temporary.unlink(missing_ok=True)
                return status["instance"]
        except OSError as exc:
            raise HelperUnavailable("Не удалось опубликовать регистрацию SL") from exc
