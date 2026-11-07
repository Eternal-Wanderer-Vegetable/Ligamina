# 插件工具面计划（2026-11-07）

适用：把 Koishi / NoneBot（以及之后的 AstrBot）插件统一成 MCP 工具，供任意 bot 或 Coding Agent 加载使用。决策依据见 [ADR 0002](../adr/0002-plugins-as-tools.md)。本计划接续第四阶段，不改写第四阶段的任何历史结论或证据。

## 1. 为什么改方向

项目定位是“插件联邦运行时”：插件只装一次，任何调用方用同一种方式使用。第四阶段 P4-05 为了做真实平台验收，写了一个只服务 stella 的 bridge，这偏离了定位。根因有三条：

| 现状 | 位置 | 后果 |
| --- | --- | --- |
| MCP 只列 `pure: true` 的 Host 自报 capability | `crates/ligamina-cli/src/mcp.rs`、`broker.rs` 的 `validate_descriptor` | 目录插件（聊天命令）在 MCP 中完全不可见 |
| 插件发送必须有持租约的 Gateway | `store/mod.rs` 的 `prepare_operation` | 没有 bot 端 Gateway 时插件的回复无处可去 |
| 插件阻塞等待 Gateway 回执（最长 60 秒） | `runtime.rs` 的 `wait_for_operation` | 每接一个 bot 都要写专用 connector 来领取并发送 |

## 2. 目标形态

```text
            ┌──────── Coding Agent（Claude 等）
            │         LLM 型 bot（stella 等）
            ▼
     MCP 工具面  cmd.<host>.<command>          ← 本计划新增
            │
     Rust Core ── 命令目录（按 Host generation）
            │   └─ tool sink：每次调用一个临时虚拟会话
            ▼
   原生 Host（Koishi / NoneBot / 未来 AstrBot）── 原生框架分发 ── 第三方插件
            ▲
     事件 / Gateway HTTP 合同（已有）           ← 传统 bot 直接把群消息交给插件时使用
```

- **一个插件，两种用法。** 同一个已安装插件，既能作为 MCP 工具被 Agent 调用，也能通过已有的事件/Gateway 合同直接服务群聊。两条路都不含任何特定 bot 的代码。
- **原生语义不变。** 工具调用仍构造原生 Session / OneBot 事件，进入框架自己的命令解析、中间件和权限。Core 不直接调用插件 handler。

## 3. 设计

### 3.1 命令描述（合同 v0.2）

新增 `contracts/v0.2/command-descriptor.schema.json`。Host 在 `host.initialize` 和 `runtime.capabilities` 的结果中增加 `commands` 数组（旧 Core 忽略该字段，向后兼容）：

| 字段 | 含义 |
| --- | --- |
| `command_id` | Host 内唯一，取框架的规范命令名，如 `echo` |
| `invocation` | 发给框架的命令文本前缀，如 `echo` |
| `description` | 框架或插件声明的说明（Koishi 取 i18n `commands.<name>.description`），可为空 |
| `usage` | 参数声明，如 `echo <message...>` |
| `arguments` / `options` | 框架声明的参数与选项（名称、是否必需、类型、说明），供生成更精确的工具 schema |
| `aliases` | 别名 |
| `framework` | `koishi` / `nonebot` / `astrbot` |

`pure` capability 合同保持不变。命令不是 pure 函数，不放进 capability Broker，而是单独维护一个命令目录，避免削弱 Broker 的不变量。

### 3.2 Core：命令目录与 tool sink

