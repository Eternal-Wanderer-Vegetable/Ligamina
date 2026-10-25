# 当前进展

## Current State

- **Last Updated:** 2026-10-25（Asia/Shanghai）。P3-05 semantics-v3 20 项 Linux pilot 已闭合；2,206 个 Linux affected 输入 SHA/长度审计均可用，全量 campaign manifest 已冻结且真实 WSL Docker 执行正在进行。
- **Current Objective:** 按 [第三阶段方案](docs/plans/2026-10-20-phase-3-automation.md) 保持 OS 与 gate 独立。P3-04 仅对 PSF 签名 CPython 3.12.10 profile admitted；原 uv CPython 3.12.14 仍被 App Control Win32 4551 阻止，且 Windows catalog 插件未运行。P3-05 从 blocked 转为 in-progress：v3 pilot 20/20 terminal、cleanup 20/20、11 recovered、9 plugin/stage failed、0 unknown；全量 2,206 项 campaign 执行中。P3-08/P3-10 仍等待 P3-05；行为兼容保持 not_evaluated。
- **P3-04 Windows signed runtime:** 分享记录中的 Codex shell sandbox 设置不影响项目 Worker 的 Windows Sandbox CLI。uv 的 CPython 3.12.14 未签名并被拦；官方 PSF 签名 CPython 3.12.10 在实际 Sandbox 低权限进程中启动成功，子解释器和清理通过。CPU hard cap（0.25 core，观测进程 CPU/wall=0.243）、active process limit（4 个进程，下一进程被拒）、Job Object memory limit（256 MiB；单独将 Sandbox VM 设为 2,048 MiB 验证）均得到实测。原有 Sandbox benign suite 7/7 通过。新报告：[admission](outputs/acceptance/phase3-p304-windows-admission-python31210-20261025.json)；未运行目录插件，现有 Windows catalog profile lock 仍指向 3.12.14，所以插件 claim 不开放。
- **P3-05 Linux cache fix:** lab/artifacts.py::_koishi_entry 原把 npm dist.unpackedSize 写进压缩 tarball size_bytes。实现已分离 metadata 解包大小与实测归档长度，旧 artifact lock 和历史 campaign 不改写。全量 P3-01 Linux affected set 审计显示 2,206/2,206 artifact SHA 匹配、2,206/2,206 dependency-lock SHA 与长度匹配；414 个 NoneBot artifact 的旧 metadata size 与实测相同，Koishi 归档长度由 semantics-v3 manifest 按原 SHA 冻结 override。
- **P3-05 Linux execution:** WSL2 Docker 29.1.3 新准入 probes 10/10 通过。旧 semantics-v2 campaign p3-recovery-linux-803a2bc56205 保持其 2 个 unknown 历史结论不变。新 semantics v3 保留 CommonJS inject 并在插件回调前预检 Host services。20-unit v3 pilot p3-recovery-linux-b9518c95b224 完成 20/20 units、60/60 required stage positions；11 recovered、9 plugin/stage failed、0 unknown、0 not_run；cleanup 20/20；infrastructure blocker false，health passed，plugin compatibility not_evaluated。全量 P3-01 Linux affected set 2,206 项以 manifest p3-recovery-linux-842f71712e2c 冻结，当前真实 Docker campaign 正运行。每个 Koishi entry 绑定明确的 core-only 空 provider profile；缺 Host service 是依赖归因，不代表插件兼容。
- **P3-05 semantics-v3 tests:** 本机 CPython 3.14.7 + Node focused suite 21/21 passed；报告 [focused tests](outputs/acceptance/phase3-p305-affected-manifest-focused-tests-20261025.json) 绑定完整日志 SHA-256。覆盖 v3 Host profile 身份/篡改拒绝和 2,206 项 affected-linux selection。
- **P3 docs gate:** 本轮 pwsh -NoProfile -File init.ps1 -Mode docs 通过：19 篇文档、296 个本地链接、11 篇历史 Markdown 索引、24 项 feature；报告 [docs check](outputs/acceptance/phase3-docs-check-p305-affected-v3-20261025.json) 绑定日志 SHA-256。
- **P3-08 / P3-10:** P3-08 仍依赖 P3-04 与 P3-05，不能开始 120 个双路径行为位置或三次干净复跑。P3-10 等待 P3-05/P3-08；尚无综合 gate 结论。
- **Lab suites:** Linux `unittest discover -s lab/tests -v` 154/154 passed；Windows 锁定 Python 同命令 154 tests passed、1 个 Linux-only case skipped。此次全量复跑包含 malformed Worker probe 投影回归。报告：[Linux](outputs/acceptance/phase3-linux-lab-tests-final2-20261025.json) 与 [Windows](outputs/acceptance/phase3-windows-lab-tests-final2-20261025.json)，日志摘要哈希写入报告。未运行 `init.ps1 -Mode full`。
- **Hosted CI / runner:** 修复前旧 SHA `67329097b6d3276b1ceff1504874fd756327091e` 的常规 CI run [40142268169](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40142268169) 在 Windows lab suite 失败；修复提交 `70f25cf9948dc16b03968d9f93bf849a2a899f48` 上，Phase 3 Linux self-test [40148262337](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40148262337) 成功，常规 CI [40148262483](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40148262483) 的 Linux 和 Windows jobs 均成功。该 hosted CI 对应本次代码修复；当前 runner 配置检查仍显示 self-hosted runner 数为 0，受控 runner variable/Environment 未确认。
- **Documentation / harness:** `pwsh -NoProfile -File init.ps1 -Mode docs` passed (19 docs / 298 local links / 11 indexed historical Markdown / 24 features); [report](outputs/acceptance/phase3-docs-check-final7-20261025.json) and [log](outputs/acceptance/phase3-docs-check-final7-20261025.log). `node --test scripts/evidence-hash.test.mjs` passed 4/4; harness validation scored 100/100 with no bottleneck; `git diff --check` passed. See [evidence-hash](outputs/acceptance/phase3-evidence-hash-tests-final-20261025.json) and [harness](outputs/acceptance/phase3-harness-validation-final-20261025.json). `git fetch origin` left main/origin/main 0/0 before the CI status follow-up commit.

