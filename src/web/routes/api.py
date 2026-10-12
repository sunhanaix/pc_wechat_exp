"""REST API endpoints."""
import os
import re
import sys
from flask import Blueprint, request, jsonify, current_app, make_response

# Ensure src/ is on path for engine imports
_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from engine.services.chat import get_contacts
from engine.services.message import query_messages, query_message_detail, get_chat_stats, get_chat_dates
from engine.services.media import serve_media, serve_hardlink_media, serve_voice, transcribe_voice, transcribe_voice_cached, decrypt_emoticon_aes_cbc
from engine.services.address_book import (get_all_contacts, get_all_groups,
                                          filter_contacts, distinct_labels)
from engine.services.contact_extra import load_labels
from engine.services.name_resolver import _find_contact_db

api_bp = Blueprint('api', __name__)


def _cfg():
    return (current_app.config.get('DECRYPTED_DIR', ''),
            current_app.config.get('WXID'),
            current_app.config.get('DB_DIR'))


# 深链定位参数：严格非负整数。`request.args.get(..., type=int)` **不能**用 ——
# Flask 的 `type=int` 在解析失败时**静默回退默认值**（`'abc'` → None），
# 那正是本 issue 要根除的"静默"。
_FOCUS_INT_RE = re.compile(r'^[0-9]+$')


def _focus_nonneg_int(name: str, raw: str) -> int:
    """`focus_*` 参数解析：非非负整数 → `ValueError(name)`（调用方转 400）。"""
    s = (raw or '').strip()
    if not _FOCUS_INT_RE.match(s):
        raise ValueError(name)
    return int(s)


@api_bp.route('/contacts')
def contacts():
    decrypted_dir, wxid, _ = _cfg()
    q = request.args.get('q', '').strip().lower()
    all_contacts = get_contacts(decrypted_dir, wxid)
    if q:
        all_contacts = [c for c in all_contacts
                        if q in c['name'].lower() or q in c['id'].lower()]
    total = len(all_contacts)
    # issue #27：支持分页（limit/offset）。**不传 limit 时返回全部**，保持旧行为，
    # 老前端（聊天页侧边栏）不受影响；新前端可先取一页、滚动到底再取下一页，
    # 避免一次把几千个会话（实测 471KB）全塞进首屏。
    limit = request.args.get('limit', type=int)
    offset = request.args.get('offset', type=int) or 0
    if offset < 0:
        offset = 0
    page = all_contacts
    if limit is not None and limit > 0:
        page = all_contacts[offset:offset + limit]
    return jsonify({'contacts': page, 'total': total, 'offset': offset,
                    'limit': limit, 'returned': len(page)})


@api_bp.route('/messages')
def messages():
    decrypted_dir, wxid, db_dir = _cfg()
    chat_id = request.args.get('chat_id', '')
    if not chat_id:
        return jsonify({'error': 'chat_id required'}), 400
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    if page < 1:
        return jsonify({'error': 'page must be >= 1'}), 400
    if per_page < 1 or per_page > 200:
        return jsonify({'error': 'per_page must be between 1 and 200'}), 400
    # 深链定位（Task 18 / known-issues #29）：两个参数**必须成对**给且都是非负整数。
    # 只给一个、给了非整数 —— 一律 400，**不得**静默忽略（静默忽略的表现就是
    # "点了搜索结果却落在第 1 页"，正是这条 issue 的现象）。
    focus_raw_id = request.args.get('focus_local_id')
    focus_raw_ts = request.args.get('focus_create_time')
    if (focus_raw_id is None) != (focus_raw_ts is None):
        return jsonify({'error': 'focus_local_id 与 focus_create_time 必须成对给出'
                                 '（都是非负整数）'}), 400
    focus_local_id = focus_create_time = None
    if focus_raw_id is not None:
        try:
            focus_local_id = _focus_nonneg_int('focus_local_id', focus_raw_id)
            focus_create_time = _focus_nonneg_int('focus_create_time', focus_raw_ts)
        except ValueError as e:
            return jsonify({'error': '%s 必须是非负整数' % e.args[0]}), 400
    try:
        result = query_messages(
            decrypted_dir, chat_id, wxid=wxid,
            page=page, per_page=per_page,
            start_date=request.args.get('start_date'),
            end_date=request.args.get('end_date'),
            msg_types=request.args.get('type'),
            sender=request.args.get('sender'),
            keyword=request.args.get('keyword'),
            focus_local_id=focus_local_id,
            focus_create_time=focus_create_time,
        )
    except FileNotFoundError as e:
        return jsonify({'error': str(e), 'messages': [], 'pagination': {'page': 1, 'per_page': 50, 'total': 0, 'total_pages': 1}}), 404
    return jsonify(result)


