"""wx_startup_watcher — WMI-based WeChat startup detection + auto key capture.

Uses Win32_ProcessStartTrace via WMI ExecNotificationQuery to detect Weixin.exe
launch with near-zero latency, then immediately installs the wrapper+0x989 hook
to capture the DB key during the login initialization window.

Usage:
    python -m engine.services.wx_startup_watcher [--output keys.json] [--timeout 300]
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time

# Polling interval while waiting for Weixin.dll to load (seconds)
_DLL_POLL_INTERVAL = 0.1
# Max time to wait for Weixin.dll after process creation (seconds)
_DLL_LOAD_TIMEOUT = 30
# Max time to wait for key capture after hook installation (seconds)
_KEY_CAPTURE_TIMEOUT = 60


class WeChatStartupWatcher:
    """Monitor WeChat startup via WMI and capture DB key on launch."""

    def __init__(self, output_path=None, on_key_captured=None):
        self._output_path = output_path
        self._on_key_captured = on_key_captured
        self._running = False
        self._lock = threading.Lock()
        self._captured_keys = []

    def start(self, timeout=300):
        """Block until key captured or timeout.

        Returns list of captured keys on success, empty list on timeout.
        """
        self._running = True

        print("[Watcher] WeChat Startup Watcher starting...")
        print("[Watcher] Subscribing to WMI Win32_ProcessStartTrace...")

        # Check if WeChat is already running — hook it now if so
        existing = self._find_wechat_pids()
        if existing:
            for mem_size, pid in existing:
                print(f"[Watcher] WeChat already running: PID={pid} ({mem_size // 1048576}MB)")
                print("[Watcher] Attempting immediate hook...")
                key = self._hook_and_capture(pid)
                if key:
                    self._captured_keys.append(key)
                    self._save_keys()
                    self._running = False
                    return self._captured_keys

        print("[Watcher] Waiting for Weixin.exe to start...")

        # Start WMI listener in background thread
        wmi_thread = threading.Thread(target=self._wmi_event_loop, daemon=True)
        wmi_thread.start()

        # Block until timeout or key captured
        deadline = time.time() + timeout
        while self._running and time.time() < deadline:
            time.sleep(0.5)

        self._running = False
        return self._captured_keys

    def stop(self):
        self._running = False

    # --- Internal ---

    @staticmethod
    def _find_wechat_pids():
        import psutil
        candidates = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
            try:
                info = proc.info
                if info['name'] and info['name'].lower() == 'weixin.exe':
                    mem = info['memory_info'].rss if info['memory_info'] else 0
                    candidates.append((mem, info['pid']))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        candidates.sort(reverse=True)
        return candidates

    @staticmethod
    def _find_weixin_dll_in_pid(pid):
        """Check if Weixin.dll is loaded in a given PID. Returns {base, size} or None."""
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi

        kernel32.OpenProcess.restype = wt.HANDLE
        psapi.EnumProcessModules.restype = wt.BOOL
        psapi.GetModuleBaseNameA.restype = wt.DWORD
        psapi.GetModuleInformation.restype = wt.BOOL

        h = kernel32.OpenProcess(0x0410, False, pid)
        if not h:
            return None

        try:
            hMods = (wt.HMODULE * 2048)()
            cb = wt.DWORD()
            if not psapi.EnumProcessModules(h, hMods, ctypes.sizeof(hMods), ctypes.byref(cb)):
                return None
            count = cb.value // ctypes.sizeof(wt.HMODULE)
            for i in range(count):
                name_buf = ctypes.create_string_buffer(260)
                if psapi.GetModuleBaseNameA(h, ctypes.c_void_p(hMods[i]), name_buf, 260):
                    if name_buf.value.decode().lower() == 'weixin.dll':
                        class MI(ctypes.Structure):
                            _fields_ = [
                                ('base', ctypes.c_void_p),
                                ('size', wt.DWORD),
                                ('entry', ctypes.c_void_p),
                            ]
                        mi = MI()
                        if psapi.GetModuleInformation(
                            h, ctypes.c_void_p(hMods[i]), ctypes.byref(mi), ctypes.sizeof(mi)
                        ):
                            bv = mi.base.value if hasattr(mi.base, 'value') else int(mi.base)
                            return {'base': bv, 'size': mi.size}
            return None
        finally:
            kernel32.CloseHandle(h)

    def _wmi_event_loop(self):
        """Background thread: WMI ExecNotificationQuery for process creation.

        Uses WMI's built-in eventing (not polling). Win32_ProcessStartTrace
        fires within ~100ms of process creation — much faster than psutil polling.
        Falls back to psutil polling if pywin32 is not available.
        """
        try:
            import pythoncom
            import win32com.client
        except ImportError:
            print("[Watcher] pywin32 not available — falling back to psutil polling")
            self._psutil_poll_loop()
            return

        pythoncom.CoInitialize()
        try:
            locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
            wmi = locator.ConnectServer(".", "root\\cimv2")

            # Track known PIDs to avoid duplicate handling
            known_pids = set(p for _, p in self._find_wechat_pids())

            # Create event query — this is a WMI-native push subscription
            # that fires when any process starts
            events = wmi.ExecNotificationQuery(
                "SELECT * FROM Win32_ProcessStartTrace"
            )

            print("[Watcher] WMI event subscription active")

            while self._running:
                try:
                    # NextEvent blocks until an event arrives (with 2s timeout)
                    event = events.NextEvent(2000)
                    if event is None:
                        # Also check via psutil as fallback
                        self._psutil_check_new_pids(known_pids)
                        continue

                    proc_name = str(event.Properties_["ProcessName"].Value or "")
                    pid = int(event.Properties_["ProcessID"].Value or 0)

                    if proc_name.lower() == "weixin.exe" and pid and pid not in known_pids:
                        known_pids.add(pid)
                        print(f"\n[Watcher] WMI: Weixin.exe started PID={pid}")
                        self._handle_new_process(pid)

                except Exception as e:
                    # NextEvent timeout (not a real error) or COM issue
                    err_msg = str(e)
                    if "0x80043001" not in err_msg:  # wbemErrTimedOut — expected
                        pass  # silent retry
                    # Fallback check
                    self._psutil_check_new_pids(known_pids)

        finally:
            pythoncom.CoUninitialize()

    def _psutil_poll_loop(self):
        """Fallback: poll via psutil when pywin32/WMI is unavailable."""
        known_pids = set(p for _, p in self._find_wechat_pids())

        print("[Watcher] psutil polling active (1s interval)")

        while self._running:
            self._psutil_check_new_pids(known_pids)
            time.sleep(1)

    def _psutil_check_new_pids(self, known_pids):
        """Check for new WeChat PIDs via psutil. Thread-safe helper."""
        current = self._find_wechat_pids()
        current_pids = set(p for _, p in current)
        new_pids = current_pids - known_pids
        for pid in new_pids:
            known_pids.add(pid)
            print(f"\n[Watcher] Weixin.exe detected: PID={pid}")
            self._handle_new_process(pid)

    def _handle_new_process(self, pid):
        """Called when a new Weixin.exe process is detected."""
        # Wait for Weixin.dll to load (175MB DLL, takes 1-3 seconds)
        print(f"[Watcher] Waiting for Weixin.dll to load in PID={pid}...")
        wait_start = time.time()
        dll_loaded = False
        while time.time() - wait_start < _DLL_LOAD_TIMEOUT:
            dll_info = self._find_weixin_dll_in_pid(pid)
            if dll_info:
                dll_base = dll_info['base']
                dll_size = dll_info['size']
                print(f"[Watcher] Weixin.dll loaded: 0x{dll_base:x} ({dll_size // 1048576}MB)")
                dll_loaded = True
                break
            time.sleep(_DLL_POLL_INTERVAL)

        if not dll_loaded:
            print(f"[Watcher] Timeout waiting for Weixin.dll in PID={pid}")
            return

        # Small delay to let WeChat initialization progress to the point
        # where the key setup chain (wrapper → inner_func → apply_func) runs
        time.sleep(1.5)

        # Install hook and capture key
        key = self._hook_and_capture(pid)
        if key:
            print(f"[Watcher] KEY CAPTURED: {key['key'][:32]}...")
            self._captured_keys.append(key)
            self._save_keys()
            if self._on_key_captured:
                self._on_key_captured(key)

    def _hook_and_capture(self, pid):
        """Install hook on given PID and wait for key capture."""
        backend = None
        for mod_name in ('py_wx_key_v3', 'py_wx_key_v2'):
            try:
                backend = __import__(f'engine.services.{mod_name}', fromlist=[
                    'initialize_hook', 'poll_key_data', 'cleanup_hook',
                    'get_last_error_msg',
                ])
                break
            except ImportError:
                continue
        if backend is None:
            print("[Watcher] ERROR: no hook backend available (py_wx_key_v3/v2)")
            return None
        initialize_hook = backend.initialize_hook
        poll_key_data = backend.poll_key_data
        cleanup_hook = backend.cleanup_hook
        get_last_error_msg = backend.get_last_error_msg

        is_admin = False
        try:
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            pass
        if not is_admin:
            print("[Watcher] ERROR: Admin privileges required for hook")
            return None

        print(f"[Watcher] Installing key-setup hook on PID={pid}...")
        ok = initialize_hook(pid=pid, timeout=_DLL_LOAD_TIMEOUT)
        if not ok:
            err = get_last_error_msg()
            print(f"[Watcher] Hook install failed: {err}")
            return None

        diag = get_last_error_msg()
        print(f"[Watcher] Hook active — {diag}")
        print(f"[Watcher] Polling for key (timeout={_KEY_CAPTURE_TIMEOUT}s)...")

        deadline = time.time() + _KEY_CAPTURE_TIMEOUT
        while time.time() < deadline and self._running:
            key_data = poll_key_data()
            if key_data:
                cleanup_hook()
                return key_data
            time.sleep(0.1)

        print("[Watcher] Key capture timeout — hook left installed for delayed capture")
        return None

    def _save_keys(self):
        if not self._output_path or not self._captured_keys:
            return
        try:
            out_dir = os.path.dirname(self._output_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(self._output_path, 'w') as f:
                json.dump(self._captured_keys, f, indent=2)
            print(f"[Watcher] Keys saved to: {self._output_path}")
        except Exception as e:
            print(f"[Watcher] Failed to save keys: {e}")


# --- Standalone entry point ---
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="WMI-based WeChat startup watcher + auto key capture"
    )
    parser.add_argument(
        '--output', '-o',
        default='output/wechat_keys.json',
        help='Output path for captured keys (default: output/wechat_keys.json)',
    )
    parser.add_argument(
        '--timeout', '-t',
        type=int, default=300,
        help='Max seconds to wait (default: 300)',
    )
    args = parser.parse_args()

    print("=" * 60)
    print(" WeChat Startup Watcher — WMI-based auto key capture")
    print("=" * 60)

    src_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    watcher = WeChatStartupWatcher(output_path=args.output)
    keys = watcher.start(timeout=args.timeout)

    if keys:
        print(f"\n[DONE] Captured {len(keys)} key(s):")
        for k in keys:
            print(f"  {k['key']}")
        return 0
    else:
        print("\n[TIMEOUT] No keys captured within time limit")
        return 1


if __name__ == '__main__':
    sys.exit(main())
