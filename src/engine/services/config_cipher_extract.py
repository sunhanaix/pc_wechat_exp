# -*- coding: utf-8 -*-
"""WeChat 4.1.10+ DB key extraction via read-only Config.Cipher scan.

Background
----------
WeChat 4.1+ no longer keeps the raw DB encryption key (x'<64hex>' strings) in
process memory. Instead the WCDB runtime keeps a `com.Tencent.WCDB.Config.Cipher`
string object whose config blob is XOR-obfuscated with a FIXED 32-byte mask and
contains the `x'<64hex key><32hex salt>'` literal for every database.

The mask and object layout are version-stable constants (verified against
Weixin 4.1.12.55, 2026-08). Because the scan only needs PROCESS_VM_READ |
PROCESS_QUERY_INFORMATION, it works WITHOUT administrator rights and WITHOUT
restarting WeChat - a big usability win over the hook strategies.

Layout (per process):
  string "com.Tencent.WCDB.Config.Cipher" -> find its std::string node
    node+0x10 = data ptr, node+0x18 = length            (validated)
    node+0x28 = config_ptr
    obj = read(config_ptr+0x88, 0x28) ; data_ptr=obj+0x8, data_len=obj+0x10
    blob = read(data_ptr, data_len)   (0 < len <= 1024)
  blob ^ XOR_MASK (repeating) -> decode -> find x'<64..192 hex>' literals
  key = first 64 hex chars, optional embedded salt = next 32 hex chars
  verify via HMAC-SHA512 (verify_enc_key); save matches.

Reference: github.com/TANGandXue/wcdb-key-tool (wcdb_key_tool_windows.py).
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import re
import struct
import time

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
IV_SZ = 16

# ---- Config.Cipher constants (version-stable) ----
CONFIG_CIPHER_NAME = b'com.Tencent.WCDB.Config.Cipher'
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
CONFIG_BLOB_MAX = 1024
CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
MAX_USER_ADDRESS = 0x0000_8000_0000_0000

_KNOWN_EXE_NAMES = {'weixin.exe', 'wechat.exe'}

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


# ---------------------------------------------------------------------------
#  HMAC verification (SQLCipher 4, same as key_scan / wechat_key_extract)
# ---------------------------------------------------------------------------
def verify_enc_key(enc_key, db_page1):
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored_hmac = db_page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


# ---------------------------------------------------------------------------
#  Process / memory helpers (pure ctypes, no psutil dependency)
# ---------------------------------------------------------------------------
def find_wechat_pids():
    """Return [(rss_bytes, pid), ...] sorted by memory desc (pure ctypes)."""
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    pids = []
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    psapi = ctypes.windll.psapi
    if kernel32.Process32First(snapshot, ctypes.byref(pe)):
        while True:
            exe = pe.szExeFile.decode('utf-8', errors='replace').lower()
            if exe in _KNOWN_EXE_NAMES:
                pid = pe.th32ProcessID
                h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                                         False, pid)
                mem = 0
                if h:
                    try:
                        pmc = PROCESS_MEMORY_COUNTERS()
                        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                        if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                            mem = pmc.WorkingSetSize
                    finally:
                        kernel32.CloseHandle(h)
                pids.append((mem, pid))
            if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(snapshot)
    pids.sort(key=lambda x: x[0], reverse=True)
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
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi),
                                   ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and 0 < mbi.RegionSize < 500 * 1024 * 1024):
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def _u64_from(data, offset):
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def _probable_32_byte_key(data):
    return (len(data) == KEY_SZ and len(set(data)) >= 15
            and data not in {b"\x00" * KEY_SZ, b"\xff" * KEY_SZ})


def _xor_repeat(data, mask):
    return bytes(v ^ mask[i % len(mask)] for i, v in enumerate(data))


def _iter_chunks(regions, read_region, chunk_size=2 * 1024 * 1024, overlap=0):
    for base, size in regions:
        offset = 0
        tail = b""
        tail_base = base
        while offset < size:
            cur = min(chunk_size, size - offset)
            chunk = read_region(base + offset, cur) or b""
            data_base = tail_base if tail else base + offset
            data = tail + chunk
            if data:
                yield data_base, data
                if overlap:
                    tail = data[-overlap:]
                    tail_base = data_base + max(0, len(data) - len(tail))
                else:
                    tail = b""
                    tail_base = base + offset + cur
            else:
                tail = b""
                tail_base = base + offset + cur
            offset += cur


# ---------------------------------------------------------------------------
#  Candidate extraction from a decoded Config.Cipher blob
# ---------------------------------------------------------------------------
def _blob_key_candidates(blob):
    """XOR-decode the blob and yield (key_hex, embedded_salt_or_None)."""
    if not blob or len(blob) > CONFIG_BLOB_MAX:
        return
    decoded = _xor_repeat(blob, CONFIG_XOR_MASK)
    seen = set()
    for m in CONFIG_LITERAL_RE.finditer(decoded):
        run = m.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            key_hex = run[start:start + 64]
            try:
                key = bytes.fromhex(key_hex)
            except ValueError:
                continue
            if not _probable_32_byte_key(key):
                continue
            embedded = run[start + 64:start + 96] if start + 96 <= len(run) else None
            item = (key_hex, embedded)
            if item not in seen:
                seen.add(item)
                yield item


# ---------------------------------------------------------------------------
#  Per-process scan
# ---------------------------------------------------------------------------
def scan_pid_for_config_cipher(pid, db_files, salt_to_dbs, key_map, print_fn,
                               remaining_salts):
    """Read-only Config.Cipher scan of one WeChat PID.

    Returns dict of stats; mutates key_map / remaining_salts in place.
    """
    stats = {"needles": 0, "nodes": 0, "candidates": 0, "verified": 0}
    h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ|QUERY
    if not h:
        return stats
    try:
        regions = enum_regions(h)
        if not regions:
            return stats

        # 1) locate the literal string occurrences
        needle_addrs = set()
        for base, data in _iter_chunks(regions,
                                       lambda a, s: read_mem(h, a, s),
                                       overlap=len(CONFIG_CIPHER_NAME) - 1):
            pos = data.find(CONFIG_CIPHER_NAME)
            while pos >= 0:
                needle_addrs.add(base + pos)
                pos = data.find(CONFIG_CIPHER_NAME, pos + 1)
        stats["needles"] = len(needle_addrs)
        if not needle_addrs:
            return stats

        pair_patterns = [
            struct.pack("<Q", addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
            for addr in needle_addrs
        ]
        seen_cands = set()

        for base, data in _iter_chunks(regions, lambda a, s: read_mem(h, a, s),
                                       overlap=0x80):
            if not remaining_salts:
                break
            for pat in pair_patterns:
                pos = data.find(pat)
                while pos >= 0:
                    qaddr = base + pos
                    node_base = qaddr - 0x10
                    node = read_mem(h, node_base, 0x50)
                    if node and len(node) >= 0x40:
                        if (_u64_from(node, 0x10) in needle_addrs
                                and _u64_from(node, 0x18) == len(CONFIG_CIPHER_NAME)):
                            config_ptr = _u64_from(node, 0x28)
                            if 0x10000 <= config_ptr < MAX_USER_ADDRESS:
                                stats["nodes"] += 1
                                obj = read_mem(h, config_ptr + 0x88, 0x28)
                                if obj and len(obj) >= 0x18:
                                    data_ptr = _u64_from(obj, 0x8)
                                    data_len = _u64_from(obj, 0x10)
                                    if (0 < data_len <= CONFIG_BLOB_MAX
                                            and 0x10000 <= data_ptr < MAX_USER_ADDRESS):
                                        blob = read_mem(h, data_ptr, int(data_len))
                                        if blob and len(blob) == data_len:
                                            for key_hex, emb_salt in _blob_key_candidates(blob):
                                                cand = (key_hex, emb_salt)
                                                if cand in seen_cands:
                                                    continue
                                                seen_cands.add(cand)
                                                stats["candidates"] += 1
                                                try:
                                                    key = bytes.fromhex(key_hex)
                                                except ValueError:
                                                    continue
                                                if emb_salt and emb_salt in remaining_salts:
                                                    target_salts = [emb_salt]
                                                else:
                                                    target_salts = list(remaining_salts)
                                                for salt_hex in target_salts:
                                                    if salt_hex not in remaining_salts:
                                                        continue
                                                    for rel, _p, _sz, s, page1 in db_files:
                                                        if s == salt_hex and verify_enc_key(key, page1):
                                                            key_map[salt_hex] = key_hex
                                                            remaining_salts.discard(salt_hex)
                                                            stats["verified"] += 1
                                                            print_fn(
                                                                f"  [Cipher-FOUND] {rel} "
                                                                f"salt={salt_hex} -> {key_hex}")
                                                            break
                                                    if salt_hex not in remaining_salts:
                                                        break
                    pos = data.find(pat, pos + 1)
    finally:
        kernel32.CloseHandle(h)
    return stats


# ---------------------------------------------------------------------------
#  Top-level entry (strategy-compatible with key_scan / wechat_key_extract)
# ---------------------------------------------------------------------------
def extract_keys_via_config_cipher(db_dir, db_files, salt_to_dbs, key_map,
                                   print_fn=None, progress_fn=None):
    """Extract DB keys with the read-only Config.Cipher scan.

    No admin rights, no WeChat restart, no hooking. Works on WeChat 4.1.10+
    (verified 4.1.12.55). Mutates key_map in place; returns keys found.

    Args:
        db_dir: path to WeChat db_storage (used only for messages)
        db_files: list from collect_db_files()
        salt_to_dbs: {salt_hex: [rel,...]}
        key_map: dict being filled {salt_hex: key_hex}
        print_fn / progress_fn: optional callbacks
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    remaining = set(salt_to_dbs) - set(key_map)
    if not remaining:
        return 0

    pids = find_wechat_pids()
    if not pids:
        print_fn("[Cipher] 未检测到微信进程。请先启动微信并登录，然后重试。")
        print_fn("[Cipher] 启动微信后无需任何额外操作——本扫描为只读，不注入、不重启。")
        return 0

    progress_fn(0, "Config.Cipher 只读扫描：检测到微信进程，开始定位密钥对象...")
    print_fn(f"[Cipher] 检测到微信进程: {[p for _, p in pids]}")
    print_fn("[Cipher] 只读扫描 WCDB Config.Cipher 对象 (无需管理员权限) ...")

    t0 = time.time()
    total_found = 0
    for mem, pid in pids:
        if not remaining:
            break
        stats = scan_pid_for_config_cipher(pid, db_files, salt_to_dbs, key_map,
                                           print_fn, remaining)
        progress_fn(min(90, 20 + total_found * 5),
                    f"进程 PID={pid}: 验证 {stats['verified']} 个密钥...")
        if stats["verified"]:
            total_found += stats["verified"]
            print_fn(f"[Cipher] PID={pid}: {stats['verified']} 个密钥验证通过 "
                     f"(节点 {stats['nodes']}, 候选 {stats['candidates']})")

    print_fn(f"[Cipher] 扫描完成: {time.time() - t0:.1f}s, "
             f"共验证 {total_found} 个密钥")
    return total_found

