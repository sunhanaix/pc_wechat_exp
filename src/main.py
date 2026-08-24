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


def cmd_backup(args):
    """Run the full backup pipeline."""
    from engine.utils import find_all_wechat_data_dirs, is_wechat_running
    from engine.config_file import set_backup_data_dir

    if is_wechat_running():
        print("警告: 微信正在运行，建议关闭后再备份以避免数据库锁定。")

    dirs = find_all_wechat_data_dirs()
    if not dirs:
        print("未找到微信数据目录。请确认微信已安装并至少登录过一次。")
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

    print(f"启动 Web 查看器 v{__version__}...")
    print(f"  解密目录: {decrypted}")
    print(f"  地址: http://{args.host}:{args.port}")
    run_server(decrypted, wxid=wxid, db_dir=db_dir, host=args.host, port=args.port)


def cmd_export(args):
    """Export chat data in various formats."""
    mode = args.mode

    if mode == 'chat':
        from chat_export import export_all_contacts
        export_all_contacts()
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
        run_employee_export(args.excel)
    elif mode == 'list':
        from chat_list import list_chats
        list_chats()
    elif mode == 'keys':
        from key_scan import run_key_scan
        from engine.utils import is_wechat_running
        db_dir = args.db_dir or _resolve_db_dir()
        out_file = None  # keys are saved to .wechat_exp_config.json by default
        if not db_dir:
            print("错误: 未找到微信数据目录。请先登录微信，或使用 --db-dir 指定 db_storage 路径")
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

    # export
    ep = sub.add_parser('export', help='导出聊天记录')
    ep.add_argument('--mode', '-m', required=True,
                    choices=['chat', 'wordcloud', 'report', 'employee', 'list', 'keys', 'decrypt'],
                    help='导出模式')
    ep.add_argument('--chat', help='指定聊天对象 (wordcloud 模式)')
    ep.add_argument('--output', '-o', help='输出路径')
    ep.add_argument('--excel', help='员工 Excel 文件路径 (employee 模式)')
    ep.add_argument('--decrypted-dir', help='解密后的数据目录')
    ep.add_argument('--db-dir', help='微信 db_storage 目录')

    # harvest-keys
    hp = sub.add_parser('harvest-keys', help='收割 V2 图片 AES 密钥（需微信运行）')
    hp.add_argument('--decrypted-dir', help='解密后的数据目录')
    hp.add_argument('--wxid', help='微信用户 ID（自动检测）')
    hp.add_argument('--interval', type=float, default=2.0,
                    help='扫描间隔秒数 (默认: 2.0)')
    hp.add_argument('--max-rounds', type=int, default=None,
                    help='最大扫描轮次 (默认: 无限，直到 Ctrl+C)')

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
    elif args.command == 'export':
        cmd_export(args)
    elif args.command == 'harvest-keys':
        cmd_harvest_keys(args)


if __name__ == '__main__':
    main()
