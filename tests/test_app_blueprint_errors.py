"""蓝图导入失败必须**可诊断**（Task 7 fix round 2 —— known-issues #21）。

背景（本轮真实发生过，见 `task-6-report.md` §9 / `known-issues.md` #21）：并行开发期间
`tests/test_search_api.py` 出现过 **31 个失败，全部是 `/api/search*` → 404**，下一次运行
又全绿。根因是 `src/web/app.py` 的注册守卫把 `ImportError` **吞掉只 print 一句 WARN**。
`print` 的真正问题**不是**"pytest 会吞掉它"（实测：**失败**用例的 stdout 照常显示为
`Captured stdout call`，只有**通过**的用例才隐藏）——而是这三点：

1. **没有异常类型名**；
2. **没有 traceback**：`ImportError` 的 `str(e)` 常常只有 "cannot import name X"，
   丢掉最关键的"哪一层导入炸的"；
3. 它走 **stdout**：打包成 GUI（无控制台）时这条信息**根本没有出口**，也不能被日志系统
   路由/过滤/采集。

于是「蓝图没注册」在 HTTP 层**只表现为「路由不存在」**（31 个 404），排查成本极高。
（这处措辞的更正过程见 `task-7-report.md` §10.6.1 与 §13.9 的实测。）

本文件钉住三件事：

1. 导入失败时**必须**留下 ERROR 记录，文本同时含 **蓝图标签** 与 **异常类型名**
   （`ImportError`），并且带**完整 traceback**（`record.exc_info is not None`）。
2. **十处**守卫都要这样（不是只给搜索那一处打补丁）—— 参数化断言每一个标签。
3. **防空转**：正常构造 app 时**不得**有任何 ERROR/WARNING 记录，**且**蓝图确实注册上了
   （否则「没有日志」可能只是因为 app 压根没构造好，是一条恒真断言）。

实现侧对应改动：`src/web/app.py` 的模块级 `_register_blueprint(app, label, register)`
（一处实现，十处复用）。把该助手临时改回裸 `print(...)` 时，本文件必须变红 —— 变异测试
证据（含还原后的 md5）见 `task-7-report.md` §10。
"""
import logging
import sys
import types

import pytest

from web.app import create_app
from engine.services import media as media_mod

# `_register_blueprint(app, label, register)` 的 label → 蓝图模块名（十处，与 app.py 一一对应）
BLUEPRINT_GUARDS = (
    ('routes.api', 'web.routes.api'),
    ('reports', 'web.reports'),
    ('reports.wrapped', 'web.reports.wrapped'),
    ('routes.backup_api', 'web.routes.backup_api'),
    ('routes.export_api', 'web.routes.export_api'),
    ('routes.avatar_api', 'web.routes.avatar_api'),
    ('routes.cleanup_api', 'web.routes.cleanup_api'),
    ('routes.keys_api', 'web.routes.keys_api'),
    ('routes.pull_api', 'web.routes.pull_api'),
    ('routes.search_api', 'web.routes.search_api'),
)


@pytest.fixture(autouse=True)
def no_real_wxid_detection(monkeypatch):
    """必须屏蔽 `_detect_wxid`（known-issues #19）。

    本机 `D:\\xwechat_files` 存在时，它会**越过传入目录**去扫真实账号目录，把真实 wxid
    灌进 `app.config['WXID']` —— 既是隐私红线，也会让"带/不带 wxid"的对照变成空转。
    """
    monkeypatch.setattr(media_mod, '_detect_wxid', lambda decrypted_dir: None)


