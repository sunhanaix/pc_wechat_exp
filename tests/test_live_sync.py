"""Native SQLite fixtures encrypted in the project's SQLCipher page format."""
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from Crypto.Cipher import AES
from engine.decrypt import derive_mac_key
from engine.services.live_sync import MessageSyncService
from engine.services.live_sync.snapshot import committed_frames, checksum, encrypted_snapshot, source_token
from engine.services.live_sync.locking import byte_lock, SourceBusy

KEY = bytes(range(32))
SALT = bytes(range(16))
CHAT = 'wxid_fixture'
TABLE = 'Msg_' + hashlib.md5(CHAT.encode()).hexdigest()


def encrypt_page(plain, pgno):
    iv = os.urandom(16)
    start = 16 if pgno == 1 else 0
    data = AES.new(KEY, AES.MODE_CBC, iv).encrypt(plain[start:4016])
    mac = hmac.new(derive_mac_key(KEY, SALT), data + iv + struct.pack('<I', pgno), hashlib.sha512).digest()
    return (SALT if pgno == 1 else b'') + data + iv + mac


def encrypt_wal(raw, committed_bytes=None):
    header = raw[:32]
    endian = '<' if struct.unpack('>I', header[:4])[0] == 0x377f0682 else '>'
    state = checksum(header[:24], endian)
    output = bytearray(header[:24] + struct.pack('>2I', *state))
    for offset in range(32, len(raw), 4120):
        fh = bytearray(raw[offset:offset + 24])
        pgno = struct.unpack('>I', fh[:4])[0]
        if committed_bytes is not None and offset >= committed_bytes:
            fh[4:8] = bytes(4)  # A checksum-valid transaction without a commit frame.
        page = encrypt_page(raw[offset + 24:offset + 4120], pgno)
        state = checksum(fh[:8] + page, endian, state)
        output.extend(fh[:16] + struct.pack('>2I', *state) + page)
    return output


