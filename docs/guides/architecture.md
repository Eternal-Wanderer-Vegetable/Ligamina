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
| 事件 | [router](../../crates/ligamina-core/src/router/mod.rs)：持久化回执、按会话排序与背压 |
| 平台动作 | [platform.rs](../../crates/ligamina-core/src/platform.rs) / [store](../../crates/ligamina-core/src/store/mod.rs)：授权、幂等、租约、回执和 unknown 恢复 |
| HTTP/MCP | [daemon](../../crates/ligamina-cli/src/daemon.rs) / [HTTP](../../crates/ligamina-cli/src/http.rs) / [MCP](../../crates/ligamina-cli/src/mcp.rs) |
| NoneBot | [Host README](../../hosts/nonebot/README.md)：OneBot v11 转换、Bot.handle_event 与框架生命周期 |
| Koishi | [Host README](../../hosts/koishi/README.md)：原生 Session/dispatch、插件服务依赖与 SQLite |
| Reference Client | [说明](../../clients/reference/README.md)：typed HTTP、独立 MCP SDK 与虚拟平台 |
| 实验室 | [run.py](../../lab/run.py)、[representative.py](../../lab/representative.py)、[独立原生基线](../../lab/baselines/paired_worker.py)、[比较器](../../lab/compare.py) |

## 边界与不变量

- Host framework 管理命令、Matcher/middleware、权限和插件依赖；Core 管理联邦路由、授权、持久化和进程生命周期。
- stdout 是协议通道，stderr 是日志通道；请求按字符串 ID 关联，Host epoch 隔离旧进程结果。
- 账号、群聊/私聊、会话 ID 共同限定事件与动作作用域。保持字符串 ID，覆盖大数与群/私同 ID。
- 已派发且失去回执的动作保持 unknown；重启不能证明真实发送成功。
- Bridge 平台能力有限，未支持 API 返回 unsupported_api；扩展必须补授权、schema、作用域和差分验证。
- NoneBot 2.5.0、Koishi core 4.18.11 是锁定 profile。Koishi 元包/loader、浏览器/控制台等需求使用独立 profile。
- S2-05 stock baseline 在不导入 Ligamina Bridge 的独立进程中调用原生 Bot/Session API；配对 Worker 固定 artifact、lock、config、stimulus 指纹后分别验证 Native 与 Federated 输出。
- 第一阶段验证虚拟 Gateway 收发；真实 QQ/平台网关不在已经通过的验收范围。

旧版本和工具链决策见 [ADR 0001](../adr/0001-baseline.md)，其中 readiness 按记录日期解释；当前裁决使用 progress 指向的最新报告。
