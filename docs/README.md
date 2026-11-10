# 文档导航

首次使用先读 [项目 README](../README.md)；执行工作先读 [AGENTS.md](../AGENTS.md)。

| 需要做什么 | 从哪里开始 |
| --- | --- |
| 了解运行时和接口责任 | [架构与接口边界](guides/architecture.md) |
| 运行、安装、测试、排查 CI | [开发与验证](guides/development.md) |
| 判断插件是否兼容 | [兼容性实验](guides/compatibility.md) |
| 解读状态、日志、实验输出 | [输出规范](guides/reporting.md) |
| 实施第二阶段 | [全量目录测试计划](plans/2026-10-12-phase-2-full-catalog.md) |
| 复核第三阶段自动化与审计 | [第三阶段工程方案及执行记录](plans/2026-10-20-phase-3-automation.md)（工程交付完成，gate 仍有阻塞） |
| 实施第四阶段行为兼容与运行就绪 | [第四阶段计划](plans/2026-10-26-phase-4-behavior-readiness.md)（P4-05 的 stella 专用接入已按 ADR 0002 退役；field gate 等待平台中立接入） |
| 第六阶段运行可靠性与可信兼容性（已完成） | [第六阶段计划](plans/2026-11-08-phase-6-runtime-reliability.md)：真实投递闭环、Gateway 故障恢复、运行时隔离、能力维度兼容性矩阵 |
| 第七阶段万级插件自动化验证（当前，下一项 P7-01） | [第七阶段计划](plans/2026-11-09-phase-7-automated-verification.md)：能力发现、生成式差分、契约检查、有状态序列、变异检出率 |
| 第五阶段 AstrBot Host（AB-01–05 完成） | [第五阶段计划](plans/2026-11-07-phase-5-astrbot-host.md)、[AstrBot Host](../hosts/astrbot/README.md) |
| 让 bot 或 Coding Agent 使用插件 | [Bot 与 Agent 接入](guides/bot-integration.md)：MCP 命令工具，或事件 + OneBot v11 Gateway |
| 插件统一为 MCP 工具（TOOL-01–05 完成） | [插件工具面计划](plans/2026-11-07-plugin-tool-surface.md)、[ADR 0002](adr/0002-plugins-as-tools.md) |
| Host 受信任等级与容器隔离 | [ADR 0003](adr/0003-host-trust-and-isolation.md)、[架构与接口边界](guides/architecture.md#host-受信任等级与隔离) |
| 维护入口、指南与状态 | [文档维护](guides/documentation.md) |
| 查阅旧设计、验收和裁决 | [证据索引](evidence/README.md) |

当前状态在 [progress.md](../progress.md)，任务清单在 [feature_list.json](../feature_list.json)。指南解释当前行为；计划描述未来工作；证据记录特定版本、环境与日期下的结果。
