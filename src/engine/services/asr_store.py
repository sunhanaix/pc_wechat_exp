# -*- coding: utf-8 -*-
"""语音转文字（ASR）结果的持久化存储（issue #24）。

为什么要存：查看器里点一次「转文字」要跑本地 Whisper 或调百度接口，几秒/条；
以前结果只 return 不落盘，下次打开又得重跑、导出也没东西可带。

设计（与用户确认）：
  * 位置：`%LOCALAPPDATA%\\WeChatEXP\\transcripts.db`（跨备份、跨账号共享）；
  * 主键：**语音内容哈希**（调用方算好传入）⇒ 同一段语音在任何备份里都能命中；
  * 另存 `(chat_id, create_time, local_id)` 供按消息反查；
  * 只有**非空**结果才入库；重复写幂等（保留 `created_at`，刷新 `updated_at`）；
  * `manual=1` 表示用户手改过（重识别覆盖前应提示）。
"""
import os
import sqlite3
import threading
import time

_TABLE = """
CREATE TABLE IF NOT EXISTS transcripts (
  hash        TEXT PRIMARY KEY,
  text        TEXT NOT NULL,
  engine      TEXT DEFAULT '',
  model       TEXT DEFAULT '',
  language    TEXT DEFAULT '',
  duration_s  REAL DEFAULT 0,
  chat_id     TEXT DEFAULT '',
  create_time INTEGER DEFAULT 0,
  local_id    INTEGER DEFAULT 0,
  manual      INTEGER DEFAULT 0,
  hit_count   INTEGER DEFAULT 0,
  created_at  INTEGER,
  updated_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_transcripts_msg
  ON transcripts(chat_id, create_time, local_id);
"""

_FIELDS = ('engine', 'model', 'language', 'duration_s', 'chat_id', 'create_time',
           'local_id', 'manual')


def store_path() -> str:
    """默认库文件位置（用户目录，打包版可写）。"""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'WeChatEXP', 'transcripts.db')


