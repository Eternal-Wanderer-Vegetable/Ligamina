# 当前进展

## Current State

- **Last Updated:** 2026-10-12（Asia/Shanghai）。
- **Current Objective:** 按 S2-01–S2-12 顺序完成第二阶段目录全量实验能力并分阶段提交。
- **Active Feature:** S2-06 / feat-008；S2-05 已提交 `854b564`，S2-06 baseline 已记录，尚未修改 S2-06 实现。
- **Completed stage commits:** H01 `2d6399a`、S2-01 `15f468d`、S2-02 `c0be568`、S2-04 `2fa06b0`、S2-03 `29bc1e5`、S2-05 `854b564`。
- 第一阶段 T01–T18、C01–C15、G1/G2 以 [T18 报告](outputs/acceptance/t18-final-audit-20261012.json) 为准；四个精确插件版本和虚拟 Gateway，真实平台收发未验证。
- 第二阶段 S2-01–S2-05 完成；S2-06–S2-12 尚未完成，状态见 [feature_list.json](feature_list.json)。

## What's Done

- 已建立短 AGENTS 入口、六个专题指南、文档导航、状态文件和 S2-01–S2-12 实施计划。
- [S2-01 冻结目录](catalogs/p2-41649744e057d61031d5/catalog.json)：NoneBot 943 + Koishi 4,747，共 5,690 registry entries；来源/原始字节/哈希/provenance/官方维护子集可复核。
- [S2-02 输出合同](outputs/acceptance/phase2-contracts-20261012.json)：四个 v1 schemas、9 个共享 golden vectors、状态转换和分母守恒验证。
- [S2-04 Worker 验收](outputs/acceptance/phase2-workers-20261012.json)：Linux Docker 10 项、Windows Sandbox 7 项 benign probes 均通过；WSL user/mount namespace 因只读 bind 可被重挂可写而拒绝。
- [S2-03 发布物与 profile 锁](outputs/acceptance/phase2-s203-20261012.json)：5,690 entries、6,247 artifact references、11,380 Windows/Linux profile 单元；10,332 个完整锁，其余显式终态。没有执行目录插件或 sdist build backend。
- [S2-05 独立原生基线](outputs/acceptance/phase2-s205-20261012.json)：四个既有支持版本的 Native/Federated 双路径及独立 oracle 均通过，cleanup 确认；Native 不导入 Ligamina Bridge。Windows Sandbox App Control 在插件启动前阻止 CPython（Win32 4551），本阶段插件探针在禁网 Linux Docker Worker 执行。

## S2-05 Verification

- 实施前 baseline：`pwsh -NoProfile -File init.ps1 -Mode full`，退出码 0；原 smoke 报告摘要和哈希保存在 [baseline 证据](outputs/acceptance/phase2-s205-baseline-smoke-windows-20261012.json)。
- 四样本最终配对命令：`uv run --locked --project lab --python 3.12.14 python -m lab.baselines.run_s205`；NoneBot autohelp 0.1.7、repeater 3.1.4、Koishi game-mini 0.4.3、multi-qa 1.0.8 均通过 Native、Federated、oracle、pair comparison 和 Worker cleanup。
- `uv run --locked --project lab --python 3.12.14 python -m unittest discover -s lab/tests`：52 项通过；包括 wrong-recipient 单边错误、双方同错仍失败的负例。
- `pwsh -NoProfile -File init.ps1 -Mode docs`：18 文档、152 本地链接、9 历史 Markdown、14 任务通过。
- Python `py_compile`、Koishi runner `node --check`、`git diff --check` 通过。
- 实施后 `pwsh -NoProfile -File init.ps1 -Mode full`：退出码 0；NoneBot 3、lab 52、Koishi 7、MCP 1、reference client 5、Rust workspace 29 均通过；Windows Core smoke 4 events / 4 gateway ops / 0 残留 PID，bundle stage 通过。
- 四样本 Native/Federated 运行只覆盖 Linux。没有执行全目录 census、Windows 插件 pilot、真实 QQ/平台投递或第一阶段 24 小时实验。

## Next / Blockers

- S2-05 验收报告、指南、feature 状态和交接已更新；当前阶段提交后开始 S2-06。
- S2-06：为配置/preload/services/loader、消息/存储/定时/adapter/console 分类建立 recipe；缺 fixture/凭证必须显式 skipped。
- Windows Sandbox 的 CPython 启动阻止仍是 Windows pilot 前置障碍；不绕过本机策略。S2-09 保留 blocker 和完整 Windows 分母。
- S2-03 的 30 个 sdist build-only 条目只能在 OS 隔离 Worker 构建；全量 census、问题归因和真实平台验收尚未执行。
- 支持声明继续限定精确版本和行为场景；虚拟回执不能当成真实平台投递成功。
