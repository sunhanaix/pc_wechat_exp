"""
密钥提取 — 从微信进程内存扫描 SQLCipher 密钥（支持 Weixin.exe / WeChat.exe）。
WCDB 为每个 DB 缓存: x'<64hex_enc_key><32hex_salt>'
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import json
import os
import re
import struct
import sys
import time

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16

kernel32 = ctypes.windll.kernel32
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
        ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
    ]


_KNOWN_EXE_NAMES = {'weixin.exe', 'wechat.exe'}


def get_pids():
    """返回所有微信进程的 (pid, mem_kb) 列表，按内存降序。

    同时支持 Weixin.exe (新版 4.x) 和 WeChat.exe (旧版 3.x)。
    """
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    psapi = ctypes.windll.psapi
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise RuntimeError("无法创建进程快照")

    pids = []
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)

    if kernel32.Process32First(snapshot, ctypes.byref(pe)):
        while True:
            exe_name = pe.szExeFile.decode('utf-8', errors='replace')
            if exe_name.lower() in _KNOWN_EXE_NAMES:
                pid = pe.th32ProcessID
                h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
                mem_kb = 0
                if h:
                    try:
                        pmc = PROCESS_MEMORY_COUNTERS()
                        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                        if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                            mem_kb = pmc.WorkingSetSize // 1024
                    finally:
                        kernel32.CloseHandle(h)
                pids.append((pid, mem_kb))
            if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                break

    kernel32.CloseHandle(snapshot)
    if not pids:
        raise RuntimeError("微信(Weixin.exe/WeChat.exe)未运行")
    pids.sort(key=lambda x: x[1], reverse=True)
    return pids


def read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
        return buf.raw[:n.value]
    return None


def enum_regions(h):
    regs = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def verify_enc_key(enc_key, db_page1):
    """通过 HMAC-SHA512 校验 page 1 验证 enc_key 是否正确。"""
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def _clean_wxid(wxid):
    """去除 wxid 第二个下划线后的后缀。
    'wxid_cpyn7pe119rs21_10e8' -> 'wxid_cpyn7pe119rs21'
    """
    if not wxid or not wxid.startswith('wxid_'):
        return wxid
    parts = wxid.split('_')
    if len(parts) >= 3:
        return '_'.join(parts[:2])
    return wxid


def _extract_keys_from_mmkv(db_dir, db_files, salt_to_dbs, print_fn):
    """从 MMKV 配置文件中提取数据库加密密钥（微信 4.1.10.x+）。

    db_storage/MMKV/ 下的 MMKV 文件使用 AES-GCM 加密。
    AES 密钥通过 MD5(str(code) + cleaned_wxid).hex()[:16] 派生。

    返回 key_map dict {salt_hex: enc_key_hex}。
    """
    key_map = {}

    mmkv_dir = os.path.join(db_dir, 'MMKV')
    if not os.path.isdir(mmkv_dir):
        print_fn("[MMKV] db_storage/MMKV/ 不存在 — 跳过 MMKV 提取")
        return key_map

    # 从 db_dir 路径提取 wxid
    # e.g. D:\xwechat_files\wxid_cpyn7pe119rs21_10e8\db_storage
    wxid_full = os.path.basename(os.path.dirname(db_dir))
    wxid_clean = _clean_wxid(wxid_full)
    if not wxid_clean or not wxid_clean.startswith('wxid_'):
        print_fn(f"[MMKV] 无法从路径提取 wxid: {db_dir}")
        return key_map

    print_fn(f"[MMKV] wxid: {wxid_full} -> cleaned: {wxid_clean}")

    # 枚举 MMKV 文件，从文件名提取 code
    # 模式: f<hex>tinfo.mmkv -> code = int(hex, 16)
    # 其他: storage_common 等 -> 使用 None (仅 wxid 回退)
    import re as _re
    _CODE_RE = _re.compile(r'^f([0-9a-fA-F]+)tinfo\.mmkv$')

    codes = {}
    try:
        for fname in os.listdir(mmkv_dir):
            if fname.endswith('.crc'):
                continue
            m = _CODE_RE.match(fname)
            if m:
                code = int(m.group(1), 16)
                codes[code] = os.path.join(mmkv_dir, fname)
            elif fname.endswith('.mmkv'):
                codes[fname] = os.path.join(mmkv_dir, fname)
    except OSError as e:
        print_fn(f"[MMKV] 无法列出 MMKV 目录: {e}")
        return key_map

    if not codes:
        print_fn("[MMKV] 未找到 .mmkv 文件")
        return key_map

    print_fn(f"[MMKV] 找到 {len(codes)} 个 MMKV 文件")

    # 构建 rel_path -> (salt_hex, page1) 查找表
    path_to_info = {}
    for rel, path, sz, salt_hex, page1 in db_files:
        path_to_info[rel] = (salt_hex, page1)
        path_to_info[rel.replace('\\', '/')] = (salt_hex, page1)

    # 尝试导入 pycryptodome (项目已有依赖)
    try:
        from Crypto.Cipher import AES
    except ImportError:
        print_fn("[MMKV] pycryptodome 不可用 — 跳过 MMKV 提取")
        return key_map

    _HEX64_RE = _re.compile(b'([0-9a-fA-F]{64})')

    # 逐个尝试每个 code
    for code_or_label, filepath in codes.items():
        if len(key_map) >= len(salt_to_dbs):
            break

        # 派生 AES 密钥
        if isinstance(code_or_label, int):
            aes_key_str = hashlib.md5(
                f"{code_or_label}{wxid_clean}".encode()
            ).hexdigest()[:16]
        else:
            aes_key_str = hashlib.md5(
                wxid_clean.encode()
            ).hexdigest()[:16]
        aes_key = aes_key_str.encode('ascii')

        label = code_or_label if isinstance(code_or_label, int) else f"'{code_or_label}'"
        print_fn(f"[MMKV] 尝试 code={label} key={aes_key_str}...")

        # 读取并解密 MMKV 文件
        try:
            with open(filepath, 'rb') as f:
                raw = f.read()
        except (IOError, PermissionError) as e:
            print_fn(f"[MMKV] 无法读取 {filepath}: {e}")
            continue

        if len(raw) < 24:
            continue

        total_size = struct.unpack('<I', raw[:4])[0]
        # MMKV files may be padded to page size (4096); use header value
        if total_size < 33 or 4 + total_size > len(raw):
            continue

        iv = raw[4:20]
        ciphertext = raw[20:4 + total_size - 16]
        auth_tag = raw[4 + total_size - 16:4 + total_size]

        try:
            cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv)
            plaintext = cipher.decrypt_and_verify(ciphertext, auth_tag)
        except (ValueError, KeyError):
            print_fn(f"[MMKV] GCM 认证失败 code={label}")
            continue
        except Exception as e:
            print_fn(f"[MMKV] 解密错误 code={label}: {e}")
            continue

        print_fn(f"[MMKV] 解密 {os.path.basename(filepath)}: {len(plaintext)} 字节")

        # 在解密数据中搜索 DB 路径 → 64 字符 hex 密钥
        for rel, (salt_hex, page1) in path_to_info.items():
            if salt_hex in key_map:
                continue

            for sep in [b'\\', b'/']:
                path_bytes = rel.replace('\\', sep.decode()).encode()
                pos = plaintext.find(path_bytes)
                while pos >= 0:
                    nearby_start = pos + len(path_bytes)
                    nearby_end = min(len(plaintext), nearby_start + 512)
                    nearby = plaintext[nearby_start:nearby_end]

                    for hex_match in _HEX64_RE.finditer(nearby):
                        candidate_hex = hex_match.group(0).decode()
                        try:
                            candidate_key = bytes.fromhex(candidate_hex)
                        except ValueError:
                            continue
                        if verify_enc_key(candidate_key, page1):
                            key_map[salt_hex] = candidate_hex
                            print_fn(f"\n  [MMKV-FOUND] salt={salt_hex}")
                            print_fn(f"    enc_key={candidate_hex}")
                            print_fn(f"    DB: {rel}")
                            break

                    if salt_hex in key_map:
                        break
                    pos = plaintext.find(path_bytes, pos + 1)

                if salt_hex in key_map:
                    break

    print_fn(f"[MMKV] 从 MMKV 提取了 {len(key_map)}/{len(salt_to_dbs)} 个密钥")
    return key_map


def _extract_keys_via_hook(db_dir, db_files, salt_to_dbs, key_map, print_fn, timeout=30):
    """Extract DB keys via py_wx_key API hooking (intercepts sqlite3_key()).

    Requires: py_wx_key installed, WeChat running, admin privileges.

    Returns updated key_map dict (mutated in place for incremental verification).
    """
    # Check prerequisites
    try:
        import wx_key
    except ImportError:
        print_fn("[Hook] py_wx_key not installed — skipping hook extraction")
        return

    # Check admin (required for DLL injection)
    is_admin = False
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        pass
    if not is_admin:
        print_fn("[Hook] Not running as admin — skipping hook extraction")
        return

    # Find WeChat PID via psutil — use largest memory process (main proc)
    try:
        import psutil
    except ImportError:
        print_fn("[Hook] psutil not installed — skipping hook extraction")
        return

    candidates = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            info = proc.info
            if info['name'] and info['name'].lower() == 'weixin.exe':
                mem = info['memory_info'].rss if info['memory_info'] else 0
                candidates.append((mem, info['pid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not candidates:
        print_fn("[Hook] Weixin.exe not running — skipping hook extraction")
        return

    # Pick the largest — that's the main UI process with DB connections
    candidates.sort(reverse=True)
    print_fn(f"[Hook] Weixin.exe PIDs found: {[p for _,p in candidates]}")

    # Try each candidate in descending size order
    for mem_size, pid in candidates:
        print_fn(f"[Hook] Trying PID={pid} ({mem_size//1048576}MB)...")

        # Build salt -> page1 lookup for remaining salts
        remaining_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
        salt_to_page1 = {}
        for rel, path, sz, salt_hex, page1 in db_files:
            if salt_hex in remaining_salts:
                salt_to_page1[salt_hex] = page1

        # Install hook
        if not wx_key.initialize_hook(pid):
            err = wx_key.get_last_error_msg()
            print_fn(f"[Hook] PID={pid} init failed: {err}")
            continue
        print_fn("[Hook] Hook installed, polling for keys...")

        try:
            import time as _time
            start = _time.time()
            found_count = 0
            while _time.time() - start < timeout:
                res = wx_key.poll_key_data()
                if res and 'key' in res:
                    key_hex = res['key'].lower()
                    if len(key_hex) != 64:
                        continue
                    key_bytes = bytes.fromhex(key_hex)

                    # Verify against all remaining salts
                    for salt_hex, page1 in list(salt_to_page1.items()):
                        if verify_enc_key(key_bytes, page1):
                            key_map[salt_hex] = key_hex
                            found_count += 1
                            print_fn(f"  [Hook-FOUND] salt={salt_hex} DBs={salt_to_dbs[salt_hex]}")
                            del salt_to_page1[salt_hex]

                            if len(key_map) >= len(salt_to_dbs):
                                break

                    if len(key_map) >= len(salt_to_dbs):
                        break

                _time.sleep(0.1)

            print_fn(f"[Hook] Captured {found_count} keys in {_time.time() - start:.1f}s")
        finally:
            wx_key.cleanup_hook()
            print_fn("[Hook] Hook cleaned up")

        if found_count > 0:
            break  # Success, don't try other PIDs


def collect_db_files(db_dir):
    """遍历 db_dir 收集所有 .db 文件及其 salt。"""
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


def scan_memory_for_keys(data, hex_re, db_files, salt_to_dbs, key_map,
                         remaining_salts, base_addr, pid, print_fn):
    matches = 0
    for m in hex_re.finditer(data):
        hex_str = m.group(1).decode()
        addr = base_addr + m.start()
        matches += 1
        hex_len = len(hex_str)

        if hex_len == 96:
            enc_key_hex = hex_str[:64]
            salt_hex = hex_str[64:]
            if salt_hex in remaining_salts:
                enc_key = bytes.fromhex(enc_key_hex)
                for rel, path, sz, s, page1 in db_files:
                    if s == salt_hex and verify_enc_key(enc_key, page1):
                        key_map[salt_hex] = enc_key_hex
                        remaining_salts.discard(salt_hex)
                        dbs = salt_to_dbs[salt_hex]
                        print_fn(f"\n  [FOUND] salt={salt_hex}")
                        print_fn(f"    enc_key={enc_key_hex}")
                        print_fn(f"    PID={pid} 地址: 0x{addr:016X}")
                        print_fn(f"    数据库: {', '.join(dbs)}")
                        break

        elif hex_len == 64:
            if not remaining_salts:
                continue
            enc_key_hex = hex_str
            enc_key = bytes.fromhex(enc_key_hex)
            for rel, path, sz, salt_hex_db, page1 in db_files:
                if salt_hex_db in remaining_salts and verify_enc_key(enc_key, page1):
                    key_map[salt_hex_db] = enc_key_hex
                    remaining_salts.discard(salt_hex_db)
                    dbs = salt_to_dbs[salt_hex_db]
                    print_fn(f"\n  [FOUND] salt={salt_hex_db}")
                    print_fn(f"    enc_key={enc_key_hex}")
                    print_fn(f"    PID={pid} 地址: 0x{addr:016X}")
                    print_fn(f"    数据库: {', '.join(dbs)}")
                    break

        elif hex_len > 96 and hex_len % 2 == 0:
            enc_key_hex = hex_str[:64]
            salt_hex = hex_str[-32:]
            if salt_hex in remaining_salts:
                enc_key = bytes.fromhex(enc_key_hex)
                for rel, path, sz, s, page1 in db_files:
                    if s == salt_hex and verify_enc_key(enc_key, page1):
                        key_map[salt_hex] = enc_key_hex
                        remaining_salts.discard(salt_hex)
                        dbs = salt_to_dbs[salt_hex]
                        print_fn(f"\n  [FOUND] salt={salt_hex} (long hex {hex_len})")
                        print_fn(f"    enc_key={enc_key_hex}")
                        print_fn(f"    PID={pid} 地址: 0x{addr:016X}")
                        print_fn(f"    数据库: {', '.join(dbs)}")
                        break
    return matches


def cross_verify_keys(db_files, salt_to_dbs, key_map, print_fn):
    """用已找到的 key 交叉验证未匹配的 salt。"""
    missing_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
    if not missing_salts or not key_map:
        return
    # Collect unique key hex values first — avoids redundant HMAC computations
    # and prevents RuntimeError from modifying key_map during dict iteration.
    unique_keys = list(dict.fromkeys(key_map.values()))
    print_fn(f"\n还有 {len(missing_salts)} 个 salt 未匹配，尝试交叉验证 ({len(unique_keys)} 个唯一密钥)...")
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
                        print_fn(f"  [CROSS] salt={salt_hex} 匹配成功")
                        missing_salts.discard(salt_hex)
                        break
                break  # found the DB file matching this salt


def save_results(db_files, salt_to_dbs, key_map, db_dir, out_file, print_fn):
    """输出扫描结果并保存到统一配置文件。"""
    print_fn(f"\n{'=' * 60}")
    print_fn(f"结果: {len(key_map)}/{len(salt_to_dbs)} salts 找到密钥")

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
        print_fn(f"\n[!] 未提取到任何密钥")
        raise RuntimeError("未能从任何微信进程中提取到密钥")

    # Persist to unified config file (.wechat_exp_config.json)
    from engine.config_file import set_db_keys
    flat_keys = {rel: info["enc_key"] for rel, info in result.items()}
    set_db_keys(flat_keys, db_dir=db_dir)
    print_fn(f"\n密钥已保存到 .wechat_exp_config.json")

    # Also write to out_file for backward compatibility
    if out_file:
        result["_db_dir"] = db_dir
        out_dir = os.path.dirname(out_file)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print_fn(f"(同时保存到: {out_file})")

    missing = [rel for rel, path, sz, salt_hex, page1 in db_files if salt_hex not in key_map]
    if missing:
        print_fn(f"\n未找到密钥的数据库:")
        for rel in missing:
            print_fn(f"  {rel}")


def _targeted_salt_scan(db_files, salt_to_dbs, key_map, print_fn, progress_fn):
    """Fallback: for each missing salt, search WeChat memory for both hex-string
    and raw-binary representations of the salt, then try to extract a nearby key.

    Phase 1: search for the 32-char hex salt string, look for nearby 64-char hex keys.
    Phase 2: search for the raw 16-byte salt, test adjacent 32-byte windows as raw keys.
    """
    remaining = set(salt_to_dbs.keys()) - set(key_map.keys())
    if not remaining:
        return

    # Build lookup: salt_hex -> list of (rel, page1)
    missing_lookup = {}
    for rel, path, sz, s, page1 in db_files:
        if s in remaining:
            missing_lookup.setdefault(s, []).append((rel, page1))
    if not missing_lookup:
        return

    print_fn(f"\n[*] 针对性扫描 {len(missing_lookup)} 个缺失 salt...")

    try:
        pids = get_pids()
    except RuntimeError:
        return

    # ---- Phase 1: search for hex salt string + nearby hex keys ----
    salt_hex_re = re.compile(b'(' + b'|'.join(s.encode() for s in missing_lookup) + b')')
    key_hex_nearby_re = re.compile(b'([0-9a-fA-F]{64})')

    for pid, mem_kb in pids:
        if not missing_lookup:
            break
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            continue
        try:
            regions = enum_regions(h)
            for base, size in regions:
                data = read_mem(h, base, size)
                if not data:
                    continue
                for salt_match in salt_hex_re.finditer(data):
                    salt_hex = salt_match.group(0).decode()
                    if salt_hex not in missing_lookup:
                        continue
                    salt_pos = salt_match.start()
                    ctx_start = max(0, salt_pos - 256)
                    ctx_end = min(len(data), salt_pos + len(salt_match.group(0)) + 256)
                    ctx = data[ctx_start:ctx_end]

                    for key_match in key_hex_nearby_re.finditer(ctx):
                        key_hex = key_match.group(0).decode()
                        try:
                            enc_key = bytes.fromhex(key_hex)
                        except ValueError:
                            continue
                        for rel, page1 in missing_lookup[salt_hex]:
                            if verify_enc_key(enc_key, page1):
                                key_map[salt_hex] = key_hex
                                print_fn(f"\n  [TARGETED-HEX] salt={salt_hex}")
                                print_fn(f"    enc_key={key_hex}")
                                print_fn(f"    PID={pid} 地址: 0x{(base + ctx_start + key_match.start()):016X}")
                                print_fn(f"    数据库: {rel}")
                                del missing_lookup[salt_hex]
                                if missing_lookup:
                                    salt_hex_re = re.compile(
                                        b'(' + b'|'.join(s.encode() for s in missing_lookup) + b')')
                                break
                        if salt_hex not in missing_lookup:
                            break
        finally:
            kernel32.CloseHandle(h)

    # ---- Phase 2: search for raw 16-byte salt + adjacent raw 32-byte key ----
    if missing_lookup:
        print_fn(f"  [TARGETED] Phase 1 hex完成, 剩余 {len(missing_lookup)} ... 尝试原始字节扫描")
        # Only scan the main (largest) WeChat process for raw bytes
        main_pid, main_mem = pids[0] if pids else (None, 0)
        if main_pid:
            h = kernel32.OpenProcess(0x0010 | 0x0400, False, main_pid)
            if h:
                try:
                    regions = enum_regions(h)
                    for base, size in regions:
                        if not missing_lookup:
                            break
                        data = read_mem(h, base, size)
                        if not data:
                            continue
                        for salt_hex in list(missing_lookup.keys()):
                            salt_bytes = bytes.fromhex(salt_hex)
                            search_start = 0
                            while True:
                                pos = data.find(salt_bytes, search_start)
                                if pos < 0:
                                    break
                                search_start = pos + 1
                                # Try 32-byte windows at various offsets around salt
                                for offset in (-32, -16, 0, 16, 32):
                                    key_start = pos + offset
                                    if 0 <= key_start <= len(data) - 32:
                                        candidate = data[key_start:key_start + 32]
                                        if len(candidate) == 32:
                                            for rel, page1 in missing_lookup[salt_hex]:
                                                if verify_enc_key(candidate, page1):
                                                    key_hex = candidate.hex()
                                                    key_map[salt_hex] = key_hex
                                                    print_fn(f"\n  [TARGETED-RAW] salt={salt_hex}")
                                                    print_fn(f"    enc_key={key_hex}")
                                                    print_fn(f"    PID={main_pid} 地址: 0x{(base + key_start):016X}")
                                                    print_fn(f"    数据库: {rel}")
                                                    del missing_lookup[salt_hex]
                                                    break
                                        if salt_hex not in missing_lookup:
                                            break
                                if salt_hex not in missing_lookup:
                                    break
                finally:
                    kernel32.CloseHandle(h)

    if not missing_lookup:
        print_fn(f"  [TARGETED] 所有缺失 salt 已找到！")
    else:
        print_fn(f"  [TARGETED] 仍有 {len(missing_lookup)} 个 salt 未找到 (冷分片，密钥不在内存中)")


def run_key_scan(db_dir, out_file, print_fn=None, progress_fn=None):
    """主入口：扫描微信进程内存提取所有 DB 密钥。
    Args:
        db_dir: 微信 db_storage 目录
        out_file: 输出 JSON 路径
        print_fn: 日志输出函数
        progress_fn: 进度回调 (pct, msg)
    Returns: key_map dict {salt: enc_key_hex}
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        raise RuntimeError("未在数据目录中找到任何 .db 文件")

    print_fn(f"找到 {len(db_files)} 个数据库, {len(salt_to_dbs)} 个不同的salt")

    hex_re = re.compile(b"x'([0-9a-fA-F]{64,192})'")
    key_map = {}
    t0 = time.time()

    # Strategy 1: MMKV 离线提取 (新版本)
    mmkv_keys = _extract_keys_from_mmkv(db_dir, db_files, salt_to_dbs, print_fn)
    if mmkv_keys:
        key_map.update(mmkv_keys)
        print_fn(f"[MMKV] {len(mmkv_keys)} 个密钥从 MMKV 配置文件提取")

    # Strategy 2: py_wx_key API Hook (注入拦截 sqlite3_key)
    remaining_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
    if remaining_salts:
        _extract_keys_via_hook(db_dir, db_files, salt_to_dbs, key_map, print_fn)

    remaining_salts = set(salt_to_dbs.keys()) - set(key_map.keys())
    if remaining_salts:
        print_fn(f"[*] {len(remaining_salts)} 个 salt 仍需内存扫描")

    all_hex_matches = 0
    pids = []

    if remaining_salts:
        pids = get_pids()
        for pid_idx, (pid, mem_kb) in enumerate(pids):
            progress_fn(10 + pid_idx * 30, f"扫描进程 PID={pid} ({mem_kb // 1024}MB)...")

            h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
            if not h:
                print_fn(f"[WARN] 无法打开进程 PID={pid}，跳过")
                continue

            try:
                regions = enum_regions(h)
                total_bytes = sum(s for _, s in regions)
                total_mb = total_bytes / 1024 / 1024
                print_fn(f"\n[*] 扫描 PID={pid} ({total_mb:.0f}MB, {len(regions)} 区域)")

                scanned_bytes = 0
                for reg_idx, (base, size) in enumerate(regions):
                    data = read_mem(h, base, size)
                    scanned_bytes += size
                    if not data:
                        continue

                    all_hex_matches += scan_memory_for_keys(
                        data, hex_re, db_files, salt_to_dbs,
                        key_map, remaining_salts, base, pid, print_fn,
                    )

                    if (reg_idx + 1) % 200 == 0:
                        elapsed = time.time() - t0
                        pct = min(99, 10 + scanned_bytes / total_bytes * 80)
                        progress_fn(pct, f"已匹配 {len(key_map)}/{len(salt_to_dbs)} salt, {elapsed:.0f}s")
            finally:
                kernel32.CloseHandle(h)

            if not remaining_salts:
                print_fn(f"\n[+] 所有密钥已找到，跳过剩余进程")
                break

    elapsed = time.time() - t0
    print_fn(f"\n扫描完成: {elapsed:.1f}s, {len(pids)} 个进程, {all_hex_matches} hex模式")

    cross_verify_keys(db_files, salt_to_dbs, key_map, print_fn)
    _targeted_salt_scan(db_files, salt_to_dbs, key_map, print_fn, progress_fn)
    save_results(db_files, salt_to_dbs, key_map, db_dir, out_file, print_fn)

    return key_map