def lookup_for_chat(chat_id, items, store: 'AsrStore' = None) -> dict:
    """批量取回某会话已存的转写，键为 ``'create_time:local_id'``（issue #24 第③步）。

    前端只知道「会话 + (create_time, local_id)」，不知道内容哈希，所以需要这个入口；
    未转写过的条目直接省略（前端据此决定按钮显示「转文字」还是「重新识别」）。
    """
    chat_id = str(chat_id or '')
    pairs = []
    for it in (items or []):
        try:
            pairs.append((int(it[0]), int(it[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if not chat_id or not pairs:
        return {}
    own = store is None
    st = store or AsrStore()
    try:
        rows = st.find_by_messages(chat_id, pairs)
    finally:
        if own:
            st.close()
    return {'%d:%d' % (ct, lid): row for (ct, lid), row in rows.items()}


class AsrStore:
    """线程安全的转写结果存储（SQLite）。"""

    def __init__(self, path: str = None):
        self.path = path or store_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_TABLE)
            self._conn.commit()

    # -- 写 ---------------------------------------------------------------
    def put(self, voice_hash: str, text: str, **meta):
        """写入/更新一条转写；空文本或空哈希直接忽略（不污染存储）。"""
        text = (text or '').strip()
        if not voice_hash or not text:
            return None
        now = int(time.time())
        row = [voice_hash, text,
               str(meta.get('engine') or ''), str(meta.get('model') or ''),
               str(meta.get('language') or ''), float(meta.get('duration_s') or 0),
               str(meta.get('chat_id') or ''), int(meta.get('create_time') or 0),
               int(meta.get('local_id') or 0), 1 if meta.get('manual') else 0, now, now]
        with self._lock:
            self._conn.execute(
                "INSERT INTO transcripts(hash, text, engine, model, language, duration_s,"
                " chat_id, create_time, local_id, manual, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(hash) DO UPDATE SET text=excluded.text, engine=excluded.engine,"
                " model=excluded.model, language=excluded.language,"
                " duration_s=excluded.duration_s, chat_id=excluded.chat_id,"
                " create_time=excluded.create_time, local_id=excluded.local_id,"
                " manual=excluded.manual, updated_at=excluded.updated_at",
                row)
            self._conn.commit()
        return voice_hash

    def delete(self, voice_hash: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM transcripts WHERE hash=?", (voice_hash,))
            self._conn.commit()
            return cur.rowcount or 0

    # -- 读 ---------------------------------------------------------------
    def get(self, voice_hash: str):
        row = self._get(voice_hash, touch=True)
        return row

    def _get(self, voice_hash, touch=False):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM transcripts WHERE hash=?", (voice_hash,))
            row = cur.fetchone()
            if row and touch:
                self._conn.execute(
                    "UPDATE transcripts SET hit_count=hit_count+1 WHERE hash=?", (voice_hash,))
                self._conn.commit()
        return dict(row) if row else None

    def get_many(self, hashes):
        """批量取（查看器一次请求带走整屏语音的文字）。返回 {hash: row}。"""
        out = {}
        for h in hashes or []:
            if not h or h in out:
                continue
            row = self._get(h, touch=True)
            if row:
                out[h] = row
        return out

    def find_by_messages(self, chat_id: str, pairs):
        """按 (create_time, local_id) 批量反查。返回 {(create_time, local_id): row}。

        issue #24 反馈：原来**一条消息一次 SQL**，几百条语音时页面会卡住；
        现在改为分块 `IN` 批量查询（每块 200 个），一次请求只碰几次数据库。
        """
        items = []
        for ct, lid in (pairs or []):
            try:
                items.append((int(ct), int(lid)))
            except (TypeError, ValueError):
                continue
        out = {}
        if not items or not chat_id:
            return out
        with self._lock:
            for i in range(0, len(items), 200):
                chunk = items[i:i + 200]
                cond = " OR ".join("(create_time=? AND local_id=?)" for _ in chunk)
                params = [chat_id]
                for ct, lid in chunk:
                    params.extend([ct, lid])
                cur = self._conn.execute(
                    "SELECT * FROM transcripts WHERE chat_id=? AND (" + cond + ")", params)
                for row in cur.fetchall():
                    out[(int(row['create_time']), int(row['local_id']))] = dict(row)
        return out

    def stats(self) -> dict:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n, SUM(manual) AS m,"
                                     " SUM(hit_count) AS h FROM transcripts")
            row = cur.fetchone()
        return {'total': int(row['n'] or 0), 'manual': int(row['m'] or 0),
                'hits': int(row['h'] or 0), 'path': self.path}

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

def _hash_of_path(path: str) -> str:
    """按文件内容算哈希（与 media._voice_content_hash 同一算法，供测试/复用）。"""
    import hashlib
    try:
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()[:16]
    except OSError:
        return ''


def save_transcript_for_voice(decrypted_dir: str, voice_path: str, text: str, chat: str = None,
                              create_time: int = None, local_id: int = None,
                              db_dir: str = None, engine: str = 'local', model: str = '',
                              store: 'AsrStore' = None) -> str:
    """把一段识别文字按**语音内容哈希**入库（issue #24 第③步）。

    默认引擎的识别跑在浏览器里，结果不会经过服务端，所以前端识别成功后要把文字回传到这里；
    入库后查看器能自动带出、导出（④⑤）也能带上。返回哈希；没定位到语音或文字为空返回 ''。
    """
    text = (text or '').strip()
    if not text:
        return ''
    from .media import locate_voice_file
    silk = locate_voice_file(decrypted_dir, voice_path, create_time, local_id,
                             db_dir=db_dir, chat=chat)
    vhash = _hash_of_path(silk) if silk else ''
    if not vhash:
        return ''
    own = store is None
    st = store or AsrStore()
    try:
        st.put(vhash, text, chat_id=chat or '', create_time=create_time or 0,
               local_id=local_id or 0, engine=engine or 'local', model=model or '')
    finally:
        if own:
            st.close()
    return vhash


