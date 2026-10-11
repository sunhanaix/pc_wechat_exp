"""Export API — SSE-streaming endpoints for chat export, wordcloud, report, employee."""
import hashlib
import os
import re
import sqlite3
import sys
import threading
from collections import defaultdict
from datetime import datetime
from flask import Blueprint, request, current_app, jsonify, send_from_directory

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

if getattr(sys, 'frozen', False):
    _DATA_ROOT = os.path.dirname(sys.executable)
else:
    _DATA_ROOT = os.path.normpath(os.path.join(_BASE, '..'))

from web.sse import create_sse_progress, sse_response

export_bp = Blueprint('export_api', __name__, url_prefix='/api/export')
_stdout_lock = threading.Lock()


def _decrypted_dir():
    return current_app.config.get('DECRYPTED_DIR', '')


def _scan_chats_flat(decrypted_dir: str, name_filter: str = ""):
    """Scan flat backup layout (no contact/session subdirs) for matching chats.

    Returns a list of chat_info dicts (compatible with scan_chats output) by
    reading Name2Id tables directly from message_*.db files in decrypted_dir.
    Each dict has: username, display_name, tables[{db_idx, db_path, table_name}].
    """
    if not os.path.isdir(decrypted_dir):
        return []

    # Find message DBs in flat layout (files directly in root dir)
    db_paths = []
    for f in os.listdir(decrypted_dir):
        if re.match(r'message_\d+\.db', f, re.IGNORECASE):
            db_paths.append(os.path.join(decrypted_dir, f))
    if not db_paths:
        return []
    db_paths.sort()

    # Build hash→username mapping from Name2Id tables
    hash_to_username = {}
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(db_path)
            conn.text_factory = bytes
            for (uname,) in conn.execute("SELECT user_name FROM Name2Id"):
                if uname:
                    u = uname.decode("utf-8", errors="replace") if isinstance(uname, bytes) else str(uname)
                    if u:
                        hash_to_username[hashlib.md5(u.encode()).hexdigest()] = u
            conn.close()
        except sqlite3.Error:
            pass

    # Build chat list from Msg_ tables
    chat_map = defaultdict(lambda: {"tables": [], "total_msgs": 0})
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(db_path)
            conn.text_factory = bytes
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (tname,) in tables:
                tn = tname.decode("utf-8", errors="replace") if isinstance(tname, bytes) else str(tname)
                h = tn[4:]  # Strip "Msg_" prefix
                uname = hash_to_username.get(h, f"unknown_{h[:12]}")
                try:
                    count = conn.execute(f'SELECT COUNT(*) FROM [{tn}]').fetchone()[0]
                except sqlite3.Error:
                    count = 0
                chat_map[uname]["tables"].append({
                    "db_idx": 0,
                    "db_path": db_path,
                    "table_name": tn,
                })
                chat_map[uname]["total_msgs"] += count
            conn.close()
        except sqlite3.Error:
            pass

    # Load contacts from neighbouring output/decrypted for display name resolution
    # (flat backup layouts lack contact.db, but the old decrypted dir may have one)
    id_to_name = {}
    name_to_id = {}
    _project_root = _DATA_ROOT  # project root / exe dir
    contact_candidates = [
        os.path.join(decrypted_dir, "contact", "contact.db"),
        os.path.join(_project_root, "output", "decrypted", "contact", "contact.db"),
    ]
    for cc in contact_candidates:
        cc = os.path.normpath(cc)
        if os.path.exists(cc):
            try:
                cconn = sqlite3.connect(cc)
                cconn.text_factory = bytes
                for r in cconn.execute(
                    "SELECT id, username, remark, nick_name, alias FROM contact"
                ):
                    cid = r[0]
                    uname = (r[1] or b"").decode("utf-8", errors="replace").strip() if isinstance(r[1], bytes) else str(r[1] or "").strip()
                    remark = (r[2] or b"").decode("utf-8", errors="replace").strip() if isinstance(r[2], bytes) else str(r[2] or "").strip()
                    nick = (r[3] or b"").decode("utf-8", errors="replace").strip() if isinstance(r[3], bytes) else str(r[3] or "").strip()
                    alias = (r[4] or b"").decode("utf-8", errors="replace").strip() if isinstance(r[4], bytes) else str(r[4] or "").strip()
                    display = remark if (remark and remark != uname) else (nick if (nick and nick != uname) else (alias if (alias and alias != uname) else uname))
                    if cid and display:
                        id_to_name[cid] = display
                    if uname:
                        name_to_id[uname] = cid
                cconn.close()
            except sqlite3.Error:
                pass

    # Build result list, filter by name if provided
    result = []
    filter_lower = name_filter.lower() if name_filter else ""
    for uname, info in chat_map.items():
        # Resolve display name via contact db
        display = uname
        if uname in name_to_id:
            cid = name_to_id[uname]
            if cid in id_to_name:
                display = id_to_name[cid]
        if display == uname and uname.endswith("@chatroom") and len(uname) > 20:
            display = f"群聊({uname[:12]}...)"
        if filter_lower:
            if filter_lower not in uname.lower() and filter_lower not in display.lower():
                continue
        result.append({
            "username": uname,
            "display_name": display,
            "msg_count": info["total_msgs"],
            "tables": info["tables"],
        })

    result.sort(key=lambda x: x["msg_count"], reverse=True)
    return result


