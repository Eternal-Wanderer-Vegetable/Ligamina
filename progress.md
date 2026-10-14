# 当前进展

## Current State

- **Last Updated:** 2026-10-15（Asia/Shanghai）。
- **Current Objective:** 按 S2-01–S2-12 顺序完成第二阶段目录全量实验能力并分阶段提交。
- **Active Feature:** S2-11 / feat-013；失败/未知账本、证据逐项核对、`process.exit(0)` 修复及四组隔离 Worker 回归已完成；新的 512 分片复跑仍在执行。已闭合 266/512 报告与 5,899/11,380 单元（0 passed、320 failed、3,382 skipped、2,197 unknown）；Linux shards 0–255、Windows shards 0–9 完成，Windows shard 10 活跃（runner PID 28372）。
- **Completed stage commits:** H01 `2d6399a`、S2-01 `15f468d`、S2-02 `c0be568`、S2-04 `2fa06b0`、S2-03 `29bc1e5`、S2-05 `854b564`、S2-06 `4e555b3`、S2-07 `7523a83`、S2-08 `cbcbb0a`、S2-09 `20d8f04`；`299d22c` 是 S2-09 baseline/status checkpoint；S2-10 census checkpoints `8f21ead`、`4295831`、`25cf805`、`7ac3f1c`、`6f864bb`、`fb3920e`、`01988bc`、`ee758da`、`36768b5`、`aa25211`、`aa89dbc`、`25a0d41`、`fa9fea8`、`bcb1e73`、`12f6203`、`45ade15`、`75c750c`、`3455a07`、`95909ff`、`498f436`、`a81dc61`、`e35bf26`、`2e44298`、`8e8189d`、`c457043`、`5a89e0f`、`60bfba3`、`466f9ec`、`1ed38d8`、`ac56cf7`、`f295313`、`f625b48`、`e2debcb`、`1455b31`、`f9c2c6b`、`c479f49`、`6356360`、`8a32e8e`、`6c1818f`、`31709fa`、`d071238`、`01d412e`、`5f7be0c`、`ecf4ebe`、`4047c76`、`f41c2f2`、`21d1336`、`0d56ba1`、`4c974f3`、`e491a3e`、`35d8428`、`b6c9237`、`2627109`、`da1322c`、`96350a3`、`084c015`、`1f1c3ae`、`3161907`、`0dfcaef`、`3f9c6ac`、`8b55df4`、`141cfee`、`4b044c7`、`9461053`、`cdb6a10`、`fe21968`、`9d401d6`、`eff6790`、`5213dac`、`1e15788`、`1bb005d`、`92b3f47`；S2-11 data checkpoint `550fd88`。
- 第一阶段 T01–T18、C01–C15、G1/G2 以 [T18 报告](outputs/acceptance/t18-final-audit-20261012.json) 为准；四个精确插件版本和虚拟 Gateway，真实平台收发未验证。
- 第二阶段 S2-01–S2-10 完成；S2-11 正在进行，S2-12 尚未开始，状态见 [feature_list.json](feature_list.json)。S2-10 的 terminal census closure 不是兼容通过：0 passed、1,235 failed、9,324 skipped、821 unknown。

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

## Next / Blockers

- S2-11 失败与未知归因账本覆盖 2,056/2,056 个单元；每行绑定 entry、OS、artifact/profile、阶段结果与 attempt hash、源 shard/run 和整 shard 复现命令，原 S2-10 census 保持不变。证据与分类见 [failure ledger](outputs/acceptance/phase2-s211-failures-20261014.md) 和 [machine-readable ledger](outputs/acceptance/phase2-s211-failures-20261014.json)：944 native-load、162 offline npm install、125 Worker nonzero、4 report-invalid failures；796 App Control、18 preparation、6 timeout、1 backend unknown。24 个 native-load 失败明确报告 DLL 被 App Control 阻止；107 个 Windows npm OOM 发生在已配置的 8 GiB Job Object 上限处。
- 四个 Koishi `process.exit(0)` 无协议报告缺陷已在 probe 层捕获并恢复原 `process.exit`；Linux Docker 与 Windows Sandbox 的两个冻结包各复跑一次，均生成 `native_plugin_load_failed` 结构化报告且 cleanup 确认。定点测试 1/1、Lab suite 109/109 通过；修复后的 `pwsh -NoProfile -File init.ps1 -Mode full` 退出 0：docs 18/228、NoneBot 3、Lab 109、Koishi 7、MCP 1、reference 5、Rust 33、Windows smoke 4 events/4 gateway operations/0 PIDs，bundle staging 通过。命令和结果见 [S2-11 scoped regression](outputs/acceptance/phase2-s211-process-exit-regression-20261014.json)。修复保留失败语义，不宣称插件通过。
- S2-11 全新 512 shard rerun 于 2026-10-14 17:06（Asia/Shanghai）启动，使用新队列 `.work/catalog-census/phase2-s211-rerun.sqlite3` 和独立报告前缀。截至 2026-10-15 02:38，266/512 份完整 canonical 报告覆盖 5,899/11,380 个 rerun 单元；只计入状态 completed、逐项闭合且所有选定阶段终态的 Linux shards 0–255 与 Windows shards 0–9。当前单元终态为 0 passed、320 failed、3,382 skipped、2,197 unknown。Windows shard 10/256 在 1 个 running queue run 中执行（runner PID 28372；checkpoint 时 SQLite 为 1 leased、64 queued）；余下 246 个分片中含该活动分片。已闭合单元余 5,481。不重用原 S2-10 队列，且不聚合不完整 shard。当前未观察到 native-success/federation-failure 配对，因为 federated/compare 被缺失的 entry-bound fixture/loader profile 跳过；空清单不能作为 parity 证据。5,621 个冻结 entry 仍无保守行为分类建议，App Control unknown 不绕过本机策略。
- P2-BEHAVIOR 未评估、P2-FIELD 未执行。预算仍 provisional；行为、安装/启动率与兼容率分别报告，不以分片闭合或安装成功替代行为兼容。
- S2-09 budget 以可达平台样本测量但保持 provisional；S2-10 census 已闭合，S2-11 新复跑的耗时、下载与磁盘观察仍待全量完成后复核。
- S2-03 的 30 个 sdist build-only 条目只能在 OS 隔离 Worker 构建；虚拟回执不能当成真实平台投递成功。
- 支持声明继续限定精确版本和行为场景；虚拟回执不能当成真实平台投递成功。
