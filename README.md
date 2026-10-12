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

status/doctor 只接受 loopback HTTP 地址。凭证来自环境或私人配置；CLI 输出 JSON，日志走 stderr。当前 report 命令汇总既有验收字段；S2-02 已建立统一 v1 schemas 和 Python/Rust/TypeScript 验证器，CLI/JSON/text/NDJSON 集成仍待 S2-08，见 [输出规范](docs/guides/reporting.md)。

部署见 [Windows bundle](deploy/windows/README.md) 与 [Linux container](deploy/linux/README.md)。Host 配置见 [NoneBot](hosts/nonebot/README.md)、[Koishi](hosts/koishi/README.md)；客户端见 [Reference Client](clients/reference/README.md)。

## 第二阶段

完整 [实施计划](docs/plans/2026-10-12-phase-2-full-catalog.md) 将工作分为 S2-01–S2-12：

1. 冻结官方目录、版本与发布物，单列官方维护子集。
2. 建立版本化输出合同、OS 隔离 Worker 与独立原生基线。
3. 分类配置/服务/行为用例，实现持久调度、分片和断点恢复。
4. 完成双平台 pilot 后扫描完整快照，归因失败并回归。
5. 从完整报告生成矩阵与 README，分别裁决调查完成、自动化闭合和行为兼容。

S2-01 已冻结 snapshot `p2-41649744e057d61031d5`：NoneBot 943 + Koishi 4,747，共 **5,690 entries**；官方维护子集分别为 9 和 46，Koishi 官方页 46 个条目均映射到 registry。NoneBot 一个重复 module/package entry 保留为两条独立记录。可查看[归一目录](catalogs/p2-41649744e057d61031d5/catalog.json)、[raw 来源与 provenance](catalogs/p2-41649744e057d61031d5/provenance.json)、[变化账本](catalogs/p2-41649744e057d61031d5/changes.json)和[验收记录](outputs/acceptance/phase2-catalog-snapshot-20261012.json)。这一步只冻结公开元数据，没有下载或运行插件。

S2-02 建立 [catalog/run/result/summary v1 schemas](contracts/lab/v1/)、共享 golden fixtures、旧状态映射和可复算分母；Python、Rust、TypeScript 消费者使用同一组验证向量。`completed` 仅代表 run 记账生命周期，不是 case 通过状态。该合同尚未接入全量调度或 CLI。

后续将分别展示目录分母、安装/启动结果、行为覆盖率与支持范围，保留 failed/skipped/unknown/flaky 及原因；目录收录和加载成功不等于行为兼容。

下一任务：**S2-03 发布物与依赖锁**。状态见 [功能清单](feature_list.json)、[当前进展](progress.md) 和 [会话交接](session-handoff.md)。

## 文档与来源

- [文档导航](docs/README.md)：架构、开发、兼容性、输出和维护指南。
- [历史证据索引](docs/evidence/README.md)：初始决策、MVP 计划与验收报告；旧 NOT READY 保留当时语境。
- [Agent 入口](AGENTS.md)；[项目级 harness-creator](.agents/skills/harness-creator/SKILL.md)。
- 规划依据：[原始讨论](https://chatgpt.com/share/6ac8b4f4-4ad8-83e9-b269-90629f1c7979)；仓库：[Eternal-Wanderer-Vegetable/Ligamina](https://github.com/Eternal-Wanderer-Vegetable/Ligamina)。