@api_bp.route('/messages/<int:msg_id>')
def message_detail(msg_id):
    decrypted_dir, _, _ = _cfg()
    chat_id = request.args.get('chat_id', '')
    detail = query_message_detail(decrypted_dir, msg_id, chat_id=chat_id)
    if detail is None:
        return jsonify({'error': 'message not found'}), 404
    return jsonify(detail)


@api_bp.route('/chat/<chat_id>/stats')
def chat_stats(chat_id):
    decrypted_dir, wxid, _ = _cfg()
    try:
        return jsonify(get_chat_stats(decrypted_dir, chat_id, wxid=wxid))
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404


@api_bp.route('/chat/<chat_id>/dates')
def chat_dates(chat_id):
    decrypted_dir, _, _ = _cfg()
    try:
        return jsonify(get_chat_dates(decrypted_dir, chat_id))
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404


@api_bp.route('/chat/<chat_id>/group-info')
def group_info(chat_id):
    decrypted_dir, _, _ = _cfg()
    from engine.services.chat import get_group_info
    try:
        info = get_group_info(decrypted_dir, chat_id)
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    if info is None:
        return jsonify({'error': 'not a group chat'}), 400
    return jsonify(info)


@api_bp.route('/media')
def media():
    _, _, db_dir = _cfg()
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'path required'}), 400
    return serve_media(db_dir, path)


@api_bp.route('/hardlink-media')
def hardlink_media():
    """Serve media file resolved via HardLink DB protobuf data.
    Query params: md5, path (local_path from media_info), type (3=image, 43=video, 6=file).
    """
    decrypted_dir, wxid, _ = _cfg()
    media_info = {
        'md5': request.args.get('md5', ''),
        'local_path': request.args.get('path', ''),
        'media_type': request.args.get('type', 0, type=int),
        'file_name': request.args.get('file_name', ''),
        'local_id': request.args.get('local_id', 0, type=int),
        'quality': request.args.get('quality', ''),
    }
    return serve_hardlink_media(decrypted_dir, media_info, wxid)


@api_bp.route('/voice')
def voice():
    """Serve voice file (SILK format, converted to WAV if possible).
    Query params: path (voice_path), create_time (optional int), local_id (optional int).
    When path alone can't find the file, create_time+local_id are used to
    extract voice data from VoiceInfo table on-the-fly.
    """
    decrypted_dir, _, db_dir = _cfg()
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'path required'}), 400
    create_time = request.args.get('create_time', type=int)
    local_id = request.args.get('local_id', type=int)
    return serve_voice(decrypted_dir, path,
                       create_time=create_time, local_id=local_id,
                       db_dir=db_dir, chat=request.args.get('chat') or None)


@api_bp.route('/voice/transcribe')
def voice_transcribe():
    """Transcribe voice to text.
    Query params: path (voice_path from media_info).
    """
    decrypted_dir, _, db_dir = _cfg()
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'path required'}), 400
    try:
        res = transcribe_voice_cached(
            decrypted_dir, path,
            create_time=request.args.get('create_time', type=int),
            local_id=request.args.get('local_id', type=int),
            db_dir=db_dir,
            chat=request.args.get('chat') or None,
            refresh=request.args.get('refresh') in ('1', 'true', 'yes'))
        return jsonify({'text': res['text'], 'cached': bool(res['cached'])})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/voice/transcripts')
