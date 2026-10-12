"""Native Windows directory notifications plus periodic commit probes."""
import os


class DirectoryWatcher:
    def __init__(self, directory):
        self.handle = None
        self.mode = 'poll'
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
            self.kernel.FindFirstChangeNotificationW.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
            self.kernel.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
            handle = self.kernel.FindFirstChangeNotificationW(str(directory), True, 1 | 8 | 16)
            if handle != ctypes.c_void_p(-1).value:
                self.handle = handle
                self.mode = 'windows_notifications'

    def wait(self, stop, interval):
        if self.handle is None:
            stop.wait(interval)
            return False
        result = self.kernel.WaitForSingleObject(self.handle, int(interval * 1000))
        if result == 0:
            if not self.kernel.FindNextChangeNotification(self.handle):
                self.close()
                self.mode = 'poll'
            return True
        if result != 258:  # WAIT_FAILED / invalidated directory handle.
            self.close()
            self.mode = 'poll'
            stop.wait(interval)
        return False

    def close(self):
        if self.handle is not None:
            self.kernel.FindCloseChangeNotification(self.handle)
            self.handle = None
