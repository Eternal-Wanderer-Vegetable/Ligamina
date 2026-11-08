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
| AB-02 / feat-038 | AB-01 | 事件路径对齐：@ 与引用、图片、管理员映射与权限过滤、`send_by_session` 主动发送（经账本）、平台适配器类型兼容策略（是否以 `aiocqhttp` 名义注册） | 每项有原生语义测试；不支持的 API 显式 `unsupported_api`；2026-11-08 完成（`tests/event_parity.py`；不以 `aiocqhttp` 名义注册，见第 4 节） |
| AB-03 / feat-039 | AB-01 | 冻结 AstrBot 插件目录：市场快照 + 自行计算的下载包 SHA-256、按插件冻结依赖锁、不可复现条目的明确终态 | 与第二阶段目录合同同等的可复核快照与锁；不执行插件；2026-11-08 完成（`ab-15937e90d26b7caf9d12`：3,271 条，3,265 冻结，3 条超过 64 MB 下载上限，3 条缺 `main.py`） |
| AB-04 / feat-040 | AB-03 | Linux Docker Worker 中的 AstrBot 安装/加载普查与 pilot（预装依赖、禁网、禁止运行时 pip） | 账本闭合、清理确认；失败与 unknown 按既有规则留账；2026-11-08 pilot 完成（见 3.1） |
| AB-05 / feat-041 | AB-04 | 选定 AstrBot 插件的工具级与 Native/Federated 行为验收 | 独立 Oracle、三次 fresh 运行、清理确认；结果单列，不扩大其他框架结论；2026-11-08 工具级完成（见 3.2） |

### 3.1 AB-04 pilot 结果（2026-11-08）

[v4 报告](../../outputs/acceptance/ab04-astrbot-linux-pilot-20261108-v4.json)：从快照按 `registry_entry_id` 分层各取 6 个（plain、requirements、platform_specific、llm_dependent），每个插件一个全新禁网 Worker（准入见 [admission](../../outputs/acceptance/tool04-linux-admission-20261108.json)）。24/24 结清：21 passed，1 failed，2 skipped，unknown 0，清理全部确认；5 个插件离线安装了预下载的依赖，17 个插件共注册 121 条命令，启动期发送 0；单个 Worker 最长 19.8 s、峰值内存 396 MB。

- failed：`astrbot_plugin_tomin` 自身 `initialize()` 抛出 `RuntimeError`，由 AstrBot 记入 `failed_plugin_dict`。
- skipped：一个插件的 `requirements.txt` 含非注释的说明文字，pip 拒绝解析（原生 AstrBot 安装同样失败）；一个依赖 `llama-cpp-python` 无二进制 wheel，按“只用 wheel、不跑构建后端”规则不解析。
- 加载成功只说明安装/加载可行，不代表行为兼容；行为见 AB-05。

pilot 过程中修正的三个 harness 问题（v2、v3 报告作废，未入库）：

1. Worker 端在 AstrBot 停止之后才输出结论，而 `core.stop()` 在禁网 Worker 中无限期阻塞事件循环（且不响应取消），使每个插件都撞上 420 s 超时。现在先输出结论再直接退出，Worker 随即销毁。
2. 加载判定改读 `star_map`（处理器分发与 `commands.py` 使用的映射）。`astrbot_plugin_provider_hub` 已加载、可分发，却不在 `star_registry` 中；报告逐行记录 `in_star_registry`。
3. 插件目录名按 AstrBot 安装器规则取 `metadata.yaml` 的 `name`（须为合法标识符，否则回退到归档根目录名，并记录来源）。插件会写死 `data.plugins.<name>` 导入路径，此前以归档根目录名（带提交哈希）命名导致误判失败。

补记：v4 中有两行回退到归档根目录名。`astrbot_plugin_media302-save` 的 name 含连字符，回退正确；`astrbot_plugin_teamup` 的 metadata.yaml 以 UTF-8 BOM 开头，当时的解析漏读了 name（AB-05 中发现并修正，该行仍为 passed，结论不变）。

### 3.2 AB-05 工具级行为验收（2026-11-08）

[campaign report](../../outputs/acceptance/ab05-astrbot-tool-linux-20261108-v2.json)，SHA-256 `33e2e6871ac41efdd3923c28c9c6b49ada6f8c09dbe3708ea73d15f84405bafb`；gate `astrbot_tool_linux=passed_scoped`。v1 结果相同但作废未保留：其记录的用例文件摘要来自 CRLF 工作副本，与仓库中的 LF 文件不符；v2 以与提交一致的字节重跑。

