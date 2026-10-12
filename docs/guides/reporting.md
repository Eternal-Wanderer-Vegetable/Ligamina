# 输出规范

适用：CLI、lab、兼容性汇总和 README。S2-02 已建立 v1 executable schemas、golden fixtures、旧状态映射和 Python/Rust/TypeScript 验证；CLI 与 catalog scheduler 集成仍由 S2-08 完成。

## 当前已实现

- Core/Host stdout 输出协议帧；CLI status、doctor、report 输出 JSON，日志走 stderr。
- 现有 lab suite 有 completed、passed、failed、not_run、environment_missing 等格式差异。
- [lab/report.py](../../lab/report.py) 保留 passed/failed/skipped/unknown，统计 missing 与分母；不能直接充当全集汇总。
- 当前 CLI report 面向 suite 报告字段；T18 使用 task/verdict/gates，直接汇总会有 null。README 命令使用 T13 报告，T18 用原始 JSON 查看。S2-02 的 `lab.result.normalize_legacy_status` 提供逐状态转换；CLI 报告转换仍待 S2-08。
- node scripts/render-readme.mjs 从 T18/T13 生成 README 标记区；--check 检查漂移。完整证据保留原始报告。

## 第二阶段 v1 格式（合同已实现）

新增 `contracts/lab/v1/`，保留旧报告并提供显式转换。schema 和 golden fixtures 已提交；批次生命周期和断言结果分别表达：

| 字段 | 约定 |
| --- | --- |
| schema_version, suite, run_id | 版本化格式、suite 类型、稳定批次身份 |
| run_state | queued / running / completed / interrupted；completed 只表示记账闭合 |
| catalog | snapshot_id、来源、UTC 获取时间、raw SHA-256、固定 entry 分母、过滤/去重规则 |
| provenance | commit/dirty、框架/解释器/OS/架构、profile ID/hash、依赖锁、artifact hash、Worker 镜像摘要 |
| results[] | registry entry、包/版本/入口、OS/profile、stage、case_id、attempt_id、状态/原因 |
| status | passed / failed / skipped / unknown；未排程项也建账，使用 skipped + not_scheduled |
| reason_code, failure_domain | 稳定机器码 + 中文解释；registry/package/profile/native/federation/harness/infrastructure/external |
| evidence | 相对路径、SHA-256、脱敏日志/trace/回执/清理、结构化 reproduction argv 与工作目录 |
| delivery_scope | virtual / service_fixture / real_platform；支持声明附带范围 |
| summary, gates | 各生态/官方维护子集/OS/stage 分母、计数、覆盖率、完成与兼容性 gate |

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

S2-08 计划统一 --format json|text，默认 JSON；NDJSON 进度每行一条版本化事件。stdout 不混日志；stderr 使用时间、level、run_id/entry_id/stage/trace_id 和短消息，敏感值统一脱敏。

计划退出码：0=命令成功且所选 gate 达标；1=所选 gate 未达标；2=参数/schema/输入错误；3=基础设施中断。全集扫描记录插件失败并继续，最终退出码由明确选择的 completion 或 compatibility gate 决定。

### README 与完整报告

README 展示日期、快照/profile、分母、L0–L5、失败/跳过/未知、行为支持数和范围。完整 per-plugin 矩阵、JSON/CSV、失败详情和复现命令放在报告 artifact 并链接。生成区从通过 schema 校验的报告更新，部分/陈旧批次必须标记。