## Previous Stage Evidence (through 2026-10-20)

- **P3-01:** [恢复 manifest](outputs/acceptance/phase3-p301-recovery-manifest-20261020.json) 从 S2-12 audit、S2-11 归因和 512 个 shard 重新计算 11,380 个单位，选择 3,179 个受影响单位（19,074 stage rows），另外记录 1,048 个静态 artifact/profile skipped。manifest SHA-256 `f084778b6cfee271876ba6493e039fc2a288e0295a7a0fe9353f0d6f2074416e`。这是选择账本，不是重跑结果。
- **P3-05 Linux checkpoint:** frozen 20-unit pilot 与新 fenced campaign 已建立；artifact/profile-lock cache ready 0/20。1 个 install attempt 在 Worker 启动前因缓存缺失变为 unknown，精确 Docker reconciliation cleanup confirmed；19 个单元未执行，无插件代码运行。checkpoint [报告](outputs/acceptance/phase3-p305-linux-pilot-checkpoint-20261020.json)，SHA-256 `5a35b12ffd323068ebe7152a1fb2a40ed7197f625e2c97a180e5762e082a551e`。Linux scoped P3-05 保持 blocked；P3-07 已完成；当前无依赖满足且符合跳过 Windows 范围的下一项。
- **P3-07 Linux:** generic entry-bound Native/Federated executor enforces isolated workspaces, independent fixture assertions, frozen identity binding, cleanup evidence, stock-native and Core/Host route proof. Four history regressions passed through real Linux Docker Workers with no new failures. Report: [phase3-p307-linux-legacy-20261020.json](outputs/acceptance/phase3-p307-linux-legacy-20261020.json), SHA-256 `d418bd3ee8a7ad6b5f05c02adc61098a0ed50bd0076a465cbaa5498d3dbda058`. Catalog behavior cases remain unexecuted; P3-BEHAVIOR stays not evaluated. P3-09 is complete; P3-08 waits on P3-04 and P3-05.
- **P3-09 Linux CI:** `df0705e` 新增 hosted PR/main/weekly self-test 与受 guard 的手动专用 runner。干净 archive checkout 报告 [self-test](outputs/acceptance/phase3-p309-linux-self-test-clean-checkout-20261020.json) 与日志均被保存，Node 8/8、Linux 自动化回归 54/54、docs 19/278、behavior suite validator 通过；Windows tests 与目录插件执行均未运行。受控 runner 注入退出码 7 的 [trap report](outputs/acceptance/phase3-p309-linux-controlled-trap-check-20261020.json) 与 SIGTERM [interruption report](outputs/acceptance/phase3-p309-linux-controlled-interrupt-check-20261020.json) 验证失败/中断报告和 log hash 可保留。GitHub hosted/self-hosted 真实运行尚未配置或执行。P3-08 仍依赖未做的 Windows admission 和 blocked P3-05；P3-10 暂无准入。
- **P3-02 initial blocker:** 初次健康准入因缺 Docker CLI/daemon 受阻，报告 [保留](outputs/acceptance/phase3-p302-linux-admission-20261020.json)；之后安装 Docker Engine、运行锁定镜像的真实 benign probes 并通过 Linux admission。原 blocker 报告不覆盖重试报告。
- **Historical S2 status:** S2-01–S2-12 已完成；P2-AUTOMATION blocked、P2-BEHAVIOR not evaluated、P2-FIELD not run。历史 CI 修复与四个精确支持版本结论保持原状。
- **CI Diagnosis:** PR #3 当前 HEAD `b0615548` 的 [run #18](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/38988173249) 与前次 [run #17](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/38915036215) 均在 Linux/Windows 的 `node scripts/check-docs.mjs` 失败，错误为 `Stale phase-two report input: outputs/acceptance/phase2-s207-cli-smoke-20261012.json`。S2-08 保存的 SHA-256 是 Windows CRLF 工作树字节 `35ba6754…`，Git blob 为 LF 字节 `2a5efbd5…`；renderer 对原始文件字节做哈希，导致 clean checkout 不匹配。本地 docs 检查通过，`git archive HEAD` 临时干净 checkout 稳定复现失败。临时诊断还发现 9 个 S2-09 source hash、1 个 S2-10 全局 source hash 和全部 512 个 S2-10 shard 对同一 Windows 写入证据保存了不匹配哈希。两平台 CI 都在第一道 docs 检查停止，后续测试没有运行。修复已提交推送，当前双平台 CI 复跑通过。
- **Historical S2 execution:** S2-11 full rerun completed at 2026-10-17 01:35:39 Asia/Shanghai: 512/512 canonical reports, 11,380/11,380 units and 68,280/68,280 stage jobs; no active, queued or leased work; invalid reports=0. Unit outcomes: 0 passed, 880 failed, 7,530 skipped, 2,970 unknown. SQLite stage-job outcomes: 24,914 passed, 880 failed, 37,357 skipped, 5,129 unknown; job pass is not plugin compatibility. Audit linked the 2,158 Linux cleanup unknowns to a Docker named-pipe API outage, 796 Windows NoneBot unknowns to App Control Win32 4551, and 16 other worker/preparation/timeout unknowns. P2-CATALOG/P2-CENSUS pass; P2-AUTOMATION remains blocked, P2-BEHAVIOR not evaluated, P2-FIELD not run. No new catalog support claims are made. See [machine audit](outputs/acceptance/phase2-s212-audit-20261017.json) and [compatibility matrix](outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md).
- **CI Fix:** `scripts/evidence-hash.mjs` 对有效 UTF-8 文本接受原始、LF、CRLF 三种等价表示的 SHA-256；renderer 的 S2-08 输入、S2-09/S2-10 source inputs、S2-10/S2-11 shard 文件和 S2-12 checkpoint 均使用该校验，历史原始哈希未改。`scripts/evidence-hash.test.mjs` 覆盖复现报告、双向换行兼容、内容变更拒绝与无效 UTF-8 拒绝，并已接入双平台 check 入口。变更提交为 `6161346`。GitHub Actions [run 38989675623](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/38989675623) attempt 2 的 Windows/Linux checks 均通过；attempt 1 Windows 曾有一次 Rust HostSession 关闭断言失败，rerun 未复现，兼容性 gate 状态未变。
- **Completed stage commits:** H01 `2d6399a`、S2-01 `15f468d`、S2-02 `c0be568`、S2-04 `2fa06b0`、S2-03 `29bc1e5`、S2-05 `854b564`、S2-06 `4e555b3`、S2-07 `7523a83`、S2-08 `cbcbb0a`、S2-09 `20d8f04`；`299d22c` 是 S2-09 baseline/status checkpoint；S2-10 census checkpoints `8f21ead`、`4295831`、`25cf805`、`7ac3f1c`、`6f864bb`、`fb3920e`、`01988bc`、`ee758da`、`36768b5`、`aa25211`、`aa89dbc`、`25a0d41`、`fa9fea8`、`bcb1e73`、`12f6203`、`45ade15`、`75c750c`、`3455a07`、`95909ff`、`498f436`、`a81dc61`、`e35bf26`、`2e44298`、`8e8189d`、`c457043`、`5a89e0f`、`60bfba3`、`466f9ec`、`1ed38d8`、`ac56cf7`、`f295313`、`f625b48`、`e2debcb`、`1455b31`、`f9c2c6b`、`c479f49`、`6356360`、`8a32e8e`、`6c1818f`、`31709fa`、`d071238`、`01d412e`、`5f7be0c`、`ecf4ebe`、`4047c76`、`f41c2f2`、`21d1336`、`0d56ba1`、`4c974f3`、`e491a3e`、`35d8428`、`b6c9237`、`2627109`、`da1322c`、`96350a3`、`084c015`、`1f1c3ae`、`3161907`、`0dfcaef`、`3f9c6ac`、`8b55df4`、`141cfee`、`4b044c7`、`9461053`、`cdb6a10`、`fe21968`、`9d401d6`、`eff6790`、`5213dac`、`1e15788`、`1bb005d`、`92b3f47`；S2-11 data checkpoints `550fd88`、`5afa75f`、`a066008`、`319f879`、`da6c08f`、`02841d4`、`382c6e1`、`2488b2f`、`f563c52`、`a975ad9`、`ee41709`、`75c0ac4`、`74df51a`、`30317cb`、`d454e96`、`395e17f`、`419367b`、`9f14b9d`、`a14b94e`、`32ba84d`、`9468348`、`2bec3a9`、`61d71d3`；S2-11 shard data checkpoints `f3de04d`、`aa28b73`、`48b511e`、`396769c`；active queue snapshots `6440690`、`a6a1b7d`；S2-11 shard data checkpoints `fa48254` and `7edc9c2`。 S2-11 shard data checkpoint `28e9e85`。 S2-11 shard data checkpoints `d48a41a`、`d58f27f`、`9b6a9b3`、`3c65326`、`e9d90a2`、`207cb30`、`2786eaa`、`005bffc`、`72c6403`。 S2-11 shard data checkpoint `9fc0f0b`。 S2-11 shard data checkpoint `a30ef7d`。 S2-11 shard data checkpoint `ba82d3d`。 S2-11 shard data checkpoint `6906bb6`。 S2-11 shard data checkpoint `e7ad3d7`。 S2-11 shard data checkpoints `675e997`、`f92f49e`、`e0bce86`、`b14321a`、`cd3fa36`、`ed8c5ae`、`7a4e6de`、`cae1af6`、`d078961`、`7f5b7f0`、`ff07b65`、`19d4130`；S2-12 renderer/terminal-tracker `d4b35d7`、data/audit report `7f81c70`。

