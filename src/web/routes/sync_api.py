"""Per-app background sync controls and reconnectable update notifications."""
import atexit
import json
import queue
import threading

from flask import Blueprint, Response, current_app, jsonify, request
from engine.services.live_sync import MessageSyncService

sync_bp = Blueprint('sync_api', __name__, url_prefix='/api/sync')


class SyncController:
    def __init__(self, app):
        self.app = app
        self.service = None
        self.lock = threading.RLock()
        self.listeners = set()
        self.sequence = 0

    def status(self):
        with self.lock:
            state = self.service.status() if self.service else dict(running=False, ready=False)
            state['viewer_active'] = bool(self.service and
                self.app.config.get('DECRYPTED_DIR') == str(self.service.output))
            return state

    def publish(self, event):
        with self.lock:
            if event['type'] == 'sync.ready' and self.service and self.service.status()['ready']:
                self.app.config.update(DECRYPTED_DIR=str(self.service.output),
                                       DB_DIR=str(self.service.source), WXID=self.service.source.parent.name)
            self.sequence += 1
            event = dict(event, eventId=str(self.sequence))
            for listener in self.listeners:
                try:
                    listener.put_nowait(event)
                except queue.Full:
                    try:
                        listener.get_nowait()
                    except queue.Empty:
                        pass
                    listener.put_nowait(dict(event, resync=True))

    def shutdown(self):
        if self.service:
            self.service.stop()


def init_sync(app):
    controller = SyncController(app)
    app.extensions['message_sync'] = controller
    atexit.register(controller.shutdown)
    app.register_blueprint(sync_bp)


@sync_bp.get('/status')
def status():
    from engine.config_file import get_db_dir
    state = current_app.extensions['message_sync'].status()
    state.setdefault('source_dir', current_app.config.get('DB_DIR') or get_db_dir() or '')
    return jsonify(state)


@sync_bp.post('/start')
def start():
    from engine.config_file import get_db_dir
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='需要 JSON 对象'), 400
    controller = current_app.extensions['message_sync']
    try:
        if data.get('db_dir') is not None and not isinstance(data['db_dir'], str):
            raise ValueError('微信源目录必须是字符串')
        source = data.get('db_dir') or current_app.config.get('DB_DIR') or get_db_dir()
        if not isinstance(source, str) or not source:
            raise ValueError('请指定微信 db_storage 目录')
        output = data.get('output_dir')
        if output is not None and not isinstance(output, str):
            raise ValueError('实时目录必须是字符串')
        output = output or None
        interval = data.get('interval', 1)
        if isinstance(interval, bool):
            raise ValueError('复核间隔必须是数字')
        with controller.lock:
            if controller.service and controller.service.status()['running']:
                return jsonify(controller.status()), 409
            service = MessageSyncService(source, output, interval, on_publish=controller.publish)
            previous = controller.service
            controller.service = service
            try:
                service.start()
            except Exception:
                controller.service = previous
                raise
        return jsonify(controller.status()), 202
    except (ValueError, TypeError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@sync_bp.post('/stop')
def stop():
    controller = current_app.extensions['message_sync']
    # Do not hold controller.lock while joining a worker which publishes through it.
    service = controller.service
    if service:
        service.stop()
    return jsonify(controller.status())


@sync_bp.get('/events')
def events():
    controller = current_app.extensions['message_sync']
    listener = queue.Queue(maxsize=32)

    def stream():
        with controller.lock:
            controller.listeners.add(listener)
            ready = controller.status()['ready']
        try:
            # Reconnect always re-queries latest data: no gap in an in-memory SSE history.
            if ready:
                yield 'event: data.updated\ndata: {"resync":true,"chats":[]}\n\n'
            else:
                yield 'event: sync.status\ndata: {"ready":false}\n\n'
            while True:
                try:
                    event = listener.get(timeout=15)
                    yield 'id: %s\nevent: data.updated\ndata: %s\n\n' % (
                        event['eventId'], json.dumps(event, ensure_ascii=False))
                except queue.Empty:
                    yield ': keepalive\n\n'
        finally:
            with controller.lock:
                controller.listeners.discard(listener)

    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
