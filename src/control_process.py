"""Keep the local desktop mailbox and its disk waits outside the trading process."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import logging
import multiprocessing as mp
from multiprocessing.connection import wait
from pathlib import Path
import threading
import time

from control_host import ControlHost, OPS, REQUEST_TTL, _project, allowed, intent_binding
from local_settings import validate_settings


_k = ctypes.WinDLL("kernel32", use_last_error=True)
_k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k.CreateJobObjectW.restype = wintypes.HANDLE
_k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k.CloseHandle.argtypes = [wintypes.HANDLE]
_k.GetCurrentProcess.restype = wintypes.HANDLE
_k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k.WaitForSingleObject.restype = wintypes.DWORD


class _BasicLimits(ctypes.Structure):
    _fields_ = [("user_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("working_min", ctypes.c_size_t), ("working_max", ctypes.c_size_t),
                ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", ctypes.c_ulonglong * 6),
                ("memory", ctypes.c_size_t * 4)]


def _job():
    # Only the parent owns this non-inheritable handle. Windows kills all assigned
    # children on close, including if the parent exits without Python cleanup.
    h = _k.CreateJobObjectW(None, None)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _k.SetInformationJobObject(h, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        _k.CloseHandle(h)
        raise ctypes.WinError(error)
    return h


def _send(conn, value):
    conn.send_bytes(json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def _receive(conn):
    result = json.loads(conn.recv_bytes(32768))
    if not isinstance(result, dict):
        raise ValueError("Unexpected desktop RPC")
    return result


def set_desktop_priority():
    """Lower only the calling GUI or mailbox process on Windows."""
    if not _k.SetPriorityClass(_k.GetCurrentProcess(), 0x4000):  # BELOW_NORMAL_PRIORITY_CLASS
        raise ctypes.WinError(ctypes.get_last_error())


class _ServiceStopped(Exception):
    pass


class _RemoteOwner:
    def __init__(self, conn, parent):
        self.conn, self.parent = conn, parent
        self.state = None
        self.serial = 0

    def rpc(self, **req):
        self.serial += 1
        _send(self.conn, dict(req, id=self.serial))
        ready = wait([self.conn, self.parent])
        if self.parent in ready:
            raise _ServiceStopped
        reply = _receive(self.conn)
        if reply.get("id") != self.serial:
            raise _ServiceStopped
        return reply

    def refresh(self):
        self.state = self.rpc(kind="snapshot")["state"]

    def snapshot(self):
        return self.state

    def dispatch_control(self, action, settings, binding, expiry):
        reply = self.rpc(kind="control", action=action, settings=settings, binding=binding, expiry=expiry)
        self.state = reply["state"]
        if reply["outcome"] == "unknown":
            raise RuntimeError("Control outcome is uncertain")
        return reply["outcome"] == "applied"


def _desktop_service(root, conn):
    # Do not open the private mailbox until our parent has attached the job object.
    parent = mp.parent_process().sentinel
    try:
        set_desktop_priority()
        ready = wait([conn, parent])
        if parent in ready or _receive(conn) != {"start": True}:
            return
        owner = _RemoteOwner(conn, parent)
        with ControlHost(root) as host:
            while True:
                owner.refresh()
                host.poll(owner)
                time.sleep(.20)
    except (_ServiceStopped, EOFError, BrokenPipeError):
        pass
    finally:
        conn.close()


@dataclass
class _Exchange:
    request: dict
    process_handle: int
    done: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None


def _state(worker):
    try:
        return _project(worker.snapshot())
    except Exception:
        return _project({"reason": "snapshot_unavailable"})


def _answer(request, worker):
    observed = _state(worker)
    if request.get("kind") == "snapshot":
        return {"state": observed}
    action = request.get("action")
    expires = request.get("expiry")
    if (request.get("kind") != "control" or not isinstance(action, str) or action not in OPS
            or type(expires) not in (int, float) or not 0 <= expires - time.time() <= REQUEST_TTL + .75
            or request.get("binding") != intent_binding(observed) or not allowed(observed, action)):
        return {"state": observed, "outcome": "rejected"}
    try:
        if action == "update_settings":
            result = worker.update_settings(validate_settings(request.get("settings")))
        elif request.get("settings") is None:
            result = worker.control(action)
        else:
            result = False
    except Exception:
        return {"state": _state(worker), "outcome": "unknown"}
    return {"state": _state(worker), "outcome": "applied" if result is True else "rejected"}


class DesktopControlProcess:
    """poll() services at most one RPC locally after the caller's trading tick."""
    def __init__(self, private_root):
        self.root = Path(private_root)
        self._ctx = mp.get_context("spawn")
        self._quit = threading.Event()
        self._guard = threading.Lock()
        self._pending = None
        self._pid = None
        self._manager = None
        self._job = None

    @property
    def pid(self):
        return self._pid

    def __enter__(self):
        self._job = _job()
        self._manager = threading.Thread(target=self._manage, name="RECOVERY desktop supervisor", daemon=True)
        try:
            self._manager.start()
        except BaseException:
            _k.CloseHandle(self._job)
            raise
        return self

    def poll(self, worker):
        # The management thread can block in Windows spawn/pipe IO, but it never
        # holds the guard there. The trading thread does not wait even for this lock.
        if self._pending is None or not self._guard.acquire(blocking=False):
            return
        try:
            exchange, self._pending = self._pending, None
            if exchange is None or self._quit.is_set():
                return
            if _k.WaitForSingleObject(exchange.process_handle, 0) != 258:  # WAIT_TIMEOUT = alive
                return
        finally:
            self._guard.release()
        try:
            exchange.response = _answer(exchange.request, worker)
        except Exception:
            exchange.response = {"state": _project({"reason": "snapshot_unavailable"}), "outcome": "unknown"}
        exchange.done.set()

    def _relay(self, conn, child):
        exited = child.sentinel
        previous_id = 0
        while not self._quit.is_set():
            ready = wait([conn, exited], timeout=.1)
            if exited in ready:
                break
            if conn not in ready:
                continue
            request = _receive(conn)
            if type(request.get("id")) is not int or request["id"] != previous_id + 1:
                break
            previous_id = request["id"]
            exchange = _Exchange(request, exited)
            with self._guard:
                self._pending = exchange
            while not exchange.done.wait(.05):
                if self._quit.is_set() or _k.WaitForSingleObject(exited, 0) != 258:
                    return
            _send(conn, dict(exchange.response, id=previous_id))

    def _manage(self):
        delay = .25
        while not self._quit.is_set():
            child = None
            conn = child_conn = None
            began = time.monotonic()
            try:
                conn, child_conn = self._ctx.Pipe()
                child = self._ctx.Process(target=_desktop_service, args=(self.root, child_conn),
                                          name="RECOVERY Desktop Control", daemon=True)
                child.start()
                child_conn.close()
                if self._quit.is_set():
                    return
                if not _k.AssignProcessToJobObject(self._job, child.sentinel):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._pid = child.pid
                _send(conn, {"start": True})
                self._relay(conn, child)
            except (OSError, ValueError, EOFError, RuntimeError):
                if not self._quit.is_set():
                    logging.getLogger(__name__).warning("Restarting local desktop service")
            finally:
                with self._guard:
                    self._pending = None
                self._pid = None
                if conn is not None:
                    conn.close()
                if child_conn is not None:
                    child_conn.close()
                if child is not None and child.pid is not None:
                    child.join(.6)
                    if child.is_alive():
                        child.terminate()
                        child.join(1)
                    if not child.is_alive():
                        child.close()
            if time.monotonic() - began >= 10:
                delay = .25
            if self._quit.wait(delay):
                break
            delay = min(delay * 2, 5)

    def __exit__(self, *exc):
        self._quit.set()
        self._manager.join(2)
        _k.CloseHandle(self._job)
        self._manager.join(1)