def voice_transcripts():
    """GET /api/voice/transcripts?chat=…&items=create_time:local_id,…

    取回该会话里**已经转写并保存过**的语音文字（查看器载入会话时调用）。
    **只读存储、绝不触发识别** —— 没转写过的语音不会出现在返回里。
    """
    chat = request.args.get('chat') or ''
    raw = request.args.get('items') or ''
    items = []
    for part in raw.split(','):
        part = part.strip()
        if ':' not in part:
            continue
        left, right = part.split(':', 1)
        try:
            items.append((int(left), int(right)))
        except ValueError:
            continue
    from engine.services.asr_store import lookup_for_chat
    rows = lookup_for_chat(chat, items)
    return jsonify({'transcripts': rows, 'count': len(rows)})


@api_bp.route('/voice/transcripts', methods=['POST'])
def voice_transcripts_save():
    """POST /api/voice/transcripts —— 前端把本地识别结果回传入库（issue #24）。

    body: {path 或 voice_url, text, chat, create_time, local_id, engine, model}
    只负责"存"：定位语音 → 算内容哈希 → 入库；返回 {saved, hash}。
    """
    decrypted_dir, _, db_dir = _cfg()
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    voice = data.get('path') or data.get('voice_url') or ''
    if not voice or not text:
        return jsonify({'error': 'path and text required'}), 400
    # voice_url 可能带查询串（?path=...&chat=...&create_time=...）
    if '?' in voice:
        from urllib.parse import parse_qs
        qs = parse_qs(voice.split('?', 1)[1])
        voice = (qs.get('path') or [voice.split('?', 1)[0]])[0]
        data.setdefault('chat', (qs.get('chat') or [None])[0])
        data.setdefault('create_time', (qs.get('create_time') or [None])[0])
        data.setdefault('local_id', (qs.get('local_id') or [None])[0])
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    from engine.services.asr_store import save_transcript_for_voice
    try:
        h = save_transcript_for_voice(decrypted_dir, voice, text,
                                      chat=data.get('chat'), create_time=_int(data.get('create_time')),
                                      local_id=_int(data.get('local_id')), db_dir=db_dir,
                                      engine=data.get('engine') or 'local',
                                      model=data.get('model') or '')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'saved': bool(h), 'hash': h})


@api_bp.route('/emoji')
def emoji():
    """Serve emoji/sticker image by MD5, with remote CDN fallback.

    Query params: account (wxid), md5, emoji_url (optional remote CDN URL).
    """
    from flask import redirect, abort

    md5_val = request.args.get('md5', '').strip().lower()
    emoji_url = request.args.get('emoji_url', '').strip()
    account = request.args.get('account', '').strip()

    if not md5_val or len(md5_val) != 32:
        abort(404)

    decrypted_dir, _, _ = _cfg()
    if not os.path.isdir(decrypted_dir):
        if emoji_url:
            return redirect(emoji_url)
        abort(404)

    # 1. Search local filesystem for emoji by MD5
    search_dirs = [
        os.path.join(decrypted_dir, 'emoticon'),
        os.path.join(decrypted_dir, 'Emoticon'),
        os.path.join(decrypted_dir, 'sticker'),
        os.path.join(decrypted_dir, 'Sticker'),
        os.path.join(decrypted_dir, 'msg', 'attach'),
    ]
    if account:
        search_dirs.insert(0, os.path.join(os.path.dirname(decrypted_dir), account, 'emoticon'))

    variants = [md5_val, f'{md5_val}_t', f'{md5_val}_h',
                f'{md5_val}.jpg', f'{md5_val}.png', f'{md5_val}.gif', f'{md5_val}.webp',
                f'{md5_val}_t.jpg', f'{md5_val}_t.png',
                f'{md5_val}.dat', f'{md5_val}_h.dat', f'{md5_val}_t.dat']

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for v in variants:
            path = os.path.join(d, v)
            if os.path.isfile(path):
                resolved = path
                # Handle .dat encrypted files
                if resolved.lower().endswith('.dat'):
                    try:
                        with open(resolved, 'rb') as f:
                            raw = f.read()
                        dec = decrypt_emoticon_aes_cbc(raw, md5_val)
                        if dec:
                            from flask import send_file
                            from io import BytesIO
                            return send_file(BytesIO(dec), mimetype='image/png')
                    except Exception:
                        pass
                    continue
                from flask import send_file
                mime, _ = __import__('mimetypes').guess_type(resolved)
                return send_file(resolved, mimetype=mime or 'image/png')

    # 2. Fallback: proxy from remote CDN URL
    if emoji_url:
        return redirect(emoji_url)

    abort(404)