## What's Done

- 已建立短 AGENTS 入口、六个专题指南、文档导航、状态文件和 S2-01–S2-12 实施计划。
- [S2-01 冻结目录](catalogs/p2-41649744e057d61031d5/catalog.json)：NoneBot 943 + Koishi 4,747，共 5,690 registry entries；来源/原始字节/哈希/provenance/官方维护子集可复核。
- [S2-02 输出合同](outputs/acceptance/phase2-contracts-20261012.json)：四个 v1 schemas、9 个共享 golden vectors、状态转换和分母守恒验证。
- [S2-04 Worker 验收](outputs/acceptance/phase2-workers-20261012.json)：Linux Docker 10 项、Windows Sandbox 7 项 benign probes 均通过；WSL user/mount namespace 因只读 bind 可被重挂可写而拒绝。
- [S2-03 发布物与 profile 锁](outputs/acceptance/phase2-s203-20261012.json)：5,690 entries、6,247 artifact references、11,380 Windows/Linux profile 单元；10,332 个完整锁，其余显式终态。没有执行目录插件或 sdist build backend。
- [S2-05 独立原生基线](outputs/acceptance/phase2-s205-20261012.json)：四个既有支持版本的 Native/Federated 双路径及独立 oracle 均通过，cleanup 确认；Native 不导入 Ligamina Bridge。Windows Sandbox App Control 在插件启动前阻止 CPython（Win32 4551），本阶段插件探针在禁网 Linux Docker Worker 执行。
- [S2-06 recipes](outputs/acceptance/phase2-s206-20261012.json)：NoneBot/Koishi 各 10 类 schema-validated recipes；绑定 entry、发布物哈希、profile、配置、服务与 fixture，独立 oracle 双路对照。全仓库验证通过（lab 60、NoneBot 3、Koishi 7、MCP 1、reference 5、Rust 29、Windows smoke 清理 0 PID）。冻结目录 dry run 仅为 69/5,690 条 entry 建议了行为分类，其余 5,621 明确留为 unmapped_category；本阶段未执行第三方插件。
- [S2-07 调度与恢复](outputs/acceptance/phase2-s207-20261012.json)：六阶段队列 DAG、SQLite WAL/同步持久化、租约、attempt/journal、幂等派发与稳定分片。重开/过期 lease 在六阶段逐一模拟，unknown 与 attempt artifact 引用保留；真实快照 CLI 为 Linux 分片计划 3 个单元/18 个作业并成功恢复同一 manifest。没有派发 catalog 插件 Worker。

