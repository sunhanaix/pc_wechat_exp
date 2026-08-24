"""py_wx_key_v3 — hook at the key-setup function ENTRY (version-tolerant).

Strategy (reverse-engineered from WeChat 4.1.11.54 Weixin.dll):

  wrapper (found via PATTERN_V1)
    +0x68: call inner_func          ; inner_func(ctx, pKey, nKey, flags)
      - copies [pKey] into an internal std::string
      - XOR-deobfuscates it IN PLACE with a 32-byte repeating mask
        (mask referenced by `lea r8, [rip+disp]` in the byte-loop and by
         two `xorps xmm, [rip+disp]` in the SSE path — same 32 bytes)
      - plaintext key is then used for sqlite3_key / codec setup

Hook design (v3):
  * Hook inner_func ENTRY — the most stable location in the whole chain:
    - no mid-function magic offsets (v2's wrapper+0x989 broke on 4.1.11)
    - stable prologue: push rbp/push rsi/push rdi/sub rsp,0x50/lea rbp,[rsp+0x50]
      = exactly 12 bytes, matching the 12-byte absolute JMP patch
  * Shellcode captures the RAW (still-obfuscated) key bytes from [rdx],
    length r8d (clamped to [16, 32] bytes copied).
  * The XOR mask is extracted at install time by scanning inner_func's
    first 0x200 bytes for `lea r8, [rip+disp]` (4C 8D 05) and reading
    32 bytes from the referenced address in the remote process.
  * Deobfuscation (key[i] ^= mask[i & 31]) happens in Python, not in
    shellcode. Both deobfuscated and raw candidates are queued — the
    caller's HMAC verification (verify_enc_key) picks the right one.
  * If a future version drops the XOR obfuscation, mask extraction fails
    gracefully and the raw candidate still works.

Drop-in API compatible with py_wx_key_v2 / wx_key usage pattern.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import threading
import time

from keystone import Ks, KS_ARCH_X86, KS_MODE_64

# --- Constants ---
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_EXECUTE_READWRITE = 0x40

# Pattern for the wrapper function (WeChat >= 4.1.6.14, still valid on 4.1.11.54)
PATTERN_V1 = bytes([
    0x24, 0x50, 0x48, 0xC7, 0x45, 0x00, 0xFE, 0xFF, 0xFF, 0xFF,
    0x44, 0x89, 0xCF, 0x44, 0x89, 0xC3, 0x49, 0x89, 0xD6,
    0x48, 0x89, 0xCE, 0x48, 0x89
])

# Shared data layout (remote, second page):
#   offset 0:    DWORD dataSize (bytes copied)
#   offset 4:    BYTE[4096] keyBuffer (raw, still obfuscated)
#   offset 4100: DWORD sequenceNumber
#   offset 4104: DWORD keyLenSeen (string size as seen at entry)
#   offset 4108: DWORD heartbeat (0xDD00DD00 = shellcode executing)
#   offset 4112: DWORD callCount (incremented on EVERY call, diagnostics)
#   offset 4116: DWORD lastR8 (r8d of most recent call, diagnostics)
#   offset 4120: DWORD lastR9 (r9d of most recent call, diagnostics)
#   offset 4124: DWORD lastStrSize ([rdx+0x10] of most recent call)
SHARED_DATA_SIZE = 4128
MAX_KEY_COPY = 4096
REMOTE_ALLOC_SIZE = 8192

# inner_func region to scan for the XOR mask reference
_MASK_SCAN_SIZE = 0x200

# --- Windows DLLs ---
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualFreeEx.restype = wt.BOOL
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.WriteProcessMemory.restype = wt.BOOL
kernel32.CloseHandle.restype = wt.BOOL
psapi.EnumProcessModules.restype = wt.BOOL
psapi.GetModuleBaseNameA.restype = wt.DWORD
psapi.GetModuleInformation.restype = wt.BOOL

# --- State ---
_state = {
    'hProcess': None,
    'pid': 0,
    'remote_shellcode': None,
    'remote_shared_data': None,
    'last_sequence': 0,
    'pending_keys': [],
    'lock': threading.RLock(),
    'running': False,
    'poll_thread': None,
    'last_error': '',
    'hook_addr': 0,
    'heartbeat_seen': False,
    'xor_mask': None,          # 32-byte mask extracted from remote process
    'inner_func_addr': 0,
    'wrapper_addr': 0,
}


def _err(msg):
    _state['last_error'] = msg
    return False


def _find_weixin_pid():
    """Return (pid, mem_mb) of largest weixin.exe process."""
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
    if candidates:
        return candidates[0][1], candidates[0][0] // 1048576
    return None, 0


def _find_weixin_dll(hProcess):
    """Find Weixin.dll base, size and file path in the target process."""
    hMods = (wt.HMODULE * 1024)()
    cbNeeded = wt.DWORD()
    if not psapi.EnumProcessModules(hProcess, hMods, ctypes.sizeof(hMods),
                                    ctypes.byref(cbNeeded)):
        return None
    count = cbNeeded.value // ctypes.sizeof(wt.HMODULE)
    for i in range(count):
        hMod = ctypes.c_void_p(hMods[i])
        name_buf = ctypes.create_string_buffer(260)
        if psapi.GetModuleBaseNameA(hProcess, hMod, name_buf, 260):
            if name_buf.value.decode().lower() == 'weixin.dll':
                class MODINFO(ctypes.Structure):
                    _fields_ = [('base', ctypes.c_void_p), ('size', wt.DWORD),
                                ('entry', ctypes.c_void_p)]
                mi = MODINFO()
                if psapi.GetModuleInformation(hProcess, hMod, ctypes.byref(mi),
                                               ctypes.sizeof(mi)):
                    path_buf = ctypes.create_string_buffer(520)
                    path = None
                    psapi.GetModuleFileNameExA.restype = wt.DWORD
                    if psapi.GetModuleFileNameExA(hProcess, hMod, path_buf, 520):
                        path = path_buf.value.decode(errors='replace')
                    return {'base': mi.base, 'size': mi.size, 'path': path}
    return None


def _read_remote(hProcess, addr, size):
    buf = ctypes.create_string_buffer(size)
    br = ctypes.c_size_t()
    if kernel32.ReadProcessMemory(hProcess, ctypes.c_void_p(int(addr)),
                                   buf, size, ctypes.byref(br)):
        return buf.raw[:br.value]
    return None


def _write_remote(hProcess, addr, data):
    buf = ctypes.create_string_buffer(data, len(data))
    bw = ctypes.c_size_t()
    return kernel32.WriteProcessMemory(hProcess, ctypes.c_void_p(int(addr)),
                                        buf, len(data), ctypes.byref(bw))


def _find_pattern_in_remote(hProcess, base, size, pattern):
    """Search for pattern in remote process memory. Returns list of addresses."""
    chunk_size = 2 * 1024 * 1024
    results = []
    offset = 0
    plen = len(pattern)
    while offset < size:
        read_size = min(chunk_size + plen, size - offset)
        data = _read_remote(hProcess, base + offset, read_size)
        if not data:
            break
        start = 0
        while True:
            i = data.find(pattern, start)
            if i < 0:
                break
            results.append(base + offset + i)
            start = i + 1
        offset += chunk_size
    return results


# --- File-based fast resolution cache: {path: (mtime, wrapper_rva, inner_rva)} ---
_file_cache = {}


def _resolve_rvas_from_file(dll_path):
    """Find wrapper/inner_func RVAs by scanning the DLL file on disk.

    ~100x faster than scanning 180MB of remote process memory — critical
    for catching the key-setup call during WeChat's auto-login window.
    Results are cached per (path, mtime).
    Returns (wrapper_rva, inner_rva) or (None, None).
    """
    import os
    try:
        mtime = os.path.getmtime(dll_path)
    except OSError:
        return None, None
    cached = _file_cache.get(dll_path)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]

    try:
        data = open(dll_path, 'rb').read()
    except (IOError, PermissionError):
        return None, None

    pe_off = struct.unpack('<I', data[0x3c:0x40])[0]
    if data[pe_off:pe_off+4] != b'PE\x00\x00':
        return None, None
    num_sec = struct.unpack('<H', data[pe_off+6:pe_off+8])[0]
    opt_size = struct.unpack('<H', data[pe_off+20:pe_off+22])[0]
    sec_off = pe_off + 24 + opt_size
    secs = []
    for i in range(num_sec):
        off = sec_off + i*40
        vsize, vaddr, rsize, raddr = struct.unpack('<IIII', data[off+8:off+24])
        secs.append((vaddr, max(vsize, rsize), raddr, rsize))

    def off2rva(fo):
        for vaddr, sz, raddr, rsize in secs:
            if raddr <= fo < raddr + rsize:
                return vaddr + (fo - raddr)
        return None

    pos = data.find(PATTERN_V1)
    if pos < 0:
        return None, None
    wrapper_fo = pos - 3
    call = data[wrapper_fo+0x68:wrapper_fo+0x68+5]
    if len(call) != 5 or call[0] != 0xE8:
        return None, None
    wrapper_rva = off2rva(wrapper_fo)
    if wrapper_rva is None:
        return None, None
    disp = struct.unpack('<i', call[1:5])[0]
    inner_rva = wrapper_rva + 0x68 + 5 + disp

    _file_cache[dll_path] = (mtime, wrapper_rva, inner_rva)
    return wrapper_rva, inner_rva


def _resolve_inner_func(hProcess):
    """Locate wrapper via PATTERN_V1, resolve inner_func from the call at +0x68.

    Returns None on success (addresses stored in _state), error string otherwise.
    """
    mod = _find_weixin_dll(hProcess)
    if not mod:
        return _err("Weixin.dll not found")

    base = mod['base'] or 0
    if isinstance(base, ctypes.c_void_p):
        base = base.value
    size = mod['size']

    # --- Fast path: resolve RVAs from the DLL file on disk, verify remotely ---
    dll_path = mod.get('path')
    if dll_path:
        wrapper_rva, inner_rva = _resolve_rvas_from_file(dll_path)
        if wrapper_rva is not None:
            wrapper_addr = base + wrapper_rva
            inner_func_addr = base + inner_rva
            # Verify remote bytes: pattern at wrapper+3, prologue at inner_func
            verify = _read_remote(hProcess, wrapper_addr + 3, len(PATTERN_V1))
            prologue = _read_remote(hProcess, inner_func_addr, 12)
            if (verify == PATTERN_V1 and prologue and len(prologue) == 12):
                _state['wrapper_addr'] = wrapper_addr
                _state['inner_func_addr'] = inner_func_addr
                _state['hook_addr'] = inner_func_addr
                _state['hook_offset'] = 'inner_func+0x00 (fast-path)'
                return None
            # fall through to full scan on mismatch

    # --- Slow path: full remote memory scan ---
    results = _find_pattern_in_remote(hProcess, base, size, PATTERN_V1)
    if not results:
        return _err("Pattern not found in Weixin.dll — unsupported version")

    wrapper_addr = results[0] - 3

    # The call to inner_func is at wrapper+0x68 (E8 rel32)
    call_bytes = _read_remote(hProcess, wrapper_addr + 0x68, 5)
    if not (call_bytes and len(call_bytes) == 5 and call_bytes[0] == 0xE8):
        return _err("Unexpected instruction at wrapper+0x68 — unsupported version")

    disp = struct.unpack('<i', call_bytes[1:5])[0]
    inner_func_addr = wrapper_addr + 0x68 + 5 + disp

    _state['wrapper_addr'] = wrapper_addr
    _state['inner_func_addr'] = inner_func_addr
    _state['hook_addr'] = inner_func_addr   # hook at ENTRY
    _state['hook_offset'] = 'inner_func+0x00'
    return None


def _extract_xor_mask(hProcess):
    """Extract the 32-byte XOR mask from inner_func's deobfuscation loop.

    Scans the first _MASK_SCAN_SIZE bytes of inner_func for
    `lea r8, [rip+disp32]` (4C 8D 05) — the byte-loop mask table pointer —
    then reads 32 bytes from that address in the remote process.

    Returns the 32-byte mask, or None if not found (graceful degradation:
    future versions without obfuscation still work via raw capture).
    """
    inner = _state['inner_func_addr']
    code = _read_remote(hProcess, inner, _MASK_SCAN_SIZE)
    if not code:
        return None

    needle = b'\x4c\x8d\x05'  # lea r8, [rip+disp32]
    pos = 0
    while True:
        i = code.find(needle, pos)
        if i < 0 or i + 7 > len(code):
            break
        disp = struct.unpack('<i', code[i + 3:i + 7])[0]
        mask_addr = inner + i + 7 + disp
        mask = _read_remote(hProcess, mask_addr, 32)
        if mask and len(mask) == 32 and any(b != 0 for b in mask):
            _state['xor_mask'] = mask
            return mask
        pos = i + 1
    return None


def _build_shellcode(shared_mem_addr, trampoline_bytes, return_addr):
    """Generate entry-hook shellcode for inner_func.

    At inner_func entry (verified against WeChat 4.1.11.54 disassembly):
      rcx = ctx, rdx = std::string* with the obfuscated key
      ([rdx+0x10]=size, [rdx+0x18]=capacity, data=[rdx] if cap>=16 else rdx)
      r8d = other param (seen: 4096), r9d = flags

    Copies the string content (16..64 bytes, SSO or heap) into the shared
    buffer. The source range is owned by the string object, so it is
    known-readable.
    """
    ks = Ks(KS_ARCH_X86, KS_MODE_64)

    asm = f"""
    mov rax, {shared_mem_addr:#x}
    mov dword ptr [rax + 4108], 0xDD00DD00
    /* diagnostics: record every call regardless of size */
    mov dword ptr [rax + 4116], r8d
    mov dword ptr [rax + 4120], r9d
    mov r11d, dword ptr [rax + 4112]
    inc r11d
    mov dword ptr [rax + 4112], r11d
    /* rdx is a custom buffer object (verified via size/data getter disasm):
       [rdx+0x10] = size, [rdx+0x08] = data pointer (never SSO) */
    mov r10, qword ptr [rdx + 0x10]
    mov dword ptr [rax + 4124], r10d
    cmp r10, 16
    jb skip_capture
    cmp r10, 8192
    ja skip_capture
    mov r11, qword ptr [rdx + 8]
    test r11, r11
    jz skip_capture
    pushfq
    push rax
    push rcx
    push rdx
    push rbx
    push rbp
    push rsi
    push rdi
    push r8
    push r9
    push r10
    push r11
    push r12
    push r13
    push r14
    push r15
    /* all regs saved -- rdi is safe to clobber now */
    mov rdi, {shared_mem_addr:#x}
    mov dword ptr [rdi + 4104], r10d
    mov eax, r10d
    cmp eax, {MAX_KEY_COPY}
    jbe len_ok
    mov eax, {MAX_KEY_COPY}
len_ok:
    mov dword ptr [rdi], eax
    /* data pointer was validated and saved in r11 */
    mov rsi, r11
    lea rcx, [rdi + 4]
    mov rdx, rdi
    mov rdi, rcx
    mov ecx, eax
    rep movsb
    mov rdi, rdx
    mov eax, dword ptr [rdi + 4100]
    inc eax
    mov dword ptr [rdi + 4100], eax
    pop r15
    pop r14
    pop r13
    pop r12
    pop r11
    pop r10
    pop r9
    pop r8
    pop rdi
    pop rsi
    pop rbp
    pop rbx
    pop rdx
    pop rcx
    pop rax
    popfq
skip_capture:
    .byte {', '.join(f'0x{b:02x}' for b in trampoline_bytes)}
    mov rax, {return_addr:#x}
    jmp rax
    """

    encoding, count = ks.asm(asm)
    if not encoding:
        return None
    return bytes(encoding)


def _deobfuscate(raw, size):
    """Apply the 32-byte repeating XOR mask. Returns hex string or None."""
    mask = _state.get('xor_mask')
    if not mask:
        return None
    n = min(size, len(raw))
    out = bytes(raw[i] ^ mask[i & 31] for i in range(n))
    return out.hex()


def suspend_process(pid):
    """Suspend all threads of a process (NtSuspendProcess). Returns hProcess or None.

    Caller must later call resume_process(hProcess). Used to freeze a freshly
    created WeChat process BEFORE it can open any database, eliminating the
    startup race for the key-setup hook.
    """
    ntdll = ctypes.windll.ntdll
    h = kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
        | PROCESS_QUERY_INFORMATION | 0x0800,  # PROCESS_SUSPEND_RESUME
        False, pid)
    if not h:
        return None
    # NtSuspendProcess(ProcessHandle, &PreviousState)
    prev = ctypes.c_void_p()
    status = ntdll.NtSuspendProcess(ctypes.c_void_p(h), ctypes.byref(prev))
    if status != 0:
        kernel32.CloseHandle(h)
        return None
    return h


def resume_process(hProcess):
    """Resume a process suspended with suspend_process and close the handle."""
    ntdll = ctypes.windll.ntdll
    prev = ctypes.c_void_p()
    ntdll.NtResumeProcess(ctypes.c_void_p(hProcess), ctypes.byref(prev))
    kernel32.CloseHandle(hProcess)


def initialize_hook(pid=None, timeout=30):
    """Install entry hook on inner_func inside the WeChat process.

    Args:
        pid: WeChat PID or None to auto-detect
    Returns:
        True if successful
    """
    with _state['lock']:
        if _state['running']:
            _err("Hook already initialized")
            return False

        if pid is None:
            pid, mem_mb = _find_weixin_pid()
            if not pid:
                return _err("Weixin.exe not running")

        _state['pid'] = pid

        access = PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION
        hProcess = kernel32.OpenProcess(access, False, pid)
        if not hProcess:
            return _err(f"OpenProcess failed (PID={pid}). Run as Administrator.")
        _state['hProcess'] = hProcess

        err = _resolve_inner_func(hProcess)
        if err:
            return False
        hook_target = _state['hook_addr']

        # Extract XOR mask BEFORE hooking (best effort — may be absent)
        _extract_xor_mask(hProcess)

        return _install_hook(hook_target)


def _install_hook(hook_target):
    """Low-level: patch a 12-byte absolute JMP at hook_target and start polling.

    Assumes _state['hProcess'] is an open handle with VM_READ/WRITE/OPERATION
    and _state['xor_mask'] is set (or None for raw-only capture).
    Exposed separately so it can be tested against a synthetic target.
    """
    with _state['lock']:
        hProcess = _state['hProcess']

        # Read original bytes at hook target (12 bytes for absolute JMP)
        original_bytes = _read_remote(hProcess, hook_target, 12)
        if not original_bytes or len(original_bytes) < 12:
            return _err("Cannot read trampoline bytes at inner_func entry")
        _state['original_bytes'] = original_bytes
        _state['hook_addr'] = hook_target

        jmp_size = 12

        # Allocate remote memory for shellcode + shared data
        remote_addr = kernel32.VirtualAllocEx(
            hProcess, None, REMOTE_ALLOC_SIZE,
            MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE
        )
        if not remote_addr:
            return _err("VirtualAllocEx failed")

        remote_addr = remote_addr.value if hasattr(remote_addr, 'value') else int(remote_addr)
        _state['remote_shellcode'] = remote_addr
        shared_data_addr = remote_addr + REMOTE_ALLOC_SIZE - SHARED_DATA_SIZE - 64
        _state['remote_shared_data'] = shared_data_addr

        return_addr = hook_target + jmp_size
        shellcode = _build_shellcode(shared_data_addr, original_bytes, return_addr)
        if not shellcode:
            return _err("Shellcode generation failed")

        mask = _state.get('xor_mask')
        mask_info = f"mask={mask.hex()}" if mask else "mask=NONE(raw-only)"
        wrapper = _state.get('wrapper_addr') or 0
        _state['last_error'] = (
            f"hook=0x{hook_target:x} wrapper=0x{wrapper:x} "
            f"remote=0x{remote_addr:x} shared=0x{shared_data_addr:x} "
            f"return=0x{return_addr:x} trampoline={original_bytes.hex()} {mask_info}"
        )

        if not _write_remote(hProcess, remote_addr, shellcode):
            return _err("WriteProcessMemory (shellcode) failed")

        zero_data = b'\x00' * SHARED_DATA_SIZE
        if not _write_remote(hProcess, shared_data_addr, zero_data):
            return _err("WriteProcessMemory (shared_data) failed")

        # mov rax, <remote_addr>; jmp rax
        jmp_code = b'\x48\xB8' + struct.pack('<Q', remote_addr) + b'\xFF\xE0'
        if not _write_remote(hProcess, hook_target, jmp_code):
            return _err("WriteProcessMemory (jmp) failed")

        verify_bytes = _read_remote(hProcess, hook_target, 12)
        if verify_bytes and verify_bytes != jmp_code:
            _state['last_error'] += f" JMP_VERIFY_MISMATCH: wrote={jmp_code.hex()} read={verify_bytes.hex()}"
        elif verify_bytes:
            _state['last_error'] += " JMP_OK"

        _state['running'] = True
        _state['last_sequence'] = 0
        _state['pending_keys'] = []
        _state['poll_thread'] = threading.Thread(target=_poll_loop, daemon=True)
        _state['poll_thread'].start()

        return True


def _poll_loop():
    """Background thread that polls shared memory for new key data."""
    hProcess = _state['hProcess']
    shared_addr = _state['remote_shared_data']

    while _state['running']:
        try:
            data = _read_remote(hProcess, shared_addr, SHARED_DATA_SIZE)
            if data and len(data) >= SHARED_DATA_SIZE:
                data_size = struct.unpack_from('<I', data, 0)[0]
                seq = struct.unpack_from('<I', data, 4100)[0]
                key_len = struct.unpack_from('<I', data, 4104)[0]

                with _state['lock']:
                    _state['call_count'] = struct.unpack_from('<I', data, 4112)[0]
                    _state['last_r8'] = struct.unpack_from('<I', data, 4116)[0]
                    _state['last_r9'] = struct.unpack_from('<I', data, 4120)[0]
                    _state['last_str_size'] = struct.unpack_from('<I', data, 4124)[0]

                if struct.unpack_from('<I', data, 4108)[0] == 0xDD00DD00:
                    with _state['lock']:
                        _state['heartbeat_seen'] = True

                if 16 <= data_size <= MAX_KEY_COPY and seq != _state['last_sequence'] and seq != 0:
                    _state['last_sequence'] = seq
                    raw = data[4:4 + data_size]
                    entries = []
                    deob = _deobfuscate(raw, data_size)
                    if deob:
                        entries.append({'key': deob, 'source': 'v3-deob',
                                        'key_len': key_len})
                        # If the deobfuscated form is 64 ASCII hex chars,
                        # the key travels as a hex string — also emit decoded
                        deob_bytes = bytes.fromhex(deob)
                        if len(deob_bytes) == 64:
                            try:
                                decoded = bytes.fromhex(deob_bytes.decode('ascii'))
                                entries.append({'key': decoded.hex(),
                                                'source': 'v3-deob-hexstr',
                                                'key_len': key_len})
                            except (ValueError, UnicodeDecodeError):
                                pass
                    entries.append({'key': raw.hex(), 'source': 'v3-raw',
                                    'key_len': key_len})
                    with _state['lock']:
                        _state['pending_keys'].extend(entries)
        except Exception:
            pass
        time.sleep(0.08)


def poll_key_data():
    """Check for newly captured keys. Returns {'key': hex_str, ...} or None."""
    with _state['lock']:
        if _state['pending_keys']:
            return _state['pending_keys'].pop(0)
    return None


def get_status_message():
    """Return (message, level) or (None, -1). Compatible with wx_key API."""
    return None, -1


def cleanup_hook():
    """Remove hook and release resources."""
    with _state['lock']:
        _state['running'] = False

        if _state['poll_thread']:
            _state['poll_thread'].join(timeout=2)
            _state['poll_thread'] = None

        hProcess = _state['hProcess']
        if hProcess:
            # Restore original bytes at hook target FIRST — otherwise the
            # dangling JMP points into freed memory and crashes the target
            # process on the next call.
            orig = _state.get('original_bytes')
            if orig and _state.get('hook_addr'):
                _write_remote(hProcess, _state['hook_addr'], orig)
                _state['original_bytes'] = None

            if _state['remote_shellcode']:
                kernel32.VirtualFreeEx(hProcess, _state['remote_shellcode'],
                                       0, 0x8000)  # MEM_RELEASE
                _state['remote_shellcode'] = None

            kernel32.CloseHandle(hProcess)
            _state['hProcess'] = None

        _state['pending_keys'] = []
        _state['last_sequence'] = 0

    return True


def heartbeat_seen():
    """Return True if shellcode heartbeat was detected (JMP is working)."""
    return _state.get('heartbeat_seen', False)


def get_call_diag():
    """Return (call_count, last_r8, last_r9, last_str_size) diagnostics."""
    with _state['lock']:
        return (_state.get('call_count', 0),
                _state.get('last_r8', 0),
                _state.get('last_r9', 0),
                _state.get('last_str_size', 0))


def get_last_error_msg():
    """Return last error / diagnostic message."""
    return _state['last_error']


def is_initialized():
    """Check if hook is currently installed."""
    return _state['running']