@api_bp.route('/harvest-keys/status')
def harvest_keys_status():
    """Get V2 key cache status: how many keys cached vs total V2 files."""
    import json as _json
    decrypted_dir, wxid, _ = _cfg()
    if not os.path.isdir(decrypted_dir):
        return jsonify({'error': 'decrypted_dir not configured'}), 400

    keys_file = os.path.join(decrypted_dir, '_media_keys.json')
    cached = 0
    try:
        if os.path.isfile(keys_file) and os.path.getsize(keys_file) > 0:
            with open(keys_file, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            cached = len(data.get('md5_keys', {}))
    except Exception:
        pass

    # Count V2 files in media/images/
    v2_total = 0
    img_dir = os.path.join(decrypted_dir, 'media', 'images')
    if os.path.isdir(img_dir):
        for fname in os.listdir(img_dir):
            if fname.lower().endswith('.dat'):
                fpath = os.path.join(img_dir, fname)
                try:
                    with open(fpath, 'rb') as f:
                        header = f.read(6)
                    if header == b'\x07\x08V2\x08\x07':
                        v2_total += 1
                except OSError:
                    pass

    from engine.services.v2_key_extract import is_wechat_running as _wx_running
    return jsonify({
        'cached': cached,
        'v2_total': v2_total,
        'pending': max(0, v2_total - cached),
        'wechat_running': _wx_running(),
    })


@api_bp.route('/harvest-keys/run', methods=['POST'])
def harvest_keys_run():
    """Run one round of V2 key harvesting from WeChat memory."""
    import json as _json
    from engine.services.v2_key_extract import harvest_v2_keys, is_wechat_running as _wx_running

    decrypted_dir, wxid, _ = _cfg()
    if not os.path.isdir(decrypted_dir):
        return jsonify({'error': 'decrypted_dir not configured'}), 400

    if not _wx_running():
        return jsonify({'error': '微信未运行，请先启动微信并浏览包含图片的聊天记录'}), 400

    # Run a single scan round
    found = harvest_v2_keys(
        decrypted_dir, wxid=wxid,
        interval=0, max_rounds=1,
        print_fn=lambda *a, **kw: None
    )

    return jsonify({
        'found': len(found),
        'keys': {md5: key.hex() for md5, key in found.items()},
    })


@api_bp.route('/address-book')
def address_book():
    """Return contacts from contact.db with message stats, paginated.

    Query params:
        q        — search keyword (matches display_name, remark, nick_name, alias, wxid, phone, description)
        sort     — 'name' (default), 'msg_count', 'last_time'
        has_chat — '1' (only with chats), '0' (only without)
        letter   — filter by first letter of display_name
        kind     — 'all' (default), 'contacts' (exclude groups), 'groups' (groups only)
        label    — filter by WeChat label/tag name (e.g. 'only_work')
        page     — page number (default 1)
        per_page — items per page (default 100, max 500)
    """
    decrypted_dir, wxid, db_dir = _cfg()
    contacts = filter_contacts(
        get_all_contacts(decrypted_dir),
        q=request.args.get('q', ''),
        sort=request.args.get('sort', 'name'),
        has_chat=request.args.get('has_chat'),
        letter=request.args.get('letter', ''),
        kind=request.args.get('kind', 'all'),
        label=request.args.get('label'),
    )

    # Pagination
    total = len(contacts)
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = max(1, min(500, int(request.args.get('per_page', 100))))
    except (ValueError, TypeError):
        per_page = 100

    start = (page - 1) * per_page
    end = start + per_page
    page_contacts = contacts[start:end]
    total_pages = max(1, (total + per_page - 1) // per_page)

    return jsonify({
        'contacts': page_contacts,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': total_pages,
    })


@api_bp.route('/address-book/labels')
def address_book_labels():
    """Return the selectable label (tag) names for the address book filter.

    Union of contact_label definitions and labels actually seen on contacts,
    so a tag defined in WeChat but not yet used is still discoverable.
    """
    decrypted_dir, _, _ = _cfg()
    names = set(distinct_labels(get_all_contacts(decrypted_dir)))
    names.update(load_labels(_find_contact_db(decrypted_dir)).values())
    names.discard('')
    return jsonify({'labels': sorted(names)})


@api_bp.route('/address-book/<wxid>')
def address_book_detail(wxid):
    """Return single contact detail."""
    decrypted_dir, _, _ = _cfg()
    contacts = get_all_contacts(decrypted_dir)
    for c in contacts:
        if c['wxid'] == wxid:
            return jsonify(c)
    return jsonify({'error': 'contact not found'}), 404


@api_bp.route('/address-book/groups')
def address_book_groups():
    """Return group chat list with resolved display names.

    Fast path: groups from chats.db index already have pre-computed display_name.
    Slow path: groups from contact.db need _resolve_display lookup.
    """
    decrypted_dir, wxid, db_dir = _cfg()
    groups = get_all_groups(decrypted_dir)

    # Check if groups already have meaningful display names (fast path)
    needs_resolve = any(
        (g.get('display_name') or '') == (g.get('wxid') or '')
        for g in groups[:10]
    )
    if needs_resolve:
        from engine.services.chat import _load_contacts, _load_sessions, _load_room_owners, _find_file, _resolve_display
        contact_db = _find_file(decrypted_dir, "contact/contact.db", "contact.db")
        session_db = _find_file(decrypted_dir, "session/session.db", "session.db")
        id_to_name, name_to_id, _ = _load_contacts(contact_db)
        session_summaries = _load_sessions(session_db)
        room_owners = _load_room_owners(contact_db)
        for g in groups:
            uname = g['wxid']
            g['display_name'] = _resolve_display(
                uname, is_group=True, decrypted_dir=decrypted_dir,
                id_to_name=id_to_name, name_to_id=name_to_id,
                session_summaries=session_summaries, room_owners=room_owners,
            )

    return jsonify({'groups': groups, 'total': len(groups)})


@api_bp.route('/address-book/export')
def address_book_export():
    """Export contacts as xlsx / csv / html.

    Query params mirror /api/address-book (q / sort / has_chat / letter / kind)
    so the downloaded file matches what the user is currently looking at.
    ``format`` defaults to csv — the original hard-coded endpoint only ever
    produced CSV, and existing links/bookmarks must keep working.
    """
    from contacts_export import MIMETYPES, default_filename, render

    decrypted_dir, _, _ = _cfg()
    kind = request.args.get('kind', 'all')
    fmt = (request.args.get('format') or 'csv').strip().lower().lstrip('.')

    contacts = filter_contacts(
        get_all_contacts(decrypted_dir),
        q=request.args.get('q', ''),
        sort=request.args.get('sort', 'name'),
        has_chat=request.args.get('has_chat'),
        letter=request.args.get('letter', ''),
        kind=kind,
        label=request.args.get('label'),
    )

    try:
        data = render(contacts, fmt)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    filename = default_filename(fmt, kind)
    resp = make_response(data)
    resp.headers['Content-Type'] = MIMETYPES[fmt]
    resp.headers['Content-Disposition'] = 'attachment; filename=%s' % filename
    return resp

@api_bp.route("/settings/asr", methods=["GET"])
def asr_settings_get():
    """GET /api/settings/asr — 读取语音转写设置（含本地模型状态，密钥打码）。"""
    from engine.config_file import get_asr_settings
    from engine.services.asr_model import DEFAULT_MODEL, model_status
    from engine.services.asr_commercial import BAIDU_DEV_PIDS

    st = get_asr_settings()
    key = st.get("baidu_api_key") or ""
    secret = st.get("baidu_secret_key") or ""
    out = dict(st)
    out["baidu_api_key"] = ("*" * max(len(key) - 4, 0) + key[-4:]) if key else ""
    out["baidu_secret_key"] = ("*" * max(len(secret) - 4, 0) + secret[-4:]) if secret else ""
    out["has_baidu_key"] = bool(key and secret)
    out["devPids"] = BAIDU_DEV_PIDS
    out["localModelStatus"] = model_status(st.get("local_model") or DEFAULT_MODEL)
    return jsonify(out)


@api_bp.route("/settings/asr", methods=["POST"])
def asr_settings_set():
    """POST /api/settings/asr — 保存语音转写设置。

    密钥留空表示"不修改"；传 clear_baidu_keys=true 才会清空。
    """
    from engine.config_file import get_asr_settings, set_asr_settings
    data = request.get_json(silent=True) or {}
    current = get_asr_settings()
    patch = {}
    for field in ("engine", "local_model", "language"):
        if data.get(field) is not None:
            patch[field] = str(data[field])
    if data.get("simplify") is not None:
        patch["simplify"] = bool(data["simplify"])
    if data.get("baidu_dev_pid") is not None:
        try:
            patch["baidu_dev_pid"] = int(data["baidu_dev_pid"])
        except (TypeError, ValueError):
            pass
    if data.get("clear_baidu_keys"):
        patch["baidu_api_key"] = ""
        patch["baidu_secret_key"] = ""
    else:
        # 打码值（含 *）视为未修改
        for field in ("baidu_api_key", "baidu_secret_key"):
            value = data.get(field)
            if isinstance(value, str) and value and "*" not in value:
                patch[field] = value.strip()
    if patch.get("engine") not in (None, "local", "baidu"):
        return jsonify({"error": "bad_engine", "message": "引擎只能是 local 或 baidu"}), 400
    saved = set_asr_settings(patch)
    saved["saved"] = True
    key = saved.get("baidu_api_key") or ""
    secret = saved.get("baidu_secret_key") or ""
    saved["baidu_api_key"] = ("*" * max(len(key) - 4, 0) + key[-4:]) if key else ""
    saved["baidu_secret_key"] = ("*" * max(len(secret) - 4, 0) + secret[-4:]) if secret else ""
    saved["has_baidu_key"] = bool(key and secret)
    if patch.get("engine") == "baidu" and not (key and secret):
        saved["warning"] = "已切换为百度语音识别，但还没填 API Key / Secret Key，请填写后再试"
    return jsonify(saved)


@api_bp.route("/settings/asr/test", methods=["POST"])
def asr_settings_test():
    """POST /api/settings/asr/test — 测试百度语音识别凭据是否可用。"""
    from engine.config_file import get_asr_settings
    from engine.services.asr_commercial import test_credentials
    data = request.get_json(silent=True) or {}
    current = get_asr_settings()
    key = (data.get("baidu_api_key") or "").strip()
    secret = (data.get("baidu_secret_key") or "").strip()
    if not key or "*" in key:
        key = current.get("baidu_api_key") or ""
    if not secret or "*" in secret:
        secret = current.get("baidu_secret_key") or ""
    return jsonify(test_credentials(key, secret))


@api_bp.route("/asr/simplify", methods=["POST"])
def asr_simplify():
    """POST /api/asr/simplify — 繁体转简体（本地模型输出常为繁体）。"""
    from engine.services.asr_commercial import to_simplified
    data = request.get_json(silent=True) or {}
    return jsonify({"text": to_simplified(data.get("text") or "")})


@api_bp.route("/asr/commercial", methods=["POST"])
def asr_commercial():
    """POST /api/asr/commercial — 用商业 ASR（百度）识别某条语音。

    body: {"voice_url": "/api/voice?...", "path": "...", "create_time": 0,
           "local_id": 0, "chat": ""}（voice_url 也可直接给完整相对地址）
    """
    from urllib.parse import parse_qs, urlparse
    from engine.config_file import get_asr_settings
    from engine.services.asr_commercial import AsrError, recognize_wav
    from engine.services.media import get_voice_wav_path

    decrypted_dir, _, db_dir = _cfg()
    data = request.get_json(silent=True) or {}
    params = {}
    voice_url = data.get("voice_url") or ""
    if voice_url:
        qs = parse_qs(urlparse(voice_url).query)
        for k, v in qs.items():
            params[k] = v[0] if v else ""
    for k in ("path", "chat"):
        if data.get(k):
            params[k] = data[k]
    # body 里的 create_time / local_id 只有在有效值（非 0、非空）时才覆盖 URL 参数。
    # 前端一度固定传 0，会把 voice_url 里的真实值覆盖成 0 → 最终被当成 None →
    # 未播放过（还没生成缓存）的语音无法从 VoiceInfo 提取，只能报
    # 「找不到这条语音的音频文件」；播放一次生成缓存后就又能转写了。
    for k in ("create_time", "local_id"):
        v = data.get(k)
        if v in (None, "", 0, "0"):
            continue
        params[k] = v
    try:
        create_time = int(params["create_time"]) if params.get("create_time") else None
        local_id = int(params["local_id"]) if params.get("local_id") else None
    except (TypeError, ValueError):
        return jsonify({"error": "bad_params", "message": "create_time / local_id 必须是整数"}), 400

    wav = get_voice_wav_path(decrypted_dir, params.get("path"), create_time, local_id,
                             db_dir=db_dir, chat=params.get("chat"))
    if not wav:
        return jsonify({"error": "voice_not_found", "message": "找不到这条语音的音频文件"}), 404

    st = get_asr_settings()
    try:
        out = recognize_wav(wav, st.get("baidu_api_key") or "", st.get("baidu_secret_key") or "",
                            dev_pid=st.get("baidu_dev_pid") or 1537)
    except AsrError as e:
        return jsonify({"error": "asr_failed", "message": str(e)}), 502
    out["wav"] = os.path.basename(wav)
    return jsonify(out)


@api_bp.route("/asr/model/<path:relpath>")
def asr_model_file(relpath):
    """GET /api/asr/model/<model>/<file> — 提供 ASR 模型文件（用户目录优先，其次内置）。"""
    from flask import send_file
    from engine.services.asr_model import model_roots
    rel = relpath.replace("/", os.sep)
    for root in model_roots():
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            if p.lower().endswith(".onnx"):
                mime = "application/octet-stream"
            elif p.lower().endswith(".json"):
                mime = "application/json"
            else:
                import mimetypes as _mt
                mime = _mt.guess_type(p)[0] or "application/octet-stream"
            return send_file(os.path.abspath(p), mimetype=mime, max_age=3600)
    return jsonify({"error": "model_file_not_found", "path": relpath}), 404


@api_bp.route("/asr/status")
def asr_status():
    """GET /api/asr/status?model=... — 语音识别模型是否就绪。"""
    from engine.services.asr_model import DEFAULT_MODEL, model_status
    model = request.args.get("model") or DEFAULT_MODEL
    return jsonify(model_status(model))


@api_bp.route("/asr/install", methods=["POST"])
def asr_install():
    """POST /api/asr/install — 下载语音识别模型（SSE 进度）。

    body: {"model": "...", "mirror": "https://hf-mirror.com"}
    """
    import threading
    from web.sse import create_sse_progress, sse_response
    from engine.services.asr_model import DEFAULT_MODEL, download_model

    data = request.get_json(silent=True) or {}
    model = data.get("model") or DEFAULT_MODEL
    mirror = (data.get("mirror") or "").strip() or None
    push, gen = create_sse_progress()

    def _run():
        try:
            def _progress(msg, pct=0.0):
                push("download", msg, min(max(pct, 0.0), 0.99))
            out = download_model(model, base_url=mirror, progress_fn=_progress)
            if out["bytes"]:
                msg = "模型下载完成（%.1f MB，源：%s）" % (out["bytes"] / 1048576.0,
                                                        out.get("mirror", ""))
            else:
                msg = "模型文件已存在，无需下载"
            push.done({"model": model, "downloaded": out["downloaded"],
                       "skipped": out["skipped"], "dir": out["dir"], "message": msg})
        except Exception as e:
            # 失败时给出"可改用哪个模型"（若它已就绪，界面可一键切换），并附手动放置目录
            hint = ""
            try:
                from engine.services.asr_model import fallback_model_info
                fb = fallback_model_info(model)
                hint = ("；可改用 %s（%s）%s" % (fb["label"], "已就绪" if fb["ready"] else "需下载",
                                               "；也可手动把模型文件放到 " + fb["manualDir"]
                                               if not fb["ready"] else ""))
            except Exception:
                pass
            push.error("下载失败: " + str(e) + hint)

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@api_bp.route("/wxgf/status")
def wxgf_status_api():
    """GET /api/wxgf/status — wxgf(H.265) 图片解码能力（供界面引导安装 ffmpeg）。"""
    from engine.services.media import wxgf_status
    try:
        return jsonify(wxgf_status())
    except Exception as e:
        return jsonify({"supported": False, "installed": False, "error": str(e)}), 500


@api_bp.route("/wxgf/install-ffmpeg", methods=["POST"])
def wxgf_install_ffmpeg():
    """POST /api/wxgf/install-ffmpeg — 一键下载并安装 ffmpeg 到 tools 目录（SSE 进度）。

    下载源可通过 body {"url": "..."} 覆盖（便于使用国内镜像）。
    """
    import threading
    import time
    from web.sse import create_sse_progress, sse_response
    from engine.services.media import (FFMPEG_DOWNLOAD_URLS, install_ffmpeg_from_zip,
                                       tools_dir, _find_ffmpeg)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip() or FFMPEG_DOWNLOAD_URLS[0][1]
    push, gen = create_sse_progress()

    def _run():
        try:
            import shutil
            import tempfile
            dest = tools_dir()
            push("download", "开始下载 ffmpeg: " + url, 0.02)
            tmpdir = tempfile.mkdtemp(prefix="ffmpeg_dl_")
            zip_path = os.path.join(tmpdir, "ffmpeg.zip")
            state = {"last": 0.0}

            def _on_bytes(got, total):
                # 统一下载器回调：带浏览器 UA / 断点续传（issue #23 同类隐患）
                now = time.time()
                if now - state["last"] <= 0.6:
                    return
                state["last"] = now
                if total:
                    push("download", "已下载 %.1f / %.1f MB" % (got / 1048576.0,
                                                                total / 1048576.0),
                         0.05 + min(got / float(total), 1.0) * 0.8)
                else:
                    push("download", "已下载 %.1f MB" % (got / 1048576.0), 0.4)

            from engine.services.http_download import download_file
            download_file(url, zip_path, timeout=600, progress_fn=_on_bytes)
            push("extract", "下载完成，正在解压 ffmpeg.exe ...", 0.88)
            target = install_ffmpeg_from_zip(zip_path, dest)
            shutil.rmtree(tmpdir, ignore_errors=True)
            found = _find_ffmpeg()
            push("verify", "安装完成: " + str(found or target), 0.98)
            push.done({"installed": True, "path": found or target, "toolsDir": dest})
        except Exception as e:
            push.error("安装失败: " + str(e) +
                       "（也可以手动下载 ffmpeg 后把 ffmpeg.exe 放进 tools 目录）")

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@api_bp.route("/wxgf/open-tools", methods=["POST"])
def wxgf_open_tools():
    """POST /api/wxgf/open-tools — 打开 tools 目录并写入放置说明。"""
    from engine.services.media import tools_dir
    d = tools_dir()
    try:
        os.makedirs(d, exist_ok=True)
        hint = os.path.join(d, "把-ffmpeg.exe-放到这里.txt")
        if not os.path.isfile(hint):
            with open(hint, "w", encoding="utf-8") as f:
                f.write(
                    "微信 wxgf(H.265) 图片需要 ffmpeg 才能显示原图。\n\n"
                    "使用方法：\n"
                    "  1) 下载 Windows 版 ffmpeg（解压即用）：\n"
                    "     https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip\n"
                    "  2) 解压后把 bin\\ffmpeg.exe 复制到本目录\n"
                    "  3) 回到聊天查看器点「重试」，图片会自动转换\n\n"
                    "程序每次请求都会重新检测本目录，放入后无需重启。\n")
        opened = False
        try:
            os.startfile(d)
            opened = True
        except Exception:
            pass
        return jsonify({"ok": True, "toolsDir": d, "opened": opened})
    except Exception as e:
        return jsonify({"error": "open_failed", "message": str(e), "toolsDir": d}), 500
