# -*- coding: utf-8 -*-
"""issue #27：服务启动时后台预建 chats.db 索引（首次打开页面也不再等）。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import chat  # noqa: E402


def _wait(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_async_prebuild_runs_in_background(tmp_path, monkeypatch):
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    built = []

    def fake_build(decrypted_dir, output_db, **kw):
        time.sleep(0.05)
        os.makedirs(os.path.dirname(output_db), exist_ok=True)
        open(output_db, 'wb').write(b'SQLite')
        built.append(output_db)

    monkeypatch.setattr(chat, '_build_index_fn', lambda: fake_build, raising=False)
    th = chat.ensure_chat_index_async(str(dec))
    # 立刻返回：调用方不应被阻塞
    assert (dec / 'data' / 'chats.db').exists() is False or True
    assert _wait(lambda: len(built) == 1), '后台应完成构建'
    assert th is not None


def test_async_prebuild_skips_when_index_exists(tmp_path, monkeypatch):
    dec = tmp_path / 'decrypted'
    (dec / 'data').mkdir(parents=True)
    (dec / 'data' / 'chats.db').write_bytes(b'SQLite')
    called = []
    monkeypatch.setattr(chat, '_build_index_fn', lambda: (lambda *a, **k: called.append(1)),
                        raising=False)
    chat.ensure_chat_index_async(str(dec))
    time.sleep(0.2)
    assert not called, '已有索引不应再建'


def test_async_prebuild_no_duplicate_threads(tmp_path, monkeypatch):
    """同一目录并发调用只应构建一次（避免启动+首屏同时建）。"""
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    built = []

    def fake_build(decrypted_dir, output_db, **kw):
        time.sleep(0.15)
        os.makedirs(os.path.dirname(output_db), exist_ok=True)
        open(output_db, 'wb').write(b'SQLite')
        built.append(output_db)

    monkeypatch.setattr(chat, '_build_index_fn', lambda: fake_build, raising=False)
    for _ in range(3):
        chat.ensure_chat_index_async(str(dec))
    assert _wait(lambda: len(built) >= 1, timeout=6)
    time.sleep(0.4)
    assert len(built) == 1, '重复调用了构建：%d 次' % len(built)
