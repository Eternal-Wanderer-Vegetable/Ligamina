# 会话交接

## Current Objective

- 日期：2026-10-26（Asia/Shanghai）。继续 feat-019 / P3-05。Linux affected campaign `p3-recovery-linux-842f71712e2c` 已完成 2,206/2,206；最终报告与同一 ext4 queue 对账通过，39 个 unit unknown 仍保留，行为兼容仍 `not_evaluated`。Ubuntu-24.04 已按用户授权完成重启。
- P3-04 已对独立 PSF 签名 CPython 3.12.10 Worker profile 准入；原 catalog profile lock 和 CPython 3.12.14 blocker 不改写。P3-05 Windows 独立 pilot 使用 profile lock v3 和 Node 24.20.0，20 个冻结样本已在 Windows Sandbox 串行闭合。
- Windows pilot `p3-recovery-windows-pilot-v2-9666ebb4609f`：manifest canonical SHA `0796fad72d14f0e84cfd91b77e63177873a26510dfa3adee55c9a3298feed4fa`；报告 20/20 settled，failed=2、skipped=8、unknown=10、passed=0，cleanup confirmed=20、cleanup unconfirmed=0。报告文件 SHA `5e1f02f68e65faecf8048a5fc722cf613dda33b55792ae1e0e09a6a35d95d976`；最后 checkpoint SHA `b3ea7bc0e010168769c394bf1f16f4f7f86a586c4e4c4b2a020c111d95325406`。
- 10 个 NoneBot pilot 结果仍原样冻结为 unknown。v1/v2 native-only 诊断在非 Evaluation policy 身份下报告该锁定 .pyd WinError 4551；v2 3099 记录为 base {0283ac0f-fff1-49ae-ada1-8a933130cad6} Options 0xb18c2714、supplemental {1678656c-05ef-481f-bc5b-ebd8c991502d} Options 0xd1882304（两者 Audit Mode bit 16 均清除）。新增 v4 在同一扩展上 import/LoadLibrary 成功，guest EVTX 18×3099，未见 3076/3077/3089；其 Evaluation base/supplemental GUID 分别 {1283ac0f-fff1-49ae-ada1-8a933130cad6} / {2678656c-05ef-481f-bc5b-ebd8c991502d}，Options 0xb18d2714 / 0xd1892304（Audit Mode bit 16 置位），另有一条 enforcing Endpoint Security policy。报告/EVTX 已纳入 outputs/acceptance/phase3-p305-windows-native-dll-diagnostic-v4-20261025.*；哈希见计划。两组观察对应不同 policy identity/options，但 Sandbox policy 切换原因和 v2 精确拦截 policy 仍未确定。v4 source hash 无对应源文件可核、报告时间字段有内部不一致；使用原始 EVTX/hash 作证，不能用此 v4 成功结果作为 enforcing-mode 接受。
- 10 个 Koishi 样本结果为 2 failed、8 skipped；两项失败具体是禁用 lifecycle scripts 后缺少 `sqlite3` native binding，以及空 provider profile 未提供 optional peer `@koishijs/plugin-console`；4 项因 required Host service 不可用而 skipped，另外 4 项 native load 正常但缺行为 fixture。20/20 Worker cleanup confirmed。失败/跳过归因不等于行为兼容裁决。
- Windows affected 总分母为 973（796 NoneBot +177 Koishi）；旧 pilot 闭合各 10 项后有 953 项待覆盖（786 NoneBot +167 Koishi）。新的 786 项 NoneBot Evaluation campaign 当前 251/786 settled：passed=0、failed=82、skipped=166、unknown=3、remaining=535、cleanup confirmed=248、cleanup unconfirmed=0；Koishi 167 项尚未开始。P3-05 保持 in-progress，P3-08/P3-10 尚未满足依赖，behavior compatibility 为 not_evaluated。不得运行 init.ps1 -Mode full、Windows full smoke 或宿主机 catalog 插件。
- 当前活动 campaign（2026-10-25T16:21:30Z observation）：用户已选用 Windows Sandbox 默认 Evaluation profile。campaign p3-recovery-windows-nonebot-evaluation-20261025 以独立 manifest canonical SHA 6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914 冻结 786 个未与旧 pilot 重叠的 NoneBot 项，profile lock SHA 31814e498cab1365fa201497faa9b3918316db701d8f503c212f32d20e9a286a，P3-04 admission SHA 3a1a1d5755df58ed8097a58b924cb683b278f475102c11be5de57fad3fca7d70。Checkpoint SHA 4aed00a3c7f57fcb44538005e57081e6066725718e96b577097625451d563736 记录 251/786 settled：passed=0、failed=82、skipped=166、unknown=3、remaining=535、cleanup confirmed=248、cleanup unconfirmed=0；status=in-progress、health=blocked、behavior=not_evaluated。唯一 controller PID 33984 仍运行；Sandbox d9bcf919-1679-4b3e-970f-fc0fe2b03230 正运行，controller 的 wsb.exe exec PID 19476 活动。 execution report 尚未生成。82 个失败均为 native_load_failed，166 个跳过均为 missing_entry_bound_fixture。3 个 unknown 均为 worker_preparation_failed：PyPI 元数据 TLS unexpected EOF，分别缺少 ayafileio==2.0.0、brotli==1.2.1、pyside6-pdf==6.12.0.140；插件未执行，unknown 保持不变。 当前 checkpoint 未显示 Sandbox 权限类错误。Evaluation policy 的 Audit Mode 仅代表该 Sandbox profile 下的加载证据，不能替代强制模式验收；未修改 App Control policy，也未运行 Windows full smoke 或宿主机插件。
- 本轮定向验证：v4 report SHA 与 sidecar 一致；raw EVTX SHA 与 JSON 内绑定值一致，Get-WinEvent -Path 解析为 18×3099；v2/v4 policy option bit 16/10 已按 Microsoft 3099 options 定义复算；本轮 docs/harness 验证结果见验证记录。未运行测试套件、Mode full、Windows smoke 或 catalog plugin。

