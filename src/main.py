"""
WeChat EXP — 微信聊天记录备份与查看工具

Commands:
  backup    扫描、解密、迁移媒体、构建索引
  serve     启动 Web 聊天记录查看器
  export    导出聊天记录 / 词云 / 报告 / 员工报表
"""
import argparse
import os
import sys

from engine.version import VERSION as __version__

if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def _resolve_decrypted_dir():
    """Auto-detect decrypted data directory."""
    from engine.config_file import get_backup_data_dir, get_latest_backup_dir
    d = get_backup_data_dir()
    if d:
        return d
    if getattr(sys, 'frozen', False):
        backup_root = os.path.join(BASE, 'backup')
        fallback = os.path.join(BASE, 'output', 'decrypted')
    else:
        backup_root = os.path.join(BASE, '..', 'backup')
        fallback = os.path.join(BASE, '..', 'output', 'decrypted')
    d = get_latest_backup_dir(backup_root)
    if d:
        return d
    return fallback


def _resolve_db_dir():
    """Auto-detect WeChat db_storage directory."""
    from engine.utils import find_all_wechat_data_dirs
    dirs = find_all_wechat_data_dirs()
    if dirs:
        return dirs[0]['db_path']
    return None


def _project_root():
    """Data root: project root when running from source, exe dir when frozen."""
    if getattr(sys, 'frozen', False):
        return BASE
    return os.path.normpath(os.path.join(BASE, '..'))


def _print_missing_dir(decrypted):
    print("错误: 解密目录不存在: " + str(decrypted))
    print("请先运行 backup 命令，或使用 --decrypted-dir 指定目录")


def cmd_backup(args):
    """Run the full backup pipeline."""
    from engine.utils import find_all_wechat_data_dirs, is_wechat_running
    from engine.config_file import set_backup_data_dir

    if is_wechat_running():
        print("警告: 微信正在运行，建议关闭后再备份以避免数据库锁定。")

    dirs = find_all_wechat_data_dirs()
    if not dirs:
        from engine.utils import data_dir_hint
        print(data_dir_hint())
        return

    # Resolve target account
    if args.db_dir:
        db_path = args.db_dir
        wxid = args.wxid or ''
    elif args.wxid:
        match = next((d for d in dirs if d['wxid'] == args.wxid), None)
        if not match:
            print(f"未找到账号 '{args.wxid}'。可用的账号：")
            for d in dirs:
                print(f"  {d['wxid']}")
            return
        db_path = match['db_path']
        wxid = match['wxid']
    elif len(dirs) == 1:
        db_path = dirs[0]['db_path']
        wxid = dirs[0]['wxid']
    else:
        print(f"检测到 {len(dirs)} 个微信账号，请用 --wxid 指定要备份的账号：\n")
        print(f"{'账号 (wxid)':30s} {'消息库':>6s} {'大小':>10s}  {'最近活动':20s}")
        print("-" * 78)
        for d in dirs:
            db_count = d.get('db_count', 0)
            size_mb = d.get('size_mb', 0)
            if size_mb >= 1024:
                size_str = f'{size_mb / 1024:.1f} GB'
            else:
                size_str = f'{size_mb:.0f} MB'
            import datetime as _dt
            if d.get('mtime'):
                mtime = _dt.datetime.fromtimestamp(d['mtime']).strftime('%Y-%m-%d %H:%M')
            else:
                mtime = '-'
            print(f"{d['wxid']:30s} {db_count:>6d} {size_str:>10s}  {mtime:20s}")
        print(f"\n示例: python main.py backup --wxid {dirs[0]['wxid']}")
        return
    if wxid:
        print(f"备份账号: {wxid}")
    print(f"数据目录: {db_path}")

    import datetime
    if getattr(sys, 'frozen', False):
        default_out = os.path.join(BASE, 'backup', datetime.datetime.now().strftime('%Y-%m-%d'))
    else:
        default_out = os.path.join(BASE, '..', 'backup', datetime.datetime.now().strftime('%Y-%m-%d'))
    output_dir = args.output or default_out
    print(f"输出目录: {output_dir}")

    key_file = args.key_file  # None = auto-detect from config

    # Date range: explicit dates override --days, default is last 30 days.
    # --days 0 means no date filter (all records).
    if args.date_from or args.date_to:
        start_date = args.date_from  # None = unbounded
        end_date = args.date_to
    elif args.days is not None and args.days == 0:
        start_date = None
        end_date = None
    else:
        days = args.days if args.days is not None else 30
        end_date = datetime.datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    if start_date:
        print(f"日期范围: {start_date} ~ {end_date or '至今'}")
    else:
        print(f"日期范围: 全部")

    harvest = not getattr(args, 'no_harvest', False)

    from backup.pipeline import run_backup
    print()
    result = run_backup(
        db_path, output_dir, key_file,
        start_date=start_date, end_date=end_date,
        link_dest=getattr(args, 'link_dest', None),
        on_progress=_make_progress_display(),
        harvest_keys=harvest,
    )
    print()

    if result['success']:
        print(f"\n备份完成！输出目录: {output_dir}")
        stats = result['stats']
        print(f"  解密数据库: {stats.get('decrypted', 0)} 个")
        m = stats.get('migrated', {})
        print(f"  媒体文件: {m.get('hardlinked', 0)} 硬链接, {m.get('link_reused', 0)} 复用上轮, {m.get('copied', 0)} 复制")
        v2k = stats.get('v2_keys_harvested', 0)
        if v2k > 0:
            print(f"  V2 图片密钥: {v2k} 个（后台收割）")
        # Persist the backup output directory for serve/report/wordcloud
        if os.path.isdir(os.path.join(output_dir, 'message')):
            wxid = result.get('wxid', '')
            set_backup_data_dir(output_dir, wxid=wxid)
    else:
        print(f"\n备份失败: {result['errors']}")


def cmd_sync(args):
    """Run an independent message-sync worker until Ctrl+C."""
    import json
    import time
    from engine.config_file import get_db_dir
    from engine.services.live_sync import MessageSyncService
    source = args.db_dir or get_db_dir()
    if not source:
        print('请用 --db-dir 指定微信 db_storage 目录')
        return 1
    try:
        service = MessageSyncService(source, args.output, args.interval)
        if args.once:
            state = service.run_once()
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0 if state['ready'] and not state['last_error'] else 1
        service.start()
        print('消息同步已启动，实时目录: ' + str(service.output))
        print('按 Ctrl+C 停止；可用 serve --decrypted-dir <实时目录> 查看数据')
        try:
            while service.status()['running']:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            service.stop()
        return 0 if not service.status()['last_error'] else 1
    except (ValueError, OSError) as exc:
        print('同步启动失败: ' + str(exc))
        return 1


