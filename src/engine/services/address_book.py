"""Address book: read all contacts from contact.db with display names and message stats.

Fast path: reads pre-computed contacts table from data/chats.db (built during backup).
Slow path: falls back to direct contact.db scan if the index is missing or empty.
"""
import os
import re
import sqlite3

from engine.services.name_resolver import pick_display_name, _find_contact_db, _load_chatroom_names, chatroom_fallback_name
from engine.services.contact_extra import load_extra_map, load_labels, resolve_labels

# Columns we always want from contact.db (core identity fields)
_CORE_CONTACT_COLS = ['username', 'remark', 'nick_name', 'alias']

# Extra columns that may exist in some WeChat versions — discovered dynamically
_KNOWN_EXTRA_COLS = [
    'description', 'sex', 'country', 'province', 'city',
    'signature', 'small_head_url', 'big_head_url', 'contactType',
]

# wxid patterns that indicate a phone-number-based account
_PHONE_WXID_RE = re.compile(r'^\+(\d{1,3})(\d{7,14})$')


def _phone_from_wxid(wxid: str) -> str:
    """If wxid looks like a phone number, return formatted version."""
    m = _PHONE_WXID_RE.match(wxid or '')
    if m:
        return f'+{m.group(1)} {m.group(2)}'
    return ''


def _discover_contact_columns(decrypted_dir: str) -> list:
    """Return list of column names present in the contact table of contact.db."""
    contact_db = _find_contact_db(decrypted_dir)
    if not contact_db or not os.path.isfile(contact_db):
        return list(_CORE_CONTACT_COLS)
    try:
        conn = sqlite3.connect(contact_db)
        rows = conn.execute("PRAGMA table_info(contact)").fetchall()
        conn.close()
        return [r[1] for r in rows]  # r[1] = column name
    except sqlite3.Error:
        return list(_CORE_CONTACT_COLS)


def _build_contact_select(available_cols: list) -> tuple:
    """Build a SELECT clause and return (col_string, col_list)."""
    cols = list(_CORE_CONTACT_COLS)
    for c in _KNOWN_EXTRA_COLS:
        if c in available_cols and c not in cols:
            cols.append(c)
    return ', '.join(cols), cols


def _parse_contact_row(col_names: list, row: tuple) -> dict:
    """Convert a row (matched to col_names) into a contact dict."""
    d = dict(zip(col_names, row))
    wxid = (d.get('username') or '').strip()
    remark = (d.get('remark') or '').strip()
    nick = (d.get('nick_name') or '').strip()
    alias = (d.get('alias') or '').strip()

    display = pick_display_name(wxid, remark, nick, alias, wxid) or wxid
    is_group = wxid.endswith('@chatroom')

    contact = {
        'wxid': wxid,
        'display_name': display,
        'remark': remark,
        'nick_name': nick,
        'alias': alias,
        'avatar_url': f'/api/avatar/{wxid}',
        'msg_count': 0,
        'last_msg_time': None,
        'is_group': is_group,
    }

    # Phone detection from wxid（兜底；真实号码由 extra_buffer 覆盖，见 _attach_extra）
    phone = _phone_from_wxid(wxid)
    if phone:
        contact['phone'] = phone
        contact['phone_source'] = 'wxid'

    # Extra fields
    for c in _KNOWN_EXTRA_COLS:
        val = d.get(c)
        if val is not None and str(val).strip():
            s = str(val).strip()
            if c == 'sex':
                try:
                    contact['sex'] = int(s)
                except (ValueError, TypeError):
                    pass
            else:
                contact[c] = s

    return contact


def _find_chats_db(decrypted_dir: str) -> str:
    """Find chats.db in decrypted_dir."""
    for p in (os.path.join(decrypted_dir, 'data', 'chats.db'),
              os.path.join(decrypted_dir, 'chats.db')):
        if os.path.isfile(p):
            return p
    return None


def _load_extra(decrypted_dir: str) -> tuple:
    """Return (extra_by_wxid, labels_map) parsed from contact.db's extra_buffer.

    Contact phone/sex/signature/region/tags are NOT columns of the contact table —
    they live inside its ``extra_buffer`` protobuf blob. Static data, cached.
    """
    cache_key = f'_extra_cache_{decrypted_dir}'
    cached = _EXTRA_CACHE.get(cache_key)
    if cached is not None:
        return cached
    contact_db = _find_contact_db(decrypted_dir)
    result = (load_extra_map(contact_db), load_labels(contact_db))
    _EXTRA_CACHE[cache_key] = result
    return result


def _attach_extra(decrypted_dir: str, contacts: list) -> None:
    """Merge extra_buffer fields into contact dicts, in place.

    Needed on BOTH load paths: the fast path reads the project's own
    data/chats.db index, which has no extra_buffer column at all, so it must
    fall back to contact.db to get phone numbers and tags.
    """
    extra_map, labels_map = _load_extra(decrypted_dir)
    for c in contacts:
        parsed = extra_map.get(c.get('wxid') or '') or {}
        label_ids = parsed.get('label_ids') or []
        c['labels'] = resolve_labels(label_ids, labels_map)
        if label_ids:
            c['label_ids'] = label_ids
        if 'sex' in parsed:
            c['sex'] = parsed['sex']
        for field in ('signature', 'country', 'province', 'city'):
            if parsed.get(field):
                c[field] = parsed[field]
        if parsed.get('phone'):
            # 真实号码优先于"从 wxid 猜出来的号码"
            c['phone'] = parsed['phone']
            c['phone_source'] = ('signature' if parsed.get('phone_from_signature')
                                 else 'contact_db')


