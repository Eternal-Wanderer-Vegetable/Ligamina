# 开发与验证

适用：构建、运行、诊断、安装、CI。来源：现有 scripts、lockfiles 和 acceptance；命令或工具链变化时复审。

## 启动工作会话

文档检查不安装依赖、不启动 Host、不覆写验收报告：

```powershell
pwsh -NoProfile -File init.ps1 -Mode docs
```

```bash
bash init.sh docs
```

完整验证复用已有入口，会安装锁定依赖、运行 tests/build 和真实 Core + Host smoke；Windows 还构建 bundle：

```powershell
pwsh -NoProfile -File init.ps1 -Mode full
```

```bash
bash init.sh full
```

工具链以 [Rust 配置](../../rust-toolchain.toml)、[NoneBot profile](../../hosts/nonebot/pyproject.toml)、[Koishi profile](../../hosts/koishi/package.json)、[检查入口](../../scripts/check.ps1) 为准。基线 Rust 1.97.1、CPython 3.12.14、Node 24.20.0、uv 0.12.5。保持系统默认环境，通过锁文件和项目环境安装。

## 按改动选择检查

| 改动 | 必要验证 |
| --- | --- |
| 文档/状态/README 渲染 | `node --test scripts/evidence-hash.test.mjs`；docs 入口；bundled validate-harness 验证结构 |
| Rust Core/CLI | cargo fmt --all -- --check、cargo test --workspace --locked；路由/生命周期改动重跑 smoke |
| NoneBot Host | uv run --locked --project hosts/nonebot --python 3.12.14 pytest -q hosts/nonebot/tests，以及 Host README 原生/Bridge/worker probes |
| Koishi Host | npm ci、npm test（在 hosts/koishi）；MCP 改动加 npm run test:mcp |
| Lab/报告 | uv run --locked --project lab --python 3.12.14 python -m unittest discover -s lab/tests -v，再运行受影响 suite；S2-10 census 改动包括 `lab.tests.test_census` |
| S2-05 原生/联邦配对 | `uv run --locked --project lab --python 3.12.14 python -m lab.baselines.run_s205`；四个既有支持版本在禁网 Linux Worker 中运行 |
| Worker 隔离策略 | `uv run --locked --project hosts/nonebot --python 3.12.14 python -m unittest lab.tests.test_workers -v`；Windows runner 另运行 `pwsh -NoProfile -File deploy/lab/check-windows-runner.ps1` |
| 目录发布物/依赖锁 | `uv run --locked --project lab --python 3.12.14 python -m lab.artifacts verify catalogs/<snapshot>` 与 `python -m lab.profiles verify catalogs/<snapshot>`；缓存根必须与生成时一致 |
| 跨语言/协议/安装 | 完整入口和双 OS CI；安装改动追加 [Windows 安装验收](../../scripts/windows-install-acceptance.ps1) / Linux容器验收 |

## 运行与诊断

```powershell
cargo build -p ligamina-cli --locked
target\debug\ligamina-cli.exe daemon --config <daemon.json>
$env:LIGAMINA_ADMIN_TOKEN = '<admin token>'
target\debug\ligamina-cli.exe status --url http://127.0.0.1:<port>
target\debug\ligamina-cli.exe doctor --url http://127.0.0.1:<port>
target\debug\ligamina-cli.exe report outputs/acceptance/t13-representative-windows-full-20261010.json
```

配置结构参考 [Linux example](../../deploy/linux/daemon.example.json)；其中占位凭证使用前替换。status/doctor 只接受 loopback 地址，管理令牌从环境读取。

安装按需读取 [Windows bundle](../../deploy/windows/README.md)、[Linux container](../../deploy/linux/README.md)。持续集成见 [CI](../../.github/workflows/ci.yml)、[代表样本工作流](../../.github/workflows/representative.yml)。

## 记录验证

