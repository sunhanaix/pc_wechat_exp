"""SQLite-compatible byte-range locks; never create source files.

SQLite's standard Windows/unix VFS uses shm bytes 120..122 for the
write/checkpoint/recovery locks. Shared locks briefly exclude those operations
while copying an encrypted snapshot. See sqlite.org/walformat.html.
"""
import os
from contextlib import contextmanager, ExitStack
from pathlib import Path


class SourceBusy(OSError):
    """Retry later instead of reading through a concurrent source write."""


@contextmanager
def byte_lock(path, offset, length, exclusive=False, create=False):
    path = Path(path)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                        ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                        ('hEvent', wintypes.HANDLE)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        access = 0x80000000 | (0x40000000 if create else 0)
        handle = kernel.CreateFileW(str(path), access, 7, None, 4 if create else 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise SourceBusy('Cannot open lock file: %s (%d)' % (path.name, ctypes.get_last_error()))
        ov = Overlapped(Offset=offset & 0xffffffff, OffsetHigh=offset >> 32)
        try:
            if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0,
                                     length, 0, ctypes.byref(ov)):
                raise SourceBusy('Database/target is busy: %s (%d)' % (path.name, ctypes.get_last_error()))
            try:
                yield
            finally:
                kernel.UnlockFileEx(handle, 0, length, 0, ctypes.byref(ov))
        finally:
            kernel.CloseHandle(handle)
    else:
        import fcntl
        fd = os.open(path, os.O_RDWR | os.O_CREAT if create else os.O_RDONLY, 0o600)
        try:
            try:
                fcntl.lockf(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
                            length, offset, os.SEEK_SET)
            except OSError as exc:
                raise SourceBusy('Database/target is busy: %s' % path.name) from exc
            try:
                yield
            finally:
                fcntl.lockf(fd, fcntl.LOCK_UN, length, offset, os.SEEK_SET)
        finally:
            os.close(fd)


@contextmanager
def source_guard(path):
    path = Path(path)
    with ExitStack() as stack:
        # Block rollback-mode writers and last-connection WAL cleanup.
        stack.enter_context(byte_lock(path, 0x40000000, 512))
        shm = Path(str(path) + '-shm')
        if shm.exists():
            stack.enter_context(byte_lock(shm, 120, 3))
        elif Path(str(path) + '-wal').exists() and Path(str(path) + '-wal').stat().st_size:
            # A hot WAL without a shared index requires coordinated recovery.
            raise SourceBusy('WAL has no shared index; wait for WeChat to open it')
        yield