## S2-05 Verification

- 实施前 baseline：`pwsh -NoProfile -File init.ps1 -Mode full`，退出码 0；原 smoke 报告摘要和哈希保存在 [baseline 证据](outputs/acceptance/phase2-s205-baseline-smoke-windows-20261012.json)。
- 四样本最终配对命令：`uv run --locked --project lab --python 3.12.14 python -m lab.baselines.run_s205`；NoneBot autohelp 0.1.7、repeater 3.1.4、Koishi game-mini 0.4.3、multi-qa 1.0.8 均通过 Native、Federated、oracle、pair comparison 和 Worker cleanup。
- `uv run --locked --project lab --python 3.12.14 python -m unittest discover -s lab/tests`：52 项通过；包括 wrong-recipient 单边错误、双方同错仍失败的负例。
- `pwsh -NoProfile -File init.ps1 -Mode docs`：18 文档、152 本地链接、9 历史 Markdown、14 任务通过。
- Python `py_compile`、Koishi runner `node --check`、`git diff --check` 通过。
- 实施后 `pwsh -NoProfile -File init.ps1 -Mode full`：退出码 0；NoneBot 3、lab 52、Koishi 7、MCP 1、reference client 5、Rust workspace 29 均通过；Windows Core smoke 4 events / 4 gateway ops / 0 残留 PID，bundle stage 通过。
- 四样本 Native/Federated 运行只覆盖 Linux。没有执行全目录 census、Windows 插件 pilot、真实 QQ/平台投递或第一阶段 24 小时实验。

## S2-07 Verification

- 实施前 baseline 在 S2-06 HEAD `4e555b3` 运行 `pwsh -NoProfile -File init.ps1 -Mode full`，退出码 0；见 [baseline](outputs/acceptance/phase2-s207-baseline-20261012.json)。
- `uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_queue lab.tests.test_catalog_run -v`：16 项通过，覆盖六阶段 interruption/reopen、过期 lease、attempt/artifact 引用、依赖/cleanup、并发 claim、幂等、分片和输入锁变化。
- `pwsh -NoProfile -File init.ps1 -Mode full`：退出码 0；docs 18/156 本地链接、NoneBot 3、lab 76、Koishi 7、MCP 1、reference 5、Rust 29；Windows Core smoke 4 events / 4 Gateway ops / 0 PID；bundle stage 通过。生成的 `ci-smoke-windows.json` 已恢复为仓库版本。
- 真实快照 `p2-41649744e057d61031d5` CLI plan/resume 报告：[初始计划](outputs/acceptance/phase2-s207-cli-smoke-20261012.json)、[恢复结果](outputs/acceptance/phase2-s207-cli-resume-20261012.json)。恢复前后 manifest SHA-256 相同，SQLite 保存 18 个 queued stage jobs；`execution_started=false`。
- CLI 每次做原始输入长度/hash、派生 snapshot ID、冻结 catalog/changes 文档哈希、entry denominator 和锁交叉校验；完整 raw→normalized 语义校验由 S2-01 快照验收覆盖，没有在每个 shard 重复执行。

## S2-08 Baseline

