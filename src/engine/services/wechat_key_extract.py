"""
WeChat DB key extraction — unified module for all WeChat versions (3.x / 4.x / 4.1.10+).

Strategies (tried in order):
  1. Load from existing .wechat_exp_config.json (instant)
  2. MMKV offline extraction (db_storage/MMKV/  AES-GCM files)
  3. Config.Cipher read-only scan (WeChat 4.1.10+, no admin needed) ★ primary for 4.1.x
  4. API hook on running WeChat (py_wx_key_v2 shellcode injection)
  5. Hook + WeChat restart workflow (most reliable for legacy DBs)
  6. Memory scan fallback (hex pattern x'<key><salt>' + targeted salt scan)

WeChat 4.1+ no longer stores DB keys as plaintext hex strings in process memory.
Keys are XOR-deobfuscated in-place by an inner function, used briefly in sqlite3_key(),
then the plaintext is gone. Since 4.1.10 the WCDB runtime keeps a
com.Tencent.WCDB.Config.Cipher object whose XOR-obfuscated blob contains the
x'<64hex key><32hex salt>' literal for every DB — readable WITHOUT admin rights
(see config_cipher_extract.py). Hooks are only a fallback for older versions.
"""
import ctypes
import hashlib
import hmac as hmac_mod
import json
import os
import re
import struct
import sys
import threading
import time

# Strategy 3: read-only Config.Cipher scan (WeChat 4.1.10+, no admin needed)
from engine.services.config_cipher_extract import extract_keys_via_config_cipher

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16

_KNOWN_EXE_NAMES = {'weixin.exe', 'wechat.exe'}


# ---------------------------------------------------------------------------
#  HMAC verification (SQLCipher 4)
# ---------------------------------------------------------------------------
def verify_enc_key(enc_key, db_page1):
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


# ---------------------------------------------------------------------------
#  DB file collection
# ---------------------------------------------------------------------------
def collect_db_files(db_dir):
    """Walk db_dir and collect all .db files with their salts."""
    db_files = []
    salt_to_dbs = {}
    for root, dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            if size < PAGE_SZ:
                continue
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
            rel = os.path.relpath(path, db_dir)
            salt = page1[:SALT_SZ].hex()
            db_files.append((rel, path, size, salt, page1))
            salt_to_dbs.setdefault(salt, []).append(rel)
    return db_files, salt_to_dbs


# ---------------------------------------------------------------------------
#  Strategy 1: Load from existing config
# ---------------------------------------------------------------------------
def load_from_config(db_dir, db_files, salt_to_dbs, key_map, print_fn):
    """Try loading keys from .wechat_exp_config.json (and .bak fallback)."""
    # Project root: __file__ = .../src/engine/services/wechat_key_extract.py
    # Go up 4 levels to get project root
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

    config_names = ['.wechat_exp_config.json', '.wechat_exp_config.json.bak']
    config_search_dirs = [
        project_root,
        os.getcwd(),
        os.path.dirname(db_dir),
    ]

    config_paths = []
    for d in config_search_dirs:
        for name in config_names:
            p = os.path.normpath(os.path.join(d, name))
            if p not in config_paths:
                config_paths.append(p)

    for cfg_path in config_paths:
        if not os.path.isfile(cfg_path):
            continue
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            db_keys = config.get('db_keys', {})
            if not db_keys:
                continue
            found = 0
            for rel, path, sz, salt_hex, page1 in db_files:
                if salt_hex in key_map:
                    continue
                # Normalize path separators for cross-platform matching
                rel_norm = rel.replace('\\', '/')
                for key in [rel, rel_norm, rel.replace('/', '\\')]:
                    if key in db_keys:
                        try:
                            enc_key = bytes.fromhex(db_keys[key])
                            if verify_enc_key(enc_key, page1):
                                key_map[salt_hex] = db_keys[key]
                                found += 1
                        except (ValueError, KeyError):
                            pass
                        break
            if found > 0:
                print_fn(f"[Config] Loaded {found}/{len(db_keys)} valid keys from {cfg_path}")
                return found
        except (json.JSONDecodeError, IOError) as e:
            print_fn(f"[Config] Error reading {cfg_path}: {e}")

    return 0


# ---------------------------------------------------------------------------
#  Strategy 2: MMKV offline extraction
# ---------------------------------------------------------------------------

def _clean_wxid(wxid):
    if not wxid or not wxid.startswith('wxid_'):
        return wxid
    parts = wxid.split('_')
    if len(parts) >= 3:
        return '_'.join(parts[:2])
    return wxid


