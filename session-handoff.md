# 会话交接

## Current Objective

- 日期：2026-10-12（Asia/Shanghai）。本次目标：完成 S2-01–S2-12 并分阶段提交。
- 执行分支：`codex/phase2-full-catalog`。文档前置提交 `2d6399a`、S2-01 `15f468d`、S2-02 `c0be568`、S2-04 `2fa06b0`。
- 当前任务：S2-05 / feat-007（独立原生基线）；S2-03 已完成。S2-03 的 30 个 sdist build-only 条目仍要求后续在隔离 Worker 中构建。
- 状态与依赖唯一入口：[feature_list.json](feature_list.json)。

## Completed

- H01：短 AGENTS 入口、专题指南、验证命令、harness 与第二阶段计划。
- S2-01：冻结 NoneBot 943 + Koishi 4,747，共 5,690 个 registry entries；完整快照在 `catalogs/p2-41649744e057d61031d5/`。
- S2-02：四个 v1 schemas、9 个多语言 golden vectors、旧状态映射和汇总；见 `outputs/acceptance/phase2-contracts-20261012.json`。
- S2-04：Linux Docker Worker 与 Windows Sandbox CLI Worker；边界、网络、资源、超时、输出、进程树、worker 丢失和兄弟隔离探针通过。综合证据见 `outputs/acceptance/phase2-workers-20261012.json`。
- WSL user/mount namespace 不接受为 Linux 边界：namespace root 能将只读 bind mount 改为可写，见 `outputs/acceptance/phase2-wsl-userns-boundary-20261012.json`。
- S2-03：5,690 个 entry 的 advertised version / artifact terminal state，6,247 个平台归档引用和 11,380 个 Windows/Linux profile 单元已冻结；10,332 个 profile lock 可复核，其余终态显式分类。完整证据见 `outputs/acceptance/phase2-s203-20261012.json`。

## Verification Evidence

- `pwsh -NoProfile -File init.ps1 -Mode docs`：18 文档、148 本地链接、9 历史 Markdown、14 任务通过。
- Linux Worker：`uv run --locked --project hosts/nonebot --python 3.12.14 python deploy/lab/linux/probes/acceptance.py`，10/10 预期结果与 cleanup 通过；固定镜像为 `python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`。
- Windows Worker：`uv run --locked --project hosts/nonebot --python 3.12.14 python deploy/lab/windows/probes/acceptance.py`，7/7 预期结果与 cleanup 通过。
- `pwsh -NoProfile -File deploy/lab/check-windows-runner.ps1`：runner PowerShell 解析和 Job Object C# 源码编译通过。
- `uv run --locked --project hosts/nonebot --python 3.12.14 python -m unittest discover -s lab/tests -v`：36 项通过。Python worker/probe py_compile 和 `git diff --check` 通过。
- `wsb.exe list --raw` 返回空环境；Docker 专用 Worker 容器检查无残留。Worker probes 没有下载或执行 catalog 插件。
- S2-03：`uv run --locked --project lab --python 3.12.14 python -m unittest discover -s lab/tests -v` 47 项通过；`python -m py_compile`、docs 检查及 `lab.artifacts verify` / `lab.profiles verify` 均通过；锁 hash 和详细计数见验收报告。没有执行第三方构建后端、npm lifecycle scripts 或目录插件。
- 第一阶段 24 小时、全 catalog、pilot、真实平台和字段验收均没有重跑。

## Files / Remaining Scope

- 路由：[AGENTS](AGENTS.md)、[docs](docs/README.md)、[第二阶段计划](docs/plans/2026-10-12-phase-2-full-catalog.md)。
- 当前代码：`lab/artifacts.py`、`lab/profiles.py` 与 artifact/profile lock；S2-03 证据见 `outputs/acceptance/phase2-s203-20261012.json`。
- 下一步 S2-05：检查独立 stock native runner 方案和既有四个支持插件案例，先记录 baseline，再实现同输入双路径及独立 oracle。
- S2-05–S2-12、pilot、全量 census、真实 QQ/平台收发仍未完成；不要将锁定成功或 Worker benign probes 等同插件兼容验收。

## Next Session Startup

1. 读取 `AGENTS.md`、`progress.md`、`feature_list.json` 和本交接。
2. 检查 `git status --short` 和最近提交，运行 `pwsh -NoProfile -File init.ps1 -Mode docs`。
3. 检查 S2-03 阶段提交与工作树；继续 S2-05，按状态文件记录 baseline、命令、终态、失败和证据，再完成阶段提交。

## Recommended Next Step

- S2-05 为当前任务；当前 DirectWorker 复用 Bridge，不能代表独立原生路径。
- 不使用既有 20 样本 gate 作为全集完成判定；准备全量执行前先完成 S2-09 双平台 pilot。
- 保留 `missing/skipped/unknown/failed/flaky` 等状态；真实平台未验证时 README 继续明确标注虚拟范围。
