"""Unified wxid -> display_name resolution.

Merge of:
- message.py:_lookup_wxid() + _pick_display_name()
- chat.py:_pick_display_name() (duplicate)
- message.py:_resolve_sender_name() logic
"""
import logging
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict

_name_cache = OrderedDict()
_name_cache_lock = threading.Lock()
_NAME_CACHE_MAX = 200
_NAME_CACHE_TTL = 300  # seconds


def pick_display_name(wxid: str, remark, nick, alias, db_username) -> str:
    """Pick best display name, skipping fields that just echo the wxid/username."""
    remark_v = (remark or '').strip()
    nick_v = (nick or '').strip()
    alias_v = (alias or '').strip()
    uname = (db_username or wxid or '').strip()

    def _ok(v):
        """Skip values containing replacement characters (garbled encoding)."""
        return v and '�' not in v and v != uname

    if _ok(remark_v):
        return remark_v
    if _ok(nick_v):
        return nick_v
    if _ok(alias_v):
        return alias_v
    return ''


def extract_chatroom_name(ext_buffer: bytes) -> str | None:
    """Extract group display name from chat_room.ext_buffer protobuf blob.

    The first readable non-wxid ASCII string (>=3 chars) in the blob is the
    display name WeChat shows for unnamed groups. Returns None when no
    usable name is found.
    """
    if not ext_buffer or len(ext_buffer) < 4:
        return None
    i = 0
    while i < len(ext_buffer):
        if ext_buffer[i] < 0x20 or ext_buffer[i] > 0x7e:
            i += 1
            continue
        start = i
        while i < len(ext_buffer) and 0x20 <= ext_buffer[i] <= 0x7e:
            i += 1
        s = ext_buffer[start:i].decode('ascii', errors='replace')
        if len(s) >= 3 and not s.startswith('wxid_') and not s.startswith('gh_'):
            return s
    return None


_chatroom_names_cache = {}          # decrypted_dir -> (指纹, 映射)


def invalidate_names(decrypted_dir: str):
    """Invalidate cached names after a live contact snapshot is committed."""
    prefix = str(decrypted_dir) + ':'
    with _name_cache_lock:
        for key in list(_name_cache):
            if key.startswith(prefix):
                del _name_cache[key]
        _chatroom_names_cache.pop(os.path.abspath(decrypted_dir), None)


def _dir_fingerprint(decrypted_dir: str) -> str:
    """数据目录指纹：各子目录下 *.db 的 (目录/名称, 大小, mtime)。任一变化即失效。"""
    parts = []
    try:
        bases = [decrypted_dir]
        for name in os.listdir(decrypted_dir):
            p = os.path.join(decrypted_dir, name)
            if os.path.isdir(p):
                bases.append(p)
        for base in bases:
            try:
                for f in sorted(os.listdir(base)):
                    if f.lower().endswith('.db'):
                        st = os.stat(os.path.join(base, f))
                        parts.append('%s/%s:%d:%d' % (os.path.basename(base), f,
                                                      st.st_size, int(st.st_mtime)))
            except OSError:
                pass
    except OSError:
        return ''
    return '|'.join(parts)


def _load_chatroom_names(decrypted_dir: str) -> dict:
    """群名映射（带缓存）。实测解析一次约 0.4s，而 /api/contacts 每次请求都要用它。

    按数据目录指纹缓存；指纹变化（重新解密/新备份）自动失效。
    注意：**返回的是缓存对象，调用方只能读、不得修改**。
    """
    key = os.path.abspath(decrypted_dir)
    fp = _dir_fingerprint(decrypted_dir)
    hit = _chatroom_names_cache.get(key)
    if fp and hit and hit[0] == fp:
        return hit[1]
    data = _load_chatroom_names_uncached(decrypted_dir)
    if fp:
        _chatroom_names_cache[key] = (fp, data)
    return data


