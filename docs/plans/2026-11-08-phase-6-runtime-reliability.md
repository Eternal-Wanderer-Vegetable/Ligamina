# 第六阶段：运行可靠性与可信兼容性（2026-11-08）

适用：第五阶段 AB-01–05 完成后，`feature_list.json` 的 `active_feature` 与 `next_feature` 均为空，已登记路线走到末端。本阶段不新增插件框架，目标是证明：来自任一已支持框架的插件，经统一接入后能在真实 bot 环境中安全、稳定、可追溯地执行，并在异常时保持正确的操作状态。

依据：2026-11-08 的一份外部项目审阅（用户提供，与用户自己的规划基本一致）。下表为本仓库逐条核对的结果；未核对的条目不作为事实使用。

## 1. 审阅结论核对

| 审阅结论 | 核对结果 | 依据 |
| --- | --- | --- |
| 41 项任务，37 done、4 blocked（P4-03/04/05/06），active/next 为空 | 属实 | `feature_list.json` |
| Gateway `pollOnce()` 未收口 `#perform()` 的异常：不支持的消息段抛 `TypeError`，`run()` 随之终止，已领取的操作没有回写 | 属实（源码可识别的故障路径，非已观察的线上故障）；P6-02 已修复 | `clients/reference/src/onebot-gateway.ts`（修复前版本） |
| Core 侧“不确定的发送不自动重放” | 属实，且比审阅描述更强：`dispatched` 不会回到 `pending`；租约过期、被替换或 Core epoch 变化都把 `dispatched` 记为 `unknown`；相同结果重复回写幂等 | `crates/*-core/src/store/mod.rs` 的 `claim_pending`、`finish_operation`、`reap_expired`、`acquire_lease` |
| 工具调用是临时虚拟私聊会话，依赖真实群上下文、平台管理员或原生客户端的插件不能默认视为可作为工具运行 | 属实 | ADR 0002、第五阶段计划“已知限制”、AstrBot Host README |
| NoneBot 命令发现只识别 `CommandRule` 与 `ShellCommandRule` | 属实（关键词、正则、消息钩子被跳过） | NoneBot Host `commands.py` |
| 生产 `LocalCommand` 只负责启动与进程树管理，不提供 OS 级隔离 | 属实；与 AGENTS.md“现有临时目录和环境隔离不是 OS 安全沙箱”一致 | `crates/*-core/src/deployment/local.rs` |
| AstrBot Host README 仍称 `send_by_session` 不支持 | 属实；已改为“仅限已见会话，经 Core 账本” | `hosts/astrbot/README.md`、`bridge.py` |
| `session-handoff.md` 保留 AB-01 的旧入口 | 属实；本次交接已更新 | `session-handoff.md` |

## 2. 方向与排序

| 优先级 | 方向 | 任务 |
| --- | --- | --- |
| P0 | 真实平台端到端投递闭环 | P6-01（即 P4-05 / feat-030，沿用其完成标准） |
| P0 | Gateway 可靠性：异常收口、故障恢复、投递状态正确性 | P6-02 / feat-042 |
| P1 | 运行时隔离：不可信插件不得在宿主环境直接执行 | P6-03 / feat-043 |
| P1 | 可信兼容性：按能力维度、绑定证据的兼容性矩阵与发布门禁 | P6-04 / feat-044 |
| P2 | MCP 参数与发现体验、NoneBot 非命令 matcher 的边界说明 | 视 P6-04 结果再登记 |
| P2 | Windows Enforcement 与发行（P4-04 / P4-06） | 保持原有阻塞与门槛，独立工作流 |
| P3 | 更多框架与插件来源 | 暂缓 |

P6-01 与 P6-02 合为下一个里程碑：P6-02 不需要真实账号，先行完成；P6-01 需要用户授权的账号、群与现场时间。

## 3. 任务

| 任务 / feature | 依赖 | 交付 | 完成标准 |
| --- | --- | --- | --- |
| P6-01 / feat-030 | P6-02 | 以已验收的 Koishi Echo、NoneBot Roll、AstrBot Teamup 为代表，经平台中立 OneBot Gateway 做真实平台收发；入站事件、Host 响应、Core 操作、OneBot 回执、独立接收端观察分别记录 | 沿用 feat-030：每生态至少一项完成正例、隔离、重复/重启、超时/unknown 各三次；API accepted 与可观察投递分开；unknown 不计为成功；旧的三个 unknown 操作不重放 |
| P6-02 / feat-042 | TOOL-05 | 参考 Gateway 三段故障收口、回写重试、租约与 Core 重启恢复；单元故障注入与真实 Core 的现场故障注入 | 每种故障都有确定的账本终态或可解释的 unknown；无重复平台发送；受控崩溃后能处理新操作；2026-11-08 完成（见 3.1） |
| P6-03 / feat-043 | P6-02 | 运行时隔离：Host 受信任等级、资源限制、网络策略、可写目录、依赖准入、停止/销毁；先做 Linux 容器路径 | 非受信插件不能经 `LocalCommand` 直接在宿主执行；违规配置被拒绝并留记录；Windows Enforcement 单独验收，不降低门槛 |
| P6-04 / feat-044 | AB-05、TOOL-04 | 按能力维度的兼容性描述与矩阵：目录、pilot、工具级、Native/Federated、field 证据绑定到同一插件版本与哈希；PR 上跑轻量回归 | 区分 verified / failed / skipped / unknown / untested；每条对外兼容声明可追溯到具体证据文件与哈希 |