def cmd_serve(args):
    """Start the web chat viewer."""
    from web.app import run_server
    from engine.config_file import get_backup_wxid

    if args.decrypted_dir:
        decrypted = args.decrypted_dir
    else:
        decrypted = _resolve_decrypted_dir()
    db_dir = args.db_dir
    wxid = get_backup_wxid()

    # issue #27：启动时**后台**预建会话索引 —— 首次打开聊天页就不用现场建（真机实测约 28 秒）
    try:
        from engine.services.chat import ensure_chat_index_async
        ensure_chat_index_async(decrypted)
        print('[index] 已在后台预建会话索引（缺失时）')
    except Exception as _e:
        print('[index] 预建索引跳过:', _e)
    print(f"启动 Web 查看器 v{__version__}...")
    print(f"  解密目录: {decrypted}")
    print(f"  地址: http://{args.host}:{args.port}")
    run_server(decrypted, wxid=wxid, db_dir=db_dir, host=args.host, port=args.port)


def cmd_import_keys(args):
    """手动输入数据库密钥：校验 -> 保存 -> 供其它功能使用。

    输入支持手写格式，也支持**直接粘贴/读取密钥扫描的整段日志**
    （`[Cipher-FOUND] …` 行会被自动识别，头尾日志行当作噪声忽略）。
    """
    import os as _os
    from engine.manual_keys import (apply_entries, export_key_list,
                                    format_results, match_entries, parse_entries,
                                    read_text_file, status, summarize)

    db_dir = getattr(args, "db_dir", None)
    if not db_dir or not _os.path.isdir(db_dir):
        try:
            from engine.config_file import get_db_dir
            db_dir = get_db_dir() or None
        except Exception:
            db_dir = None
    if not db_dir or not _os.path.isdir(db_dir):
        try:
            from engine.utils import find_all_wechat_data_dirs
            dirs = find_all_wechat_data_dirs()
            if dirs:
                db_dir = dirs[0]["db_path"]
        except Exception:
            dirs = []
    if not db_dir or not _os.path.isdir(db_dir):
        from engine.utils import data_dir_hint
        print(data_dir_hint())
        return

    print("微信数据目录: " + db_dir)
    st = status(db_dir)
    print("数据库 %d 个：已就绪 %d，缺少密钥 %d，密钥不匹配 %d"
          % (st["total"], st["verified"], st["missing"], st["invalid"]))

    if getattr(args, "list", False):
        for d in st["databases"]:
            flag = "OK " if d["verified"] else ("BAD" if d["hasKey"] else "---")
            print("  [%s] %-32s %6s MB" % (flag, d["rel"], d["sizeMb"]))
        return

    text_parts = []
    if getattr(args, "file", None):
        if not _os.path.isfile(args.file):
            print("错误: 密钥文件不存在: " + args.file)
            return
        # 自动处理 UTF-8 / GBK / BOM —— 日志文件常是 GBK
        text_parts.append(read_text_file(args.file))
    if getattr(args, "key", None):
        text_parts.extend(args.key)
    text = "\n".join(text_parts)
    if not text.strip():
        print("请用 --file 或 --key 提供密钥，示例：")
        print("  wechat_exp.exe import-keys --key \"message_0.db=<64位hex>\"")
        print("  wechat_exp.exe import-keys --file keys.txt")
        print("  wechat_exp.exe import-keys --file keyscan.log   # 可直接读密钥扫描日志")
        return

    entries = parse_entries(text)
    if not entries:
        print("没有解析到任何密钥行")
        return

    s = summarize(entries)
    bits = []
    if s["cipher_log"]:
        bits.append("识别到 %d 条密钥扫描日志" % s["cipher_log"])
    if s["generic"]:
        bits.append("%d 条手写格式" % s["generic"])
    if s["noise"]:
        bits.append("%d 行日志噪声（已忽略）" % s["noise"])
    if s["masked"]:
        bits.append("%d 行打码/脱敏（无法校验）" % s["masked"])
    if s["errors"]:
        bits.append("%d 行无法识别" % s["errors"])
    if bits:
        print("解析：" + "；".join(bits))
    print("解析到 %d 条密钥，开始校验..." % len(entries))

    export_path = getattr(args, "export", None)

    if getattr(args, "dry_run", False):
        results = match_entries(db_dir, entries)
        print(format_results(results))
        ok = sum(1 for r in results if r["status"] == "matched")
        print("校验通过 %d/%d（未保存，--dry-run）" % (ok, len(entries)))
        if export_path:
            res = export_key_list(db_dir, entries, export_path)
            print("已导出对应关系清单 %d 条 -> %s" % (res["count"], res["path"]))
            if res.get("unverified"):
                print("  其中 %d 条未通过校验（已在文件中标注）" % res["unverified"])
        return

    out = apply_entries(db_dir, entries, force=bool(getattr(args, "force", False)))
    print(format_results(out["results"]))
    print("已保存 %d 个数据库的密钥到 .wechat_exp_config.json" % out["saved"])
    st = out["status"]
    print("当前覆盖：已就绪 %d/%d，缺少 %d，校验失败 %d"
          % (st["verified"], st["total"], st["missing"], st["invalid"]))
    if export_path:
        res = export_key_list(db_dir, entries, export_path)
        print("已导出对应关系清单 %d 条 -> %s" % (res["count"], res["path"]))
        if res.get("unverified"):
            print("  其中 %d 条未通过校验（已在文件中标注）" % res["unverified"])
    if st["missing"] == 0:
        print("全部数据库密钥就绪，现在可以运行: wechat_exp.exe export -m decrypt")
    else:
        print("提示: 仍缺少密钥的数据库可用界面「手动输入密钥」继续补充")

def cmd_chatlab_pull(args):
    """启动 ChatLab Pull 远程数据源服务。"""
    from engine.config_file import get_backup_wxid
    from engine.utils import find_all_wechat_data_dirs
    from chatlab_pull_server import run_pull_server

    decrypted = args.decrypted_dir or _resolve_decrypted_dir()
    if not os.path.isdir(decrypted):
        print("错误: 解密目录不存在: " + str(decrypted))
        print("请先运行 backup 命令，或使用 --decrypted-dir 指定目录")
        return

    own_wxid = get_backup_wxid()
    if not own_wxid:
        try:
            dirs = find_all_wechat_data_dirs()
            if dirs:
                own_wxid = dirs[0]['wxid']
        except Exception:
            pass

    run_pull_server(decrypted, own_wxid=own_wxid,
                    host=args.host, port=args.port,
                    token=args.token, print_fn=print)


