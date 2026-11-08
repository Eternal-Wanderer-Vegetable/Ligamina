# Bot 与 Agent 接入

适用：让任意 bot、LLM 型 bot 或 Coding Agent 使用 Ligamina 加载的插件。决策依据见 [ADR 0002](../adr/0002-plugins-as-tools.md)，任务背景见 [插件工具面计划](../plans/2026-11-07-plugin-tool-surface.md)。Ligamina 不为任何特定 bot 写专用代码，下面两条路径对所有 bot 相同。

## 选哪条路径

| 你的场景 | 用法 | 平台账号 |
| --- | --- | --- |
| Coding Agent（Claude Code 等）或 LLM 型 bot 想“调用插件” | MCP 命令工具 `cmd.<host>.<command>` | 不需要 |
| 传统 bot 想让群消息直接进入插件，由插件回复到群里 | 事件 + Gateway 合同；OneBot v11 可直接用参考 Gateway | 需要，由 bot 自己持有连接 |

两条路径可以同时开启。同一个已安装插件，在 MCP 里是工具，在群聊里是原生命令。

## 路径一：MCP 命令工具

1. 守护进程配置开启：`"command_tools": { "enabled": true, "allow": ["cmd.<host>.*"] }`（默认关闭；`allow` 省略表示全部命令）。
2. 客户端连接 `http://127.0.0.1:<port>/mcp`，使用 `mcp_client_id` 对应客户端的 Bearer token。Claude Code 示例：
   `claude mcp add --transport http plugins http://127.0.0.1:<port>/mcp --header "Authorization: Bearer <client token>"`
3. 每个工具的输入是原样文本 `{"arguments": "..."}`；Koishi 命令还可按声明传结构化参数（见计划 3.4.1）。结果包含插件回复文本和结构化的消息段。

每次调用都在独立的临时私聊会话中运行，使用保留账号和普通权限；不会向任何真实平台发送消息。定时器等在命令处理结束后才发出的消息不计入结果。

客户端须知：

- MCP 会话空闲 5 分钟后由服务端关闭（rmcp 默认 keep-alive）。之后带旧会话 ID 的请求得到 HTTP 404，客户端应按 MCP 规范重新 `initialize` 并重试；该请求未被处理，重试不会重复执行命令。Python MCP SDK 把 404 报告为 `McpError(code=32600, "Session terminated")`。
- 同时给出原样文本 `arguments` 与结构化字段时，非空的 `arguments` 优先、结构化字段被忽略（空字符串视为未给出）。参数不合工具 `inputSchema`（未知字段、`arguments` 非字符串）时，结果为 `isError: true`，文本以 `invalid arguments:` 开头，会话不受影响；只有未知或未开放的工具名才返回 JSON-RPC 错误。

## 路径二：事件 + Gateway（以 OneBot v11 为例）

bot 自己持有 QQ 等平台连接（NapCat、Lagrange、LLOneBot 等 OneBot v11 实现）。Ligamina 只负责把消息交给插件、把插件要发的消息交还给 bot：

```text
OneBot 实现 ──消息事件──▶ bot/接入进程 ──POST /v1/events──▶ Core ──▶ 原生 Host ──▶ 插件
OneBot 实现 ◀──send_*_msg── OneBotGateway ◀──claim/result── Core ◀── 插件回复
```

参考实现在 [clients/reference/src/onebot-gateway.ts](../../clients/reference/src/onebot-gateway.ts)：

- `toEventEnvelope(onebotEvent, targetHost)`：把 OneBot 消息事件转成 Core 事件，再用 `LigaminaClient.submitEvent` 提交。
- `new OneBotGateway({ core, gatewayId, registrationToken, onebot: { baseUrl, accessToken } }).run(signal)`：领取 Core 的平台操作，调用 OneBot HTTP API（`send_group_msg` / `send_private_msg` / `get_msg` / `get_login_info`），并回写回执。
- 投递规则：网络错误、超时、OneBot 异步受理（`retcode 1`）或缺少 `message_id` 都回写为 `unknown`，**不重试**，与 Core 账本的“未知不重放”一致；OneBot 明确失败才回写 `failed`。
- 故障收口（P6-02）：每个领取的操作分三段处理，任何一段出错都只影响该操作，`run()` 不会因此停止。
  - 准备段（校验、消息段转换）出错：尚未发送，回写 `failed`，错误以 `not sent:` 开头。
  - 平台段：传输不确定为 `unknown`（见上一条）；OneBot 已应答但应答无法解析时，发送类回写 `unknown`，只读类（`get_msg`、`get_login_info`）回写 `failed`。
  - 回写段：Core 暂时不可达或返回 5xx 时保留结果，下一次轮询用**同一租约**重写同一结果（Core 对相同结果幂等），不重新领取、不重新发送；租约已过期或 Core 以 4xx 拒绝（租约被替换、Core 重启）时丢弃该结果并调用 `onUnreported`，账本中该操作保持 `unknown`，由运维按记录核对，不重放。
  - `run()` 的 Core/OneBot 错误交给 `onError` 并指数退避（上限 5 秒）；Core 拒绝租约后下一轮自动重新取租约。

守护进程配置需要：一个客户端（提交事件）、一个绑定该客户端与账号的 Gateway（`registration_token` 至少 32 字节），以及 Host 对该账号的 `operation_grants`。保留账号 `ligamina-tool` 与 `0` 不能用于这些配置。

参考客户端 CLI 的 Gateway 命令从环境变量读取凭据：`LIGAMINA_GATEWAY_REGISTRATION_TOKEN` 用于 `gateway-lease`；`gateway-claim` / `gateway-result` 读取 `LIGAMINA_GATEWAY_LEASE`，即 `gateway-lease` 输出的完整租约 JSON。

## 验证

- 单元测试：`cd clients/reference && npm test`（含 Gateway 的成功、未知、失败、空队列与消息段映射；`tests/onebot-gateway-faults.test.mjs` 覆盖三段故障收口、回写重试、租约过期、Core/OneBot 中断与恢复）。
- 端到端：先 `cargo build -p ligamina-cli` 并在 `hosts/koishi` 执行 `npm ci && npm run build`，再 `cd clients/reference && npm run test:live`。该测试启动真实守护进程与 Koishi Host，用本地假 OneBot HTTP 服务代替平台连接，验证 OneBot 群消息进入插件、插件回复以 `send_group_msg` 发回、事件状态为 completed。另两项为故障注入：发送进行中杀掉 Core 并在同一账本上重启，以及发送进行中由新的 Gateway 实例接管；两者都要求被中断的发送在账本中保持 `unknown`、只发送一次，且新消息能继续投递。
- 真实平台验收（P4-FIELD）仍需在用户授权的账号与群里另行执行；本地端到端不是投递证据。