def _load_chatroom_names_uncached(decrypted_dir: str) -> dict:
    """Load {chatroom_id: display_name} from chat_room table in contact.db.

    Strategy:
    1. Try extract_chatroom_name(ext_buffer) — direct group name
    2. Fallback: extract member wxids from ext_buffer, resolve each through
       contact.db, concat up to 3 member names (WeChat's own behavior for
       unnamed groups).
    3. Last resort: generate 群聊(shortID) for any @chatroom still unresolved
       (empty contact fields + tiny ext_buffer with no member data).
    """
    result = {}
    contact_db = _find_contact_db(decrypted_dir)
    if not contact_db:
        return result
    try:
        conn = sqlite3.connect(contact_db)

        # Pre-load contact names for member resolution fallback
        contact_names = {}
        alias_to_name = {}
        for r in conn.execute(
            "SELECT username, remark, nick_name, alias FROM contact"
        ):
            uname, remark, nick, alias_v = r
            uname = (uname or '').strip()
            if uname:
                name = pick_display_name(uname, remark, nick, alias_v, uname)
                if name:
                    contact_names[uname] = name
            alias_v = (alias_v or '').strip()
            if alias_v and alias_v not in contact_names:
                alias_to_name[alias_v] = uname

        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_room'"
        ).fetchone()
        if has_table:
            for r in conn.execute("SELECT username, ext_buffer FROM chat_room"):
                uname, ext_buf = r
                if not uname or not uname.endswith('@chatroom'):
                    continue
                # Prefer member-based naming (like WeChat itself for unnamed groups).
                # extract_chatroom_name is too permissive — it picks up member
                # aliases (e.g. "ziyi919", "sunhaoas") as "group names".
                name = _resolve_from_members(ext_buf, contact_names, alias_to_name)
                if not name:
                    name = extract_chatroom_name(ext_buf)
                if name:
                    result[uname] = name

        # Last-resort fallback: any @chatroom still unresolved gets 群聊(shortID)
        for r in conn.execute(
            "SELECT username FROM contact WHERE username LIKE '%@chatroom'"
        ):
            uname = (r[0] or '').strip()
            if uname and uname not in result:
                base = uname[:-9] if uname.endswith('@chatroom') else uname
                short = base[:12] + '..' if len(base) > 12 else base
                result[uname] = f'群聊({short})'

        conn.close()
    except sqlite3.Error:
        pass
    return result


def chatroom_fallback_name(wxid: str) -> str:
    """Generate a readable fallback name for a @chatroom with no other name."""
    base = wxid[:-9] if wxid.endswith('@chatroom') else wxid
    short = base[:12] + '..' if len(base) > 12 else base
    return f'群聊({short})'


def _resolve_from_members(ext_buffer: bytes, contact_names: dict,
                          alias_to_name: dict = None) -> str | None:
    """Build group display name from member display names in ext_buffer.

    Extracts both wxid_xxx strings and non-wxid aliases from the protobuf blob,
    resolves each through contact.db name maps, and joins up to 3 names with '、'.
    """
    if not ext_buffer:
        return None
    import re as _re

    # Extract all candidate strings from the protobuf blob (wxids + aliases)
    candidates = []
    wxids = _re.findall(rb'wxid_[a-z0-9]+', ext_buffer)
    candidates.extend(wxid_bytes.decode('ascii') for wxid_bytes in wxids)

    # Also find non-wxid, non-gh_ aliases (>=3 chars ASCII) that look like member IDs
    i = 0
    while i < len(ext_buffer):
        if ext_buffer[i] < 0x20 or ext_buffer[i] > 0x7e:
            i += 1
            continue
        start = i
        while i < len(ext_buffer) and 0x20 <= ext_buffer[i] <= 0x7e:
            i += 1
        s = ext_buffer[start:i].decode('ascii', errors='replace')
        if len(s) >= 3 and not s.startswith('wxid_') and not s.startswith('gh_'):
            candidates.append(s)

    seen = set()
    names = []
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        # Try direct wxid lookup first, then alias lookup
        name = contact_names.get(key)
        if not name and alias_to_name:
            uname = alias_to_name.get(key)
            if uname:
                name = contact_names.get(uname)
        if name:
            names.append(name)
            if len(names) >= 3:
                break
    if names:
        return '、'.join(names)
    return None