### 3.1 P6-02 结果（2026-11-08）

改动在 `clients/reference/src/onebot-gateway.ts`。每个领取的操作分三段：

| 阶段 | 出错时 | 理由 |
| --- | --- | --- |
| 准备（校验、消息段转换、未知操作） | `failed`，错误以 `not sent:` 开头 | 尚未调用 OneBot |
| 平台（OneBot 调用与应答解析） | 传输不确定为 `unknown`；应答无法解析时发送类 `unknown`、只读类 `failed` | 发送可能已生效；只读操作无副作用 |
| 回写（向 Core 报告） | 网络错误或 5xx：保留结果，下一轮以同一租约重写，不领取新操作、不重新发送；租约已过期或 4xx：丢弃并调用 `onUnreported`，Core 中该操作为 `unknown` | Core 对相同结果幂等；过期或被替换的租约无法完成操作 |

`run()` 不再因单个操作或 Core/OneBot 中断而退出：错误交给 `onError`，指数退避（上限 5 秒）；Core 拒绝租约后自动重新取租约。

验证（本机 Windows，2026-11-08）：

- `cd clients/reference && npm test`：23/23 通过。新增 `tests/onebot-gateway-faults.test.mjs` 11 项：准备段不支持的消息段与 6 类畸形请求、平台段不可解析应答（发送 / 只读）、非对象应答体、回写网络错误重试、5xx 重试后 403 丢弃、回写前租约过期、Core 连续不可达后恢复、Core 重启后的租约拒绝、OneBot 中断、退避中的 abort。每项都断言 OneBot 调用次数。
- `cd clients/reference && npm run test:live`（真实守护进程 + Koishi Host + 本地假 OneBot）：3/3 通过，连续 3 轮。新增两项现场故障注入：
  - 发送进行中杀掉 Core，并在同一 SQLite 账本、同一端口重启：被中断的操作在新 Core 中为 `unknown`（`core_epoch_changed`），Gateway 迟到的 `succeeded` 被拒绝并交给 `onUnreported`，下一条消息正常投递，OneBot 侧发送序列严格为 `FIRST, SECOND`。
  - 发送进行中由新的 Gateway 实例接管（模拟 Gateway 进程卡死或崩溃）：新租约替换旧租约，被放弃的操作为 `unknown`，新实例继续投递，同样无重放。
- 修复时发现现有现场测试在 Windows 上的清理问题：`finally` 先删临时目录、后等守护进程退出，SQLite 文件仍被占用（`EBUSY`），会掩盖测试本身的失败。现在先等进程退出再删除。

未覆盖，留作后续：

- 长时间 soak 与真实 OneBot 实现（NapCat 等）下的故障注入；以上均为本地假 OneBot。
- 被丢弃结果的只读核对：目前只交给 `onUnreported` 回调，Core 没有“补录观察”的接口；`unknown` 的后续核对仍按 P4 的只读恢复规则人工进行。
- Gateway 进程真正被杀（而非卡在请求中）的场景与上面的接管场景在 Core 侧等价（都是租约替换或过期），未单独测。

### 3.2 P6-03 设计要点（待实施）

- Host 配置新增受信任等级：`trusted`（自写或审阅过的插件，允许 `LocalCommand`）、`untrusted`（目录插件，默认）。`untrusted` 只能通过隔离启动器运行，Core 启动时校验并拒绝不满足的配置，拒绝原因写入日志与状态接口。
- Linux 路径复用实验室 Worker 的做法：固定镜像摘要、只读根、`network=none` 或显式白名单、内存与 PID 上限、只挂载必要的可写目录、预装且锁定的依赖（禁止运行时 pip/npm）、停止时销毁容器并确认无残留。
- Windows：沿用 P4-04 的门槛，不以自签名或 Audit 证据替代 Enforcement。
- 完成前先写 ADR，明确受信任等级与默认值。

### 3.3 P6-04 能力描述（待实施）

每个插件版本一条，替代单一的“支持 / 不支持”：

```json
{
  "plugin": "example-plugin",
  "framework": "astrbot",
  "artifact_sha256": "<冻结归档哈希>",
  "capabilities": {
    "mcp_tool": "verified",
    "federated_event": "untested",
    "proactive_send": "requires_gateway",
    "native_platform_api": "unsupported"
  },
  "evidence": ["outputs/acceptance/<report>.json#<case-id>"]
}
```

取值：`verified`、`failed`、`skipped`、`unknown`、`untested`，以及说明性的 `requires_gateway`、`unsupported`。`verified` 必须引用证据；静态推断（如插件引用 `aiocqhttp` 原生客户端）只能得到 `unsupported` 或 `untested`，不能得到 `verified`。MCP 工具列表可据此隐藏或标注在虚拟会话中无法正常工作的命令。

## 4. 成功指标

不以“新增多少 Host”或“测了多少插件”为主要指标：

- 端到端真实投递：三框架代表插件均有可信的真实投递证据（P6-01）。首要发布门槛。
- 投递正确性：重复平台发送次数为 0；未解释的 unknown 数量；每个 unknown 都有原因（`core_epoch_changed`、`lease_replaced`、`gateway_lease_expired` 或 Gateway 的具体错误）。
- 持续运行：操作超时率、异常恢复时间、Host 进程泄漏；阈值在 soak 测试测得数据后再定。
- 工具级行为：两组既定验收 42/42 只代表这些用例，不是整体兼容率。
