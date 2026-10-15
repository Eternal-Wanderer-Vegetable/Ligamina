# 会话交接

## Current Objective

- 日期：2026-10-15（Asia/Shanghai）。用户要求完成 `docs/plans/2026-10-12-phase-2-full-catalog.md` 的 S2-01–S2-12 并分阶段提交。
- 分支：`codex/phase2-full-catalog`。仓库新增文档先提交 `2d6399a`；S2-01–S2-10 已按阶段提交，S2-11 归因、修复、scoped regression 与持续复跑证据均已提交。最新复跑 shard 数据与活动队列 checkpoint `0c2a6c2`（Windows shards 113–118，活动队列为 shard 119），progress/feature/README checkpoint `e9a0f38`；兼容指南与计划将按本阶段分别提交；活动分片为 Windows shard 119，由 runner PID 28372 执行。
- 当前：S2-10 / feat-012 已完成并提交；S2-11 / feat-013 正在进行。S2-10 clean baseline 在 S2-09 HEAD `20d8f04db8c647da1f95336ca576966fccc15abb` 上通过；完整 census 已关闭，P2-CENSUS 通过。
- 断点：冻结目录仍为 5,690 entries × 2 OS = 11,380 个单元；10,332 locked profiles 仅是候选。S2-10 原始 census 保持不变并已闭合。S2-11 新队列截至 2026-10-15 19:44:27（Asia/Shanghai）有 375/512 份完整 canonical 报告、8,334/11,380 单元；Linux shards 0–255、Windows shards 0–118 已闭合，Windows shard 119 正在运行（PID 28372）。已闭合单元 0 passed、538 failed、5,254 skipped、2,542 unknown；剩余 137 shards、3,046 units。SQLite 阶段作业快照另计 17,231 passed、542 failed、27,597 skipped、4,701 unknown、52 queued、1 leased；通过的 stage job 不代表插件单元兼容通过。375 报告均通过报告/文件 SHA、SQLite run/manifest/job closure、连续无重复分片检查，invalid reports=0。P2-AUTOMATION 等完整 rerun 后裁决；P2-BEHAVIOR 未评估、P2-FIELD 未执行。

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
- The original S2-10 database and reports remain immutable. The new rerun uses `.work/catalog-census/phase2-s211-rerun.sqlite3`, runner logs `.work/catalog-census/runner-s211-{linux,windows}.log`, and `phase2-s211-rerun-census-{linux,windows}-shard*of256-20261014.json` reports. It started at 2026-10-14 17:06 Asia/Shanghai. At the 2026-10-15 19:44:27 Asia/Shanghai checkpoint, 375/512 canonical reports cover 8,334/11,380 units; Linux shards 0–255 and Windows shards 0–118 are terminal, and Windows shard 119 is active under runner PID 28372. Closed unit outcomes are 0 passed, 530 failed, 5,155 skipped, and 2,529 unknown; 143 shards and 3,166 units remain. SQLite separately records 17,231 passed, 542 failed, 27,597 skipped, 4,701 unknown, 52 queued and 1 leased. Completed reports account for 50,004 jobs, with 120 additional terminal jobs from the active shard. Stage-job passes are not plugin-unit compatibility passes. All 375 reports passed report/file SHA, SQLite run/manifest/job closure and contiguous-shard checks; invalid reports=0. Data checkpoint `0c2a6c2` adds Windows shards 113–118 and refreshes shard 119 state. Do not launch a duplicate. P2-BEHAVIOR is not evaluated, P2-FIELD is not run, the 5,621-entry behavior classification gap remains, 10,332 locked profiles are candidates only, and the resource budget remains provisional.

## Files

- Original S2-10 queue: `.work/catalog-census/phase2-s210.sqlite3` (do not resume for S2-11). S2-11 rerun queue: `.work/catalog-census/phase2-s211-rerun.sqlite3`.
- Original S2-10 runner logs: `.work/catalog-census/runner-linux.log` and `.work/catalog-census/runner-windows.log`; S2-11 rerun logs: `.work/catalog-census/runner-s211-linux.log` and `.work/catalog-census/runner-s211-windows.log`.
- Original S2-10 runner: `.work/catalog-census/run-remaining.ps1`; S2-11 full rerun runner: `.work/catalog-census/run-s211-full.ps1` (resume only after confirming no active runner).
- Aggregate: `outputs/acceptance/phase2-s210-census-interim-20261012.json`; shard evidence is in canonical `outputs/acceptance/phase2-s210-census-{linux,windows}-shard*of256-20261012.json` reports.
- Checkpoint refresher: `.work/catalog-census/refresh_checkpoint.py`; report and README validation uses `node scripts/render-readme.mjs --check`.
- Status and plan: `progress.md`, `feature_list.json`, `docs/plans/2026-10-12-phase-2-full-catalog.md`.

## Next Session

- Recheck `git status`, both S2-11 runner logs, matching processes, and read-only queue state. At the 2026-10-15 19:44:27 checkpoint, Windows shards 0–118/256 are closed, shard 113/256 is active (runner PID 28372); completed canonical reports are 375/512 covering 8,334/11,380 units. Do not start another runner. If PID 28372 stops, inspect its log, canonical report and SQLite state first; resume `.work/catalog-census/phase2-s211-rerun.sqlite3` through `pwsh -NoProfile -File .work/catalog-census/run-s211-full.ps1` only after confirming there is no active runner. Never resume the original S2-10 queue.
- S2-11 evidence already committed: attribution ledger `464470d` (base data `4136171`), probe fix `760a5e1`, scoped regression report `ca6f735`, and full verification evidence `6496995`. Focused test 1/1, Lab suite 109/109, four scoped Worker runs passed as harness runs, and `pwsh -NoProfile -File init.ps1 -Mode full` exits 0; the packages remain native-load failures. Latest shard data checkpoint is `aca333e`, active queue checkpoint is `6ac35b4`, and progress/feature checkpoint is `b199b84` and current progress verification is `f61127a`; compatibility guide is `f657744` and plan checkpoint is `aecf6b4`. The 18:49 verification passed docs mode (18 documents/229 links), README renderer checks, JSON parse, diff check, and harness validation 100/100.
- Continue the fresh 512-shard rerun against the same frozen snapshot and cohort. At each checkpoint, first confirm the runner/log/SQLite state; then run `python .work/catalog-census/refresh_s211_checkpoint.py`, inspect its canonical-report validation output, and regenerate the README with `node scripts/render-readme.mjs`. Retain original census, failure ledger, attempts and all rerun shard hashes. Do not describe the 5,621 behavior-unmapped entries, App Control unknowns, or failed native loads as passes.
- After all 512 reports are terminal and verified, aggregate only those canonical reports, refresh the S2-11 checkpoint and generated README, and separately adjudicate census, automation, behavior, and field gates. Review the observed native-success/federation-failure set alongside the fixture/loader coverage limitation.
- Once S2-11 exit criteria are met, complete S2-12 audit, README, compatibility matrix, evidence index, and handoff. Keep the task active until required validation and separate documentation commits are complete.

