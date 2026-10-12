"""One worker per live directory: snapshots, durable deduplication and events."""
import hashlib
import json
import logging
import queue
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

from backup.decryptor import _resolve_key
from backup.indexer import _load_display_names
from .locking import byte_lock, SourceBusy
from .snapshot import encrypted_snapshot, source_token
from .watcher import DirectoryWatcher

logger = logging.getLogger(__name__)
SHARD = re.compile(r'message_\d+\.db$', re.I)


def configured_keys():
    # Read only: config helpers can migrate/persist legacy keys implicitly.
    from engine.config_file import _config_path
    with open(_config_path(), encoding='utf-8') as stream:
        cfg = json.load(stream)
    return cfg.get('db_keys', {}), cfg.get('db_keys_by_salt', {})


def default_output(source):
    from engine.config_file import _config_path
    source = Path(source).resolve()
    identity = hashlib.sha256(str(source).casefold().encode()).hexdigest()[:12]
    return Path(_config_path()).parent / 'output' / 'live_sync' / identity


class MessageSyncService:
    def __init__(self, source_dir, output_dir=None, interval=1.0, on_publish=None,
                 keys_provider=None):
        self.source = Path(source_dir).resolve()
        self.output = Path(output_dir or default_output(self.source)).resolve()
        if not self.source.is_dir() or not any((self.source / 'message').glob('message_[0-9]*.db')):
            raise ValueError('微信源目录不存在或没有消息分片')
        if self.source == self.output or self.source in self.output.parents or self.output in self.source.parents:
            raise ValueError('实时目录与微信源目录不能相同或互相包含')
        if not .2 <= float(interval) <= 60:
            raise ValueError('复核间隔必须在 0.2 到 60 秒之间')
        self.interval = float(interval)
        self.on_publish = on_publish
        self.keys_provider = keys_provider or configured_keys
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._owner_lock = None
        self._tokens = {}
        self._errors = {}
        self._subscribers = set()
        self._sequence = 0
        self._status = dict(running=False, ready=False, source_dir=str(self.source),
                            output_dir=str(self.output), interval=self.interval,
                            watcher='stopped', synced_files=0, new_messages=0,
                            last_sync=None, last_error=None, source_lock_seconds=0)

    def status(self):
        with self._lock:
            return dict(self._status, errors=dict(self._errors))

    def _prepare(self):
        self.output.mkdir(parents=True, exist_ok=True)
        owner = self.output / 'sync_owner.json'
        identity = dict(format=1, source=str(self.source))
        self._owner_lock = byte_lock(self.output / 'service.lock', 0, 1, exclusive=True, create=True)
        self._owner_lock.__enter__()
        try:
            if owner.exists():
                if json.loads(owner.read_text(encoding='utf-8')) != identity:
                    raise ValueError('实时目录已属于其他账号或服务版本')
            else:
                if any(p.name != 'service.lock' for p in self.output.iterdir()):
                    raise ValueError('实时目录必须为空，不能覆盖已有备份')
                owner.write_text(json.dumps(identity), encoding='utf-8')
            (self.output / 'data').mkdir(exist_ok=True)
            with closing(self._catalog()) as conn:
                conn.executescript('''
                    CREATE TABLE IF NOT EXISTS sync_messages (
                        source_db TEXT, chat_id TEXT, local_id INTEGER, create_time INTEGER,
                        local_type INTEGER, digest TEXT,
                        PRIMARY KEY(source_db,chat_id,local_id,create_time));
                    CREATE TABLE IF NOT EXISTS sync_shards (
                        source_db TEXT PRIMARY KEY, digest TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS chats (
                        chat_id TEXT PRIMARY KEY, display_name TEXT, message_count INTEGER,
                        first_msg_time INTEGER, last_msg_time INTEGER, is_group INTEGER, source_db TEXT);
                    CREATE INDEX IF NOT EXISTS sync_chat ON sync_messages(chat_id);
                ''')
                self._initial = conn.execute('SELECT COUNT(*) FROM sync_shards').fetchone()[0] == 0
        except Exception:
            self._release_owner()
            raise

    def _release_owner(self):
        if self._owner_lock is not None:
            self._owner_lock.__exit__(None, None, None)
            self._owner_lock = None

    def _catalog(self):
        conn = sqlite3.connect(self.output / 'data' / 'chats.db', timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        return conn

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._prepare()
            self._stop.clear()
            self._tokens.clear()  # Reconcile the mirror and ledger on every restart.
            self._status.update(running=True, ready=False, last_error=None, new_messages=0,
                                synced_files=0)
            self._thread = threading.Thread(target=self._run, name='wechat-message-sync', daemon=True)
            self._thread.start()
        return self.status()

    def stop(self, timeout=10):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return self.status()  # If a snapshot is still in flight, running remains true.

    def run_once(self):
        """Standalone one-shot reconciliation, useful for CLI and diagnostics."""
        self._prepare()
        try:
            self._stop.clear()
            self._status['running'] = True
            self._cycle()
        finally:
            self._status['running'] = False
            self._release_owner()
        return self.status()

    def subscribe(self):
        events = queue.Queue(maxsize=32)
        with self._lock:
            self._subscribers.add(events)
        return events

    def unsubscribe(self, events):
        with self._lock:
            self._subscribers.discard(events)

    def _publish(self, **payload):
        with self._lock:
            self._sequence += 1
            event = dict(eventId=str(self._sequence), timestamp=time.time(), **payload)
            for events in self._subscribers:
                try:
                    events.put_nowait(event)
                except queue.Full:
                    try:
                        events.get_nowait()
                    except queue.Empty:
                        pass  # Consumer may have drained it after put_nowait.
                    events.put_nowait(dict(event, resync=True))
        if self.on_publish:
            self.on_publish(event)

    def _files(self):
        files = sorted(p for p in (self.source / 'message').glob('*.db') if SHARD.fullmatch(p.name))
        for rel in ('contact/contact.db', 'session/session.db', 'hardlink/hardlink.db',
                    'message/message_resource.db'):
            candidate = self.source / rel
            if candidate.is_file():
                files.append(candidate)
        # Auxiliary names first, so the initial message catalog has display names.
        return sorted(files, key=lambda p: (SHARD.fullmatch(p.name) is not None, str(p)))

    def _mirror(self, decoded, relative):
        target = self.output / relative
        resolved = target.resolve()
        if self.output not in resolved.parents or self.source in resolved.parents:
            raise ValueError('实时目录中的路径不能指向源目录或其他目录')
        target.parent.mkdir(parents=True, exist_ok=True)
        # SQLite backup writes one transaction. Readers retain their old snapshot;
        # replacing an open destination file on Windows is deliberately avoided.
        with closing(sqlite3.connect(Path(decoded).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as src:
            with closing(sqlite3.connect(target, timeout=5)) as dst:
                dst.execute('PRAGMA journal_mode=WAL')
                src.backup(dst, pages=256, sleep=.01)
                dst.execute('PRAGMA wal_checkpoint(PASSIVE)')

    def _index(self, decoded, relative):
        with closing(sqlite3.connect(Path(decoded).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as src:
            names = {hashlib.md5(u.encode()).hexdigest(): u
                     for (u,) in src.execute('SELECT user_name FROM Name2Id') if u}
            rows = []
            for (table,) in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*'"):
                quoted = '"' + table.replace('"', '""') + '"'
                chat_id = names.get(table[4:], 'unknown_' + table[4:])
                for local_id, ts, kind, content in src.execute(
                        'SELECT local_id,create_time,local_type,message_content FROM ' + quoted):
                    digest = hashlib.sha256(repr((kind, content)).encode()).hexdigest()
                    rows.append((str(relative), chat_id, local_id, ts, kind, digest))
        new = {(r[1], r[2], r[3]): r[5] for r in rows}
        with closing(self._catalog()) as catalog:
            old = {(c, i, t): d for c, i, t, d in catalog.execute(
                'SELECT chat_id,local_id,create_time,digest FROM sync_messages WHERE source_db=?', (str(relative),))}
            inserted = set(new) - set(old)
            affected = {k[0] for k in set(new) | set(old) if old.get(k) != new.get(k)}
            digest = hashlib.sha256(repr(sorted(new.items())).encode()).hexdigest()
            with catalog:
                catalog.execute('DELETE FROM sync_messages WHERE source_db=?', (str(relative),))
                catalog.executemany('INSERT INTO sync_messages VALUES (?,?,?,?,?,?)', rows)
                catalog.execute('INSERT OR REPLACE INTO sync_shards VALUES (?,?)', (str(relative), digest))
                self._refresh_chats(catalog)
        return sorted(affected), (0 if self._initial else len(inserted)), bool(affected)

    def _refresh_chats(self, conn):
        display = _load_display_names(str(self.output))
        conn.execute('DELETE FROM chats')
        for chat_id, count, first, last, sources in conn.execute('''
                SELECT chat_id,COUNT(*),MIN(create_time),MAX(create_time),GROUP_CONCAT(DISTINCT source_db)
                FROM sync_messages WHERE create_time>1000000000 GROUP BY chat_id''').fetchall():
            conn.execute('INSERT INTO chats VALUES (?,?,?,?,?,?,?)',
                         (chat_id, display.get(chat_id, chat_id), count, first, last,
                          int(chat_id.endswith('@chatroom')),
                          ', '.join(sorted({Path(s).name for s in sources.split(',')}))))

    def _cycle(self):
        was_ready = self.status()['ready']
        message_ok = False
        for path in self._files():
            if self._stop.is_set():
                break
            relative = path.relative_to(self.source)
            label = str(relative)
            try:
                token = source_token(path)
                if (token == self._tokens.get(label) and label not in self._errors
                        and (self.output / relative).is_file()):
                    message_ok |= SHARD.fullmatch(path.name) is not None
                    continue
                keys, salts = self.keys_provider()
                key = _resolve_key(keys, str(path), salts)
                if key is None:
                    raise ValueError('没有与源库匹配的已验证密钥')
                with tempfile.TemporaryDirectory(prefix='wx-sync-') as directory:
                    decoded = Path(directory) / 'snapshot.db'
                    info = encrypted_snapshot(path, key, decoded)
                    self._mirror(decoded, relative)
                    if SHARD.fullmatch(path.name):
                        chats, added, changed = self._index(decoded, relative)
                        message_ok = True
                    else:
                        if relative.as_posix() == 'contact/contact.db':
                            from engine.services.name_resolver import invalidate_names
                            from engine.services.address_book import invalidate_address_book
                            invalidate_names(str(self.output))
                            invalidate_address_book(str(self.output))
                        with closing(self._catalog()) as catalog, catalog:
                            self._refresh_chats(catalog)
                        chats, added, changed = [], 0, True
                # Checkpoint of identical messages advances the source token but
                # does not cause another message event or increment the counter.
                self._tokens[label] = info['token']
                with self._lock:
                    self._errors.pop(label, None)
                    self._status.update(last_sync=time.time(), synced_files=len(self._tokens),
                                        source_lock_seconds=info['source_lock_seconds'])
                    self._status['new_messages'] += added
                if changed:
                    self._publish(type='data.updated', chats=chats, newMessages=added,
                                  initial=self._initial, sourceDb=label)
            except Exception as exc:
                with self._lock:
                    self._errors[label] = str(exc)
                if not isinstance(exc, SourceBusy):
                    logger.debug('Message sync retry for %s: %s', label, exc)
                # No successful token recorded: failures are retried on the next cycle.
        required = {str(p.relative_to(self.source)) for p in self._files() if SHARD.fullmatch(p.name)}
        ready = message_ok and required <= self._tokens.keys() and not (required & self._errors.keys())
        with self._lock:
            self._status.update(ready=ready, last_error=next(iter(self._errors.values()), None))
        if ready:
            self._initial = False
            # Initial publication can have no new/changed rows on restart.
            if ready and not was_ready:
                self._publish(type='sync.ready', chats=[], newMessages=0)

    def _run(self):
        watcher = DirectoryWatcher(self.source)
        with self._lock:
            self._status['watcher'] = watcher.mode
        try:
            while not self._stop.is_set():
                self._cycle()
                if watcher.wait(self._stop, self.interval):
                    self._stop.wait(.15)  # Coalesce a burst without postponing forever.
                with self._lock:
                    self._status['watcher'] = watcher.mode
        except Exception as exc:
            logger.exception('Message sync worker stopped')
            with self._lock:
                self._status['last_error'] = str(exc)
        finally:
            watcher.close()
            self._release_owner()
            with self._lock:
                self._status.update(running=False, watcher='stopped')
