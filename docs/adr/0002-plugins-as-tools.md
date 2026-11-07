# ADR 0002：插件统一为工具面，移除客户专用 bridge

- 日期：2026-11-07
- 状态：已采纳；实施计划见 [插件工具面计划](../plans/2026-11-07-plugin-tool-surface.md)
- 取代：第四阶段计划中“Stella 作为首个集成客户”的 P4-05 接入方式（原记录保留为历史证据）

## 背景

Ligamina 的目标是把 Koishi、NoneBot（以及长期计划中的 AstrBot）插件统一成类似 MCP 的形态，由任意 bot 或 Claude 这类 Coding Agent 统一加载使用。

现状偏离了这一目标：

- MCP 端点（`crates/ligamina-cli/src/mcp.rs`）只列出 Host 自报的 `pure` capability。目录插件本质是聊天命令，只能走“事件 → 插件 → `send_message` → Gateway 领取”的 bot 路径。
- 插件的发送必须有一个持有有效租约的 Gateway（`store/mod.rs` 的 `prepare_operation`），并且插件会阻塞等待 Gateway 回执。所以每接一个 bot，都要专门写一个 connector。
- P4-05 因此在 2026-10-29 写出了 stella 专用的 `clients/stella/ligamina_bridge`。它把项目绑定到某一个客户 bot，违背“统一加载”的定位。

## 决策

1. **插件命令是一等工具。** Host 上报原生框架已注册的命令（`command descriptor`，合同 v0.2），Core 维护按 Host generation 替换的命令目录，MCP 把每个命令暴露为一个工具。
2. **工具调用在 Core 内闭环。** 每次调用 Core 都新建一个临时虚拟私聊会话，用原生事件路径分发给 Host。插件对该会话的发送由 Core 内置的 tool sink 直接完成并收集，作为工具结果返回。不经过 Gateway，不写入真实平台操作账本，也不需要任何平台账号。
3. **bot 接入只走中立协议。** LLM 型 bot 与 Coding Agent 使用同一个 MCP 工具面。需要“群消息直接进入插件”的传统 bot 使用已有的事件/Gateway HTTP 合同，参考实现是平台中立的客户端，不为某个 bot 写专用代码。
4. **移除 stella 专用 bridge。** 未提交的 `clients/stella` 于 2026-11-07 移出仓库，归档在被忽略的 `.work/retired/stella-bridge-20261107/`，以保持既有证据报告可复核。仓库外暂存的扩展副本与 stella 私有配置不由本仓库处理，也未被启用（pending revision 从未 apply）。

## 后果

- 工具调用默认不暴露：守护进程必须显式开启命令工具，并可用 allowlist 精确限定。
- 只收集分发窗口内发往本次虚拟会话的 `send_message`。插件在中间件结束后才发送的消息（定时器、延迟回调）不计入结果，结果会如实标注。`get_message` 等需要真实平台数据的操作在虚拟会话中失败，并返回 `unsupported_in_tool_session`。
- 虚拟会话的结果不是平台投递证据，不计入 P4-FIELD；它进入新的工具行为验收（见计划）。
- 既有事件/Gateway/ledger 语义、unknown 不重放约束和全部历史证据保持不变。
