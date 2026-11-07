# 第五阶段：AstrBot Host（2026-11-07）

适用：让 AstrBot 插件与 NoneBot、Koishi 插件一样，在原生框架中运行，并通过同一 MCP 工具面与事件/Gateway 合同被任意 bot 或 Coding Agent 使用。方向依据 [ADR 0002](../adr/0002-plugins-as-tools.md)，工具面见[插件工具面计划](2026-11-07-plugin-tool-surface.md)。

## 1. 已核实的事实

依据对 AstrBot 4.29.1 源码（PyPI sdist，只读）的研究与本机实测：

| 项 | 结论 |
| --- | --- |
| 版本 | `astrbot==4.29.1`，2026-10-17 发布（满足“发布两周以上”），Python ≥3.12，AGPL-3.0-or-later；本 Host 锁定于 `hosts/astrbot/uv.lock`（156 个包） |
| 插件模型 | 插件目录含 `main.py` 与 `metadata.yaml`；`Star` 子类自动注册；`filter.command(name, alias)`、`command_group`、`regex`、权限、平台类型等装饰器；回复用 `yield event.plain_result(...)` 或 `await event.send(MessageChain)` |
| 平台适配器 | `register_platform_adapter` + `Platform` 子类，构造参数 `(platform_config, platform_settings, event_queue)`；事件经 `commit_event` 进入 AstrBot 事件队列，由 PipelineScheduler 执行全部阶段 |
| 无头运行 | 可直接使用真实 `AstrBotCoreLifecycle`：预设配置关闭 LLM provider、远程 t2i 与遥测，只启用桥接平台，不启动 dashboard（已实测） |
| 导入副作用 | `import astrbot.core` 会在数据根下创建 `data/`、写 `cmd_config.json`、打开 SQLite；部分存储（知识库、模板）使用当前目录。必须先设 `ASTRBOT_ROOT` 与工作目录再导入（已实测并修正） |
| 命令登记 | `star_handlers_registry` 中的 `CommandFilter` 带命令名、别名、父命令组与按函数签名推出的类型化参数（`GreedyStr` 为剩余整行） |
| 插件市场 | 约 3,267 个条目，带 `download_url` 与 `commit_sha`，但 **AstrBot 自身不校验任何哈希**，按仓库安装时取分支 HEAD，不可复现 |

## 2. 设计

- **原生框架，不重写。** 使用未修改的 AstrBot，Ligamina 只提供 `ligamina_bridge` 平台适配器与 stdio worker，独立进程运行，不复制 AGPL 代码。Stella 的 `astrbot_compat` 是独立重写，不作为本 Host 的基础。
- **完成信号。** PipelineScheduler 在 `finally` 中调用事件的 `cleanup_temporary_local_files()`；桥接事件覆盖该方法作为“本事件处理完毕”的信号，`event.dispatch` 据此返回，不靠超时猜测。
- **工具面。** 命令描述 `framework = "astrbot"`；工具会话使用与 Koishi 相同的字符串身份（`ligamina.tool` / `ligamina-tool`）。类型化参数生成结构化字段；AstrBot 按空白切分且不支持引号，所以只有最后一个 `GreedyStr` 参数可含空格，其余含空格的值被拒绝并提示改用原样文本。
- **安全边界。** AstrBot 内置 star（`builtin_commands`、`astrbot`，含插件安装与配置命令）永不作为工具暴露；工具会话用户为普通成员。
- **日志与协议。** worker 保留真实 stdout 只写协议帧，把 `sys.stdout` 指向 stderr。

## 3. 任务

| 任务 / feature | 依赖 | 交付 | 完成标准 |
| --- | --- | --- | --- |
| AB-01 / feat-037 | TOOL-01–03 | Host（生命周期、桥接适配器、命令发现、worker）、Core 的 AstrBot 工具身份与结构化参数、宿主冒烟与 Rust 端到端测试 | 真实 AstrBot 在 Core 下经 MCP 列出并调用自写插件命令（类型化参数、整行参数、多条发送、并发、群/私聊），内置 star 不暴露；2026-11-07 完成 |
| AB-02 / feat-038 | AB-01 | 事件路径对齐：@ 与引用、图片、管理员映射与权限过滤、`send_by_session` 主动发送（经账本）、平台适配器类型兼容策略（是否以 `aiocqhttp` 名义注册） | 每项有原生语义测试；不支持的 API 显式 `unsupported_api` |
| AB-03 / feat-039 | AB-01 | 冻结 AstrBot 插件目录：市场快照 + 自行计算的下载包 SHA-256、按插件冻结依赖锁、不可复现条目的明确终态 | 与第二阶段目录合同同等的可复核快照与锁；不执行插件 |
| AB-04 / feat-040 | AB-03 | Linux Docker Worker 中的 AstrBot 安装/加载普查与 pilot（预装依赖、禁网、禁止运行时 pip） | 账本闭合、清理确认；失败与 unknown 按既有规则留账 |
| AB-05 / feat-041 | AB-04 | 选定 AstrBot 插件的工具级与 Native/Federated 行为验收 | 独立 Oracle、三次 fresh 运行、清理确认；结果单列，不扩大其他框架结论 |

## 4. 已知限制（AB-01 范围内）

- 以 `filter.platform_adapter_type(...)` 限定真实平台的处理器不会匹配桥接平台；依赖平台原始客户端（如 `AiocqhttpMessageEvent.bot`）的插件不可用。
- `send_by_session` 主动发送暂时拒绝。
- 只映射文本、提及、引用、图片四类消息段。
- AstrBot 会在导入失败时用 pip 现场安装依赖，目录插件只能在已准入的 OS 隔离 Worker 中、以预先冻结的依赖运行。

## 5. 验证命令

- 宿主冒烟：`uv run --locked --project hosts/astrbot --python 3.12.14 python hosts/astrbot/tests/bridge_smoke.py`
- 端到端：`cargo test -p ligamina-cli --locked --test command_tools astrbot`（首次运行会按锁安装 AstrBot 环境）
- 两个 check 脚本均已加入宿主冒烟；工作区 `cargo test` 包含端到端测试。
