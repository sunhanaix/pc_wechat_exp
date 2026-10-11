"""Avatar API — serve user and group avatar images."""
import os
import sys
import sqlite3
import hashlib as _hashlib
import requests
from xml.sax.saxutils import escape as _xml_escape
from flask import Blueprint, Response, current_app, request

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

avatar_bp = Blueprint('avatar', __name__, url_prefix='/api')

_AVATAR_PALETTE = [
    ('#e94560', '#f0883e'), ('#569cd6', '#56d364'),
    ('#ffd54f', '#e94560'), ('#c084fc', '#569cd6'),
]


@avatar_bp.route('/avatar/<username>')
def avatar(username):
    """Serve avatar image — CDN proxy → local cache → SVG default. Never 404."""
    decrypted_dir = current_app.config.get('DECRYPTED_DIR', '')
    ext, body = _fetch_avatar(decrypted_dir, username)
    # issue #27 深入②：加 ETag + 强缓存 —— 首屏几十张头像在浏览器里各要 1.1s+（服务端仅 0.05s，
    # 成本在排队），带上缓存后重复进入/切会话/翻页不再重新下载。
    etag = '"' + _hashlib.md5(body if isinstance(body, bytes) else bytes(body)).hexdigest() + '"'
    inm = (request.headers.get('If-None-Match') or '').strip()
    if inm and etag in [p.strip() for p in inm.split(',')]:
        resp = Response(status=304, mimetype='image/png')
    elif ext == 'svg':
        resp = Response(body, mimetype='image/svg+xml')
    else:
        resp = Response(body, mimetype=f'image/{ext}' if ext != 'jpg' else 'image/jpeg')
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


