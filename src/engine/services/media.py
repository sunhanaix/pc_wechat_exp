"""Media file resolution and serving."""
import hashlib as _hashlib
import os
import re
import sqlite3
import mimetypes
import struct
import subprocess
import sys
from flask import abort, send_file

STORAGE_CANDIDATES = [
    'MsgAttach', 'FileStorage', 'Image', 'Video',
    'FileStorage/MsgAttach', 'FileStorage/Image',
]

# Common WeChat .dat file XOR keys
_DAT_XOR_KEYS = [0xC9, 0x37, 0x96, 0x6A, 0xFF]

# WeChat .dat encryption version signatures
# V1: \x07\x08V1\x08\x07 — AES-128-ECB with fixed key (MD5 of '0')
# V2: \x07\x08V2\x08\x07 — AES-128-ECB with dynamic app-specific key
_DAT_V1_HEADER = b'\x07\x08\x56\x31\x08\x07'
_DAT_V2_HEADER = b'\x07\x08\x56\x32\x08\x07'
_DAT_V2_DEFAULT_XOR = 0xC9  # empirically confirmed: 17325+ files use this
# V1 fixed AES key: MD5 of '0'
_DAT_V1_AES_KEY = bytes.fromhex('cfcd208495d565ef')

# Cache of image AES keys collected from type 3 XML messages
_image_aes_keys = {}

# 已知的微信数据根目录（**兜底**用）。
# 优先用 hardlink.db 的 db_info 推出的真实根；这里只是"配置文件里什么都没有"时的最后一招。
# 提成模块常量而不是内联字面量，是为了：① 单测可以注入，避免测试去探测本机真实微信目录；
# ② 让"哪些路径会被探测"这件事在一处可见（issue #16 之后新增的纪律）。
_FALLBACK_STORAGE_ROOTS = (
    r'D:\xwechat_files', r'C:\xwechat_files',
    r'D:\WeChat Files', r'C:\WeChat Files',
)

# 单个存储根下最多探测多少个账号目录（防止异常目录把一次请求拖死）。
_MAX_ACCOUNT_DIRS = 32


