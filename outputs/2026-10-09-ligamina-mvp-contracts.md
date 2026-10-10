# Ligamina MVP Interface 草案与技术证据

日期：2026-10-09。状态：为 [MVP 工程任务](2026-10-09-ligamina-engineering-plan.md)提供确定输入；T02 将其转为 schema 与一致性检查。本文件不是已运行的协议实现。

## 1. 版本与传输

- Ligamina 内部协议：`ligamina/0.1`，schema：JSON Schema 2020-12。
- Core–Host：UTF-8 NDJSON，JSON-RPC 2.0 风格双向请求与响应。该协议是 Ligamina 管理/执行协议，不声称这些管理方法属于 MCP。
- 每帧最多 1 MiB；每 Host 在途请求最多 64；每会话等待事件最多 256；超过预算返回 `overloaded`，不无限积压。没有插件时不创建 Host。
- stdout 仅协议，stderr 仅日志；插件普通 print/console 输出应转到 stderr。Host 启动前保存专用协议输出句柄，协议写入经串行 writer，防止重定向破坏协议。
- Core 读取 Host 握手后，依据受管进程与配置绑定 Host ID/代次；不能信任插件提交的身份。两个方向使用不同请求 ID前缀，ID 不复用；取消是显式通知，超时释放在途状态，迟到结果丢弃并记录。
- 截止时间在 wire 使用 UTC 毫秒，接收后转为本地单调剩余时长；取消和断连不意味着已经发生的外部操作已撤销。
- 每条能力、事件和操作关联 `trace_id`。Host 重新创建产生新 `host_epoch`；Core 每次启动产生新 `core_epoch`，旧结果一律不能恢复新代次状态。

MCP 首版固定 `2025-11-25` + Streamable HTTP，协议处理交给 SDK。明确配置版本协商，验证旧生命周期及能力变更通知；新版 `2026-07-28` 的逐请求元数据与发现流程延后独立验收。管理 HTTP 与 MCP共用一个 daemon，MCP 能力调用转换成 Broker 请求。

## 2. 公共对象

| 对象 | 必需内容 | 不变量 |
|---|---|---|
| PluginSpec | 原生生态、包/源码位置、精确版本、哈希、依赖锁、配置、启用状态、桥接配置档 | 桥接描述是侧车，业务文件不修改；加载在 Host 中发生 |
| HostIdentity | host_id、host_epoch、core_epoch、框架/解释器版本、配置档、受管连接 | 唯一所有者是 Core；Host 自报信息只用于核对 |
| EventEnvelope | event_id、trace_id、platform、account_id、conversation(kind,id)、actor_id、timestamp、message、target_host、extensions | ID 为字符串，类型与账号参与作用域；target_host 由已授权客户路由策略确定 |
| CapabilityDescriptor | 稳定能力 ID、schema、语义版本、Host/插件归属声明、运行代次、纯函数/副作用类型 | 仅已激活 Host 的声明可用；工具 schema 不含权限凭据 |
| InvocationContext | grant 引用、调用者、账号/会话约束、deadline、trace、调用链 | Core 从可信请求生成；Host 不可通过参数扩大授权 |
| PlatformOperation | operation_id、event/call 来源、Host 代次、operation、target、message/params、grant 引用 | 先持久化 fingerprint 再转交网关；operation_id 作用域含客户、账号和操作类别 |
| OperationResult | status、platform_message_id、可选 `data`、error、observed_at、evidence_ref | status 为 succeeded/failed/unknown；`data` 承载只读平台操作的结构化回包（如 `get_message`）；accepted 是投递状态，不是发送成功 |
| TestResult | 插件/环境指纹、场景、native/federated 各层状态、预期、差异、证据 | 同错不通过；安装、加载、行为和稳定性独立计数 |

事件例子（值为合成数据）：

```json
{
  "schema_version": "ligamina/0.1",
  "event_id": "evt-001",
  "trace_id": "trace-001",
  "platform": "onebot.v11",
  "account_id": "10001",
  "conversation": {"kind": "group", "id": "20001"},
  "actor_id": "30001",
  "timestamp": "2026-10-09T08:00:00Z",
  "target_host": "nonebot-default",
  "message": [
    {"type": "mention", "user_id": "10001"},
    {"type": "text", "text": "/nb.echo hello"},
    {"type": "reply", "message_id": "40001"}
  ],
  "extensions": {"onebot.v11": {"message_id": "50001"}}
}
```

原生转换必须根据配置档生成 Event/Session，而不是把 extensions 当成任意可信字段合并。身份与路由字段发生冲突时拒绝事件；OneBot v11 模型所需 sender、sub_type、raw_message 等信息由明确映射补齐，并保留其来源。消息查询的虚拟历史要包含 reply 所需字段。

图片段采用 `image_ref`，包含 URL/资源引用及可选 MIME；不得因不支持下载而自动把它删除。需要实际图片内容的测试场景标记未覆盖/不支持。

## 3. Core–Host 方法