@export_bp.route('/chat', methods=['POST'])
def export_chat():
    """POST /api/export/chat — Export chat messages."""
    data = request.get_json(silent=True) or {}
    contact = data.get('contact', '')
    display_name_hint = data.get('display_name', '')
    date_start = data.get('date_start', '')
    date_end = data.get('date_end', '')
    fmt = data.get('format', 'txt')
    # 勾了「顺带识别」但没有可用引擎时给出可操作提示（issue #24 反馈④）
    if data.get('transcribe_missing'):
        try:
            from chat_export import asr_engine_available
            _ok, _eng = asr_engine_available()
        except Exception:
            _ok, _eng = True, ''
        if not _ok:
            return jsonify({
                'error': 'no_asr_engine',
                'message': '未检测到可用的识别引擎：请在「设置」里下载本地模型，或填写百度语音识别 Key，再勾选此选项',
            }), 400

    decrypted_dir = _decrypted_dir()
    # 注意：后台线程没有 Flask 应用上下文，配置必须在此处（请求线程内）取出
    own_wxid = current_app.config.get('WXID')
    push, gen = create_sse_progress()

    def _run():
        try:
            from chat_export import export_chat
            from engine.constants import TZ

            # ChatLab 导出只需 wxid/显示名，走轻量会话扫描（数秒）；
            # 完整 scan_chats 需遍历全部 Msg_ 表统计消息数，大数据量下很慢。
            is_chatlab = fmt.lower().startswith('chatlab')
            all_chats = []
            if is_chatlab:
                push('export', '正在扫描聊天列表（轻量模式）...', 0.1)
                try:
                    from chatlab_pull_server import fast_chat_list
                    all_chats = fast_chat_list(decrypted_dir, own_wxid=own_wxid)
                except Exception as e:
                    push('export', '轻量扫描失败，回退完整扫描: %s' % e, 0.1)
                    all_chats = []
            if not all_chats:
                from chat_list import scan_chats
                push('export', '正在扫描聊天列表...', 0.1)
                all_chats, _, _ = scan_chats(decrypted_dir)
            if not all_chats:
                # Flat backup layout (no contact/session subdirs)
                all_chats = _scan_chats_flat(decrypted_dir, contact)

            target = None
            # Exact match first
            for c in all_chats:
                if c['username'] == contact or c['display_name'] == contact:
                    target = c
                    break
            # Fuzzy match: collect all matches, let user pick
            if not target:
                contact_lower = contact.lower()
                fuzzy_matches = []
                for c in all_chats:
                    if (contact_lower in c['display_name'].lower()
                            or contact_lower in c['username'].lower()):
                        fuzzy_matches.append({
                            'username': c['username'],
                            'display_name': c['display_name'],
                            'msg_count': c.get('msg_count', 0),
                        })
                if not fuzzy_matches:
                    push.error(f"未找到联系人: {contact}")
                    return
                if len(fuzzy_matches) == 1:
                    m = fuzzy_matches[0]
                    for c in all_chats:
                        if c['username'] == m['username']:
                            target = c
                            break
                else:
                    push.select(fuzzy_matches)
                    return

            start_ts = None
            end_ts = None
            if date_start:
                dt = datetime.strptime(date_start, '%Y-%m-%d').replace(tzinfo=TZ)
                start_ts = int(dt.timestamp())
            if date_end:
                dt = datetime.strptime(date_end, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=TZ)
                end_ts = int(dt.timestamp())

            out_dir = os.path.join(_DATA_ROOT, 'export')
            os.makedirs(out_dir, exist_ok=True)

            def _print(msg):
                push('export', msg, 0.5)

            # ChatLab 标准格式（JSONL 可导入 ChatLab，支持断点续传）
            if fmt.lower().startswith('chatlab'):
                from chatlab_export import export_chatlab
                sub_fmt = 'json' if fmt.lower().endswith('json') else 'jsonl'
                raw_name = (display_name_hint or target.get('display_name')
                            or target.get('username') or 'chat')
                safe = re.sub(r'[\\/:*?"<>|\s]+', '_', str(raw_name)).strip('_.') or 'chat'
                out_path = os.path.join(out_dir, 'chatlab_%s.%s' % (safe, sub_fmt))
                push('export', 'ChatLab 格式导出中（%s）...' % sub_fmt, 0.3)
                res = export_chatlab(
                    decrypted_dir, target, out_path, fmt=sub_fmt,
                    own_wxid=own_wxid,
                    resume=bool(data.get('resume', True)),
                    start_ts=start_ts, end_ts=end_ts,
                    print_fn=_print)
                fname = os.path.basename(res.get('path') or out_path)
                total = res.get('count_total') or 0
                note = '本次新增 %d 条' % (res.get('count_new') or 0)
                if res.get('resumed'):
                    note = '断点续传：' + note
                note += '。导入方式：ChatLab → 导入聊天记录 → 选择该 %s 文件。' % sub_fmt.upper()
                push.done({'msg_count': total,
                           'download_url': '/api/export/download/' + fname,
                           'filename': fname,
                           'note': note})
                return

            count, filepath = export_chat(target, out_dir,
                                          start_ts=start_ts, end_ts=end_ts,
                                          print_fn=_print, fmt=fmt,
                                          display_name=display_name_hint, include_asr=bool(data.get('include_asr')) or bool(data.get('transcribe_missing')),
                                  transcribe_missing=bool(data.get('transcribe_missing')), decrypted_dir=decrypted_dir)
            if count and filepath:
                fname = os.path.basename(filepath)
                download_url = f'/api/export/download/{fname}'
                push.done({'msg_count': count, 'download_url': download_url, 'filename': fname})
            else:
                push.done({'msg_count': count, 'file': filepath})
        except Exception as e:
            push.error(str(e))

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@export_bp.route('/wordcloud/view/<path:filename>')
def view_wordcloud(filename):
    """GET /api/export/wordcloud/view/<filename> — Serve a generated wordcloud HTML."""
    out_dir = os.path.join(_DATA_ROOT, 'export', 'wordcloud')
    return send_from_directory(os.path.normpath(out_dir), filename)