def _derive_mmkv_aes_key(code, wxid_clean):
    """Derive AES-GCM key for MMKV file decryption.

    WeChat uses MD5(str(code) + cleaned_wxid).hex()[:16] as the 16-byte ASCII key.
    Returns list of (label, key_bytes) candidates to try.
    """
    candidates = []
    code_str = str(code)

    # Primary: code + clean_wxid (from py_wx_key / H3CoF6)
    s = code_str + wxid_clean
    candidates.append(('code+wxid', hashlib.md5(s.encode()).hexdigest()[:16].encode()))

    # Alternate: clean_wxid + code
    s = wxid_clean + code_str
    candidates.append(('wxid+code', hashlib.md5(s.encode()).hexdigest()[:16].encode()))

    # Try full 32-char MD5
    candidates.append(('code+wxid_full', hashlib.md5((code_str + wxid_clean).encode()).hexdigest().encode()))

    # Try SHA256 variants
    for trunc in [16, 32]:
        candidates.append(
            (f'sha256:{trunc}',
             hashlib.sha256((code_str + wxid_clean).encode()).hexdigest()[:trunc].encode()))

    return candidates


def extract_keys_from_mmkv(db_dir, db_files, salt_to_dbs, key_map, print_fn):
    """Extract DB keys from db_storage/MMKV/ files (AES-GCM encrypted)."""
    mmkv_dir = os.path.join(db_dir, 'MMKV')
    if not os.path.isdir(mmkv_dir):
        return 0

    wxid_full = os.path.basename(os.path.dirname(db_dir))
    wxid_clean = _clean_wxid(wxid_full)
    if not wxid_clean or not wxid_clean.startswith('wxid_'):
        print_fn("[MMKV] Cannot determine wxid from path")
        return 0

    print_fn(f"[MMKV] wxid={wxid_full} -> {wxid_clean}")

    CODE_RE = re.compile(r'^f([0-9a-fA-F]+)tinfo\.mmkv$')

    mmkv_files = {}
    try:
        for fname in os.listdir(mmkv_dir):
            if fname.endswith('.crc'):
                continue
            m = CODE_RE.match(fname)
            if m:
                code = int(m.group(1), 16)
                mmkv_files[code] = os.path.join(mmkv_dir, fname)
            elif fname.endswith('.mmkv'):
                mmkv_files[fname] = os.path.join(mmkv_dir, fname)
    except OSError as e:
        print_fn(f"[MMKV] Cannot list directory: {e}")
        return 0

    if not mmkv_files:
        return 0

    print_fn(f"[MMKV] Found {len(mmkv_files)} MMKV files")

    try:
        from Crypto.Cipher import AES
    except ImportError:
        print_fn("[MMKV] pycryptodome not available")
        return 0

    # Build path -> (salt, page1) lookup
    path_to_info = {}
    for rel, path, sz, salt_hex, page1 in db_files:
        if salt_hex not in key_map:
            path_to_info[rel] = (salt_hex, page1)
            path_to_info[rel.replace('\\', '/')] = (salt_hex, page1)

    HEX64_RE = re.compile(b'([0-9a-fA-F]{64})')
    found = 0

    for code_or_label, filepath in mmkv_files.items():
        if len(key_map) >= len(salt_to_dbs):
            break

        try:
            with open(filepath, 'rb') as f:
                raw = f.read()
        except (IOError, PermissionError) as e:
            continue

        if len(raw) < 24:
            continue

        total_size = struct.unpack('<I', raw[:4])[0]
        if total_size < 33 or 4 + total_size > len(raw):
            continue

        iv = raw[4:20]
        auth_tag_start = 4 + total_size - 16
        ciphertext = raw[20:auth_tag_start]
        auth_tag = raw[auth_tag_start:4 + total_size]

        # Derive keys
        if isinstance(code_or_label, int):
            key_candidates = _derive_mmkv_aes_key(code_or_label, wxid_clean)
            label = f'code={code_or_label}'
        else:
            # Non-standard filename - try just wxid
            key_candidates = [
                ('wxid_only', hashlib.md5(wxid_clean.encode()).hexdigest()[:16].encode()),
            ]
            label = f"'{code_or_label}'"

        plaintext = None
        matched_key_label = None
        for key_label, aes_key in key_candidates:
            try:
                cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv)
                plaintext = cipher.decrypt_and_verify(ciphertext, auth_tag)
                matched_key_label = key_label
                break
            except (ValueError, KeyError):
                continue
            except Exception:
                continue

        if plaintext is None:
            continue

        print_fn(f"[MMKV] Decrypted {os.path.basename(filepath)} ({matched_key_label}): "
                 f"{len(plaintext)} bytes")

        # Search plaintext for DB paths and nearby 64-char hex keys
        for rel, (salt_hex, page1) in path_to_info.items():
            if salt_hex in key_map:
                continue
            for sep in [b'\\', b'/']:
                path_bytes = rel.replace('\\', sep.decode()).encode()
                pos = plaintext.find(path_bytes)
                if pos < 0:
                    continue
                nearby = plaintext[pos + len(path_bytes):pos + len(path_bytes) + 512]
                for hex_match in HEX64_RE.finditer(nearby):
                    candidate_hex = hex_match.group(0).decode()
                    try:
                        candidate_key = bytes.fromhex(candidate_hex)
                    except ValueError:
                        continue
                    if verify_enc_key(candidate_key, page1):
                        key_map[salt_hex] = candidate_hex
                        found += 1
                        print_fn(f"  [MMKV-FOUND] {rel} salt={salt_hex}")
                        break
                if salt_hex in key_map:
                    break

    print_fn(f"[MMKV] Extracted {found} keys")
    return found


