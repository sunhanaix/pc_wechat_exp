# -*- coding: utf-8 -*-
"""导出侧按内容哈希兜底查找（issue #24 反馈第 1 点）。

场景：文字存在库里，但存的时候没带上/带错了 (chat, create_time, local_id) ——
只要语音文件还在，就该能靠**内容哈希**把文字找回来。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_export  # noqa: E402
from engine.services import asr_store  # noqa: E402

CHAT = 'wxid_demo_chat'


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_store, 'store_path', lambda: str(tmp_path / 't.db'))
    voice = tmp_path / 'demo.silk'
    voice.write_bytes(b'SILK-DEMO-BYTES')
    monkeypatch.setattr(chat_export, '_locate_voice_file',
                        lambda dec, vp, ct, lid, db_dir=None, chat=None: str(voice))
    return str(tmp_path), str(voice)


def test_hash_fallback_finds_text_when_tuple_misses(env):
    """库里的行没有 chat/ct/lid（旧数据或参数缺失）⇒ 仍要按哈希找回来。"""
    tmp, voice = env
    h = asr_store._hash_of_path(voice)
    st = asr_store.AsrStore()
    st.put(h, '靠哈希找回来的文字')          # 注意：不传 chat/create_time/local_id
    st.close()
    lookup = chat_export.make_asr_lookup(CHAT, decrypted_dir=tmp)
    assert lookup(1614933012, 7) == '靠哈希找回来的文字'


def test_tuple_lookup_still_wins(env):
    """三元组命中时直接用（快路径），不需要定位文件。"""
    tmp, voice = env
    h = asr_store._hash_of_path(voice)
    st = asr_store.AsrStore()
    st.put(h, '三元组里的文字', chat_id=CHAT, create_time=100, local_id=1)
    st.close()
    lookup = chat_export.make_asr_lookup(CHAT, decrypted_dir=tmp)
    assert lookup(100, 1) == '三元组里的文字'


def test_no_match_returns_none(env):
    tmp, _v = env
    lookup = chat_export.make_asr_lookup(CHAT, decrypted_dir=tmp)
    assert lookup(999, 999) is None


def test_bad_input_returns_none(env):
    tmp, _v = env
    lookup = chat_export.make_asr_lookup(CHAT, decrypted_dir=tmp)
    assert lookup(None, None) is None


def test_without_decrypted_dir_only_tuple_works(env):
    """没给解密目录时退回旧行为（只按三元组），且不报错。"""
    tmp, voice = env
    h = asr_store._hash_of_path(voice)
    st = asr_store.AsrStore()
    st.put(h, '只有哈希可查')
    st.close()
    lookup = chat_export.make_asr_lookup(CHAT)
    assert lookup(1614933012, 7) is None