def _export_chatlab_cmd(args):
    """导出 ChatLab 标准格式（支持断点续传）。"""
    from engine.config_file import get_backup_wxid
    from engine.utils import find_all_wechat_data_dirs
    from chatlab_export import export_all_chatlab

    decrypted = args.decrypted_dir or _resolve_decrypted_dir()
    fmt = getattr(args, 'format', None) or 'jsonl'
    resume = not getattr(args, 'no_resume', False)
    name_filter = getattr(args, 'chat', None)
    out_dir = args.output or os.path.join(decrypted, '..', 'chatlab_export')

    if not os.path.isdir(decrypted):
        print("错误: 解密目录不存在: " + str(decrypted))
        print("请先运行 backup 命令，或使用 --decrypted-dir 指定目录")
        return

    own_wxid = get_backup_wxid()
    if not own_wxid:
        try:
            dirs = find_all_wechat_data_dirs()
            if dirs:
                own_wxid = dirs[0]['wxid']
        except Exception:
            pass

    print("=" * 56)
    print("  ChatLab 格式导出")
    print("=" * 56)
    print("  解密目录: " + str(decrypted))
    print("  输出目录: " + str(out_dir))
    print("  格式: " + fmt + ("  (支持断点续传)" if fmt == 'jsonl' else "  (不支持续传，建议 jsonl)"))
    print("  断点续传: " + ("开启（中断后重跑本命令即可继续）" if resume else "关闭"))
    if name_filter:
        print("  过滤: " + str(name_filter))
    print()

    result = export_all_chatlab(
        decrypted, out_dir, fmt=fmt, own_wxid=own_wxid,
        resume=resume, name_filter=name_filter,
        print_fn=print, progress_fn=lambda pct, msg: None,
    )
    print()
    print("完成: %d 个会话导出, %d 个跳过（已完成）, %d 个失败"
          % (result['exported'], result['skipped'], result['failed']))
    print("进度清单: " + result['state_file'])
    print()
    print("导入 ChatLab：打开 ChatLab → 导入 → 选择输出目录下的 .jsonl 文件")


def _contacts_out_path(output, fmt, kind):
    """把 -o 解析为完整文件路径。

    -o 可以是文件路径，也可以是目录（包括尚未存在的目录）。
    没有扩展名的路径按目录处理，否则会生成没有后缀、双击打不开的文件。
    """
    from contacts_export import default_filename
    if not output:
        return os.path.join(_project_root(), 'export', default_filename(fmt, kind))
    if os.path.isdir(output) or not os.path.splitext(output)[1]:
        return os.path.join(output, default_filename(fmt, kind))
    return output


# --------------------------------------------------------------------------
# 退出码约定（Task 8 建立，T8-A1）
# --------------------------------------------------------------------------
# 本项目在此之前的子命令**从不**设置退出码（`src/main.py` 里此前没有任何
# `sys.exit`），于是"搜索失败"/"索引构建失败"在脚本与自动化里和"成功"无法区分。
# 新约定（`search` / `build-search-index` 首先遵守）：
#   0 = 成功
#   1 = 失败（解密目录不存在 / 查询为空或语法错误 / 索引故障 / 构建失败）
#   2 = 用法错误（argparse 自带，例如缺少位置参数或非法 --sort）
# 处理函数**返回**退出码，由 `main()` 调 `_exit_with()` 转成进程退出码；
# 返回值约定让处理函数可以脱离 `SystemExit` 被单测。
EXIT_OK = 0
EXIT_FAILURE = 1


def _exit_with(code):
    """把处理函数的返回码转成进程退出码。

    只在非 0 时 `sys.exit()`：成功路径保持"正常返回"，不打断调用 `main()` 的宿主。
    """
    if code:
        sys.exit(int(code))


