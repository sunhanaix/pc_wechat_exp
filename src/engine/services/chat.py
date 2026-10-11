"""Contact / group list queries.

Uses the same resolution strategy as chat_list.scan_chats():
1. Pre-load contact.db → id_to_name + name_to_id maps
2. Pre-load session.db → session_summaries
3. Pre-load chat_room → room_owners
4. Build group_members → sender display names per chatroom
5. Resolve each chat via _resolve_display()
"""
import hashlib
import json
import os
import threading
import re
import sqlite3

from engine.services.name_resolver import pick_display_name, resolve_wxid, _load_chatroom_names, chatroom_fallback_name
from engine.services.message.decode import decompress_content
from engine.utils import bare_wxid

# Max characters for auto-generated group name before truncation
_GROUP_NAME_MAX_LEN = 32


def _own_wxid_forms(wxid):
    """本人 wxid 的**两种可接受形式**的集合（known-issues #20）。

    调用方传进来的值来自 `app.config['WXID']` ← `_detect_wxid()`，实测是**账号目录名**
    （24 字符，形如 `<裸 wxid>_10e8`）；而这里的 `Name2Id.user_name` / `chats.db.chat_id`
    存的是**裸 wxid**（19 字符）。只用一种形式比较 → **永不相等** → 本人会话泄漏。
    所以两种形式都接受：集合里多一个不存在的值是无害的，而配置里存的是哪种形式
    我们无法保证（与 `search._resolve_scope` 的裁决一致）。

    空值（`None` / `''`）被丢掉 → 返回**空集** → 调用方不排除任何会话，与旧行为一致。
    """
    return {form for form in (wxid, bare_wxid(wxid)) if form}


def _build_index_fn():
    """索引构建函数（单独一层便于测试替换）。"""
    from backup.indexer import build_index
    return build_index


def _ensure_chat_index(decrypted_dir: str) -> bool:
    """确保 data/chats.db 索引存在，缺失则**补建一次**（issue #27）。

    没有这个索引时 `get_contacts()` 会退化成"扫描所有消息分片"的慢路径：真机实测
    2,539 个会话需要 **218 秒**，页面表现为长时间卡在"正在加载联系人"。
    建好之后每次请求都走快路径。构建失败返回 False，调用方照旧走慢路径（不影响可用性）。
    """
    db = os.path.join(decrypted_dir, 'data', 'chats.db')
    if os.path.isfile(db) and os.path.getsize(db) > 0:
        return True
    # 已有构建在飞（例如启动时的后台预建）⇒ 等它，别重复构建同一个 DB
    inflight = None
    with _index_build_lock:
        inflight = _index_build_threads.get(os.path.abspath(decrypted_dir))
    if (inflight is not None and inflight.is_alive() and inflight is not threading.current_thread()):
        inflight.join(timeout=600)
        return os.path.isfile(db) and os.path.getsize(db) > 0
    try:
        os.makedirs(os.path.dirname(db), exist_ok=True)
        _build_index_fn()(decrypted_dir, db)
        return os.path.isfile(db) and os.path.getsize(db) > 0
    except Exception:
        return False


_index_build_lock = threading.Lock()
_index_build_inflight = set()
_index_build_threads = {}


def ensure_chat_index_async(decrypted_dir: str):
    """后台（守护线程）预建 chats.db 索引，立即返回（issue #27）。

    首次打开聊天页若没有索引，`get_contacts()` 要现场建索引（真机实测约 28 秒）；
    在服务启动时先把这一步做掉，用户第一次打开页面就是快的。
    同一目录并发调用只会真正构建一次。
    """
    key = os.path.abspath(decrypted_dir)
    db = os.path.join(decrypted_dir, 'data', 'chats.db')
    if os.path.isfile(db) and os.path.getsize(db) > 0:
        return None
    with _index_build_lock:
        if key in _index_build_inflight:
            return None
        _index_build_inflight.add(key)

    def _run():
        try:
            _ensure_chat_index(decrypted_dir)
        finally:
            with _index_build_lock:
                _index_build_inflight.discard(key)
                _index_build_threads.pop(key, None)

    th = threading.Thread(target=_run, name='chats-index-prebuild', daemon=True)
    with _index_build_lock:
        _index_build_threads[key] = th
    th.start()
    return th