def _get_avatar_url_from_contact(decrypted_dir: str, username: str) -> str:
    """Look up small_head_url for a user from contact.db."""
    contact_db = os.path.join(decrypted_dir, 'contact', 'contact.db')
    if not os.path.isfile(contact_db):
        contact_db = os.path.join(decrypted_dir, 'Contact', 'contact.db')
    if not os.path.isfile(contact_db):
        # Walk one level deep
        try:
            for entry in os.scandir(decrypted_dir):
                if entry.is_dir():
                    c = os.path.join(entry.path, 'contact.db')
                    if os.path.isfile(c):
                        contact_db = c
                        break
        except OSError:
            pass
    if not os.path.isfile(contact_db):
        return None
    conn = None
    try:
        conn = sqlite3.connect(contact_db)
        row = conn.execute(
            "SELECT small_head_url FROM contact WHERE username=?",
            (username,)
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
        # Exact match on alias column (wxid may be stored as alias, not username)
        row = conn.execute(
            "SELECT small_head_url FROM contact WHERE alias=?",
            (username,)
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
        # Try fuzzy: strip @chatroom suffix and search
        base = username
        is_group = base.endswith('@chatroom')
        if is_group:
            base = base[:-9]
        escaped = base.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        if is_group:
            row = conn.execute(
                "SELECT small_head_url FROM contact WHERE username LIKE ? ESCAPE '\\' AND username LIKE '%@chatroom'",
                (f'%{escaped}%',)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT small_head_url FROM contact WHERE username LIKE ? ESCAPE '\\'",
                (f'%{escaped}%',)
            ).fetchone()
            # Username LIKE may miss contacts where the wxid is stored as alias;
            # try alias column as well.
            if not row or not row[0]:
                row = conn.execute(
                    "SELECT small_head_url FROM contact WHERE alias LIKE ? ESCAPE '\\'",
                    (f'%{escaped}%',)
                ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
    except sqlite3.Error:
        pass
    finally:
        if conn:
            conn.close()
    return None


def _fetch_avatar(decrypted_dir: str, username: str) -> tuple:
    """Return (ext, bytes) for avatar. ext is 'jpg'|'png'|'svg'."""
    is_group = username.endswith('@chatroom')

    # 1. Try CDN proxy from contact.db small_head_url
    url = _get_avatar_url_from_contact(decrypted_dir, username)
    if url:
        cache_dir = os.path.join(os.path.dirname(decrypted_dir), 'avatar_cache')
        os.makedirs(cache_dir, exist_ok=True)
        cache_key = __hashlib.md5(username.encode()).hexdigest()
        # Check both jpg and png cache variants
        for ext in ('jpg', 'png'):
            cache_path = os.path.join(cache_dir, cache_key + '.' + ext)
            if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 100:
                with open(cache_path, 'rb') as f:
                    return (ext, f.read())
        try:
            r = requests.get(url, timeout=8, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            if r.status_code == 200 and len(r.content) > 100:
                ct = r.headers.get('Content-Type', 'image/jpeg')
                ext = 'png' if 'png' in ct else 'jpg'
                cache_path = os.path.join(cache_dir, cache_key + '.' + ext)
                tmp_path = cache_path + '.tmp'
                with open(tmp_path, 'wb') as f:
                    f.write(r.content)
                os.replace(tmp_path, cache_path)
                return (ext, r.content)
        except requests.RequestException:
            pass

    # 2. Group avatar: composite of first 4 members
    if is_group:
        group_svg = _make_group_avatar_svg(decrypted_dir, username)
        if group_svg:
            return ('svg', group_svg.encode('utf-8'))

    # 3. Default SVG with first char + gradient
    display = _get_display_for_avatar(decrypted_dir, username)
    ch = _first_char(display or username)
    svg = _make_default_avatar_svg(username, ch)
    return ('svg', svg.encode('utf-8'))


def _first_char(s: str) -> str:
    """Extract first meaningful character (CJK, letter, or digit)."""
    for c in str(s):
        if c.isalpha() or c.isdigit() or '一' <= c <= '鿿' or '぀' <= c <= 'ヿ':
            return c
    return '?'


def _make_default_avatar_svg(username: str, ch: str) -> str:
    """Generate a deterministic SVG avatar with gradient background."""
    h = __hashlib.md5(username.encode()).digest()
    hue = ((h[0] << 8 | h[1]) * 137.508) % 360
    c1 = f'hsl({hue:.0f}, 70%, 45%)'
    c2 = f'hsl({(hue + 40) % 360:.0f}, 70%, 55%)'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0%" stop-color="{c1}"/><stop offset="100%" stop-color="{c2}"/>
</linearGradient></defs>
<rect width="128" height="128" rx="64" fill="url(#g)"/>
<text x="64" y="86" text-anchor="middle" fill="white" font-size="60"
 font-family="Microsoft YaHei,PingFang SC,sans-serif">{_xml_escape(ch)}</text>
</svg>'''


def _make_group_avatar_svg(decrypted_dir: str, chatroom_id: str) -> str:
    """Composite 2x2 grid SVG from first 4 members' initials."""
    from engine.services.chat import get_group_members
    members = get_group_members(decrypted_dir, chatroom_id)[:4]
    if not members:
        return None
    colors = _AVATAR_PALETTE
    cells = []
    for i in range(4):
        if i < len(members):
            name = members[i].get('display_name', '') or members[i].get('wxid', '?')
            ch = _first_char(name)
            c1, c2 = colors[i % len(colors)]
        else:
            ch = ''
            c1 = c2 = '#30363d'
        x = 2 + (i % 2) * 62
        y = 2 + (i // 2) * 62
        cells.append(f'<rect x="{x}" y="{y}" width="60" height="60" rx="8" fill="{c1}"/>')
        if ch:
            cells.append(f'<text x="{x + 30}" y="{y + 41}" text-anchor="middle" fill="white" font-size="28" font-family="Microsoft YaHei,PingFang SC,sans-serif">{_xml_escape(ch)}</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">{"".join(cells)}</svg>'


def _get_display_for_avatar(decrypted_dir: str, username: str) -> str:
    """Get a display name for the avatar initial character."""
    contact_db = os.path.join(decrypted_dir, 'contact', 'contact.db')
    if not os.path.isfile(contact_db):
        contact_db = os.path.join(decrypted_dir, 'Contact', 'contact.db')
    if os.path.isfile(contact_db):
        try:
            conn = sqlite3.connect(contact_db)
            row = conn.execute(
                "SELECT COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), NULLIF(alias,''), username) FROM contact WHERE username=?",
                (username,)
            ).fetchone()
            # Username may not match — try alias column
            if not row:
                row = conn.execute(
                    "SELECT COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), NULLIF(alias,''), username) FROM contact WHERE alias=?",
                    (username,)
                ).fetchone()
            conn.close()
            if row and row[0]:
                return row[0].strip()
        except sqlite3.Error:
            pass
    return username
