# -*- coding: utf-8 -*-
"""issue #27-B(e)：分片并行扫描的结果必须与串行**完全一致**。"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import chat  # noqa: E402


def _make_shards(tmp_path, n=3):
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    (dec / 'contact').mkdir()
    (dec / 'contact' / 'contact.db').write_bytes(b'')
    for i in range(n):
        conn = sqlite3.connect(str(dec / 'message' / ('message_%d.db' % i)))
        # 每个分片两个会话表；last_time 递增，便于验证"取更大者"的合并规则
        for j in range(2):
            conn.execute('CREATE TABLE Msg_%02d%02d (local_id INTEGER, create_time INTEGER, '
                         'local_type INTEGER, message_content BLOB)' % (i, j))
            conn.execute('INSERT INTO Msg_%02d%02d VALUES (1, ?, 1, x\'00\')' % (i, j),
                         (100 + i * 10 + j,))
        conn.commit()
        conn.close()
    return dec


def test_parallel_matches_serial(tmp_path, monkeypatch):
    dec = _make_shards(tmp_path, 3)
    monkeypatch.setattr(chat, '_load_from_chats_db', lambda *a, **k: None)

    monkeypatch.setattr(chat, '_SHARD_WORKERS', 1, raising=False)
    serial = chat.get_contacts(str(dec), None)
    # 清缓存后再用并行跑
    cache = dec / 'data' / 'contacts_cache.json'
    if cache.is_file():
        cache.unlink()
    monkeypatch.setattr(chat, '_SHARD_WORKERS', 8, raising=False)
    parallel = chat.get_contacts(str(dec), None)

    key = lambda r: sorted((c['id'], c['msg_count'], c['last_msg_time']) for c in r)
    assert key(serial) == key(parallel), '并行结果与串行不一致'


def test_parallel_dedupes_across_shards_keeping_latest(tmp_path, monkeypatch):
    """同一会话出现在两个分片时，取 last_msg_time 更大的那条（与串行规则一致）。"""
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    (dec / 'contact').mkdir()
    (dec / 'contact' / 'contact.db').write_bytes(b'')
    for i, ts in ((0, 100), (1, 200)):
        conn = sqlite3.connect(str(dec / 'message' / ('message_%d.db' % i)))
        conn.execute('CREATE TABLE Msg_same (local_id INTEGER, create_time INTEGER, '
                     'local_type INTEGER, message_content BLOB)')
        conn.execute('INSERT INTO Msg_same VALUES (1, ?, 1, x\'00\')', (ts,))
        conn.commit()
        conn.close()
    monkeypatch.setattr(chat, '_load_from_chats_db', lambda *a, **k: None)
    res = chat.get_contacts(str(dec), None)
    same = [c for c in res if c['last_msg_time'] in (100, 200)]
    assert len(same) == 1, '同一会话应只保留一条'
    assert same[0]['last_msg_time'] == 200
