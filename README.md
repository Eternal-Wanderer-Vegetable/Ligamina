# Ligamina

Ligamina 的初步定位是：**个人部署优先、独立于具体机器人的插件联邦运行时，以及配套的自动化兼容性实验室。**

它计划保留 NoneBot、Koishi、AstrBot 等生态的原生插件与宿主机制，通过公共接口连接插件能力、聊天事件与生命周期管理。Stella 将作为首个集成客户端；其他机器人与非 AI 客户端也应能够独立接入。

工程执行已通过 T01–T12 的 G1 技术闭环，并已实现 T13/T14 验收工具。T13 的 Windows 与本机 WSL Ubuntu 24.04.5 20 样本 gate 均通过，每生态有 2 个 L4 通过样本；WSL 最终报告记录 8 个样本有 L0/L1/L2 失败，后续 skipped 状态保留且不计通过，只有 4 个 L4 插件进入支持名单，详见[WSL 全量报告](outputs/acceptance/t13-representative-wsl-final-20261010.json)。T14 Windows/WSL 短测通过恢复、CLI 与进程清理检查；用户要求跳过已运行超过 10 小时的 WSL 24 小时长测，状态记为 `skipped`，没有 24 小时报告，资源门槛不计通过，详见[跳过记录](outputs/acceptance/t14-stability-linux-wsl-100x24h-20261010-skip.json)。T15 的 Windows bundle 安装、首启、双 Host、失败更新回滚、停机、卸载保留数据和锁定第三方 `nonebot-plugin-autohelp==0.1.7` 的真实 Host 行为/虚拟回执均已通过本机 PATH 隔离验收，详见[Windows T15 报告](outputs/acceptance/t15-windows-third-party-plugin-20261010.json)。干净 Windows VM 验收仍未完成。T16 Linux 容器 smoke 已通过。T17 本地与 GitHub CI 两端检查均通过。T18 已完成 C01–C15 证据审阅，最终报告为 `NOT READY`，因为 C12 长测跳过且 C14 干净 Windows VM 缺失，详见[T18 最终审阅报告](outputs/acceptance/t18-final-audit-20261010.json)。

## 规划文档

- [v0.1 MVP 必选清单](outputs/2026-10-09-ligamina-mvp-checklist.md)
- [MVP 工程执行计划](outputs/2026-10-09-ligamina-engineering-plan.md)
- [MVP Interface 草案与技术证据](outputs/2026-10-09-ligamina-mvp-contracts.md)
- [初步计划与阶段验收](outputs/2026-10-09-ligamina-initial-plan.md)
- [讨论结论与决策边界](outputs/2026-10-09-ligamina-discussion-decisions.md)

规划依据：[原始分享讨论](https://chatgpt.com/share/6ac8b4f4-4ad8-83e9-b269-90629f1c7979)。

初步计划已由用户核实通过。执行计划记录 T01–T17 的实现进展及阶段证据；G1 于 2026-10-10 通过。验收报告位于 [acceptance](outputs/acceptance/)。T13 的 Windows 与本机 WSL Linux 完整矩阵均已通过 gate；T14 长测按用户决定跳过并保持未通过；T15 bundle 的本机 PATH 隔离、失败更新回滚和第三方插件行为已验证，干净 VM 未验证；T17 GitHub Windows/Linux jobs 均通过；T18 C01–C15 证据审阅已完成，但 `NOT READY`，具体缺口见[最终审阅报告](outputs/acceptance/t18-final-audit-20261010.json)。

## 本机运行诊断

Core 启动后可查看状态、运行诊断或汇总验收报告。`status` 与 `doctor` 只接受 loopback HTTP 地址，并从环境变量读取管理令牌：

```powershell
$env:LIGAMINA_ADMIN_TOKEN = "<daemon admin token>"
target\debug\ligamina-cli.exe status --url http://127.0.0.1:<port>
target\debug\ligamina-cli.exe doctor --url http://127.0.0.1:<port>
target\debug\ligamina-cli.exe report outputs/acceptance/<report>.json
```

执行稳定性验收：

```powershell
uv run --project lab python lab/stability.py --cycles-per-host 100 --duration-hours 24
```

稳定性报告把失败、跳过、未知和缺失样本分开统计。短时运行只验证恢复流程；缺少完整暖机、基线和末段采样窗口时，资源门槛会保持 `skipped`，G2 不会通过。

## 跨平台检查与 CI

本地检查入口覆盖 Windows 与 Linux（在本机 WSL 中运行），并生成对应的 G1 smoke 报告：

```powershell
pwsh -NoProfile -File scripts/check.ps1
```

```bash
bash scripts/check.sh
```

GitHub Actions 工作流位于 `.github/workflows/ci.yml` 和 `.github/workflows/representative.yml`。本地检查记录见 [T17 报告](outputs/acceptance/t17-local-ci-checks-20261010.json)，Windows 与 WSL smoke 证据分别见 [Windows](outputs/acceptance/ci-smoke-windows.json) 和 [Linux](outputs/acceptance/ci-smoke-linux.json)。首个托管 CI 的 Windows job 发现 Git 默认换行转换改变锁文件与 profile 的 SHA-256。仓库现通过 `.gitattributes` 固定 LF，Windows autocrlf 模拟 checkout 下 Runner 17 项复测通过；GitHub `CI #2`–`CI #5` 的 Windows/Linux jobs（含 Linux 镜像构建）均通过。手动代表样本扫描中，GitHub Windows 与 Linux jobs 均扫完 20 个样本且 gate PASS（每生态 2 个 L4）。合并前 PR #1 的 run `38032297005`（提交 `9409d20`）通过 Windows/Linux checks、Windows PATH 隔离 fresh-install 与第三方插件行为验收、G1 smoke 和 Linux 镜像构建，artifact ID 为 Windows `11662631994`、Linux `11662407118`；后续 run `38032965125` 也通过双平台 checks。T15 更新回滚场景已在本机通过，后续 PR CI 待运行。WSL Linux 全量 gate 已完成并通过；报告见[WSL 最终结果](outputs/acceptance/t13-representative-wsl-final-20261010.json)。

仓库：[Eternal-Wanderer-Vegetable/Ligamina](https://github.com/Eternal-Wanderer-Vegetable/Ligamina)。