- 对象：AB-04 中加载成功、且有离线可判定命令的三个插件——`astrbot_plugin_teamup`（plain）、`astrbot_plugin_gallery`（platform_specific）、`astrbot_plugin_pet`（platform_specific，离线安装 9 个 wheel）。其余插件依赖 LLM、外网或真实平台，不适合离线判定。
- 用例：[astrbot-tool-cases.json](../../contracts/lab/automation/tools/v1/astrbot-tool-cases.json)，8 个（正例、负例、权限/隔离），含并发步骤与“无回复”断言。Oracle 由冻结归档的源码静态推导（不运行插件），每条期望回复引用文件摘要、行号以及字符串常量或原文；编排器派发前逐条复核 28 处引用，文本或源码漂移即拒绝。AstrBot 原生权限拒绝的回复引用 AstrBot 4.29.1 源码，并以正则匹配每次调用不同的 `tool-actor-<uuid>`。Worker 只收到 stimulus。
- 执行：`lab/astrbot_tool_campaign.py`，每个用例 3 次，每次一个全新的已准入禁网 Worker（镜像 [astrbot-tool-runtime.Dockerfile](../../deploy/lab/linux/astrbot/astrbot-tool-runtime.Dockerfile)：AB-04 镜像只替换 Core 二进制，`ligamina-astrbot-tool-runtime@sha256:07b6a1ae5a1a3c659294da7abd966f93de54aacd1055fb13efb25762e91b5ba0`）。Worker 内运行真实 Core 守护进程与 AstrBot Host，经 MCP 调用插件命令；虚拟 Gateway 持有租约且必须空闲。
- 结果：24/24 通过，24 个不同 Worker，清理全部确认；54 次工具调用，Gateway 操作 0；Core 均以 0 退出，Host 进程均已回收。单个 Worker 最长 8 s、峰值内存 255 MB。
- 首次证明的 authority 路径：匿名工具调用者被插件自身的管理员检查（teamup 的创建/重置）和 AstrBot 原生 `PermissionType.ADMIN` 过滤（gallery、pet）拒绝，且被拒绝的创建没有留下状态。补上了 TOOL-04 中未被触发的 authority 拒绝路径（那次是 Koishi 在参数校验阶段就拒绝了）。
- 过程中发现并修复的 Core 缺陷：MCP 工具名只允许 ASCII，非 ASCII 字符一律替换为 `_`，中文命令名因此全部塌缩为同样的下划线并被当作歧义丢弃（teamup 的 13 个命令一个都不会暴露）。现在清洗改变原值时追加 `-` 与原名 SHA-256 的前 8 位十六进制，ASCII 名称不变，TOOL-04 证据不受影响。
- 范围：工具级。市场插件的 Native（真实平台对照）与 Federated（经 Core 事件与 Gateway）行为未覆盖：前者需要真实 QQ/aiocqhttp 环境，后者在 AB-02 中以自写插件验证了事件路径语义，未对市场插件逐个复验。


## 4. 已知限制

- 以 `filter.platform_adapter_type(...)` 限定真实平台的处理器不会匹配桥接平台；依赖平台原始客户端（如 `AiocqhttpMessageEvent.bot`）的插件不可用。
- `send_by_session` 只发往 Host 已见过的会话，以新 trace 走 Core 授权/账本/Gateway。
- 桥接平台不以 `aiocqhttp` 名义注册：冒用会让插件拿到并调用不存在的原始客户端。
- AstrBot 的 `core.stop()` 在禁网环境中可能不返回；Worker 生命周期以销毁容器为准。
- 只映射文本、提及、引用、图片四类消息段。
- AstrBot 会在导入失败时用 pip 现场安装依赖，目录插件只能在已准入的 OS 隔离 Worker 中、以预先冻结的依赖运行。

## 5. 验证命令

- 宿主冒烟：`uv run --locked --project hosts/astrbot --python 3.12.14 python hosts/astrbot/tests/bridge_smoke.py`
- 端到端：`cargo test -p ligamina-cli --locked --test command_tools astrbot`（首次运行会按锁安装 AstrBot 环境）
- 两个 check 脚本均已加入宿主冒烟；工作区 `cargo test` 包含端到端测试。
