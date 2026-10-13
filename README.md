# Ligamina

Ligamina 是个人部署优先、独立于具体机器人的**插件联邦运行时与自动化兼容性实验室**。它保留 NoneBot 和 Koishi 的原生插件机制，通过 Rust Core 连接事件、插件能力、平台操作和生命周期；客户端可通过本地 HTTP/MCP 接入。

第一阶段已完成。第二阶段目标是冻结两个官方商店的全量插件目录、逐插件隔离测试、比较原生与联邦行为，并以可复现报告展示兼容性。**S2-01 目录冻结与 S2-02 输出合同已完成；全量扫描尚未运行。**

## 第一阶段结果

以下摘要由 `node scripts/render-readme.mjs` 从验收 JSON 生成，检查时校验漂移；各级别定义见 [兼容性实验](docs/guides/compatibility.md)。

<!-- ligamina:phase1:start -->
验收日期：2026-10-12。C01–C15：15/15；G1：**PASS**；G2：**PASS**。

固定代表样本每平台 20 个；以下按级别展示 **通过 / 失败 / 跳过或未执行 / 未知**，分母均为 20。加载与联邦派发通过数分别保留，行为支持只看 L4。

| 级别 | Windows | WSL Linux |
| --- | --- | --- |
| L0 | 20 / 0 / 0 / 0 | 17 / 3 / 0 / 0 |
| L1 | 18 / 2 / 0 / 0 | 15 / 2 / 3 / 0 |
| L2 | 14 / 4 / 2 / 0 | 12 / 3 / 5 / 0 |
| L3 | 14 / 0 / 6 / 0 | 12 / 0 / 8 / 0 |
| L4 | 4 / 0 / 16 / 0 | 4 / 0 / 16 / 0 |

| 生态 | 已通过行为验证的精确插件版本 |
| --- | --- |
| nonebot | nonebot-plugin-autohelp == 0.1.7 |
| nonebot | nonebot-plugin-repeater == 3.1.4 |
| koishi | koishi-plugin-game-mini == 0.4.3 |
| koishi | koishi-plugin-multi-qa == 1.0.8 |

证据：[T18 最终审阅](outputs/acceptance/t18-final-audit-20261012.json)、[Windows 20 样本](outputs/acceptance/t13-representative-windows-full-20261010.json)、[WSL Linux 20 样本](outputs/acceptance/t13-representative-wsl-final-20261010.json)。

稳定性与安装依据：[WSL 24 小时 / 200 恢复循环](outputs/acceptance/t14-stability-linux-wsl-100x24h-20261011.json)、[Windows Sandbox C14](outputs/acceptance/t15-windows-sandbox-c14-20261011.json)、[Linux 容器 smoke](outputs/acceptance/t16-linux-wsl-container-20261010.json)。

范围：虚拟 Gateway、已锁定 Host/profile 和上述行为用例；未连接真实 QQ/其他平台网关。Linux 容器结果是本机 WSL 发起的 smoke。四个版本的支持不能外推为商店全集兼容。
<!-- ligamina:phase1:end -->

## 使用与验证

Windows 从仓库根目录运行：

```powershell
pwsh -NoProfile -File init.ps1 -Mode docs
pwsh -NoProfile -File init.ps1 -Mode full
```

Linux/WSL 使用 `bash init.sh docs` 或 `bash init.sh full`。docs 检查文档/状态/证据摘要；full 复用锁定依赖、测试、构建与 Core + 双 Host smoke。完整环境与按改动检查见 [开发与验证](docs/guides/development.md)。

运行与诊断：

```powershell
cargo build -p ligamina-cli --locked
target\debug\ligamina-cli.exe daemon --config <daemon.json>
$env:LIGAMINA_ADMIN_TOKEN = '<admin token>'
target\debug\ligamina-cli.exe status --url http://127.0.0.1:<port>
target\debug\ligamina-cli.exe doctor --url http://127.0.0.1:<port>
target\debug\ligamina-cli.exe report outputs/acceptance/t13-representative-windows-full-20261010.json
```

status/doctor 只接受 loopback HTTP 地址。凭证来自环境或私人配置；CLI 输出 JSON，日志走 stderr。`report` 与 `lab.report` 支持旧报告摘要及 v1 JSON/text/NDJSON；v1 输入会校验 schema 并重算分母，退出码按所选 gate 返回，见 [输出规范](docs/guides/reporting.md)。

部署见 [Windows bundle](deploy/windows/README.md) 与 [Linux container](deploy/linux/README.md)。Host 配置见 [NoneBot](hosts/nonebot/README.md)、[Koishi](hosts/koishi/README.md)；客户端见 [Reference Client](clients/reference/README.md)。

## 第二阶段

完整 [实施计划](docs/plans/2026-10-12-phase-2-full-catalog.md) 将工作分为 S2-01–S2-12：

1. 冻结官方目录、版本与发布物，单列官方维护子集。
2. 建立版本化输出合同、OS 隔离 Worker 与独立原生基线。
3. 分类配置/服务/行为用例，实现持久调度、分片和断点恢复。
4. 完成双平台 pilot 后扫描完整快照，归因失败并回归。
5. 从完整报告生成矩阵与 README，分别裁决调查完成、自动化闭合和行为兼容。

以下第二阶段摘要由冻结目录、分类 dry run、调度和报告验收记录生成。目录收录、加载成功和行为兼容分别记账；完整支持声明只按已验证的精确范围给出。

