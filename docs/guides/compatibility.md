# 兼容性实验

适用：插件获取、隔离安装、测试结果与支持声明。依据：T13 runner 与 T18 报告；扩展见 [第二阶段计划](../plans/2026-10-12-phase-2-full-catalog.md)。

## 当前可运行入口

```powershell
uv run --locked --project lab --python 3.12.14 python lab/run.py --suite representative --manifest samples/third-party.lock.json --worker isolated --report outputs/acceptance/representative-local.json
```

追加 --sample-id nonebot-autohelp 可复跑一个样本。清单是 [20 个锁定样本](../../samples/third-party.lock.json)，每生态 10 个；它不是官方商店全集。

S2-05 独立原生配对回归只覆盖四个既有支持版本，可复跑全部或单个样本：

```powershell
uv run --locked --project lab --python 3.12.14 python -m lab.baselines.run_s205
uv run --locked --project lab --python 3.12.14 python -m lab.baselines.run_s205 --sample-id koishi-game-mini
```

它核验原始发布物摘要，在锁定的 Linux profile 中分别执行 stock framework 与生产 Core/Host 路径，并以同一配置、刺激和独立 oracle 比较；完整结果写入 [S2-05 报告](../../outputs/acceptance/phase2-s205-20261012.json)。目录插件只在禁网 Docker Worker 中运行。Windows Sandbox 因 App Control 在插件启动前阻止 CPython；这不替代后续 Windows pilot 验收。虚拟回执仍不代表真实平台投递。

representative 校验发布物哈希、创建单插件 profile、生成依赖锁、运行 DirectWorker 与生产 Core 联邦路径、保存行为 trace。现有 gate 是 C11 的“每生态至少两个 L4”，不能判定全集扫描完成。

## 已有级别

| 级别 | 当前含义 | 结论 |
| --- | --- | --- |
| L0 | 下载发布物并核对来源/摘要 | 已核验发布物 |
| L1 | 精确依赖在临时 profile 中安装 | 此 profile 可安装 |
| L2 | 原生 Host 初始化且测试事件被接受 | Host 加载/派发可运行 |
| L3 | 相同 fixture 进入生产 Core 联邦路径 | 联邦事件路径可运行 |
| L4 | 独立 expected oracle 与两条路径的行为/操作匹配 | 此版本在此场景行为兼容 |
| L5 | 生命周期与资源稳定性 | 对应稳定性窗口通过 |

当前 L2 DirectWorker 仍复用 Ligamina Bridge；它不是完全独立的 stock NoneBot/Koishi baseline。S2-05 已添加独立原生路径及双路径 oracle 比较；旧级别报告仍按其生成时的含义解释。

## 当前支持范围

以 [T18](../../outputs/acceptance/t18-final-audit-20261012.json) supported_plugins 和 T13 fixtures 为准：四个精确版本，Windows 与本机 WSL Linux 有证据。L0–L3 失败、未运行与条件缺失不进入支持名单。

S2-05 在禁网 Linux Docker Worker 对四个精确版本完成 stock/native 与 federated 双路径复核，四项通过；Windows Sandbox 被本机 App Control 阻止于插件启动前，后续 Windows cohort 仍未验收。

Bridge 对复杂平台 API、真实网关、控制台/loader、浏览器、第三方在线凭证的覆盖有限。需要这些能力的插件仍进入全量账本，记录具体限制；不能静默过滤。

## 第二阶段目录口径

- 主集合：冻结时点的 NoneBot 官方商店与 Koishi 官方市场收录插件，包含社区作者插件。
- 官方维护子集：NoneBot is_official、Koishi 官方插件页与可验证仓库所有权；Koishi verified 单列，不能等同官方维护。
- 每个 registry entry 独立记账；同包不同模块/入口保留身份，物理下载可去重。
- 每快照使用一个冻结版本策略，不穷举历史版本；旧支持版本保留回归 profile。框架不适配时保留结果，不自动降级主 profile。
- Adapter、driver、控制台、数据库和服务型插件使用相应探针和独立 profile，不强行用文本回复判定。

## 已冻结目录（S2-01）

从仓库根目录抓取完整 registry 与 Koishi 官方维护页：

```powershell
uv run --locked --project lab --python 3.12.14 python -m lab.catalog.snapshot capture --output-root catalogs
uv run --locked --project lab --python 3.12.14 python -m lab.catalog.snapshot verify catalogs/<snapshot-id>
```

快照保存各来源原始字节、UTC 获取时间、HTTP validators、SHA-256、上游版本（若来源提供）、规范化 entries 和来源切换差异。Koishi `total` 必须等于实际对象数；NoneBot 目录没有总数域，验证其完整 JSON 数组。重复 package entry 保留独立身份。源失败、空响应、结构异常、计数不符或无效 JSON 均拒绝生成快照。

