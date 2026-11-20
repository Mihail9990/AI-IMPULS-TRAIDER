"""Open snapshots without blocking an atomic Windows rename or delete."""
import errno
import os

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    _bad_handle = ctypes.c_void_p(-1).value
    class _RenameInfo(ctypes.Structure):
        _fields_ = [("flags", wintypes.DWORD), ("root", wintypes.HANDLE), ("length", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 1)]


def open_shared_read(path):
    if os.name != "nt":
        return open(path, "rb")
    # GENERIC_READ, FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE, OPEN_EXISTING.
    handle = _kernel.CreateFileW(os.fspath(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == _bad_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def sharing_error(error):
    if os.name != "nt":
        return False
    code = getattr(error, "winerror", None)
    # MoveFileEx can return ACCESS_DENIED for a destination held without delete sharing.
    return code in (5, 32, 33) or (code is None and error.errno in (errno.EACCES, errno.EPERM))


def replace_shared(source, target):
    """Atomically publish a replacement while existing share-delete readers finish."""
    try:
        os.replace(source, target)
        return
    except OSError as exc:
        if not sharing_error(exc):
            raise
        original = exc
    # Legacy MoveFileEx (os.replace) rejects even an open FILE_SHARE_DELETE reader
    # on Windows NTFS. FileRenameInfoEx keeps that reader's old file object valid.
    # A reader denying FILE_SHARE_DELETE still prevents replacement.
    name = os.path.abspath(target).encode("utf-16-le")
    offset = _RenameInfo.name.offset
    # The Win32 wrapper resolves the DOS path before dispatch to the filesystem.
    # Supply a terminating WCHAR in addition to the byte-counted file name.
    buf = ctypes.create_string_buffer(max(ctypes.sizeof(_RenameInfo), offset + len(name) + 2))
    info = _RenameInfo.from_buffer(buf)
    info.flags = 3  # FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS
    info.length = len(name)
    ctypes.memmove(ctypes.addressof(buf) + offset, name, len(name))
    handle = _kernel.CreateFileW(os.fspath(source), 0x10000, 7, None, 3, 0x80, None)  # DELETE
    if handle == _bad_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not _kernel.SetFileInformationByHandle(handle, 22, buf, ctypes.sizeof(buf)):
            error = ctypes.get_last_error()
            if error in (1, 50, 87, 120):  # No extended rename support: retry the normal path later.
                raise original
            raise ctypes.WinError(error)
    finally:
        _kernel.CloseHandle(handle)
