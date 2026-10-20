# 会话交接

## Current Objective

- 日期：2026-10-20（Asia/Shanghai）。用户要求完成 [第三阶段计划](docs/plans/2026-10-20-phase-3-automation.md)，分阶段提交；P3-01/02 Linux、P3-03 Linux、P3-06、P3-07 Linux 已完成。P3-05 Linux 执行入口已交付，但 pilot 被精确冻结输入缓存缺失阻塞；P3-09 Linux CI/self-test 已完成并阶段提交 `df0705e`；当前等待后续依赖（P3-05 精确缓存、P3-04 Windows admission）。
- 计划/导航文档阶段提交 `e510d7c`；P3-01 实施提交 `0ccc0f6`；P3-02 实施提交 `80800a0`。
- `feature_list.json` 中 P3-01/02/03/06/07（feat-015/016/017/020/021）为 done；P3-02 Linux Worker admission 在 WSL2 Linux Docker 环境 10/10 benign probes 通过。P3-03 queue/dispatcher Linux tests 21 项通过，真实 Docker 标签清理注入通过，报告见 [P3-03 acceptance](outputs/acceptance/phase3-p303-linux-20261020.json)。P3-07 四个历史 Linux Docker 回归 passed，报告见 [P3-07 legacy regression](outputs/acceptance/phase3-p307-linux-legacy-20261020.json)。P3-05（feat-019）标为 blocked，P3-09（feat-023）已 done；无依赖满足且符合当前跳过 Windows 范围的下一阶段。P3-04 Windows admission 暂缓；Windows 测试未执行。
- P3-09 Linux hosted self-test 与 guarded controlled runner 已提交 `df0705e`；干净 checkout 证据：[report](outputs/acceptance/phase3-p309-linux-self-test-clean-checkout-20261020.json) + [log](outputs/acceptance/phase3-p309-linux-self-test-clean-checkout-20261020.log)。Node 8/8、focused Linux 54/54、docs 19/278、suite validator passed。失败注入证据：[controlled trap report](outputs/acceptance/phase3-p309-linux-controlled-trap-check-20261020.json)。GitHub 环境变量、Environment 审批保护及专用 runner 仍待部署仓库侧配置；没有声称真实 GitHub workflow 已运行。
- 方案关键项：逐阶段生成恢复集合、新 campaign 身份/DB、实际 Worker 准入、fencing/heartbeat/熔断/reconciliation、Windows 单独准入、20 entries 的独立 oracle 行为 pilot、CI 与 scoped gates。
- P2 历史裁决不变；Windows 当前主机能力与 P3 参数尚未实测。计划中的模块/命令/阈值属于设计，不可当成现有实现。
- 本次用户要求跳过 Windows 测试。所有 Windows runtime/OS 测试均未执行；一次较早 Linux baseline suite 误包含只读取 Windows bootstrap 脚本的静态测试，未启动 Windows/WSL。后续验证已显式排除该静态测试；Windows/Sandbox 实机证据仍待用户之后带到 Windows/WSL 环境进行全量实测。P2 原始结果没有被修改。
- P3-02 初次 docker-unavailable blocker report 保留：[phase3-p302-linux-admission-20261020.json](outputs/acceptance/phase3-p302-linux-admission-20261020.json)。之后安装 Docker Engine 29.1.3、通过当前代理 pull 锁定镜像并真实运行 10 项 probes。admission fingerprint `3bb6e32bdea454df69571d7e621c3027fe55c0d3992e974cebe1ed09efa47e42`；Windows gate 未测。
- 可复跑 Linux checks：`uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_automation_health lab.tests.test_workers.WorkerPolicyTests.test_limits_reject_non_positive_and_unbounded_values lab.tests.test_workers.WorkerPolicyTests.test_docker_command_is_digest_pinned_and_has_closed_boundaries lab.tests.test_workers.WorkerPolicyTests.test_docker_backend_rejects_unpinned_image_and_repository_input lab.tests.test_workers.WorkerPolicyTests.test_docker_worker_parses_peak_memory_sample -v`。在有 Docker 的 Linux 主机上先运行 Linux probes 到独立报告，再运行 `python -m lab.automation preflight --platform linux --probe-report <report> --output <health>`。
- P3-03 已交付 fencing / heartbeat / circuit breaker / reconciliation / Linux host lock。cleanup 未确认时 run 必须保持 interrupted。P3-04、Windows/Sandbox gate 暂缓；后续只在用户转到 Windows/WSL 阶段后执行相应测试。
- P3-05 pilot manifest：[worker-identity manifest](outputs/acceptance/phase3-p305-linux-pilot-rebuilt-worker-manifest-20261020.json)，20 units, campaign `p3-recovery-linux-8c161abc3e6e`。冻结 artifact/profile-lock cache 为 0/20 ready；attempt 在 Worker 启动前失败，未运行插件代码。unknown 被保留，精确 Docker cleanup reconciliation 已确认；19 units 未运行。详见 [checkpoint](outputs/acceptance/phase3-p305-linux-pilot-checkpoint-20261020.json)。恢复同 SHA 缓存后才能恢复相同身份补测；新解析的不同 lock 不可替换。
- P3-06 已完成 suite/schema/source/profile 身份冻结：[validation report](outputs/acceptance/phase3-p306-suite-validation-20261020.json)。20 entries / 60 cases / 4 独立历史回归；Linux 验证 18 项通过。没有插件运行，行为兼容仍 not evaluated；运行 `uv run --locked --project lab --python 3.12.14 python -m lab.automation validate-behavior` 可复核。Windows 测试未执行。

