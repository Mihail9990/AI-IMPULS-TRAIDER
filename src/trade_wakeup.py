"""Wake the trade owner on a verifier pulse, with a periodic reconciliation fallback."""
import ctypes
from ctypes import wintypes
import math
from pathlib import Path
import time


PULSE_NAME = "trade-wakeup.tsv"
FILE_NOTIFY_CHANGE_FILE_NAME = 0x01
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
INVALID_HANDLE = ctypes.c_void_p(-1).value
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
kernel.FindFirstChangeNotificationW.restype = wintypes.HANDLE
kernel.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
kernel.FindNextChangeNotification.restype = wintypes.BOOL
kernel.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
kernel.FindCloseChangeNotification.restype = wintypes.BOOL
kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel.WaitForSingleObject.restype = wintypes.DWORD


class TradeWakeup:
    """Directory changes are hints. Only a changed pulse wakes the trade loop early."""

    def __init__(self, directory):
        self.path = Path(directory) / PULSE_NAME
        self.handle = None
        self.stamp = None
        self.retry_at = 0.0
        self._attach()

    def _stamp(self):
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    def _attach(self):
        now = time.monotonic()
        if self.handle is not None or now < self.retry_at:
            return
        self.retry_at = now + 1.0
        handle = kernel.FindFirstChangeNotificationW(
            str(self.path.parent), False, FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE
        )
        if handle and handle != INVALID_HANDLE:
            self.handle = handle
            self.stamp = self._stamp()

    def wait(self, timeout=0.20):
        deadline = time.monotonic() + timeout
        self._attach()
        while self.handle:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            result = kernel.WaitForSingleObject(self.handle, math.ceil(remaining * 1000))
            if result == WAIT_TIMEOUT:
                return False
            if result != WAIT_OBJECT_0 or not kernel.FindNextChangeNotification(self.handle):
                self.close()
                break
            # Rearm before stat so a new pulse concurrent with this read stays observable.
            stamp = self._stamp()
            if stamp is not None and stamp != self.stamp:
                self.stamp = stamp
                return True
        time.sleep(max(0, deadline - time.monotonic()))
        return False

    def close(self):
        if self.handle is not None:
            kernel.FindCloseChangeNotification(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