- 在干净 HEAD `16fdf63d707211622c8045ff96fc092b7391ab12`、无 S2-08 实现改动时运行 `pwsh -NoProfile -File init.ps1 -Mode full`，退出码 0。docs 18/156、NoneBot 3、lab 76、Koishi 7、MCP 1、reference 5、Rust 29；Windows Core smoke 4 events / 4 Gateway ops / 0 PID，bundle stage 通过。详见 [S2-08 baseline](outputs/acceptance/phase2-s208-baseline-20261012.json)。

## S2-08 Verification

- 报告 schema 要求唯一 `expected_result_ids`、所选 gate、gate 结论和退出码；Python/Rust 会校验 schema 并从终态 results 重算 summary。completion 仅要求 run 结束且账目闭合；automation 拒绝 unknown/未调度及未解决 harness/infrastructure 失败；compatibility 要求全通过。中断返回 3，输入/schema 错误返回 2。
- `uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_report lab.tests.test_result_contract -v`：18 项通过，覆盖三种 gate、部分分母、completed 与兼容分离、未知状态、summary 篡改、JSON/text/NDJSON、脱敏、日志长度和 queued catalog plan。
- `cargo test -p ligamina-cli -p ligamina-contracts --locked`：CLI 5 项、contracts 4 项通过；`cargo fmt --all -- --check` 通过。Python 与 Rust CLI 使用共享 valid/invalid v1 fixtures 验证 NDJSON 与 0/2 退出码；两 CLI 对 S2-07 队列计划都明确显示 18 个 job queued 且未执行插件。
- `node scripts/render-readme.mjs --check` 通过；临时篡改 phase-two 输入报告哈希和 README 生成区均被拒绝（退出码 1），恢复原字节后检查通过。`pwsh -NoProfile -File init.ps1 -Mode docs`：18 文档、159 链接通过。
- 全仓库 `pwsh -NoProfile -File init.ps1 -Mode full` 退出码 0：docs 18/161；NoneBot 3、lab 85、Koishi 7、MCP 1、reference 5、Rust workspace 33；Windows Core smoke 4 events / 4 gateway ops / 0 PID，bundle stage 通过。生成的 `outputs/acceptance/ci-smoke-windows.json` 已恢复；完整结果见 [S2-08 acceptance](outputs/acceptance/phase2-s208-20261012.json)。
- 没有派发第三方插件。S2-07 实际队列仍为 18 queued；5,621 个 entry 缺少保守 recipe 分类建议。Windows Sandbox CPython 启动受 App Control（Win32 4551）阻止；真实平台登录/投递未验证。

## S2-09 Baseline

- 在提交 `cbcbb0a2639bcf9c524847c8778bc87b36b80813` 的干净工作树上运行 `pwsh -NoProfile -File init.ps1 -Mode full`，退出码 0：docs 18/162、NoneBot 3、lab 85、Koishi 7、MCP 1、reference 5、Rust 33；Windows Core smoke 4 events / 4 gateway ops / 0 PID，bundle stage 通过。见 [S2-09 baseline](outputs/acceptance/phase2-s209-baseline-20261012.json)。生成的 `ci-smoke-windows.json` 已恢复。
- 这是 S2-09 实施前基线；结果仅对应提交 `cbcbb0a`。

## S2-09 Pilot Progress

- [pilot manifest](outputs/acceptance/phase2-s209-pilot-manifest-20261012.json) 固定 NoneBot/Koishi 各 30 个 entry + 四个历史支持版本，并保留 11,380 条目录 entry×OS 分配行；双 OS 试点分母 128。
- [Linux 历史版本配对](outputs/acceptance/phase2-s209-legacy-linux-20261012.json)：四个 Native/Federated 独立 oracle、比较、cleanup 均通过。每个 Worker 峰值内存 246–272 MB、只读输入 190–210 MB、耗时 17.578–23.625 秒；S2-05 原报告 hash 未变。首次复跑因 `lab/recipes.py` 未被复制进 worker 导致失败，见 [attempt 1](outputs/acceptance/phase2-s209-legacy-linux-attempt1-20261012.json)；补入 module closure 后四项通过。
- [NoneBot Linux pilot](outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json)：30/30 终态，21 个 Worker 安装与清理通过；3 个 native load 因冻结的 `nonebot_plugin_htmlrender` profile 缺少插件依赖的导出符号而失败，2 个缺配置/凭证情形已分类为 skipped。依赖下载共 24,957,125 bytes；Worker 峰值内存 87–249 MB、scratch 61–160 MB。
- [Koishi Linux pilot](outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json) 与 [Windows pilot](outputs/acceptance/phase2-s209-koishi-windows-pilot-20261012.json)：各 30/30 终态、各 22 个 Worker 安装和清理通过；每平台 4 个 native load failed、26 个 skipped。Windows 清理未知的 `random-chooser` 经独立重试确认清理并改为 `missing_entry_bound_fixture` skipped，初次报告与 retry 均保留。
- [汇总 acceptance](outputs/acceptance/phase2-s209-20261012.json)：128 个结果位置闭合为 4 passed、11 failed、88 skipped、25 unknown。每生态/OS 的 30 条 catalog 记录均有终态；NoneBot Windows 仍有 23 个 CPython App Control Win32 4551 unknown，另有 2 个历史 Windows 配对不适用/未执行而为 unknown。目录行为 fixture 尚未派发，不能据此宣称行为兼容。
- [失败复核](outputs/acceptance/phase2-s209-failure-review-20261012.json) 记录 11 个 native-load 错误、阶段/平台和源报告 hash。NoneBot Linux scratch P95 为 102 MB；Koishi Linux scratch P95 34 MB、Docker RSS P95 250 MB；Koishi Windows Sandbox scratch P95 143 MB，Job Object peak commit P95 6.71 GB、最大 8.57 GB（8 GiB 限额内）。这些是隔离 pilot 样本，不是全量资源承诺。
- 预算仍 provisional：NoneBot Windows CPython 在启动前被 App Control Win32 4551 阻止，因此该生态/OS 没有 Worker 内存或 scratch 样本；5,690-entry census 总下载/磁盘/耗时尚未实测。配置文件保存各平台 pilot P50/P95、样本数和源报告 SHA-256。
- 汇总 acceptance 的 `pilot_run_status=completed`、分母 128/128；4 passed、11 failed、88 skipped、25 unknown。higher gates 仍分开：自动化受 Windows CPython blocker 阻塞，行为兼容性未评估，完整 census 未开始。
- `uv run --locked --project lab --python 3.12.14 python -m lab.pilot ...` 重建 acceptance；`lab.tests.test_pilot_nonebot` 覆盖 checkpoint resume 和 worker-lost 后先记 unknown 再以新 attempt 重试。negative Worker probes 与队列恢复证据见 acceptance 的 `negative_scenarios`。
- 最终验证：README renderer 与 `--check` 通过；`pwsh -NoProfile -File init.ps1 -Mode docs` 为 18 docs / 191 links。聚焦 S2-09 命令 `uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_pilot lab.tests.test_pilot_nonebot lab.tests.test_pilot_workers lab.tests.test_workers lab.tests.test_baselines -v` 为 25 项通过；完整 `pwsh -NoProfile -File init.ps1 -Mode full` 退出 0，lab 102、NoneBot 3、Koishi 7、MCP 1、reference 5、Rust workspace 全通过，Windows smoke 为 4 events / 4 gateway operations / 0 PIDs，bundle stage 成功。验证生成的 `ci-smoke-windows.json` 已恢复至原 SHA-256 `033D8EA2534603B880DB2A729B328C3A75C7F4CF0B8F8DEE087C22BEBBCAC752`；`git diff --check` 通过。