def _break_module(monkeypatch, module_name):
    """让 `module_name` 的**任何名字导入**都抛 ImportError（模拟半成品/依赖缺失）。

    手法：往 `sys.modules` 里塞一个 `__getattr__` 抛 ImportError 的假模块。
    `from X import Y` 会先取 `sys.modules[X]` 再 `getattr(X, 'Y')`，所以异常**照原样**
    从 import 语句里冒出来 —— 与真实 ImportError 在调用方的可观察行为一致。
    （本文件另有一条 `test_the_injection_really_breaks_the_import` 自检这个手法不是空转。）
    """
    fake = types.ModuleType(module_name)
    # `__path__` 必须**存在**：importlib 的 `_handle_fromlist` 先看它，不存在的话
    # 会在探 `__path__` 时就抛（异常原文会变成 `...__path__`，看不到是哪个符号炸的）。
    # 给了它，异常原文就精确指出被请求的那个名字（`...search_api.search_bp`）。
    fake.__path__ = []

    def _raise(attr):
        raise ImportError('simulated broken import: %s.%s' % (module_name, attr))

    fake.__getattr__ = _raise
    monkeypatch.setitem(sys.modules, module_name, fake)
    return fake


def _halt_import(monkeypatch, module_name):
    """第二个注入手法：`sys.modules[name] = None` → `import` 抛 **ModuleNotFoundError**。

    两条注入路径覆盖 `ImportError` 家族的两个分支，同时证明日志里的类型名是
    `type(e).__name__` **算出来的**，不是写死 "ImportError"。
    """
    monkeypatch.setitem(sys.modules, module_name, None)


def _build_app(decrypted_dir):
    return create_app(str(decrypted_dir))


def _error_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestImportFailureIsDiagnosable:
    def test_the_injection_really_breaks_the_import(self, monkeypatch):
        """防空转：先证明 `_break_module` 真的让 import 抛 ImportError。

        否则「有 ERROR 记录」可能来自别的原因，整组用例都会恒真/恒假而看不出来。
        """
        _break_module(monkeypatch, 'web.routes.search_api')
        with pytest.raises(ImportError):
            from web.routes.search_api import search_bp  # noqa: F401

    def test_search_import_error_is_logged_and_app_still_starts(self, tmp_path, monkeypatch, caplog):
        """判别性主用例（brief 的判据 + 控制流不变的显式断言）。

        `raised or any(...)` 的形状是控制方指定的：实现方可以选择「抛异常」或
        「记 ERROR」。本项目要求**保持控制流不变**（不让整个 `serve` 挂掉），
        所以这里把 `raised` 硬断言成 False —— 只留「有 ERROR 记录」这一条出路。
        """
        _break_module(monkeypatch, 'web.routes.search_api')
        raised = False
        try:
            app = _build_app(tmp_path)
        except ImportError:
            raised = True
            app = None

        assert raised or any(
            r.levelno >= logging.ERROR
            and 'routes.search_api' in r.getMessage()
            and 'ImportError' in r.getMessage()
            for r in caplog.records
        ), '蓝图导入失败被静默吞掉：既没有抛异常，也没有 ERROR 记录（known-issues #21 回归）'
        # 控制流要求：导入失败时应用**仍然启动**（不得 re-raise 让 serve 起不来）
        assert raised is False
        assert app is not None

    def test_error_record_carries_the_exception_type_and_a_full_traceback(
            self, tmp_path, monkeypatch, caplog):
        """要求的原文是「ERROR 级别 + 完整 traceback」。

        ① 异常**类型名**必须在文本里（只 print 一句 `e`（常常为空串）时排查不了问题）；
        ② `record.exc_info` 必须非 None —— 没有堆栈只知道"某个蓝图加载不了"，
           不知道是**哪一行**导入炸的（真实排查就是卡在这一步）。
        """
        _break_module(monkeypatch, 'web.routes.search_api')
        _build_app(tmp_path)

        matching = [r for r in _error_records(caplog) if '(routes.search_api)' in r.getMessage()]
        assert matching, '没有针对 routes.search_api 的 ERROR 记录：%r' % (_error_records(caplog),)
        record = matching[0]
        assert 'ImportError' in record.getMessage()
        assert 'simulated broken import: web.routes.search_api.search_bp' in record.getMessage(), \
            '异常原文必须进日志（否则只有一句"加载失败"）'
        assert record.exc_info is not None, 'ERROR 记录没有 traceback（须 logger.error(..., exc_info=True)）'
        assert 'Traceback' in caplog.text, '格式化后的日志里看不到堆栈'

    @pytest.mark.parametrize('label,module_name', BLUEPRINT_GUARDS)
    def test_every_guard_is_observable(self, label, module_name, tmp_path, monkeypatch, caplog):
        """「一处实现，十处复用」——**每一个**标签都要能被观测到。

        标签用**带括号**的形式匹配（`(routes.api)`），而不是裸子串：裸 `reports`
        会被 `reports.wrapped` 的级联失败误命中，那样这条参数化对 'reports'
        就没有判别力了（一处漏改会静默通过）。
        """
        _break_module(monkeypatch, module_name)
        try:
            _build_app(tmp_path)
        except ImportError:
            pass

        errors = _error_records(caplog)
        messages = [r.getMessage() for r in errors]
        assert any('(%s)' % label in m and 'ImportError' in m for m in messages), (
            '蓝图 %s（模块 %s）导入失败没有被 ERROR 记录观测到；实际记录：%r'
            % (label, module_name, messages))
        assert all(r.exc_info is not None for r in errors), \
            '有 ERROR 记录但不带 traceback：%r' % [m for m, r in zip(messages, errors)]

    def test_halted_import_reports_its_real_exception_type(self, tmp_path, monkeypatch, caplog):
        """`sys.modules[name] = None` → `ModuleNotFoundError`（ImportError 的子类）。

        类型名必须是**运行期算出来的**：写死 'ImportError' 的实现会在这条变红。
        同时它也覆盖了「整个模块文件都不见了」这种最恶劣的半成品状态。
        """
        _halt_import(monkeypatch, 'web.routes.search_api')
        _build_app(tmp_path)

        matching = [r for r in _error_records(caplog) if '(routes.search_api)' in r.getMessage()]
        assert matching, '整个模块 import 挂起时没有任何 ERROR 记录'
        assert 'ModuleNotFoundError' in matching[0].getMessage()
        # 'ModuleNotFoundError' 里**不含** 'ImportError' 子串，所以这条能真正判定
        # 「类型名是算出来的」（写死 'ImportError' 的实现会在这里变红）
        assert 'ImportError' not in matching[0].getMessage()
        assert matching[0].exc_info is not None


