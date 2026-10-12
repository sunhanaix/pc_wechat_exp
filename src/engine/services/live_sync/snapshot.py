"""Validate encrypted pages and replay only committed WAL frames."""
import hashlib
import hmac
import sqlite3
import struct
import time
from pathlib import Path

from engine.decrypt import decrypt_page, derive_mac_key
from .locking import source_guard, SourceBusy

PAGE_SIZE = 4096


def checksum(data, endian, state=(0, 0)):
    words = struct.unpack(endian + str(len(data) // 4) + 'I', data)
    a, b = state
    for i in range(0, len(words), 2):
        a = (a + words[i] + b) & 0xffffffff
        b = (b + words[i + 1] + a) & 0xffffffff
    return a, b


def committed_frames(raw):
    if not raw:
        return [], {'frames': 0, 'committed': 0, 'pages': 0, 'generation': None}
    if len(raw) < 32:
        raise SourceBusy('Incomplete WAL header')
    magic, version, size, seq, salt1, salt2, c1, c2 = struct.unpack('>8I', raw[:32])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != PAGE_SIZE:
        raise ValueError('Unsupported WAL format/page size')
    endian = '<' if magic == 0x377f0682 else '>'
    state = checksum(raw[:24], endian)
    if state != (c1, c2):
        raise ValueError('WAL header checksum mismatch')
    frames = []
    last = pages = 0
    for offset in range(32, len(raw) - PAGE_SIZE - 23, PAGE_SIZE + 24):
        header = raw[offset:offset + 24]
        data = raw[offset + 24:offset + 24 + PAGE_SIZE]
        pgno, npage, s1, s2, fc1, fc2 = struct.unpack('>6I', header)
        if not pgno or (s1, s2) != (salt1, salt2):
            break  # Unused capacity or stale frames from an earlier WAL generation.
        state = checksum(header[:8] + data, endian, state)
        if state != (fc1, fc2):
            # With a locked source, a checksum mismatch is not an ordinary
            # concurrent partial write. Fail closed rather than advancing state.
            raise ValueError('WAL frame checksum mismatch at frame %d' % (len(frames) + 1))
        frames.append((pgno, data))
        if npage:
            last, pages = len(frames), npage
    return frames[:last], {'frames': len(frames), 'committed': last, 'pages': pages,
                          'generation': [seq, salt1, salt2]}


def source_token(path):
    """Cheap commit probe, including mmap changes and same-size WAL reuse."""
    path = Path(path)
    parts = []
    for suffix in ('', '-wal'):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            stat = candidate.stat()
            with candidate.open('rb') as stream:
                header = stream.read(32)
            parts.append((stat.st_size, stat.st_mtime_ns, header))
        else:
            parts.append(None)
    shm = Path(str(path) + '-shm')
    if shm.exists():
        with shm.open('rb') as stream:
            parts.append(stream.read(96))  # Both WalIndexHdr copies, before lock bytes.
    elif parts[1]:
        # Closed/recovering WAL, no mxFrame available; do not rely on its size.
        parts.append(hashlib.sha256(Path(str(path) + '-wal').read_bytes()).digest())
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _decrypt(raw, key, mac_key, pgno):
    if len(raw) != PAGE_SIZE:
        raise ValueError('Incomplete encrypted page %d' % pgno)
    data = raw[16 if pgno == 1 else 0:4032]
    digest = hmac.new(mac_key, data + struct.pack('<I', pgno), hashlib.sha512).digest()
    if not hmac.compare_digest(digest, raw[4032:]):
        raise ValueError('Encrypted page HMAC mismatch at page %d' % pgno)
    return decrypt_page(key, raw, pgno)


def encrypted_snapshot(path, key, output):
    """Copy under source VFS locks, release locks before decrypting/indexing."""
    path = Path(path)
    started = time.monotonic()
    with source_guard(path):
        before = source_token(path)
        main = path.read_bytes()
        wal_path = Path(str(path) + '-wal')
        wal = wal_path.read_bytes() if wal_path.exists() else b''
        if source_token(path) != before:
            raise SourceBusy('Source generation changed while copying')
    held = time.monotonic() - started
    if len(main) < PAGE_SIZE or len(main) % PAGE_SIZE:
        raise ValueError('Incomplete source database')
    mac_key = derive_mac_key(key, main[:16])
    frames, info = committed_frames(wal)
    # Only decrypt the newest committed version of a page. Unused pages past
    # a committed truncation are discarded before decoding.
    overlays = dict(frames)
    npage = info['pages'] if frames else len(main) // PAGE_SIZE
    with Path(output).open('wb') as stream:
        for pgno in range(1, npage + 1):
            raw = overlays.get(pgno, main[(pgno - 1) * PAGE_SIZE:pgno * PAGE_SIZE])
            plain = bytearray(_decrypt(raw, key, mac_key, pgno))
            if pgno == 1:
                plain[18:20] = b'\x01\x01'  # Standalone output, no source WAL attached.
            stream.write(plain)
    conn = sqlite3.connect(Path(output).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Decrypted snapshot integrity check failed')
    finally:
        conn.close()
    return dict(info, token=before, source_lock_seconds=round(held, 4))