## Historical S2 Objective (2026-10-17)

- 日期：2026-10-17（Asia/Shanghai）。用户要求的 `docs/plans/2026-10-12-phase-2-full-catalog.md` S2-01–S2-12 均已完成，并已按阶段提交。
- 分支：`codex/phase2-full-catalog`。S2-12 按阶段提交：renderer/终态跟踪 `d4b35d7`、数据与审计报告 `7f81c70`、progress/feature tracker `c156b2c`、compatibility/证据索引 `1a1627f`、计划 `5f0e407`；本 handoff 为单独的最终 closeout 提交。
- 最终分母：512/512 canonical reports、11,380/11,380 catalog entry×OS units、68,280/68,280 selected stage jobs；报告与文件 SHA、SQLite run/manifest/job closure、Linux/Windows 连续分片审计通过，invalid reports=0；无活动/queued/leased work。
- 单元终态：0 passed、880 failed、7,530 skipped、2,970 unknown。SQLite stage-job 终态另为 24,914 passed、880 failed、37,357 skipped、5,129 unknown；这两个分母不同，stage-job passed 不等于 plugin compatibility。
- Gate 状态：P2-CATALOG/P2-CENSUS passed；P2-AUTOMATION blocked（2,158 Linux Docker API unavailable unknown、796 Windows App Control Win32 4551 unknown、16 其他 worker/preparation/timeout unknown）；P2-BEHAVIOR not evaluated；P2-FIELD not run。没有 catalog-wide compatibility pass 或新增 support claim；四个精确 T18 版本维持原范围。

## CI Diagnosis (2026-10-17)