def make_source(tmp_path, uncommitted=False, shard='message_0.db'):
    directory = tmp_path / ('fixture_' + shard)
    directory.mkdir()
    plain = directory / 'plain.db'
    with closing(sqlite3.connect(plain)) as conn:
        conn.execute('PRAGMA page_size=4096')
        conn.execute('PRAGMA user_version=1')
        conn.commit()
    empty = bytearray(plain.read_bytes())
    empty[20] = 80
    empty[105:107] = struct.pack('>H', 4016)  # Empty page's usable cell area.
    plain.write_bytes(empty)
    conn = sqlite3.connect(plain)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA wal_autocheckpoint=0')
    conn.execute('CREATE TABLE Name2Id(user_name TEXT)')
    conn.execute('INSERT INTO Name2Id VALUES (?)', (CHAT,))
    conn.execute('CREATE TABLE [%s](local_id INTEGER PRIMARY KEY, local_type INTEGER, '
                 'origin_source INTEGER, create_time INTEGER,status INTEGER,message_content TEXT,'
                 'real_sender_id INTEGER,packed_info_data BLOB)' % TABLE)
    conn.execute('INSERT INTO [%s] VALUES (1,1,0,1791798000,3,"before",1,NULL)' % TABLE)
    conn.commit()
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    main = plain.read_bytes()
    conn.execute('INSERT INTO [%s] VALUES (2,1,0,1791798000,3,"committed",1,NULL)' % TABLE)
    conn.commit()
    wal = Path(str(plain) + '-wal').read_bytes()
    committed_bytes = len(wal)
    if uncommitted:
        conn.execute('INSERT INTO [%s] VALUES (3,1,0,1791798001,3,"uncommitted",1,NULL)' % TABLE)
        conn.commit()
        wal = Path(str(plain) + '-wal').read_bytes()
    conn.close()
    source = directory / 'source' / 'message' / shard
    source.parent.mkdir(parents=True)
    source.write_bytes(b''.join(encrypt_page(main[i:i + 4096], i // 4096 + 1)
                                for i in range(0, len(main), 4096)))
    Path(str(source) + '-wal').write_bytes(encrypt_wal(wal, committed_bytes if uncommitted else None))
    Path(str(source) + '-shm').write_bytes(bytes(32768))
    return source


def keys():
    return {'message_0.db': KEY.hex(), 'message_1.db': KEY.hex()}, {SALT.hex(): KEY.hex()}


def read_messages(path):
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return conn.execute('SELECT local_id,message_content FROM [%s] ORDER BY local_id' % TABLE).fetchall()


def test_decrypts_committed_frames_ignores_uncommitted_tail(tmp_path):
    source = make_source(tmp_path, uncommitted=True)
    dest = tmp_path / 'decoded.db'
    info = encrypted_snapshot(source, KEY, dest)
    assert info['frames'] > info['committed'] > 0
    assert read_messages(dest) == [(1, 'before'), (2, 'committed')]


def test_corrupt_wal_fails_without_publishing(tmp_path):
    source = make_source(tmp_path)
    wal = Path(str(source) + '-wal')
    data = bytearray(wal.read_bytes()); data[-100] ^= 1; wal.write_bytes(data)
    service = MessageSyncService(source.parents[1], tmp_path / 'live', keys_provider=keys)
    state = service.run_once()
    assert not state['ready']
    assert 'checksum' in state['last_error']
    assert not (service.output / 'message' / source.name).exists()


def test_wrong_page_key_rejected(tmp_path):
    source = make_source(tmp_path)
    with pytest.raises(ValueError, match='HMAC'):
        encrypted_snapshot(source, bytes(32), tmp_path / 'bad.db')


def test_same_size_wal_content_and_shm_probe_change(tmp_path):
    source = make_source(tmp_path)
    before = source_token(source)
    shm = Path(str(source) + '-shm')
    stamp = shm.stat()
    data = bytearray(shm.read_bytes()); data[16] = 1; shm.write_bytes(data)
    os.utime(shm, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert source_token(source) != before


def test_checkpoint_restart_dedup_and_cross_shard_identity(tmp_path):
    source = make_source(tmp_path)
    root = source.parents[1]
    output = tmp_path / 'live'
    service = MessageSyncService(root, output, keys_provider=keys)
    assert service.run_once()['ready']
    assert read_messages(output / 'message' / source.name) == [(1, 'before'), (2, 'committed')]
    assert not service.status()['running']
    # Checkpoint: encrypted main acquires the same messages and WAL resets.
    decoded = tmp_path / 'checkpoint.db'
    encrypted_snapshot(source, KEY, decoded)
    raw = decoded.read_bytes()
    source.write_bytes(b''.join(encrypt_page(raw[i:i + 4096], i // 4096 + 1) for i in range(0, len(raw), 4096)))
    Path(str(source) + '-wal').write_bytes(b'')
    restarted = MessageSyncService(root, output, keys_provider=keys)
    q = restarted.subscribe()
    assert restarted.run_once()['ready']
    assert restarted.status()['new_messages'] == 0
    events = []
    while not q.empty(): events.append(q.get_nowait())
    assert all(e['type'] == 'sync.ready' for e in events)
    # Same chat/local_id/timestamp in a second shard must remain distinct.
    other = make_source(tmp_path, shard='message_1.db')
    for suffix in ('', '-wal', '-shm'):
        (root / 'message' / ('message_1.db' + suffix)).write_bytes(Path(str(other) + suffix).read_bytes())
    restarted = MessageSyncService(root, output, keys_provider=keys)
    assert restarted.run_once()['ready']
    with closing(sqlite3.connect(output / 'data' / 'chats.db')) as conn:
        assert conn.execute('SELECT message_count FROM chats').fetchone()[0] == 4
        assert conn.execute('SELECT COUNT(*) FROM sync_messages').fetchone()[0] == 4


def test_failed_key_is_retried_without_recording_progress(tmp_path):
    source = make_source(tmp_path)
    supplied = [{}, {}]
    service = MessageSyncService(source.parents[1], tmp_path / 'live', interval=.2,
                                 keys_provider=lambda: supplied)
    service.start()
    try:
        deadline = time.monotonic() + 3
        while not service.status()['last_error'] and time.monotonic() < deadline: time.sleep(.02)
        assert not service.status()['ready']
        supplied[:] = keys()
        while not service.status()['ready'] and time.monotonic() < deadline: time.sleep(.02)
        assert service.status()['ready']
        assert service.status()['last_error'] is None
    finally:
        assert not service.stop()['running']


def test_existing_backup_and_source_overlap_rejected(tmp_path):
    source = make_source(tmp_path)
    existing = tmp_path / 'backup'; existing.mkdir(); (existing / 'keep.txt').write_text('keep')
    with pytest.raises(ValueError, match='覆盖'):
        MessageSyncService(source.parents[1], existing, keys_provider=keys).run_once()
    assert (existing / 'keep.txt').read_text() == 'keep'
    with pytest.raises(ValueError, match='互相包含'):
        MessageSyncService(source.parents[1], source.parents[1] / 'live')


def test_windows_byte_lock_conflicts_with_writer(tmp_path):
    if os.name != 'nt':
        pytest.skip('POSIX locks are per-process; Windows handles test SQLite VFS conflict directly')
    path = tmp_path / 'shm'; path.write_bytes(bytes(32768))
    with byte_lock(path, 120, 3):
        with pytest.raises(SourceBusy):
            with byte_lock(path, 120, 1, exclusive=True): pass


def test_native_sqlite_writer_blocks_snapshot_locks(tmp_path):
    from engine.services.live_sync.locking import source_guard
    path = tmp_path / 'native.db'
    code = ('import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); '
            'c.execute("PRAGMA journal_mode=WAL"); c.execute("CREATE TABLE IF NOT EXISTS t(v)"); '
            'c.commit(); c.execute("BEGIN IMMEDIATE"); print("locked",flush=True); '
            'sys.stdin.readline(); c.rollback(); c.close()')
    child = subprocess.Popen([sys.executable, '-c', code, str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(SourceBusy):
            with source_guard(path): pass
    finally:
        child.communicate('\n', timeout=5)
    assert child.returncode == 0


def test_active_reader_keeps_old_transaction_and_new_message_notifies_once(tmp_path):
    source = make_source(tmp_path, uncommitted=True)
    service = MessageSyncService(source.parents[1], tmp_path / 'live', keys_provider=keys)
    assert service.run_once()['ready']
    target = service.output / 'message' / source.name
    with closing(sqlite3.connect(target)) as reader:
        reader.execute('BEGIN')
        assert reader.execute('SELECT COUNT(*) FROM [%s]' % TABLE).fetchone()[0] == 2
        # Publish the previously uncommitted tail with a valid commit marker.
        wal = Path(str(source) + '-wal')
        raw = bytearray(wal.read_bytes())
        _, info = committed_frames(raw)
        raw[len(raw) - 4120 + 4:len(raw) - 4120 + 8] = struct.pack('>I', info['pages'])
        endian = '<' if struct.unpack('>I', raw[:4])[0] == 0x377f0682 else '>'
        state = checksum(raw[:24], endian)
        for offset in range(32, len(raw), 4120):
            state = checksum(raw[offset:offset + 8] + raw[offset + 24:offset + 4120], endian, state)
            raw[offset + 16:offset + 24] = struct.pack('>2I', *state)
        wal.write_bytes(raw)
        events = service.subscribe()
        assert service.run_once()['new_messages'] == 1
        assert reader.execute('SELECT COUNT(*) FROM [%s]' % TABLE).fetchone()[0] == 2
        reader.commit()
        assert reader.execute('SELECT COUNT(*) FROM [%s]' % TABLE).fetchone()[0] == 3
    notified = []
    while not events.empty(): notified.append(events.get_nowait())
    assert sum(e['newMessages'] for e in notified) == 1
    assert service.run_once()['new_messages'] == 1  # No additional count for unchanged data.
    assert events.empty()
