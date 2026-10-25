# 会话交接

## Current Objective

- 日期：2026-10-25（Asia/Shanghai）。继续 feat-019 / P3-05。Linux affected campaign `p3-recovery-linux-842f71712e2c` 已完成 2,206/2,206；最终报告与同一 ext4 queue 对账通过，39 个 unit unknown 仍保留，行为兼容仍 `not_evaluated`。Ubuntu-24.04 已按用户授权完成重启。
- P3-04 已对独立 PSF 签名 CPython 3.12.10 Worker profile 准入；原 catalog profile lock 和 CPython 3.12.14 blocker 不改写。P3-05 Windows 独立 pilot 使用 profile lock v3 和 Node 24.20.0，20 个冻结样本已在 Windows Sandbox 串行闭合。
- Windows pilot `p3-recovery-windows-pilot-v2-9666ebb4609f`：manifest canonical SHA `0796fad72d14f0e84cfd91b77e63177873a26510dfa3adee55c9a3298feed4fa`；报告 20/20 settled，failed=2、skipped=8、unknown=10、passed=0，cleanup confirmed=20、cleanup unconfirmed=0。报告文件 SHA `5e1f02f68e65faecf8048a5fc722cf613dda33b55792ae1e0e09a6a35d95d976`；最后 checkpoint SHA `b3ea7bc0e010168769c394bf1f16f4f7f86a586c4e4c4b2a020c111d95325406`。
- 10 个 NoneBot 样本依赖安装均通过，`pydantic_core` 原生 DLL 导入失败，保留 `unknown`。原 pilot 协议没有检出 policy marker，host CodeIntegrity/AppLocker 也无匹配事件；后续独立 Sandbox 诊断使用相同锁定 runtime 和 `pydantic-core` wheel，直接 `LoadLibrary` 得到 WinError 4551，确认该 `.pyd` 被 App Control 阻止。扩展 SHA-256 `d38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297`。报告 [native DLL diagnostic](outputs/acceptance/phase3-p305-windows-native-dll-diagnostic-20261025.json)，SHA-256 `66646448fc2dedd3a4d311fcf0d2e8ce6163832abe01f1fcf4b81d778837d013`。这给出共享 runtime 层的失败归因，不改变原 10 个 unknown，也不是 plugin compatibility 结论。没有 Sandbox 启动权限错误，因此没有宿主机插件执行。
- 10 个 Koishi 样本结果为 2 failed、8 skipped；两项失败具体是禁用 lifecycle scripts 后缺少 `sqlite3` native binding，以及空 provider profile 未提供 optional peer `@koishijs/plugin-console`；4 项因 required Host service 不可用而 skipped，另外 4 项 native load 正常但缺行为 fixture。20/20 Worker cleanup confirmed。失败/跳过归因不等于行为兼容裁决。
- Windows affected 总分母为 973（796 NoneBot +177 Koishi）；pilot 已闭合各 10 项，仍有 953 项未执行（786 NoneBot +167 Koishi）。P3-05 仍 `in-progress`，P3-08/P3-10 尚未满足依赖，behavior compatibility 保持 `not_evaluated`。不得运行 `init.ps1 -Mode full`、Windows smoke 或无 Sandbox 隔离的 catalog 插件。
- 本轮定向验证：CPython 3.14.7 `py_compile` 通过；`lab.tests.test_profiles lab.tests.test_catalog_run lab.tests.test_pilot_nonebot lab.tests.test_pilot_workers` 34/34 通过；`init.ps1 -Mode docs` 通过（19 documents / 315 local links）。DLL load failed 且没有明确 policy marker 时，新增中性 `windows_native_extension_load_unattributed` reason code；原已封存 pilot report 不回写。每 15 分钟状态 heartbeat 已更新为 Windows pilot 当前范围。

## Resume Commands

- 查看进度与 hash：读取 `outputs/acceptance/phase3-p305-windows-pilot-v2-checkpoint-20261025.json` 及同名 `.sha256`，并核对 `outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json` 的 canonical SHA。
- 原 pilot 已闭合，不得重启或重复派发；脚本有 campaign 互斥锁和 closed-report idempotency guard。其正式 manifest 是 `outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json`（文件 SHA `c28dcdddc035df73841e42a811abd4b4fd4c89c6fd795b783ea0214dcf9f21d5`）。
- NoneBot 原生扩展的失败已确认是 App Control 对锁定 `.pyd` 的 WinError 4551；不要关闭策略或在宿主机运行插件。若要继续覆盖 Python 条目，需要先获得经批准的 Sandbox 代码完整性策略/签名二进制方案，并冻结新 profile lock 与新 campaign；原 10 个 unknown 不改写。Koishi 两项 native failure 已分别归因为禁用 npm lifecycle scripts 缺 `sqlite3` binding，以及空 Host provider profile 缺 `@koishijs/plugin-console`；扩大前需冻结完整的新 provider/native dependency profile。完成这些准备后再决定全量 Windows 973 个 affected units 的可执行 scope。
- 运行 pilot 的脚本为 `scripts/phase3-windows-pilot.py`；当前报告和 checkpoint 都是 terminal，恢复执行不会重复已结算样本。Sandbox permission-error 条件未发生；不要把插件改成宿主机运行。
- 本轮未重启 WSL：整体 P3-05 仍未完成 Windows affected scope。不要终止与当前任务无关的 WSL/Python 进程。

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

- 继续 feat-019 / P3-05。不要重跑已完成的 Linux 2,206-unit campaign 或 Windows 20-unit pilot；保留所有 unknown/failed/skipped 与 hash 绑定。
- Windows 样本证据：`phase3-p305-windows-pilot-v2-execution-20261025.json`、其 checkpoint、v2 manifest 和 `scripts/phase3-windows-pilot.py`。先调查 `pydantic_core` DLL load failed 是否可由 guest 事件/静态 PE 依赖解释；本轮 policy marker=false，不能断定 App Control。调查 Koishi 的 `sqlite3` `hasInstallScript=true` 与 `@koishijs/plugin-console` optional peer，再冻结新的 Host/native dependency profile；不要原地改写现有 campaign。
- 完整 Windows affected ledger 是 796 NoneBot + 177 Koishi 项；扣除 pilot 已闭合的 10+10 项，还剩 786 NoneBot + 167 Koishi 未执行。扩展前需决定哪个子集具备完整 profile 和可重复运行条件，并创建新的 manifest、checkpoint、报告；没有可支持的 Python profile 时如实保留 unknown/blocker。P3-08 等 P3-05/P3-04/P3-07，P3-10 等 P3-05/P3-08/P3-09。
- 每 15 分钟中文汇报仍是用户偏好；automation `ligamina-p3-05` 已更新为该 Windows campaign。`init.ps1 -Mode full`、Windows Core smoke、宿主机 catalog 插件执行均不在范围内；本轮没有 Sandbox 权限错误，不触发本机 fallback。
- 完成 P3-05 整体前不要按旧 Linux completion 指令再次重启 WSL；届时先核对所有 WSL/相关 Worker 进程，避免终止其他任务。