## S2-10 Implementation and Census Progress

- S2-09 clean HEAD `20d8f04db8c647da1f95336ca576966fccc15abb` 上的 S2-10 baseline 通过：`pwsh -NoProfile -File init.ps1 -Mode full` 退出码 0，docs 18/191、NoneBot 3、lab 102、Koishi 7、MCP 1、reference 5、Rust 33、Windows smoke 4 events / 4 gateway ops / 0 PIDs、bundle 通过。见 [baseline](outputs/acceptance/phase2-s210-baseline-20261012.json)。
- 已实现 SQLite 可恢复六阶段 shard dispatcher、per-entry evidence、跨 shard/per-plugin 汇总及 Koishi `platform=all` artifact 映射。实现后的 `Mode full` 退出码 0：docs 18/199、NoneBot 3、lab 106、Koishi 7、MCP 1、reference 5、Rust 33、Windows smoke 4/4/0、bundle 通过；10 个 focused catalog/census tests 通过；README 证据检查与 `git diff --check` 通过，详见 [implementation checkpoint](outputs/acceptance/phase2-s210-implementation-20261012.json)。
- 隔离 Linux smoke shard [报告](outputs/acceptance/phase2-s210-census-linux-shard0of256-smoke-20261012.json) 独立保存、不并入正式结果。正式 [aggregate](outputs/acceptance/phase2-s210-census-interim-20261012.json) 已关闭全部 11,380/11,380 个 entry×OS 单元和 512/512 个 canonical shards；Linux、Windows 均为 0–255/256，无缺失或重复，活动 runner：none。P2-CENSUS 通过。单元结果为 0 passed、1,235 failed、9,324 skipped、821 unknown；报告 SHA-256 `bef7b141d791a49eafa844b9f792afcb967511ef507a080520d163ab6069f6f2`，数据 checkpoint `f76c99d` 已提交。P2-AUTOMATION 待 S2-11 归因与修复后复核；P2-BEHAVIOR 未评估，P2-FIELD 未执行。SQLite 的 68,280 个阶段作业中有 822 个 unknown job；这是 job 分母，不能与 821 个 unknown unit 混用。README renderer 与 docs 模式验证已通过（18 文档、210 本地链接）。
- `ci-smoke-windows.json` 已恢复到原 SHA-256 `033D8EA2534603B880DB2A729B328C3A75C7F4CF0B8F8DEE087C22BEBBCAC752`。10,332 个 locked profile 仍只是执行候选；Windows NoneBot CPython 的 App Control Win32 4551 保持 unknown。

## Final Status