<!-- ligamina:phase2:start -->
状态截至 2026-10-12：S2-01–S2-09 pilot 执行已完成；128 个结果位置闭合（通过 / 失败 / 跳过 / 未知：4 / 11 / 88 / 25）。NoneBot Linux、Koishi Linux 和 Windows 各 30 个目录 entry 已进入 L1/L2 pilot。 冻结快照 `p2-41649744e057d61031d5` 含 **5,690 entries**（NoneBot 943、Koishi 4,747；官方维护子集 55）。

| 任务 | 状态 |
| --- | --- |
| S2-01–S2-08 | 已完成并分别保存验收证据 |
| S2-09 | pilot 执行完成：128/128 位置闭合；通过 / 失败 / 跳过 / 未知 4 / 11 / 88 / 25；自动化与行为 gate 保留 blocker |
| S2-10 | 进行中：实现全仓库验证通过；正式 census 9,939/11,380 单元、448/512 shards 闭合；执行中：windows 192/256；全快照未闭合 |
| S2-11 | 未开始 |
| S2-12 | 未开始 |

分类 dry run 只建议 69/5690 entries，5621 项仍是 `unmapped_category`。S2-09 的 128 个双 OS 结果位置全部闭合：四个历史支持版本和 60 个分层目录 entry 分别保留状态；NoneBot Linux 尝试 21 个 Docker Worker（L1 {"passed":21,"failed":0,"skipped":0,"unknown":0}；L2 {"passed":16,"failed":3,"skipped":2,"unknown":0}），Koishi Linux 尝试 22 个 Docker Worker（L1 {"passed":22,"failed":0,"skipped":0,"unknown":0}；L2 {"passed":18,"failed":4,"skipped":0,"unknown":0}），Windows 尝试 22 个 Sandbox Worker（L1 {"passed":22,"failed":0,"skipped":0,"unknown":0}；L2 {"passed":18,"failed":4,"skipped":0,"unknown":0}）。

Windows Sandbox 的 CPython 启动仍受 App Control（Win32 4551）阻止；Node.js 24.20.0 签名 runtime 已用于 Windows Koishi L1/L2 pilot。Koishi Windows Job Object commit P95 6.25 GiB（8 GiB 限额内），scratch P95 136.0 MiB；预算仍 provisional，NoneBot Windows 无 Worker 样本。目录 cohort 缺 entry-bound behavior fixture，L4 未评估，不能外推为兼容。正式 census 暂时闭合 9,939/11,380 个 entry×OS 单元，448/512 个 shard；Linux shard 0/256 为 16/16 单元、shard 1/256 为 24/24 单元、shard 2/256 为 28/28 单元；Windows shard 0/256 为 26/26（通过 / 失败 / 跳过 / 未知：0 / 1 / 22 / 3），shard 1/256 为 22/22（通过 / 失败 / 跳过 / 未知：0 / 2 / 17 / 3）。当前执行 shard：windows 192/256。独立 smoke shard 不计入正式分母。全快照尚未闭合，真实平台登录、发送与投递验收未执行。

证据：[目录快照](outputs/acceptance/phase2-catalog-snapshot-20261012.json)、[分类 dry run](outputs/acceptance/phase2-s206-20261012.json)、[调度验收](outputs/acceptance/phase2-s207-20261012.json)、[S2-07 实际 CLI 计划](outputs/acceptance/phase2-s207-cli-smoke-20261012.json)、[S2-08 报告验收](outputs/acceptance/phase2-s208-20261012.json)、[S2-09 baseline](outputs/acceptance/phase2-s209-baseline-20261012.json)、[S2-09 pilot 清单](outputs/acceptance/phase2-s209-pilot-manifest-20261012.json)、[S2-09 pilot 结果](outputs/acceptance/phase2-s209-20261012.json)、[S2-10 baseline](outputs/acceptance/phase2-s210-baseline-20261012.json)、[S2-10 实现验证](outputs/acceptance/phase2-s210-implementation-20261012.json)、[S2-10 smoke shard](outputs/acceptance/phase2-s210-census-linux-shard0of256-smoke-20261012.json)、[S2-10 正式 Linux shard 0](outputs/acceptance/phase2-s210-census-linux-shard0of256-20261012.json)、[S2-10 正式 Linux shard 1](outputs/acceptance/phase2-s210-census-linux-shard1of256-20261012.json)、[S2-10 正式 Linux shard 2](outputs/acceptance/phase2-s210-census-linux-shard2of256-20261012.json)、[S2-10 正式 Windows shard 0](outputs/acceptance/phase2-s210-census-windows-shard0of256-20261012.json)、[S2-10 正式 Windows shard 1](outputs/acceptance/phase2-s210-census-windows-shard1of256-20261012.json)、[Windows 首次投影 attempt](outputs/acceptance/phase2-s210-census-windows-shard0of256-attempt1-20261012.json)、[S2-10 当前未闭合汇总](outputs/acceptance/phase2-s210-census-interim-20261012.json)、[完整实施计划](docs/plans/2026-10-12-phase-2-full-catalog.md)。
<!-- ligamina:phase2:end -->

状态见 [功能清单](feature_list.json)、[当前进展](progress.md) 和 [会话交接](session-handoff.md)。

## 文档与来源

- [文档导航](docs/README.md)：架构、开发、兼容性、输出和维护指南。
- [历史证据索引](docs/evidence/README.md)：初始决策、MVP 计划与验收报告；旧 NOT READY 保留当时语境。
- [Agent 入口](AGENTS.md)；[项目级 harness-creator](.agents/skills/harness-creator/SKILL.md)。
- 规划依据：[原始讨论](https://chatgpt.com/share/6ac8b4f4-4ad8-83e9-b269-90629f1c7979)；仓库：[Eternal-Wanderer-Vegetable/Ligamina](https://github.com/Eternal-Wanderer-Vegetable/Ligamina)。
