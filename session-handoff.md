# 会话交接

## Current Objective

- 日期：2026-10-14（Asia/Shanghai）。用户要求完成 `docs/plans/2026-10-12-phase-2-full-catalog.md` 的 S2-01–S2-12 并分阶段提交。
- 分支：`codex/phase2-full-catalog`。仓库新增文档先提交 `2d6399a`；已提交 S2-01 `15f468d`、S2-02 `c0be568`、S2-04 `2fa06b0`、S2-03 `29bc1e5`、S2-05 `854b564`、S2-06 `4e555b3`、S2-07 `7523a83`、S2-08 `cbcbb0a`、S2-09 `20d8f04`；`299d22c` 是此前 S2-09 baseline/status checkpoint；S2-10 census checkpoints `8f21ead`、`4295831`、`25cf805`、`7ac3f1c`、`6f864bb`、`fb3920e`、`01988bc`、`ee758da`、`36768b5`、`aa25211`、`aa89dbc`、`25a0d41`、`fa9fea8`、`bcb1e73`、`12f6203`、`45ade15`、`75c750c`、`3455a07`、`95909ff`、`498f436`、`a81dc61`、`e35bf26`、`2e44298`、`8e8189d`、`c457043`、`5a89e0f`、`60bfba3`、`466f9ec`、`1ed38d8`、`ac56cf7`、`f295313`、`f625b48`、`e2debcb`、`1455b31`、`f9c2c6b`、`c479f49`、`6356360`、`8a32e8e`、`6c1818f`、`31709fa`、`d071238`、`01d412e`、`5f7be0c`、`ecf4ebe`、`4047c76`、`f41c2f2`、`21d1336`、`0d56ba1`、`4c974f3`、`e491a3e`、`35d8428`、`b6c9237`、`2627109`、`da1322c`、`96350a3`、`084c015`、`1f1c3ae`、`3161907`、`0dfcaef`、`3f9c6ac`、`8b55df4`、`141cfee`、`4b044c7`、`9461053`、`cdb6a10`、`fe21968`、`9d401d6`、`eff6790`、`5213dac`、`1e15788`、`1bb005d`、`92b3f47`、a15acc3、be79e7a、a7bbc93、bed0c8c、2a793cb、ff409f6、0a740b7、5fee52b、e68bcff、8e254a3。
- 当前：S2-10 / feat-012 已完成并提交；S2-11 / feat-013 正在进行。S2-10 clean baseline 在 S2-09 HEAD `20d8f04db8c647da1f95336ca576966fccc15abb` 上通过；完整 census 已关闭，P2-CENSUS 通过。
- 断点：正式分母为 5,690 entries × 2 OS = 11,380 单元；10,332 locked profile 仅为候选。Linux shards 0–255/256、Windows shards 0–255/256，11,380/11,380 单元和 512/512 canonical shards 终态闭合，无缺失、重复或活动 runner。单元计数为 0 passed、1,235 failed、9,324 skipped、821 unknown；aggregate SHA-256 `bef7b141d791a49eafa844b9f792afcb967511ef507a080520d163ab6069f6f2`。P2-AUTOMATION 待 S2-11 修复和复跑后复核；P2-BEHAVIOR 未评估，P2-FIELD 未执行。SQLite 阶段作业为 30,843 passed、1,235 failed、35,380 skipped、822 unknown，共 68,280；job 分母与 11,380 unit 分母分开。行为 fixture 缺失、预算 provisional 和 App Control unknown 均保留；不绕过策略。

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

## Blockers

- P2-CENSUS is passed: 11,380/11,380 units and 512/512 canonical shards closed, with no missing or duplicate units. Original unit outcomes remain 0 passed, 1,235 failed, 9,324 skipped, 821 unknown. S2-11 attribution, the evidenced probe fix, and scoped regressions are complete; the required fresh 512-shard rerun remains open, so P2-AUTOMATION has not been adjudicated.
- The failure ledger attributes all 2,056 failed/unknown units. It includes 24 native-load failures with explicit App Control DLL-block diagnostics and 107 Windows npm OOM failures at the configured 8 GiB Job Object cap. The policy is not bypassed. Four previously report-invalid Koishi cases are now captured as native-load failures by the probe fix; the two frozen packages remain failed, with cleanup confirmed on both Linux Docker and Windows Sandbox.
- The original S2-10 database and reports remain immutable. The new rerun must use `.work/catalog-census/phase2-s211-rerun.sqlite3` and new `phase2-s211-rerun-census-{linux,windows}-shard*of256-20261014.json` reports. At last check, no runner was active. P2-BEHAVIOR is not evaluated, P2-FIELD is not run, the 5,621-entry behavior classification gap remains, 10,332 locked profiles are candidates only, and the resource budget remains provisional.

## Files

- Durable queue: `.work/catalog-census/phase2-s210.sqlite3`.
- Runner logs: `.work/catalog-census/runner-linux.log` and `.work/catalog-census/runner-windows.log`.
- Runner/resume script: `.work/catalog-census/run-remaining.ps1`.
- Aggregate: `outputs/acceptance/phase2-s210-census-interim-20261012.json`; shard evidence is in canonical `outputs/acceptance/phase2-s210-census-{linux,windows}-shard*of256-20261012.json` reports.
- Checkpoint refresher: `.work/catalog-census/refresh_checkpoint.py`; report and README validation uses `node scripts/render-readme.mjs --check`.
- Status and plan: `progress.md`, `feature_list.json`, `docs/plans/2026-10-12-phase-2-full-catalog.md`.

## Next Session

- Recheck `git status`, both runner logs, matching processes, and read-only queue state. Never resume the original S2-10 queue for the rerun; use the new S2-11 DB and output names above. Start only when no S2-11 runner is active.
- S2-11 evidence already committed: attribution ledger `464470d` (base data `4136171`), probe fix `760a5e1`, scoped regression report `ca6f735`, and full verification evidence `6496995`. The focused test is 1/1, Lab suite 109/109, four scoped Worker runs pass as harness runs (two frozen `process.exit(0)` packages on Linux Docker and Windows Sandbox), and `pwsh -NoProfile -File init.ps1 -Mode full` exits 0 (docs 18/228, NoneBot 3, Lab 109, Koishi 7, MCP 1, reference 5, Rust 33, Windows Core smoke 4/4/0, bundle passed). The packages remain native-load failures.
- Run a complete fresh 512-shard census against the same frozen snapshot and cohort, with a separate SQLite queue and per-platform 0–255 canonical reports. Retain original census, failure ledger, attempts, and all rerun shard hashes. Do not describe 5,621 behavior-unmapped entries, App Control unknowns, or failed native loads as passes.
- After all 512 reports are terminal and verified, aggregate only those canonical reports, refresh the S2-11 checkpoint and generated README, and separately adjudicate census, automation, behavior, and field gates. Review the observed native-success/federation-failure set alongside the fixture/loader coverage limitation.
- Once S2-11 exit criteria are met, complete S2-12 audit, README, compatibility matrix, evidence index, and handoff. Keep the task active until required validation and separate documentation commits are complete.