- **S2-01–S2-12:** 完成。S2-12 交付物包括机器审计、兼容矩阵、README 摘要、证据索引和逐阶段状态记录；本计划的执行与验收文档已按授权分阶段提交。
- **S2-11 full rerun:** 512/512 canonical shards、11,380/11,380 单元、68,280/68,280 阶段作业完成；无 active/queued/leased work，invalid reports=0。单元结果 0 passed / 880 failed / 7,530 skipped / 2,970 unknown。阶段作业结果另为 24,914 passed / 880 failed / 37,357 skipped / 5,129 unknown；job-level passed 不代表插件兼容通过。
- **Gate decisions:** P2-CATALOG、P2-CENSUS passed；P2-AUTOMATION blocked（2,158 个 Linux Docker API unavailable unknown、796 个 Windows App Control 4551 unknown、16 个其他 Worker/preparation/timeout unknown）；P2-BEHAVIOR not evaluated；P2-FIELD not run。目录层兼容通过为 0；未新增 catalog-wide supported plugin 声明，保留四个精确 T18 支持版本。
- **Evidence:** [S2-12 machine audit](outputs/acceptance/phase2-s212-audit-20261017.json)、[compatibility matrix](outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md)、[S2-11 final checkpoint](outputs/acceptance/phase2-s211-process-exit-regression-20261014.json)。Docker Desktop 29.8.2 在完整复跑后才可用；该检查不构成补跑，也不改写已有 unknown。
- **Final verification:** `python scripts/build-s212-audit.py` verified 512 reports and regenerated the audit/matrix; `pwsh -NoProfile -File init.ps1 -Mode docs`, `node scripts/render-readme.mjs --check`, `node --check scripts/render-readme.mjs`, `node --check scripts/check-docs.mjs`, harness validation (100/100), feature/audit JSON parsing, and `git diff --check` passed. S2-11 runtime regression and `init.ps1 -Mode full` evidence is preserved in the scoped regression report.
- **Runner state:** both S2-11 logs contain terminal platform and full-rerun markers; audit/SQLite show zero active runs and zero queued/leased jobs. No runner was restarted for S2-12.
- **Next:** no remaining S2-12 task. Any new automation rerun or behavior/real-platform work requires a separately scoped task and must preserve these raw reports.

## 2026-10-20 第三阶段方案

- 用户本次要求制定可工程落地的下一阶段自动化检测方案；已核对 census、queue、Worker、recipe/comparator、原生配对 runner、CI 与 S2-12 报告，并在新计划内给出代码落点、合同、资源/重试预算、验收、10 项依赖任务及 24–26 人日初估。
- 主要决策：新 campaign/DB 保留历史；逐阶段而非单元最终状态生成补测集合；runtime 在实际 guest 中准入；cleanup 未确认即熔断并 quarantine；attempt generation 拒绝迟到提交；20 个目录 entry 的至少 120 个双平台 case 位置与三次独立复跑。
- 制定方案时未执行任何目录插件、OS 隔离实机探针或完整 runtime build/test。该方案阶段已提交为 `e510d7c`；没有新增支持声明。
- 文档修改前：`bash init.sh docs` 通过（18 documents / 233 links / 14 features）；bundled Node 执行 `node --test scripts/evidence-hash.test.mjs` 4/4 通过；`validate-harness.mjs --target . --json` 100/100。系统 shell 无默认 node，docs 由 init.sh 选择 /opt Node；独立 Node 命令使用本会话已发现的 bundled Node 路径。
- 文档修改后验证结果见下方验收记录；仅验证方案/状态文档，不代表 P3 运行时功能已实现。

### P3-01 合同与补测账本

- 实施提交待创建。新增 automation manifest schema、严格 source/identity 校验、`python -m lab.automation plan/validate`、正例 fixture 与拒绝负例。
- 实际 manifest：[phase3-p301-recovery-manifest-20261020.json](outputs/acceptance/phase3-p301-recovery-manifest-20261020.json)，SHA-256 `f084778b6cfee271876ba6493e039fc2a288e0295a7a0fe9353f0d6f2074416e`：11,380 source units，3,179 受影响 units / 19,074 stage rows，1,048 静态 artifact/profile skips。没有派发任何插件或 Worker。
- 基线提交 `e510d7c` 上，Linux `queue/recipes/compare` 24 项通过。实施后 `uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_automation_selection lab.tests.test_queue lab.tests.test_recipes lab.tests.test_compare -v`：28 项通过。完整 ledger test 从 512 shard 复算并验证输入 SHA、终态分母和六阶段账目。没有运行涉及 Windows 的测试。
- `uv run --locked --project lab --python 3.12.14 python -m lab.automation plan --output outputs/acceptance/phase3-p301-recovery-manifest-20261020.json` 与 `... validate ...` 均退出 0；测试同时拒绝缺 shard、重复 shard/unit、摘要漏项、报告内容篡改、profile/artifact 身份错配与跨 snapshot 合并。
- `bash init.sh docs` 退出 0（19 documents / 256 local links / 11 indexed historical Markdown / 24 features）；Node evidence-hash tests 4/4；harness validation 100/100；`git diff --check` 通过。未运行 `Mode full`，因为它会启动 Windows Core smoke/build 测试。
- 当前 next feature 为 P3-02。先实施并验证 Linux Worker 准入与 recovery；Windows 测试和 Windows/Sandbox gate 留待后续 Windows/WSL 实机阶段。

## P3-06 Entry-bound Behavior Suite (2026-10-20)

- P3-06 工程交付完成：冻结 NoneBot/Koishi 各 10 个目录 entry 的 60 个必选行为 case，绑定 catalog/artifact/profile/dependency-lock/source-member 哈希；四个历史支持版本作为独立 regression identities。详见计划中的 P3-06 实施记录及 [suite validation report](outputs/acceptance/phase3-p306-suite-validation-20261020.json)。
- Linux `lab.tests.test_automation_behavior lab.tests.test_recipes lab.tests.test_compare` 共 18 项通过；`python -m lab.automation validate-behavior` 输出 20 entries / 60 cases / 40 platform positions / 4 legacy regressions。此为 schema/identity 验证，`behavior_executed=false`，未安装或运行第三方插件，没有兼容性结论。
- Windows 测试均跳过。feature list 中 P3-06 为 done；P3-02 后续在 WSL2 Linux 上通过 Docker 实机准入，详见本文件后续 P3-02 记录。

