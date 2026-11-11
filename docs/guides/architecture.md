# 架构与接口边界

适用：修改 Core、Host、Client、事件或操作协议。依据：当前源码、[v0.1 schemas](../../contracts/v0.1/event-envelope.schema.json)、[第一阶段验收](../../outputs/acceptance/t18-final-audit-20261012.json)。结构变化时同步更新本指南。

## 运行路径

客户端通过 loopback HTTP 或 MCP 调用 Rust Core；Core 启动和监督原生 Host 进程，使用 UTF-8 NDJSON stdio 交换请求。Host 通过原生框架处理事件，将平台动作交回 Core；客户自己的 Gateway 领取动作并写回回执。

| 模块 | 事实来源与职责 |
| --- | --- |
| 协议 | [contracts/v0.1](../../contracts/v0.1/event-envelope.schema.json)：事件、能力、操作 schemas；三语言共享 fixtures |
| Core runtime | [runtime.rs](../../crates/ligamina-core/src/runtime.rs)：组合 supervisor、broker、router、store 和授权 |
| 生命周期 | [supervisor.rs](../../crates/ligamina-core/src/supervisor.rs)：Host readiness、epoch、退出、重启和进程树回收 |
| 能力 | [broker.rs](../../crates/ligamina-core/src/broker.rs)：按 Host generation 替换能力，校验输入与输出 |
| 命令工具 | [commands.rs](../../crates/ligamina-core/src/commands.rs)：Host 上报的原生命令目录（[v0.2 合同](../../contracts/v0.2/command-descriptor.schema.json)）与虚拟会话 tool sink；MCP `cmd.*` 工具见 [mcp.rs](../../crates/ligamina-cli/src/mcp.rs)，决策见 [ADR 0002](../adr/0002-plugins-as-tools.md) |
| 事件 | [router](../../crates/ligamina-core/src/router/mod.rs)：持久化回执、按会话排序与背压 |
| 平台动作 | [platform.rs](../../crates/ligamina-core/src/platform.rs) / [store](../../crates/ligamina-core/src/store/mod.rs)：授权、幂等、租约、回执和 unknown 恢复 |
| HTTP/MCP | [daemon](../../crates/ligamina-cli/src/daemon.rs) / [HTTP](../../crates/ligamina-cli/src/http.rs) / [MCP](../../crates/ligamina-cli/src/mcp.rs) |
| NoneBot | [Host README](../../hosts/nonebot/README.md)：OneBot v11 转换、Bot.handle_event 与框架生命周期 |
| Koishi | [Host README](../../hosts/koishi/README.md)：原生 Session/dispatch、插件服务依赖与 SQLite |
| AstrBot | [Host README](../../hosts/astrbot/README.md)：未修改的 AstrBot 4.29.1 无头生命周期、`ligamina_bridge` 平台适配器、命令发现（内置 star 不暴露） |
| Reference Client | [说明](../../clients/reference/README.md)：typed HTTP、独立 MCP SDK、虚拟平台与平台中立的 [OneBot v11 Gateway](../../clients/reference/src/onebot-gateway.ts)（接入见 [Bot 与 Agent 接入](bot-integration.md)） |
| 实验室 | [run.py](../../lab/run.py)、[representative.py](../../lab/representative.py)、[独立原生基线](../../lab/baselines/paired_worker.py)、[比较器](../../lab/compare.py) |

## 边界与不变量

- Host framework 管理命令、Matcher/middleware、权限和插件依赖；Core 管理联邦路由、授权、持久化和进程生命周期。
- stdout 是协议通道，stderr 是日志通道；请求按字符串 ID 关联，Host epoch 隔离旧进程结果。
- 账号、群聊/私聊、会话 ID 共同限定事件与动作作用域。保持字符串 ID，覆盖大数与群/私同 ID。
- 已派发且失去回执的动作保持 unknown；重启不能证明真实发送成功。
- 命令工具调用使用保留账号（Koishi `ligamina-tool`，NoneBot/OneBot `0`）和 Core 生成的 trace；这类操作只在 Core 内完成或拒绝，绝不进入 ledger/Gateway，工具结果也不是平台投递证据。
- Bridge 平台能力有限，未支持 API 返回 unsupported_api；扩展必须补授权、schema、作用域和差分验证。
- NoneBot `send_group_forward_msg` 的受限自定义 node 输入映射到现有 `send_message` operation 的 outbound `forward_node` segments；Reference OneBot Gateway 再用标准 `send_group_msg` 发送 node segments。它不扩展 EventEnvelope 的 inbound message schema。
- NoneBot 2.5.0、Koishi core 4.18.11 是锁定 profile。Koishi 元包/loader、浏览器/控制台等需求使用独立 profile。
- S2-05 stock baseline 在不导入 Ligamina Bridge 的独立进程中调用原生 Bot/Session API；配对 Worker 固定 artifact、lock、config、stimulus 指纹后分别验证 Native 与 Federated 输出。
- 第一阶段验证虚拟 Gateway 收发；真实 QQ/平台网关不在已经通过的验收范围。

## Host 受信任等级与隔离

决策见 [ADR 0003](../adr/0003-host-trust-and-isolation.md)。守护进程配置中每个 Host 有 `trust`：

- `trusted`（未声明 `isolation` 时的缺省值）：以 `program` / `args` 作为本地进程运行，启动时在 stderr 打印一条“未经 OS 隔离”的提示。
- `untrusted`（声明了 `isolation` 时的缺省值）：只能由 Core 生成的容器运行，不得同时给出 `program`、`args`、`current_dir`、`environment`。目录插件一律按此运行。

`isolation` 只写镜像、挂载与资源，加固项由 Core 固定添加（`--network none --read-only --cap-drop ALL --security-opt no-new-privileges`、`--pids-limit`、`--memory`、`--cpus`、`--rm -i`、唯一容器名与归属标签）：

```json
{
  "id": "field-nonebot-roll", "trust": "untrusted",
  "isolation": {
    "docker": ["C:\\Windows\\System32\\wsl.exe", "-e", "docker"],
    "image": "<name>@sha256:<64 hex>",
    "entrypoint": "/usr/local/bin/python", "command": ["/input/field/nonebot_host.py"],
    "mounts": [{ "source": "/home/me/bundle", "target": "/input/project" }],
    "tmpfs": [{ "target": "/work", "size_mb": 768, "exec": true }],
    "environment": { "LIGAMINA_FIELD_PLUGIN": "nonebot_plugin_roll" },
    "memory_mb": 1024, "cpus": 1, "pids_limit": 128
  }
}
```

- 拒绝规则（守护进程以非零状态退出，错误写明 Host 与规则）：镜像未按摘要固定、网络不是 `none`、资源上限缺失或非正、untrusted Host 的可写绑定挂载、挂载目标为 `/`、`/proc`、`/sys`、`/dev` 或非绝对路径、`isolation` 中的未知字段。
- 收尾：每次启动的容器名为 `ligamina-<host>-<epoch>-<随机>`；Host 进程退出、重启或停止后 Core 执行一次 `docker rm -f <容器名>`；守护进程启动时按 `io.ligamina.owner`（由数据库路径派生）与 Host 标签清理上一轮遗留的容器。
- 不覆盖：Windows 原生隔离（按 P4-04 单独验收）、网络白名单、自定义 seccomp/AppArmor。实验室 Worker 的准入与清理确认不受影响。

旧版本和工具链决策见 [ADR 0001](../adr/0001-baseline.md)，其中 readiness 按记录日期解释；当前裁决使用 progress 指向的最新报告。