@export_bp.route('/download/<path:filename>')
def download_file(filename):
    """GET /api/export/download/<filename> — Serve an exported file."""
    out_dir = os.path.join(_DATA_ROOT, 'export')
    return send_from_directory(os.path.normpath(out_dir), filename, as_attachment=True)


@export_bp.route('/wordcloud', methods=['POST'])
def export_wordcloud():
    """POST /api/export/wordcloud — Generate word cloud."""
    data = request.get_json(silent=True) or {}
    chat = data.get('chat', '')
    try:
        year = int(data.get('year', datetime.now().year))
    except (TypeError, ValueError):
        year = datetime.now().year

    decrypted_dir = _decrypted_dir()
    push, gen = create_sse_progress()

    def _run():
        try:
            from wordcloud_gen import generate_wordcloud, extract_text_messages
            from chat_list import scan_chats
            from engine.constants import TZ

            push('analyze', '正在扫描聊天数据...', 0.1)

            chat_info = None
            if chat:
                all_chats, _, _ = scan_chats(decrypted_dir)
                if not all_chats:
                    # Flat backup layout (no contact/session subdirs) —
                    # scan Name2Id from message DBs directly
                    all_chats = _scan_chats_flat(decrypted_dir, chat)
                # Exact match first
                for c in all_chats:
                    if c['username'] == chat or c['display_name'] == chat:
                        chat_info = c
                        break
                # Fuzzy match: collect all matches, let user pick
                if not chat_info:
                    chat_lower = chat.lower()
                    fuzzy_matches = []
                    for c in all_chats:
                        if (chat_lower in c['display_name'].lower()
                                or chat_lower in c['username'].lower()):
                            fuzzy_matches.append({
                                'username': c['username'],
                                'display_name': c['display_name'],
                                'msg_count': c.get('msg_count', 0),
                            })
                    if not fuzzy_matches:
                        push.error(f'未找到匹配的聊天: {chat}')
                        return
                    if len(fuzzy_matches) == 1:
                        # Single match — auto-select
                        m = fuzzy_matches[0]
                        for c in all_chats:
                            if c['username'] == m['username']:
                                chat_info = c
                                break
                    else:
                        push.select(fuzzy_matches)
                        return

            start_ts = int(datetime(year, 1, 1, tzinfo=TZ).timestamp())
            end_ts = int(datetime(year + 1, 1, 1, tzinfo=TZ).timestamp()) - 1

            push('analyze', f'正在提取 {year} 年文本...', 0.3)
            text, msg_count = extract_text_messages(decrypted_dir, chat_info=chat_info,
                                                     start_ts=start_ts, end_ts=end_ts)

            if msg_count == 0:
                # Year filter may exclude all data — retry without time filter
                push('analyze', f'{year} 年无数据，正在全时段提取...', 0.35)
                text, msg_count = extract_text_messages(decrypted_dir, chat_info=chat_info,
                                                         start_ts=None, end_ts=None)
                year_ts = (None, None)
            else:
                year_ts = (start_ts, end_ts)

            push('analyze', f'提取到 {msg_count} 条消息', 0.5)

            out_dir = os.path.join(_DATA_ROOT, 'export', 'wordcloud')
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            out_file = os.path.join(out_dir, f'wordcloud_{ts}.html')
            def _print(msg):
                push('analyze', str(msg), 0.6)

            result = generate_wordcloud(decrypted_dir, chat_info=chat_info,
                                        out_path=out_file, start_ts=year_ts[0], end_ts=year_ts[1],
                                        print_fn=_print)

            if result:
                fname = os.path.basename(result)
                view_url = f'/api/export/wordcloud/view/{fname}'
                push.done({'msg_count': msg_count, 'view_url': view_url, 'filename': fname})
            else:
                push.error('词云生成失败，未找到足够的文本消息')
        except Exception as e:
            push.error(str(e))

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@export_bp.route('/report/view/<path:filename>')
def view_report(filename):
    """GET /api/export/report/view/<filename> — Serve a generated report HTML."""
    out_dir = os.path.join(_DATA_ROOT, 'export', 'report')
    return send_from_directory(os.path.normpath(out_dir), filename)