## P3-02 Linux Worker Admission (2026-10-20)

- 安装 Ubuntu Docker Engine 29.1.3，拉取锁定镜像 `python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`。Docker daemon 通过当前代理启动，仅用于本次 Linux Worker 验收。
- 真实 probe 共 10 项，全部达到预期终态且 cleanup confirmed；覆盖 boundary、并发状态隔离、非零退出、cgroup CPU/memory/PID 限额、timeout、输出截断、OOM、进程树清理与 worker-loss。预期异常终态按 `unknown` 保留，不作为插件成功。
- Probe [report](outputs/acceptance/phase3-p302-linux-workers-20261020.json) SHA-256 `e9f3a5a466680227b86c54c4df2ad8ae6d83ef7244e6b0cedb317c1fb42618b4`；health [admission report](outputs/acceptance/phase3-p302-linux-admission-retry-20261020.json) 为 admitted，fingerprint `3bb6e32bdea454df69571d7e621c3027fe55c0d3992e974cebe1ed09efa47e42`。原始 docker-unavailable blocker report 保留。
- Windows preflight/runtime 均未执行；Windows admission 待后续 Windows/WSL 专项阶段实测。P3-02 Linux 准入完成；P3-03 已成为 active feature。

## P3-05 Linux Recovery Blocker (2026-10-20)

- Pilot manifest: `outputs/acceptance/phase3-p305-linux-pilot-rebuilt-worker-manifest-20261020.json` (20 units; campaign `p3-recovery-linux-8c161abc3e6e`; SHA-256 `8e3d931c297cb7e22f9d293f954e908bd9c02383c9e5a6af36c4e8060a0bc668`). The rebuilt Python profile image has a distinct exact digest and is recorded in a separate manifest.
- Input inventory: frozen artifact and profile-lock cache 0/20 selected units ready. Exact historic profile locks are absent; a regenerated Koishi lock had a different SHA-256 and was not substituted. The local cache also lacks complete inputs across 2,206 Linux recovery units.
- Attempt checkpoint: `outputs/acceptance/phase3-p305-linux-pilot-checkpoint-20261020.json`, SHA-256 `5a35b12ffd323068ebe7152a1fb2a40ed7197f625e2c97a180e5762e082a551e`. One install attempt failed before Worker startup; no plugin code ran. Exact attempt resources were reconciled cleanup-confirmed. Queue remains interrupted, 19 units not run, compatibility not evaluated.
- Reproduce the Linux tests with `uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_automation_recovery lab.tests.test_automation_selection lab.tests.test_queue lab.tests.test_automation_dispatcher -v`; docs `bash init.sh docs`; evidence hash tests `/home/vegetable/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --test scripts/evidence-hash.test.mjs`. Windows tests were skipped.
- P3-05 needs the exact artifact and dependency-lock bytes restored before same-identity recovery can proceed. P3-07 is active because its declared dependencies P3-03 and P3-06 are complete; P3-08 remains blocked on P3-04 and P3-05.

## P3-03 Baseline (2026-10-20)

- 修改前 Linux baseline：`uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_automation_health lab.tests.test_queue -v`，13 项通过。未运行 Windows 测试。

## P3-03 Linux Implementation (2026-10-20)

- 队列新增 fenced-run migration、随机 attempt token 和 generation；过期租约被记为 unknown 后将 fenced run 保持 interrupted。旧 token 的 heartbeat/finish 被拒绝。只有精确 attempt 的 Docker cleanup evidence 为 true 才能重试和恢复 claims；cleanup=false 或 API 无法查询时继续 quarantine。
- 新增 dispatcher heartbeat/circuit breaker、Linux 主机跨进程互斥，以及 Docker campaign/job/attempt/generation labels 和精确资源 reconciliation。reconciler 先检查全部标签，再清理目标并查询确认；可接入 queue attempt ledger。
- 首轮队列回归暴露一处 joined query 的 `run_id` 歧义，已限定为 `jobs.run_id` 并复测通过。收紧 cleanup=false 后再次复测，run 保持 interrupted。
- Linux 验证：queue + dispatcher 21 tests；`bash init.sh docs` 19 文档 / 269 本地链接；bundled Node `scripts/evidence-hash.test.mjs` 4/4；`git diff --check` 通过。真实 Docker 29.1.3 故障注入使用锁定 Python image：仅清除匹配标签的目标容器，独立无关容器仍运行，测试容器之后全部清理。证据：[P3-03 Linux acceptance](outputs/acceptance/phase3-p303-linux-20261020.json)。
- Windows/Sandbox runtime tests 均 skipped；P3-05 的唯一 pilot attempt 在 Worker 启动前失败，未运行 catalog plugins，未改变兼容 gate。P3-04 按用户要求暂缓；当前 active feature 为 P3-09，P3-05 等待恢复精确缓存输入。
- P3-07: `lab.automation.execution.run_behavior_pair` checks that fixtures are independently oracle-checked and bound to frozen entry/profile inputs, with distinct writable workspaces and explicit Core/Host and cleanup evidence. Four historical samples pass on Linux. One static Windows-bootstrap source test was inadvertently included in an early baseline suite; it launched no Windows/WSL runtime. Final Linux verification excludes it.
