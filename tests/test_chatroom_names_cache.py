# -*- coding: utf-8 -*-
"""issue #27 深入①：群名映射解析结果应按数据目录指纹缓存（每次请求省 ~0.4s）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import chat  # noqa: E402


def _mk(tmp_path):
    dec = tmp_path / 'decrypted'
    (dec / 'contact').mkdir(parents=True)
    (dec / 'contact' / 'contact.db').write_bytes(b'x')
    return dec


def test_second_call_hits_cache(tmp_path):
    dec = _mk(tmp_path)
    a = chat._load_chatroom_names(str(dec))
    b = chat._load_chatroom_names(str(dec))
    assert a is b, '第二次应直接复用缓存对象（否则每次请求都会重新解析）'


def test_cache_invalidated_on_data_change(tmp_path):
    dec = _mk(tmp_path)
    a = chat._load_chatroom_names(str(dec))
    p = dec / 'contact' / 'contact.db'
    st = p.stat()
    os.utime(str(p), (st.st_atime, st.st_mtime + 10))     # 数据变了
    b = chat._load_chatroom_names(str(dec))
    assert a is not b, '数据变化后必须重新解析'


def test_different_dirs_do_not_share(tmp_path):
    d1 = _mk(tmp_path / 'a')
    d2 = _mk(tmp_path / 'b')
    assert chat._load_chatroom_names(str(d1)) is not chat._load_chatroom_names(str(d2))