记录命令、退出码、环境、commit/dirty 和证据路径。区分本机测试、托管 CI、干净安装与真实平台验收。S2-09 已对 NoneBot Linux 与 Koishi Linux/Windows 的分层 cohort 接通隔离安装和 native-load pilot；行为 fixture 尚未派发，Windows NoneBot 受 App Control 限制而保持 unknown。重建 acceptance 时需传入完整的 NoneBot/Koishi/Linux/Windows 三份平台报告，命令见[兼容性指南](compatibility.md#s2-09-分层-pilot已完成)。

S2-04 双平台运行时探针可复跑：`uv run --locked --project hosts/nonebot --python 3.12.14 python deploy/lab/linux/probes/acceptance.py` 与 `uv run --locked --project hosts/nonebot --python 3.12.14 python deploy/lab/windows/probes/acceptance.py`。它们只运行 benign isolation probes，不下载或执行 catalog 插件；结果写入 `outputs/acceptance/phase2-workers-*-20261012.json`。

P3 Linux Worker 准入先用 `uv run --locked --project lab --python 3.12.14 python -m lab.automation preflight --platform linux --probe-report <report.json> --output <health.json>`。只有 Docker daemon、锁定镜像、主机资源余量及真实 benign probe（包含逐项 cleanup）都通过时，报告才允许 claim。Docker 不可用、probe 缺失或资源不足会退出 3 并留下 `blocked` 证据；不能用 mock 代替准入。为避免覆盖 S2 历史证据，可将 Linux probe 输出写到新路径：`uv run --locked --project hosts/nonebot --python 3.12.14 python deploy/lab/linux/probes/acceptance.py --report outputs/acceptance/phase3-p302-linux-workers-20261020.json`。Windows preflight 入口当前只会写 `not_tested` blocker。Windows host-only runtime 探针只能诊断本机解释器能否启动，不能代替 Sandbox 内低权限启动、隔离/限额和进程清理准入；App Control 拦截应保留对应 Win32 证据，不关闭策略来伪造通过。

P3-05 默认生成 20 项分层 pilot；`plan-recovery-pilot --selection-scope affected-linux --probe-semantics-version 3` 会按 P3-01 冻结身份选出全部锁定的 Linux affected entries，并把实际归档长度和 Koishi core-only Host service profile 绑定进新 manifest。示例：`uv run --locked --project lab --python 3.12.14 python -m lab.automation plan-recovery-pilot --selection-scope affected-linux --probe-semantics-version 3 --archive-cache-root <cache-root> --output <manifest.json>`。执行前仍需 `validate-recovery-pilot` 与 30 分钟内的真实 Worker 准入；Worker image 必须是本机可用且 digest 匹配的引用，必要时使用 `--nonebot-image` / `--koishi-image` 覆盖并冻结到新 manifest。v3 的缺失 Host service 会在目标插件回调前记为 `skipped/required_host_service_unavailable`；该结果是依赖归因，不是行为兼容通过。

Linux recovery executor 从 `config/lab/phase3-automation-v1.json` 读取 `initial_linux_concurrency`，当前上限为 2 个 Worker。共享 backend circuit breaker 在并发访问下加锁；后端故障会中断 fenced run、停止新 claim，并等待已领取 attempt 收尾。恢复时继续使用原 manifest 与 SQLite queue；有过期 lease 时，必须先按精确 attempt/generation 核验 Worker 清理，再恢复派发。

P3 Linux CI 分为两个入口。PR、主分支与每周的非特权自测可在任意 fork 运行：`bash scripts/phase3-linux-self-test.sh`；它只运行 Linux 可复核的契约、文档和自动化单测，不派发目录插件，并写出带日志摘要的报告。专用 Linux recovery runner 仅能由源仓库默认分支 `workflow_dispatch` 触发；仓库变量 `PHASE3_LINUX_RUNNER_ENABLED=true`、带 `ligamina-phase3` 标签的隔离 Linux runner，以及 `phase3-linux-controlled` 环境保护都必须先配置。该 workflow 不接受 PR 事件，显式拒绝 fork/ref 不匹配，权限仅 `contents: read`，并对固定 campaign 禁止并发取消。受控实验只在审核准入后执行；报告/日志及外部 evidence root 会通过 `always()` 上传。运行入口为 `bash scripts/phase3-linux-controlled-runner.sh`。这些 workflow 的静态合同可用 `node --test scripts/phase3-workflows.test.mjs` 检查；本地静态检查不代表 GitHub hosted/self-hosted runner 已实际运行。

P3 entry-bound 行为 suite 的合同和冻结身份可用 `uv run --locked --project lab --python 3.12.14 python -m lab.automation validate-behavior` 检查。它只验证 fixture/schema 和 artifact/profile 锁定，不运行插件，也不代表行为通过；执行前仍需对应 OS Worker 准入。

S2-03 的发布物和依赖锁可按 [兼容性指南](compatibility.md#发布物与依赖锁s2-03) 重跑并校验。它只读取官方元数据、下载并校验归档、解析依赖；不在宿主机执行 sdist backend 或 npm lifecycle scripts。

S2-05 配对回归见[兼容性指南](compatibility.md#当前可运行入口)。报告保留 artifact/profile/config/stimulus 指纹、每条路径结果、oracle 比较和 Worker 清理。当前 Windows Sandbox 启动前置被 App Control 阻止；不可据此声明 Windows 插件行为通过。
