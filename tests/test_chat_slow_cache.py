# -*- coding: utf-8 -*-
"""issue #27-B(d)：慢路径结果缓存 —— 指纹不变时不再全库扫描（实测一次约 175 秒）。"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import chat  # noqa: E402

FAKE = [{'id': 'wxid_aaa', 'name': '甲', 'msg_count': 1, 'last_msg_time': 5,
         'type': 'friend', 'avatar_url': ''}]


def _make_dir(tmp_path):
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    db = dec / 'message' / 'message_0.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE Msg_deadbeef (local_id INTEGER, create_time INTEGER, '
                 'local_type INTEGER, message_content BLOB)')
    conn.execute('INSERT INTO Msg_deadbeef VALUES (1, 100, 1, x\'00\')')
    conn.commit()
    conn.close()
    (dec / 'contact').mkdir()
    (dec / 'contact' / 'contact.db').write_bytes(b'')
    return dec


def test_cache_write_then_hit(tmp_path, monkeypatch):
    dec = _make_dir(tmp_path)
    calls = {'n': 0}
    monkeypatch.setattr(chat, '_load_from_chats_db', lambda *a, **k: None)
    real_find = chat._find_msg_dbs

    def counting_find(*a, **k):
        calls['n'] += 1
        return real_find(*a, **k)

    monkeypatch.setattr(chat, '_find_msg_dbs', counting_find)
    r1 = chat.get_contacts(str(dec), None)
    assert calls['n'] >= 1, '首次应真的扫描'
    cache = dec / 'data' / 'contacts_cache.json'
    assert cache.is_file(), '扫描后应写入缓存'
    n_after_first = calls['n']
    r2 = chat.get_contacts(str(dec), None)
    assert calls['n'] == n_after_first, '第二次命中缓存，不应再扫描'
    assert [c['id'] for c in r2] == [c['id'] for c in r1]


def test_cache_invalidated_when_source_changes(tmp_path, monkeypatch):
    dec = _make_dir(tmp_path)
    monkeypatch.setattr(chat, '_load_from_chats_db', lambda *a, **k: None)
    chat.get_contacts(str(dec), None)
    calls = {'n': 0}
    real_find = chat._find_msg_dbs
    monkeypatch.setattr(chat, '_find_msg_dbs',
                        lambda *a, **k: (calls.__setitem__('n', calls['n'] + 1),
                                         real_find(*a, **k))[1])
    shard = dec / 'message' / 'message_0.db'
    st = shard.stat()
    os.utime(str(shard), (st.st_atime, st.st_mtime + 10))     # 源文件变了
    chat.get_contacts(str(dec), None)
    assert calls['n'] >= 1, '指纹变化后必须重新扫描'


def test_corrupt_cache_is_ignored(tmp_path, monkeypatch):
    dec = _make_dir(tmp_path)
    (dec / 'data').mkdir()
    (dec / 'data' / 'contacts_cache.json').write_text('{ not json', encoding='utf-8')
    monkeypatch.setattr(chat, '_load_from_chats_db', lambda *a, **k: None)
    res = chat.get_contacts(str(dec), None)          # 不应抛异常
    assert isinstance(res, list)


def test_cache_wxid_mismatch_not_used(tmp_path, monkeypatch):
    dec = _make_dir(tmp_path)
    monkeypatch.setattr(chat, '_load_from_chats_db', lambda *a, **k: None)
    chat.get_contacts(str(dec), None)
    fp = chat._slow_cache_fingerprint(str(dec))
    (dec / 'data' / 'contacts_cache.json').write_text(
        json.dumps({'fingerprint': fp, 'wxid': 'someone_else', 'contacts': FAKE}),
        encoding='utf-8')
    res = chat.get_contacts(str(dec), None)
    assert [c['id'] for c in res] != ['wxid_aaa'], 'wxid 不同不得复用缓存'