def _slow_cache_fingerprint(decrypted_dir: str) -> str:
    """慢路径缓存的指纹：各 message_N.db + contact.db + session.db 的 (名字,大小,mtime)。

    任一源文件变化（重新解密/新备份）⇒ 指纹不同 ⇒ 缓存自动失效。
    """
    cands = []
    for base in (os.path.join(decrypted_dir, 'message'), decrypted_dir):
        try:
            for name in sorted(os.listdir(base)):
                if re.match(r'message_\d+\.db$', name, re.IGNORECASE):
                    cands.append(os.path.join(base, name))
        except OSError:
            pass
    for rel in ('contact/contact.db', 'session/session.db'):
        p = os.path.join(decrypted_dir, *rel.split('/'))
        if os.path.isfile(p):
            cands.append(p)
    parts = []
    for p in sorted(set(cands)):
        try:
            st = os.stat(p)
            parts.append('%s:%d:%d' % (os.path.basename(p), st.st_size, int(st.st_mtime)))
        except OSError:
            pass
    return '|'.join(parts)


def _slow_cache_path(decrypted_dir: str) -> str:
    return os.path.join(decrypted_dir, 'data', 'contacts_cache.json')


def _load_slow_cache(decrypted_dir: str, wxid):
    """命中返回联系人列表；未命中/损坏/指纹不符返回 None（issue #27-B(d)）。"""
    fp = _slow_cache_fingerprint(decrypted_dir)
    if not fp:
        return None
    try:
        with open(_slow_cache_path(decrypted_dir), encoding='utf-8') as f:
            data = json.load(f)
        if data.get('fingerprint') != fp:
            return None
        if (data.get('wxid') or None) != (wxid or None):
            return None
        contacts = data.get('contacts')
        if isinstance(contacts, list) and contacts:
            return contacts
    except Exception:
        return None
    return None


