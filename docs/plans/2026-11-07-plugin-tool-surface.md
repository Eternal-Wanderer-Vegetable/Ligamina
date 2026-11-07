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
- **虚拟会话**：每次调用生成随机 `trace_id` / `event_id`，以及保留身份：私聊会话，会话 ID 与 actor ID 均为本次调用生成的 ID。Koishi 使用 `platform=ligamina.tool`、账号 `ligamina-tool`。NoneBot 适配器只接受 `onebot.v11` 和数字 ID，由 TOOL-02 按框架选择身份格式。事件直接通过 Host 句柄发送 `event.dispatch`，不进入持久事件队列，因为工具调用是一次性的，没有会话顺序要求。
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
| TOOL-02 / feat-033 | TOOL-01 | NoneBot 命令发现（`on_command` 的 `CommandRule`、别名、`on_alconna` 若存在），虚拟会话用 OneBot 合法数字 ID | NoneBot 插件经同一 MCP 工具面可调用；`nonebot_plugin_roll@0.2.3` 等已冻结样本工具级正/负例通过 |
| TOOL-03 / feat-034 | TOOL-01 | 结构化参数 schema（Koishi 声明 → JSON Schema），服务端按框架语法拼接命令文本 | 生成的 schema 与框架声明一致，原样文本入口保留 |
| TOOL-04 / feat-035 | TOOL-02 | 工具级行为验收：复用第四阶段冻结的 echo / roll 等样本与独立 Oracle，在 Linux Worker 中三次 fresh 运行 | 结果单独记为 `tool-surface` scope，不与 Native/Federated 或 field 结论混用 |
| TOOL-05 / feat-036 | TOOL-01 | 平台中立的 bot 接入参考：reference client 的 Gateway 示例与接入文档（以 OneBot v11 为例，不绑定具体 bot） | 任意支持 OneBot v11 的 bot 可按文档接入；替代原 P4-05 的 stella 专用路径 |

AstrBot Host 仍留在第五阶段，但命令描述合同已按三框架设计，加入时只需新增 Host。

## 5. 对第四阶段的影响

- **P4-05 / feat-030**：从 in-progress 改为 blocked，原因记为“接入方式被 ADR 0002 取代，等待 TOOL-05 提供平台中立接入”。真实平台验收（P4-FIELD）作为 gate 仍然有效，但今后只通过中立客户端执行。三次 direct SnowLuma `result=120` 操作继续保持 unknown，不重放。
- **P4-03 / P4-04 / P4-06**：状态与阻塞原因不变。
- **active feature**：改为 TOOL-01 / feat-032。

## 6. 验证命令

- Rust：`cargo test -p ligamina-core -p ligamina-cli --locked`
- Koishi Host：`cd hosts/koishi && npm test`
- 端到端：TOOL-01 新增的 Rust 集成测试会启动真实 Koishi Host（需要先 `npm run build`），通过 MCP 列出并调用命令工具。
- 文档：`pwsh -NoProfile -File init.ps1 -Mode docs`
