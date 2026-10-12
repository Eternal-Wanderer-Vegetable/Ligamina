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
| 文档/状态/README 渲染 | docs 入口；bundled validate-harness 验证结构 |
| Rust Core/CLI | cargo fmt --all -- --check、cargo test --workspace --locked；路由/生命周期改动重跑 smoke |
| NoneBot Host | uv run --locked --project hosts/nonebot --python 3.12.14 pytest -q hosts/nonebot/tests，以及 Host README 原生/Bridge/worker probes |
| Koishi Host | npm ci、npm test（在 hosts/koishi）；MCP 改动加 npm run test:mcp |
| Lab/报告 | uv run --locked --project hosts/nonebot --python 3.12.14 python -m unittest discover -s lab/tests -v，再运行受影响 suite |
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

记录命令、退出码、环境、commit/dirty 和证据路径。区分本机测试、托管 CI、干净安装与真实平台验收。S2-09 已对 NoneBot Linux 与 Koishi Linux/Windows 的分层 cohort 接通隔离安装和 native-load pilot；行为 fixture 尚未派发，Windows NoneBot 受 App Control 限制而保持 unknown。重建 acceptance 时需传入完整的 NoneBot/Koishi/Linux/Windows 三份平台报告，命令见[兼容性指南](compatibility.md#s2-09-分层-pilot进行中)。

S2-04 双平台运行时探针可复跑：`uv run --locked --project hosts/nonebot --python 3.12.14 python deploy/lab/linux/probes/acceptance.py` 与 `uv run --locked --project hosts/nonebot --python 3.12.14 python deploy/lab/windows/probes/acceptance.py`。它们只运行 benign isolation probes，不下载或执行 catalog 插件；结果写入 `outputs/acceptance/phase2-workers-*-20261012.json`。

S2-03 的发布物和依赖锁可按 [兼容性指南](compatibility.md#发布物与依赖锁s2-03) 重跑并校验。它只读取官方元数据、下载并校验归档、解析依赖；不在宿主机执行 sdist backend 或 npm lifecycle scripts。

S2-05 配对回归见[兼容性指南](compatibility.md#当前可运行入口)。报告保留 artifact/profile/config/stimulus 指纹、每条路径结果、oracle 比较和 Worker 清理。当前 Windows Sandbox 启动前置被 App Control 阻止；不可据此声明 Windows 插件行为通过。
