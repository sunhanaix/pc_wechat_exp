# -*- coding: utf-8 -*-
"""issue #27：/api/contacts 分页（limit/offset），并保持旧行为向后兼容。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app import create_app  # noqa: E402

FAKE = [{'id': 'wxid_%03d' % i, 'name': '联系人%03d' % i, 'msg_count': i, 'is_group': 0}
        for i in range(25)]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    app = create_app(str(tmp_path))
    import web.routes.api as api_mod
    monkeypatch.setattr(api_mod, 'get_contacts', lambda *_a, **_k: [dict(c) for c in FAKE])
    return app.test_client()


def _get(client, qs=''):
    r = client.get('/api/contacts' + qs)
    assert r.status_code == 200, r.data[:200]
    return r.get_json()


def test_no_limit_keeps_old_behaviour(client):
    d = _get(client)
    assert d['total'] == 25 and len(d['contacts']) == 25


def test_limit_and_offset(client):
    d = _get(client, '?limit=10&offset=10')
    assert d['total'] == 25 and len(d['contacts']) == 10
    assert d['contacts'][0]['id'] == 'wxid_010'
    assert d['offset'] == 10 and d['limit'] == 10


def test_offset_beyond_end_returns_empty(client):
    d = _get(client, '?limit=10&offset=100')
    assert d['total'] == 25 and d['contacts'] == []


def test_limit_zero_or_invalid_keeps_all(client):
    assert len(_get(client, '?limit=0')['contacts']) == 25
    assert len(_get(client, '?limit=abc')['contacts']) == 25


def test_limit_with_keyword(client):
    d = _get(client, '?q=联系人01&limit=3')
    assert d['total'] == 10 and len(d['contacts']) == 3      # 010..019