def _make_output_safe():
    """把 stdout/stderr 的编码错误策略改成"降级显示"，避免在 GBK 控制台上崩溃。

    背景（2026-09 Critical，真实数据上必现）：本机控制台是 GBK（cp936），而
    **真实消息正文与显示名里什么都可能有** —— U+2005 四点空格、U+2776 ❶、emoji、
    罕见汉字……在 101 万条真实消息上，直接 `print(snippet)` 几乎必然撞上
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2005'`：
    搜索本身**成功了**，用户却只拿到一条 traceback 和 0 个结果。

    与 `_make_progress_display`（按 `sys.stdout.encoding` 挑安全字符）的区别：
    进度条是我们自己画的固定字符，可以预筛；**结果内容不能预筛**（你不能决定用户
    的消息里有什么），所以需要的是"编码安全输出"而不是"挑字符"：
      * `errors='replace'`：可显示字符照旧，编不出的字符降级成 `?` —— 不崩、可读；
      * **不改 encoding**：GBK 控制台上写 UTF-8 字节只会让中文全部变成乱码。
    stderr 一并处理：异常信息里也可能带路径/正文（否则连错误提示都打不出来）。

    这不覆盖 `--json`：JSON 走 `ensure_ascii=True`，输出纯 ASCII、**零数据损失**
    （见 `_cmd_search`）；反过来说，若 JSON 输出被 `errors='replace'` 处理，
    数据里的字符会**真的变成 `?`** —— 所以两者必须同时成立。

    任何不支持 `reconfigure` 的流（或 `pythonw` 下 `sys.stdout is None`）都静默跳过：
    输出美化**绝不能**让命令本身失败。`io.UnsupportedOperation` 同时是
    `OSError`/`ValueError` 的子类，已被下面两个 except 覆盖。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, 'reconfigure'):
            continue
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, OSError, ValueError):
            continue


def _cmd_search(args):
    """全局搜索：打印摘要结果，或原样输出 JSON。返回进程退出码（0 = 成功）。

    退出码（T8-A1）：解密目录不存在 / 查询为空或语法错误 / 索引故障 → 非 0。
    **不使用裸 `except Exception`**：只接住已经声明的失败类型
    （`SearchIndexError` 及其子类、`ValueError`），编程错误必须原样冒出来 ——
    否则真 bug 会被伪装成"一次正常的失败提示"。

    ⚠️ 注意 `UnicodeEncodeError` **是 `ValueError` 的子类**：不做编码安全输出的话，
    一次成功的搜索会因为"结果里有 GBK 编不出的字符"被这里的 `except ValueError`
    当成查询无效而吞掉（表现为"静默失败"）。`_make_output_safe()` 是这条路径的前提。
    """
    # 必须在**任何**打印之前（含缺目录提示、查询回显、引擎回调的进度文案）：
    # 真实正文/显示名/路径里什么都可能有，GBK 控制台编不出就会崩。
    _make_output_safe()

    import json as _json
    from engine.config_file import get_backup_wxid
    from engine.services.search import SearchIndexError, search_messages
    from engine.services.search_query import is_empty, parse_query

    decrypted = args.decrypted_dir or _resolve_decrypted_dir()
    if not os.path.isdir(decrypted):
        _print_missing_dir(decrypted)
        return EXIT_FAILURE

    parsed = parse_query(args.query)
    if is_empty(parsed):
        # 引擎也会为这种情况抛 ValueError，这里先拦是为了逐条打印解析错误
        print("错误: 查询为空或没有有效条件")
        for err in parsed.get('errors') or []:
            print("  - %s: %s" % (err.get('token'), err.get('message')))
        return EXIT_FAILURE

    try:
        # own_wxid 必须**原样**传下去（#16/#17）：它的值形式是账号**目录名**
        # （如 wxid_xxx_10e8），归一化由引擎负责；CLI 既不能自己剥后缀，
        # 也不能把它当成筛选条件拼进查询串 —— 它只是保留 token「我」的解析依据。
        res = search_messages(decrypted, args.query, page=args.page,
                              per_page=args.per_page, sort=args.sort,
                              own_wxid=get_backup_wxid())
    except SearchIndexError as e:
        # 索引损坏/SQL 失败**绝不能**显示成"0 条结果"（T5-A1）：打印原始 SQL 错误
        # 并让退出码非 0，脚本才不会把故障当成"没有匹配"。
        print("错误: " + str(e))
        if e.detail:
            print("  详情: " + str(e.detail))
        if e.hint:
            print("  建议: " + str(e.hint))
        return EXIT_FAILURE
    except ValueError as e:
        print("错误: " + str(e))
        return EXIT_FAILURE

    # 查询有语法错误 → 用户要的查询没有被完整执行，脚本不该把它当成成功（T8-A1）；
    # 但 `--json` 仍须先输出机器可读的完整响应体。
    errs = (res.get('parsed') or {}).get('errors') or []

    if args.json:
        # T8-A2：整个响应体原样透传（脚本接口），不挑字段、不加工、不改键名。
        #
        # `ensure_ascii=True` 是**刻意的**（2026-09 Critical）：输出因此是**纯 ASCII**，
        # 在任何编码的控制台/重定向/管道下都不会崩，而且**数据零损失** —— 转义是
        # 精确的，消费者 `json.loads()` 之后与原始对象逐键相等（中文只是显示成
        # `\uXXXX`）。**不要**"为了好看"改回 `ensure_ascii=False`：真实数据里的
        # U+2005/emoji 会让它直接 UnicodeEncodeError，搜索成功却什么都拿不到。
        print(_json.dumps(res, ensure_ascii=True, indent=2))
        return EXIT_FAILURE if errs else EXIT_OK

    idx = res.get('index') or {}
    print("=" * 56)
    print("  全局搜索")
    print("=" * 56)
    print("  查询: " + args.query)
    print("  索引: " + ("已就绪（%d 条文本 / %d 条元数据）"
                        % (idx.get('fts_rows') or 0, idx.get('meta_rows') or 0)
                        if idx.get('ready') else "未构建 —— 正在降级直扫（较慢）"))
    print("  扫描方式: %s（候选 %d 行，耗时 %.1fms）"
          % (res.get('scan_mode') or '未知', res.get('candidate_rows') or 0,
             float(res.get('elapsed_ms') or 0.0)))
    if res.get('used_fallback'):
        print("  提示: 索引未构建，可运行 build-search-index 提速约 25 倍")
    if res.get('regex_degraded'):
        print("  提示: 正则无法预筛，已退化为全量扫描")
    if res.get('sender_filter_unsupported'):
        print("  提示: 降级路径不支持发送者筛选，请先构建索引")
    if res.get('slow_query_hint'):
        print("  提示: " + str(res['slow_query_hint']))
    if res.get('fallback_text_only'):
        # 降级语料只有"有正文"的消息：类型/日期等筛选会少给甚至给空，
        # 用户会误以为"库里没有"（Task 6 追加项）—— 必须显式说出来。
        print("  提示: 降级扫描只覆盖有正文的消息，类型/日期等筛选可能少给或为空；"
              "这不代表库里没有这些消息")
    if res.get('truncated'):
        # T16：结果集过大时引擎只保留了前 N 条幸存行用于分页（`total` 仍是**精确**命中数）。
        # 结构化字段（`truncated`/`retained_rows`）+ 人话**必须同时出现**：这里由字段驱动，
        # 引擎也会把同一句追加进 `warnings`（下面照样打印）—— 与本命令既有的
        # `fallback_text_only` 同一取向：宁可说两遍，也绝不静默少给结果。
        print("  提示: 结果集过大（共 %d 条），只保留了前 %d 条用于分页 —— "
              "请增加筛选条件（日期范围 / 会话 / 类型）缩小范围。"
              % (res.get('total') or 0, res.get('retained_rows') or 0))
    for warn in res.get('warnings') or []:
        print("  警告: " + str(warn))
    for err in errs:
        print("  语法警告: %s: %s" % (err.get('token'), err.get('message')))
    print("  命中: %d 条（第 %d/%d 页）"
          % (res['total'], res['page'], res['total_pages']))
    print()
    if not res['results']:
        if res.get('truncated'):
            # T16 契约 #5：**这一页的空不是"没有匹配的消息"**，而是它落在保留窗口之外
            # （`total_pages` 仍按精确 total 算，所以这些页确实存在、但必然为空）。
            # 沿用「（无结果）」就是把"我们只保留了前 N 条"谎报成"库里没有这条消息"。
            print("  当前页超出保留范围（只保留了前 %d 条），请缩小查询范围"
                  % (res.get('retained_rows') or 0))
        else:
            print("  （无结果）")
        return EXIT_FAILURE if errs else EXIT_OK

    import datetime as _dt
    from engine.constants import TZ
    for hit in res['results']:
        ts = _dt.datetime.fromtimestamp(hit['create_time'] or 0, TZ).strftime('%Y-%m-%d %H:%M')
        who = hit['sender_display_name'] or hit['chat_display_name']
        # 分隔符一律用 ASCII：GBK 控制台编不出 '›'（U+203A）之类的字符，
        # 打印时会 UnicodeEncodeError。
        print("  [%s] %s / %s  (%s)" % (ts, hit['chat_display_name'], who,
                                        hit['type_label']))
        print("      " + (hit['snippet'] or ''))
    return EXIT_FAILURE if errs else EXIT_OK


def _cmd_build_search_index(args):
    """构建/刷新全局搜索索引。返回进程退出码（0 = 成功）。

    失败（任意异常）→ 打印错误 + **非 0** 退出码（T8-A1）。这里按"任意异常"兜底是
    有意的（与 `_cmd_search` 相反）：构建是长事务，崩在中途只会留下一个半成品索引，
    而此时后续搜索会**静默**走降级路径 —— 给用户一句可操作的提示比抛栈更有用。
    """
    import time as _time
    from engine.services import search_index

    # 同 `_cmd_search`：解密目录路径、引擎回调的进度文案都可能含 GBK 编不出的字符
    # （目录名里的 emoji、分表名里的罕见字），必须在任何打印之前设好安全输出。
    _make_output_safe()

    decrypted = args.decrypted_dir or _resolve_decrypted_dir()
    if not os.path.isdir(decrypted):
        _print_missing_dir(decrypted)
        return EXIT_FAILURE

    if getattr(args, 'no_text', False) and getattr(args, 'no_meta', False):
        # 两个开关同时给 = 什么都不建，但 `build_index` 仍会执行 `DELETE FROM msg_text`
        # （text=False 的既定语义）并把 index_meta 的计数重写成 0 —— 实际结果是
        # **已有的索引被清空、状态变成"未就绪"，而这一次什么都没建**。
        # （`index_status.ready = schema_ok and (fts_rows or meta_rows)`：两个计数都是
        # 0 时 ready=False，所以这不是"看起来就绪的空索引"，而是白白毁掉好索引。）
        # 用户不可能想要这个结果，直接拒绝而不是静默通过。
        print("错误: --no-text 与 --no-meta 不能同时使用（至少要构建一种索引）")
        return EXIT_FAILURE

    def _progress(stage, message, pct):
        print("  [%-8s] %3d%%  %s" % (stage, int(pct * 100), message))

    print("=" * 56)
    print("  全局搜索索引")
    print("=" * 56)
    print("  解密目录: " + str(decrypted))
    print("  模式: " + ("增量刷新" if args.refresh else "全量构建"))
    print()

    t0 = _time.time()
    try:
        if args.refresh:
            res = search_index.refresh_index(decrypted, progress=_progress)
        else:
            res = search_index.build_index(decrypted, text=not args.no_text,
                                           meta=not args.no_meta, force=True,
                                           progress=_progress)
    except Exception as e:              # noqa: BLE001 - 见 docstring（T8-A1）
        print("错误: " + str(e))
        print("  提示: 索引未更新；请检查解密目录、磁盘空间后重试")
        return EXIT_FAILURE

    st = search_index.index_status(decrypted)
    print()
    if res.get('skipped'):
        print("源数据未变化，无需刷新。")
    else:
        print("完成: 元数据 %s 行, 全文 %s 行, 耗时 %.1fs"
              % (res.get('meta_rows'), res.get('fts_rows'), _time.time() - t0))
    print("索引文件: " + search_index.index_path(decrypted))
    print("状态: " + ("就绪" if st.get('ready') else "未就绪"))
    return EXIT_OK


def _export_contacts_cmd(args):
    """导出通讯录（联系人 / 群聊）为 xlsx / csv / html。"""
    from contacts_export import FORMATS, KIND_LABELS, export_contacts

    fmt = (getattr(args, 'format', None) or 'xlsx').strip().lower()
    if fmt not in FORMATS:
        print("错误: 通讯录导出支持的格式为 %s，收到: %s"
              % (' / '.join(FORMATS), fmt))
        print("提示: jsonl / json 是 chatlab 专用格式，请配合 -m chatlab 使用。")
        return

    kind = (getattr(args, 'kind', None) or 'all').strip().lower()
    if kind not in KIND_LABELS:
        print("错误: 不支持的导出范围 --kind %s（可选: %s）"
              % (kind, ' / '.join(KIND_LABELS)))
        return

    decrypted = args.decrypted_dir or _resolve_decrypted_dir()
    if not os.path.isdir(decrypted):
        _print_missing_dir(decrypted)
        return

    out_path = _contacts_out_path(args.output, fmt, kind)

    print("=" * 56)
    print("  通讯录导出")
    print("=" * 56)
    print("  解密目录: " + str(decrypted))
    print("  范围: " + KIND_LABELS[kind])
    print("  格式: " + fmt)
    if getattr(args, 'chat', None):
        print("  搜索: " + str(args.chat))
    if getattr(args, 'has_chat', False):
        print("  筛选: 仅有聊天记录的联系人")
    if getattr(args, 'letter', None):
        print("  首字母: " + str(args.letter))
    if getattr(args, 'label', None):
        print("  标签: " + str(args.label))
    print("  输出: " + str(out_path))
    print()

    try:
        res = export_contacts(
            decrypted, out_path, fmt=fmt, kind=kind,
            q=getattr(args, 'chat', None) or '',
            has_chat='1' if getattr(args, 'has_chat', False) else None,
            letter=getattr(args, 'letter', None) or '',
            sort=getattr(args, 'sort', None) or 'name',
            label=getattr(args, 'label', None),
            print_fn=print,
        )
    except (ValueError, RuntimeError) as e:
        print("错误: " + str(e))
        return

    if res['count'] == 0:
        print("提示: 没有匹配的通讯录记录。若通讯录为空，请先执行 backup 提取联系人数据。")


def _parse_date_arg(text, end_of_day=False):
    """``YYYY-MM-DD`` → 本地时区时间戳（``end_of_day`` 取当天 23:59:59）。"""
    if not text:
        return None
    from datetime import datetime
    from engine.constants import TZ
    dt = datetime.strptime(str(text).strip(), '%Y-%m-%d').replace(tzinfo=TZ)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def cmd_voice_export(args):
    """按人批量导出语音留言（独立 HTML / 合并音频 / 逐条文件）。"""
    from engine.services.voice_export import export_voices

    decrypted = getattr(args, 'decrypted_dir', None) or _resolve_decrypted_dir()
    if not os.path.isdir(decrypted):
        _print_missing_dir(decrypted)
        return 1
    chat = (getattr(args, 'chat', '') or '').strip()
    senders = [s for s in (getattr(args, 'sender', None) or []) if s]
    if not chat and not senders:
        print('至少要给 --chat 或 --sender 之一（例：--chat 张三 --sender 爷爷）')
        print('提示：不知道会话怎么写时，先用 `--chat <名字>` 不带 --sender，会把该会话里所有人的语音都导出。')
        return 2
    if chat:
        # --chat 允许填显示名（人手打的名字），先解析成真实 wxid
        from engine.services.voice_export.collect import resolve_chat_target
        resolved, matches = resolve_chat_target(decrypted, chat)
        if matches:
            print('「%s」匹配到多个会话，请改用 wxid 精确指定其中之一：' % chat)
            for item in matches[:20]:
                print('  %s  %s' % (item['username'], item['display_name']))
            return 2
        chat = resolved or chat

    layouts = tuple(x.strip() for x in (getattr(args, 'layout', '') or '').split(',') if x.strip())
    if not layouts:
        layouts = ('html-folder', 'html-inline', 'files', 'merged')
    out_root = getattr(args, 'out', None) or os.path.join(_project_root(), 'export', 'voice')

    def _progress(stage, message):
        print('[%s] %s' % (stage, message), flush=True)

    try:
        report = export_voices(
            decrypted, out_root,
            chats=[chat] if chat else None,
            senders=senders or None,
            start_ts=_parse_date_arg(getattr(args, 'from_date', None)),
            end_ts=_parse_date_arg(getattr(args, 'to_date', None), end_of_day=True),
            include_other_chats=bool(getattr(args, 'include_other_chats', False)),
            fmt=(getattr(args, 'format', None) or 'mp3'),
            layouts=layouts,
            merge_by=(getattr(args, 'merge_by', None) or 'person'),
            split=(getattr(args, 'split', None) or 'single'),
            gap_s=float(getattr(args, 'gap', 1.0) or 0.0),
            keep_silk=bool(getattr(args, 'keep_silk', False)),
            include_asr=bool(getattr(args, 'include_asr', False)),
            workers=int(getattr(args, 'workers', 4) or 1),
            mp3_quality=int(getattr(args, 'mp3_quality', 7) or 7),
            zip_output=bool(getattr(args, 'zip_output', True)),
            progress_fn=_progress)
    except RuntimeError as exc:                     # 例如选了 m4a 但没有 ffmpeg
        print('✗ %s' % exc)
        return 3

    print('完成：%d 条，缺失 %d 条，总时长 %.1f 分钟'
          % (report['count'], report['missing'], report['duration_total_s'] / 60.0))
    print('输出目录：%s' % report['out_dir'])
    if report.get('zip'):
        print('打包：%s' % report['zip'])
    for err in report.get('errors', []):
        print('提示：%s' % err)
    if report['missing']:
        print('缺失明细见 missing.csv（通常是该条不在当前备份里，重跑一次「一键备份」可补齐）')
    return 0


def cmd_export(args):
    """Export chat data in various formats."""
    mode = args.mode

    if mode == 'chat':
        from chat_export import export_all_contacts

        decrypted = args.decrypted_dir or _resolve_decrypted_dir()
        if not os.path.isdir(decrypted):
            _print_missing_dir(decrypted)
            return

        # 聊天记录只支持 txt / html；xlsx、csv 属于通讯录，jsonl/json 属于 chatlab
        fmt = (getattr(args, 'format', None) or 'txt').strip().lower()
        if fmt not in ('txt', 'html'):
            print("提示: 聊天记录导出支持 txt / html，%s 已回退为 txt。" % fmt)
            print("      通讯录导出请用 -m contacts --format xlsx|csv|html。")
            fmt = 'txt'

        out_dir = args.output or os.path.join(_project_root(), 'export')
        print("=" * 56)
        print("  聊天记录导出")
        print("=" * 56)
        print("  解密目录: " + str(decrypted))
        print("  输出目录: " + str(out_dir))
        print("  格式: " + fmt)
        if getattr(args, 'chat', None):
            print("  过滤: " + str(args.chat))
        print()

        results = export_all_contacts(
            decrypted, out_dir,
            name_filter=getattr(args, 'chat', None),
            fmt=fmt, print_fn=print,
            include_asr=bool(getattr(args, 'include_asr', False)), transcribe_missing=bool(getattr(args, 'transcribe_missing', False)))

        print()
        print("完成: %d 个会话导出" % len(results))
    elif mode == 'contacts':
        _export_contacts_cmd(args)
    elif mode == 'wordcloud':
        from wordcloud_gen import generate_wordcloud
        decrypted = args.decrypted_dir or _resolve_decrypted_dir()
        generate_wordcloud(decrypted, chat_info=args.chat, out_path=args.output)
    elif mode == 'report':
        from report_gen import generate_report
        decrypted = args.decrypted_dir or _resolve_decrypted_dir()
        generate_report(decrypted)
    elif mode == 'employee':
        from employee_match import run_employee_export
        excel = getattr(args, 'excel', None)
        if not excel:
            print("错误: employee 模式需要 --excel 指定员工名单 Excel 文件")
            return
        decrypted = args.decrypted_dir or _resolve_decrypted_dir()
        if not os.path.isdir(decrypted):
            _print_missing_dir(decrypted)
            return
        out_dir = args.output or os.path.join(_project_root(), 'export')
        print("=" * 56)
        print("  员工报表导出")
        print("=" * 56)
        print("  解密目录: " + str(decrypted))
        print("  员工名单: " + str(excel))
        print("  输出目录: " + str(out_dir))
        print()
        run_employee_export(decrypted, excel, out_dir, print_fn=print)
    elif mode == 'list':
        from chat_list import list_chats
        decrypted = args.decrypted_dir or _resolve_decrypted_dir()
        if not os.path.isdir(decrypted):
            _print_missing_dir(decrypted)
            return
        list_chats(decrypted, name_filter=getattr(args, 'chat', None))
    elif mode == 'chatlab':
        _export_chatlab_cmd(args)
    elif mode == 'keys':
        from key_scan import run_key_scan
        from engine.utils import is_wechat_running
        db_dir = args.db_dir or _resolve_db_dir()
        out_file = None  # keys are saved to .wechat_exp_config.json by default
        if not db_dir:
            from engine.utils import data_dir_hint
            print(data_dir_hint())
            return
        print("=" * 56)
        print("  密钥提取")
        print("=" * 56)
        if not is_wechat_running():
            print("提示: 当前未检测到微信进程。")
            print("      请先启动微信并登录，然后重新运行本命令。")
            print("      提取密钥只需要微信保持运行，无需管理员权限。")
            print()
        else:
            print("已检测到微信运行中 — 开始只读提取密钥...")
            print()
        run_key_scan(db_dir, out_file)
    elif mode == 'decrypt':
        from engine.decrypt import run_decrypt
        from engine.config_file import set_backup_data_dir
        db_dir = args.db_dir or _resolve_db_dir()
        out_dir = args.output or os.path.join(BASE, '..', 'output', 'decrypted')
        success, failed, skipped = run_decrypt(keys_file=None, db_dir=db_dir, out_dir=out_dir)
        if success > 0:
            set_backup_data_dir(out_dir)
    else:
        print(f"未知导出模式: {mode}")


_STAGE_LABELS: dict[str, str] = {
    "scan": "扫描账号",
    "decrypt": "解密数据库",
    "migrate": "迁移媒体",
    "index": "构建索引",
    "done": "完成",
}


def _make_progress_display():
    """Return an on_progress callback that renders a visual progress bar."""
    import time as _time
    start_time = _time.time()

    # Pick safe progress-bar characters for the current stdout encoding.
    # GBK (cp936) cannot encode Unicode box-drawing chars (█░), so we fall
    # back to ASCII when the encoding doesn't support them.
    try:
        "█░".encode(sys.stdout.encoding or 'utf-8')
        _fill_char, _empty_char = "█", "░"
    except (UnicodeEncodeError, LookupError):
        _fill_char, _empty_char = "#", "-"

    def _display(stage: str, detail: str, progress: float):
        elapsed = _time.time() - start_time
        bar_width = 28
        filled = int(bar_width * min(progress, 1.0))
        bar = _fill_char * filled + _empty_char * (bar_width - filled)
        pct = int(progress * 100)
        label = _STAGE_LABELS.get(stage, stage)

        # Build the line
        line = f"  {label:8s} [{bar}] {pct:3d}%  {detail}"
        # Pad to 80 chars to clear previous output
        line = line.ljust(100)
        print(f"\r{line}", end="", flush=True)

        if stage == "done" or progress >= 1.0:
            mins, secs = divmod(int(elapsed), 60)
            print(f"\n  耗时: {mins}分{secs}秒")

    return _display


def cmd_harvest_keys(args):
    """Continuously scan WeChat memory for V2 image AES keys and cache them.

    The harvester pre-loads all V2 .dat files from the backup, then polls
    WeChat process memory. As the user scrolls through chats in WeChat,
    image keys appear in memory and are captured + cached to _media_keys.json.

    Run this before viewing a backup offline — once keys are cached, V2 images
    decrypt without needing WeChat.
    """
    from engine.services.v2_key_extract import harvest_v2_keys, is_wechat_running
    from engine.config_file import get_backup_wxid

    decrypted = args.decrypted_dir or _resolve_decrypted_dir()
    wxid = args.wxid or get_backup_wxid()

    if not os.path.isdir(decrypted):
        print(f"错误: 解密目录不存在: {decrypted}")
        print("请先运行 backup 命令，或使用 --decrypted-dir 指定目录")
        return

    print(f"V2 密钥收割器")
    print(f"  解密目录: {decrypted}")
    if wxid:
        print(f"  账号: {wxid}")
    print()

    if not is_wechat_running():
        print("微信未运行。请先启动微信并浏览包含图片的聊天记录，")
        print("使图片密钥加载到内存中，然后重新运行此命令。")
        return

    print("开始扫描微信内存中的 V2 图片密钥...")
    print("请在微信中滚动浏览包含图片的聊天记录。")
    print("按 Ctrl+C 停止。")
    print()

    found = harvest_v2_keys(
        decrypted, wxid=wxid,
        interval=args.interval,
        max_rounds=args.max_rounds,
        print_fn=print
    )

    print()
    if found:
        print(f"成功获取 {len(found)} 个密钥！已缓存到 _media_keys.json")
        print("现在可以离线查看这些图片了。")
    else:
        print("未获取到新密钥。")
        if is_wechat_running():
            print("提示: 请在微信中打开更多包含图片的聊天记录后重试。")


def _cmd_quick(args):
    """Quick-test mode: skip backup, open chat viewer directly to a contact."""
    from web.app import run_server
    from engine.config_file import get_backup_wxid

    decrypted = args.decrypted_dir if hasattr(args, 'decrypted_dir') and args.decrypted_dir else _resolve_decrypted_dir()
    db_dir = args.db_dir if hasattr(args, 'db_dir') and args.db_dir else _resolve_db_dir()
    wxid = get_backup_wxid()

    if not os.path.isdir(decrypted):
        print(f"错误: 解密目录不存在: {decrypted}")
        print("请先运行 backup 命令，或使用 python main.py serve --decrypted-dir <目录>")
        return

    host = '127.0.0.1'
    port = 5000
    if args.contact:
        from urllib.parse import quote
        chat_url = f'http://{host}:{port}/chat?contact={quote(args.contact)}'
    else:
        chat_url = f'http://{host}:{port}/chat'

    print(f"快速测试模式")
    print(f"  解密目录: {decrypted}")
    if args.contact:
        print(f"  目标联系人: {args.contact}")
    print(f"  地址: {chat_url}")
    run_server(decrypted, wxid=wxid, db_dir=db_dir, host=host, port=port,
               open_url=chat_url)


def main():
    parser = argparse.ArgumentParser(
        description=f'WeChat EXP v{__version__} — 微信聊天记录备份与查看工具'
    )
    parser.add_argument('--version', '-V', action='version',
                        version=f'WeChat EXP {__version__}')
    parser.add_argument('--quick', '-q', action='store_true',
                        help='快速测试: 跳过备份，直接打开聊天查看器 (使用已有解密数据)')
    parser.add_argument('--contact', '-c', default=None,
                        help='快速测试: 自动打开指定联系人的聊天 (配合 --quick 使用)')
    sub = parser.add_subparsers(dest='command', help='可用命令')

    # backup
    bp = sub.add_parser('backup', help='备份微信聊天记录')
    bp.add_argument('--db-dir', help='微信 db_storage 目录路径')
    bp.add_argument('--output', '-o', help='备份输出目录')
    bp.add_argument('--key-file', help='密钥文件路径 (可选，默认从配置自动加载)')
    bp.add_argument('--days', type=int, default=30,
                    help='备份最近N天的聊天记录 (默认: 30, 0=全部)')
    bp.add_argument('--date-from', help='起始日期 YYYY-MM-DD (覆盖 --days)')
    bp.add_argument('--date-to', help='截止日期 YYYY-MM-DD (覆盖 --days)')
    bp.add_argument('--wxid', help='指定备份的微信账号 (多个账号时必选)')
    bp.add_argument('--link-dest', help='前一次备份目录，媒体文件优先从该目录硬链接复用')
    bp.add_argument('--no-harvest', action='store_true',
                    help='禁用后台 V2 图片密钥收割')

    # serve
    sp = sub.add_parser('serve', help='启动 Web 聊天记录查看器')
    sp.add_argument('--decrypted-dir', help='解密后的数据目录')
    sp.add_argument('--db-dir', help='微信 db_storage 目录 (用于媒体解析)')
    sp.add_argument('--host', default='127.0.0.1')
    sp.add_argument('--port', type=int, default=5000)

    sy = sub.add_parser('sync', help='独立同步微信源库中的新消息（Ctrl+C 停止）')
    sy.add_argument('--db-dir', help='微信 db_storage 目录；默认使用已配置目录')
    sy.add_argument('--output', help='独立的空实时目录；默认 output/live_sync/<账号标识>')
    sy.add_argument('--interval', type=float, default=1, help='提交状态复核间隔（秒，默认 1）')
    sy.add_argument('--once', action='store_true', help='执行一次同步并退出')

    # export
    ep = sub.add_parser('export', help='导出聊天记录 / 通讯录 / 词云 / 报告 / 员工报表')
    ep.add_argument('--transcribe-missing', dest='transcribe_missing', action='store_true', help='导出时给还没转写的语音现场识别一次（较慢）')
    ep.add_argument('--include-asr', dest='include_asr', action='store_true', help='导出时带上已转写的语音文字（只读转写库，不触发识别）')
    ep.add_argument('--mode', '-m', required=True,
                    choices=['chat', 'contacts', 'wordcloud', 'report', 'employee',
                             'list', 'keys', 'decrypt', 'chatlab'],
                    help='导出模式（contacts = 导出通讯录为 xlsx/csv/html；'
                         'chatlab = 导出 ChatLab 标准格式，支持断点续传）')
    ep.add_argument('--format', '-f', choices=['jsonl', 'json', 'xlsx', 'csv', 'html'],
                    default=None,
                    help='输出格式：chatlab 用 jsonl/json（默认 jsonl），'
                         'contacts 用 xlsx/csv/html（默认 xlsx），'
                         'chat 用 txt/html（默认 txt）')
    ep.add_argument('--no-resume', action='store_true',
                    help='chatlab 模式禁用断点续传')
    ep.add_argument('--chat',
                    help='指定聊天对象 (wordcloud 模式) / 名称过滤 (chat、list 模式) / '
                         '搜索关键词 (contacts 模式)')
    ep.add_argument('--kind', choices=['all', 'contacts', 'groups'], default='all',
                    help='contacts 模式导出范围 (默认: all)')
    ep.add_argument('--has-chat', action='store_true',
                    help='contacts 模式只导出有聊天记录的联系人')
    ep.add_argument('--letter', help='contacts 模式按显示名首字母筛选')
    ep.add_argument('--label',
                    help='contacts 模式按微信标签筛选（如 only_work）')
    ep.add_argument('--sort', choices=['name', 'msg_count', 'last_time'],
                    default='name', help='contacts 模式排序方式 (默认: name)')
    ep.add_argument('--output', '-o',
                    help='输出路径（文件或已存在目录）；contacts 模式省略时写入 '
                         '<项目根>/export/contacts_<时间戳>.<ext>')
    ep.add_argument('--excel', help='员工 Excel 文件路径 (employee 模式)')
    ep.add_argument('--decrypted-dir', help='解密后的数据目录')
    ep.add_argument('--db-dir', help='微信 db_storage 目录')

    # voice-export（按人批量导出语音留言）
    vp = sub.add_parser('voice-export',
                        help='按人批量导出语音留言（独立 HTML / 合并音频 / 逐条音频文件）')
    vp.add_argument('--chat', default='', help='会话 wxid 或显示名')
    vp.add_argument('--sender', action='append', default=[],
                    help='发送者（可重复；留空 = 该会话全部人）')
    vp.add_argument('--include-other-chats', action='store_true',
                    help='把该人在其它会话里的语音也一起导出（全库扫描，较慢）')
    vp.add_argument('--from', dest='from_date', default=None, help='开始日期 YYYY-MM-DD')
    vp.add_argument('--to', dest='to_date', default=None, help='结束日期 YYYY-MM-DD')
    vp.add_argument('--format', choices=['mp3', 'wav', 'm4a'], default='mp3',
                    help='音频格式（m4a 需要 ffmpeg；默认 mp3）')
    vp.add_argument('--layout', default='html-folder,html-inline,files,merged',
                    help='逗号分隔：html-folder,html-inline,files,merged')
    vp.add_argument('--merge-by', dest='merge_by', choices=['person', 'chat', 'none'],
                    default='person', help='合并分组方式（默认 person）')
    vp.add_argument('--split', choices=['single', 'per-day', 'per-month'], default='single',
                    help='合并文件拆分方式（默认 single）')
    vp.add_argument('--gap', type=float, default=1.0, help='合并时段间静音秒数（默认 1.0）')
    vp.add_argument('--out', default=None, help='输出根目录（默认 <项目根>/export/voice）')
    vp.add_argument('--keep-silk', action='store_true', help='同时保留原始 .silk 文件')
    vp.add_argument('--workers', type=int, default=4, help='解码并行线程数（默认 4）')
    vp.add_argument('--mp3-quality', dest='mp3_quality', type=int, default=7,
                    choices=range(0, 10), metavar='0-9',
                    help='MP3 编码质量 0=最好最慢 … 9=最快（默认 7；体积/码率不变）')
    vp.add_argument('--include-asr', dest='include_asr', action='store_true', help='导出时带上已转写的语音文字（只读转写库，不触发识别）')
    vp.add_argument('--no-zip', dest='zip_output', action='store_false', default=True,
                    help='不打包 zip')
    vp.add_argument('--decrypted-dir', help='解密后的数据目录')
    vp.set_defaults(func=cmd_voice_export)

    # chatlab-pull
    cp = sub.add_parser('chatlab-pull',
                        help='启动 ChatLab Pull 远程数据源服务（供 ChatLab 拉取）')
    cp.add_argument('--decrypted-dir', help='解密后的数据目录')
    cp.add_argument('--host', default='127.0.0.1',
                    help='监听地址 (默认: 127.0.0.1；局域网可填 0.0.0.0)')
    cp.add_argument('--port', type=int, default=8765, help='监听端口 (默认: 8765)')
    cp.add_argument('--token', default=None,
                    help='可选 Bearer Token（设置后 ChatLab 需填相同 Token）')

    # import-keys
    kp = sub.add_parser('import-keys',
                        help='手动输入数据库密钥（已有密钥时使用，自动校验并保存）')
    kp.add_argument('--file', '-f', help='密钥文本文件（每行一条，写法见 README）')
    kp.add_argument('--key', '-k', action='append', default=None,
                    help='直接指定一条密钥，可重复；形如 message_0.db=<64位hex> 或只写密钥')
    kp.add_argument('--db-dir', help='微信 db_storage 目录（默认自动检测）')
    kp.add_argument('--force', action='store_true',
                    help='强制保存未通过校验的密钥（需该行写明数据库名）')
    kp.add_argument('--dry-run', action='store_true', help='只校验不保存')
    kp.add_argument('--list', action='store_true', help='仅显示当前密钥覆盖情况')
    kp.add_argument('--export', metavar='路径',
                    help='把识别出的「数据库 = 密钥」对应关系导出成可回读的清单文件'
                         '（含完整密钥，勿外传/勿入版本库）')

    # harvest-keys
    hp = sub.add_parser('harvest-keys', help='收割 V2 图片 AES 密钥（需微信运行）')
    hp.add_argument('--decrypted-dir', help='解密后的数据目录')
    hp.add_argument('--wxid', help='微信用户 ID（自动检测）')
    hp.add_argument('--interval', type=float, default=2.0,
                    help='扫描间隔秒数 (默认: 2.0)')
    hp.add_argument('--max-rounds', type=int, default=None,
                    help='最大扫描轮次 (默认: 无限，直到 Ctrl+C)')

    # search
    sep = sub.add_parser('search', help='全局搜索聊天记录（摘要 + 筛选 + 正则）')
    sep.add_argument('query',
                     help='查询串，语法：维修 会话:张三 类型:图片 '
                          '日期:2026-01..2026-03 /报修|维修/')
    sep.add_argument('--json', action='store_true',
                     help='输出原始 JSON（完整响应体，供脚本解析）')
    sep.add_argument('--page', type=int, default=1, help='页码 (默认: 1)')
    sep.add_argument('--per-page', type=int, default=50,
                     help='每页条数 (默认: 50，上限 200)')
    sep.add_argument('--sort', choices=['time_desc', 'time_asc', 'chat'],
                     default='time_desc',
                     help='排序方式 (默认: time_desc)')
    sep.add_argument('--decrypted-dir', help='解密后的数据目录')

    # build-search-index
    bip = sub.add_parser('build-search-index',
                         help='构建全局搜索索引（默认全量；--refresh 增量刷新）')
    bip.add_argument('--refresh', action='store_true',
                     help='增量刷新（源未变则跳过）')
    bip.add_argument('--no-text', action='store_true', help='只建元数据索引')
    bip.add_argument('--no-meta', action='store_true', help='只建全文索引')
    bip.add_argument('--decrypted-dir', help='解密后的数据目录')

    args = parser.parse_args()
    if args.command is None:
        if args.quick:
            _cmd_quick(args)
        else:
            print("启动 Web 管理面板...")
            print("提示: 使用 python main.py --help 查看所有可用命令")
            cmd_serve(argparse.Namespace(
                decrypted_dir=None, db_dir=None, host='127.0.0.1', port=5000))
        return
    elif args.command == 'backup':
        cmd_backup(args)
    elif args.command == 'serve':
        cmd_serve(args)
    elif args.command == 'sync':
        _exit_with(cmd_sync(args))
    elif args.command == 'export':
        cmd_export(args)
    elif args.command == 'voice-export':
        _exit_with(cmd_voice_export(args))
    elif args.command == 'chatlab-pull':
        cmd_chatlab_pull(args)
    elif args.command == 'import-keys':
        cmd_import_keys(args)
    elif args.command == 'harvest-keys':
        cmd_harvest_keys(args)
    elif args.command == 'search':
        _exit_with(_cmd_search(args))
    elif args.command == 'build-search-index':
        _exit_with(_cmd_build_search_index(args))


if __name__ == '__main__':
    main()