# ---------------------------------------------------------------------------
#  Strategy 3+4: API hook (py_wx_key_v2 shellcode injection)
# ---------------------------------------------------------------------------

def _find_wechat_pids():
    """Return list of (rss_bytes, pid) for weixin.exe, sorted by memory desc."""
    try:
        import psutil
    except ImportError:
        return []
    candidates = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            info = proc.info
            if info['name'] and info['name'].lower() in _KNOWN_EXE_NAMES:
                mem = info['memory_info'].rss if info['memory_info'] else 0
                candidates.append((mem, info['pid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    candidates.sort(reverse=True)
    return candidates


def _import_hook_backend():
    """Import the best available hook backend. Returns (module, name) or (None, None).

    Preference order:
      v3 — entry hook on inner_func (version-tolerant, no magic offsets)
      v2 — mid-function hook at wrapper+0x989 (4.1.10.x only)
    """
    try:
        from engine.services import py_wx_key_v3
        return py_wx_key_v3, 'v3'
    except ImportError:
        pass
    try:
        from engine.services import py_wx_key_v2
        return py_wx_key_v2, 'v2'
    except ImportError:
        pass
    return None, None


def _window_search_key(blob, salt_to_page1, print_fn, tag=''):
    """Slide a 32-byte window over a captured blob, HMAC-verify each window.

    Returns dict {salt_hex: key_hex} for every salt that verifies.
    """
    found = {}
    for off in range(0, len(blob) - 31):
        cand = blob[off:off + 32]
        if cand == b'\x00' * 32:
            continue
        for salt_hex, page1 in salt_to_page1.items():
            if salt_hex in found:
                continue
            if verify_enc_key(cand, page1):
                found[salt_hex] = cand.hex()
                print_fn(f"  [Hook-WIN{tag}] salt={salt_hex} key found at blob+0x{off:x}")
    return found


def _poll_for_keys(backend, pid, salt_to_page1, salt_to_dbs, key_map,
                   print_fn, timeout, backend_name='', all_salt_to_page1=None):
    """Poll an installed hook for captured keys and HMAC-verify them."""
    poll_key_data = backend.poll_key_data
    start = time.time()
    found_count = 0
    unverified_count = 0
    seen_keys = set()
    while time.time() - start < timeout:
        res = poll_key_data()
        if res and 'key' in res:
            key_hex = res['key'].lower()
            if len(key_hex) == 64 and key_hex not in seen_keys:
                seen_keys.add(key_hex)
                key_bytes = bytes.fromhex(key_hex)
                verified = False
                for salt_hex, page1 in list(salt_to_page1.items()):
                    if verify_enc_key(key_bytes, page1):
                        key_map[salt_hex] = key_hex
                        found_count += 1
                        print_fn(f"  [Hook-FOUND] salt={salt_hex} DBs={salt_to_dbs[salt_hex]}")
                        del salt_to_page1[salt_hex]
                        verified = True
                        if len(key_map) >= len(salt_to_dbs):
                            break
                if not verified:
                    unverified_count += 1
                    print_fn(f"  [Hook-DEBUG] unverified capture: src={res.get('source')} "
                             f"len={res.get('key_len')} key={key_hex}")
                if len(key_map) >= len(salt_to_dbs):
                    break
            elif len(key_hex) > 64 and len(key_hex) % 2 == 0 and key_hex not in seen_keys:
                seen_keys.add(key_hex)
                blob = bytes.fromhex(key_hex)
                print_fn(f"  [Hook-DEBUG] large capture: src={res.get('source')} "
                         f"len={res.get('key_len')} blob={len(blob)}B — window search...")
                # search remaining salts first (fast path)
                hits = _window_search_key(blob, salt_to_page1, print_fn, '-rem')
                for salt_hex, k in hits.items():
                    key_map[salt_hex] = k
                    found_count += 1
                    print_fn(f"  [Hook-FOUND] salt={salt_hex} DBs={salt_to_dbs[salt_hex]}")
                    del salt_to_page1[salt_hex]
                # diagnostic: does the blob contain keys for already-known DBs?
                if all_salt_to_page1:
                    rest = {s: p for s, p in all_salt_to_page1.items()
                            if s not in key_map and s not in salt_to_page1}
                    diag = _window_search_key(blob, rest, print_fn, '-diag')
                    if diag:
                        print_fn(f"  [Hook-DEBUG] blob contains {len(diag)} KNOWN-db keys!")
                if len(key_map) >= len(salt_to_dbs):
                    break
        time.sleep(0.1)

    print_fn(f"[Hook] Captured {found_count} verified keys in {time.time() - start:.1f}s"
             f" ({unverified_count} unverified)")
    if hasattr(backend, 'heartbeat_seen'):
        hb = backend.heartbeat_seen()
        print_fn(f"[Hook] heartbeat_seen={hb} "
                 f"({'key-setup func WAS called' if hb else 'key-setup func NEVER called — hook point may be wrong'})")
    if hasattr(backend, 'get_call_diag'):
        cc, r8, r9, ssz = backend.get_call_diag()
        print_fn(f"[Hook] call_diag: total_calls={cc} last_r8={r8} (0x{r8:x}) "
                 f"last_r9=0x{r9:x} last_str_size={ssz}")
    return found_count


def _try_hook_on_pid(pid, db_files, salt_to_dbs, key_map, print_fn, timeout=30):
    """Install hook on a PID and poll for keys. Returns count found."""
    backend, backend_name = _import_hook_backend()
    if backend is None:
        print_fn("[Hook] no hook backend available (py_wx_key_v3/v2)")
        return 0
    initialize_hook = backend.initialize_hook
    cleanup_hook = backend.cleanup_hook
    get_last_error_msg = backend.get_last_error_msg

    remaining_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
    if not remaining_salts:
        return 0

    # Build salt -> page1 lookup for verification
    salt_to_page1 = {}
    all_salt_to_page1 = {}
    for rel, path, sz, salt_hex, page1 in db_files:
        all_salt_to_page1[salt_hex] = page1
        if salt_hex in remaining_salts:
            salt_to_page1[salt_hex] = page1

    t0 = time.time()
    if not initialize_hook(pid):
        err = get_last_error_msg()
        print_fn(f"[Hook:{backend_name}] PID={pid} init failed: {err}")
        return 0

    print_fn(f"[Hook:{backend_name}] PID={pid} hook installed in {time.time()-t0:.2f}s — polling for key setup calls...")
    print_fn(f"[Hook:{backend_name}] {get_last_error_msg()}")
    print_fn("[Hook] TIP: Navigate chats, open Moments, Favorites, Stickers to trigger DB opens")

    try:
        return _poll_for_keys(backend, pid, salt_to_page1, salt_to_dbs,
                              key_map, print_fn, timeout, backend_name,
                              all_salt_to_page1)
    finally:
        cleanup_hook()


def _try_hook_on_pid_startup(pid, db_files, salt_to_dbs, key_map, print_fn,
                             timeout=90, dll_budget=20.0):
    """Race-proof startup hook for a freshly created WeChat process.

    Suspends the process at creation, then cycles resume(100ms)/suspend while
    waiting for Weixin.dll to load. Once loaded, the hook is installed while
    the process is still frozen — guaranteeing no DB can be opened before the
    hook is in place. Helper processes (no DLL within dll_budget seconds)
    are released unharmed.
    """
    backend, backend_name = _import_hook_backend()
    if backend is None or not hasattr(backend, 'suspend_process'):
        return _try_hook_on_pid(pid, db_files, salt_to_dbs, key_map, print_fn, timeout)

    remaining_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
    if not remaining_salts:
        return 0
    salt_to_page1 = {}
    all_salt_to_page1 = {}
    for rel, path, sz, salt_hex, page1 in db_files:
        all_salt_to_page1[salt_hex] = page1
        if salt_hex in remaining_salts:
            salt_to_page1[salt_hex] = page1

    suspend_process = backend.suspend_process
    resume_process = backend.resume_process

    hsusp = suspend_process(pid)
    if not hsusp:
        print_fn(f"[Hook] PID={pid}: suspend failed, falling back to live hook")
        return _try_hook_on_pid(pid, db_files, salt_to_dbs, key_map, print_fn, timeout)

    installed = False
    try:
        deadline = time.time() + dll_budget
        loaded = False
        while time.time() < deadline:
            if _dll_loaded_fast(pid):
                loaded = True
                break
            resume_process(hsusp)
            time.sleep(0.1)
            hsusp = suspend_process(pid)
            if not hsusp:
                print_fn(f"[Hook] PID={pid}: re-suspend failed (process exited?)")
                return 0

        if not loaded:
            print_fn(f"[Hook] PID={pid}: no Weixin.dll in {dll_budget:.0f}s (helper process), released")
            return 0

        print_fn(f"[Hook] PID={pid}: Weixin.dll loaded (process FROZEN) — installing hook...")
        t0 = time.time()
        if not backend.initialize_hook(pid):
            print_fn(f"[Hook:{backend_name}] PID={pid} init failed: {backend.get_last_error_msg()}")
            return 0
        installed = True
        print_fn(f"[Hook:{backend_name}] PID={pid} hook installed in {time.time()-t0:.2f}s (before any DB open)")
    finally:
        resume_process(hsusp)

    if not installed:
        return 0
    print_fn(f"[Hook] PID={pid} resumed — polling for key setup calls...")
    try:
        return _poll_for_keys(backend, pid, salt_to_page1, salt_to_dbs,
                              key_map, print_fn, timeout, backend_name,
                              all_salt_to_page1)
    finally:
        backend.cleanup_hook()


def _dll_loaded_fast(pid):
    """Check Weixin.dll presence via a single Toolhelp32 module snapshot.

    Much faster than EnumProcessModules + GetModuleBaseNameA per module —
    critical inside suspend/resume freeze cycles where wall-clock matters.
    """
    import ctypes.wintypes as wt
    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPMODULE = 0x00000008

    class MODULEENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
            ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
            ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
            ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)
    if snap == ctypes.c_void_p(-1).value or not snap:
        return False
    try:
        me = MODULEENTRY32()
        me.dwSize = ctypes.sizeof(MODULEENTRY32)
        if not kernel32.Module32First(snap, ctypes.byref(me)):
            return False
        while True:
            if me.szModule.decode(errors='replace').lower() == 'weixin.dll':
                return True
            if not kernel32.Module32Next(snap, ctypes.byref(me)):
                return False
    finally:
        kernel32.CloseHandle(snap)


def _wait_dll_loaded(pid, timeout=30):
    """Wait until Weixin.dll is loaded in the given PID. Returns True/False."""
    import psutil
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = psutil.Process(pid)
            for m in proc.memory_maps():
                if 'weixin.dll' in m.path.lower():
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        time.sleep(0.2)
    return False


class _ProcessStartWatcher:
    """Low-latency Weixin.exe start detection.

    Uses WMI Win32_ProcessStartTrace (~100ms) when pywin32 is available,
    otherwise falls back to 0.3s psutil polling. Call pop_new_pids() to
    drain newly detected PIDs.
    """

    def __init__(self):
        import threading
        self._new_pids = []
        self._lock = threading.Lock()
        self._running = True
        self._known = set(p for _, p in _find_wechat_pids())
        self._thread = None
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
            self._thread = threading.Thread(target=self._wmi_loop, daemon=True)
        except ImportError:
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def pop_new_pids(self):
        with self._lock:
            pids, self._new_pids = self._new_pids, []
        return pids

    def stop(self):
        self._running = False

    def _report(self, pid):
        with self._lock:
            if pid not in self._known:
                self._known.add(pid)
                self._new_pids.append(pid)

    def _wmi_loop(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
            wmi = locator.ConnectServer(".", "root\\cimv2")
            events = wmi.ExecNotificationQuery("SELECT * FROM Win32_ProcessStartTrace")
            while self._running:
                try:
                    event = events.NextEvent(500)
                except Exception:
                    continue
                if event is None:
                    continue
                name = str(event.Properties_["ProcessName"].Value or "")
                pid = int(event.Properties_["ProcessID"].Value or 0)
                if name.lower() == 'weixin.exe' and pid:
                    self._report(pid)
        finally:
            pythoncom.CoUninitialize()

    def _poll_loop(self):
        while self._running:
            for _, pid in _find_wechat_pids():
                self._report(pid)
            time.sleep(0.3)


def extract_keys_via_hook(db_dir, db_files, salt_to_dbs, key_map, print_fn, timeout=30):
    """Extract DB keys via API hooking (intercepts the key setup chain).

    Phase 1: Try hooking currently running WeChat.
    Phase 2: Wait for WeChat restart and hook the new process.
    """
    backend, backend_name = _import_hook_backend()
    if backend is None:
        print_fn("[Hook] no hook backend available — skipping hook extraction")
        return 0

    is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    if not is_admin:
        print_fn("[Hook] Not running as admin — skipping hook extraction")
        return 0

    # --- Phase 1: Try hooking currently running WeChat ---
    candidates = _find_wechat_pids()
    if candidates:
        print_fn(f"[Hook] Weixin.exe PIDs: {[p for _, p in candidates]}")
        for mem_size, pid in candidates:
            print_fn(f"[Hook] Trying PID={pid} ({mem_size // 1048576}MB)...")
            found = _try_hook_on_pid(pid, db_files, salt_to_dbs, key_map, print_fn, timeout)
            if found > 0:
                return found
        print_fn("[Hook] No keys captured from running WeChat (DBs already open).")
    else:
        print_fn("[Hook] WeChat is not running.")

    # --- Phase 2: Wait for WeChat restart ---
    remaining = len(salt_to_dbs) - len(key_map)
    if remaining == 0:
        return len(key_map)

    print_fn(f"\n[Hook] {'='*50}")
    print_fn(f"[Hook] {remaining} keys still needed.")
    print_fn(f"[Hook] >>> Please CLOSE WeChat completely, then RESTART and LOGIN <<<")
    print_fn(f"[Hook] The hook will automatically detect the new process.")
    print_fn(f"[Hook] Waiting for WeChat restart (timeout: 300s)...")

    watcher = _ProcessStartWatcher()
    wait_start = time.time()
    wait_timeout = 300
    threads = []
    try:
        while time.time() - wait_start < wait_timeout:
            for pid in watcher.pop_new_pids():
                print_fn(f"[Hook] New Weixin.exe: PID={pid}")
                # Each PID handled in its own thread: helpers get a 5s
                # freeze-cycle budget while the main DB process is hooked
                # concurrently — no head-of-line blocking.
                t = threading.Thread(
                    target=_try_hook_on_pid_startup,
                    args=(pid, db_files, salt_to_dbs, key_map, print_fn),
                    kwargs={'timeout': 240},
                    daemon=True)
                t.start()
                threads.append(t)
            if len(key_map) >= len(salt_to_dbs):
                return len(key_map)
            time.sleep(0.2)
        # Window over — let in-flight handler threads finish polling
        for t in threads:
            t.join(timeout=245)
    finally:
        watcher.stop()

    print_fn("[Hook] Timeout waiting for WeChat restart")
    return 0


# ---------------------------------------------------------------------------
#  Strategy 5: Memory scan fallback (hex pattern + targeted salt scan)
# ---------------------------------------------------------------------------

def extract_keys_via_memory_scan(db_dir, db_files, salt_to_dbs, key_map, print_fn):
    """Traditional memory scanning for hex key patterns.
    Only effective on WeChat < 4.1.10 where keys are stored as x'<hex>' strings.
    """
    kernel32 = ctypes.windll.kernel32
    MEM_COMMIT = 0x1000
    READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

    class MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
            ("AllocationProtect", ctypes.wintypes.DWORD), ("_pad1", ctypes.wintypes.DWORD),
            ("RegionSize", ctypes.c_uint64), ("State", ctypes.wintypes.DWORD),
            ("Protect", ctypes.wintypes.DWORD), ("Type", ctypes.wintypes.DWORD),
            ("_pad2", ctypes.wintypes.DWORD),
        ]

    remaining_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
    if not remaining_salts:
        return 0

    hex_re = re.compile(b"x'([0-9a-fA-F]{64,192})'")

    try:
        pids = _find_wechat_pids()
    except Exception:
        return 0

    found_total = 0
    for pid, mem_kb in [(p, m) for m, p in pids]:
        if not remaining_salts:
            break
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            continue
        try:
            # Enumerate regions
            regions = []
            addr = 0
            mbi = MBI()
            while addr < 0x7FFFFFFFFFFF:
                if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr),
                                           ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                    break
                if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                        and 0 < mbi.RegionSize < 500 * 1024 * 1024):
                    regions.append((mbi.BaseAddress, mbi.RegionSize))
                nxt = mbi.BaseAddress + mbi.RegionSize
                if nxt <= addr:
                    break
                addr = nxt

            total_mb = sum(s for _, s in regions) / 1024 / 1024
            print_fn(f"[MemScan] PID={pid}: {len(regions)} regions, {total_mb:.0f}MB")

            scanned = 0
            for base, size in regions:
                buf = ctypes.create_string_buffer(size)
                n = ctypes.c_size_t(0)
                if not kernel32.ReadProcessMemory(h, ctypes.c_uint64(base),
                                                  buf, size, ctypes.byref(n)):
                    continue
                data = buf.raw[:n.value]
                scanned += len(data)

                for m in hex_re.finditer(data):
                    hex_str = m.group(1).decode()
                    hex_len = len(hex_str)
                    addr_match = base + m.start()

                    if hex_len == 96:
                        enc_key_hex = hex_str[:64]
                        salt_hex = hex_str[64:]
                        if salt_hex in remaining_salts:
                            try:
                                enc_key = bytes.fromhex(enc_key_hex)
                            except ValueError:
                                continue
                            for rel, path, sz, s, page1 in db_files:
                                if s == salt_hex and verify_enc_key(enc_key, page1):
                                    key_map[salt_hex] = enc_key_hex
                                    remaining_salts.discard(salt_hex)
                                    found_total += 1
                                    print_fn(f"  [MemScan-FOUND] {rel}")
                                    break

                    elif hex_len == 64 and remaining_salts:
                        enc_key_hex = hex_str
                        try:
                            enc_key = bytes.fromhex(enc_key_hex)
                        except ValueError:
                            continue
                        for rel, path, sz, salt_hex_db, page1 in db_files:
                            if salt_hex_db in remaining_salts and verify_enc_key(enc_key, page1):
                                key_map[salt_hex_db] = enc_key_hex
                                remaining_salts.discard(salt_hex_db)
                                found_total += 1
                                print_fn(f"  [MemScan-FOUND] {rel}")
                                break
        finally:
            kernel32.CloseHandle(h)

    return found_total


