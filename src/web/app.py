"""Flask application factory for the chat viewer."""
import logging
import os
import sys
import threading
import webbrowser
from flask import Flask, render_template, jsonify
from engine.version import VERSION as __version__

logger = logging.getLogger(__name__)


def _register_blueprint(app: Flask, label: str, register):
    """注册一个蓝图；导入失败时留下**可诊断**痕迹（一处实现，十处复用）。

    控制流刻意保持原样：导入失败时应用**仍然启动**（不 re-raise，否则一个半成品
    路由文件会让整个 `serve` 起不来），区别只在"怎么报"。

    为什么必须升级（known-issues #21）：原先每处只 `print` 一句 WARN ——
      * 没有**异常类型**、没有 **traceback**：`ImportError` 的 `str(e)` 常常只有
        "cannot import name X"，丢掉最关键的"哪一层导入炸的"；
      * 走 stdout 而不是 logging：打包成 GUI（无控制台）时这条信息**根本没有出口**，
        也不能被日志系统路由/过滤/采集。
    代价（本轮实测）：并行开发期间 `tests/test_search_api.py` 出现 **31 个失败，
    全部是 `/api/search*` → 404**，下一次运行又全绿 —— 「蓝图没注册」在 HTTP 层
    只表现为「路由不存在」，真实原因不可见。

    `register` 是零参可调用，内部做 `from ... import ...; app.register_blueprint(...)`；
    它的返回值原样返回（搜索那处要顺便拿到 `_search_type_options`），失败时返回 None。

    ⚠️ **边界（有意为之，别"顺手"放宽）**：本助手**只接 `ImportError`**
    （含其子类 `ModuleNotFoundError`）。`SyntaxError` / `NameError` 等会**照常冒出**，
    让 `create_app` 直接失败并打出 traceback —— 那类异常意味着"你发布的源码本身就是
    错的"（构建/部署错误），**响亮地崩**才是正确的；#21 要解决的是**静默** 404，
    不是"崩溃太响"。所以不要把 `except` 放宽到 `Exception` / `SyntaxError`。
    """
    try:
        return register()
    except ImportError as e:
        # 文本里必须同时有：蓝图标签 + 异常类型名；exc_info=True 带完整堆栈
        logger.error('[blueprint] 无法加载蓝图 (%s): %s: %s',
                     label, type(e).__name__, e, exc_info=True)
        return None


def _resolve_path(relative_path: str) -> str:
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'src', 'web', relative_path)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, relative_path)


