# 会话交接

## Current Objective

- 日期：2026-10-12（Asia/Shanghai）。用户要求完成 `docs/plans/2026-10-12-phase-2-full-catalog.md` 的 S2-01–S2-12 并分阶段提交。
- 分支：`codex/phase2-full-catalog`。仓库新增文档已先提交 `2d6399a`，随后 S2-01 `15f468d`、S2-02 `c0be568`、S2-04 `2fa06b0`、S2-03 `29bc1e5`、S2-05 `854b564`。
- 当前：S2-07 / feat-009 在实施。S2-06 已提交 `4e555b3` 并通过验收；S2-07 baseline 已在此提交、无 S2-07 代码的状态下完成，见 `outputs/acceptance/phase2-s207-baseline-20261012.json`。
- 状态唯一入口：[feature_list.json](feature_list.json)。

## Completed Stages

- H01：Agent 文档 harness、专题指南、README 摘要与第二阶段计划。
- S2-01：官方 NoneBot / Koishi 完整目录快照，共 5,690 registry entries。
- S2-02：lab v1 schemas、状态映射、summary 合同和多语言 vectors。
- S2-04：Linux Docker 与 Windows Sandbox OS Worker；拒绝把 WSL namespace 当安全边界。
- S2-03：冻结 artifact 和依赖 profile 终态；5,690 entries、6,247 平台 artifact 引用、11,380 OS profile 单元；10,332 个完整依赖锁。sdist 后端尚未运行。
- S2-05：独立 stock NoneBot/Koishi runner、同冻结 artifact/lock/config/stimulus 的 Federated 路径、独立 expected oracle 和双路 comparator。四个历史支持版本全通过；Native source 无 Ligamina Bridge import。
- S2-06：两生态各十类行为 recipe；基于冻结 profile 扩展，不修改 S2-05 profile。entry/profile/config/service/fixture binding、缺条件 skipped 与 oracle-first pair comparator 已完成。全量目录只有 69 entries 获得保守分类建议；其余 5,621 以 unmapped_category 留账，recipe 尚未接入 plugin runner。

## S2-05 Evidence

- 报告：[phase2-s205-20261012.json](outputs/acceptance/phase2-s205-20261012.json)。实施前 `pwsh -NoProfile -File init.ps1 -Mode full` 退出码 0；基线摘要/hash：[phase2-s205-baseline-smoke-windows-20261012.json](outputs/acceptance/phase2-s205-baseline-smoke-windows-20261012.json)。
- 最终执行：`uv run --locked --project lab --python 3.12.14 python -m lab.baselines.run_s205`。autohelp 0.1.7、repeater 3.1.4、game-mini 0.4.3、multi-qa 1.0.8 的 Native/Federated/oracle/pair/cleanup 均通过。
- 错收件人和“两边都错仍不能通过”用例在 `lab/tests/test_compare.py`；Native Bridge 排除检查在 `lab/tests/test_baselines.py`。
- `uv run --locked --project lab --python 3.12.14 python -m unittest discover -s lab/tests`：52 项通过。
- `pwsh -NoProfile -File init.ps1 -Mode docs`：18 文档、152 本地链接、9 历史 Markdown、14 tasks；py_compile、`node --check lab/baselines/koishi/runner.mjs`、`git diff --check` 通过。
- 实施后 `pwsh -NoProfile -File init.ps1 -Mode full` 退出码 0：NoneBot 3、lab 52、Koishi 7、MCP 1、reference 5、Rust 29；Windows Core smoke 4 events / 4 Gateway ops / 0 PIDs；bundle stage 通过。
- Windows Sandbox App Control 阻止 unsigned CPython 启动，Win32 error 4551，证据：[phase2-s205-windows-blocker-20261012.json](outputs/acceptance/phase2-s205-windows-blocker-20261012.json)。未绕过策略；四个插件探针在禁网 Linux Docker Worker 中执行。S2-09 Windows pilot 尚未通过。

## Next: S2-07

S2-06 已完成。S2-07 baseline 已在提交 `4e555b3` 上保存，现在只处理 `feat-009`。

- 实现 catalog suite、持久队列与 stage journal，覆盖 prepare/install/native/federated/compare/cleanup 状态；支持 lease、retry、shard 与 checkpoint/resume。
- 验证机器重启、任意 stage kill、过期租约、重复派发、shard overlap、cache contamination 和部分报告；unknown 不可被重试覆盖。
- 当前 frozen catalog 有 5,621 条尚无保守类别建议，后续调度要以显式 `unmapped_category` 记账，不得从全集分母删除。
- S2-03 的 30 个 sdist build-only 条目只能在已验收的 OS Worker 构建；全量 census、真实平台投递与第一阶段 24 小时实验仍未执行。