| 方向 | 方法 | 请求/结果 |
|---|---|---|
| Host → Core | `host.hello` | 框架/运行时版本、协议、配置档；Core 核对后返回绑定身份与限制 |
| Core → Host | `runtime.configure` | 期望插件集合、原生配置、数据目录和桥接允许范围；Host 原生加载后返回逐插件状态与能力描述 |
| Core → Host | `event.dispatch` | 已校验事件及 Core 生成的上下文；返回受理/处理状态及可观察错误 |
| Core → Host | `capability.invoke` | 能力 ID、schema 校验后的参数与上下文；返回结构化结果或确定错误 |
| Host → Core | `service.invoke` | 显式目标能力、参数、父调用引用；Core 继承并收紧授权，再通过 Broker 路由 |
| Host → Core | `platform.execute` | 明确操作、目标、ID、来源上下文；Core 检查 grant、代次、账本后转交网关 |
| Core → Host | `runtime.health` | 请求 Host 状态、受管任务和注册数；不能仅以进程存在判健康 |
| Core → Host | `runtime.stop` | drain 截止时间；停止新任务、运行 shutdown；超时由 Supervisor 清理进程树 |
| 双向通知 | `request.cancel`、`runtime.state_changed` | 指定请求取消；宿主活动/失败及能力变化信息 |

保持读协议循环与执行插件循环解耦，收到请求后启动受限任务，不在 reader 中等待插件处理完成。否则插件处理等待平台请求回复、Core 又等待 Host 完成，会造成跨进程死锁。Node/Python 两边都用请求关联表与串行输出。

第一版 `runtime.configure` 在新 Host 启动时调用；改变插件集合先 stop 再创建新 Host。能力注册为启动结果的原子快照，全部有效 schema 与依赖通过才 publish；局部失败只发布明确 active 的插件能力。

Host 状态：`stopped → starting → ready → draining → stopped`；异常为 `failed`，连续 60 秒内 3 次启动失败后进入 `blocked`，等待管理操作重新启用。插件单独报告 `installed/starting/active/stopped/failed/blocked`；Host ready 不代表所有插件成功。

## 4. 客户端 HTTP Interface

| 请求 | 行为 |
|---|---|
| `GET /v1/status` | Core/Host/插件状态、实际版本与错误 |
| `PUT /v1/hosts/{id}/desired` | 管理令牌授权后更新期望插件集合；返回变更 ID，状态单独查询 |
| `POST /v1/events` | 授权、去重、入队；202 返回事件 ID；只有入账/入队成功才 accepted |
| `GET /v1/events/{id}` | accepted/processing/completed/failed/unknown；发送结果通过操作记录查询 |
| `GET /v1/capabilities`、`POST /v1/invocations` | 参考客户端直接发现/调用；与 MCP共用 Broker |
| `POST /v1/gateways/{id}/lease` | 获得账号对应单一活动 lease；客户/账号和 token 在 Core 注册配置中绑定 |
| `POST /v1/gateways/{id}/actions/claim` | 持当前 lease 领取 pending 操作；在返回前持久化 dispatched 状态 |
| `POST /v1/gateways/{id}/actions/{operation_id}/result` | 回填实际平台结果；Core 核对 lease、代次和 fingerprint |
| `GET /v1/operations/{operation_id}` | 同权限作用域查询状态；不将 unknown 自动置为 pending |
| `POST /mcp`（及 SDK需要的其他方法） | MCP 发现与调用；独立客户端固定版本互操作 |

HTTP 仅绑定 loopback；Bearer token 从本地受保护配置取得，区分管理令牌与客户权限；MCP 访问只允许其既有 grant，并执行 Host/Origin 校验。配置不得把 token 放入工具参数或日志。分布式认证、OAuth、多租户延后。

平台操作走事务序列：授权 → 写 pending → 网关 claim 写 dispatched → 外部执行 → 回填 succeeded/failed。dispatched 丢回执、lease 失效或 Core 重启进入 unknown，不重新分配。网关本身也持久记录 operation_id 和结果；租约切换后旧网关不得继续执行/回填。

能够在发送前确定失败的请求可返回 failed；无法排除实际发送的请求返回 unknown。duplicate 使用相同 fingerprint 查询原结果；不同 fingerprint 是 `operation_conflict`。该设计避免自动重复，不承诺跨真实平台 exactly-once。

事件 dedup 的 key 包含客户、平台、账号、会话类型/ID及 event_id；相同 key 不同载荷拒绝。单会话串行，不同会话有界并发；同 Host 原生处理产生的回复和后台动作分别受其 grant 约束。

`completed` 仅表示受管事件处理返回，不宣称任意 detached 后台任务都结束。测试夹具用明确完成屏障、期望操作数和受管任务状态；无法提供可靠完成信号的第三方场景采用固定观察窗口并标记范围，不据超时推断永久不会回复。

## 5. 统一错误与报告