def _load_from_contacts_index(decrypted_dir: str) -> list:
    """Fast path: read pre-computed contacts from chats.db contacts table.

    Returns None if the table doesn't exist or is empty, signaling fallback.
    """
    chats_db = _find_chats_db(decrypted_dir)
    if not chats_db:
        return None
    try:
        conn = sqlite3.connect(chats_db)
        # Check if contacts table exists
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='contacts'"
        ).fetchone()
        if not exists:
            conn.close()
            return None
        # Dynamically read contacts table columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
        col_str = ', '.join(cols)
        rows = conn.execute(f"SELECT {col_str} FROM contacts").fetchall()
        if not rows:
            conn.close()
            return None
        contacts = {}
        for row in rows:
            d = dict(zip(cols, row))
            wxid = (d.get('wxid') or d.get('username') or '').strip()
            if not wxid:
                continue
            contact = {
                'wxid': wxid,
                'display_name': d.get('display_name') or wxid,
                'remark': d.get('remark') or '',
                'nick_name': d.get('nick_name') or '',
                'alias': d.get('alias') or '',
                'avatar_url': f'/api/avatar/{wxid}',
                'msg_count': 0,
                'last_msg_time': None,
                'is_group': bool(d.get('is_group', 0)),
            }
            # Phone detection（兜底；真实号码由 extra_buffer 覆盖）
            phone = _phone_from_wxid(wxid)
            if phone:
                contact['phone'] = phone
                contact['phone_source'] = 'wxid'
            # Copy extra fields
            for c in _KNOWN_EXTRA_COLS:
                val = d.get(c)
                if val is not None and str(val).strip():
                    s = str(val).strip()
                    if c == 'sex':
                        try:
                            contact['sex'] = int(s)
                        except (ValueError, TypeError):
                            pass
                    else:
                        contact[c] = s
            contacts[wxid] = contact
        # Enrich with message stats from chats table
        for r in conn.execute(
            "SELECT chat_id, message_count, last_msg_time FROM chats"
        ):
            chat_id, msg_count, last_time = r
            if chat_id in contacts:
                contacts[chat_id]['msg_count'] = msg_count or 0
                contacts[chat_id]['last_msg_time'] = last_time
        conn.close()
        return sorted(contacts.values(), key=lambda c: (
            c['display_name'] or c['wxid']
        ).lower())
    except sqlite3.Error:
        return None


def _load_from_contact_db(decrypted_dir: str) -> list:
    """Slow path: scan contact.db directly when chats.db index is unavailable."""
    contact_db = _find_contact_db(decrypted_dir)
    if not contact_db or not os.path.isfile(contact_db):
        return []

    available_cols = _discover_contact_columns(decrypted_dir)
    select_str, select_cols = _build_contact_select(available_cols)

    contacts = {}
    try:
        conn = sqlite3.connect(contact_db)
        for row in conn.execute(f"SELECT {select_str} FROM contact"):
            contact = _parse_contact_row(select_cols, row)
            wxid = contact['wxid']
            if not wxid:
                continue
            contacts[wxid] = contact
        conn.close()
    except sqlite3.Error:
        return []

    _attach_chat_stats(decrypted_dir, contacts)
    return sorted(contacts.values(), key=lambda c: (
        c['display_name'] or c['wxid']
    ).lower())


def _enrich_with_chatroom_names(decrypted_dir: str, contacts: list) -> None:
    """Replace raw @chatroom IDs with display names from chat_room table."""
    chatroom_names = _load_chatroom_names(decrypted_dir)
    for c in contacts:
        wxid = c.get('wxid', '')
        if wxid.endswith('@chatroom') and c.get('display_name') == wxid:
            name = chatroom_names.get(wxid) if chatroom_names else None
            if name:
                c['display_name'] = name
            else:
                c['display_name'] = chatroom_fallback_name(wxid)


def get_all_contacts(decrypted_dir: str) -> list:
    """Return all contacts with display names and optional chat stats.

    Fast path: reads pre-computed contacts table from chats.db (instant).
    Slow path: scans contact.db directly when index is missing.

    Result is cached in memory — contact data is static during a session.
    """
    cache_key = f'_contacts_cache_{decrypted_dir}'
    if cache_key in _ALL_CONTACTS_CACHE:
        return _ALL_CONTACTS_CACHE[cache_key]
    contacts = _load_from_contacts_index(decrypted_dir)
    if contacts is None:
        contacts = _load_from_contact_db(decrypted_dir)
    if contacts:
        _attach_extra(decrypted_dir, contacts)
        _enrich_with_chatroom_names(decrypted_dir, contacts)
    _ALL_CONTACTS_CACHE[cache_key] = contacts
    return contacts