- PR #3 当前 HEAD `b0615548d65fd0b04a3bb00a5a00db304cc32f20` 的 [GitHub Actions run #18](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/38988173249) 失败；前一次 [run #17](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/38915036215) 有相同失败。两个 run 的 Linux 和 Windows job 都在第一道 `node scripts/check-docs.mjs` 失败，错误为 `Stale phase-two report input: outputs/acceptance/phase2-s207-cli-smoke-20261012.json`。`scripts/check.sh:24` 与 `scripts/check.ps1:26` 均先运行该 gate，因此 pytest、lab、Koishi、Rust、smoke 和 bundle 后续步骤没有运行。
- 根因是证据文件哈希绑定了工作树换行字节：`.gitattributes` 要求 LF，renderer 在 [scripts/render-readme.mjs](scripts/render-readme.mjs) 对原始 `readFile` 字节计算 SHA-256，而 [S2-08 报告](outputs/acceptance/phase2-s208-20261012.json) 为 S2-07 输入记录了 Windows CRLF 字节哈希 `35ba6754590ed3aef560468c544daed7cc1c9e7b71e57441b4fb1ea16a383588`。当前 Windows 工作树哈希与该值相同；Git blob/干净归档 checkout 是 LF，哈希为 `2a5efbd5461ebbb9277f845fb199cf3404916e44f0c004a61c9900c27aeac159`。
- 复现证据：本地 `node scripts/check-docs.mjs` 退出码 0；从 HEAD 建立临时 `git archive` checkout 后运行同一命令，退出码 1 并出现 CI 的相同错误。只在临时归档副本中把 stale `throw` 改为记录，进一步发现 9 个 S2-09 source hash、1 个 S2-10 全局 source hash 和 512 个 S2-10 shard source hash 也依赖 CRLF 版本的同一 S2-09 证据文件。该诊断没有改动仓库中的 checker 或报告。
- 本节为根因诊断记录；修复与验证结果见下节。

## CI Fix (2026-10-17)

- `scripts/evidence-hash.mjs` 保留并校验历史 SHA-256：先比较原始字节，再对有效 UTF-8 文本比较 LF 与 CRLF 等价字节；孤立 CR 或无效 UTF-8 不会触发归一化。renderer 的所有原始文件哈希验证均改用该函数，覆盖 S2-08 input reports、S2-09 source inputs、S2-10 sources/shards/retry evidence、S2-11 shard files 与 S2-12 checkpoint。报告中的历史哈希与源证据文件都未重写。
- `scripts/evidence-hash.test.mjs` 覆盖实际 S2-07 失败输入、LF→CRLF 与 CRLF→LF、内容变化拒绝、无效 UTF-8 拒绝；Windows 与 Linux `scripts/check.*` 均在 docs gate 前运行该回归。
- 修复前 baseline：Windows `pwsh -NoProfile -File init.ps1 -Mode docs` 通过（18 documents / 233 local links）；从 HEAD `git archive` 建立 LF 归档后，`node scripts/check-docs.mjs` 以 stale S2-07 输入错误退出 1。
- 修复后：`node --test scripts/evidence-hash.test.mjs` 4 项通过；`pwsh -NoProfile -File init.ps1 -Mode docs` 通过；`node --check scripts/evidence-hash.mjs`、`node --check scripts/render-readme.mjs` 与 `git diff --check` 通过。从 LF 归档覆盖本次改动后，再运行回归与 `node scripts/check-docs.mjs` 均通过（18 documents / 233 local links）。
- 本地完整 `scripts/check.ps1` / `scripts/check.sh` 未运行；改动已提交并推送，commit `6161346`（`fix(ci): normalize text evidence hash line endings`）。GitHub Actions [run 38989675623](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/38989675623) attempt 2 的 Linux 和 Windows checks 均通过。Linux 的锁定检查、G1 smoke、bundle 与容器镜像构建通过；Windows 的锁定检查、G1 smoke、bundle 与 packaged runtime 验收通过。
- attempt 1 的 Linux job 通过；Windows 在 docs gate 和前序 Node/NoneBot/lab checks 通过后，Rust `host_session::eof_fails_pending_requests_and_in_flight_limit_is_enforced` 有一次关闭状态断言失败。日志没有保留实际 terminal error。本机该定向测试连续 25 次通过，`cargo test --workspace --locked` 也通过；failed-job rerun attempt 2 未复现且最终通过。未改动 Rust transport 或测试代码；此次间歇现象不影响已通过的双平台最终 run，也不改变插件兼容性结论。

## Completed Stages