@export_bp.route('/report', methods=['POST'])
def export_report():
    """POST /api/export/report — Generate HTML report."""
    data = request.get_json(silent=True) or {}
    decrypted_dir = _decrypted_dir()

    push, gen = create_sse_progress()

    def _run():
        try:
            from report_gen import generate_report
            push('analyze', f'正在收集统计数据... ({decrypted_dir})', 0.2)
            out_file = generate_report(decrypted_dir)
            if out_file:
                # Copy to web-accessible export dir and return view URL
                fname = os.path.basename(out_file)
                web_dir = os.path.join(_DATA_ROOT, 'export', 'report')
                os.makedirs(web_dir, exist_ok=True)
                import shutil
                shutil.copy2(out_file, os.path.join(web_dir, fname))
                view_url = f'/api/export/report/view/{fname}'
                push.done({'msg_count': 0, 'view_url': view_url, 'filename': fname})
            else:
                push.error('报告生成失败 — 解密数据目录中未找到任何消息。请确认已执行备份与解密。')
        except Exception as e:
            push.error(str(e))

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@export_bp.route('/employee', methods=['POST'])
def export_employee():
    """POST /api/export/employee — Export employee chat reports.

    This is a synchronous JSON endpoint (not SSE) because the file upload
    via multipart/form-data makes SSE streaming impractical. All work runs
    in the request thread.
    """
    import tempfile

    excel_path = None
    tmp = None
    if 'file' in request.files:
        f = request.files['file']
        tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        f.save(tmp.name)
        excel_path = tmp.name
        tmp.close()
    elif request.form.get('excel'):
        excel_path = request.form['excel']

    if not excel_path:
        return jsonify({'success': False, 'error': '请上传员工 Excel 文件'}), 400

    decrypted_dir = _decrypted_dir()
    out_dir = os.path.join(_DATA_ROOT, 'export')
    os.makedirs(out_dir, exist_ok=True)

    try:
        from employee_match import run_employee_export
        import io
        import sys as _sys
        with _stdout_lock:
            old_stdout = _sys.stdout
            _sys.stdout = io.StringIO()
            try:
                run_employee_export(decrypted_dir, excel_path, out_dir)
                output = _sys.stdout.getvalue()
            finally:
                _sys.stdout = old_stdout
        return jsonify({'success': True, 'output': output})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
