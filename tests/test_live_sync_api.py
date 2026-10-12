import json
import time
from contextlib import closing
import sqlite3

import pytest
from web.app import create_app
from engine.services.live_sync import service as sync_module
from tests.test_live_sync import make_source, keys, CHAT


@pytest.fixture
def client(tmp_path, monkeypatch):
    source = make_source(tmp_path)
    monkeypatch.setattr(sync_module, 'configured_keys', keys)
    monkeypatch.setattr('engine.services.media._detect_wxid', lambda _: None)
    backup = tmp_path / 'original_backup'; backup.mkdir()
    app = create_app(str(backup), db_dir=str(source.parents[1]))
    app.testing = True
    yield app.test_client(), app, source, tmp_path / 'live'
    app.extensions['message_sync'].shutdown()


def test_controls_ready_sse_and_viewer_queries(client):
    http, app, source, output = client
    assert http.get('/sync').status_code == 200
    assert http.get('/api/sync/status').json['running'] is False
    response = http.post('/api/sync/start', json={'output_dir': str(output), 'interval': .2})
    assert response.status_code == 202
    deadline = time.monotonic() + 5
    while not http.get('/api/sync/status').json['ready'] and time.monotonic() < deadline:
        time.sleep(.02)
    assert http.get('/api/sync/status').json['viewer_active']
    assert app.config['DECRYPTED_DIR'] == str(output)
    contacts = http.get('/api/contacts').json['contacts']
    assert contacts[0]['id'] == CHAT
    assert contacts[0]['msg_count'] == 2
    messages = http.get('/api/messages', query_string={'chat_id': CHAT}).json
    assert len(messages['messages']) == 2
    assert http.post('/api/sync/start', json={}).status_code == 409
    stream = http.get('/api/sync/events', buffered=False)
    assert stream.mimetype == 'text/event-stream'
    assert b'resync' in next(iter(stream.response))
    assert len(app.extensions['message_sync'].listeners) == 1
    stream.close()
    assert not app.extensions['message_sync'].listeners
    assert not http.post('/api/sync/stop', json={}).json['running']
    assert http.get('/api/messages', query_string={'chat_id': CHAT}).status_code == 200


@pytest.mark.parametrize('body', [[], {'interval': True}, {'interval': 0}, {'output_dir': []}, {'db_dir': 123}])
def test_rejects_bad_configuration(client, body):
    http, *_ = client
    assert http.post('/api/sync/start', json=body).status_code == 400


def test_sse_updates_and_coalesces_slow_client(client):
    http, app, *_ = client
    stream = http.get('/api/sync/events', buffered=False)
    assert b'sync.status' in next(iter(stream.response))
    controller = app.extensions['message_sync']
    for _ in range(40):
        controller.publish({'type': 'data.updated', 'chats': [CHAT], 'newMessages': 1})
    listener = next(iter(controller.listeners))
    assert listener.qsize() == 32
    assert b'data.updated' in next(iter(stream.response))
    stream.close()
    assert not controller.listeners
