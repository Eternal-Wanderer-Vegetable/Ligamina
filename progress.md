# 当前进展

## Current State

- **Last Updated:** 2026-10-12（Asia/Shanghai）。
- **Current Objective:** 按 S2-01–S2-12 顺序实现第二阶段目录全量测试与输出规范，分阶段提交。
- **Active Feature:** S2-05 / feat-007（独立原生基线）。H01/S2-01/S2-02/S2-04/S2-03 提交分别为 `2d6399a`、`15f468d`、`c0be568`、`2fa06b0`、（待本次提交）。
- 第一阶段 MVP：T01–T18、C01–C15、G1/G2 已通过，以 [T18](outputs/acceptance/t18-final-audit-20261012.json) 为准。
- 支持范围为四个精确插件版本；虚拟 Gateway；真实 QQ/平台收发保持未验证。
- 第二阶段 S2-01–S2-04 已完成；S2-05 正在进行，S2-06–S2-12 尚未完成，状态见 [feature_list.json](feature_list.json)。

## What's Done

- 指定 npx skills add 命令成功，安装在 .agents/skills/harness-creator，source/hash 在 skills-lock.json。
- 已建立短 AGENTS、docs 导航与六个专题指南，保留 dated outputs 与 ADR 的历史内容。
- [第二阶段计划](docs/plans/2026-10-12-phase-2-full-catalog.md) 覆盖官方目录口径、任务依赖、文件、场景、预算、gates 和输出合同。
- [S2-01 冻结目录](catalogs/p2-41649744e057d61031d5/catalog.json)：5,690 条 registry entry，含原始来源、官方维护子集和可复算身份；[provenance 与完整性](outputs/acceptance/phase2-catalog-snapshot-20261012.json)。
- README 保留第一阶段摘要，并链接目录快照与输出合同证据；发布物锁和插件执行仍待完成。
- S2-04 双平台 Worker 已实现并完成 runtime acceptance：Linux Docker 10 项、Windows Sandbox 7 项均按预期分类且 cleanup 通过，汇总见 [Worker 验收](outputs/acceptance/phase2-workers-20261012.json)。只执行了自有 probes，没有执行目录插件。
- 先前来源探针记录 [943 / 4,747 条观测](outputs/acceptance/phase2-catalog-source-probe-20261012.json)；S2-01 已重新抓取完整原始源并固定分母。
- S2-03 已为 5,690 entries 冻结 6,247 个平台归档引用及 11,380 个 Windows/Linux profile 单元；10,332 个 profile 有完整可复核锁，其余均为显式终态。验收与限制见 [S2-03 报告](outputs/acceptance/phase2-s203-20261012.json)。

## Current Task

- S2-04 完成：Docker 和 Windows Sandbox CLI 后端、受限执行、资源/输出限制、worker 丢失分类和清理；Windows 延迟创建可写输出 share，避免 guest 插件伪造宿主结果。
- 当前开始 S2-05：实现与 Ligamina Bridge 独立的 stock NoneBot/Koishi 原生回归路径，先记录既有 Host 与四个已支持插件版本的基线，再扩展同一输入/独立 oracle 差分。
- S2-02 完成并已提交：定义 catalog/run/result/summary v1 schemas、9 个共享 golden vectors、旧状态映射与守恒汇总；Python/Rust/TypeScript 验证一致，见 [合同验收](outputs/acceptance/phase2-contracts-20261012.json)。
- S2-01 完成：抓取两官方 registry 和 Koishi 官方插件页，保存 raw/hash/provenance、entry 身份、官方维护子集及基线变化账本。
- S2-04 只执行自有 benign worker probes；没有下载或执行目录插件。
- Windows 前置 baseline `pwsh -NoProfile -File init.ps1 -Mode full` 退出码 0；摘要及 smoke artifact hash 见 [baseline 证据](outputs/acceptance/phase2-baseline-windows-20261012.json)。
- Catalog 单元测试 9 项通过；`uv run --locked --project lab --python 3.12.14 python -m lab.catalog.snapshot verify catalogs/p2-41649744e057d61031d5` 完整性通过。
- S2-02 后 `pwsh -NoProfile -File init.ps1 -Mode full` 退出码 0：docs 18/140、NoneBot pytest 3、lab 31、Koishi 7、MCP 1、reference client 5、Rust workspace、release build、Windows Core smoke（4 events / 4 gateway operations / 0 残留 PID）和 bundle stage 均通过；详见 [合同验收](outputs/acceptance/phase2-contracts-20261012.json)。