class TestNormalPathIsUntouched:
    def test_normal_startup_logs_no_error_or_warning(self, tmp_path, caplog):
        """防空转要求①：正常路径**不得**产生任何 ERROR/WARNING 记录。

        同时自检「蓝图确实注册上了」—— 否则"没有日志"可能只是因为导入悄悄失败了，
        这条断言会退化成恒真。
        """
        with caplog.at_level(logging.WARNING):
            app = _build_app(tmp_path)

        rules = {str(r) for r in app.url_map.iter_rules()}
        assert '/api/search' in rules, '搜索蓝图没注册上（正常路径被改坏了）'
        assert '/search' in rules
        # 仍然**不传** url_prefix：传了就会变成 /api/search/api/search（T7-A2 口径）
        assert '/api/search/api/search' not in rules

        noisy = [(r.levelname, r.name, r.getMessage())
                 for r in caplog.records if r.levelno >= logging.WARNING]
        assert noisy == [], '正常导入路径产生了日志记录：%r' % (noisy,)

    def test_blueprint_registration_order_is_unchanged(self, tmp_path):
        """注册顺序是既有行为的一部分（`app.url_map` 的规则顺序由它决定）—— 重构不得改变。

        `app.blueprints` 是 dict（3.7+ 保序），键就是蓝图的 name 属性，所以它能
        真实地反映注册顺序。把十个守卫改写成同一个助手很容易顺手重排/漏掉一处，
        这条断言让那种改动立刻变红。
        """
        app = _build_app(tmp_path)
        assert tuple(app.blueprints) == (
            'api', 'reports', 'wrapped', 'backup_api', 'export_api',
            'voice_export_api', 'avatar', 'cleanup', 'keys_api', 'pull_api', 'search_api',
        )