- H01：Agent 文档 harness、专题指南、README 摘要与第二阶段计划。
- S2-01：官方 NoneBot / Koishi 完整目录快照，共 5,690 registry entries。
- S2-02：lab v1 schemas、状态映射、summary 合同和多语言 vectors。
- S2-04：Linux Docker 与 Windows Sandbox OS Worker；拒绝把 WSL namespace 当安全边界。
- S2-03：冻结 artifact 和依赖 profile 终态；5,690 entries、6,247 平台 artifact 引用、11,380 OS profile 单元；10,332 个完整依赖锁。sdist 后端尚未运行。
- S2-05：独立 stock NoneBot/Koishi runner、同冻结 artifact/lock/config/stimulus 的 Federated 路径、独立 expected oracle 和双路 comparator。四个历史支持版本全通过；Native source 无 Ligamina Bridge import。
- S2-06：两生态各十类行为 recipe；entry/profile/config/service/fixture binding、缺条件 skipped 与 oracle-first pair comparator 已完成。全量目录只有 69 entries 获得保守分类建议，其余 5,621 以 unmapped_category 留账。
- S2-07：SQLite 持久 stage queue、六阶段 DAG、attempt/journal、lease/retry、resume 与稳定 shard 已验收。完整报告：[phase2-s207-20261012.json](outputs/acceptance/phase2-s207-20261012.json)。真实快照 plan/resume 证据：[初始计划](outputs/acceptance/phase2-s207-cli-smoke-20261012.json)、[resume](outputs/acceptance/phase2-s207-cli-resume-20261012.json)。
- S2-08：Python/Rust v1 报告校验与 projection、catalog plan 摘要、JSON/text/NDJSON、计数矩阵、脱敏和 completion/automation/compatibility gates 已通过全仓库验收。README phase-one/phase-two generated block 校验了源报告 hash，并拒绝 stale report/README 内容。详见 [S2-08 acceptance](outputs/acceptance/phase2-s208-20261012.json)。没有派发 catalog plugin worker。
- S2-09：分层双平台 pilot 完成，结果与 higher-gate 限制保存在 `outputs/acceptance/phase2-s209-20261012.json`。
- S2-10：11,380-entry×OS census 和 512 个 canonical shard 记账闭合；原始结果保持不可变。
- S2-11：失败归因、process-exit probe 修复、scoped regression 与 512-shard full rerun 完成；详见 `outputs/acceptance/phase2-s211-process-exit-regression-20261014.json`。
- S2-12：最终 audit、compatibility matrix、README、证据索引和各状态文档完成；gate 边界见两个 `phase2-s212-*20261017` 交付物。

## S2-07 Evidence

- `uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_queue lab.tests.test_catalog_run -v`：16 项通过；中断/重开覆盖 prepare/install/native/federated/compare/cleanup 六阶段，且验证过期 lease、unknown attempt/artifact 引用、重试、并发 claim、幂等与分片。
- `pwsh -NoProfile -File init.ps1 -Mode full`：退出码 0，docs 18/153 本地链接、NoneBot 3、lab 76、Koishi 7、MCP 1、reference 5、Rust 29；Windows Core smoke 4 events / 4 Gateway ops / 0 PID，bundle stage 通过。生成的 `ci-smoke-windows.json` 已恢复。
- 在冻结快照 `p2-41649744e057d61031d5` 上以 shard `288/1000` 计划 3 个 Linux 单元/18 个作业；同一 SQLite 队列 resume 后 manifest SHA-256 不变且 18 个 job 保持 queued。
- 此 CLI 只计划/恢复队列；没有派发第三方插件 Worker 或完成目录 census。5,621 个 entry 仍缺保守 recipe 类别建议。真实快照 full raw→normalized 检查见 S2-01 验收；每 shard 执行原始字节/文档哈希与锁交叉校验。

## S2-08 Evidence

- Full `pwsh -NoProfile -File init.ps1 -Mode full` exited 0: docs 18/161; NoneBot 3; lab 85; Koishi 7; MCP 1; reference 5; Rust 33; Windows Core smoke 4 events / 4 gateway operations / 0 PIDs; bundle stage passed. Generated `outputs/acceptance/ci-smoke-windows.json` was restored.
- Focused verification: Python report/contract 18 tests, Rust CLI 5 tests and contracts 4 tests; valid/invalid CLI fixtures returned 0/2; stale source report and stale README block both rejected with exit 1.