## Verification Evidence

- 安装：npx skills add walkinglabs/learn-harness-engineering --skill harness-creator，退出码 0。
- Windows 与 Git Bash 文档检查通过：18 文档、130 本地链接、9 历史 Markdown、14 任务；harness 结构 100/100。README 正常输入与五项拒绝负例、Node/PowerShell/Bash 语法、CLI 示例和 diff 检查通过。完整记录见 [本次验证报告](outputs/acceptance/documentation-harness-20261012.json)。
- Ubuntu WSL 文档命令超时，记录 skipped；任务自己的 WSL 客户端已停止，未终止 distro。`bash init.sh docs` 已在 Git Bash 通过；Linux runtime 本次未验证。
- 完整验证首次打印 Windows `pydantic_core` access violation，同时新测试导入路径错误；修正目录后重跑未复现该异常。另一次停在 rustfmt 检查，格式化后完整命令通过。
- 没有重跑第一阶段 24 小时、Sandbox 或真实平台验收；S2-02 未下载或执行插件。
- S2-04 runtime：Linux Docker Engine 29.7.2、固定镜像 digest，10/10 预期结果和清理通过；Windows Sandbox CLI 7/7 通过。证据含边界、网络/秘密、只读输入、CPU/内存/PID 限额、超时、日志截断、进程树、worker 丢失与兄弟隔离。报告分别为 [Linux](outputs/acceptance/phase2-workers-linux-20261012.json)、[Windows](outputs/acceptance/phase2-workers-windows-20261012.json) 和[汇总](outputs/acceptance/phase2-workers-20261012.json)。
- WSL namespace root 可把只读 bind mount 重新挂成可写，故 WSL 不作为 Linux Worker；见 [WSL 拒绝证据](outputs/acceptance/phase2-wsl-userns-boundary-20261012.json)。`wsb list` 与 Docker 专用容器检查无残留。
- S2-04 后 lab 单元测试 36 项通过；Windows runner PowerShell 解析与 Job Object 源码编译、Python py_compile、`bash -n`、`git diff --check` 和 docs 检查通过（18 文档、148 本地链接、14 任务）。
- S2-03 baseline：S2-04 提交 `2fa06b0`；lab tests 36 项。阶段实现后 lab tests 47 项、Python py_compile、docs（18 文档、152 本地链接、9 历史 Markdown、14 features）、artifact/profile lock verify 均通过。详细命令、摘要与限制见 [S2-03 报告](outputs/acceptance/phase2-s203-20261012.json)。

## Next / Blockers

- S2-03 完成并待提交：发布物锁 5,690 entries / 6,247 个归档引用；依赖 profile 11,380 单元，其中 10,332 有完整锁。30 个 sdist build-only entry 保持 worker-only；没有安装或执行目录插件。
- 下一步 S2-05：为 stock framework 原生路径建立独立执行基线；本阶段必须覆盖四个已支持插件版本、wrong-recipient 与双方同错反例，且 native runner 不导入 Ligamina Bridge。
- 全量运行前须完成 OS 隔离 Worker、独立 stock native baseline、pilot 和资源预算；当前 isolated 不是 OS sandbox。
- 凭证、服务、fixture、其他 adapter/loader 等条件缺口留在全量账本，不静默删项。
- 顺序调整依据：S2-03 依赖解析可能运行 sdist build backend；虽然原计划把 S2-03 与 S2-04 列为并列依赖，执行状态现在要求 S2-04 先通过再解析第三方构建代码。
- 计划口径仍为完整官方商店收录集合为主、官方维护为子集；历史版本不穷举。全量 census、pilot 与真实平台验收尚未进行。