def create_app(decrypted_dir: str, wxid: str = None, db_dir: str = None) -> Flask:
    app = Flask(__name__,
        template_folder=_resolve_path('templates'),
        static_folder=_resolve_path('static'),
    )
    from engine.services.media import resolve_account_dir
    app.config['DECRYPTED_DIR'] = decrypted_dir
    # issue #16：这里**不能**再写 `wxid or _detect_wxid(...)` ——
    # 配置里持久化的账号名可能是个**不存在**的值（用户实测是 `"output"`，多半是备份输出目录名泄漏进去的），
    # 它是 truthy 的，于是自动检测根本不会跑，而接下来每一次媒体解析都会以它拼路径 ⇒
    # **图片/文件 0 命中且不报错**。`resolve_account_dir` 只在**校验出真实目录**时才替换它。
    app.config['WXID'] = resolve_account_dir(decrypted_dir, wxid)
    app.config['DB_DIR'] = db_dir
    app.config['APP_VERSION'] = __version__
    app.json.ensure_ascii = False
    # 模板改动即时生效（打包后模板在 _MEIPASS 里只读，无额外开销）
    app.config['TEMPLATES_AUTO_RELOAD'] = True

    # Inject version into all template contexts
    @app.context_processor
    def _inject_version():
        return {'app_version': __version__}

    # Existing API
    def _register_api_bp():
        from .routes.api import api_bp
        app.register_blueprint(api_bp, url_prefix='/api')

    _register_blueprint(app, 'routes.api', _register_api_bp)

    # Existing reports
    def _register_reports_bp():
        from .reports import reports_bp
        app.register_blueprint(reports_bp)

    _register_blueprint(app, 'reports', _register_reports_bp)

    # Wrapped annual report
    def _register_wrapped_bp():
        from .reports.wrapped import wrapped_bp
        app.register_blueprint(wrapped_bp)

    _register_blueprint(app, 'reports.wrapped', _register_wrapped_bp)

    # New: Backup API (SSE endpoints)
    def _register_backup_bp():
        from .routes.backup_api import backup_bp
        app.register_blueprint(backup_bp)

    _register_blueprint(app, 'routes.backup_api', _register_backup_bp)

    # New: Export API (SSE endpoints)
    def _register_export_bp():
        from .routes.export_api import export_bp
        app.register_blueprint(export_bp)

    _register_blueprint(app, 'routes.export_api', _register_export_bp)

    # New: 语音导出 API（按人批量导出语音留言）
    def _register_voice_export_bp():
        from .routes.voice_export_api import voice_export_bp
        app.register_blueprint(voice_export_bp)

    _register_blueprint(app, 'routes.voice_export_api', _register_voice_export_bp)

    # Avatar API
    def _register_avatar_bp():
        from .routes.avatar_api import avatar_bp
        app.register_blueprint(avatar_bp)

    _register_blueprint(app, 'routes.avatar_api', _register_avatar_bp)

    # Cleanup API
    def _register_cleanup_bp():
        from .routes.cleanup_api import cleanup_bp
        app.register_blueprint(cleanup_bp, url_prefix='/api')

    _register_blueprint(app, 'routes.cleanup_api', _register_cleanup_bp)

    # Manual key entry API (paste keys by hand)
    def _register_keys_bp():
        from .routes.keys_api import keys_bp
        app.register_blueprint(keys_bp)

    _register_blueprint(app, 'routes.keys_api', _register_keys_bp)

    # ChatLab Pull data source API (start / stop / status)
    def _register_pull_bp():
        from .routes.pull_api import pull_bp
        app.register_blueprint(pull_bp)

    _register_blueprint(app, 'routes.pull_api', _register_pull_bp)

    # Global search API (/api/search — 前缀已在模块内声明，此处不得再传 url_prefix)
    def _register_search_bp():
        from .routes.search_api import search_bp, _search_type_options
        app.register_blueprint(search_bp)
        return _search_type_options

    # 成功时 `_search_type_options` 就是 routes.search_api 里的那个函数（与原先
    # `from ... import` 绑定的对象**同一个**）；失败时为 None，页面路由照旧会炸成
    # 500（与原先的 NameError 同类），但不会把整个 app 拖垮。
    _search_type_options = _register_blueprint(app, 'routes.search_api', _register_search_bp)

    # JSON error handlers — prevent Flask HTML pages for API routes
    @app.errorhandler(404)
    def _json_404(e):
        return jsonify({'error': 'not found'}), 404

    @app.errorhandler(500)
    def _json_500(e):
        return jsonify({'error': 'internal server error'}), 500

    # Dashboard (new home page)
    @app.route('/')
    def dashboard():
        return render_template('dashboard.html')

    # Chat viewer
    @app.route('/chat')
    def chat():
        return render_template('index.html')

    # Wizard pages
    @app.route('/backup')
    def backup_page():
        return render_template('backup.html')

    @app.route('/keyscan')
    def keyscan_page():
        return render_template('keyscan.html')

    @app.route('/decrypt')
    def decrypt_page():
        return render_template('decrypt.html')

    # Export pages
    @app.route('/export')
    def export_page():
        return render_template('export.html')

    @app.route('/voice-export')
    def voice_export_page():
        """按人批量导出语音留言（独立 HTML / 合并音频 / 逐条文件）。"""
        return render_template('voice_export.html')

    @app.route('/pull')
    def pull_page():
        return render_template('pull.html')

    @app.route('/keys')
    def manual_keys_page():
        return render_template('keys.html')

    @app.route('/settings')
    def settings_page():
        return render_template('settings.html')

    @app.route('/wordcloud')
    def wordcloud_page():
        return render_template('wordcloud.html')

    @app.route('/report')
    def report_page():
        return render_template('report.html')

    @app.route('/employee')
    def employee_page():
        return render_template('employee.html')

    @app.route('/contacts')
    def contacts_page():
        return render_template('contacts.html')

    # Global search page — 类型下拉由服务端渲染（唯一事实源 TYPE_ALIASES，见 T7-A7）
    @app.route('/search')
    def search_page():
        return render_template('search.html', type_options=_search_type_options())

    @app.route('/cleanup')
    def cleanup_page():
        return render_template('cleanup.html')

    @app.route('/wrapped')
    def wrapped_page():
        return render_template('wrapped.html')

    @app.route('/manual')
    def manual_page():
        try:
            return render_template('manual.html')
        except Exception:
            return "<html><body style='background:#0d1117;color:#c9d1d9;padding:40px;font-family:sans-serif;'><p>手册尚未生成。请运行 <code>python scripts/build_readme_html.py</code> 生成手册。</p></body></html>", 404

    # Serve WeChat built-in expression assets (dev mode only — not bundled in PyInstaller)
    _WXEMOJI_DIR = None
    if not getattr(sys, 'frozen', False):
        from pathlib import Path as _Path
        _WXEMOJI_DIR = _Path(__file__).resolve().parents[4] / 'tempWeChatDataAnalysis' / 'frontend' / 'public' / 'wxemoji'

    @app.route('/wxemoji/<path:filename>')
    def wxemoji(filename):
        from flask import send_from_directory, abort as _abort
        if _WXEMOJI_DIR is None or not os.path.isdir(_WXEMOJI_DIR):
            _abort(404)
        if '..' in filename or filename.startswith('/'):
            _abort(404)
        return send_from_directory(str(_WXEMOJI_DIR), filename)

    return app


def run_server(decrypted_dir: str, wxid: str = None, db_dir: str = None,
               host: str = '127.0.0.1', port: int = 5000, open_url: str = None):
    app = create_app(decrypted_dir, wxid=wxid, db_dir=db_dir)
    url = open_url or f'http://{host}:{port}'
    timer = threading.Timer(1.0, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()
    app.run(host=host, port=port, debug=False, use_reloader=False)
