# -*- coding: utf-8 -*-
"""issue #27：后台预建进行中时，同步 get_contacts 不应再开一次构建（避免两个构建写同一 DB）。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import chat  # noqa: E402


def test_sync_waits_for_inflight_build(tmp_path, monkeypatch):
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    built = []
    gate = threading.Event()

    def slow_build(decrypted_dir, output_db, **kw):
        built.append(1)
        gate.wait(3)                       # 卡住，模拟"正在构建"
        os.makedirs(os.path.dirname(output_db), exist_ok=True)
        open(output_db, 'wb').write(b'SQLite')

    monkeypatch.setattr(chat, '_build_index_fn', lambda: slow_build, raising=False)
    chat.ensure_chat_index_async(str(dec))  # 后台开始
    time.sleep(0.1)
    gate.set()                              # 放行
    ok = chat._ensure_chat_index(str(dec))  # 同步调用
    assert ok is True
    assert len(built) == 1, '同步调用又开了一次构建：%d 次' % len(built)