## S2-09 Evidence

- Pilot manifest: [phase2-s209-pilot-manifest-20261012.json](outputs/acceptance/phase2-s209-pilot-manifest-20261012.json) includes 30 NoneBot + 30 Koishi catalog entries, all four legacy support versions, and a full 11,380-entry×OS assignment ledger. The pilot denominator is 128 results.
- Legacy regression: [phase2-s209-legacy-linux-20261012.json](outputs/acceptance/phase2-s209-legacy-linux-20261012.json) reran all four Native/Federated/oracle pairs in the Linux Docker Worker; all passed and cleanup was confirmed. Peak memory was 246–272 MB, Worker input 190–210 MB, duration 17.578–23.625 seconds. The original S2-05 report hash is unchanged.
- The first replay found `lab/recipes.py` was missing from the copied worker module closure after the S2-08 comparator import change. The failed attempt is preserved in [attempt1](outputs/acceptance/phase2-s209-legacy-linux-attempt1-20261012.json); after packaging the module and making schema validation import lazy, all four passed.
- [phase2-s209-20261012.json](outputs/acceptance/phase2-s209-20261012.json) closes 128 positions as 4 passed, 11 failed, 88 skipped, 25 unknown. The three catalog reports are individually 30/30 closed: [NoneBot Linux](outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json), [Koishi Linux](outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json), and [Koishi Windows](outputs/acceptance/phase2-s209-koishi-windows-pilot-20261012.json). See [failure review](outputs/acceptance/phase2-s209-failure-review-20261012.json) for eleven native-load errors and the Windows retry evidence.
- NoneBot Linux: 21 Worker installs/cleanups passed, three native-load failures share the frozen htmlrender API mismatch; dependency downloads total 24,957,125 bytes and Linux RSS/scratch are measured. Koishi Linux and Windows each: 22 installs/cleanups passed and four native-load failures. Koishi Linux/Windows scratch and Windows Job Object peak committed memory are also measured; Windows P95 peak commit is 6.71 GB with an 8 GiB limit. These pilot distributions do not establish a full-census resource budget.
- The entry-bound behavior stage has not run; behavior is not accepted. Windows CPython App Control Win32 4551 leaves 23 NoneBot catalog rows unknown, and two historical Windows rows remain unknown. The budget remains provisional; no full census or field acceptance has started.
- README renderer and evidence block check passed; `pwsh -NoProfile -File init.ps1 -Mode docs` passed (18 documents, 191 local links). Focused S2-09 suite passed (25 tests); `pwsh -NoProfile -File init.ps1 -Mode full` exited 0 with lab 102, NoneBot 3, Koishi 7, MCP 1, reference 5, Rust workspace green, Windows smoke 4 events / 4 Gateway operations / 0 PIDs, and bundle stage passed. `git diff --check` passed. The generated `ci-smoke-windows.json` was restored to its original SHA-256 `033D8EA2534603B880DB2A729B328C3A75C7F4CF0B8F8DEE087C22BEBBCAC752`. The pilot execution meets S2-09 criteria while higher behavior/automation gates remain open; commit the evidence and start S2-10.

## S2-10 Baseline

- S2-09 stage commit: `20d8f04db8c647da1f95336ca576966fccc15abb`.
- S2-10 clean baseline: `pwsh -NoProfile -File init.ps1 -Mode full`, exit 0; 18 docs/191 links; NoneBot 3, lab 102, Koishi 7, MCP 1, reference 5, Rust 33; Windows Core smoke 4 events / 4 Gateway operations / 0 PIDs; bundle stage passed. Evidence: `outputs/acceptance/phase2-s210-baseline-20261012.json`. Generated smoke JSON was restored.
- Profile lock statuses identify 10,332 candidate entry×OS units, not completed executions. The isolated Linux smoke report remains excluded from formal census. Formal Linux shard `0/256` is complete at 16 units / 96 terminal jobs (4 failed, 12 skipped). Windows `0/256` attempt 1 is preserved; a regression now projects confirmed Worker cleanup independently when plugin protocol output is absent, and the shard is being rerun with this correction. Full 11,380-unit census remains open; App Control Win32 4551 remains an evidenced blocker and the resource budget is provisional. Implementation and verification are recorded in `outputs/acceptance/phase2-s210-implementation-20261012.json`.

