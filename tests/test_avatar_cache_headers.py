# -*- coding: utf-8 -*-
"""issue #27 深入②：头像接口应带 ETag + Cache-Control，并支持 304 协商。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import web.routes.avatar_api as av
    monkeypatch.setattr(av, '_fetch_avatar', lambda d, u: ('png', b'PNGDATA'))
    app = create_app(str(tmp_path))
    return app.test_client()


def test_first_response_has_cache_headers(client):
    r = client.get('/api/avatar/wxid_demo')
    assert r.status_code == 200
    assert r.headers.get('ETag'), '应返回 ETag'
    assert 'max-age' in (r.headers.get('Cache-Control') or ''), '应返回 Cache-Control'
    assert r.data == b'PNGDATA'


def test_second_request_with_etag_returns_304(client):
    r1 = client.get('/api/avatar/wxid_demo')
    etag = r1.headers['ETag']
    r2 = client.get('/api/avatar/wxid_demo', headers={'If-None-Match': etag})
    assert r2.status_code == 304, '带相同 ETag 应返回 304（不再传图片数据）'
    assert not r2.data


def test_different_avatar_has_different_etag(client, monkeypatch):
    import web.routes.avatar_api as av
    r1 = client.get('/api/avatar/wxid_a')
    monkeypatch.setattr(av, '_fetch_avatar', lambda d, u: ('png', b'OTHER'))
    r2 = client.get('/api/avatar/wxid_a')
    assert r1.headers['ETag'] != r2.headers['ETag']
