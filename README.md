# Ligamina

Ligamina 的初步定位是：**个人部署优先、独立于具体机器人的插件联邦运行时，以及配套的自动化兼容性实验室。**

它计划保留 NoneBot、Koishi、AstrBot 等生态的原生插件与宿主机制，通过公共接口连接插件能力、聊天事件与生命周期管理。Stella 将作为首个集成客户端；其他机器人与非 AI 客户端也应能够独立接入。

工程执行已完成 T01–T18 的计划验收。T13 的 Windows 与本机 WSL Ubuntu 24.04.5 20 样本 gate 均通过；8 个 WSL 样本保留 L0/L1/L2 失败与 skipped 结果，不计为支持样本，支持名单仅含 4 个 L4 插件，详见[WSL 全量报告](outputs/acceptance/t13-representative-wsl-final-20261010.json)。此前因主机过载中断的 T14 长测仍作为历史 `skipped` 记录；重跑的 WSL 24 小时稳定性与资源门槛现已通过，记录 200/200 恢复循环和 1,440/1,440 个通过采样点，见[最终报告](outputs/acceptance/t14-stability-linux-wsl-100x24h-20261011.json)。T15 bundle 安装、回滚、双 Host、第三方 `nonebot-plugin-autohelp==0.1.7` 行为及虚拟回执已通过；干净 Windows Sandbox 的 C14 八项检查也全部通过，见[Windows T15 报告](outputs/acceptance/t15-windows-third-party-plugin-20261010.json)和[T15/C14 Sandbox 报告](outputs/acceptance/t15-windows-sandbox-c14-20261011.json)。T16 Linux 容器 smoke、T17 本地和 GitHub CI 检查均通过。T18 于 2026-10-12 完成 C01–C15 复核，G1/G2 均为 `PASS`；最新审阅见[最终报告](outputs/acceptance/t18-final-audit-20261012.json)。支持名单仍限于已通过的四个精确插件版本；未连接真实 QQ 或其他平台网关，因此这些结果不代表真实平台收发。

## 规划文档

- [v0.1 MVP 必选清单](outputs/2026-10-09-ligamina-mvp-checklist.md)
- [MVP 工程执行计划](outputs/2026-10-09-ligamina-engineering-plan.md)
- [MVP Interface 草案与技术证据](outputs/2026-10-09-ligamina-mvp-contracts.md)
- [初步计划与阶段验收](outputs/2026-10-09-ligamina-initial-plan.md)
- [讨论结论与决策边界](outputs/2026-10-09-ligamina-discussion-decisions.md)

规划依据：[原始分享讨论](https://chatgpt.com/share/6ac8b4f4-4ad8-83e9-b269-90629f1c7979)。

初步计划已由用户核实通过。验收报告位于 [acceptance](outputs/acceptance/)。G1 于 2026-10-10 通过；完成 T14 长测和 C14 Sandbox 验收后，T18 复核了全部 C01–C15，G2 于 2026-10-12 通过。10 月 10 日的 `NOT READY` 审阅报告保留为历史裁决；当前裁决见[最新 T18 报告](outputs/acceptance/t18-final-audit-20261012.json)，执行记录见[工程计划第 9 节](outputs/2026-10-09-ligamina-engineering-plan.md)。真实平台网关和实际 QQ/平台投递不属于本次已验证范围。

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

GitHub Actions 工作流位于 `.github/workflows/ci.yml` 和 `.github/workflows/representative.yml`。本地检查记录见 [T17 报告](outputs/acceptance/t17-local-ci-checks-20261010.json)，Windows 与 WSL smoke 证据分别见 [Windows](outputs/acceptance/ci-smoke-windows.json) 和 [Linux](outputs/acceptance/ci-smoke-linux.json)。首个托管 CI 的 Windows job 发现 Git 默认换行转换改变锁文件与 profile 的 SHA-256。仓库现通过 `.gitattributes` 固定 LF，Windows autocrlf 模拟 checkout 下 Runner 17 项复测通过；GitHub `CI #2`–`CI #5` 的 Windows/Linux jobs（含 Linux 镜像构建）均通过。手动代表样本扫描中，GitHub Windows 与 Linux jobs 均扫完 20 个样本且 gate PASS（每生态 2 个 L4）。合并前 PR #1 的 run `38032297005`（提交 `9409d20`）通过 Windows/Linux checks、Windows PATH 隔离 fresh-install 与第三方插件行为验收、G1 smoke 和 Linux 镜像构建，artifact ID 为 Windows `11662631994`、Linux `11662407118`；合并后 run `38032965125` 也通过双平台 checks。PR #2 的 run `38060766011` 通过 Windows/Linux jobs，包含 T15 损坏哈希更新回滚验收和 Linux 镜像构建；artifact ID 为 Windows `11672952328`、Linux `11673007283`。WSL Linux 全量 gate 已完成并通过；报告见[WSL 最终结果](outputs/acceptance/t13-representative-wsl-final-20261010.json)。

仓库：[Eternal-Wanderer-Vegetable/Ligamina](https://github.com/Eternal-Wanderer-Vegetable/Ligamina)。