def _save_slow_cache(decrypted_dir: str, wxid, contacts) -> None:
    """原子写入慢路径结果（失败静默：缓存只是加速，绝不能影响正确性）。"""
    try:
        fp = _slow_cache_fingerprint(decrypted_dir)
        if not fp or not contacts:
            return
        path = _slow_cache_path(decrypted_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'fingerprint': fp, 'wxid': wxid, 'contacts': contacts},
                      f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def get_contacts(decrypted_dir: str, wxid: str = None) -> list:
    """Return all contacts from decrypted WeChat 4.x message databases.

    Fast path: reads pre-computed summaries from data/chats.db (built during
    backup index phase). Falls back to slow full-scan if chats.db is missing.
    """
    # Fast path: pre-built summary index
    # 注意：这里**不要**自动补建索引 —— 快路径与慢路径的 chat_id 形式不同
    # （慢路径做了裸 wxid 归一化），混用会改变结果集（issue #27 回归修复）。
    # 索引的补建放在 serve 启动时的后台预建里（ensure_chat_index_async）。
    contacts = _load_from_chats_db(decrypted_dir, wxid)
    if contacts:
        _enrich_contacts_chatroom_names(decrypted_dir, contacts)
        return contacts

    # issue #27-B(d)：慢路径结果缓存（指纹不变就不再全库扫描）
    cached = _load_slow_cache(decrypted_dir, wxid)
    if cached is not None:
        return cached

    # Slow path: full scan of all message databases
    msg_dir = os.path.join(decrypted_dir, "message")
    if os.path.isdir(msg_dir):
        msg_dbs = _find_msg_dbs(msg_dir)
    else:
        # Flat backup layout: .db files directly in decrypted_dir
        msg_dbs = []
        try:
            for f in os.listdir(decrypted_dir):
                m = re.match(r'message_(\d+)\.db', f, re.IGNORECASE)
                if m:
                    msg_dbs.append((int(m.group(1)), os.path.join(decrypted_dir, f)))
        except OSError:
            pass
    if not msg_dbs:
        return []

    # Pre-load all contact name mappings (same as chat_list.py)
    contact_db = _find_file(decrypted_dir, "contact/contact.db", "contact.db")
    if not os.path.isfile(contact_db):
        # Fallback: output/decrypted for backup-based data
        alt = os.path.normpath(os.path.join(decrypted_dir, "..", "..", "..",
                                            "output", "decrypted", "contact", "contact.db"))
        if os.path.isfile(alt):
            contact_db = alt
    session_db = _find_file(decrypted_dir, "session/session.db", "session.db")

    id_to_name, name_to_id, name_to_avatar = _load_contacts(contact_db)
    session_summaries = _load_sessions(session_db)
    room_owners = _load_room_owners(contact_db)

    hash_to_name = _build_hash_map(msg_dbs)
    group_members = _build_group_members(msg_dbs, hash_to_name,
                                          name_to_id, id_to_name)

    # Pre-load chatroom names from chat_room.ext_buffer for the slow path
    chatroom_names = _load_chatroom_names(decrypted_dir)

    contacts = {}
    own_forms = _own_wxid_forms(wxid)
    for idx, db_path in msg_dbs:
        try:
            conn = sqlite3.connect(db_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (tname,) in tables:
                h = tname[4:]
                uname = hash_to_name.get(h)
                if not uname:
                    uname = f"unknown_{h[:8]}"
                if uname in own_forms:
                    continue

                # issue #27-B：COUNT 与 MAX 合并成一条查询（每个会话少一次往返；
                # 结果与分两条查完全一致）
                count_row, last_row = conn.execute(
                    f"SELECT COUNT(*), MAX(create_time) FROM [{tname}]"
                ).fetchone()[:2]
                count_row = (count_row,)
                last_row = (last_row,)
                cnt = count_row[0] if count_row else 0
                last_time = last_row[0] if last_row else 0

                if uname in contacts and (last_time or 0) <= (contacts[uname].get('last_msg_time') or 0):
                    continue

                is_group = uname.endswith('@chatroom')
                resolved = _resolve_display(uname, is_group, decrypted_dir,
                                              id_to_name, name_to_id,
                                              session_summaries, room_owners,
                                              group_members)
                # Fallback to chat_room.ext_buffer if still unresolved
                if resolved == uname and is_group:
                    resolved = chatroom_names.get(uname, resolved)
                contacts[uname] = {
                    'id': uname,
                    'name': resolved,
                    'type': 'group' if is_group else 'user',
                    'last_msg_time': last_time,
                    'msg_count': cnt,
                    'avatar_url': f'/api/avatar/{uname}',
                }
            conn.close()
        except sqlite3.Error:
            continue

    result = sorted(contacts.values(), key=lambda c: c['last_msg_time'] or 0, reverse=True)
    _save_slow_cache(decrypted_dir, wxid, result)
    return result


def _enrich_contacts_chatroom_names(decrypted_dir: str, contacts: list) -> None:
    """Replace raw @chatroom IDs with names from chat_room.ext_buffer."""
    chatroom_names = _load_chatroom_names(decrypted_dir)
    for c in contacts:
        cid = c.get('id', '')
        if cid.endswith('@chatroom') and c.get('name', '') == cid:
            name = chatroom_names.get(cid) if chatroom_names else None
            if name:
                c['name'] = name
            else:
                c['name'] = chatroom_fallback_name(cid)


def _find_file(decrypted_dir: str, *rel_paths: str) -> str:
    """Find a file by trying multiple relative paths, with a shallow walk fallback."""
    for rel in rel_paths:
        path = os.path.join(decrypted_dir, rel.replace('/', os.sep))
        if os.path.isfile(path):
            return path
    # Walk one level deep as fallback
    target_name = os.path.basename(rel_paths[0])
    try:
        for entry in os.scandir(decrypted_dir):
            if entry.is_dir():
                candidate = os.path.join(entry.path, target_name)
                if os.path.isfile(candidate):
                    return candidate
            elif entry.is_file() and entry.name == target_name:
                return entry.path
    except OSError:
        pass
    return os.path.join(decrypted_dir, rel_paths[0].replace('/', os.sep))


def _find_msg_dbs(msg_dir: str) -> list:
    dbs = []
    for f in os.listdir(msg_dir):
        m = re.match(r'message_(\d+)\.db', f)
        if m:
            dbs.append((int(m.group(1)), os.path.join(msg_dir, f)))
    dbs.sort(key=lambda x: x[0])
    return dbs


def _load_from_chats_db(decrypted_dir: str, wxid: str = None) -> list:
    """Load contact list from pre-built data/chats.db (fast path).

    Returns None if chats.db is missing or empty, signaling caller to fall
    back to the slow full-scan path.
    """
    summary_db = os.path.join(decrypted_dir, 'data', 'chats.db')
    if not os.path.isfile(summary_db):
        # Try one level up (for alternate backup layouts)
        alt = os.path.join(decrypted_dir, 'chats.db')
        if os.path.isfile(alt):
            summary_db = alt
        else:
            return None
    try:
        conn = sqlite3.connect(summary_db)
        rows = conn.execute(
            "SELECT chat_id, display_name, message_count, last_msg_time, is_group "
            "FROM chats ORDER BY last_msg_time DESC"
        ).fetchall()
        conn.close()
        if not rows:
            return None
        contacts = []
        own_forms = _own_wxid_forms(wxid)
        for chat_id, display_name, msg_count, last_time, is_group in rows:
            if chat_id in own_forms:
                continue
            contacts.append({
                'id': chat_id,
                'name': display_name or chat_id,
                'type': 'group' if is_group else 'user',
                'last_msg_time': last_time,
                'msg_count': msg_count or 0,
                'avatar_url': f'/api/avatar/{chat_id}',
            })
        return contacts
    except sqlite3.Error:
        return None


def _build_hash_map(msg_dbs: list) -> dict:
    hmap = {}
    for idx, db_path in msg_dbs:
        try:
            conn = sqlite3.connect(db_path)
            for (uname,) in conn.execute("SELECT user_name FROM Name2Id"):
                if uname:
                    hmap[hashlib.md5(uname.encode()).hexdigest()] = uname
            conn.close()
        except sqlite3.Error:
            pass
    return hmap


def _load_contacts(contact_db: str) -> tuple:
    """Load contact.db into id→name, name→id, and name→avatar maps (mirrors chat_list.py)."""
    id_to_name = {}
    name_to_id = {}
    name_to_avatar = {}
    if not os.path.isfile(contact_db):
        return id_to_name, name_to_id, name_to_avatar
    try:
        conn = sqlite3.connect(contact_db)
        for r in conn.execute(
            "SELECT id, username, remark, nick_name, alias, small_head_url FROM contact"
        ):
            cid, uname, remark, nick, alias, avatar_url = r
            uname = (uname or "").strip()
            display = pick_display_name(uname, remark, nick, alias, uname)
            if cid and display:
                id_to_name[cid] = display
            if uname:
                name_to_id[uname] = cid
                if avatar_url:
                    name_to_avatar[uname] = str(avatar_url).strip()
        conn.close()
    except sqlite3.Error:
        pass
    return id_to_name, name_to_id, name_to_avatar


def _load_sessions(session_db: str) -> dict:
    """Load session.db SessionTable summaries."""
    summaries = {}
    if not os.path.isfile(session_db):
        return summaries
    try:
        conn = sqlite3.connect(session_db)
        for r in conn.execute("SELECT username, summary FROM SessionTable"):
            uname, summary = r
            if uname and summary:
                summaries[uname] = str(summary).strip()
        conn.close()
    except sqlite3.Error:
        pass
    return summaries


def _load_room_owners(contact_db: str) -> dict:
    """Load chat_room owner field from contact.db."""
    owners = {}
    if not os.path.isfile(contact_db):
        return owners
    try:
        conn = sqlite3.connect(contact_db)
        for r in conn.execute("SELECT username, owner FROM chat_room"):
            uname, owner = r
            if uname and owner:
                owners[uname] = str(owner).strip()
        conn.close()
    except sqlite3.Error:
        pass
    return owners


def _build_group_members(msg_dbs: list, hash_to_name: dict,
                         name_to_id: dict, id_to_name: dict) -> dict:
    """Build chatroom → [display_name, ...] map from message sender prefixes.

    Samples up to 200 text messages per table to extract unique sender wxids,
    then maps them to contact display names.
    """
    members = {}
    for idx, db_path in msg_dbs:
        try:
            conn = sqlite3.connect(db_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (tname,) in tables:
                h = tname[4:]
                uname = hash_to_name.get(h)
                if not uname or not uname.endswith('@chatroom'):
                    continue
                if uname in members:
                    continue  # already collected from a richer DB

                rows = conn.execute(
                    f"SELECT message_content FROM [{tname}] "
                    f"WHERE message_content LIKE '%:' || char(10) || '%' LIMIT 200"
                ).fetchall()
                seen = set()
                names = []
                for (content,) in rows:
                    if isinstance(content, bytes):
                        content = decompress_content(content)
                    if isinstance(content, bytes):
                        try:
                            content = content.decode('utf-8', errors='replace')
                        except Exception:
                            continue
                    if not isinstance(content, str) or ':\n' not in content[:100]:
                        continue
                    raw = content.split(':\n', 1)[0].strip()
                    m = re.search(
                        r'(?:wxid_[a-z0-9]{10,20}'
                        r'|[a-zA-Z][a-zA-Z0-9_]{3,30}'
                        r'|[0-9]{5,20}@openim'
                        r'|[0-9]{5,20})',
                        raw
                    )
                    if not m:
                        continue
                    wxid = m.group(0)
                    if wxid in seen:
                        continue
                    seen.add(wxid)
                    # Resolve to display name
                    name = wxid
                    if wxid in name_to_id:
                        cid = name_to_id[wxid]
                        if cid in id_to_name:
                            name = id_to_name[cid]
                    names.append(name)
                if names:
                    members[uname] = names
            conn.close()
        except sqlite3.Error:
            continue
    return members


def get_group_members(decrypted_dir: str, chatroom_id: str) -> list:
    """Return member list with display names for a group chat.
    Returns [{wxid, display_name, is_owner}, ...].
    """
    msg_dir = os.path.join(decrypted_dir, "message")
    if os.path.isdir(msg_dir):
        msg_dbs = _find_msg_dbs(msg_dir)
    else:
        msg_dbs = []
        try:
            for f in os.listdir(decrypted_dir):
                m = re.match(r'message_(\d+)\.db', f, re.IGNORECASE)
                if m:
                    msg_dbs.append((int(m.group(1)), os.path.join(decrypted_dir, f)))
        except OSError:
            pass
    if not msg_dbs:
        return []

    contact_db = _find_file(decrypted_dir, "contact/contact.db", "contact.db")
    id_to_name, name_to_id, _ = _load_contacts(contact_db)
    room_owners = _load_room_owners(contact_db)
    hash_to_name = _build_hash_map(msg_dbs)
    members_map = _build_group_members(msg_dbs, hash_to_name, name_to_id, id_to_name)
    member_names = members_map.get(chatroom_id, [])

    owner_wxid = room_owners.get(chatroom_id, '')
    owner_display = ''
    if owner_wxid and owner_wxid in name_to_id:
        cid = name_to_id[owner_wxid]
        owner_display = id_to_name.get(cid, owner_wxid)
    else:
        owner_display = owner_wxid

    result = []
    seen = set()
    for name in member_names:
        wxid = None
        for w, cid in name_to_id.items():
            if id_to_name.get(cid) == name:
                wxid = w
                break
        if not wxid:
            wxid = name
        if wxid in seen:
            continue
        seen.add(wxid)
        result.append({
            'wxid': wxid,
            'display_name': name,
            'is_owner': (wxid == owner_wxid),
        })

    # Add owner if not already in list
    if owner_wxid and owner_wxid not in seen:
        result.insert(0, {
            'wxid': owner_wxid,
            'display_name': owner_display or owner_wxid,
            'is_owner': True,
        })

    return result


def get_group_info(decrypted_dir: str, chat_id: str) -> dict:
    """Return group info including members, owner, notice."""
    if not chat_id.endswith('@chatroom'):
        return None

    # SessionTable.summary field is "群名:最新消息预览" not the actual
    # announcement. Real columns (announcement_/xml_announcement_) are in
    # a different table not available in this schema.
    notice = ''

    members = get_group_members(decrypted_dir, chat_id)
    owner = next((m['display_name'] for m in members if m['is_owner']), '')

    return {
        'chat_id': chat_id,
        'member_count': len(members),
        'owner': owner,
        'notice': notice,
        'members': members,
    }


def _extract_group_member_names(decrypted_dir: str, chat_id: str,
                                 name_to_id: dict, id_to_name: dict,
                                 max_names: int = 4) -> list:
    """Extract a few member display names for a group from message content.

    Efficiently samples recent text messages from the group's Msg_ table,
    decompresses zstd content, extracts sender wxids from ``wxid:\\n``
    prefix, and resolves them to display names.

    Returns a list of display name strings (up to max_names).
    """
    h = hashlib.md5(chat_id.encode()).hexdigest()
    tname = f"Msg_{h}"

    msg_dir = os.path.join(decrypted_dir, "message")
    if not os.path.isdir(msg_dir):
        return []

    for f in sorted(os.listdir(msg_dir)):
        if not (f.startswith('message_') and f.endswith('.db')):
            continue
        db_path = os.path.join(msg_dir, f)
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tname,)
            ).fetchone()
            if not row:
                conn.close()
                continue

            rows = conn.execute(
                f"SELECT message_content FROM [{tname}] "
                f"WHERE message_content IS NOT NULL "
                f"ORDER BY create_time DESC LIMIT 50"
            ).fetchall()
            conn.close()

            seen = set()
            names = []
            for (content,) in rows:
                if isinstance(content, bytes):
                    content = decompress_content(content)
                if isinstance(content, bytes):
                    try:
                        content = content.decode('utf-8', errors='replace')
                    except Exception:
                        continue
                if not isinstance(content, str):
                    continue
                if ':\n' not in content[:100]:
                    continue
                raw = content.split(':\n', 1)[0].strip()
                m = re.search(
                    r'(?:wxid_[a-z0-9]{10,20}'
                    r'|[a-zA-Z][a-zA-Z0-9_]{3,30}'
                    r'|[0-9]{5,20}@openim'
                    r'|[0-9]{5,20})',
                    raw
                )
                if not m:
                    continue
                wxid = m.group(0)
                if wxid in seen:
                    continue
                seen.add(wxid)
                # Resolve to display name
                name = wxid
                if wxid in name_to_id:
                    cid = name_to_id[wxid]
                    if cid in id_to_name:
                        name = id_to_name[cid]
                names.append(name)
                if len(names) >= max_names:
                    break

            return names
        except sqlite3.Error:
            continue

    return []