- **命令目录**（新模块 `crates/ligamina-core/src/commands.rs`）：沿用 `sync_host_generations` 的轮询，在 Host 新 generation 激活时读取 `commands`，按 `(host_id, epoch)` 整体替换；Host 退出即移除；变化时广播，供 MCP 发出 `tools/list_changed`。
- **虚拟会话**：每次调用生成随机 `trace_id` / `event_id`，以及按框架构造的保留身份：私聊会话，会话 ID 与 actor ID 相同且每次不同。Koishi 使用 `platform=ligamina.tool`、账号 `ligamina-tool`、字符串 ID；NoneBot 适配器只接受 `onebot.v11` 与数字 ID，因此使用账号 `0`（真实 QQ/OneBot 账号不会是 0）和大数随机 actor/message ID。sink 返回的 `platform_message_id` 一律为数字，两种 Host 都能解析。两个保留账号都不得出现在任何 grant 或 Gateway 配置中，Core 与 daemon 启动时校验。事件直接通过 Host 句柄发送 `event.dispatch`，不进入持久事件队列，因为工具调用是一次性的，没有会话顺序要求。
- **tool sink**：Core 处理 Host 的 `platform.execute` 时，先检查 `trace_id` 是否属于正在进行的工具调用。只有 `trace_id`、账号和目标会话三者都与该调用一致，才进入 sink：
  - `send_message`：记录消息段，立即返回 `succeeded` 和合成的 `platform_message_id`；
  - `get_self_info`：返回虚拟账号信息；
  - 其他操作：返回 `failed`，错误码为 `unsupported_in_tool_session`。

  不满足条件的操作照常走授权、ledger 与 Gateway。真实账号的操作不会因为工具功能改变语义；sink 只登记由 Core 自己生成、无法猜测的 trace。
- **完成判定**：`event.dispatch` 返回即视为本次调用结束（Koishi 等待 `middleware` 事件，NoneBot 等待 `handle_event`）。结果包含收集到的消息、分发状态和 `late_messages_not_collected` 说明；超时返回错误，不伪造成功。

### 3.3 MCP 工具形态

- **每个命令一个工具**，名为 `cmd.<host_id>.<command_id>`，非法字符统一替换为 `_`。前缀 `cmd.` 与 capability ID 若冲突则跳过并记录日志。选择一命令一工具，是因为 Agent 可以直接从工具列表发现插件能力；描述里带上框架说明和用法。
- **输入**：`{ "arguments": string }`，即命令名之后的原样文本，由框架自己解析。这是 Koishi、NoneBot、AstrBot 都能遵守的最小公约数。结构化参数（`arguments` / `options` → JSON Schema）在 TOOL-03 中引入，并保留原样文本入口。
- **输出**：结构化 `{ messages: [{ segments, text }], dispatch: "completed" }`，同时把所有文本拼接后放进 MCP text content，方便 LLM 直接阅读。
- **默认关闭**：守护进程需要显式开启（配置 `command_tools.enabled`，可选 `allow` 精确列表）。未开启时 MCP 行为与之前完全相同。

### 3.4 安全与边界

- 工具调用不持有任何平台账号，也不会产生真实平台发送；插件试图访问真实平台数据会显式失败。
- 每次调用使用独立的虚拟会话与 actor，调用之间不共享插件的会话状态。插件的持久化存储（如 Koishi SQLite 中的用户数据）仍属于该 Host profile。
- 插件权限：虚拟 actor 按框架默认权限运行（Koishi 新用户 authority 1），不提权；需要更高权限的命令会按原生语义拒绝。
- 第三方插件仍按现有规则只在已准入的 OS 隔离 Worker 里做目录级执行；本地守护进程只加载用户自己配置的插件。

## 4. 任务拆分

| 任务 / feature | 依赖 | 交付 | 完成标准 |
| --- | --- | --- | --- |
| TOOL-01 / feat-032 | P4-01 工程交付 | v0.2 命令合同；Koishi 命令发现；Core 命令目录与 tool sink；MCP 命令工具；Rust 单测与真实 Koishi Host 端到端测试 | 默认关闭时既有测试全部不变地通过；开启后 MCP 列出并调用 Koishi 命令，收到插件原生回复；越权操作、未知命令、超时、sink 隔离均有负例 |
| TOOL-02 / feat-033 | TOOL-01 | NoneBot 命令发现（`on_command` / `on_shell_command` 的命令规则与别名，按 `command_start` / `command_sep` 生成调用文本）与 OneBot 合法 ID 的虚拟会话 | NoneBot 命令经同一 MCP 工具面可调用，含并发与超时负例。2026-11-07 已完成；原写在此处的冻结样本 `nonebot_plugin_roll@0.2.3` 工具级验收属于第三方目录插件执行，按 AGENTS.md 必须在 OS 隔离 Worker 中进行，已移入 TOOL-04 |
| TOOL-03 / feat-034 | TOOL-01 | 结构化参数 schema（Koishi 声明 → JSON Schema），服务端按框架语法拼接命令文本 | 生成的 schema 与框架声明一致，原样文本入口保留 |
| TOOL-04 / feat-035 | TOOL-02 | 工具级行为验收：冻结的 `@koishijs/plugin-echo@2.2.5`、`nonebot_plugin_roll@0.2.3` 经工具面在 Linux Worker 中三次 fresh 运行 | 2026-11-07 完成：18/18 通过、清理全部确认，`tool_surface_linux=passed_scoped`（见 3.6，含 authority 路径未覆盖的说明） |
| TOOL-05 / feat-036 | TOOL-01 | 平台中立的 bot 接入：接入说明、参考客户端修复与 OneBot v11 Gateway | 2026-11-07 完成：单元与本地端到端测试通过（见 3.7）；真实平台验收仍属 P4-05 |