## Resume Commands

- 查看进度与 hash：读取 `outputs/acceptance/phase3-p305-windows-pilot-v2-checkpoint-20261025.json` 及同名 `.sha256`，并核对 `outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json` 的 canonical SHA。
- 原 pilot 已闭合，不得重启或重复派发；脚本有 campaign 互斥锁和 closed-report idempotency guard。其正式 manifest 是 `outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json`（文件 SHA `c28dcdddc035df73841e42a811abd4b4fd4c89c6fd795b783ea0214dcf9f21d5`）。
- Koishi offline cache audit 已确认剩余 167 个条目的 167/167 profile lock、3,255/3,255 npm registry archive 和 167/167 plugin artifact 都匹配冻结 hash/SRI。脚本 [cache audit](scripts/phase3-windows-koishi-cache-audit.py)，报告 [report](outputs/acceptance/phase3-p305-windows-koishi-cache-audit-20261025.json)，SHA-256 `24130cb65f49f0aca2507f999d15e2c4621fe2eb115b32ddaa37139fa8a366fb`。此前缺失的 21 个 registry npm blobs 已在外部 cache 按 lock URL 下载并逐个校验；无插件执行。尚有两行锁定到 Worker allowlist 不接受的 Git SSH commit；另有 113 个 lifecycle-script package occurrences，`sqlite3` 安装脚本保持禁用时需要单独冻结 prebuilt binding/安装方案。新 Koishi Host profile 也须冻结需要的 provider，旧 pilot 的 `@koishijs/plugin-console` 缺失问题不回写。
- 本轮验证：v4 JSON/EVTX SHA 及 pilot identity 绑定核验；原始 EVTX 为 18×3099；PowerShell 采集脚本语法解析通过但未运行。宿主 build 26200、Sandbox package 0.8.107.0，宿主 registry SAC state=0，host CiTool 返回 0x80070005。初次 Sandbox 列表为空，随后发现活动 ID 3323b88e-1f8c-438f-8b73-c56f85597af8；来源不明，本轮未连接/执行/停止。docs 检查通过（19 documents / 327 local links / 11 indexed historical Markdown / 24 features）；harness validation 100/100；git diff --check 通过。无测试、Mode full、Windows smoke 或 catalog plugin。
- 策略解释与当前决定：v4 的 Evaluation GUID 对应 Smart App Control Evaluation；该 guest 的 Evaluation base/supplemental 设置 Audit Mode，但同时存在其他 enforcing policy。用户已接受默认 Evaluation profile 作为本次隔离加载 campaign 基线，因此按已冻结的 windows-sandbox-default-sac-evaluation-v1 继续；结论不扩展到强制模式。此前任务 Sandbox 3323b88e-1f8c-438f-8b73-c56f85597af8 已由用户确认归属并授权关闭，之后专用 preflight Sandbox 已停止。不得生成/部署 Hash Allow、修改 App Control policy 或把成功加载解释成 enforcement 验收。
- 执行器为 scripts/phase3-windows-pilot.py；当前 checkpoint 为 in-progress，最终报告尚未生成。唯一 controller 为 PID 33984，campaign command 使用新的 Evaluation manifest/profile lock/checkpoint/report；若 controller 意外退出，先核验 checkpoint SHA/manifest/profile，再用同一参数恢复，不能启动重复 controller或重跑终态样本。
- 本轮未重启 WSL：整体 P3-05 尚未完成 Windows affected scope。完成前不要重启；届时先核对所有 WSL/相关 Worker 进程，避免终止其他任务。

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
- 当前 Windows execution：使用上方的独立 Evaluation manifest/profile lock/checkpoint/report 路径与 SHA；最终报告尚未落盘。先检查 controller、wsb.exe list、checkpoint 与 hash，再继续或在 controller 意外退出后用相同参数/同一 checkpoint 恢复；不得启动重复 controller 或重跑终态样本。
- 旧 Windows pilot 固定为 20/20 terminal（passed=0、failed=2、skipped=8、unknown=10、cleanup=20），不重跑；NoneBot 新 campaign 当前 251/786 settled：passed=0、failed=82、skipped=166、unknown=3、remaining=535、cleanup confirmed=248、cleanup unconfirmed=0，535 项尚未有终态；旧 pilot 之后总计还有 167 项 Koishi 尚未开始。
- 新 NoneBot manifest canonical SHA 6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914，profile lock SHA 31814e498cab1365fa201497faa9b3918316db701d8f503c212f32d20e9a286a，checkpoint 文件 outputs/acceptance/phase3-p305-windows-nonebot-evaluation-checkpoint-20261025.json。其 profile 锁定为 CPython 3.12.10/Windows x86-64 并绑定 P3-04 admission 和 Evaluation policy preflight。无 plugin fixture 的项保持 skipped；native load failures 保持 failed；无证据时不得改成 pass。Koishi 仍需独立完整 Host/native profile 和冻结 manifest。
- 每 15 分钟中文汇报仍是用户偏好；截至 2026-10-25T16:21:30Z，campaign checkpoint SHA 4aed00a3c7f57fcb44538005e57081e6066725718e96b577097625451d563736，settled 251/786、passed=0、failed=82、skipped=166、unknown=3、cleanup confirmed=248、cleanup_unconfirmed=0、remaining=535；唯一 controller PID 33984 仍运行；Sandbox d9bcf919-1679-4b3e-970f-fc0fe2b03230 正运行，controller 的 wsb.exe exec PID 19476 活动。 execution report 尚未生成。三个 worker-preparation unknown 未运行插件，原因为 PyPI metadata TLS unexpected EOF；checkpoint sidecar 与本体 SHA 匹配，冻结身份未变。继续只监视同一 campaign，不重复派发；当前 checkpoint 未显示 Sandbox 权限错误，不触发本机 fallback。init.ps1 -Mode full、Windows full smoke、宿主机 catalog 插件执行均不在范围内。
- 完成 P3-05 整体前不要按旧 Linux completion 指令再次重启 WSL；届时先核对所有 WSL/相关 Worker 进程，避免终止其他任务。