## Final Verification

- `python scripts/build-s212-audit.py`：512 份 canonical 报告通过完整性和 SQLite/分片闭合检查；invalid reports=0；生成 S2-12 audit 与 compatibility matrix。报告解释 unit-level passed=0：六个选定阶段必须全通过才算单元通过，而全部 11,380 单元的 federated 与 compare 都是 skipped。
- `pwsh -NoProfile -File init.ps1 -Mode docs`：通过，18 documents / 231 local links。
- `node scripts/render-readme.mjs --check`、`node --check scripts/render-readme.mjs`、`node --check scripts/check-docs.mjs`：通过。
- `node .agents/skills/harness-creator/scripts/validate-harness.mjs --target . --json`：100/100。
- feature/audit JSON 解析及 `git diff --check`：通过。S2-11 scoped regression 与全仓库 `init.ps1 -Mode full` 结果保存在回归证据中；S2-12 未重复派发插件或启动 runner。

## Evidence and Limits

- Final audit: `outputs/acceptance/phase2-s212-audit-20261017.json`.
- Compatibility matrix: `outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md`.
- Final S2-11 checkpoint and commands: `outputs/acceptance/phase2-s211-process-exit-regression-20261014.json`.
- Runner logs: `.work/catalog-census/runner-s211-linux.log` ends with Linux platform closed; `.work/catalog-census/runner-s211-windows.log` ends with 255/256 shard closed and Windows platform closed; Linux log records `S2-11 RERUN COMPLETE 512/512`.
- Docker Desktop 29.8.2 availability was checked only after the rerun; it does not retroactively resolve the 2,158 recorded Docker API unknowns. Do not rerun against the S2-10 database; a valid future re-attempt needs a new scoped execution with its own preserved report set.
- No next S2 feature remains. Behavior fixtures/mappings and real-platform login/action/receipt/recipient evidence are outside the completed evidence; keep those gates not evaluated/not run until separately executed.

## Blockers

- The S2-12 plan is complete, while P2-AUTOMATION remains blocked by rerun infrastructure/platform unknowns listed above. P2-BEHAVIOR and P2-FIELD remain not evaluated/not run; do not publish broader compatibility claims.
- The post-rerun Docker Desktop availability check did not create new plugin evidence. A future attempt needs a separately scoped rerun with preserved new reports and gate adjudication.

## Files

- Final machine audit and matrix: `outputs/acceptance/phase2-s212-audit-20261017.json`, `outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md`.
- Full S2-11 checkpoint, source hashes, and replay commands: `outputs/acceptance/phase2-s211-process-exit-regression-20261014.json`.
- Failure attribution and scoped regression: `outputs/acceptance/phase2-s211-failures-20261014.json`, `outputs/acceptance/phase2-s211-process-exit-regression-20261014.json`.
- Current status and plan: `progress.md`, `feature_list.json`, `docs/plans/2026-10-12-phase-2-full-catalog.md`; compatibility scope: `docs/guides/compatibility.md`.

## Next Session

- Next: P3-09 is complete. Continue P3-05 only if the exact frozen artifact and dependency-lock bytes can be restored; P3-08 remains ineligible until P3-04 and P3-05 are resolved, and P3-10 also depends on P3-05/P3-08. Per user instruction, skip Windows runtime/tests until the Windows/WSL phase. Preserve the interrupted recovery campaign and all regression attempt evidence; do not reuse S2 queues or rewrite historical reports.
- Do not restart the old S2-10 queue or relabel existing unknowns based on later Docker availability.
- Behavior and real-platform acceptance require their missing fixtures/mappings and explicit platform evidence; keep those separate from this completed audit until separately performed.