_ALL_CONTACTS_CACHE = {}
_EXTRA_CACHE = {}


def _attach_chat_stats(decrypted_dir: str, contacts: dict) -> None:
    """Enrich contacts dict with msg_count and last_msg_time from chats.db."""
    chats_db = _find_chats_db(decrypted_dir)
    if not chats_db:
        return
    try:
        conn = sqlite3.connect(chats_db)
        rows = conn.execute(
            "SELECT chat_id, message_count, last_msg_time FROM chats"
        ).fetchall()
        conn.close()
        for chat_id, msg_count, last_time in rows:
            if chat_id in contacts:
                contacts[chat_id]['msg_count'] = msg_count or 0
                contacts[chat_id]['last_msg_time'] = last_time
    except sqlite3.Error:
        pass


def filter_contacts(contacts: list, q: str = '', sort: str = 'name',
                    has_chat=None, letter: str = '', kind: str = 'all',
                    label: str = None) -> list:
    """Filter + sort a contact list for the address book UI and the exporters.

    Extraction of the logic that used to live inline in
    ``/api/address-book``, so the exported file always matches what the user
    sees on screen. Semantics are kept identical to the old inline version —
    the searchable fields are display_name / remark / nick_name / alias /
    wxid / phone / description (signature and region are deliberately *not*
    searched, as before).

    Args:
        q        — search keyword (case-insensitive)
        sort     — 'name' (default), 'msg_count', 'last_time'
        has_chat — '1' only contacts with messages, '0' only without, else all
        letter   — first letter of display_name
        kind     — 'all' (default), 'contacts' (exclude groups),
                   'groups' (groups only)
        label    — WeChat label (tag) name, matched case-insensitively against
                   the contact's ``labels`` list

    Returns a NEW list; the input (which may be the shared get_all_contacts()
    cache) is never mutated — the old inline sort reordered that cache and
    leaked the ordering into later requests.
    """
    out = list(contacts)

    if kind == 'groups':
        out = [c for c in out if c.get('is_group')]
    elif kind == 'contacts':
        out = [c for c in out if not c.get('is_group')]

    q = (q or '').strip().lower()
    if q:
        def _match(c):
            for field in ('display_name', 'remark', 'nick_name', 'alias', 'wxid'):
                if q in (c.get(field) or '').lower():
                    return True
            if c.get('phone') and q in c['phone']:
                return True
            return bool(c.get('description') and q in c['description'].lower())
        out = [c for c in out if _match(c)]

    if has_chat == '1':
        out = [c for c in out if (c.get('msg_count') or 0) > 0]
    elif has_chat == '0':
        out = [c for c in out if not (c.get('msg_count') or 0)]

    label = (label or '').strip().lower()
    if label:
        out = [c for c in out
               if any(label == (name or '').lower()
                      for name in (c.get('labels') or []))]

    letter = (letter or '').strip().upper()
    if letter:
        out = [c for c in out
               if ((c.get('display_name') or c.get('wxid') or '')[:1].upper() == letter)]

    if sort == 'msg_count':
        out.sort(key=lambda c: c.get('msg_count') or 0, reverse=True)
    elif sort == 'last_time':
        out.sort(key=lambda c: c.get('last_msg_time') or 0, reverse=True)
    else:
        # 显式按显示名排序：旧实现依赖 get_all_contacts() 已排好序而直接跳过，
        # 导出路径传入的列表未必有序。排序键与 get_all_contacts() 保持一致。
        out.sort(key=lambda c: (c.get('display_name') or c.get('wxid') or '').lower())

    return out


def distinct_labels(contacts: list) -> list:
    """Sorted unique label names present in the given contacts.

    Used by the Web UI to build the label filter dropdown, so it only ever
    offers labels that actually exist in the data.
    """
    seen = set()
    for c in contacts:
        for name in (c.get('labels') or []):
            if name:
                seen.add(name)
    return sorted(seen)


def get_all_groups(decrypted_dir: str) -> list:
    """Return all group chats with pre-computed display names.

    Fast path: reads from chats.db contacts table (is_group=1), pre-computed
              during backup indexing.
    Slow path: reads chat_room from contact.db directly.
    """
    # Fast path: use pre-computed contacts index
    all_contacts = _load_from_contacts_index(decrypted_dir)
    if all_contacts is not None:
        groups = [c for c in all_contacts if c['is_group']]
        _attach_extra(decrypted_dir, groups)   # 索引表没有 extra_buffer，需回源
        return groups

    # Slow path: direct contact.db scan
    contact_db = _find_contact_db(decrypted_dir)
    if not contact_db or not os.path.isfile(contact_db):
        return []

    groups = []
    try:
        conn = sqlite3.connect(contact_db)
        for r in conn.execute("SELECT username, owner FROM chat_room"):
            uname, owner = r
            uname = (uname or '').strip()
            if not uname:
                continue
            groups.append({
                'wxid': uname,
                'display_name': uname,
                'owner': (owner or '').strip(),
                'avatar_url': f'/api/avatar/{uname}',
            })
        conn.close()
    except sqlite3.Error:
        pass
    return groups