def _resolve_display(uname: str, is_group: bool, decrypted_dir: str,
                     id_to_name: dict, name_to_id: dict,
                     session_summaries: dict, room_owners: dict,
                     group_members: dict = None) -> str:
    """Resolve display name for a chat username (mirrors chat_list._resolve_display)."""
    # Contact lookup via name_resolver (exact match + LIKE fuzzy, cached)
    name = resolve_wxid(decrypted_dir, uname)
    if name and name != uname:
        return name

    if not is_group:
        # Check pre-built id_to_name dict (populated by _load_contacts from contact.db).
        # resolve_wxid queries by username= which can miss contacts where the lookup
        # key is an alias or nick_name — the dicts cover all contacts regardless of column.
        if uname in name_to_id:
            cid = name_to_id[uname]
            if cid in id_to_name:
                name = id_to_name[cid]
                if name and name != uname:
                    return name
        return uname

    # --- Group chat resolution below ---

    # 1. Try contact.db with @chatroom stripped (some DBs store without suffix)
    if uname.endswith('@chatroom'):
        base_grp = uname[:-9]
        if base_grp in name_to_id:
            cid = name_to_id[base_grp]
            if cid in id_to_name:
                name = id_to_name[cid]
                if name and name != base_grp:
                    return name

    # 2. Session summary: "GroupName:last_message" format
    if uname in session_summaries:
        summary = session_summaries[uname]
        for sep in [":", "："]:
            if sep in summary:
                name_part = summary.split(sep, 1)[0].strip()
                if name_part and len(name_part) < 60:
                    return name_part
        # No colon — if summary is short enough to be a group name, use it
        if summary and len(summary) < 40 and not any(c in summary for c in '\n\r'):
            return summary.strip()

    # 3. Member-based name: join display names with '、'
    # Only when caller passes a group_members dict (signals intent to do
    # expensive DB scans). API endpoints that list many groups at once
    # skip this to avoid scanning message databases per group.
    member_names = group_members.get(uname) if group_members else None
    if member_names is None and is_group and group_members is not None:
        member_names = _extract_group_member_names(decrypted_dir, uname, name_to_id, id_to_name)
        group_members[uname] = member_names  # cache for later
    if member_names:
        joined = '、'.join(member_names)
        if len(joined) <= _GROUP_NAME_MAX_LEN:
            return joined
        truncated = joined[:_GROUP_NAME_MAX_LEN]
        while truncated and truncated[-1] in ('、', '​', '‍'):
            truncated = truncated[:-1]
        return truncated + '..'

    # 4. Room owner from chat_room table
    if uname in room_owners:
        owner = room_owners[uname]
        if owner in name_to_id:
            cid = name_to_id[owner]
            if cid in id_to_name:
                owner_name = id_to_name[cid]
                return f"{owner_name}的群聊"
        return f"{owner}的群聊"

    # 5. Fallback: shortened chatroom ID
    short = uname[:12] + "..." if len(uname) > 12 else uname
    return f"群聊({short})"