# ---------------------------------------------------------------------------
#  Cross-verification
# ---------------------------------------------------------------------------
def cross_verify_keys(db_files, salt_to_dbs, key_map, print_fn):
    """Use already-found keys to verify against remaining salts."""
    missing_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
    if not missing_salts or not key_map:
        return
    unique_keys = list(dict.fromkeys(key_map.values()))
    print_fn(f"\n[CrossCheck] {len(missing_salts)} missing salts, "
             f"trying {len(unique_keys)} known keys...")
    for salt_hex in list(missing_salts):
        for rel, path, sz, s, page1 in db_files:
            if s == salt_hex:
                for key_hex in unique_keys:
                    if len(key_hex) != 64:
                        continue
                    try:
                        enc_key = bytes.fromhex(key_hex)
                    except ValueError:
                        continue
                    if verify_enc_key(enc_key, page1):
                        key_map[salt_hex] = key_hex
                        print_fn(f"  [CrossCheck-FOUND] {rel}")
                        missing_salts.discard(salt_hex)
                        break
                break


# ---------------------------------------------------------------------------
#  Save results
# ---------------------------------------------------------------------------
def save_key_results(db_files, salt_to_dbs, key_map, db_dir, print_fn):
    """Save extracted keys to .wechat_exp_config.json."""
    print_fn(f"\n{'=' * 50}")
    print_fn(f"Results: {len(key_map)}/{len(salt_to_dbs)} salts have keys")

    result = {}
    for rel, path, sz, salt_hex, page1 in db_files:
        if salt_hex in key_map:
            result[rel] = {
                "enc_key": key_map[salt_hex],
                "salt": salt_hex,
                "size_mb": round(sz / 1024 / 1024, 1)
            }
            print_fn(f"  OK: {rel} ({sz / 1024 / 1024:.1f}MB)")
        else:
            print_fn(f"  MISSING: {rel} (salt={salt_hex})")

    if not result:
        raise RuntimeError("No keys extracted")

    # Persist to unified config file
    from engine.config_file import set_db_keys
    flat_keys = {rel: info["enc_key"] for rel, info in result.items()}
    set_db_keys(flat_keys, db_dir=db_dir)
    print_fn(f"Keys saved to .wechat_exp_config.json")

    return key_map


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------
def extract_all_keys(db_dir, print_fn=None, progress_fn=None,
                     use_hook=True, use_mmkv=True, use_memory_scan=True):
    """Extract all DB keys using the best available strategy.

    Args:
        db_dir: Path to WeChat db_storage directory
        print_fn: Logging function (default: print)
        progress_fn: Progress callback (pct, msg)
        use_hook: Enable API hook strategy (requires admin)
        use_mmkv: Enable MMKV offline extraction
        use_memory_scan: Enable memory scanning fallback

    Returns:
        dict: {salt_hex: enc_key_hex}
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    progress_fn(0, "Collecting DB files...")
    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        raise RuntimeError("No .db files found in db_storage directory")

    print_fn(f"Found {len(db_files)} DB files, {len(salt_to_dbs)} unique salts")
    key_map = {}

    # Strategy 1: Load from existing config (fast path)
    progress_fn(5, "Checking existing config...")
    loaded = load_from_config(db_dir, db_files, salt_to_dbs, key_map, print_fn)
    if loaded == len(salt_to_dbs):
        print_fn("[Config] All keys loaded from existing config!")
        return key_map

    # Strategy 2: MMKV offline extraction
    if use_mmkv and len(key_map) < len(salt_to_dbs):
        progress_fn(10, "Trying MMKV extraction...")
        try:
            found = extract_keys_from_mmkv(db_dir, db_files, salt_to_dbs, key_map, print_fn)
            progress_fn(20, f"MMKV: {found} keys extracted")
        except Exception as e:
            print_fn(f"[MMKV] Error: {e}")

    # Strategy 3: Config.Cipher read-only scan (WeChat 4.1.10+, no admin)
    if len(key_map) < len(salt_to_dbs):
        progress_fn(25, "Config.Cipher 只读扫描 (无需管理员)...")
        try:
            found = extract_keys_via_config_cipher(
                db_dir, db_files, salt_to_dbs, key_map, print_fn, progress_fn)
            progress_fn(45, f"Config.Cipher: {found} keys extracted")
        except Exception as e:
            print_fn(f"[Cipher] Error: {e}")

    # Strategy 4: API hooking
    if use_hook and len(key_map) < len(salt_to_dbs):
        progress_fn(50, "Trying API hook...")
        try:
            found = extract_keys_via_hook(db_dir, db_files, salt_to_dbs, key_map, print_fn)
            progress_fn(65, f"Hook: {found} keys extracted")
        except Exception as e:
            print_fn(f"[Hook] Error: {e}")

    # Strategy 5: Memory scan fallback
    if use_memory_scan and len(key_map) < len(salt_to_dbs):
        progress_fn(70, "Trying memory scan fallback...")
        try:
            found = extract_keys_via_memory_scan(db_dir, db_files, salt_to_dbs, key_map, print_fn)
            progress_fn(90, f"MemScan: {found} keys extracted")
        except Exception as e:
            print_fn(f"[MemScan] Error: {e}")

    # Cross-verification
    progress_fn(95, "Cross-verifying keys...")
    cross_verify_keys(db_files, salt_to_dbs, key_map, print_fn)

    # Save
    progress_fn(98, "Saving results...")
    save_key_results(db_files, salt_to_dbs, key_map, db_dir, print_fn)
    progress_fn(100, "Done!")

    return key_map


# ---------------------------------------------------------------------------
#  Standalone CLI
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='WeChat DB Key Extraction')
    ap.add_argument('db_dir', nargs='?',
                    help='Path to db_storage directory')
    ap.add_argument('--no-hook', action='store_true',
                    help='Disable API hook strategy')
    ap.add_argument('--no-mmkv', action='store_true',
                    help='Disable MMKV extraction')
    ap.add_argument('--no-memscan', action='store_true',
                    help='Disable memory scan fallback')
    ap.add_argument('--export', '-o', default=None,
                    help='Export keys to JSON file')
    args = ap.parse_args()

    if args.db_dir:
        db_dir = args.db_dir
    else:
        # Auto-detect: search Documents/xwechat_files for db_storage
        import glob as _glob
        candidates = _glob.glob(
            os.path.expandvars(r'%USERPROFILE%\Documents\xwechat_files\*\db_storage'))
        if not candidates:
            candidates = _glob.glob(r'D:\xwechat_files\*\db_storage')
        if not candidates:
            candidates = _glob.glob(r'C:\xwechat_files\*\db_storage')
        if not candidates:
            print("Error: Cannot auto-detect db_storage. Please specify path.")
            sys.exit(1)
        db_dir = candidates[0]
        print(f"Auto-detected: {db_dir}")

    key_map = extract_all_keys(
        db_dir,
        use_hook=not args.no_hook,
        use_mmkv=not args.no_mmkv,
        use_memory_scan=not args.no_memscan,
    )

    if args.export:
        with open(args.export, 'w', encoding='utf-8') as f:
            json.dump(key_map, f, indent=2)
        print(f"Keys exported to {args.export}")
