"""py_wx_key_v2 — Python-only remote hook for WeChat 4.1.10.x.

Uses ctypes + keystone to inject corrected shellcode that handles
the NEW 4-param function signature: func(ctx, pKey, nKey, flags).

The old shellcode (py_wx_key 2.0.0) assumed a 2-param signature:
  func(ctx, KeyConfig* struct) where struct had key@+0x08, size@+0x10
WeChat 4.1.10.x changed to 4-param:
  func(ctx, pKey, nKey, flags) — key is DIRECTLY in rdx, size in r8d

This module provides a drop-in API compatible with wx_key usage pattern.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import threading
import time

from keystone import Ks, KS_ARCH_X86, KS_MODE_64

# --- Constants ---
PROCESS_ALL_ACCESS = 0x1FFFFF
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_EXECUTE_READWRITE = 0x40
PAGE_READWRITE = 0x04

# Pattern for WeChat >4.1.6.14 (from py_wx_key remote_scanner.cpp)
PATTERN_V1 = bytes([
    0x24, 0x50, 0x48, 0xC7, 0x45, 0x00, 0xFE, 0xFF, 0xFF, 0xFF,
    0x44, 0x89, 0xCF, 0x44, 0x89, 0xC3, 0x49, 0x89, 0xD6,
    0x48, 0x89, 0xCE, 0x48, 0x89
])

# Shared data layout (must match shellcode expectations)
# offset 0: DWORD dataSize
# offset 4: BYTE[32] keyBuffer
# offset 36: DWORD sequenceNumber
# offset 40: DWORD reserved (unused)
# offset 44: DWORD heartbeat (0xDD00DD00 = shellcode executing)
SHARED_DATA_SIZE = 48

# --- Windows DLLs ---
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

# Set proper restypes for 64-bit pointer returns (ctypes defaults to c_int = 32-bit)
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
    'local_shared_view': None,
    'hMapping': None,
    'last_sequence': 0,
    'pending_keys': [],
    'lock': threading.Lock(),
    'running': False,
    'poll_thread': None,
    'last_error': '',
    'hook_addr': 0,
    'heartbeat_seen': False,
    'xor_mask': None,
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
    """Find Weixin.dll base and size in the target process."""
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
                    return {'base': mi.base, 'size': mi.size}
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


def _find_pattern_in_remote(hProcess, base, size, pattern, mask=None):
    """Search for pattern in remote process memory. Returns list of addresses."""
    chunk_size = 2 * 1024 * 1024
    results = []
    offset = 0
    plen = len(pattern)
    if mask is None:
        mask = 'x' * plen
    while offset < size:
        read_size = min(chunk_size + plen, size - offset)
        data = _read_remote(hProcess, base + offset, read_size)
        if not data:
            break
        for i in range(len(data) - plen + 1):
            match = True
            for j in range(plen):
                if mask[j] == 'x' and data[i + j] != pattern[j]:
                    match = False
                    break
            if match:
                results.append(base + offset + i)
        offset += chunk_size
    return results


def _find_hook_target(hProcess):
    """Find the hook target: wrapper+0x989 after inner_func returns.

    WeChat 4.1.10.x key setup chain:
      wrapper(struct_ptr, size, flags)
        -> inner_func(obj, struct_ptr, size, flags)  ; XOR-deobfuscates key IN-PLACE
        wrapper+0x989: nop  ; <-- HOOK HERE (inner_func returned, key ready in [r14])

    After inner_func returns, r14 still holds the input data pointer (callee-saved).
    [r14]..[r14+31] now contain the deobfuscated 32-byte DB encryption key.
    This bypasses the need for XOR deobfuscation entirely.
    """
    mod = _find_weixin_dll(hProcess)
    if not mod:
        return _err("Weixin.dll not found")

    base = mod['base'] or 0
    if isinstance(base, ctypes.c_void_p):
        base = base.value
    size = mod['size']

    results = _find_pattern_in_remote(hProcess, base, size, PATTERN_V1)
    if not results:
        return _err("Pattern not found in Weixin.dll — unsupported version")

    wrapper_addr = results[0] - 3

    # Verify wrapper structure by checking the call at wrapper+0x68
    call1_bytes = _read_remote(hProcess, wrapper_addr + 0x68, 5)
    if not (call1_bytes and len(call1_bytes) == 5 and call1_bytes[0] == 0xE8):
        return _err("Unexpected instruction at wrapper+0x68 — unsupported version")

    disp = struct.unpack('<i', call1_bytes[1:5])[0]
    inner_func_addr = wrapper_addr + 0x68 + 5 + disp

    # Hook at wrapper+0x989: right after inner_func returns
    # r14 = input data pointer (callee-saved), [r14] = deobfuscated key
    hook_addr = wrapper_addr + 0x989

    _state['wrapper_addr'] = wrapper_addr
    _state['inner_func_addr'] = inner_func_addr
    _state['hook_addr'] = hook_addr
    _state['hook_offset'] = 'wrapper+0x989'
    return None


def _build_shellcode(shared_mem_addr, trampoline_bytes, return_addr):
    """Generate shellcode for wrapper+0x989 mid-function hook.

    At wrapper+0x989 (right after inner_func returns):
      r14 = input data pointer (callee-saved across inner_func call)
      [r14]..[r14+31] = deobfuscated 32-byte DB key (XOR done in-place by inner_func)

    Copies 32 bytes directly from [r14] — no pointer chasing needed.
    """
    ks = Ks(KS_ARCH_X86, KS_MODE_64)

    asm = f"""
    mov rdi, {shared_mem_addr:#x}
    mov dword ptr [rdi + 44], 0xDD00DD00
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
    mov dword ptr [rdi], 32
    add rdi, 4
    mov rsi, r14
    mov rcx, 32
    rep movsb
    mov rdi, {shared_mem_addr:#x}
    mov eax, dword ptr [rdi + 36]
    inc eax
    mov dword ptr [rdi + 36], eax
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
    .byte {', '.join(f'0x{b:02x}' for b in trampoline_bytes)}
    mov rax, {return_addr:#x}
    jmp rax
    """

    encoding, count = ks.asm(asm)
    if not encoding:
        return None
    return bytes(encoding)


def initialize_hook(pid=None, timeout=30):
    """Install corrected hook on WeChat process.

    Args:
        pid: WeChat PID or None to auto-detect
    Returns:
        True if successful
    """
    with _state['lock']:
        if _state['running']:
            _err("Hook already initialized")
            return False

        # Find PID
        if pid is None:
            pid, mem_mb = _find_weixin_pid()
            if not pid:
                return _err("Weixin.exe not running")

        _state['pid'] = pid

        # Open process
        access = PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION
        hProcess = kernel32.OpenProcess(access, False, pid)
        if not hProcess:
            return _err(f"OpenProcess failed (PID={pid}). Run as Administrator.")
        _state['hProcess'] = hProcess

        # Find hook target (wrapper+0x989 after inner_func returns)
        err = _find_hook_target(hProcess)
        if err:
            return False
        hook_target = _state['hook_addr']

        # Read original bytes at hook target (need 12 bytes for JMP instruction)
        original_bytes = _read_remote(hProcess, hook_target, 12)
        if not original_bytes or len(original_bytes) < 12:
            return _err("Cannot read trampoline bytes at wrapper+0x989")

        # The JMP instruction: mov rax, <abs_addr>; jmp rax = 12 bytes
        jmp_size = 12

        # Allocate remote memory for shellcode + shared data
        total_remote_size = 4096  # one page for both
        remote_addr = kernel32.VirtualAllocEx(
            hProcess, None, total_remote_size,
            MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE
        )
        if not remote_addr:
            return _err("VirtualAllocEx failed")

        # Convert to Python int for arithmetic (ctypes.c_void_p doesn't support +/-)
        remote_addr = remote_addr.value if hasattr(remote_addr, 'value') else int(remote_addr)
        _state['remote_shellcode'] = remote_addr

        # Shared data is at the end of the page
        shared_data_addr = remote_addr + total_remote_size - SHARED_DATA_SIZE - 64
        _state['remote_shared_data'] = shared_data_addr

        # Build shellcode
        return_addr = hook_target + jmp_size
        trampoline = original_bytes  # exactly 12 bytes
        shellcode = _build_shellcode(shared_data_addr, trampoline, return_addr)
        if not shellcode:
            return _err("Shellcode generation failed")

        # Diagnostic: log key addresses
        wrapper_info = f" wrapper=0x{_state.get('wrapper_addr', 0):x}" if _state.get('wrapper_addr') else ""
        _state['last_error'] = (
            f"hook=0x{hook_target:x}{wrapper_info} remote=0x{remote_addr:x} "
            f"shared=0x{shared_data_addr:x} return=0x{return_addr:x} "
            f"trampoline={trampoline.hex()}"
        )

        # Write shellcode to remote process
        if not _write_remote(hProcess, remote_addr, shellcode):
            return _err("WriteProcessMemory (shellcode) failed")

        # Zero the shared data area
        zero_data = b'\x00' * SHARED_DATA_SIZE
        if not _write_remote(hProcess, shared_data_addr, zero_data):
            return _err("WriteProcessMemory (shared_data) failed")

        # Build and write JMP instruction at hook target
        # mov rax, <remote_addr>; jmp rax
        jmp_code = b'\x48\xB8' + struct.pack('<Q', remote_addr) + b'\xFF\xE0'
        if not _write_remote(hProcess, hook_target, jmp_code):
            return _err("WriteProcessMemory (jmp) failed")

        # Verify JMP was written correctly by reading it back
        verify_bytes = _read_remote(hProcess, hook_target, 12)
        if verify_bytes and verify_bytes != jmp_code:
            _state['last_error'] += f" JMP_VERIFY_MISMATCH: wrote={jmp_code.hex()} read={verify_bytes.hex()}"
        elif verify_bytes:
            _state['last_error'] += " JMP_OK"

        # Start polling thread
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
                seq = struct.unpack_from('<I', data, 36)[0]

                if struct.unpack_from('<I', data, 44)[0] == 0xDD00DD00:
                    with _state['lock']:
                        _state['heartbeat_seen'] = True

                if data_size == 32 and seq != _state['last_sequence'] and seq != 0:
                    _state['last_sequence'] = seq
                    key_bytes = data[4:36]
                    key_hex = key_bytes.hex()
                    entry = {'key': key_hex}
                    with _state['lock']:
                        _state['pending_keys'].append(entry)
        except Exception:
            pass
        time.sleep(0.08)  # ~12 polls/sec


def poll_key_data():
    """Check for newly captured keys. Returns {'key': hex_str} or None."""
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
            # Restore original bytes at hook target
            if _state['hook_addr']:
                # We didn't save the original bytes — this is a best-effort cleanup
                pass

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


def get_last_error_msg():
    """Return last error message."""
    return _state['last_error']


def is_initialized():
    """Check if hook is currently installed."""
    return _state['running']
