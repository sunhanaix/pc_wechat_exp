# 实时消息同步

在仪表盘打开“实时消息同步”（`/sync`），确认当前账号的 `db_storage` 目录，点击“启动同步”。首次同步完成后，当前 Web 查看器自动切换到独立实时目录；聊天页通过 SSE 自动更新，保留当前筛选和页码。

停止后保留已同步数据，查看器仍可读取；关闭 Web 程序会停止后台线程，下次需重新启动。程序不会修改原始微信数据库，也不会覆盖原备份。

## 独立运行

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe src\main.py sync
```

默认源目录来自已有配置，默认输出为 `output/live_sync/<源目录标识>`。也可以指定：

```powershell
.\.venv\Scripts\python.exe src\main.py sync --db-dir "C:\路径\db_storage" --output "F:\微信实时数据" --interval 1
```

`--once` 同步一次后退出；持续运行时按 Ctrl+C 停止。输出目录必须为空或属于同一源目录的既有同步服务，多个进程不能同时写同一实时目录。它不能与源目录相同、互相包含或覆盖已有备份。

CLI 模式只同步数据。独立查看器可通过 `serve --decrypted-dir <实时目录> --db-dir <源目录>` 读取；要使用自动页面通知，请在 Web 程序的“实时消息同步”页启动服务。

ChatLab Pull 服务读取启动时选择的数据目录，因此应先启动同步，再启动 Pull；若 Pull 先前已经启动，请停止再启动，使其读取实时目录。现有 Pull SSE 的 3 秒检测机制继续生效。

## 同步流程

- Windows 原生目录变化通知触发检查，并合并短时间的连续事件；每秒还会复核源库和 WAL 提交状态，通知不可用时自动使用轮询。
- 监测所有 `message/message_数字.db` 及对应 WAL，也同步 `contact/contact.db`、`session/session.db`、`hardlink/hardlink.db` 和 `message/message_resource.db`。运行中会发现新增消息分片。
- 复核读取 WAL 头、主库文件元数据、`-shm` 中两份 WAL-index 头；这能检测文件大小不变的提交和日志代次变化，不依赖 `-shm` 修改时间。
- 按 SQLite 标准 VFS 的字节锁取得共享锁，暂时排除源库写入、checkpoint 和恢复，复制主库/WAL后立即释放锁；解密、校验和索引在释放源锁后执行。源库忙时稍后重试，不强行读取。
- 校验 WAL 头、代次 salt、累计 checksum，截取最后一个有效提交边界；未提交的尾部不参与合并，旧日志代次的残留帧被忽略。解密使用已有密钥，每个最终页面验证 HMAC。
- 生成临时解密快照并通过 SQLite 完整性快速检查，再用 SQLite backup 事务更新实时副本；避免覆盖 Windows 下已经打开的数据库文件。
- 消息索引以 `(源分片, 会话, local_id, create_time)` 为唯一标识，包含内容摘要，用于发现新增、修改、删除；同秒消息和不同分片重复 ID 均可保留。
- 同步进度/去重记录保存在实时目录的 `data/chats.db`。源库忙、缺密钥、校验失败时不记录成功进度，持续重试；停止重启后重新核对副本和索引。
- 数据提交后再发送通知。消息已写回主库但内容未变时，不会再次计入新增消息。联系人变化会使姓名和通讯录缓存失效。

锁约定参考 [SQLite WAL-index 文档](https://www.sqlite.org/walformat.html)；当前实现适用于标准 Windows/unix SQLite VFS。若微信未来修改底层 VFS 或加密格式，需要重新验证兼容性。

## HTTP 接口

| 接口 | 功能 |
| --- | --- |
| `POST /api/sync/start` | JSON 可选字段：`db_dir`、`output_dir`、`interval`；后台启动返回 202，已运行返回 409 |
| `GET /api/sync/status` | 运行/就绪状态、查看器是否读取实时目录、错误、同步文件数、新增消息数、最近同步时间 |
| `POST /api/sync/stop` | 请求停止；若任务仍在执行，响应里的 `running` 保持 true，可继续查询 |
| `GET /api/sync/events` | SSE `data.updated` 通知，仅携带更新的会话和计数；重连时要求重新拉取最新数据 |

首次同步视作历史导入，不计入“本次新增消息”。状态里的 `ready` 表示当前消息分片已成功同步；辅助库若缺密钥会在 `errors` 中逐项显示。

## 第一版范围与性能

第一版在检测到变化后，对发生变化的数据库生成完整快照；消息索引负责增量识别和去重。它尚未实现只复制/解密新增 WAL 页的性能优化，因此大分片的复制、解密和索引可能影响同步延迟，源快照复制期间也会暂时阻挡微信写入。状态里的 `source_lock_seconds` 可用于观察最近一次源锁持有时间。

各数据库分别以事务更新，不保证联系人、会话、所有消息分片在同一跨库事务中提交。媒体文件不自动备份，媒体资源是否可用取决于微信源文件仍在本机和现有解析器的支持。

微信搜索辅助库 `message_fts.db`、全局搜索索引和语音转写结果不在当前同步范围；实时目录中的聊天查看器可直接按会话筛选消息，但完整全局全文搜索需要后续接入。

## 验证

`tests/test_live_sync.py` 使用本机 SQLite 生成真实 WAL，并按项目页面格式加密，验证已提交事务、未提交尾部、checksum/HMAC拒绝、checkpoint/重启去重、跨分片 ID、同大小提交检测、锁冲突和失败重试。

`tests/test_live_sync_api.py` 验证启动/停止、目录切换、现有消息/联系人 API、SSE重连与断开清理和参数校验。