NoneBot 官方维护标记来自 `is_official`；Koishi 官方维护标记来自[官方插件页](https://koishi.chat/zh-CN/plugins/)，与 registry `verified` 分列。快照命令仅读取目录元数据，不下载或执行插件。目录冻结不表示发布物可安装或行为兼容。

## 发布物与依赖锁（S2-03）

对冻结快照运行 `lab.artifacts`，它按 entry 的 `advertised_version` 查询精确版本；不把预发布、撤回或缺包替换成更新版本。Python wheel 和 npm tarball 下载到仓库外的内容寻址缓存，并逐项校验官方 SHA-256 / SRI；sdist 可以固定原始摘要，但构建后端只能在已验收的 OS Worker 内运行。未满足发布物条件的 entry 保留明确终态。

```powershell
uv run --locked --project lab --python 3.12.14 python -m lab.artifacts resolve catalogs/p2-41649744e057d61031d5 --workers 12
uv run --locked --project lab --python 3.12.14 python -m lab.artifacts verify catalogs/p2-41649744e057d61031d5
uv run --locked --project lab --python 3.12.14 python -m lab.profiles resolve catalogs/p2-41649744e057d61031d5 --workers 8
uv run --locked --project lab --python 3.12.14 python -m lab.profiles verify catalogs/p2-41649744e057d61031d5
```

`artifacts.lock.json` 固定每个 registry entry 的版本、release 元数据摘要、按平台选择的档案和缓存路径；`profiles.lock.json` 固定 NoneBot 2.5.0 / OneBot adapter 2.4.6 / Python 3.12.14 或 Koishi core 4.18.11 / Node 24.20.0 的完整依赖图和哈希。uv 解析使用 `--no-build`，npm 使用 `--package-lock-only --ignore-scripts --strict-peer-deps --engine-strict`；依赖锁成功只表示可复现解析，不代表插件安装或行为通过。外部缓存默认在仓库的同级目录 `Ligamina-artifact-cache`，可用 `--cache-root` 显式指定。

## 隔离与差分

现有 `isolated` Worker 仍只隔离 HOME、依赖、目录、凭证和进程，不能替代 OS 沙箱。S2-04 已添加 Docker 容器与 Windows Sandbox CLI Worker：两端都限制输入挂载、网络和资源，采集有上限，超时或 worker 丢失记为 `unknown`，并核验进程树和后端资源清理。实机探针见 [双平台报告](../../outputs/acceptance/phase2-workers-20261012.json)。该验收只运行自有 benign probes，没有执行目录中的第三方插件。

WSL user/mount namespace 探针未获准入：namespace root 可以把只读 bind mount 改成可写。不能以现有 WSL 发行版替代 Docker 隔离；证据见 [WSL boundary report](../../outputs/acceptance/phase2-wsl-userns-boundary-20261012.json)。

Native 与 federated 使用同一发布物、依赖锁、配置和刺激，分开数据目录。两者先满足独立 expected oracle，再比较收件人、操作、消息片段、回执和状态。随机数、时间、外部 HTTP 用注明边界的输入控制；相同错误结果不能通过。

缺少用例写 skipped/missing_fixture，加载结果单列；真实平台收发是独立维度，虚拟回执不代表真实投递。

## 行为 recipes（S2-06）

`recipes/nonebot/recipes.json` 与 `recipes/koishi/recipes.json` 为两个生态分别声明 config、preload、services、loader、command、message、storage、scheduler、adapter、console 的十类 v1 recipe；定义校验合同在 [`recipe.schema.json`](../../contracts/lab/v1/recipe.schema.json)。旧 `*-group-v0.1.json` 是 S2-05 冻结输入，不修改；新增的 [`nonebot-lab-v1.json`](../../samples/profiles/nonebot-lab-v1.json) 和 [`koishi-lab-v1.json`](../../samples/profiles/koishi-lab-v1.json) 声明 recipe 绑定面，标记 `declarative`，不宣称调度器已执行插件。

`lab.recipes.validate_recipe_catalog()` 校验每生态十类 recipe；`prepare_recipe()` 把 registry entry ID、冻结发布物 SHA-256、Host profile、配置 fixture、service fixture 与行为 fixture ID/摘要绑定。配置和服务值不会写入返回的 binding；包含 token、secret、password、credential 或 API key 字段的 fixture 会拒绝。缺少行为/配置/服务 fixture 分别记 `skipped/missing_fixture`、`skipped/missing_config_fixture`、`skipped/missing_service_fixture`；未提供声明所需随机种子、固定时间或外部凭证时，分别记 `skipped/missing_random_fixture`、`skipped/missing_time_fixture`、`skipped/missing_credentials`。未知 registry 标签会保留在 `unmapped_labels`，不静默强行归类。

`lab.compare.compare_recipe_pair()` 要求 Native 与 Federated 各自符合 fixture 的独立 expected oracle、绑定相同 entry/profile/config/services，之后才比较两边观察值。即使两边同样返回错误，也会分别触发 `oracle_mismatch`。显式期望空列表可以通过；缺少 expected 字段会失败。S2-06 只实现 recipe 定义、绑定和比较合同；S2-07 持久化调度队列但没有接入 catalog plugin dispatch。浏览器/真实平台与凭证 profile 仍未验收。

```powershell
uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_recipes -v
```

## S2-09 分层 pilot（已完成）

当前分层清单和汇总入口：

```powershell
uv run --locked --project lab --python 3.12.14 python -m lab.pilot `
  --snapshot catalogs/p2-41649744e057d61031d5 `
  --output outputs/acceptance/phase2-s209-pilot-manifest-20261012.json `
  --legacy-report outputs/acceptance/phase2-s209-legacy-linux-20261012.json `
  --failed-attempt outputs/acceptance/phase2-s209-legacy-linux-attempt1-20261012.json `
  --nonebot-catalog-report outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json `
  --catalog-report outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json `
  --windows-catalog-report outputs/acceptance/phase2-s209-koishi-windows-pilot-20261012.json `
  --acceptance-output outputs/acceptance/phase2-s209-20261012.json
```

这会按 snapshot hash 稳定选取每生态至少 30 entries，加入四个历史精确支持版本，并写出全部 5,690×2 OS 的选取分配账本。当前验收报告中 128 个 pilot 位置终态闭合为 4 passed、11 failed、88 skipped、25 unknown。NoneBot Linux、Koishi Linux/Windows 各自完成 30 项结果闭合；实际有 21/22/22 个 Worker 被派发，其他条目因目录、发布物或 profile 状态而跳过。Koishi Windows 首次清理未知经独立重试闭合。结果只证明发布物依赖安装和 native load 阶段已测，不代表行为兼容。

历史版本配对可用单独输出重跑，避免覆盖 S2-05 原报告：

```powershell
uv run --locked --project lab --python 3.12.14 python -m lab.baselines.run_s205 `
  --report outputs/acceptance/phase2-s209-legacy-linux-20261012.json
```

当前 catalog pilot 没有派发 entry-bound behavior fixture；S2-06 recipes 仍是声明式合同。NoneBot Windows 的 23 个选中 catalog 行受 App Control（Win32 4551）阻止并保持 unknown；Windows Koishi native-load pilot 已运行，但行为仍因缺 fixture 跳过。11 个 native-load 失败的观察和源 hash 见[失败复核](../../outputs/acceptance/phase2-s209-failure-review-20261012.json)：NoneBot 三项是冻结 `htmlrender` profile API 不匹配；Koishi 两平台的四项包括配置入口文件缺失和 `koishi-thirdeye` 类型错误。

资源预算见 [S2-09 provisional config](../../config/lab/phase2-pilot-budget-v1.json)：NoneBot Linux 有 Docker RSS、依赖下载和 scratch 测量；Koishi Linux 有 Docker RSS/scratch，Windows 有 Job Object 峰值 commit 内存与 Sandbox Workspace scratch。Koishi Windows Job Object P95 为 6.71 GB，最大 8.57 GB，Worker 限额为 8 GiB；这不是 RSS 指标。NoneBot Windows 没有 Worker 样本，因为 CPython 被 App Control 阻止。预算保持 provisional，不能据此宣称目录全集兼容。报告 attempt 和 checkpoint 保留在 `outputs/acceptance/`，重建汇总前应先复核完整的 30 项平台报告。

## S2-10 全快照 census（进行中）

S2-10 在 S2-09 commit `20d8f04` 上的 clean baseline 已通过，见 [baseline](../../outputs/acceptance/phase2-s210-baseline-20261012.json)。执行器按 `entry×OS` 稳定分 shard，并用 SQLite stage queue 记录 prepare/install/native/federated/compare/cleanup；报告绑定目录、artifact/profile lock、阶段结果和清理证据。相同 queue DB 和 run ID 可加 `--resume` 恢复。隔离 smoke 见 [报告](../../outputs/acceptance/phase2-s210-census-linux-shard0of256-smoke-20261012.json)：16 单元、96 jobs 终态（4 failed、12 skipped），不并入正式汇总。

每个 OS 分 256 shard，逐片保留报告；全部完成前不要聚合：
```powershell
$queue = '.work/catalog-census/phase2-s210.sqlite3'
uv run --locked --project lab --python 3.12.14 python -m lab.census `
  --snapshot catalogs/p2-41649744e057d61031d5 --queue-db $queue `
  --platform linux --shard 0/256 --report outputs/acceptance/phase2-s210-census-linux-shard0of256.json
```
Windows 使用 `--platform windows`。NoneBot CPython 被 App Control Win32 4551 阻止时记为 `unknown`；缺少 entry-bound fixture 的 federated/compare/behavior 记为 `skipped`，native load 和 cleanup 保留实测状态。

齐备两 OS 的 512 份报告后聚合；校验包含 shard 哈希、归属、重复/遗漏单元和 artifact/profile identity，并输出双 OS per-plugin 报告及 evidence manifest：
```powershell
$shards = Get-ChildItem outputs/acceptance -Filter 'phase2-s210-census-*-shard*of256.json' | ForEach-Object FullName
uv run --locked --project lab --python 3.12.14 python -m lab.census --snapshot catalogs/p2-41649744e057d61031d5 `
  --aggregate @shards --aggregate-output outputs/acceptance/phase2-s210-20261012.json
```
Smoke、report-only、不同 shard count 或未完成报告不能替代 512 份正式报告，`queued` 也不等于 Worker 已执行。全量 census 仍未关闭；S2-09 provisional budget 待用 census 资源数据更新。
