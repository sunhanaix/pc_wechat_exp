"""Backup API — SSE-streaming endpoints for backup, key scan, and decrypt."""
import os
import sys
import ctypes
import threading
import time as _time
from flask import Blueprint, request, current_app

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

if getattr(sys, 'frozen', False):
    _DATA_ROOT = os.path.dirname(sys.executable)
else:
    _DATA_ROOT = os.path.normpath(os.path.join(_BASE, '..'))

from web.sse import create_sse_progress, sse_response

backup_bp = Blueprint('backup_api', __name__, url_prefix='/api/backup')
_stdout_lock = threading.Lock()



def _safe_path(user_input: str) -> str:
    """Normalize a user-supplied path to resolve .. traversal."""
    return os.path.realpath(os.path.abspath(user_input))


@backup_bp.route('/run', methods=['POST'])
def backup_run():
    """POST /api/backup/run — Full backup pipeline."""
    data = request.get_json(silent=True) or {}
    db_dir = data.get('db_dir') or current_app.config.get('DB_DIR', '')
    if data.get('output_dir'):
        output_dir = _safe_path(data['output_dir'])
    else:
        import datetime as _dt
        output_dir = os.path.join(_DATA_ROOT, 'backup', _dt.datetime.now().strftime('%Y-%m-%d'))
    key_file = _safe_path(data['key_file']) if data.get('key_file') else None
    start_date = data.get('start_date') or None
    end_date = data.get('end_date') or None
    days = data.get('days')
    if not start_date and not end_date and days is not None:
        if int(days) == 0:
            start_date = None
            end_date = None
        else:
            import datetime as _dt
            end_date = _dt.datetime.now().strftime('%Y-%m-%d')
            start_date = (_dt.datetime.now() - _dt.timedelta(days=int(days))).strftime('%Y-%m-%d')

    # Auto-detect WeChat data directory if not provided or invalid
    if not db_dir or not os.path.isdir(db_dir):
        try:
            from engine.utils import find_all_wechat_data_dirs
            dirs = find_all_wechat_data_dirs()
            if len(dirs) == 1:
                db_dir = dirs[0]['db_path']
            elif len(dirs) > 1:
                push, gen = create_sse_progress()
                push.select([{
                    'db_path': d['db_path'],
                    'wxid': d['wxid'],
                    'db_count': d.get('db_count', 0),
                    'size_mb': d.get('size_mb', 0),
                    'mtime': d.get('mtime', 0),
                } for d in dirs])
                return sse_response(gen)
        except Exception:
            pass
    if not db_dir or not os.path.isdir(db_dir):
        push, gen = create_sse_progress()
        push.error("未找到微信数据目录 — 请确认微信已安装并至少登录过一次，或手动填写 db_storage 路径")
        return sse_response(gen)

    push, gen = create_sse_progress()
    flask_app = current_app._get_current_object()

    def _run():
        from backup.pipeline import run_backup
        from engine.config_file import set_backup_data_dir

        def _progress(stage, detail, progress):
            push(stage, detail, progress)

        try:
            result = run_backup(db_dir, output_dir, key_file,
                                start_date=start_date, end_date=end_date,
                                on_progress=_progress)
            if result.get('success'):
                if os.path.isdir(os.path.join(output_dir, 'message')):
                    set_backup_data_dir(output_dir, wxid=result.get('wxid', ''))
                    with flask_app.app_context():
                        flask_app.config['DECRYPTED_DIR'] = output_dir
            push.done(result)
        except Exception as e:
            push.error(str(e))

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@backup_bp.route('/scan', methods=['POST'])
def backup_scan():
    """POST /api/backup/scan — Detect WeChat data directories."""
    push, gen = create_sse_progress()

    def _run():
        try:
            from engine.utils import find_all_wechat_data_dirs
            push('scan', '正在检测微信数据目录...', 0.3)
            dirs = find_all_wechat_data_dirs()
            push('scan', f'找到 {len(dirs)} 个账号', 1.0)
            push.done({
                'accounts': [
                    {
                        'db_path': d['db_path'],
                        'wxid': d['wxid'],
                        'mtime': d['mtime'],
                        'db_count': d.get('db_count', 0),
                        'size_mb': d.get('size_mb', 0),
                    }
                    for d in dirs
                ]
            })
        except Exception as e:
            push.error(str(e))

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@backup_bp.route('/keyscan', methods=['POST'])
def backup_keyscan():
    """POST /api/backup/keyscan — Extract decryption keys.

    Preferred path: read-only Config.Cipher scan (WeChat 4.1.10+, no admin,
    no restart, no hook). Falls back to key_scan's full strategy chain only
    when the read-only scan cannot resolve every salt.
    """
    data = request.get_json(silent=True) or {}
    db_dir = data.get('db_dir') or current_app.config.get('DB_DIR')
    if not db_dir:
        try:
            from engine.utils import find_all_wechat_data_dirs
            dirs = find_all_wechat_data_dirs()
            if dirs:
                db_dir = dirs[0]['db_path']
        except Exception:
            pass
    if not db_dir:
        push, gen = create_sse_progress()
        push.error("未找到微信数据目录 — 请指定 --db-dir 启动服务")
        return sse_response(gen)

    if not os.path.isdir(db_dir):
        push, gen = create_sse_progress()
        push.error(f"微信数据目录不存在或不可访问: {db_dir}")
        return sse_response(gen)

    push, gen = create_sse_progress()
    flask_app = current_app._get_current_object()

    def _run():
        import io
        import sys as _sys
        try:
            from engine.services.config_cipher_extract import (
                extract_keys_via_config_cipher)
            from engine.services.wechat_key_extract import (
                collect_db_files, load_from_config, save_key_results)

            push('scan', '正在收集数据库信息...', 0.05)
            db_files, salt_to_dbs = collect_db_files(db_dir)
            total = len(salt_to_dbs)
            push('scan', f'找到 {len(db_files)} 个数据库, {total} 个不同密钥', 0.10)

            key_map = {}
            # 1) existing config fast path
            loaded = load_from_config(db_dir, db_files, salt_to_dbs, key_map, print)
            if loaded == total:
                push.done({'keys': total, 'total': total,
                           'source': 'config',
                           'output': f'所有 {total} 个密钥已存在配置中。'})
                return

            # 2) read-only Config.Cipher scan (primary for 4.1.10+)
            push('scan', '开始只读 Config.Cipher 扫描（无需管理员权限，微信保持运行即可）...', 0.15)
            with _stdout_lock:
                old_stdout = _sys.stdout
                _sys.stdout = io.StringIO()
                try:
                    found = extract_keys_via_config_cipher(
                        db_dir, db_files, salt_to_dbs, key_map, print,
                        lambda pct, msg: push('scan', msg, 0.15 + pct * 0.6))
                    output = _sys.stdout.getvalue()
                finally:
                    _sys.stdout = old_stdout

            # 3) fallback: full key_scan chain (MMKV -> hook -> memscan)
            if len(key_map) < total:
                push('scan', f'只读扫描获得 {len(key_map)}/{total}，'
                             '尝试完整策略链 (MMKV/Hook/内存扫描)...', 0.80)
                from key_scan import run_key_scan
                with _stdout_lock:
                    old_stdout = _sys.stdout
                    _sys.stdout = io.StringIO()
                    try:
                        run_key_scan(db_dir, None)
                        output += '\n' + _sys.stdout.getvalue()
                    finally:
                        _sys.stdout = old_stdout

            # 4) persist any newly found keys
            if key_map:
                save_key_results(db_files, salt_to_dbs, key_map, db_dir, print)

            from engine.config_file import get_db_keys
            keys = get_db_keys()
            result = {'keys': len(keys) if keys else len(key_map),
                      'total': total,
                      'source': 'config-cipher' if len(key_map) >= total else 'mixed',
                      'output': output}
            push.done(result)
        except Exception as e:
            push.error(str(e))

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@backup_bp.route('/decrypt', methods=['POST'])
def backup_decrypt():
    """POST /api/backup/decrypt — Decrypt databases only."""
    data = request.get_json(silent=True) or {}
    db_dir = data.get('db_dir') or current_app.config.get('DB_DIR')
    if not db_dir:
        try:
            from engine.utils import find_all_wechat_data_dirs
            dirs = find_all_wechat_data_dirs()
            if dirs:
                db_dir = dirs[0]['db_path']
        except Exception:
            pass
    if not db_dir:
        push, gen = create_sse_progress()
        push.error("未找到微信数据目录 — 请指定 --db-dir 启动服务")
        return sse_response(gen)

    if not os.path.isdir(db_dir):
        push, gen = create_sse_progress()
        push.error(f"微信数据目录不存在或不可访问: {db_dir}")
        return sse_response(gen)

    output_dir = _safe_path(data['output_dir']) if data.get('output_dir') else os.path.join(_DATA_ROOT, 'output', 'decrypted')
    key_file = _safe_path(data['key_file']) if data.get('key_file') else None

    push, gen = create_sse_progress()
    flask_app = current_app._get_current_object()

    def _run():
        try:
            from backup.decryptor import load_keys, decrypt_for_backup
            from engine.config_file import set_backup_data_dir
            push('decrypt', '加载密钥...', 0.05)
            keys = load_keys(key_file)
            if not keys:
                push.error("未找到数据库密钥 — 请先执行密钥提取")
                return

            def _on_progress(detail, progress):
                push('decrypt', detail, progress)

            results, skipped_keys = decrypt_for_backup(db_dir, output_dir, keys, on_progress=_on_progress)
            if results and os.path.isdir(os.path.join(output_dir, 'message')):
                set_backup_data_dir(output_dir)
                with flask_app.app_context():
                    flask_app.config['DECRYPTED_DIR'] = output_dir
            push.done({'decrypted': len(results), 'files': results,
                        'skipped_missing_key': skipped_keys})
        except Exception as e:
            push.error(str(e))

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@backup_bp.route('/hook-keyscan', methods=['POST'])
def backup_hook_keyscan():
    """POST /api/backup/hook-keyscan — Interactive hook-based key extraction.

    Guides the user through: exit WeChat → login → intercept sqlite3_key()
    calls during database initialization. Uses SSE for real-time status.
    """
    data = request.get_json(silent=True) or {}
    db_dir = data.get('db_dir') or current_app.config.get('DB_DIR')
    if not db_dir:
        try:
            from engine.utils import find_all_wechat_data_dirs
            dirs = find_all_wechat_data_dirs()
            if dirs:
                db_dir = dirs[0]['db_path']
        except Exception:
            pass
    if not db_dir or not os.path.isdir(db_dir):
        push, gen = create_sse_progress()
        push.error("未找到微信数据目录")
        return sse_response(gen)

    push, gen = create_sse_progress()

    def _run():
        try:
            # --- Check prerequisites ---
            push('check', '正在检查运行环境...', 0.05)

            # Admin check
            try:
                is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
            except Exception:
                is_admin = False
            if not is_admin:
                push.error("需要以管理员身份运行才能使用Hook模式")
                return

            # py_wx_key check — prefer v2 (built-in, 4-param corrected), fall back to wx_key
            try:
                from engine.services.py_wx_key_v2 import initialize_hook
            except ImportError:
                try:
                    import wx_key
                except ImportError:
                    push.error("py_wx_key 模块未安装，无法使用Hook模式")
                    return

            # psutil check
            try:
                import psutil
            except ImportError:
                push.error("psutil 模块未安装")
                return

            # Collect DB info
            from key_scan import collect_db_files, _find_wechat_pids, _try_hook_on_pid
            push('check', '正在收集数据库信息...', 0.08)
            db_files, salt_to_dbs = collect_db_files(db_dir)
            total_salts = len(salt_to_dbs)
            push('check', f'找到 {len(db_files)} 个数据库, {total_salts} 个不同密钥', 0.10)

            key_map = {}

            # --- Phase 1: Check if WeChat is already running ---
            candidates = _find_wechat_pids()
            if candidates:
                largest_pid = candidates[0][1]
                largest_mem = candidates[0][0] // 1048576
                push('wait_exit',
                     f'检测到微信正在运行 (PID={largest_pid}, {largest_mem}MB)。'
                     '请完全退出微信：右键点击系统托盘中的微信图标 → 退出微信',
                     0.15)
                push('wait_exit',
                     '等待微信退出...',
                     0.18)

                # Wait for all weixin.exe processes to exit
                wait_start = _time.time()
                while _time.time() - wait_start < 120:
                    current = _find_wechat_pids()
                    if not current:
                        break
                    _time.sleep(1)
                else:
                    push.error("等待微信退出超时(120秒)，请手动关闭后重试")
                    return

            # --- Phase 2: Wait for WeChat to restart ---
            push('wait_login',
                 '微信已退出。现在请打开微信，在登录界面点击"登录"按钮',
                 0.25)
            push('wait_login',
                 '等待微信进程启动...',
                 0.28)

            # Wait for new WeChat process
            wait_start = _time.time()
            new_pid = None
            new_mem = 0
            while _time.time() - wait_start < 120:
                current = _find_wechat_pids()
                if current:
                    # Give WeChat 2s to finish spawning all processes,
                    # then pick the largest (main UI process)
                    _time.sleep(2)
                    current = _find_wechat_pids()
                    if current:
                        new_pid = current[0][1]
                        new_mem = current[0][0] // 1048576
                    break
                _time.sleep(1)
            else:
                push.error("等待微信启动超时(120秒)，请手动启动微信后重试")
                return

            push('install',
                 f'检测到微信进程 PID={new_pid} ({new_mem}MB)，正在安装Hook...',
                 0.40)

            # --- Phase 3: Install hook and poll for keys ---
            # Wrap push as a print_fn for _try_hook_on_pid
            def _hook_print(msg):
                push('polling', msg, 0.50)

            found = _try_hook_on_pid(
                new_pid, db_files, salt_to_dbs, key_map,
                print_fn=_hook_print, timeout=60
            )

            # --- Phase 4: Results ---
            if key_map:
                # Save keys to config
                from engine.config_file import set_db_keys as _set_db_keys
                db_keys = {}
                for salt_hex, key_hex in key_map.items():
                    for rel_path in salt_to_dbs.get(salt_hex, []):
                        db_keys[rel_path] = key_hex
                try:
                    _set_db_keys(db_keys, db_dir)
                except Exception:
                    pass

                push('done', f'成功捕获 {len(key_map)}/{total_salts} 个密钥', 1.0)
                push.done({
                    'keys': len(key_map),
                    'total': total_salts,
                    'found': list(key_map.keys()),
                    'output': '\n'.join(
                        f'[OK] {salt} → {", ".join(salt_to_dbs[salt])}'
                        for salt in key_map
                    )
                })
            else:
                push.done({
                    'keys': 0,
                    'total': total_salts,
                    'output': 'Hook已安装但未拦截到 sqlite3_key() 调用。'
                              '请确保微信在Hook安装后才点击登录，重新尝试。'
                })

        except Exception as e:
            import traceback
            push.error(f'{e}\n{traceback.format_exc()}')

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)