def _detect_wxid(decrypted_dir: str) -> str:
    """Auto-detect the WeChat user wxid from the storage directory.

    Tries: db_info uuid, scanning xwechat_files for matching subdir,
    checking contact DB for own username.
    """
    # Strategy 1: From hardlink.db db_info -> uuid -> storage root
    hardlink_db = os.path.join(decrypted_dir, "hardlink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        hardlink_db = os.path.join(decrypted_dir, "HardLink", "hardlink.db")
    if os.path.isfile(hardlink_db):
        conn = None
        try:
            conn = sqlite3.connect(hardlink_db)
            row = conn.execute(
                "SELECT ValueStdStr FROM db_info WHERE Key='uuid'"
            ).fetchone()
            conn.close()
            conn = None
            if row and row[0]:
                parts = str(row[0]).split('_', 2)
                if len(parts) >= 3:
                    storage_root = parts[-1]
                    if os.path.isdir(storage_root):
                        # 账号目录名**不一定**带 wxid_ 前缀，且可能有多个账号（issue #16）：
                        # 先挑有 db_storage 的，再退到 wxid_ 前缀，最后退到任意真实目录。
                        picked = _pick_account_dir(storage_root)
                        if picked:
                            return picked
        except (sqlite3.Error, OSError):
            pass
        finally:
            if conn:
                conn.close()

    # Strategy 2: Scan known storage locations
    for storage_root in _FALLBACK_STORAGE_ROOTS:
        try:
            if os.path.isdir(storage_root):
                picked = _pick_account_dir(storage_root)
                if picked:
                    return picked
        except OSError:
            continue

    return os.path.basename(os.path.dirname(decrypted_dir))


def _get_base_storage(decrypted_dir: str) -> str:
    """Get the original WeChat file storage root from hardlink.db's db_info table."""
    hardlink_db = os.path.join(decrypted_dir, "hardlink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        hardlink_db = os.path.join(decrypted_dir, "HardLink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        return None
    conn = None
    try:
        conn = sqlite3.connect(hardlink_db)
        row = conn.execute(
            "SELECT ValueStdStr FROM db_info WHERE Key='uuid'"
        ).fetchone()
        conn.close()
        conn = None
        if row and row[0]:
            parts = str(row[0]).split('_', 2)
            if len(parts) >= 3:
                storage_path = parts[-1]
                if os.path.isdir(storage_path):
                    return storage_path
    except sqlite3.Error:
        pass
    finally:
        if conn:
            conn.close()
    return None


def _storage_roots(decrypted_dir: str) -> list:
    """本次要探测的存储根：由 db_info 推出的**真实根**优先，其后是已知兜底根。"""
    roots = []
    base = _get_base_storage(decrypted_dir)
    if base:
        roots.append(base)
    for sr in _FALLBACK_STORAGE_ROOTS:
        if os.path.isdir(sr) and sr not in roots:
            roots.append(sr)
    return roots


def _account_dirs_under(storage_root: str) -> list:
    """列出 storage_root 下**可能**是账号目录的真实子目录（排序后，结果稳定）。

    issue #16：微信 4.x 的账号目录名有时就是 wxid、有时是 wxid+随机后缀、
    **有时完全不带 ``wxid_`` 前缀**，而且同一台机器上可能同时存在多个账号。
    所以这里**不按名字筛**，只要求它是真实目录 —— "到底是哪一个"交给调用方用
    "文件是否真的存在"来判定，而不是靠名字猜。
    """
    try:
        names = sorted(os.listdir(storage_root))
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith('.'):
            continue
        if os.path.isdir(os.path.join(storage_root, n)):
            out.append(n)
            if len(out) >= _MAX_ACCOUNT_DIRS:
                break
    return out


def _pick_account_dir(storage_root: str):
    """在存储根下挑一个最像账号的目录：**有 db_storage 的优先**，其次 ``wxid_`` 前缀，其次任意目录。"""
    dirs = _account_dirs_under(storage_root)
    for d in dirs:
        if os.path.isdir(os.path.join(storage_root, d, 'db_storage')):
            return d
    for d in dirs:
        if d.startswith('wxid_'):
            return d
    return dirs[0] if dirs else None


def resolve_account_dir(decrypted_dir: str, wxid: str = None):
    """返回**校验过的**账号目录名；没有任何可信取值时返回 ``None``（issue #16 根因修法）。

    规则：
      * 传入的 ``wxid`` 只有**确实是某个存储根下的真实目录**时才被采用；
      * 否则改用 :func:`_detect_wxid` 的检测结果（同样要求是真实目录）；
      * 两者都不可信 ⇒ 返回 ``None``。

    返回规则里最后一条**很重要**：校验失败时**不要**把调用方给的值抹成 ``None`` ——
    我们只是"证明不了它对"，并没有"证明它错"。抹成 ``None`` 会让 `own_wxid` 这类功能
    从"可能错"变成"必然缺"（并会连带弄坏大量用合成账号名的测试）。
    只有当**确实找到了一个校验通过的真实目录**时，才替换调用方给的值。
    """
    wxid = (wxid or '').strip()
    roots = _storage_roots(decrypted_dir)
    if wxid and any(os.path.isdir(os.path.join(r, wxid)) for r in roots):
        return wxid
    detected = _detect_wxid(decrypted_dir)
    if detected and any(os.path.isdir(os.path.join(r, detected)) for r in roots):
        return detected
    return wxid or None


def _resolve_hardlink_path(decrypted_dir: str, media_info: dict, wxid: str = None) -> str:
    """Resolve a HardLink-based media reference to an absolute filesystem path.

    media_info contains: md5, local_path (relative to wxid dir), media_type (3/43/6/34).
    local_path format per type:
      - Image (3):  msg/attach/{dir1_hash}/{dir2_date}/Img/{file_name}
      - Video (43): msg/video/{dir1_date}/{file_name}
      - File (6):   msg/file/{dir1_date}/{file_name}
    """
    if not media_info:
        return None

    if media_info.get('media_type') == 3 and media_info.get('quality') == 'original':
        # WeChat's _h file is the HD image; the bare file can be a smaller
        # wxgf version. Resolve HD explicitly before the ordinary fallback chain.
        full_info = dict(media_info)
        full_info.pop('quality', None)
        full_info['md5'] = re.sub(r'_[th]$', '', full_info.get('md5', ''))
        for field in ('local_path', 'file_name'):
            full_info[field] = re.sub(r'_[th](?=\.[^./\\]+$)', '',
                                      full_info.get(field, '') or '')
        if not full_info['local_path'] and full_info['md5']:
            db_path = _resolve_from_hardlink_db(decrypted_dir, full_info['md5'], 3)
            full_info['local_path'] = re.sub(r'_[th](?=\.[^./\\]+$)', '', db_path or '')
        hd_info = dict(full_info)
        hd_info['md5'] = full_info['md5'] + '_h' if full_info['md5'] else ''
        for field in ('local_path', 'file_name'):
            value = full_info[field]
            if value:
                base, ext = os.path.splitext(value)
                hd_info[field] = base + '_h' + ext
        if not hd_info['file_name'] and full_info['md5']:
            hd_info['file_name'] = full_info['md5'] + '_h.dat'
        return (_resolve_hardlink_path(decrypted_dir, hd_info, wxid)
                or _resolve_hardlink_path(decrypted_dir, full_info, wxid))

    local_path = media_info.get('local_path')
    md5 = media_info.get('md5', '')
    media_type = media_info.get('media_type', 0)

    # ⚠️ 这里**不再**用 `wxid or os.path.basename(os.path.dirname(decrypted_dir))`：
    # 对 `<...>\backup\2026-09-21` 这类布局，那个回退值就是 `'backup'`/`'output'`（备份目录名），
    # **必然错**，而错一个名字就会让所有媒体 0 命中且不报错（issue #16 实测：传对 5/12，传错 0/12）。
    # 现在改成：给的名字优先，其后枚举存储根下**真实存在**的目录，胜负由"文件是否真的存在"决定。
    storage_roots = _storage_roots(decrypted_dir)
    _account_cache = {}

    def _account_candidates(root):
        """候选账号目录名（缓存：一次解析里会被多个候选相对路径复用）。"""
        if root in _account_cache:
            return _account_cache[root]
        names = []
        if wxid:
            names.append(wxid)
        for d in _account_dirs_under(root):
            if d not in names:
                names.append(d)
        names.append('')   # 有些布局里"账号目录"就等于存储根本身
        _account_cache[root] = names
        return names

    # Helper: try all combinations of base + wxid + path
    def _try_paths(rel_path):
        if not rel_path:
            return None
        rel_path = rel_path.replace('/', os.sep)
        for root in storage_roots:
            for wd in _account_candidates(root):
                candidate = os.path.join(root, wd, rel_path)
                try:
                    real = os.path.realpath(candidate)
                except (OSError, ValueError):
                    continue
                if not os.path.isfile(real):
                    continue
                # Containment check — prevent path traversal (e.g. ?path=..\..\Windows\...)
                expected_parent = os.path.realpath(os.path.join(root, wd))
                try:
                    if os.path.commonpath([real, expected_parent]) != expected_parent:
                        continue
                except ValueError:
                    continue
                return real
        return None

    if local_path:
        result = _try_paths(local_path)
        if result:
            return result

    # Fallback: look up md5 in HardLink DB and construct path
    if md5 and (len(md5) == 32 or
                (media_type == 3 and len(re.sub(r'_[th]$', '', md5)) == 32)):
        result = _resolve_from_hardlink_db(decrypted_dir, md5, media_type)
        print(f"  [RESOLVE] bare md5={md5[:16]}... hldb_result={result}")
        if result:
            abs_path = _try_paths(result)
            if abs_path:
                return abs_path
            # _try_paths failed — try backup media/ directory
            hldb_fname = os.path.basename(result)
            candidate = _try_backup_media_dir(decrypted_dir, media_type, hldb_fname)
            if candidate:
                return candidate

    # Fallback: search backup media/ directory by file_name from media_info
    # After backup, media files are migrated to {decrypted_dir}/media/{category}/
    # but _try_paths only searches original WeChat storage.
    file_name = media_info.get('file_name', '')
    if not file_name and local_path:
        file_name = os.path.basename(local_path)
    candidate = _try_backup_media_dir(decrypted_dir, media_type, file_name)
    if candidate:
        return candidate

    # Fallback: md5-based search in backup media directory
    # When file_name is missing (e.g. forwarded videos), try common extensions
    if md5 and len(md5) == 32:
        candidate = _try_backup_media_by_md5(decrypted_dir, media_type, md5)
        if candidate:
            return candidate

    # Final fallback: when bare md5/path (no _t/_h suffix) doesn't resolve,
    # try thumbnail variants, then directory scan. Full-size image may exist
    # on disk without a HardLink DB entry.
    if md5 and len(md5) == 32 and not (md5.endswith('_t') or md5.endswith('_h')):
        _fallback_file = None  # thumbnail to use if directory scan finds nothing
        _fallback_dir = None   # directory to scan for full-size file
        for suffix in ('_h', '_t'):  # _h first (higher quality thumbnail)
            variant_md5 = md5 + suffix
            # Try HardLink DB
            variant_hl_path = _resolve_from_hardlink_db(decrypted_dir, variant_md5, media_type)
            if variant_hl_path:
                v_dir, v_fname = os.path.split(variant_hl_path)
                v_name, v_ext = os.path.splitext(v_fname)
                # Try bare (full-size) file first
                if v_name.endswith(suffix):
                    bare_fname = v_name[:-len(suffix)] + v_ext
                    bare_hl_path = os.path.join(v_dir, bare_fname) if v_dir else bare_fname
                    abs_path = _try_paths(bare_hl_path)
                    if abs_path:
                        return abs_path
                # Check if variant (thumbnail) file exists — remember as fallback
                abs_path = _try_paths(variant_hl_path)
                if abs_path and _fallback_file is None:
                    _fallback_file = abs_path
                    _fallback_dir = v_dir
                # Try backup media by variant filename
                hldb_fname = os.path.basename(variant_hl_path)
                candidate = _try_backup_media_dir(decrypted_dir, media_type, hldb_fname)
                if candidate:
                    return candidate
                if _fallback_dir is None:
                    _fallback_dir = v_dir
            # Try backup media by variant md5
            if _fallback_file is None:
                candidate = _try_backup_media_by_md5(decrypted_dir, media_type, variant_md5)
                if candidate:
                    _fallback_file = candidate
        # Try _t/_h suffix on the local_path filename itself
        if local_path and _fallback_file is None:
            dir_part, fname = os.path.split(local_path)
            name, ext = os.path.splitext(fname)
            if not (name.endswith('_t') or name.endswith('_h')):
                for suffix in ('_h', '_t'):
                    variant_path = os.path.join(dir_part, name + suffix + ext) if dir_part else name + suffix + ext
                    result = _try_paths(variant_path)
                    if result:
                        _fallback_file = result
                        break
        # Directory scan: look for files matching the bare md5 prefix in the
        # same directory as the thumbnail. The original may exist without a
        # HardLink DB entry (e.g. WeChat indexed only the thumbnail).
        if _fallback_dir:
            wxid_val = wxid or os.path.basename(os.path.dirname(decrypted_dir))
            roots = [_get_base_storage(decrypted_dir)] if _get_base_storage(decrypted_dir) else []
            for sr in ['D:\\xwechat_files', 'C:\\xwechat_files',
                       'D:\\WeChat Files', 'C:\\WeChat Files']:
                if os.path.isdir(sr) and sr not in roots:
                    roots.append(sr)
            for root in roots:
                abs_dir = os.path.join(root, wxid_val, _fallback_dir)
                if os.path.isdir(abs_dir):
                    try:
                        for fname in os.listdir(abs_dir):
                            if fname.startswith(md5) and not (
                                fname.endswith('_t.dat') or fname.endswith('_h.dat')):
                                candidate = os.path.join(abs_dir, fname)
                                if os.path.isfile(candidate):
                                    print(f"  [RESOLVE] dir scan found: {fname}")
                                    return candidate
                    except OSError:
                        pass
        # Return thumbnail as last resort
        if _fallback_file:
            return _fallback_file

    return None


def _try_backup_media_dir(decrypted_dir: str, media_type: int, file_name: str) -> str:
    """Try to find a media file in the backup's flat media/ directory.

    After backup, `migrate_media` copies files to:
      {decrypted_dir}/media/images/  (type 3)
      {decrypted_dir}/media/videos/  (type 43)
      {decrypted_dir}/media/files/   (type 6)
      {decrypted_dir}/media/voice/   (type 34)
    """
    if not file_name:
        return None
    cat_map = {3: 'images', 43: 'videos', 6: 'files', 34: 'voice', 49: 'files'}
    category = cat_map.get(media_type, '')
    if not category:
        return None
    candidate = os.path.join(decrypted_dir, 'media', category, file_name)
    if os.path.isfile(candidate):
        return os.path.realpath(candidate)
    return None


def _try_backup_media_by_md5(decrypted_dir: str, media_type: int, md5: str) -> str:
    """Find a media file in the backup media/ dir by md5 with common extensions."""
    cat_map = {3: 'images', 43: 'videos', 6: 'files', 34: 'voice', 49: 'files'}
    category = cat_map.get(media_type, '')
    if not category or len(md5) < 8:
        return None
    base_dir = os.path.join(decrypted_dir, 'media', category)
    if not os.path.isdir(base_dir):
        return None
    # Common extensions per type
    exts_map = {3: ['.dat', '.jpg', '.png', '.gif', '.webp', '_h.dat', '_t.dat'],
                43: ['.mp4', '.mov', '.avi', '.dat'],
                6: ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar', ''],
                34: ['.silk', '.wav', '.amr', '.mp3']}
    exts = exts_map.get(media_type, ['.dat'])
    for ext in exts:
        fname = md5 + ext
        candidate = os.path.join(base_dir, fname)
        if os.path.isfile(candidate):
            return os.path.realpath(candidate)
    # Broader search: any file in the category dir starting with this md5
    md5_lower = md5.lower()
    try:
        for fname in os.listdir(base_dir):
            if fname.lower().startswith(md5_lower):
                return os.path.realpath(os.path.join(base_dir, fname))
    except OSError:
        pass
    return None


def _hardlink_rows_by_keys(conn, table_name: str, md5: str) -> list:
    """按 hardlink DB 的**两套 md5** 查行：先 `md5` 列（CDN md5），再 `file_name` 前缀（文件 md5）。

    issue #19（作者定位准确）：`<x>_hardlink_info_v4` 里

      * `md5` 列   = **CDN 资源 md5**
      * `file_name` = `<文件 md5>.dat`（本地文件名）

    而消息 XML / `packed_info_data` 里**两种形态都会出现**。旧代码只查 `WHERE md5=?`，
    对"带文件 md5"的消息 **100% miss**（控制方本机实测：60/60 条图片消息请求的 md5
    只命中 `file_name`、不命中 `md5` 列）⇒ 只能回落到 `MessageResourceInfo` 的
    local_id 兜底，而那条兜底在本机 60 条里 **9 条（15%）取到的是别的图** ——
    这就是"显示成另一张图 / 尺寸明显不对"的来源。

    返回按"原图优先、缩略图 `_h`/`_t` 靠后"排序的行列表（与旧行为一致）；
    每行是 ``(file_name, file_size, dir1, dir2)``。
    ⚠️ 有的 HardLink 库**没有 `file_size` 列**（版本差异 / 测试夹具）⇒ 按实际列自适应，
    缺列时用 0 占位，**绝不因此抛异常**（否则整条定位链会静默返回 None）。
    """
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info([%s])' % table_name)}
    except sqlite3.Error:
        return []
    if not cols:
        return []
    sel = ('file_name, file_size, dir1, dir2' if 'file_size' in cols
           else 'file_name, 0, dir1, dir2')
    order = ("ORDER BY CASE WHEN substr(file_name, -6)='_h.dat' THEN 2 "
             "WHEN substr(file_name, -6)='_t.dat' THEN 3 ELSE 1 END")
    rows = conn.execute(
        f"SELECT {sel} FROM [{table_name}] WHERE md5=? {order}",
        (md5,)
    ).fetchall()
    if rows:
        return rows
    # 文件 md5 形态（issue #19）：file_name 以请求的 md5 开头
    return conn.execute(
        f"SELECT {sel} FROM [{table_name}] WHERE file_name LIKE ? {order}",
        (md5 + '%',)
    ).fetchall()


def _resolve_from_hardlink_db(decrypted_dir: str, md5: str, media_type: int) -> str:
    """Look up a file in the HardLink DB by md5 and return its relative path.

    `md5` 既可以是 CDN md5（`md5` 列），也可以是文件 md5（`file_name` 前缀）——
    两种都查，见 :func:`_hardlink_rows_by_keys`（issue #19）。
    """
    hardlink_db = os.path.join(decrypted_dir, "hardlink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        hardlink_db = os.path.join(decrypted_dir, "HardLink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        return None

    table_map = {3: 'image', 43: 'video', 6: 'file', 34: 'voice'}
    table_suffix = table_map.get(media_type, 'image')
    table_name = f'{table_suffix}_hardlink_info_v4'

    conn = None
    try:
        conn = sqlite3.connect(hardlink_db)
        rows = _hardlink_rows_by_keys(conn, table_name, md5)
        if not rows:
            return None

        file_name, _file_size, dir1, dir2 = rows[0]
        dir1_name = None
        dir2_name = None
        if dir2:
            d2 = conn.execute("SELECT * FROM dir2id WHERE rowid=?", (dir2,)).fetchone()
            dir2_name = d2[0] if d2 else None
        if dir1:
            d1 = conn.execute("SELECT * FROM dir2id WHERE rowid=?", (dir1,)).fetchone()
            dir1_name = d1[0] if d1 else None

        if media_type == 3:  # Image
            if dir1_name and dir2_name:
                return f'msg/attach/{dir1_name}/{dir2_name}/Img/{file_name}'
        elif media_type == 43:  # Video
            if dir1_name:
                return f'msg/video/{dir1_name}/{file_name}'
        elif media_type == 6:  # File
            if dir1_name:
                return f'msg/file/{dir1_name}/{file_name}'

        return None
    except sqlite3.Error:
        return None
    finally:
        if conn:
            conn.close()


def resolve_media_path(db_dir: str, file_path: str) -> str:
    """Resolve a WeChat file reference to an absolute filesystem path."""
    if not file_path or not db_dir:
        return None

    if file_path.startswith('http://') or file_path.startswith('https://'):
        return None

    cleaned = file_path
    for prefix in ['THUMBNAIL_DIRPATH://', 'FILEID://', 'big/']:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]

    wxid_dir = os.path.dirname(db_dir)
    allowed_roots = [db_dir, wxid_dir]
    allowed_roots = [os.path.realpath(r) for r in allowed_roots if r]

    def _safe_join_try(base, rel):
        trial = os.path.realpath(os.path.join(base, rel))
        if not os.path.isfile(trial):
            return None
        for root in allowed_roots:
            try:
                if os.path.commonpath([trial, root]) == root:
                    return trial
            except ValueError:
                pass
        return None

    if os.path.isabs(cleaned):
        for root in allowed_roots:
            result = _safe_join_try(root, os.path.basename(cleaned))
            if result:
                return result

    for candidate in STORAGE_CANDIDATES:
        for base in [db_dir, wxid_dir]:
            result = _safe_join_try(base, os.path.join(candidate, cleaned))
            if result:
                return result

    result = _safe_join_try(wxid_dir, cleaned)
    if result:
        return result

    return None


def serve_media(db_dir: str, file_path: str):
    """Flask response: serve a media file using traditional path resolution."""
    resolved = resolve_media_path(db_dir, file_path)
    if resolved is None or not os.path.isfile(resolved):
        abort(404)
    mime, _ = mimetypes.guess_type(resolved)
    return send_file(resolved, mimetype=mime or 'application/octet-stream',
                     max_age=86400)


# Common WeChat .dat file XOR keys
_DAT_XOR_KEYS = [0xC9, 0x37, 0x96, 0x6A, 0xFF]


def _detect_dat_xor_key(file_path: str) -> tuple:
    """Try to detect the XOR key for an encrypted .dat file.

    Returns (key, file_ext) if found, or (None, None) if the file is not
    XOR-encrypted or uses an unknown key.
    """
    MAGICS = [
        # (magic_bytes, ext, mime, min_match_len)
        (b'\xff\xd8\xff', 'jpg', 'image/jpeg', 3),
        (b'\x89PNG\r\n\x1a\n', 'png', 'image/png', 4),
        (b'GIF89a', 'gif', 'image/gif', 4),
        (b'GIF87a', 'gif', 'image/gif', 4),
        (b'RIFF', 'webp', 'image/webp', 4),
    ]
    try:
        fsize = os.path.getsize(file_path)
        with open(file_path, 'rb') as f:
            data = f.read(16)
        if len(data) < 4:
            return None, None
        # Try known keys first
        for key in _DAT_XOR_KEYS:
            dec = bytes(b ^ key for b in data)
            for magic, ext, _, min_len in MAGICS:
                if dec[:min_len] == magic[:min_len]:
                    return key, ext
        # Auto-detect: try all 256 possible keys (only for strong matches)
        for key in range(256):
            if key in _DAT_XOR_KEYS:
                continue
            dec = bytes(b ^ key for b in data)
            for magic, ext, _, min_len in MAGICS:
                if dec[:min_len] == magic[:min_len]:
                    # For JPEG, also verify the next marker byte
                    if ext == 'jpg' and dec[3] not in (0xe0, 0xe1, 0xe2, 0xdb, 0xc4, 0xc0):
                        continue
                    return key, ext
    except OSError:
        pass
    return None, None


def _decrypt_dat_file(src_path: str, xor_key: int, output_dir: str) -> str:
    """Decrypt a .dat file using XOR and cache the result.

    Returns the path to the decrypted file.
    """
    import hashlib
    src_hash = hashlib.md5(src_path.encode()).hexdigest()[:12]
    out_name = f'{src_hash}.dec'
    out_path = os.path.join(output_dir, out_name)
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    try:
        with open(src_path, 'rb') as f:
            data = f.read()
        dec = bytes(b ^ xor_key for b in data)
        os.makedirs(output_dir, exist_ok=True)
        with open(out_path, 'wb') as f:
            f.write(dec)
        return out_path
    except OSError:
        return None


def _detect_wechat_dat_version(data: bytes) -> int:
    """Detect WeChat .dat image encryption version from file header.

    V0: No version signature — pure XOR encryption (classic WeChat)
    V1: \\x07\\x08V1\\x08\\x07 — AES-128-ECB with fixed key cfcd208495d565ef
    V2: \\x07\\x08V2\\x08\\x07 — AES-128-ECB with dynamic app-specific key
    Returns 0, 1, or 2.
    """
    if len(data) < 6:
        return 0
    if data[:6] == _DAT_V2_HEADER:
        return 2
    if data[:6] == _DAT_V1_HEADER:
        return 1
    # Short match: just the first 4 bytes (some variants)
    if data[:4] == b'\x07\x08\x56\x32':
        return 2
    if data[:4] == b'\x07\x08\x56\x31':
        return 1
    return 0


def _decrypt_dat_v1(file_path: str, output_dir: str) -> str:
    """Decrypt a V1 .dat file using fixed AES key + XOR.

    V1 format: 6-byte header + AES-ECB body + XOR tail.
    Returns path to decrypted file, or None.
    """
    try:
        from Crypto.Cipher import AES
    except ImportError:
        return None

    try:
        with open(file_path, 'rb') as f:
            data = f.read()
    except OSError:
        return None

    if len(data) < 22:
        return None

    # Try to find XOR key from the last bytes
    # V1 files have an XOR-encrypted tail after the AES portion
    # The XOR key is typically the last byte XOR magic_byte
    xor_key = None
    for key in _DAT_XOR_KEYS:
        # Try decrypting the last few bytes with this key
        test = bytes(b ^ key for b in data[-16:])
        # Check for JPEG/PNG end markers
        if test[-2:] == b'\xff\xd9' or b'IEND' in test:
            xor_key = key
            break

    if xor_key is None:
        return None

    # Decrypt: AES-128-ECB from byte 6 to (end - xor_tail_len)
    # The exact split between AES and XOR is encoded in the header after the 6-byte signature
    try:
        aes_size = struct.unpack_from('<I', data, 6)[0]
        xor_size = struct.unpack_from('<I', data, 10)[0]
    except struct.error:
        return None

    body_start = 15  # 6 (sig) + 4 (aes_size) + 4 (xor_size) + 1 (padding)
    if body_start + aes_size > len(data):
        return None

    aes_data = data[body_start:body_start + aes_size]
    raw_start = body_start + aes_size
    xor_data = data[raw_start:]

    # Decrypt AES portion
    try:
        cipher = AES.new(_DAT_V1_AES_KEY, AES.MODE_ECB)
        dec_aes = cipher.decrypt(aes_data)
        # Remove PKCS7 padding
        pad = dec_aes[-1]
        if 0 < pad <= 16:
            dec_aes = dec_aes[:-pad]
    except Exception:
        return None

    # Decrypt XOR portion (if any)
    dec_xor = bytes(b ^ xor_key for b in xor_data) if xor_data else b''

    result = dec_aes + dec_xor

    import hashlib
    src_hash = hashlib.md5(file_path.encode()).hexdigest()[:12]
    out_path = os.path.join(output_dir, f'{src_hash}_v1.dec')
    try:
        os.makedirs(output_dir, exist_ok=True)
        with open(out_path, 'wb') as f:
            f.write(result)
        return out_path
    except OSError:
        return None


_ACCOUNT_KEYS_CACHE: dict = {}  # wxid -> {'xor': int, 'aes': bytes}


# --- Per-image V2 key map (replaces broken per-account model) ---

_IMAGE_KEY_MAP: dict = {}  # md5 -> {'aes': bytes, 'xor': int}
_IMAGE_KEY_MAP_DIR: str = None


def _load_or_build_image_key_map(decrypted_dir: str) -> dict:
    """Load md5->key map from _media_keys.json cache.

    Only loads keys that were verified by memory extraction (harvest-keys).
    Does NOT rebuild from message DBs — those contain CDN aeskeys which have
    been confirmed to NOT decrypt local .dat files.
    """
    global _IMAGE_KEY_MAP, _IMAGE_KEY_MAP_DIR

    if _IMAGE_KEY_MAP and _IMAGE_KEY_MAP_DIR == decrypted_dir:
        return _IMAGE_KEY_MAP

    import json as _json
    keys_file = os.path.join(decrypted_dir, '_media_keys.json')

    result = {}
    try:
        if os.path.isfile(keys_file) and os.path.getsize(keys_file) > 0:
            with open(keys_file, 'r', encoding='utf-8') as f:
                cached = _json.load(f)
            md5_keys = cached.get('md5_keys', {})
            _xor_from_cache = 0
            _xor_defaulted = 0
            # 来源统计（issue #16 新评论）：**"派生真值恰好是 0xC9"与"老版本写死的 0xC9"
            # 在缓存里长得一模一样**，只看值无法区分 ⇒ 从 `xor_src` 字段分类，
            # 老缓存没有这个字段的记为 `legacy`（来源不明，**不假设**它是哪一种）。
            _xor_src_counts = {}
            for md5, v in md5_keys.items():
                try:
                    _raw_xor = v.get('xor_key')
                    if _raw_xor is None or (isinstance(_raw_xor, str)
                                            and not _raw_xor.strip()):
                        # 字段缺失/为空 ⇒ 回退**默认值**。
                        # ⚠️ 绝不能退化成 0：0 = 尾部一个字节都不 XOR ⇒ 尾部原样返回，
                        # 后果与"用错密钥"完全相同，却更隐蔽（issue #16 症状 2 同族）。
                        xor_val = _DAT_V2_DEFAULT_XOR
                        _xor_defaulted += 1
                        _src = 'default-missing'
                    else:
                        xor_val = (int(_raw_xor, 16) if isinstance(_raw_xor, str)
                                   else int(_raw_xor))
                        _xor_from_cache += 1
                        _src = str(v.get('xor_src') or 'legacy')
                    _xor_src_counts[_src] = _xor_src_counts.get(_src, 0) + 1
                    result[md5] = {
                        'aes': bytes.fromhex(v['aes_key']),
                        'xor': xor_val,
                        'xor_src': _src,
                    }
                except (ValueError, KeyError, TypeError):
                    pass
            if result:
                # Ensure _h thumbnail variants exist for all cached keys
                _h_added = 0
                for md5 in list(result.keys()):
                    if not md5.endswith('_h'):
                        thumb = md5 + '_h'
                        if thumb not in result:
                            result[thumb] = result[md5]
                            _h_added += 1
                if _h_added:
                    print(f"[media] Added {_h_added} _h thumbnail variants to cached keys", flush=True)
                # 把"这次用的是缓存里的值"还是"回退到默认值"显式打出来 ——
                # 这个缺陷之所以长期存在，正是因为两者从外部看不出区别。
                # 追加：**来源**也要打（derived / repaired / default-* / legacy）。
                _src_txt = ', '.join('%s=%d' % (k, _xor_src_counts[k])
                                     for k in sorted(_xor_src_counts))
                if _xor_defaulted:
                    print(f"[media] Loaded {len(result)} verified keys from cache "
                          f"(xor: from cache={_xor_from_cache}, "
                          f"missing xor_key → default 0x{_DAT_V2_DEFAULT_XOR:02X}="
                          f"{_xor_defaulted} — NOT a derived value; "
                          f"xor_src: {_src_txt})", flush=True)
                else:
                    print(f"[media] Loaded {len(result)} verified keys from cache "
                          f"(xor from cache for all {_xor_from_cache}; "
                          f"xor_src: {_src_txt})", flush=True)
                # 老缓存的来源是未知的 ⇒ 必须说出来：报告者那种"0xC9 到底是不是派生真值"
                # 的疑问，靠这一行就能定性（下一行给出可操作的建议）。
                if any(k == 'legacy' for k in _xor_src_counts):
                    print(f"[media] ⚠️ 其中 {_xor_src_counts.get('legacy', 0)} 条 xor_key "
                          f"**来源不明**（老版本写下的，没有 xor_src 字段）—— "
                          f"若图片『只有上面一小部分能显示』，就是这个值可能不是派生真值；"
                          f"让它被纠正的办法：保持微信运行并打开一次该图（内存路径会回填真值），"
                          f"或执行 harvest-keys", flush=True)

    except Exception:
        pass

    _IMAGE_KEY_MAP = result
    _IMAGE_KEY_MAP_DIR = decrypted_dir
    return result


def _get_account_media_keys(decrypted_dir: str, wxid: str) -> tuple:
    """Get per-account XOR and AES keys for V2 .dat decryption.

    Checks local cache first, then extracts keys from decrypted message DBs
    by scanning V2 image/video XML for the per-account aeskey attribute.
    Returns (xor_key: int, aes_key: bytes) or (None, None).
    """
    global _ACCOUNT_KEYS_CACHE

    if wxid in _ACCOUNT_KEYS_CACHE:
        cached = _ACCOUNT_KEYS_CACHE[wxid]
        return cached.get('xor'), cached.get('aes')

    # Check _media_keys.json cache file
    keys_file = os.path.join(decrypted_dir, '_media_keys.json')
    try:
        if os.path.isfile(keys_file):
            import json
            with open(keys_file, 'r', encoding='utf-8') as f:
                all_keys = json.load(f)
            account_keys = all_keys.get(wxid, {})
            if account_keys.get('aes_key'):
                xor_raw = account_keys.get('xor_key', '')
                if isinstance(xor_raw, str) and xor_raw.startswith('0x'):
                    xor_key = int(xor_raw, 16)
                elif isinstance(xor_raw, (int, float)):
                    xor_key = int(xor_raw)
                else:
                    xor_key = int(xor_raw) if xor_raw else 0
                aes_key_raw = account_keys['aes_key']
                if len(aes_key_raw) == 32 and all(c in '0123456789abcdefABCDEF' for c in aes_key_raw):
                    aes_key = bytes.fromhex(aes_key_raw)
                else:
                    aes_key = aes_key_raw[:16].encode('ascii')
                _ACCOUNT_KEYS_CACHE[wxid] = {'xor': xor_key, 'aes': aes_key}
                return xor_key, aes_key
    except Exception:
        pass

    # Extract keys locally from decrypted message DBs
    xor_key, aes_key = _extract_media_keys_from_dbs(decrypted_dir)
    if aes_key is None:
        return None, None

    # Cache in memory and file
    _ACCOUNT_KEYS_CACHE[wxid] = {'xor': xor_key, 'aes': aes_key}

    try:
        os.makedirs(os.path.dirname(keys_file), exist_ok=True)
        all_keys = {}
        if os.path.isfile(keys_file):
            with open(keys_file, 'r', encoding='utf-8') as f:
                all_keys = json.load(f)
        all_keys[wxid] = {'xor_key': f'0x{xor_key:02X}', 'aes_key': aes_key.hex()}
        with open(keys_file, 'w', encoding='utf-8') as f:
            json.dump(all_keys, f, indent=2)
    except Exception:
        pass

    return xor_key, aes_key


def _extract_media_keys_from_dbs(decrypted_dir: str) -> tuple:
    """Extract V2 media AES key from decrypted message databases.

    Scans Msg_ tables for V2 image/video XML that contains the per-account
    aeskey attribute. The XOR key defaults to 0 (most common case).

    Returns (xor_key: int, aes_key: bytes) or (None, None).
    """
    import json
    import re as _re

    msg_dir = os.path.join(decrypted_dir, 'message')
    if not os.path.isdir(msg_dir):
        print("[media] message dir not found:", msg_dir, flush=True)
        return None, None

    try:
        import zstandard as zstd
        dctx = zstd.ZstdDecompressor()
    except ImportError:
        print("[media] zstandard not installed — cannot decompress message_content", flush=True)
        return None, None

    _ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
    _AESKEY_RE = _re.compile(rb'''aeskey\s*=\s*["']([0-9a-fA-F]{32,64}|[0-9a-zA-Z+/=]{16,48})["']''')
    _XORKEY_RE = _re.compile(rb'xorkey\s*=\s*["\']([0-9a-fA-F]{2})["\']')

    total_scanned = 0
    total_zstd = 0
    for fname in sorted(os.listdir(msg_dir)):
        if not fname.startswith('message_') or not fname.endswith('.db'):
            continue
        db_path = os.path.join(msg_dir, fname)
        try:
            conn = sqlite3.connect(db_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (tname,) in tables:
                # Search both message_content and packed_info_data for each type individually
                # to avoid the IN clause which might be optimized differently
                for search_type in (3, 43):
                    rows = conn.execute(
                        f"SELECT message_content FROM [{tname}] "
                        f"WHERE (local_type & 0xFFFF) = ? AND length(message_content) > 50 "
                        f"LIMIT 100",
                        (search_type,)
                    ).fetchall()
                    for (content,) in rows:
                        total_scanned += 1
                        if not isinstance(content, bytes):
                            continue
                        # Try zstd first
                        dec = None
                        if content[:4] == _ZSTD_MAGIC:
                            total_zstd += 1
                            try:
                                dec = dctx.decompress(content)
                            except Exception:
                                pass
                        if dec is None:
                            # Not zstd — try raw text
                            try:
                                dec = content.decode('utf-8', errors='replace')
                                if 'aeskey' not in dec:
                                    dec = None
                            except Exception:
                                pass
                        if dec is None:
                            continue
                        m = _AESKEY_RE.search(dec)
                        if not m:
                            continue
                        aes_hex = m.group(1).decode('ascii')
                        if len(aes_hex) >= 32:
                            try:
                                aes_key = bytes.fromhex(aes_hex[:32])
                            except ValueError:
                                aes_key = aes_hex[:16].encode('ascii')
                        else:
                            aes_key = aes_hex[:16].encode('ascii')

                        xor_key = _DAT_V2_DEFAULT_XOR
                        xm = _XORKEY_RE.search(dec)
                        if xm:
                            try:
                                xor_key = int(xm.group(1), 16)
                            except ValueError:
                                pass

                        print(f"[media] Found aeskey in {fname}/{tname} type={search_type}, "
                              f"xor=0x{xor_key:02X}", flush=True)
                        conn.close()
                        return xor_key, aes_key
            conn.close()
        except sqlite3.Error:
            continue

    print(f"[media] Key scan complete: scanned {total_scanned} messages "
          f"({total_zstd} zstd) across {msg_dir}, no aeskey found", flush=True)
    return None, None


def _translate_file_md5_to_cdn_md5(decrypted_dir: str, file_md5: str) -> str:
    """Map a file-name md5 (from .dat file) to the CDN md5 (from XML) via hardlink DB.

    The hardlink DB's image_hardlink_info_v4 table maps:
      - md5 column: CDN image md5 (appears in message_content XML with aeskey)
      - file_name column: local .dat file name ({file_content_md5}.dat)

    This function bridges the two, enabling key lookup:
      file_md5 → hardlink DB → CDN md5 → message_content XML → aeskey

    Returns the CDN md5 string, or None if not found.
    """
    if not file_md5 or len(file_md5) != 32:
        return None
    hardlink_db = os.path.join(decrypted_dir, "hardlink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        hardlink_db = os.path.join(decrypted_dir, "HardLink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        return None
    try:
        conn = sqlite3.connect(hardlink_db)
        # file_name is stored as {md5}.dat, {md5}_t.dat, {md5}_h.dat, etc.
        row = conn.execute(
            "SELECT md5 FROM image_hardlink_info_v4 WHERE file_name LIKE ? LIMIT 1",
            (file_md5 + '%',)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except sqlite3.Error:
        pass
    return None


def _collect_image_aeskey(decrypted_dir: str, md5: str) -> str:
    """Find the AES key for a specific V2 image by searching message DBs.

    Handles thumbnail (_h) suffix: if md5 ends with '_h', also tries the
    parent image's md5. Searches both aeskey and cdnthumbaeskey attributes.

    Returns the 32-char hex aeskey string, or None.
    """
    global _image_aes_keys
    if md5 in _image_aes_keys:
        return _image_aes_keys[md5]

    import re as _re

    # Resolve search targets: if md5 ends with '_h', also try base md5
    search_md5s = [md5]
    is_thumb = md5.endswith('_h')
    if is_thumb:
        base_md5 = md5[:-2]
        if base_md5 in _image_aes_keys:
            _image_aes_keys[md5] = _image_aes_keys[base_md5]
            return _image_aes_keys[base_md5]
        search_md5s.append(base_md5)

    # If direct search fails, also try CDN md5 via hardlink DB bridge.
    # The file md5 (from .dat file name) is different from the CDN md5
    # (from XML <img md5="...">), but they map via image_hardlink_info_v4.
    for _sm5 in list(search_md5s):
        _cdn = _translate_file_md5_to_cdn_md5(decrypted_dir, _sm5)
        if _cdn and _cdn not in search_md5s:
            search_md5s.append(_cdn)

    msg_dir = os.path.join(decrypted_dir, 'message')
    if not os.path.isdir(msg_dir):
        return None

    try:
        import zstandard as zstd
        dctx = zstd.ZstdDecompressor()
    except ImportError:
        return None

    _ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
    _AESKEY_PAT = _re.compile(rb'''aeskey\s*=\s*["']([0-9a-fA-F]{32,64})["']''')
    _CDNTHUMB_PAT = _re.compile(rb'''cdnthumbaeskey\s*=\s*["']([0-9a-fA-F]{32,64})["']''')

    # Pre-compile md5 patterns for all search variants
    _md5_patterns = [
        (sm5, _re.compile(
            rb'''md5\s*=\s*["'](''' + _re.escape(sm5.encode()) + rb''')["']'''
        ))
        for sm5 in search_md5s
    ]

    for fname in os.listdir(msg_dir):
        if not fname.startswith('message_') or not fname.endswith('.db'):
            continue
        db_path = os.path.join(msg_dir, fname)
        try:
            conn = sqlite3.connect(db_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (tname,) in tables:
                # Search both type 3 (image) and type 43 (video, may ref thumbnails)
                for search_type in (3, 43):
                    cursor = conn.execute(
                        f"SELECT message_content FROM [{tname}] "
                        f"WHERE (local_type & 0xFFFF) = ? AND length(message_content) > 50",
                        (search_type,)
                    )
                    while True:
                        batch = cursor.fetchmany(500)
                        if not batch:
                            break
                        for (content,) in batch:
                            if not isinstance(content, bytes):
                                continue
                            dec = None
                            if content[:4] == _ZSTD_MAGIC:
                                try:
                                    dec = dctx.decompress(content)
                                except Exception:
                                    pass
                            if dec is None:
                                # zstd failed — check if content has aeskey patterns as plain text.
                                # Keep as bytes for regex compatibility.
                                if b'aeskey' not in content and b'cdnthumbaeskey' not in content:
                                    dec = None
                                else:
                                    dec = content
                            if dec is None:
                                continue
                            # Check each search md5
                            for sm5, md5_pat in _md5_patterns:
                                if not md5_pat.search(dec):
                                    continue
                                # Try aeskey first, then cdnthumbaeskey
                                m_key = _AESKEY_PAT.search(dec)
                                if not m_key:
                                    m_key = _CDNTHUMB_PAT.search(dec)
                                if m_key:
                                    key = m_key.group(1).decode('ascii')[:32]
                                    # Cache for both the searched md5 and the original
                                    _image_aes_keys[sm5] = key
                                    if sm5 != md5:
                                        _image_aes_keys[md5] = key
                                    # Also cache _h variant for non-thumb md5s
                                    if not sm5.endswith('_h'):
                                        _image_aes_keys[sm5 + '_h'] = key
                                    print(f"[media] Found per-image aeskey for md5={sm5[:16]}... in {fname}", flush=True)
                                    conn.close()
                                    return key
            conn.close()
        except sqlite3.Error:
            pass

    return None


def _sniff_image_mime(head: bytes) -> str:
    """根据文件头判断图片 MIME（返回 "application/octet-stream" 表示不是可直接展示的图片）。"""
    if not head:
        return "application/octet-stream"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


# --- 图片完整性（三态）校验：issue #46 ---------------------------------------
# 用途：判断一个**已经解出来**的图片候选是不是"尾部被截断 / 被解错"。
# 为什么必须三态、不许塌成布尔：
#   控制方用真实数据实测过 —— 本机 `decrypted_media` 缓存 31 个文件（28 JPEG / 3 PNG）里
#   **2 个 JPEG 缺 EOI（≈7%）**，很可能是**源图本身就被截断**、但实际**能正常显示**。
#   ⇒ 绝不允许把"缺尾"当成"这张图不能给用户"。因此 `None`（不可判定）必须与 `False`
#   （可判定类型且**确认**缺尾）分开：`None` 走原行为（立刻返回），只有 `False` 才降级。
_IMAGE_COMPLETENESS_MIN_SIZE = 32   # 比这更短 ⇒ 连"尾部"都谈不上，判不了（返回 None）
_IMAGE_TAIL_WINDOW = 64             # 判断只读尾部这么多个字节（原图可能几十 MB，别整块读）
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"   # 标准 12 字节 IEND 块
# 只有这些格式的"完整尾部"能用一把固定 magic 判定；其它一律 None（不可判定）
_IMAGE_DECIDABLE_MIMES = ("image/jpeg", "image/png", "image/gif")


def _tail_verdict(mime: str, tail: bytes):
    """按类型判定尾部（只在 ``mime`` 属于 ``_IMAGE_DECIDABLE_MIMES`` 时才有意义）。"""
    if mime == "image/jpeg":
        return tail[-2:] == b"\xff\xd9"
    if mime == "image/png":
        return tail[-len(_PNG_IEND):] == _PNG_IEND
    if mime == "image/gif":
        return tail[-1:] == b"\x3b"
    return None


def _image_completeness(data_or_path, mime: str = None):
    """三态"图片完整性"校验：``True`` 完整 / ``False`` 可判定类型且确认缺尾 / ``None`` 不可判定。

    * **JPEG**：结尾必须是 ``FF D9``；否则 ``False``；
    * **PNG** ：结尾必须是标准 12 字节 IEND 块（``_PNG_IEND``）；否则 ``False``；
    * **GIF** ：结尾必须是 ``0x3B``；否则 ``False``；
    * **WEBP / BMP / 其它 / 数据太短** ⇒ ``None``（**不可判定**：这些格式尾部没有可以一把判定
      "完整"的固定 magic，或信息不足）。

    ``data_or_path`` 既可以是完整字节串，也可以是文件路径 —— 走路径时**只读头 16 字节 +
    尾 ``_IMAGE_TAIL_WINDOW`` 字节**（避免为了一次校验把几十 MB 的原图整块读进内存）；
    而且**不可判定/太短的路径连尾部都不读**就返回 ``None``：`None` 这条路上不许比旧行为
    多任何一次 IO（WEBP/BMP 仍然要"立刻返回、不变慢"）。
    读不到文件（被删/被占）也返回 ``None``：判不了就按原行为处理，不许因此拒绝服务。
    """
    if data_or_path is None:
        return None

    if isinstance(data_or_path, (bytes, bytearray, memoryview)):
        data = bytes(data_or_path)
        size = len(data)
        head = data[:16]
        tail = data[-_IMAGE_TAIL_WINDOW:]
    else:
        try:
            size = os.path.getsize(data_or_path)
            if size <= 0:
                return None
            with open(data_or_path, 'rb') as f:
                head = f.read(16)
                if mime is None:
                    mime = _sniff_image_mime(head)
                if mime not in _IMAGE_DECIDABLE_MIMES or size < _IMAGE_COMPLETENESS_MIN_SIZE:
                    return None      # 不可判定/太短：**连尾部都不读**
                if size > _IMAGE_TAIL_WINDOW:
                    f.seek(size - _IMAGE_TAIL_WINDOW)
                else:
                    f.seek(0)
                tail = f.read(_IMAGE_TAIL_WINDOW)
        except OSError:
            return None

    if mime is None:
        mime = _sniff_image_mime(head)
    if mime not in _IMAGE_DECIDABLE_MIMES or size < _IMAGE_COMPLETENESS_MIN_SIZE:
        return None
    return _tail_verdict(mime, tail)


def _describe_incompleteness(mime: str) -> str:
    """把"缺哪种尾"说成人话（日志/取证用；不许含糊成"坏了"）。"""
    if mime == "image/jpeg":
        return "JPEG 缺 EOI（结尾不是 FF D9）"
    if mime == "image/png":
        return "PNG 缺 IEND 块"
    if mime == "image/gif":
        return "GIF 缺尾部 0x3B"
    return f"{mime} 缺尾部"


def _pick_incomplete_candidate(candidates):
    """从"未通过完整性校验"的候选里挑一个作为最后兜底：**当前大小最大**者，平手取先出现的。

    为什么按大小而不是按候选链顺序：这些候选**都已经**被判"缺尾"，用户能看到的内容只可能
    与**已经解出来的像素量**正相关 ⇒ 原图（大）比缩略图（小）更接近他的预期；拿小的会让
    用户看到的内容比改动前**更少**，那是纯粹的退步。

    ⚠️ 大小按**取用当时**重算而不是暂存时记录：同一源文件的多个候选共享同一个输出路径
    （``_decrypt_dat_v2`` 按源路径命名），后一次解密会覆盖前一次的字节。所以这里重算，
    并且跳过已经被删除的路径。
    """
    best = None
    best_size = -1
    for cand in candidates:
        try:
            size = os.path.getsize(cand['path'])
        except (OSError, KeyError, TypeError):
            continue
        if size > best_size:
            best, best_size = cand, size
    return best


def tools_dir() -> str:
    """本程序的 tools 目录（放 ffmpeg.exe / silk_decoder.exe 的地方）。

    打包运行时 = exe 同目录的 tools\\；源码运行时 = 项目根目录的 tools\\。
    """
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(_sys.executable)), "tools")
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    return os.path.join(project_root, "tools")


def _ffmpeg_search_paths():
    """ffmpeg 的候选位置（按优先级）；每次调用都重新探测，用户放进去即可生效。"""
    import sys as _sys
    paths = []
    bundle = getattr(_sys, "_MEIPASS", None)
    if bundle:
        paths.append(os.path.join(bundle, "tools", "ffmpeg.exe"))
    tdir = tools_dir()
    paths.append(os.path.join(tdir, "ffmpeg.exe"))
    exe_dir = os.path.dirname(os.path.abspath(_sys.executable)) if getattr(_sys, "frozen", False) \
        else os.path.dirname(tdir)
    paths.append(os.path.join(exe_dir, "ffmpeg.exe"))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        paths.append(os.path.join(local, "WeChatEXP", "tools", "ffmpeg.exe"))
    return paths


def _find_ffmpeg() -> str:
    """查找可用的 ffmpeg：PATH → 程序 tools 目录 → 程序根目录 → %LOCALAPPDATA%。

    不缓存结果：用户把 ffmpeg.exe 放进 tools 后，下一张图片就会自动转换。
    """
    import shutil as _shutil
    found = _shutil.which("ffmpeg")
    if found:
        return found
    for c in _ffmpeg_search_paths():
        if c and os.path.isfile(c):
            return c
    return None


# ffmpeg 下载源（wxgf 图片解码用；国内可换镜像）
FFMPEG_DOWNLOAD_URLS = [
    ("gyan.dev（官方推荐）",
     "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    ("BtbN GitHub（essentials 构建）",
     "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"),
]


def install_ffmpeg_from_zip(zip_path: str, dest_dir: str = None):
    """从 ffmpeg 压缩包里提取 bin/ffmpeg.exe 到本程序 tools 目录。

    Returns: 安装后的 ffmpeg.exe 路径；找不到时报 ValueError。
    """
    import zipfile
    if dest_dir is None:
        dest_dir = tools_dir()
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, "ffmpeg.exe")
    with zipfile.ZipFile(zip_path) as zf:
        member = None
        for name in zf.namelist():
            low = name.lower().replace("\\", "/")
            if low.endswith("/bin/ffmpeg.exe") or low == "ffmpeg.exe":
                member = name
                break
        if not member:
            raise ValueError("压缩包里没有找到 bin/ffmpeg.exe")
        with zf.open(member) as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
    if not os.path.isfile(target) or os.path.getsize(target) < 100000:
        raise ValueError("解压出来的 ffmpeg.exe 不完整")
    return target

def wxgf_status() -> dict:
    """wxgf(H.265) 图片解码能力状态（供界面提示用户安装 ffmpeg）。"""
    ffmpeg = _find_ffmpeg()
    dll = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native", "VoipEngine.dll")
    target = os.path.join(tools_dir(), "ffmpeg.exe")
    return {
        "supported": bool(ffmpeg) or os.path.isfile(dll),
        "ffmpeg": ffmpeg or "",
        "decoderDll": dll if os.path.isfile(dll) else "",
        "toolsDir": tools_dir(),
        "targetPath": target,
        "installed": bool(ffmpeg),
        "downloadUrls": [{"name": n, "url": u} for n, u in FFMPEG_DOWNLOAD_URLS],
    }


def wxgf_supported() -> bool:
    """本机是否能解码微信 wxgf(H.265) 图片。"""
    if os.name != "nt":
        return bool(_find_ffmpeg())
    return True if os.path.isfile(os.path.join(os.path.dirname(__file__), "native",
                                            "VoipEngine.dll")) else bool(_find_ffmpeg())


def _convert_wxgf_with_ffmpeg(data: bytes):
    """用 ffmpeg 把 wxgf(H.265 裸流) 转成 JPEG；不可用时返回 None。"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    import shutil as _shutil
    import subprocess
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="wxgf_")
    src = os.path.join(tmpdir, "in.wxgf")
    dst = os.path.join(tmpdir, "out.jpg")
    try:
        with open(src, "wb") as f:
            f.write(data)
        proc = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-f", "hevc", "-i", src,
             "-frames:v", "1", "-q:v", "3", dst],
            capture_output=True, timeout=90)
        if proc.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 100:
            with open(dst, "rb") as f:
                return f.read()
        return None
    except Exception:
        return None
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)


def _find_sibling_thumbnail(resolved_path: str) -> str:
    """找同目录的可用替代图，优先高清 _h，再用缩略图 _t。"""
    return next(iter(_sibling_image_variants(resolved_path)), None)


def _sibling_image_variants(resolved_path: str) -> list:
    if not resolved_path:
        return []
    base, ext = os.path.splitext(resolved_path)
    current = ''
    if base.endswith(('_t', '_h')):
        current, base = base[-2:], base[:-2]
    candidates = []
    for suffix in ("_h", "", "_t"):
        if suffix == current:
            continue
        for cand in (base + suffix + ext, base + suffix + ".dat"):
            if cand not in candidates and os.path.isfile(cand) and os.path.getsize(cand) > 0:
                candidates.append(cand)
    return candidates

def _decrypt_dat_v2(file_path: str, aes_key: bytes, xor_key: int = None, output_dir: str = None) -> str:
    """Decrypt a V2 .dat file using AES-128-ECB key + XOR tail.

    V2 format (WeChat 4.x):
      [15 bytes: 6-byte sig + 4-byte AES size + 4-byte XOR size + 1 pad]
      [AES-encrypted data (aes_sz plaintext, PKCS7-padded to 16-byte boundary)]
      [Raw unencrypted data]
      [XOR-encrypted tail (xor_sz bytes)]

    aes_key: 16 raw bytes (ASCII string from _media_keys.json, not hex-decoded)
    xor_key: integer 0-255 (default: 0xC9, the empirically-confirmed WeChat 4.x value)
    output_dir: directory for cached decrypted file (default: temp dir next to file)

    Returns path to decrypted file, or None.
    """
    if xor_key is None:
        xor_key = _DAT_V2_DEFAULT_XOR
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(file_path), 'decrypted_media')
    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Padding
    except ImportError:
        return None

    try:
        with open(file_path, 'rb') as f:
            data = f.read()
    except OSError:
        return None

    if len(data) < 22:
        return None

    try:
        sig = data[:6]
        aes_size, hdr_xor_size = struct.unpack_from('<II', data, 6)
    except struct.error:
        return None

    if sig != _DAT_V2_HEADER:
        return None

    padded_aes_size = aes_size + 16 - (aes_size % 16)

    body_start = 15
    if body_start + padded_aes_size > len(data):
        return None

    aes_data = data[body_start:body_start + padded_aes_size]

    try:
        cipher = AES.new(aes_key[:16], AES.MODE_ECB)
        dec_aes_padded = cipher.decrypt(aes_data)
        dec_aes = Padding.unpad(dec_aes_padded, AES.block_size)
    except Exception:
        return None

    raw_start = body_start + padded_aes_size
    if hdr_xor_size > 0 and raw_start + hdr_xor_size <= len(data):
        raw_data = data[raw_start:-hdr_xor_size]
        xor_data = data[-hdr_xor_size:]
    elif hdr_xor_size > 0:
        # 【防御性 / 一致性修复，**不是** issue #16 症状 2 的根因 —— 本机无实测实例】
        # 头部声称的 XOR 长度**超过实际剩余**（字段被钳在某个上限、或文件没下完被截断）。
        # 控制方/调查员在全库 18714 个 V2 上实测：`plain < file_size` = 0/18714、
        # 头部 `delta<0` = 0/41747、`len(raw 明文) == file_size` = 1229/1229
        # ⇒ 本机没有文件会走到这里；且"截断"本身**不产生绿条**（缺失区被掩盖，绿 0.0000），
        # 所以它**解释不了**用户看到的 `rgb(0,135,0)` —— 那是"中段被 XOR 破坏"才有的指纹。
        # 之所以还是按"整段剩余都 XOR"处理：① 一个字节都不 XOR = 把**密文**当明文交给解码器，
        # 是静默隐患；② 同仓库 V1 解码器（``_decrypt_dat_v1``）就是 ``xor_data = data[raw_start:]``，
        # 根本不看头里的 xor_size —— V2 这里才是语义不一致的那一个。
        # ⚠️ 假设：声称长度 > 剩余时，剩余字节都是 XOR 区（本机无法判定；若真存在"大段未加密
        # 中段 + xor_size 被写大"，本分支会把中段也 XOR 一遍。消歧可后续用"尾部是否为合法
        # 文件尾（JPEG FF D9 / PNG IEND）"来做，本任务没做）。
        raw_data = b''
        xor_data = data[raw_start:]
        print(f"  [V2] 头部声称 XOR 长度 {hdr_xor_size} > 实际剩余 "
              f"{len(data) - raw_start} —— 按可用长度 XOR 尾部"
              f"（防御性一致性修复；本机无实测实例，不代表症状 2 的成因）", flush=True)
    else:
        # hdr_xor_size == 0 = 声明"没有 XOR 尾部"，保持既有语义：尾部就是明文
        raw_data = data[raw_start:]
        xor_data = b''

    dec_xor = bytes(b ^ xor_key for b in xor_data) if xor_data else b''
    result = dec_aes + raw_data + dec_xor

    # Convert wxgf (WeChat proprietary H.265 container) to a normal JPEG.
    # 优先用微信自带 DLL；没有时退回 ffmpeg（PATH 或 tools/ffmpeg.exe）。
    # 都不可用时保留原始 wxgf 字节，由调用方改用 _t/_h 缩略图兜底，
    # 绝不能把 wxgf 当成 image/jpeg 返回给浏览器（会显示成裂图）。
    if result[:4] == b'wxgf':
        converted = _convert_wxgf(result) or _convert_wxgf_with_ffmpeg(result)
        if converted:
            result = converted

    import hashlib
    src_hash = hashlib.md5(file_path.encode()).hexdigest()[:12]
    out_path = os.path.join(output_dir, f'{src_hash}_v2.dec')
    try:
        os.makedirs(output_dir, exist_ok=True)
        with open(out_path, 'wb') as f:
            f.write(result)
        return out_path
    except OSError:
        return None


def _convert_wxgf(data: bytes) -> bytes:
    """Convert WeChat wxgf image format to standard JPEG/PNG using native DLL."""
    if os.name != 'nt':
        return None

    # Try to find VoipEngine.dll
    dll_paths = [
        os.path.join(os.path.dirname(__file__), 'native', 'VoipEngine.dll'),
        r'D:\perl_wrk\PC_Wechat\WeChatDataAnalysis_ref\src\wechat_decrypt_tool\native\VoipEngine.dll',
    ]
    dll_path = None
    for p in dll_paths:
        if os.path.isfile(p):
            dll_path = p
            break
    if not dll_path:
        return None

    try:
        import ctypes

        class _WxAMConfig(ctypes.Structure):
            _fields_ = [('mode', ctypes.c_int), ('reserved', ctypes.c_int)]

        voip = ctypes.WinDLL(dll_path)
        fn = voip.wxam_dec_wxam2pic_5
        fn.argtypes = [ctypes.c_int64, ctypes.c_int, ctypes.c_int64,
                       ctypes.POINTER(ctypes.c_int), ctypes.c_int64]
        fn.restype = ctypes.c_int64

        max_out = 52 * 1024 * 1024
        for mode in (0, 3):
            config = _WxAMConfig()
            config.mode = mode
            config.reserved = 0
            in_buf = ctypes.create_string_buffer(data, len(data))
            out_buf = ctypes.create_string_buffer(max_out)
            out_sz = ctypes.c_int(max_out)

            ret = fn(ctypes.addressof(in_buf), len(data),
                     ctypes.addressof(out_buf), ctypes.byref(out_sz),
                     ctypes.addressof(config))
            if ret == 0 and out_sz.value > 0:
                return out_buf.raw[:out_sz.value]
    except Exception:
        pass

    return None


def _find_cached_thumbnail(decrypted_dir: str, md5: str, local_id: int = 0, wxid: str = None) -> str:
    """Find a cached thumbnail in WeChat's cache directory for a V2 image.

    WeChat 4.x stores decrypted thumbnails at:
      {storage_root}/{wxid}/cache/YYYY-MM/Message/{dir1_hash}/Thumb/{local_id}_{ts}_thumb.jpg

    This is a fallback when the V2 AES key is unavailable — thumbnails are
    already decrypted by WeChat and can be served directly.
    """
    if not md5 or len(md5) != 32:
        return None

    # Get dir1 from hardlink DB
    hardlink_db = os.path.join(decrypted_dir, "hardlink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        hardlink_db = os.path.join(decrypted_dir, "HardLink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        return None

    try:
        conn = sqlite3.connect(hardlink_db)
        # Try file_name LIKE first (file md5), then md5 column (CDN md5)
        row = conn.execute(
            "SELECT dir1 FROM image_hardlink_info_v4 WHERE file_name LIKE ? LIMIT 1",
            (md5 + '%',)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT dir1 FROM image_hardlink_info_v4 WHERE md5=? LIMIT 1",
                (md5,)
            ).fetchone()
        if not row:
            conn.close()
            return None

        dir1_id = row[0]
        dir1_row = conn.execute(
            "SELECT username FROM dir2id WHERE rowid=?", (dir1_id,)
        ).fetchone()
        conn.close()
        if not dir1_row or not dir1_row[0]:
            return None
        dir1_name = dir1_row[0]
    except sqlite3.Error:
        return None

    # Find WeChat storage root and wxid
    storage_root = _get_base_storage(decrypted_dir)
    wxid = wxid or _detect_wxid(decrypted_dir)
    if not storage_root or not wxid:
        # Try well-known locations
        for sr in [r'D:\xwechat_files', r'C:\xwechat_files']:
            if os.path.isdir(sr):
                storage_root = sr
                break
        if not storage_root:
            return None

    wxid_dir = os.path.join(storage_root, wxid)
    if not os.path.isdir(wxid_dir):
        return None

    # Search cache directories for matching thumbnail
    cache_base = os.path.join(wxid_dir, 'cache')
    if not os.path.isdir(cache_base):
        return None

    if not local_id or local_id <= 0:
        return None  # must have local_id for correct mapping

    thumb_dir = os.path.join(cache_base, '*', 'Message', dir1_name, 'Thumb')
    thumb_glob = os.path.join(thumb_dir, f'{local_id}_*_thumb.*')

    import glob
    matches = glob.glob(thumb_glob)
    if not matches:
        return None

    # Return the largest (highest quality) match
    best = max(matches, key=os.path.getsize)
    return best if os.path.isfile(best) else None


def serve_hardlink_media(decrypted_dir: str, media_info: dict, wxid: str = None):
    """Flask response: serve a media file resolved via HardLink DB.

    Handles V0 (XOR), V1 (fixed AES), and V2 (dynamic AES) WeChat .dat encryption.
    """
    if not media_info:
        abort(404)

    # 统一成绝对路径：否则 send_file 会把相对路径解析到 Flask 应用目录下
    # （用 --decrypted-dir backup\2026-09-20 这类相对路径启动时会 500）
    if decrypted_dir:
        decrypted_dir = os.path.abspath(decrypted_dir)

    resolved = _resolve_hardlink_path(decrypted_dir, media_info, wxid)
    print(f"  [LIGHTBOX] md5={media_info.get('md5','')[:16]}... resolved={resolved}")
    if resolved is None or not os.path.isfile(resolved):
        abort(404)

    # Handle .dat encrypted files
    if resolved.lower().endswith('.dat'):
        # Detect encryption version
        try:
            with open(resolved, 'rb') as f:
                header = f.read(256)
        except OSError:
            abort(404)

        version = _detect_wechat_dat_version(header)

        # V2: per-image AES key (keys are only in WeChat process memory)
        if version == 2:
            md5_val = media_info.get('md5', '')

            # 记录解密原图时用上的密钥，供缩略图兜底复用
            _found_key = {'aes': None, 'xor': None}

            # --- issue #46：未通过完整性校验的候选，暂存为"最后才用的兜底" -----------
            # 背景：缓存命中即 return，而"成功"的判据只是**前 16 字节像图片**
            # （用错 XOR 时那 16 字节来自**正确的 AES 段**）⇒ 照样 200 + 一张坏图
            # ⇒ 后续的 MMKV 重新派生永远不执行 ⇒ 缓存里的错 XOR 永久固化。
            # 新语义：只有 `True`/`None` 才立刻返回；`False`（可判定类型且**确认**缺尾）
            # 暂存 + 继续，让后续步骤（MMKV 重新派生 → 内存）有机会给出通过校验的候选。
            # ⚠️ 链走完仍没有更好的 ⇒ **回退返回**暂存的那个：绝不 404 / 500
            # （真实缓存里 2/28 个 JPEG 是"源图本身缺尾"，用户本来就能看到）。
            _incomplete_candidates = []

            def _stash_incomplete(dec_path, mime, source_tag=None, more_steps=True,
                                  key_src=None):
                """记住一个"可判定类型但确认缺尾"的候选，并**继续**尝试后续步骤。

                为什么暂存**路径**而不是字节：一个候选可能就是几十 MB，而真正需要它的概率
                很低（控制方实测真实缓存里 2/28≈7%）。

                ``key_src``：**解这张图用的密钥来自哪里**（`cache:derived` /
                `cache:legacy` / `memory` / `mmkv` / `thumbnail`…）。issue #16 新评论的
                报告者只能看到"图不完整"，看不出"用的是哪把 XOR、它是派生真值还是老版本
                写死的默认值" ⇒ 这一行就是给那个问题用的。
                """
                _incomplete_candidates.append({
                    'path': dec_path,
                    'mime': mime,
                    'source_tag': source_tag,
                    'key_src': key_src,
                    'why': _describe_incompleteness(mime),
                })
                _tag = f"（源={source_tag}）" if source_tag else ""
                _key = f"，密钥来源={key_src}" if key_src else ""
                _tail = "暂存为兜底并**继续**尝试后续步骤" if more_steps else "暂存为兜底（候选链已走完）"
                print(f"  [V2] 候选未通过完整性校验（解出的图不完整："
                      f"{_describe_incompleteness(mime)}），"
                      f"{_tail}: {os.path.basename(dec_path)}{_tag}{_key}", flush=True)

            def _serve_incomplete_fallback():
                """链走完仍没有 `True`/`None` 的候选 ⇒ 回退返回暂存的"最大"那个。

                **绝不 404 / 415 / 500**：源图本身就被截断的图（真实缓存里 7%）用户本来就能
                看到，不许因为"校验不过"而变成读不出来。宁可给一张坏的，也不能不给。
                """
                cand = _pick_incomplete_candidate(_incomplete_candidates)
                if not cand:
                    return None
                try:
                    _size = os.path.getsize(cand['path'])
                except OSError:
                    return None
                print(f"  [V2] 所有候选都未通过完整性校验（暂存 {len(_incomplete_candidates)} 个），"
                      f"回退返回其中最大的一个: {os.path.basename(cand['path'])} "
                      f"({cand['why']}, {_size} 字节, 源={cand.get('source_tag') or 'cache'}"
                      f"{('，密钥来源=' + cand['key_src']) if cand.get('key_src') else ''}"
                      f") —— 不 404",
                      flush=True)
                resp = send_file(os.path.abspath(cand['path']), mimetype=cand['mime'],
                                 max_age=86400)
                resp.headers['X-WeChat-Image-Completeness'] = 'incomplete'
                # 既有 `X-WeChat-Image-Source` 的语义（缩略图标记）不变；只在它本来为空时补上来源。
                resp.headers['X-WeChat-Image-Source'] = (cand.get('source_tag')
                                                         or 'v2-incomplete-fallback')
                return resp

            def _serve_decrypted(dec_path, source_tag=None, key_src=None):
                """按文件头给出正确 MIME 后返回响应；不是图片则返回 None。

                完整性校验（issue #46）：
                  * `True`（可判定类型且尾部完整）/ `None`（不可判定）⇒ **立刻返回**（与旧行为一致）；
                  * `False`（可判定类型且确认缺尾）⇒ **不返回**，暂存为最后兜底并返回 None，
                    让候选链继续 —— **这正是让 MMKV 重新派生有机会接管、从而纠正缓存的那一步**。
                """
                try:
                    with open(dec_path, 'rb') as f:
                        head = f.read(16)
                except OSError:
                    return None
                mime = _sniff_image_mime(head)
                if mime == 'application/octet-stream':
                    return None
                # 只有"可判定类型"才值得去做校验：不可判定（WEBP/BMP/其它）连
                # `_image_completeness` 都不调用 ⇒ 这条路上**零额外 IO**、与旧行为逐字一致。
                if (mime in _IMAGE_DECIDABLE_MIMES
                        and _image_completeness(dec_path, mime) is False):
                    _stash_incomplete(dec_path, mime, source_tag, key_src=key_src)
                    return None
                resp = send_file(os.path.abspath(dec_path), mimetype=mime,
                                 max_age=0 if source_tag else 86400)
                if source_tag:
                    resp.headers['X-WeChat-Image-Source'] = source_tag
                return resp

            def _try_v2_decrypt(key_bytes, xor_val, src=None, source_tag=None, key_src=None):
                """Try to decrypt and return a Flask response or None."""
                if key_bytes is None:
                    return None
                cache_dir = os.path.join(os.path.dirname(decrypted_dir), 'decrypted_media')
                dec_path = _decrypt_dat_v2(src or resolved, key_bytes, xor_val, cache_dir)
                if not dec_path or not os.path.isfile(dec_path):
                    return None
                try:
                    with open(dec_path, 'rb') as f:
                        head = f.read(16)
                except OSError:
                    return None
                if head[:4] == b'wxgf':
                    # 解密成功但内容是微信私有 wxgf：记住密钥，稍后用小图兜底
                    _found_key['aes'] = key_bytes
                    _found_key['xor'] = xor_val
                    return None
                return _serve_decrypted(dec_path, source_tag, key_src=key_src)

            # Collect md5 variants to try (base + _h thumbnail)
            # NOTE: CDN md5 bridge and message-DB aeskey search are intentionally
            # removed. CDN aeskeys from XML <img aeskey="..."> have been confirmed
            # to NEVER decrypt local .dat files. Only memory-extracted keys
            # (from harvest-keys) can decrypt V2 files.
            _md5_variants = []
            if md5_val and len(re.sub(r'_[th]$', '', md5_val)) == 32:
                _md5_variants = [md5_val]
                # Add common WeChat thumbnail suffixes (_h, _t) and strip suffix for lookup
                for _suffix in ('_h', '_t'):
                    if md5_val.endswith(_suffix):
                        _md5_variants.append(md5_val[:-2])
                        break
                else:
                    _md5_variants.append(md5_val + '_h')
                    _md5_variants.append(md5_val + '_t')

                # 1) Try cached key map (keys from memory extraction via harvest-keys)
                key_map = _load_or_build_image_key_map(decrypted_dir)
                for _try_md5 in _md5_variants:
                    entry = key_map.get(_try_md5)
                    if entry:
                        # 把"这把密钥是哪来的"带进日志（issue #16 新评论）：
                        # `cache:legacy` = 老版本写的、来源不明；`cache:derived` = 派生真值。
                        rv = _try_v2_decrypt(entry['aes'], entry['xor'],
                                             key_src='cache:' + str(entry.get('xor_src')
                                                                    or 'legacy'))
                        if rv:
                            return rv

                # 2) Try MMKV-based local key derivation (py_wx_key approach)
                # Derives keys OFFLINE from %APPDATA%\Tencent\xwechat\**\kvcomm\
                # key_N_.statistic files — no WeChat process or memory scan needed.
                from engine.services.v2_key_extract import extract_keys_from_mmkv
                try:
                    mmkv_keys = extract_keys_from_mmkv(decrypted_dir, wxid)
                    # `extract_keys_from_mmkv` 的**发现语义没变**：返回的仍然只是"本次新发现的
                    # md5 集合"。但它在**离线**状态下还会顺手把缓存里写坏的 XOR 就地纠正
                    # （known-issues #46 残留：微信没在跑也要能自愈），这时返回值是**空集合**
                    # （稳态下本来就没有新文件可发现）⇒ 不能只看 `mmkv_keys` 的真假：
                    # 否则内存里那份**旧的** key_map（带着那条错 XOR）会继续赢，
                    # 整个进程都看不到修复结果（下一次请求又会拿旧值去解）。
                    if mmkv_keys or getattr(mmkv_keys, 'cache_repaired', 0):
                        # extract_keys_from_mmkv already caches to _media_keys.json
                        # Invalidate in-memory cache so reload picks up new keys
                        global _IMAGE_KEY_MAP, _IMAGE_KEY_MAP_DIR
                        _IMAGE_KEY_MAP = {}
                        _IMAGE_KEY_MAP_DIR = None
                        key_map = _load_or_build_image_key_map(decrypted_dir)
                        for _try_md5 in _md5_variants:
                            entry = key_map.get(_try_md5)
                            if entry:
                                rv = _try_v2_decrypt(entry['aes'], entry['xor'],
                                                     key_src='mmkv:' + str(
                                                         entry.get('xor_src') or 'legacy'))
                                if rv:
                                    return rv
                except Exception as e:
                    print(f"  [V2] MMKV key extraction failed: {e}")

                # 2b) Account-key fallback: V2 keys are per-account, not per-image.
                # If we have ANY cached key but this specific MD5 isn't in the map,
                # try the account-level key directly — it should decrypt ALL V2 files.
                if key_map and not any(key_map.get(m) for m in _md5_variants):
                    fallback_entry = next(iter(key_map.values()))
                    rv = _try_v2_decrypt(fallback_entry['aes'], fallback_entry['xor'],
                                         key_src='account-cache:' + str(
                                             fallback_entry.get('xor_src') or 'legacy'))
                    if rv:
                        return rv

                # 3) Try live memory extraction from running WeChat
                #    ⚠️ 可观测性（issue #16 新评论）：这一段原本**完全静默**
                #    （`find_keys_for_files` 的 `print_fn` 没传 ⇒ 默认是个空函数），
                #    而它是这条链路里唯一可能跑几十秒的一步 ⇒ 用户只看到"图片解密异常漫长"
                #    却没有任何日志能指向它。现在把它的日志接出来 + 打印耗时。
                from engine.services.v2_key_extract import find_keys_for_files, is_wechat_running
                if _found_key['aes']:
                    print("  [V2] 已成功解密 wxgf：跳过重复内存扫描，尝试同目录高清图", flush=True)
                elif not is_wechat_running():
                    print("  [V2] 内存密钥路径：微信未运行 ⇒ 跳过（这一步需要微信在跑）",
                          flush=True)
                else:
                    def _mem_log(*_a, **_kw):
                        _kw.setdefault('flush', True)
                        print(*_a, **_kw)

                    import time as _time
                    _mem_t0 = _time.time()
                    print(f"  [V2] 内存密钥路径：开始扫描（变体 {len(_md5_variants)} 个）"
                          f"—— 这一步可能耗时数十秒", flush=True)
                    _mem_found_any = False
                    for _try_md5 in _md5_variants:
                        found = find_keys_for_files(decrypted_dir, wxid, [_try_md5],
                                                    print_fn=_mem_log)
                        if _try_md5 in found:
                            _mem_found_any = True
                            # 尾部 XOR 必须用**派生真值**（`code & 0xFF`）；只有实在拿不到
                            # 派生值时，才回退到既有默认值。写死默认值 ⇒ 凡是
                            # `code & 0xFF != 0xC9` 的账号，图只有上面一小部分能显示、
                            # 其余是纯色/垃圾（issue #16 症状 2：XOR 只作用文件尾部）。
                            _mem_xor = getattr(found, 'derived_xor', None)
                            if _mem_xor is None:
                                _mem_xor = _DAT_V2_DEFAULT_XOR
                                # 只在**异常情况**（手上没有派生值）打日志：这样日志里
                                # "NOT a derived value" 才真的等于"这次不是按账号派生的"。
                                print(f"  [V2] 内存找到密钥但无派生 XOR —— 尾部按默认值 "
                                      f"0x{_mem_xor:02X} 解（NOT a derived value）")
                            rv = _try_v2_decrypt(found[_try_md5], _mem_xor,
                                                 key_src='memory')
                            if rv:
                                return rv
                    print(f"  [V2] 内存密钥路径：结束（{_time.time() - _mem_t0:.1f}s，"
                          f"{'取到密钥' if _mem_found_any else '未取到密钥'}）", flush=True)

            # 原版本不能解码时，按高清 _h → 普通图 → 小缩略图 _t 尝试。
            for thumb_src in _sibling_image_variants(resolved):
                cache_dir = os.path.join(os.path.dirname(decrypted_dir), 'decrypted_media')
                key_pairs = []
                if _found_key['aes']:
                    key_pairs.append((_found_key['aes'], _found_key['xor']))
                for _m in _md5_variants:
                    _e = key_map.get(_m) if key_map else None
                    if _e:
                        key_pairs.append((_e['aes'], _e.get('xor')))
                if key_map:
                    _fe = next(iter(key_map.values()))
                    key_pairs.append((_fe['aes'], _fe.get('xor')))
                for _aes, _xk in key_pairs:
                    try:
                        _dec = _decrypt_dat_v2(thumb_src, _aes, _xk, cache_dir)
                    except Exception:
                        _dec = None
                    if _dec and os.path.isfile(_dec):
                        _source = ('hd-image' if os.path.splitext(thumb_src)[0].endswith('_h')
                                   else 'thumbnail' if os.path.splitext(thumb_src)[0].endswith('_t')
                                   else 'image')
                        _rv = _serve_decrypted(_dec, source_tag=_source,
                                              key_src='thumbnail')
                        if _rv:
                            print(f"  [WXGF] 已使用同目录替代图（{_source}）: {os.path.basename(thumb_src)}")
                            return _rv

            # 4) Thumbnail cache fallback — WeChat stores decrypted thumbnails
            local_id = media_info.get('local_id', 0) if media_info else 0
            thumb = _find_cached_thumbnail(decrypted_dir, md5_val, local_id, wxid)
            if thumb and os.path.isfile(thumb):
                try:
                    with open(thumb, 'rb') as _tf:
                        _thumb_mime = _sniff_image_mime(_tf.read(16))
                except OSError:
                    _thumb_mime = 'application/octet-stream'
                # 与上面同一条语义：只跳过"可判定类型且**确认**缺尾"的缩略图；
                # 不可判定（WEBP/BMP/读不到）⇒ 保持旧行为，直接返回（连校验都不调用）。
                if (_thumb_mime in _IMAGE_DECIDABLE_MIMES
                        and _image_completeness(thumb, _thumb_mime) is False):
                    _stash_incomplete(thumb, _thumb_mime, 'thumbnail-cache', more_steps=False)
                else:
                    mime, _ = mimetypes.guess_type(thumb)
                    resp = send_file(thumb, mimetype=mime or 'image/jpeg', max_age=0)
                    resp.headers['X-WeChat-Image-Source'] = 'thumbnail-cache'
                    return resp

            # 4b) issue #46：候选链走完仍**没有**任何通过校验的候选
            #     ⇒ 回退返回暂存的"最大"那个未校验候选。绝不 404 / 415 / 500：
            #     真实缓存里 2/28 个 JPEG 是"源图本身就被截断"，用户本来就能显示它们。
            _rv_incomplete = _serve_incomplete_fallback()
            if _rv_incomplete:
                return _rv_incomplete

            # Diagnostic: report why this image failed
            diag_parts = [os.path.basename(resolved)]
            if md5_val and len(md5_val) == 32:
                diag_parts.append(f'md5={md5_val[:8]}...')
                diag_parts.append('steps:key+mmkv')
                try:
                    _v2_check_wx = is_wechat_running
                except NameError:
                    _v2_check_wx = None
                if _v2_check_wx and _v2_check_wx():
                    diag_parts.append('+mem')
            elif md5_val:
                diag_parts.append(f'md5-short({len(md5_val)})')
            else:
                diag_parts.append('no-md5')
            if not thumb:
                diag_parts.append('no-thumb')
            elif not os.path.isfile(thumb):
                diag_parts.append('thumb-miss')
            print(f"  [V2 IMG FAIL] {' | '.join(diag_parts)}")

            # Provide actionable error message
            from engine.services.v2_key_extract import is_wechat_running as _wx_running
            if _found_key['aes'] and not wxgf_supported():
                # 密钥没问题，只是本机缺少 wxgf(H.265) 解码器、且没有可用缩略图
                abort(415, description='该图片是微信 wxgf(H.265) 格式：请把 ffmpeg.exe 放到程序目录的 '
                                       'tools\\ 下（或在微信中打开该图片后再刷新），即可显示原图')
            if _wx_running():
                abort(415, description='V2加密图片，请在微信中查看该图片后刷新重试')
            else:
                abort(415, description='V2加密图片 — 本地密钥推导未匹配，请启动微信浏览该图片后刷新')

        # V1: fixed AES key — decryptable
        if version == 1:
            cache_dir = os.path.join(os.path.dirname(decrypted_dir), 'decrypted_media')
            dec_path = _decrypt_dat_v1(resolved, cache_dir)
            if dec_path and os.path.isfile(dec_path):
                try:
                    with open(dec_path, 'rb') as _f:
                        _head = _f.read(16)
                except OSError:
                    _head = b''
                _mime = _sniff_image_mime(_head)
                if _mime != 'application/octet-stream':
                    return send_file(dec_path, mimetype=_mime, max_age=86400)

        # V0: XOR encryption — try known keys
        xor_key, ext = _detect_dat_xor_key(resolved)
        if xor_key is not None:
            cache_dir = os.path.join(os.path.dirname(decrypted_dir), 'decrypted_media')
            dec_path = _decrypt_dat_file(resolved, xor_key, cache_dir)
            if dec_path and os.path.isfile(dec_path):
                try:
                    with open(dec_path, 'rb') as _f:
                        _head = _f.read(16)
                except OSError:
                    _head = b''
                _mime = _sniff_image_mime(_head)
                if _mime != 'application/octet-stream':
                    return send_file(dec_path, mimetype=_mime, max_age=86400)

        # Serve raw (might be non-encrypted .dat) — 但绝不把 wxgf 当图片返回
        try:
            with open(resolved, 'rb') as _f:
                _head = _f.read(16)
        except OSError:
            _head = b''
        if _head[:4] == b'wxgf':
            abort(415, description='该图片是微信 wxgf(H.265) 格式，当前环境无法解码：'
                                   '请把 ffmpeg.exe 放到程序目录的 tools\\ 下后重试')
        _mime = _sniff_image_mime(_head)
        if _mime != 'application/octet-stream':
            return send_file(resolved, mimetype=_mime, max_age=86400)
        mime, _ = mimetypes.guess_type(resolved)
        return send_file(resolved, mimetype=mime or 'application/octet-stream',
                         max_age=86400)

    mime, _ = mimetypes.guess_type(resolved)
    return send_file(resolved, mimetype=mime or 'application/octet-stream',
                     max_age=86400)


# --- Emoji / Sticker AES-128-CBC decryption ---


def decrypt_emoticon_aes_cbc(data: bytes, aes_key_hex: str):
    """Decrypt WeChat emoticon/sticker payload using AES-128-CBC.

    Scheme (observed in WeChat 4.x):
      - Key = bytes.fromhex(aes_key_hex)  (16 bytes, typically the emoji MD5)
      - IV  = key
      - Cipher = AES-128-CBC
      - Padding = PKCS7

    Returns decrypted bytes or None on failure.
    """
    if not data or len(data) % 16 != 0:
        return None

    khex = str(aes_key_hex or '').strip().lower()
    if len(khex) != 32 or not all(c in '0123456789abcdef' for c in khex):
        return None

    try:
        key = bytes.fromhex(khex)
    except Exception:
        return None

    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Padding
        pt_padded = AES.new(key, AES.MODE_CBC, iv=key).decrypt(data)
        return Padding.unpad(pt_padded, AES.block_size)
    except Exception:
        return None


def get_voice_wav_path(decrypted_dir: str, voice_path: str = None, create_time=None,
                       local_id=None, db_dir: str = None, chat: str = None):
    """取某条语音的 WAV 路径（缓存优先；必要时从 media 分片提取并转码）。

    供商业 ASR（百度等）复用，避免重复实现一遍查找逻辑。
    """
    if not decrypted_dir:
        return None
    decrypted_dir = os.path.abspath(decrypted_dir)
    filename = os.path.basename(voice_path or "")
    cache_name = _voice_cache_filename(voice_path or "", create_time, local_id)
    names = [n for n in (filename, cache_name) if n]
    search_dirs = [
        os.path.join(decrypted_dir, "media", "voice"),
        os.path.join(os.path.dirname(decrypted_dir), "voice"),
    ]
    for d in search_dirs:
        for n in names:
            wav = os.path.splitext(os.path.join(d, n))[0] + ".wav"
            if os.path.isfile(wav) and os.path.getsize(wav) > 0:
                return wav
    silk = None
    for d in search_dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and p.lower().endswith(".silk"):
                silk = p
                break
        if silk:
            break
    if not silk and create_time is not None and local_id is not None:
        silk = _extract_voice_from_db(decrypted_dir, create_time, local_id,
                                      db_dir=db_dir, chat=chat,
                                      cache_key=cache_name or None)
    if not silk:
        return None
    wav = os.path.splitext(silk)[0] + ".wav"
    if os.path.isfile(wav) and os.path.getsize(wav) > 0:
        return wav
    converted = _silk_to_wav(silk, wav)
    return converted if converted and os.path.isfile(converted) else None

def serve_voice(decrypted_dir: str, voice_path: str,
                create_time: int = None, local_id: int = None,
                db_dir: str = None, chat: str = None):
    """Flask response: serve a voice file (SILK or converted WAV).

    Searches cached voice directories first. If the file isn't found and
    create_time+local_id are provided, extracts the voice from the VoiceInfo
    table —— 遍历所有 media_*.db 分片（含回源按需解密）。
    """
    if not voice_path:
        abort(404)

    filename = os.path.basename(voice_path)
    cache_name = _voice_cache_filename(voice_path, create_time, local_id)
    lookup_names = [n for n in (filename, cache_name) if n]

    # Search both new and old voice cache locations
    search_dirs = [
        os.path.join(decrypted_dir, "media", "voice"),          # migrator + new cache
        os.path.join(os.path.dirname(decrypted_dir), "voice"),  # old _resolve_voice_path cache
    ]

    silk_file = None
    wav_file = None
    for d in search_dirs:
        for name in lookup_names:
            candidate_silk = os.path.join(d, name)
            candidate_wav = os.path.splitext(candidate_silk)[0] + '.wav'
            if os.path.isfile(candidate_wav) and os.path.getsize(candidate_wav) > 0:
                wav_file = candidate_wav
                break
            if os.path.isfile(candidate_silk):
                silk_file = candidate_silk
                wav_file = candidate_wav
                break
        if silk_file or wav_file:
            break

    # Fallback: extract from VoiceInfo tables on-the-fly (all media shards)
    if not silk_file and not wav_file and create_time is not None and local_id is not None:
        silk_file = _extract_voice_from_db(decrypted_dir, create_time, local_id,
                                           db_dir=db_dir, chat=chat,
                                           cache_key=cache_name or None)
        if silk_file and os.path.isfile(silk_file):
            wav_file = os.path.splitext(silk_file)[0] + '.wav'

    if wav_file and not silk_file and os.path.isfile(wav_file):
        return send_file(os.path.abspath(wav_file), mimetype='audio/wav')

    if silk_file:
        wav_path = _silk_to_wav(silk_file, wav_file)
        if wav_path and os.path.isfile(wav_path):
            return send_file(os.path.abspath(wav_path), mimetype='audio/wav')
        print(f"  [WARN] SILK→WAV conversion failed for: {silk_file}")
        return send_file(os.path.abspath(silk_file), mimetype='application/octet-stream',
                         as_attachment=True, download_name=os.path.basename(silk_file))

    abort(404)


def _voice_cache_filename(voice_path: str, create_time=None, local_id=None) -> str:
    """给语音缓存文件起名。

    前端传来的 path 经常不是文件名，而是 msg_content 的十六进制 blob，
    这时直接用 blob 当缓存键，第二次请求就能命中缓存。
    """
    base = os.path.basename(voice_path or "")
    if len(base) >= 20 and all(c in "0123456789abcdefABCDEF" for c in base):
        # 前端传的常是 msg_content 的十六进制 blob（几百字符），
        # 直接当文件名会超出 Windows 260 字符路径上限，改用哈希
        return _hashlib.md5(base.encode("utf-8", "ignore")).hexdigest() + ".silk"
    if create_time and local_id:
        return "%s_%s.silk" % (create_time, local_id)
    return ""


def _media_db_search_order(decrypted_dir: str, db_dir: str = None):
    """语音查询要遍历的 media_*.db 顺序。

    微信 4.x 把语音分散在多个 media_*.db 分片（最近的往往在 media_1.db），
    所以必须逐个分片查；解密副本比源库旧时，再按需解密源库分片补查。
    """
    ordered = []
    msg_dir = os.path.join(decrypted_dir, "message")
    if os.path.isdir(msg_dir):
        for name in sorted(os.listdir(msg_dir)):
            if name.startswith("media_") and name.endswith(".db"):
                ordered.append(os.path.join(msg_dir, name))
    if not db_dir or not os.path.isdir(os.path.join(db_dir, "message")):
        return ordered

    src_msg = os.path.join(db_dir, "message")
    stale = []
    for name in sorted(os.listdir(src_msg)):
        if not (name.startswith("media_") and name.endswith(".db")):
            continue
        src = os.path.join(src_msg, name)
        dec = os.path.join(msg_dir, name)
        try:
            src_mtime = os.path.getmtime(src)
            dec_mtime = os.path.getmtime(dec) if os.path.isfile(dec) else 0
        except OSError:
            continue
        if dec_mtime >= src_mtime:
            continue  # 解密副本不比源库旧，无需重复解密
        stale.append((src_mtime, src))
    stale.sort(reverse=True)
    for _mtime, src in stale:
        dec = _decrypt_media_db_on_the_fly(src, decrypted_dir)
        if dec and os.path.isfile(dec):
            ordered.append(dec)
    return ordered


def _query_voice_blob(media_db: str, create_time: int, local_id: int,
                      chat: str = None):
    """在单个 media_*.db 里取语音数据（给了会话名则优先精确匹配）。"""
    import sqlite3 as _sqlite3
    if not os.path.isfile(media_db):
        return None
    try:
        conn = _sqlite3.connect("file:%s?mode=ro" % media_db, uri=True)
    except _sqlite3.Error:
        try:
            conn = _sqlite3.connect(media_db)
        except _sqlite3.Error:
            return None
    try:
        if chat:
            try:
                row = conn.execute(
                    "SELECT v.voice_data FROM VoiceInfo v "
                    "JOIN Name2Id n ON n.rowid = v.chat_name_id "
                    "WHERE n.user_name = ? AND v.create_time = ? AND v.local_id = ?",
                    (chat, create_time, local_id)).fetchone()
                if row and isinstance(row[0], bytes) and len(row[0]) >= 10:
                    return row[0]
            except _sqlite3.Error:
                pass
        row = conn.execute(
            "SELECT voice_data FROM VoiceInfo WHERE create_time=? AND local_id=?",
            (create_time, local_id)).fetchone()
        return row[0] if row else None
    except _sqlite3.Error:
        return None
    finally:
        conn.close()


def _extract_voice_from_db(decrypted_dir: str, create_time: int,
                           local_id: int, db_dir: str = None,
                           chat: str = None, cache_key: str = None) -> str:
    """从 media_*.db 的 VoiceInfo 中取出语音并缓存成 .silk。

    遍历**所有** media_*.db 分片（旧实现只查 media_0.db，导致放在
    media_1.db 里的语音播放/转写一律 404），解密副本过期时回源按需解密。
    """
    if create_time is None or local_id is None:
        return None
    for media_db in _media_db_search_order(decrypted_dir, db_dir):
        blob = _query_voice_blob(media_db, create_time, local_id, chat)
        if not isinstance(blob, bytes) or len(blob) < 10:
            continue
        output_dir = os.path.join(decrypted_dir, "media", "voice")
        os.makedirs(output_dir, exist_ok=True)
        name = cache_key or _voice_cache_filename("", create_time, local_id)
        silk_file = os.path.join(output_dir, name)
        if not os.path.isfile(silk_file):
            with open(silk_file, "wb") as f:
                f.write(blob)
        return silk_file
    return None


def _decrypt_media_db_on_the_fly(src_db: str, decrypted_dir: str) -> str:
    """Decrypt a single media_*.db from WeChat source to the decrypted message dir.

    Returns path to decrypted file, or None.
    """
    import sqlite3 as _sqlite3
    try:
        from engine.decrypt import decrypt_database
        from backup.decryptor import resolve_key_for
    except ImportError:
        return None

    # salt 优先（多账号同名库靠 salt 区分）；没有可用密钥就返回 None，绝不拿错密钥硬试
    key = resolve_key_for(src_db)
    if key is None:
        return None

    basename = os.path.basename(src_db)
    dst = os.path.join(decrypted_dir, "message", basename)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        ok = decrypt_database(src_db, dst, key)
        return dst if ok else None
    except Exception:
        return None


def _find_silk_decoder():
    """定位 silk_decoder.exe：PyInstaller 解包目录优先，其次项目 tools/。"""
    bundle_dir = getattr(sys, '_MEIPASS', None)
    if bundle_dir:
        candidate = os.path.join(bundle_dir, 'tools', 'silk_decoder.exe')
        if os.path.isfile(candidate):
            return candidate
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    candidate = os.path.join(project_root, 'tools', 'silk_decoder.exe')
    return candidate if os.path.isfile(candidate) else None


def silk_to_pcm(silk_path: str):
    """SILK → 16-bit 单声道 PCM 字节；失败返回 None（不抛异常、不打印）。

    批量导出会调用成千上万次，所以这里保持"安静"：失败由调用方汇总成缺失清单。
    """
    decoder = _find_silk_decoder()
    if not decoder or not silk_path or not os.path.isfile(silk_path):
        return None
    pcm_path = silk_path + '.pcm'
    try:
        result = subprocess.run([decoder, silk_path, pcm_path],
                                capture_output=True, timeout=30)
        if result.returncode != 0 or not os.path.isfile(pcm_path):
            return None
        with open(pcm_path, 'rb') as f:
            data = f.read()
        return data or None
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        if os.path.isfile(pcm_path):
            try:
                os.remove(pcm_path)
            except OSError:
                pass


def _silk_to_wav(silk_path: str, wav_path: str, sample_rate: int = 24000) -> str:
    """Convert a SILK V3 file to WAV using standalone decoder. Returns WAV path or None.

    采样率默认 24000：本机实测微信语音解码出的 PCM 就是 24 kHz（8/8 条与 XML
    voicelength 吻合），因此既有行为的默认值保持不变。
    """
    pcm_data = silk_to_pcm(silk_path)
    if not pcm_data:
        print(f"  [WARN] SILK 解码失败或解码器缺失: {silk_path}")
        return None
    _write_wav(wav_path, pcm_data, sample_rate)
    return wav_path


def _voice_content_hash(path: str) -> str:
    """语音内容的哈希（用于转写结果去重：同一段语音在任何备份/文件名下都命中）。"""
    import hashlib
    try:
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()[:16]
    except OSError:
        return ''


def locate_voice_file(decrypted_dir: str, voice_path: str, create_time: int = None,
                      local_id: int = None, db_dir: str = None, chat: str = None):
    """定位语音 SILK 文件（缓存目录 → 相邻 voice 目录 → 各 media_*.db 分片按需提取）。

    抽成公开函数是为了让"回传本地识别结果"（issue #24 第③步）也能用同一套定位算内容哈希。
    """
    filename = os.path.basename(voice_path or '')
    cache_name = _voice_cache_filename(voice_path, create_time, local_id)
    lookup_names = [n for n in (filename, cache_name) if n]
    for d in (os.path.join(decrypted_dir, "media", "voice"),
              os.path.join(os.path.dirname(decrypted_dir), "voice")):
        for name in lookup_names:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                return candidate
    return _extract_voice_from_db(decrypted_dir, create_time, local_id,
                                  db_dir=db_dir, chat=chat, cache_key=cache_name or None)


def transcribe_voice_cached(decrypted_dir: str, voice_path: str, create_time: int = None,
                            local_id: int = None, db_dir: str = None, chat: str = None,
                            use_cache: bool = True, refresh: bool = False) -> dict:
    """带缓存的转写（issue #24）。

    先按**语音内容哈希**查存储：命中直接返回（`cached=True`，不再跑模型）；
    未命中才识别，识别成功且文字非空时写入存储。`refresh=True` 强制重识别并覆盖。
    返回 {'text', 'cached'}；识别失败/为空时文本为空串，且**不写库**。
    """
    silk_file = locate_voice_file(decrypted_dir, voice_path, create_time,
                                   local_id, db_dir=db_dir, chat=chat)

    store = None
    vhash = _voice_content_hash(silk_file) if silk_file else ''
    if use_cache:
        try:
            from .asr_store import AsrStore
            store = AsrStore()
        except Exception:
            store = None
    if store and vhash and not refresh:
        hit = store.get(vhash)
        if hit:
            return {'text': hit['text'], 'cached': True}

    text = transcribe_voice(decrypted_dir, voice_path, create_time=create_time,
                            local_id=local_id, db_dir=db_dir, chat=chat)
    if store and vhash and text and text.strip():
        duration_s = 0.0
        try:
            if silk_file:
                wav_file = os.path.splitext(silk_file)[0] + '.wav'
                if os.path.isfile(wav_file):
                    duration_s = round(os.path.getsize(wav_file) / 2.0 / 24000.0, 3)
        except OSError:
            duration_s = 0.0
        store.put(vhash, text, chat_id=chat or '', create_time=create_time or 0,
                  local_id=local_id or 0, duration_s=duration_s)
    if store:
        store.close()
    return {'text': text, 'cached': False}


def transcribe_voice(decrypted_dir: str, voice_path: str, create_time: int = None,
                     local_id: int = None, db_dir: str = None, chat: str = None) -> str:
    """Convert voice SILK to WAV and transcribe using available speech recognition.

    和 serve_voice 一样：缓存目录找不到时，会去所有 media_*.db 分片里
    按 (create_time, local_id) 取语音（含回源按需解密）。

    Returns the transcription text, or raises ValueError with a user-friendly message.
    """
    if not voice_path:
        raise ValueError('voice_path required')

    filename = os.path.basename(voice_path)
    cache_name = _voice_cache_filename(voice_path, create_time, local_id)
    lookup_names = [n for n in (filename, cache_name) if n]

    search_dirs = [
        os.path.join(decrypted_dir, "media", "voice"),
        os.path.join(os.path.dirname(decrypted_dir), "voice"),
    ]

    silk_file = None
    for d in search_dirs:
        for name in lookup_names:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                silk_file = candidate
                break
        if silk_file:
            break

    if not silk_file:
        silk_file = _extract_voice_from_db(decrypted_dir, create_time, local_id,
                                           db_dir=db_dir, chat=chat,
                                           cache_key=cache_name or None)

    if not silk_file:
        raise ValueError('语音文件不存在')

    wav_file = os.path.splitext(silk_file)[0] + '.wav'

    if not os.path.isfile(wav_file) or os.path.getsize(wav_file) == 0:
        wav_path = _silk_to_wav(silk_file, wav_file)
        if not wav_path or not os.path.isfile(wav_path):
            raise ValueError('语音解码失败')

    return _transcribe_wav(wav_file)


# Cache whisper model across requests
_whisper_model = None
_whisper_model_name = None


def _get_whisper_model(model_name: str = 'base'):
    """Load and cache a Whisper model. Uses 'base' for good Chinese accuracy/speed balance."""
    global _whisper_model, _whisper_model_name
    if _whisper_model is not None and _whisper_model_name == model_name:
        return _whisper_model

    try:
        import whisper
        _whisper_model = whisper.load_model(model_name)
        _whisper_model_name = model_name
        return _whisper_model
    except ImportError:
        return None
    except Exception:
        return None


def _transcribe_wav(wav_path: str) -> str:
    """Transcribe a WAV file using Whisper (openai-whisper) or WhisperX."""
    errors = []

    # Try openai-whisper first
    try:
        model = _get_whisper_model('base')
        if model is None:
            raise ImportError('whisper not available')
        result = model.transcribe(wav_path, language='zh', fp16=False)
        text = result.get('text', '').strip()
        if text:
            return text
    except ImportError:
        errors.append('openai-whisper 未安装')
    except Exception as e:
        errors.append(f'whisper 识别失败: {e}')

    # Try WhisperX as fallback
    try:
        import whisperx
        import gc
        device = 'cpu'
        model = whisperx.load_model('base', device, compute_type='int8')
        audio = whisperx.load_audio(wav_path)
        result = model.transcribe(audio, language='zh', batch_size=1)
        text = ' '.join(s.get('text', '') for s in result.get('segments', [])).strip()
        # Clean up to free memory
        gc.collect()
        if text:
            return text
    except ImportError:
        errors.append('whisperx 未安装')
    except Exception as e:
        errors.append(f'whisperx 识别失败: {e}')

    if errors:
        raise ValueError(
            '语音识别失败:\n' +
            '\n'.join(f'  - {e}' for e in errors) +
            '\n\n请安装 Whisper:\n  pip install openai-whisper'
        )
    else:
        raise ValueError('未识别到语音内容')


def _write_wav(wav_path: str, pcm_data: bytes, sample_rate: int = 24000):
    """Write raw 16-bit mono PCM data to a WAV file."""
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm_data)

    with open(wav_path, 'wb') as f:
        # RIFF header
        f.write(b'RIFF')
        f.write((36 + data_size).to_bytes(4, 'little'))
        f.write(b'WAVE')
        # fmt chunk
        f.write(b'fmt ')
        f.write((16).to_bytes(4, 'little'))
        f.write((1).to_bytes(2, 'little'))  # PCM
        f.write(num_channels.to_bytes(2, 'little'))
        f.write(sample_rate.to_bytes(4, 'little'))
        f.write(byte_rate.to_bytes(4, 'little'))
        f.write(block_align.to_bytes(2, 'little'))
        f.write(bits_per_sample.to_bytes(2, 'little'))
        # data chunk
        f.write(b'data')
        f.write(data_size.to_bytes(4, 'little'))
        f.write(pcm_data)
