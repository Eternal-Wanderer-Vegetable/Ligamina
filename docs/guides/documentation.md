# 文档维护

适用：README、AGENTS、指南、计划、状态和历史索引。依据：[课程第四讲](https://walkinglabs.github.io/learn-harness-engineering/zh/lectures/lecture-04-why-one-giant-instruction-file-fails/)和[已安装 harness-creator](../../.agents/skills/harness-creator/SKILL.md)。

## 权威位置

| 内容 | 维护位置 | 更新触发 |
| --- | --- | --- |
| 用户入口与结果摘要 | 根 README；证据表由脚本生成 | 功能或验收变更 |
| Agent 路由与硬约束 | AGENTS.md | 通用约束或任务路由变化 |
| 当前行为与操作 | docs/guides/、模块 README | 相应实现/命令变化 |
| 当前任务状态 | feature_list.json | 开始、阻塞、完成和验收 |
| 当前证据/下一步 | progress.md / session-handoff.md | 每次会话结束 |
| 未来任务/依赖/验收 | docs/plans/ | scope 或执行设计变化 |
| 特定日期的计划/审阅/结果 | outputs/ 与 docs/adr/ | 追加新证据，旧记录保持原文 |

## 预算与路由

- AGENTS 目标 50–100 行，检查上限 100；指南目标 50–150 行，上限 150。
- README 上限 150 行；实施计划允许更长，以任务表和专题链接组织。阈值是维护预算，不是效果保证或 token 换算。
- 路由写明触发条件；按当前任务读取专题和历史证据。
- 新增指南先复用已有权威位置；完整矩阵放在报告目录。
- 历史 Markdown 加入 [证据索引](../evidence/README.md)。旧 NOT READY 是当时结论，当前状态用最新证据解释。

## 约束来源、范围和复审

| AGENTS 规则 | 来源 | 适用条件 | 复审/替换条件 |
| --- | --- | --- | --- |
| 单任务、状态与交接 | harness-creator；本次交接需求 | 工程执行 | 引入明确任务所有权时修订 |
| 原始发布物、精确支持 | C03/C10/C11；T13/T18 | 插件实验/支持声明 | 报告 schema/版本矩阵变化 |
| NDJSON stdout、stderr、脱敏 | contracts/v0.1；当前 Host/CLI | 协议/报告 | 版本化 transport 迁移 |
| 会话作用域、unknown | Core store/router/platform；C04/C05/C09 | 事件/平台动作 | 新回执协议有端到端证据 |
| 全量 OS 隔离 | representative isolation_boundary；范围扩大 | 第三方运行 | 有等效可证明隔离后端 |
| 指南与历史记录分离 | 第四讲；多次验收记录 | 文档/状态 | 归档系统替代并保持可追溯 |

## 维护流程

1. 核对源码/配置/报告，明确当前事实、未来计划或历史证据。
2. 修改权威位置，更新导航和需要的 feature status。
3. 用 node scripts/render-readme.mjs 更新证据表。
4. 运行 node scripts/check-docs.mjs，检查链接、预算、索引、依赖图、done 证据和漂移。
5. 运行 node .agents/skills/harness-creator/scripts/validate-harness.mjs --target .；分数只代表结构，不证明插件兼容或 agent 性能。
6. 交接记录实际验证命令/输出和下一任务。文档整理不需重复 24 小时实验；运行时改动按开发指南验证。
