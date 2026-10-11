# -*- coding: utf-8 -*-
"""issue #27：chats.db 索引缺失时应当**按需补建**，而不是每次首屏全库扫描。

真机实测（2,539 个会话）：/api/contacts 走慢路径需 218 秒；补建索引后第二次请求应回到秒级。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import chat  # noqa: E402


def test_ensure_index_builds_when_missing(tmp_path, monkeypatch):
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    calls = []

    def fake_build(decrypted_dir, output_db, **kw):
        calls.append(output_db)
        os.makedirs(os.path.dirname(output_db), exist_ok=True)
        open(output_db, 'wb').write(b'SQLite')
        return output_db

    monkeypatch.setattr(chat, '_build_index_fn', lambda: fake_build, raising=False)
    assert chat._ensure_chat_index(str(dec)) is True
    assert calls and calls[0].endswith(os.path.join('data', 'chats.db'))


def test_ensure_index_skips_when_present(tmp_path, monkeypatch):
    dec = tmp_path / 'decrypted'
    (dec / 'data').mkdir(parents=True)
    (dec / 'data' / 'chats.db').write_bytes(b'SQLite')
    called = []

    def fake_build(*a, **kw):
        called.append(1)

    monkeypatch.setattr(chat, '_build_index_fn', lambda: fake_build, raising=False)
    assert chat._ensure_chat_index(str(dec)) is True
    assert not called, '已有索引不应重建'


def test_ensure_index_failure_returns_false(tmp_path, monkeypatch):
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)

    def boom(*a, **kw):
        raise RuntimeError('build failed')

    monkeypatch.setattr(chat, '_build_index_fn', lambda: boom, raising=False)
    assert chat._ensure_chat_index(str(dec)) is False, '构建失败要返回 False，让调用方走慢路径'