AstrBot Host 仍留在第五阶段，但命令描述合同已按三框架设计，加入时只需新增 Host。

### 3.4.1 结构化参数（TOOL-03，2026-11-07 完成）

- 实现在 `crates/ligamina-core/src/command_args.rs`。Koishi 命令有参数或选项声明时，工具 schema 在原样 `arguments` 字段之外，为每个位置参数生成同名字段（`number`/`integer`/`posint`/`natural`/`boolean` 映射为对应 JSON 类型，其余为字符串），选项放在 `options` 对象中。
- 原样文本与结构化字段二选一，同时提供即拒绝。参数名与保留字段 `arguments` / `options` 冲突时只提供原样文本。NoneBot 没有类型化声明，只提供原样文本。
- 拼接规则：选项按声明顺序放在最前（Koishi 末尾的 `text` 参数会吞掉其后的整行，包括形似选项的内容）；布尔选项为 true 时输出 `--name`，false 省略；位置参数按声明顺序，缺必填参数或跳过前面的可选参数即报错；含空白、为空或以 `-` 开头的值加双引号。Koishi 的 argv 解析器不支持引号内转义，所以需要加引号且含 `"` 的值会被拒绝，提示改用原样文本。
- 验证：4 项单元测试覆盖 schema、拼接、拒绝场景和 NoneBot；Koishi 端到端测试用结构化输入调用 `ligamina.echo`，Koishi 原生解析得到预期回复。

### 3.6 工具级行为验收（TOOL-04，2026-11-07 完成）

- 用例：[tool-surface-cases.json](../../contracts/lab/automation/tools/v1/tool-surface-cases.json)，冻结 `@koishijs/plugin-echo@2.2.5` 与 `nonebot_plugin_roll@0.2.3` 各 3 个用例（正例、负例、隔离）。每个期望回复都标注其来源的父 Phase 4 Oracle 文本（`case_id#operation`），编排器在派发前逐条比对冻结父套件，文本漂移即拒绝。Oracle 只留在编排器侧。
- 执行：`lab/automation/tool_surface_campaign.py` 每个用例 3 次，每次一个全新的已准入、禁网 Docker Worker（`phase4_docker_side.py --side tool`）。Worker 内复用 Federated 测试框架的工具模式：同一冻结 artifact、依赖锁、插件配置和随机种子，经 MCP 调用插件原生命令，虚拟 Gateway 照常运行但必须保持空闲。
- 运行镜像：[tool-surface-runtime.Dockerfile](../../deploy/lab/linux/tool-surface-runtime.Dockerfile) 在已准入的 P4-01 镜像之上只替换 Core 二进制与 Koishi Host 构建产物，只复制线协议合同；镜像 `ligamina-tool04-runtime@sha256:9c1280e40a0bfa7fb2a7135a58316624a9999ad5333ad66938eb363f7213b6b5`，运行时探针通过（Python 3.12.14、Node 24.20.0、禁网、无 Oracle 合同、Koishi Host 锁哈希不变）。
- 准入：[benign probes](../../outputs/acceptance/tool04-linux-worker-probes-20261107.json) 10/10 符合预期且全部清理确认；[admission](../../outputs/acceptance/tool04-linux-admission-20261107.json) 为 admitted（SHA-256 `c6642b4eabebebb28cc7e08e1b85d4ec095b152cdfbadd3afe6aede2fa97732c`）。
- 结果：[campaign report](../../outputs/acceptance/tool04-tool-surface-linux-20261107-v1.json)，SHA-256 `a1453dffec0095547eb00737efd583ee1f74fa96a5778a415d0625805d0f41fd`。6 个位置、18 次尝试、18 个不同 Worker 全部通过，清理全部确认，Gateway 操作数均为 0，运行后无残留容器。gate `tool_surface_linux=passed_scoped`；该结果单列 tool-surface scope，不改变 Native/Federated 行为、field 或 release 结论。
- 如实记录的发现：echo 隔离用例第三个调用 `-u onebot.v11:77 leaked-message` 原本意在验证无特权会话无法使用 authority 3 的转发选项。实际回复是“选项 user 输入无效，请指定正确的用户。”，即 Koishi 在参数校验阶段就拒绝了，authority 检查并未被触发。Oracle 断言的“不泄漏、不越界”成立，但 **authority 拒绝路径尚未被证明**；后续可用当前工具会话平台内的合法用户 ID 补一个专门用例。

