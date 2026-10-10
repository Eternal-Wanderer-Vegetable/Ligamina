# Ligamina

Ligamina 的初步定位是：**个人部署优先、独立于具体机器人的插件联邦运行时，以及配套的自动化兼容性实验室。**

它计划保留 NoneBot、Koishi、AstrBot 等生态的原生插件与宿主机制，通过公共接口连接插件能力、聊天事件与生命周期管理。Stella 将作为首个集成客户端；其他机器人与非 AI 客户端也应能够独立接入。

工程执行已通过 T01–T12 的 G1 技术闭环，并已实现 T13/T14 验收工具。T13 的 Windows 20 样本 gate 通过，含每生态 2 个 L4 通过样本；本机 WSL Linux 20 样本扫描已完成，但 gate 为 `NOT READY`：两个 Koishi L4 候选的 npm tarball 在完整扫描和单独重试时均下载超时。T14 在 Windows 和本机 WSL 的短测均验证生产 Core 双 Host 恢复、CLI 检查和进程清理；WSL 100 次/Host、24 小时稳定性长测正在运行，短测资源门槛按设计为 `skipped`。T15 的 Windows x64 runtime bundle 与版本化安装/更新/卸载初版通过中文空格路径安装及失败回滚探针；首启配置和干净 VM 仍待验证。T16 Linux 容器 smoke 已通过；T17 Windows/WSL 本地 CI 入口通过，首个 GitHub 托管 CI 暴露 Windows 换行转换导致哈希不稳定的问题，LF 规范及 CI 式复测已通过，托管重跑待确认；T18 未完成，G2 保持 `NOT READY`。

## 规划文档

- [v0.1 MVP 必选清单](outputs/2026-10-09-ligamina-mvp-checklist.md)
- [MVP 工程执行计划](outputs/2026-10-09-ligamina-engineering-plan.md)
- [MVP Interface 草案与技术证据](outputs/2026-10-09-ligamina-mvp-contracts.md)
- [初步计划与阶段验收](outputs/2026-10-09-ligamina-initial-plan.md)
- [讨论结论与决策边界](outputs/2026-10-09-ligamina-discussion-decisions.md)

规划依据：[原始分享讨论](https://chatgpt.com/share/6ac8b4f4-4ad8-83e9-b269-90629f1c7979)。

初步计划已由用户核实通过。执行计划记录 T01–T17 的实现进展及阶段证据；G1 于 2026-10-10 通过。验收报告位于 [acceptance](outputs/acceptance/)。T13 WSL 全量矩阵已执行，但两个 Koishi L4 候选未能从 registry 下载，Linux gate 保持 `NOT READY`；T14 长测尚未完成；T15 首启/干净 VM 验收、T17 GitHub 托管 CI 修复后的重跑与每周代表样本扫描、T18 和 C01–C15 总体验收仍待完成，G2 保持 `NOT READY`。

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

GitHub Actions 工作流位于 `.github/workflows/ci.yml` 和 `.github/workflows/representative.yml`。本地检查记录见 [T17 报告](outputs/acceptance/t17-local-ci-checks-20261010.json)，Windows 与 WSL smoke 证据分别见 [Windows](outputs/acceptance/ci-smoke-windows.json) 和 [Linux](outputs/acceptance/ci-smoke-linux.json)。首个托管 CI 已由初始推送触发；Windows job 发现 Git 默认换行转换改变锁文件与 profile 的 SHA-256。仓库现通过 `.gitattributes` 固定 LF，Windows autocrlf 模拟 checkout 下 Runner 17 项复测通过，托管重跑尚待确认，因此 T17 仍为部分通过。

仓库：[Eternal-Wanderer-Vegetable/Ligamina](https://github.com/Eternal-Wanderer-Vegetable/Ligamina)。