def resolve_wxid(decrypted_dir: str, wxid: str) -> str:
    """Look up a wxid in contact.db, returning display name or the wxid itself.

    Priority: remark > nick_name > alias > username.
    Falls back to LIKE-fuzzy matching when exact username match fails.
    Results are cached per-process (max 200 entries).
    """
    if not wxid:
        return wxid

    cache_key = f'{decrypted_dir}:{wxid}'
    now = time.monotonic()
    with _name_cache_lock:
        if cache_key in _name_cache:
            result, ts = _name_cache[cache_key]
            if now - ts < _NAME_CACHE_TTL:
                _name_cache.move_to_end(cache_key)
                return result
            # expired — remove and fall through to fresh lookup
            del _name_cache[cache_key]

    contact_db = _find_contact_db(decrypted_dir)
    if not contact_db:
        return wxid

    result = wxid
    try:
        with sqlite3.connect(contact_db) as conn:
            row = conn.execute(
                "SELECT remark, nick_name, alias, username FROM contact WHERE username=?",
                (wxid,)
            ).fetchone()
            if row:
                name = pick_display_name(wxid, row[0], row[1], row[2], row[3])
                if name and name != wxid:
                    result = name
                # WeChat 4.x: the same wxid can appear both with and without an
                # account suffix (e.g. wxid_xxx vs wxid_xxx_10e8). The unsuffixed
                # row often lacks a remark, while the suffixed one carries it.
                # When the exact match yielded a nick/alias (not remark), check
                # for a suffixed variant with a remark.
                remark = (row[0] or '').strip()
                if not remark:
                    suffixed = conn.execute(
                        "SELECT remark, nick_name, alias, username FROM contact "
                        "WHERE username LIKE ? ESCAPE '\\'"
                        "  AND remark IS NOT NULL AND remark != ''"
                        "  AND remark != username"
                        " LIMIT 1",
                        (wxid.replace('_', '\\_') + '\\_%',)
                    ).fetchone()
                    if suffixed:
                        name2 = pick_display_name(wxid, suffixed[0], suffixed[1],
                                                  suffixed[2], suffixed[3])
                        if name2 and name2 != wxid:
                            result = name2
            else:
                # Exact match on alias (the wxid may be stored as alias, not username)
                row = conn.execute(
                    "SELECT remark, nick_name, alias, username FROM contact WHERE alias=?",
                    (wxid,)
                ).fetchone()
                if row:
                    name = pick_display_name(wxid, row[0], row[1], row[2], row[3])
                    if name and name != wxid:
                        result = name

            if result == wxid:
                # LIKE-fuzzy fallback — try username then alias
                base = wxid
                for sfx in ('@chatroom', '@openim'):
                    if base.endswith(sfx):
                        base = base[:-len(sfx)]
                        break
                if base and len(base) >= 4:
                    for col in ('username', 'alias'):
                        row = conn.execute(
                            f"SELECT remark, nick_name, alias, username FROM contact "
                            f"WHERE {col} LIKE ? LIMIT 1",
                            (f'%{base}%',)
                        ).fetchone()
                        if row:
                            name = pick_display_name(wxid, row[0], row[1], row[2], row[3])
                            if name and name != wxid:
                                result = name
                                break
    except sqlite3.Error:
        logging.warning("name_resolver: failed to query %s for wxid=%s", contact_db, wxid)

    with _name_cache_lock:
        while len(_name_cache) >= _NAME_CACHE_MAX:
            _name_cache.popitem(last=False)
        _name_cache[cache_key] = (result, time.monotonic())

    return result


def _find_contact_db(decrypted_dir: str) -> str:
    """Find contact.db under decrypted_dir."""
    for name in ('contact/contact.db', 'Contact/contact.db'):
        path = os.path.join(decrypted_dir, name.replace('/', os.sep))
        if os.path.isfile(path):
            return path
    return None
