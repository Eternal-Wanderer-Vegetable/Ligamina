# 输出规范

适用：CLI、lab、兼容性汇总和 README。S2-08 已将 v1 schemas、golden fixtures、旧状态映射和可复算 summary 接入 Python/Rust `report` CLI，并为 lab run 命令加入 JSON/text/NDJSON 摘要输出。

## 当前已实现

- Core/Host stdout 输出协议帧；CLI status、doctor、report 输出 JSON，日志走 stderr。
- 现有 lab suite 有 completed、passed、failed、not_run、environment_missing 等格式差异。
- [lab/report.py](../../lab/report.py) 对 v1 run 校验 schema、预期 ID、状态和计数；summary 必须能由 results 重算。旧 T13/T18 等报告仍保留兼容摘要路径，不伪装成 v1。
- Rust `ligamina-cli report` 与 Python `python -m lab.report` 接受同一 v1 JSON，输出 JSON/text/NDJSON；默认 JSON。`lab.run --format` 输出 schema-versioned 的短命令摘要，完整证据继续保留在报告文件。
- `node scripts/render-readme.mjs` 更新 T18/T13 与第二阶段的独立标记区；`--check` 验证源报告、固定快照身份、输入哈希和 README 漂移。部分运行及尚未派发会直接显示。

## 第二阶段 v1 格式（合同已实现）

新增 `contracts/lab/v1/`，保留旧报告并提供显式转换。schema 和 golden fixtures 已提交；批次生命周期和断言结果分别表达：

| 字段 | 约定 |
| --- | --- |
| schema_version, suite, run_id | 版本化格式、suite 类型、稳定批次身份 |
| run_state | queued / running / completed / interrupted；completed 只表示记账闭合 |
| expected_result_ids | 本批次预期结果 ID 唯一集合；用于重算分母和 missing/unexpected |
| catalog | snapshot_id、来源、UTC 获取时间、raw SHA-256、固定 entry 分母、过滤/去重规则 |
| provenance | commit/dirty、框架/解释器/OS/架构、profile ID/hash、依赖锁、artifact hash、Worker 镜像摘要 |
| results[] | registry entry、包/版本/入口、OS/profile、stage、case_id、attempt_id、状态/原因 |
| status | passed / failed / skipped / unknown；未排程项也建账，使用 skipped + not_scheduled |
| reason_code, failure_domain | 稳定机器码 + 中文解释；registry/package/profile/native/federation/harness/infrastructure/external |
| evidence | 相对路径、SHA-256、脱敏日志/trace/回执/清理、结构化 reproduction argv 与工作目录 |
| delivery_scope | virtual / service_fixture / real_platform；支持声明附带范围 |
| summary, gate, gate_passed, exit_code | 可重算计数、覆盖率、所选 gate 及其退出状态；gate 是 completion / automation / compatibility 之一 |

v1 run 报告必须给出 `expected_result_ids`、`gate`、`gate_passed` 和 `exit_code`。消费者会重新计算 summary 并校验所记录的 gate 结果；缺少分母、summary 不一致或无效状态为输入错误（退出码 2）。展示矩阵按 ecosystem、official-maintained、platform、stage 汇总观察结果，完整预期分母与缺项仍以全局 summary 为准。

### 状态与原因映射

- 上游插件/框架异常或断言不符：failed，记录失败路径和原生/联邦结果。
- 明确缺少凭证、service、fixture、适配器或安装构建策略：skipped + 具体 reason_code。
- 不确定的超时、Worker 丢失、批次中断、无法分类旧状态：unknown；确定的清理超时断言另记 failed。
- 旧 not_run 映射 skipped；environment_missing 在原因可证时映射 skipped，否则 unknown；completed 属 run_state。
- 重试产生新的 attempt；保留首次和最后结果，间歇通过标 flaky，不能擦掉失败。

`run_state` 与 `result.status` 分开校验：run 可以是 `completed`，但 result 只接受 `passed/failed/skipped/unknown`。`normalize_legacy_status` 将 `not_run` 映射为 `skipped + not_scheduled`；`environment_missing` 只有存在明确证据时才映射为 skipped，否则为 unknown；legacy `completed` 映射为 unknown + `completed_is_run_state`。

### 分母与支持声明

- entry coverage = 有终态账目 entry / 快照全部 entry；有 skipped 仍须披露 execution coverage。
- stage execution coverage = (passed + failed + unknown) / 该 stage 全部应测单元；missing/unexpected/重复单独列出。
- executed pass rate = passed / (passed + failed + unknown)，零分母为 null；同时展示 passed / 全部应测单元。
- `lab.result.summarize_results` 按唯一 `result_id` 计算分母；重复 ID 单独列出且不重复增加通过计数，漏项和意外 ID 均带明细。零分母 rates 为 null，未知 v1 状态直接报错。
- 行为兼容 = 两路径执行并通过独立 oracle；支持名单要求全部必选场景、清理、可复现和非 flaky。
- 分别展示 observed cohort 和适用范围分类；native 不可运行时仍记录 federation 未执行及覆盖缺口。

### CLI 与日志

`lab.report` 和 Rust CLI 的 `report <report.json> [--format json|text|ndjson] [--gate completion|automation|compatibility]` 默认输出 JSON。NDJSON 每行是一条带 `schema_version` 的事件。`lab.run --format` 对 smoke、representative 和 catalog plan 输出短摘要；默认 JSON，不把日志混进 stdout。展示内容做密钥字段与常见内联凭证脱敏，并把长字符串限制在 4096 个字符；完整 trace/log 应保存为带哈希的 artifact 引用。

退出码：0=所选 gate 达标；1=所选 gate 未达标；2=参数/schema/输入错误；3=run 因基础设施中断。`completion` 只看 run 是否结束且预期账目齐全；`automation` 允许已归因的插件失败，但不能有未解决的 harness/infrastructure 失败、unknown 或未调度项；`compatibility` 要求所有预期结果通过。`completed` 本身不让任何 gate 自动通过。

### README 与完整报告

README 展示日期、快照/profile、分母、L0–L5、失败/跳过/未知、行为支持数和范围。完整 per-plugin 矩阵、JSON/CSV、失败详情和复现命令放在报告 artifact 并链接。生成区从已接受报告更新，输入报告哈希变化时检查失败；partial、queued、stale 或尚未派发的批次必须标记，不能显示为完成或支持。