| 错误 | 必须体现的含义 |
|---|---|
| invalid_request / protocol_mismatch | 参数不合法/版本不兼容，未执行 |
| permission_denied / stale_epoch | 无授权或旧实例，未通过桥接执行 |
| unsupported_api / missing_dependency | 实现或原生依赖不满足，不能伪造成功 |
| host_unavailable / overloaded | 对应 Host不可用/超过资源上限 |
| deadline_exceeded / cancelled | 等待终止；外部副作用是否发生另见 operation status |
| operation_conflict / gateway_unavailable | 操作 ID载荷不同/没有有效网关 |
| native_failed / adapter_regression / environment_unavailable | 报告归因，不与 RPC传输错误混用 |

每个错误包含 code、message、trace_id、可重试提示及外部副作用状态；只读重试与副作用重试分开判断。MCP 协议错误、工具执行错误按官方模型映射，不把所有失败都变成 JSON-RPC 内部错误。[MCP Tools 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

报告层级为 L0–L5，各有 pass/fail/skipped/unknown；基准的预期正确性和联邦等价性分别记录。必选断言未执行时不关闭清单项。每个 report 提供证据路径、hash、实际命令和退出码。

## 6. 本次核实的技术证据

核实方式：读取官方文档、PyPI/npm/crates.io 元数据与精确版本发布包源文件；没有安装这些插件，没有运行宿主或执行编译测试。包元数据满足约束不是联合运行通过。

| 来源 | 本次确认 | 工程影响 |
|---|---|---|
| [NoneBot Adapter 文档](https://nonebot.dev/docs/advanced/adapter) | Adapter/Bot/Event/Message 参与原生事件和平台调用转换 | T08 必须在原生 Adapter 路径接入 |
| [OneBot Adapter 2.4.6 发布元数据](https://pypi.org/pypi/nonebot-adapter-onebot/2.4.6/json)及[Bot 版本源码](https://github.com/nonebot/adapter-onebot/blob/v2.4.6/nonebot/adapters/onebot/v11/bot.py) | `Bot.handle_event` 在消息进入 Matcher 前做回复、提及和昵称预处理；回复查询调用 get_msg | 必须提供消息查询并保留预处理，不能直接伪造 Matcher 输入 |
| [NoneBot 2.5.0 发布元数据](https://pypi.org/pypi/nonebot2/2.5.0/json) | 发布包包含无网络 driver；其运行中有 startup/shutdown 流程 | T01验证无网络原生生命周期；未把私有入口当成稳定公开接口 |
| [Koishi Bot](https://koishi.chat/en-US/api/core/bot)、[Context](https://koishi.chat/en-US/api/core/context)、[Mock 测试](https://koishi.chat/zh-CN/cookbook/practice/testing) | 原生 Bot/Session 派发与 Context命令/依赖可复用；官方 Mock适合基准 | 生产 Adapter与基准 Mock分开，才能验证真实桥接 |
| [rmcp 3.5.1 元数据](https://crates.io/api/v1/crates/rmcp/3.5.1)、[官方 Rust SDK](https://github.com/modelcontextprotocol/rust-sdk) | 包模型有 2025-11-25 与 2026-07-28；MSRV声明1.88，许可Apache-2.0 | 现有Rust1.97.1满足声明下限，仍需编译/握手实测 |
| [TS SDK 1.32.1 元数据](https://registry.npmjs.org/@modelcontextprotocol%2fsdk/1.32.1) | 发布包 `dist/esm/types.js` 声明最高协议2025-11-25 | 首版互操作基线明确固定，不能继承新版文档或Rust默认值 |
| [MCP 2025-11-25 Transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)与[2026-07-28 Transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports) | 新旧生命周期与请求元数据不同 | 按首版规范实现，升级单独测试 |

精确发布包指纹（SHA-256）：

```text
nonebot2-2.5.0-py3-none-any.whl
12380d396a3d5126eeeae0a1aa53af45e9d99b63896e3e2dd6340ee436869e36
nonebot_adapter_onebot-2.4.6-py3-none-any.whl
b1ec7023fd83d731f63b513217327a57d12893a261944934b9195f79173791ad
@modelcontextprotocol/sdk-1.32.1.tgz
63a3962282ff29d2ce532945c2edefd9b7c7195b8ec20c027e120e4498b0cb19
@koishijs/plugin-mock-2.6.6.tgz
982d0cc0b51a56328b66e8123f021ccf7112929311349467522d28a14a4acb01
rmcp-3.5.1 registry checksum
ab5023804cbe0304cbe06197ac16e645a0a0d0ffa2953bf735c15c2fa0213350
```

前两 wheel 已对照 PyPI提供的 SHA-256；两个 npm包对照 registry shasum并额外计算 SHA-256。rmcp checksum为 registry声明，本次读取其发布包源文件，未额外对照下载包摘要；T01锁定时验证。

本机只读核实：Rust/Cargo1.97.1、Node24.20.0、npm12.0.2、Docker客户端/服务端29.7.2；uv列出已安装CPython3.12.14。默认系统Python是3.14.7，Host工程显式选择3.12.14，不改系统默认值。