### 3.7 平台中立的 bot 接入（TOOL-05，2026-11-07 完成）

- 接入说明：[Bot 与 Agent 接入](../guides/bot-integration.md)。两条路径对所有 bot 相同：MCP 命令工具，或事件 + Gateway。
- 修复：参考客户端的 Gateway 调用此前无法对接真实 Core——claim 发送空 `{}`，而 Core 要求请求体是完整租约凭据；result 也缺少 `{credential, result}` 外层。其单元测试使用注入的 fetch，所以一直未暴露。现已修正，CLI 改为从 `LIGAMINA_GATEWAY_LEASE` 读取完整租约 JSON。
- 参考实现：[onebot-gateway.ts](../../clients/reference/src/onebot-gateway.ts)。`OneBotGateway` 领取 Core 操作并调用任意 OneBot v11 HTTP API，`toEventEnvelope` 转换入站消息事件。网络错误、超时、异步受理、缺少 `message_id` 一律回写 `unknown` 且不重试。
- 验证：单元测试 7 项（成功、四类未知、明确失败、空队列、事件与消息段映射）；`npm run test:live` 启动真实守护进程与 Koishi Host，用假 OneBot HTTP 服务代替平台，OneBot 群消息进入插件、回复以 `send_group_msg` 发回、事件状态 completed。这是本地接线证据，不是真实平台投递证据。

### 3.5 NoneBot 命令识别规则（TOOL-02）

- 只识别带 `CommandRule` / `ShellCommandRule` 的 matcher；关键词、正则和普通消息 hook 不是命令，不暴露。
- `on_command` 把主命令和别名合并进一个无序集合，原始主名无法恢复。为保证每次启动工具名稳定，取段数最多的名字为 `command_id`，同段数按文本排序，其余作为别名。
- 调用文本 = 配置中排序后的第一个 `command_start`（若含空串则不加前缀）+ 以第一个 `command_sep` 连接的命令段。描述取插件 `PluginMetadata` 的 description 与 usage。
- `on_alconna` 等第三方命令框架尚未识别；如需支持，在 TOOL-03 结构化参数时一并处理。

## 5. 对第四阶段的影响

- **P4-05 / feat-030**：从 in-progress 改为 blocked，原因记为“接入方式被 ADR 0002 取代，等待 TOOL-05 提供平台中立接入”。真实平台验收（P4-FIELD）作为 gate 仍然有效，但今后只通过中立客户端执行。三次 direct SnowLuma `result=120` 操作继续保持 unknown，不重放。
- **P4-03 / P4-04 / P4-06**：状态与阻塞原因不变。
- **active feature**：改为 TOOL-01 / feat-032。

## 6. 验证命令

- Rust：`cargo test -p ligamina-core -p ligamina-cli --locked`
- Koishi Host：`cd hosts/koishi && npm test`
- 端到端：TOOL-01 新增的 Rust 集成测试会启动真实 Koishi Host（需要先 `npm run build`），通过 MCP 列出并调用命令工具。
- 文档：`pwsh -NoProfile -File init.ps1 -Mode docs`
