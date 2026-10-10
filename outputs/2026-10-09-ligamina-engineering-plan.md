# Ligamina MVP 工程执行计划

日期：2026-10-09。执行状态更新于 2026-10-10：T01–T17 的实现与 Windows/本机 WSL 阶段性验证已推进；G1 技术闭环通过。T13 Windows 20 样本 gate 通过；两个 WSL Koishi L4 候选单独重试通过，完整 20 样本 WSL Linux 矩阵仍在运行。T14 的 WSL 100 次/Host、24 小时稳定性运行正在进行，资源验收尚未裁决。T15 的本机 Windows x64 bundle 升级、交互式首启、Core/双 Host、停机清理和默认卸载数据保留均通过；干净 VM 与真实第三方插件验收未完成。T16 Linux 容器 C15 smoke 在本机 WSL 环境通过；GitHub `CI #2`、`CI #3` 的 Windows/Linux checks、G1 smoke 和 Linux 镜像构建均通过；手动代表扫描 run `38025466166` 的 Ubuntu/Windows jobs 各完成 20 个样本且 gate PASS（每生态 L4 各 2）。T18 未完成，G2 保持 `NOT READY`。前置依据：用户已核实通过[初步计划](2026-10-09-ligamina-initial-plan.md)。范围以 [C01–C15 清单](2026-10-09-ligamina-mvp-checklist.md)为准；接口草案见[配套文档](2026-10-09-ligamina-mvp-contracts.md)。

## 1. 技术选择与工程结构

采用 Rust Core、Python NoneBot Host、TypeScript Koishi Host。开发基线为本机已确认的 Rust 1.97.1、CPython 3.12.14、Node 24.20.0；发布 runtime 的来源、许可和哈希由 T01/T15 固定。依赖初始候选已核对发布元数据，尚未安装或进行联合运行。

| 依赖 | 初始精确候选 | 使用位置 |
|---|---|---|
| nonebot2 | 2.5.0 | Python Host |
| nonebot-adapter-onebot | 2.4.6 | NoneBot v11 原生事件/Bot |
| nonebug | 0.4.4 | 原生基准及测试，不进入用户 runtime |
| koishi / @koishijs/core | 4.18.11 | Node Host |
| @koishijs/plugin-mock | 2.6.6 | 原生基准，不用它冒充正式桥接 Adapter |
| rmcp | 3.5.1 | Rust MCP 接入；明确限制首版协议 |
| @modelcontextprotocol/sdk | 1.32.1 | 独立验收客户端 |

其余 Rust/JS/Python 依赖在 T01 一次解析后纳入锁文件。不得在 CI/用户启动时追随 `latest`。候选组合失败时，在 T01 记录最小调整、原因和重跑结果；后续只使用通过的组合。

建议具体目录：

```text
Cargo.toml / Cargo.lock / rust-toolchain.toml
crates/ligamina-contracts/     wire、配置、错误、状态及共享类型
crates/ligamina-core/          registry、broker、router、policy、store、supervisor
crates/ligamina-cli/           daemon、管理命令、MCP、安装与诊断入口
hosts/nonebot/                 pyproject.toml、uv.lock、ligamina_nonebot/
hosts/koishi/                  package.json、package-lock.json、src/
clients/reference/            独立 TypeScript 客户端与 MCP 验收
contracts/v0.1/                schema、正反例、兼容配置档
lab/                          Python Runner、虚拟网关、差分、故障与报告
samples/                      自制原生插件、第三方样本锁定清单与夹具
deploy/windows/               安装/更新/卸载脚本和 runtime bundle 清单
deploy/linux/                 Dockerfile、compose.yaml、数据卷说明
scripts/                      跨平台开发/验证入口
.github/workflows/            Windows/Linux 构建和分层测试
docs/adr/                     持久化、授权、路由、协议及工具链决定
outputs/                      规划及用户交付；正式运行报告单列目录
```

初步计划的 `core/` 收敛为 Rust workspace；`contracts/` 保留语言无关规范；`catalog/` 的首版工作收敛为 `samples/third-party.lock.json`。目录为待创建路径，本次没有放置空壳业务实现。

## 2. 模块 Interface 与测试位置

| Module | 最小 Interface | Implementation 集中的行为 | 接口级验证 |
|---|---|---|---|
| Runtime | `apply_desired_set`, `status`, `shutdown` | 生命周期、代次、注册撤销和恢复 | 黑盒启停、失败恢复、退出清理 |
| HostSession | `request`, `close` | 帧解析、关联、超时、取消、反向操作请求 | Python/Node 测试 Worker；畸形帧、乱序回复 |
| CapabilityBroker | `list`, `invoke` | schema、权限、代次、调用链与错误 | 公开客户端与跨宿主夹具 |
| EventRouter | `dispatch` | 身份、路由、去重、队列与处理状态 | 同值 ID 隔离、冲突、过载、重复事件 |
| PlatformGateway | `execute`, `lookup` | 授权、落账、回执、未知结果与幂等 | 受控发送网关故障矩阵 |
| Deployment | `launch`, `stop_tree` | Windows Job Object/Linux 进程组清理 | 本地及 Linux 容器内同一流程 |
| CompatibilityRunner | `run_case`, `compare`, `report` | 双环境、预期、归一化和证据 | 注入反例必须检测失败 |

复杂实现留在模块内部；Client/Host Adapter 不需要了解 SQLite 表、进程创建细节或原生宿主私有全局状态。两个 Host 和两个执行环境真实使用同一 Interface，避免提前为尚不存在的分布式部署扩大 Interface。

## 3. 执行任务表

任务按依赖推进。每项关闭应包含实现、接口级测试和证据；只提交 schema 或空目录不能关闭相应任务。

| ID | 工作包与目标文件 | 前置 | 必须验证的结果 | 清单映射 |
|---|---|---|---|---|
| T01 | 固定工具链/依赖；`rust-toolchain.toml`、两 Host 锁文件、`docs/adr/0001-baseline.md`；核对 runtime 分发许可 | 无 | Windows/Linux 精确候选可安装、原生最小插件启动、rmcp 与 TS SDK 在 2025-11-25 互通；锁定哈希；若需要更换候选附证据 | C01、C03、C07 |
| T02 | 定稿 `contracts/v0.1/` 和 `ligamina-contracts`；正反例由 Rust/Python/TS 共用 | T01 | 大整数 ID、群/私聊、版本不兼容、缺字段、非法消息段、不支持操作、operation ID 冲突有确定结果 | C02、C04、C09 |
| T03 | `ligamina-core/transport/` + 测试 Worker；JSON-RPC 风格 NDJSON stdio | T02 | 双向并发请求无关联错误；stderr 噪声不入协议；1 MiB 帧限、EOF、坏帧、超时、取消和迟到响应被处理 | C02、C08 |
| T04 | `supervisor/`、`deployment/local.rs`；Host 状态、代次、进程树和重启预算 | T03 | 未启用 Host 零进程；初始化失败不激活；终止后旧代次不可写；一边崩溃另一边工作；Core 退出清理子孙进程 | C08 |
| T05 | `store/`、`policy/`、`platform/`；SQLite 迁移、grant、操作账本、客户网关 lease | T02、T04 | 发送前持久化；相同 ID 不重复执行；不同载荷冲突；发送后丢回执/重启得到 unknown；越权与旧 lease 拒绝 | C05、C09 |
| T06 | `router/` + `clients/reference` 管理/事件/网关模式；虚拟平台发送与历史查询 | T05 | 单一 Host 路由；重复事件不重分发；不同会话有独立队列；过载拒绝；接受事件与发送回执分开返回 | C04–C06 |
| T07 | `lab/native_runner.py`、`lab/compare.py`、`samples/fixtures/`；确定事件夹具和预期 | T02、T06 | 两环境版本/配置一致；对结果同错、错收件人、重复回复、消息段丢失、漏清理的注入反例判失败 | C10、C13 |
| T08 | `hosts/nonebot/ligamina_nonebot/{adapter,lifecycle,worker}.py`；原生 v11 BridgeAdapter | T03–T07 | `json_to_event → Bot.handle_event` 路径保留 reply/at/nickname 预处理；驱动 startup/shutdown 真正运行；`get_msg` 与发送 API 经网关；不能用直接 Matcher 调用代替 | C03–C06、C08 |
| T09 | `hosts/koishi/src/{adapter,lifecycle,worker}.ts`；虚拟 Bot、原生 Context 与存储配置档 | T03–T07 | Session 派发、命令 authority、原生插件依赖实际生效；群/私聊/quote 映射正确；数据库夹具重建后数据仍可用；未知 internal API 明确失败 | C03–C06、C08 |
| T10 | `broker/` + 两 Host 侧车能力映射与 `samples/fixtures/cross-host/` | T08、T09 | 每边纯函数 schema 正确；Host A→Broker→B 正常及超时/断连；循环调用上限；能力仅对活跃代次可用 | C07、C08 |
| T11 | `ligamina-cli/mcp.rs` + 独立 TS MCP 客户端 | T01、T10 | legacy initialize/initialized、tools/list、tools/call、取消、工具错误和能力变化通过；未经 grant 不能扩大消息权限；不宣称 2026 协议 | C07 |
| T12 | 联邦总体验收脚本 `lab/run.py --suite smoke`，故障夹具与清理检查 | T07–T11 | 完成 G1 全链路；自动保存事件/请求/操作/回执与环境指纹；故意破坏一个环节时退出非零 | C01–C10、C13 |
| T13 | `samples/third-party.lock.json` 与分层样本夹具；一次性 Worker 包装 | T12 | 20 个唯一真实样本、每生态至少 10；原生基准失败正确归因；至少每生态 2 个 L4 通过（含消息插件）；包业务源码无修改 | C03、C10、C11 |
| T14 | `lab/stability.py`、`lab/report.py`、CLI `status/doctor/report` | T12、T13 | 100 次/Host 重建、24 h 组合运行、资源/恢复预算通过；退出后无进程；报告分母和 skipped/unknown 正确 | C08、C12、C13 |
| T15 | `deploy/windows/`；runtime manifest、`install.ps1/update.ps1/uninstall.ps1`、可恢复环境目录 | T01、T12 | 依赖安装或候选更新失败不激活；配置/数据独立；清单哈希与许可附带；宿主按需下载/安装；不改系统默认 Python/Node | C01、C14 |
| T16 | `deploy/linux/`；单容器 Core+可选 Host，Compose profiles、healthcheck 与持久卷 | T01、T12 | 所有关键契约复用同一适配器；卷保留数据库与插件状态；停容器清理进程；默认端口只绑定本机，无 Docker socket | C15 |
| T17 | `.github/workflows/` + `scripts/check.ps1/check.sh`；锁定构建、分层 CI | T02、T12、T15、T16 | PR 跑契约/G1/两 OS 构建；第三方深测在受限 Worker；定时深测使用精确版本；日志和报告可追溯 | C01、C10、C13 |
| T18 | 干净 Windows VM + Linux 交付验收，`outputs/acceptance/` 报告与支持名单 | T13–T17 | 实际执行 C01–C15；全部必选证据齐全，Windows 无预装运行时完成安装/更新失败恢复/卸载；未通过保持 NOT READY | C01–C15 |

任务依赖中的 T03–T07、T07–T11 等表示该范围内全部任务完成。代码执行时按依赖推进，不将同时可做的工作误当作无需前置的工作。

## 4. 关键实现约束

### 宿主适配

NoneBot Host 使用原生 v11 Adapter/Bot/Event。桥接 Adapter 覆盖平台调用，不启动真实 OneBot 网络连接；保留原生事件解析和 Bot 预处理。原生 `Bot.handle_event` 会处理回复/提及再交给 Matcher，这条链不能省略。[版本化 Bot 源码](https://github.com/nonebot/adapter-onebot/blob/v2.4.6/nonebot/adapters/onebot/v11/bot.py)

驱动整合先由 T01/T08 实验验证，优先使用原生无网络 driver 的生命周期；若必须调用非公开生命周期入口，将其限制在一个版本锁定的 Adapter 内，并用 startup/shutdown 夹具守住。NoneBot 停用采用启用集合重建，不靠删除 `sys.modules` 假装完整卸载。

Koishi Host 用正式 BridgeBot/Adapter 接收公共事件并发送受控平台请求。原生基准可用官方 Mock；正式路径不能用 Mock 代替桥接 Adapter，否则差分会漏测生产转换。`bot.session`、`bot.dispatch`、Context 的依赖及生命周期分别由原生机制负责。[Bot Interface](https://koishi.chat/en-US/api/core/bot)、[Context Interface](https://koishi.chat/en-US/api/core/context)

### 事件、能力与平台操作

MVP 配置档只定义必要操作：消息发送、消息查询、当前账号信息。桥接 API 外的访问失败为 `unsupported_api`；不返回空成功对象。OneBot v11 数字 ID 限制在其支持范围，Koishi/公共协议保留字符串；不把任意超大字符串 ID强转为数字。

消息事件显式绑定一个 handler Host；消息处理未回复也不自动转交第二 Host。跨框架共享消费链延后，从而避免无法证实的 `handled=false` 造成二次发送。同 Host 的多个插件继续遵守原生优先级和阻断语义。

能力映射由侧车声明绑定明确模块/导出函数或原生服务桥接，不执行自然语言推断。跨宿主夹具不修改既有插件业务源码；工具调用与消息操作都经过 Broker/Gateway 的 Interface。

### 持久化和恢复

建议 SQLite 表：`plugin_installations`、`desired_hosts`、`host_epochs`、`gateway_leases`、`event_receipts`、`operation_ledger`、`schema_migrations`。能力是活跃 Host 的运行状态，重启后重新握手注册，不从旧数据库记录直接恢复为可用。

Core 启动先增加运行代次、使旧 lease 失效，并把未确定完成的外部操作标成 unknown。事件已受理但 Core 崩溃时不自动重放，客户重试同 ID查询其状态；无法证明结果时明确 unknown。提供显式重新投递入口时必须生成新事件 ID并说明可能重复。

Host 重启只重载期望启用集合；客户端网关离线时不发送、不缓存待上线群发。平台副作用需要有效 lease 与 grant；迟到响应不能激活旧能力或覆盖新代次状态。

### 权限与部署

MVP 外部身份是 Client/Host，grant 限定账号、会话和操作。插件级归属仅做审计；同 Host 内代码不能被认为相互隔离。定时任务只有预配置发送目标才有后台 grant，不继承某条历史消息的权限。

Windows 的受管进程树使用 Job Object 或同等已验收方法，Linux 使用进程组；不能只终止一级 Host 并留下浏览器等子进程。自制故障夹具必须包含孙进程。

Linux 进程组主要保证正常 stop 的整体清理，单靠它不能保证 Core 被 SIGKILL 后清理。首版 Linux 交付以容器 PID namespace 的整体结束覆盖异常退出；若增加原生守护进程交付，必须由 systemd/cgroup 等受验收的外部管理方式补齐异常清理，不能只依赖 Host 读取 stdin EOF。

每个 Host profile 有独立锁定依赖环境与数据目录，同一 profile 内的插件共享环境。冲突依赖在安装阶段拒绝或分配到另一个独立 Host profile；不在运行中覆盖共享库版本。样本原生/联邦环境彼此独立，避免全局注册与数据库污染比较。

一个非强制 Docker 的本地 Deployment Adapter 覆盖 Windows/Linux；Compose 初版仅包装 Linux 单机部署，容器内仍走同一 stdio Host 代码。远程 Host 是后续第二 Adapter，当前不创建只有占位方法的分布式模块。

## 5. 验证命令与退出条件

下列入口分为已实现验证命令和后续部署命令。T15 有初版 Windows 安装脚本，T16 Linux Compose 已由 WSL 实测；T17 的 Windows/WSL 检查入口和 CI 工作流已实现并完成本地检查。托管 CI 执行证据及 T15 干净系统验收仍待完成。

```powershell
uv run --project lab python lab/run.py --suite smoke --platform windows
uv run --project lab python lab/run.py --suite representative --manifest samples/third-party.lock.json --worker isolated
uv run --project lab python lab/stability.py --cycles-per-host 100 --duration-hours 24
target/debug/ligamina-cli report outputs/acceptance/<report>.json
bash deploy/linux/docker-wsl.sh compose --profile nonebot --profile koishi up --build -d
```

跨平台检查入口为 `pwsh -NoProfile -File scripts/check.ps1` 和 `bash scripts/check.sh`；工作流位于 `.github/workflows/ci.yml` 与 `.github/workflows/representative.yml`。Linux 容器入口现为 `deploy/linux/docker-wsl.sh` 与 `compose.yaml`；停止时要包含启动时启用的 Compose profile，例如 `bash deploy/linux/docker-wsl.sh compose --profile nonebot --profile koishi down`。

脚本成功为 0；契约/行为失败为 1；环境不足为 2；报告保留各测试状态。代表样本中上游失败允许作为调查结果完成扫描，但支持名单和 G2 单独裁决；`--require-gate G2` 缺任一必选证据即非零。

关键测试场景：

| Suite | 最低场景 |
|---|---|
| wire | 版本、必填字段、帧大小、并发请求、双向重入、坏帧、EOF、cancel、迟到回复 |
| identity | 群/私聊同值 ID、不同账号相同用户 ID、伪造 grant、未经授权改变目标 |
| native/federated | 私聊/群聊命令、at、reply/get_msg、原生阻断、依赖缺失、startup/shutdown、定时任务 |
| operation | 发送前失败、发送后丢回执、重复 ID相同/不同载荷、Core/网关重启、旧 lease/epoch、无网关 |
| lifecycle | 初始化中途失败、两 Host 隔离、100 次重建、Core 崩溃及恢复、子孙进程清理、存储保留 |
| differential-negative | 两边同错、错误收件人、重复回复、丢消息段、残留能力；均必须被识别 |
| packaging | 干净系统、中文/空格路径、缺失/篡改包、离线缓存、候选更新失败、用户数据保留 |

稳定性默认参数：正常受控环境下 Host 失效后 5 秒内停止对外提供能力；健康恢复后 30 秒内重新可用；正常停止 10 秒内完成，随后强制清理有明确状态。超时可配，参考环境和默认值写入报告。

资源门槛在 T12 小规模基线后、T14 长测前固定为 `lab/budgets.json`：采样每 60 秒，预热 30 分钟；每进程最后 30 分钟 RSS 中位数相对预热后首个 30 分钟增长不超过 `max(20 MiB, 10%)`；注册数严格不增长。保留整条曲线，持续斜率异常仍需调查。该阈值是初始工程预算，不是性能实测承诺；调整必须记录原因，不能在失败后静默放宽。

## 6. 执行批次与完成裁决

| 批次 | 任务 | 批次完成证据 |
|---|---|---|
| A 契约/骨架 | T01–T03 | 锁文件、版本化 schema、真实双向跨语言通信与协议反例 |
| B 管理/网关/实验室 | T04–T07 | 代次、进程清理、操作账本、路由、差分判定反例 |
| C 原生宿主 | T08–T09 | 两生态分别经过原生生命周期和真实桥接路径 |
| D 联邦闭环 | T10–T12 | 独立 MCP 客户端、跨 Host 调用、故障恢复；G1 裁决 |
| E 样本/交付 | T13–T17 | 20 样本、长测、两 OS 包装与 CI 证据 |
| F 最终验收 | T18 | C01–C15 逐项索引、支持名单、G2 verdict |

每个工作包的变更记录写清：文件、行为、Interface 变化、测试命令、证据及遗留问题。没有提交历史时先建立可审阅本地变更；提交和远端发布按后续实际授权进行，不作为本轮规划工作的默认动作。

T01 中三个高风险实验优先：无网络 NoneBot 生命周期与 v11 reply 预处理；Koishi 原生 Bot + 数据存储 + 依赖激活；rmcp/TS SDK固定协议互操作。它们各自失败时，调整对应 Adapter 或版本，不改写初步计划的项目独立性、原生宿主和证据化兼容目标。

当前按依赖推进：T13 Windows gate 通过；两个 WSL Koishi L4 候选重试通过，完整 WSL 20 样本矩阵重跑中。T14 的 WSL 100 次/Host、24 小时稳定性任务运行中。T15 已在本机 Windows 完成新 bundle 更新安装、交互首启、Core/双 Host readiness、进程清理和默认卸载数据保留；C14 仍缺干净 VM 与真实第三方插件运行。T16 Linux 容器在本机 WSL smoke 通过。T17 本地检查通过，GitHub `CI #2`、`CI #3` 的双 OS checks、G1 smoke 和 Linux 镜像构建通过；手动代表样本扫描 run `38025466166` 的 Windows/Linux jobs 各完成 20 个样本且 gate PASS（每生态 L4 各 2）；PR 事件验证待完成。随后由 T18 汇总干净系统验收。T12 联邦 smoke 满足 G1 四项闭环；G1 不代替 G2 裁决。用户已确认的初步方向不需要再次询问。待确定事项由实验与记录关闭，只有涉及新增业务范围或具体外部权限的事项才需要用户输入。

## 7. 执行记录（2026-10-10）

本节记录代码工作区的实际进度；任务表中的验收条件仍是裁决依据。

| 任务 | 实际状态与证据 |
|---|---|
| T01–T03 | 已完成；工具链、锁文件、契约、跨语言双向 stdio 与 MCP 互操作证据见 [ADR 0001](../docs/adr/0001-baseline.md)。 |
| T04 | 已完成；Windows Job Object、WSL 进程组、代次隔离、故障恢复及孙进程清理均通过工作区测试。 |
| T05 | 已完成；SQLite grant、lease、操作账本和 unknown 恢复；Windows/WSL 各通过 3 个平台存储集成测试。 |
| T06 | 模块级完成；SQLite v2 事件回执、单 Host 路由、会话串行/有界并发、过载拒绝、参考客户端与虚拟平台；T14 后的完整 Rust 工作区套件在 Windows/WSL 各通过 27 项测试。Core HTTP 事件/状态接口已由生产 daemon 接通并经联邦 smoke 实测；在线修改期望 Host 集合尚未暴露管理路由。 |
| T07 | Runner/Compare、锁定环境指纹、单群聊回复夹具及负例检测已实现；Windows/WSL 各通过 9 个 Python 接口测试。独立 Worker 实际计算 CPython 版本与锁文件/配置哈希，并验证正向输出与同错、错误收件人、重复回复、丢消息段和清理残留注入。自编 NoneBot/Koishi 夹具已走原生和跨 Host 路径；代表性第三方插件差分仍待 T13。 |
| T08 | 已完成；NoneBot v11 原生 `json_to_event → Bot.handle_event` 路径、无网络 driver 生命周期、reply/at/nickname 预处理、`get_msg`/发送网关、双向 Worker 和 `service.invoke` 均由 Windows/WSL 验证。 |
| T09 | 已完成；Koishi 原生 Session/Context、命令 authority、SQLite 持久化、群/私聊/quote 映射、受限 API 与双向 Worker 均由 Windows/WSL 验证。 |
| T10 | 已完成模块及双 Worker 集成验证；新增跨语言纯函数 Capability Descriptor schema、原子代次快照注册、参数/结果校验、deadline、断连结果丢弃、调用链循环与深度上限。实际测试启动 NoneBot 和 Koishi Worker，由 NoneBot 原生 Matcher 发起 `service.invoke`，Rust Broker 路由至 Koishi `capability.invoke`，结果 42 经平台网关回复；另有超时、断连、旧代次与递归负例。该模块级测试仍保留；T12 又用生产 Core daemon 完成了 Host 生命周期和 HTTP/MCP 路由联调。 |
| T11 | 已完成适配器与独立客户端验证。Rust MCP 端点只声明 `2025-11-25`，支持 legacy 初始化、动态纯能力 `tools/list`、schema 校验的 `tools/call`、取消、工具级执行错误和能力变化通知。Broker 拒绝 `pure=false` 描述符，因此消息发送能力不会通过 MCP 被扩权。独立 TS SDK 1.32.1 客户端已连真实 CLI stdio 并完成 ping；Rust 协议夹具覆盖能力调用、取消和 `tools/list_changed`。T12 另由同一 SDK 连 Core 的 Streamable HTTP `/mcp`，独立调用两 Host 能力。 |
| T12 | 已完成；新增 `CoreRuntime`、loopback HTTP daemon、Bearer 授权管理/事件/能力/调用/操作状态/网关 lease-claim-result 路由及 MCP Streamable HTTP。Host 能力快照随监督代次登记和撤销，Host-originated `service.invoke` 与 `platform.execute` 已接到 Broker/Gateway。`lab/run.py --suite smoke` 在 Windows 和本机 WSL 各通过：两 Host 原生插件收到群/私聊共 4 个事件、经虚拟平台网关产出 4 个回执；独立 TS MCP SDK 调用两 Host；强杀 NoneBot 后能力撤销且 Koishi 仍成功；错误收件人注入被 Compare 识别；graceful shutdown 后无 Host PID。报告见 [`Windows`](acceptance/t12-smoke-windows-corrected-20261010.json) 和 [`WSL Linux`](acceptance/t12-smoke-linux-corrected-20261010.json)。虚拟网关与自编插件只证明 G1 技术闭环，不证明实际 QQ/平台网络收发或第三方插件兼容性。 |
| T13 | Windows 完成 20 个唯一样本（NoneBot 10、Koishi 10），4 个 L4 通过（每生态各 2，含消息插件），`gate=PASS`；见 [`Windows 全量报告`](acceptance/t13-representative-windows-full-20261010.json)。WSL Ubuntu 24.04.5 初次全量扫描记录 20 个样本，18 个 L0 artifact 校验通过，报告为 `gate=NOT READY`：Koishi 的两个且仅有两个 L4 候选 `koishi-game-mini`、`koishi-multi-qa` 在下载 npm tarball 时超时；当时 Linux L4 计数为 NoneBot 2、Koishi 0。之后在本机 WSL 对这两个候选单独重试均通过 L4（2/2）；另启动完整 20 样本 WSL 矩阵重跑，结果尚未产出。首次报告见[`WSL 全量结果`](acceptance/t13-representative-wsl-full-20261010.json)，首次重试见[`WSL L4 候选重试`](acceptance/t13-representative-wsl-l4-retry-20261010.json)，网络证据见[记录](acceptance/t13-wsl-network-limitation-20261010.md)。GitHub hosted Ubuntu 的手动代表扫描另已完成 20 样本且 `gate=PASS`（NoneBot/Koishi L4 各 2），仅作托管 CI 证据，不代替按用户要求的本机 WSL Linux gate。 |
| T14 | 实现已完成：`lab/stability.py` 执行生产 Core 的双 Host 100 次/Host恢复循环、能力撤销/恢复检查、24 小时资源监控；`lab/report.py` 分开统计期望/已执行分母并保留 skipped/unknown；CLI 提供 loopback `status`、`doctor`、`report`。Supervisor 并行停止 Hosts；修复 NoneBot reader 与 Koishi readline 在 `host.shutdown` 后遗留进程的问题。Windows/WSL Rust 工作区各 27 项通过；两端 Koishi 各 6 项、Runner 各 17 项、NoneBot Bridge 原生探针通过；Windows/WSL Worker 退出探针分别确认两个 Host 均在 0.1 秒内正常退出。生产 Core 快速恢复探针各 2/2 通过、CLI 与清理通过，但因短测资源窗口不足，资源状态为 `skipped`、报告为 `NOT READY`（[`Windows`](acceptance/t14-stability-windows-quick-20261010.json)、[`WSL Linux`](acceptance/t14-stability-linux-wsl-quick-20261010.json)）。WSL 100 次/Host、24 小时长测已启动，运行中；最终资源预算及 T14 仍未通过裁决。 |
| T15 | Windows x64 runtime bundle 与版本化安装/更新/卸载已推进，见 [`deploy/windows`](../deploy/windows/README.md)。uv 0.12.5 与 Node 24.20.0 固定 SHA256；CPython 3.12.14 由 uv 托管安装到应用目录并关闭注册和可执行 shim。真实 Windows 中文/空格路径更新安装遇到并修复三个问题：release bundle 漏带 `contracts/v0.1`、旧安装将 FastAPI extra 放在开发依赖、`File.Replace` 不能接收空备份路径。修复后更新安装成功，首启向导经 `run.ps1` 交互选择 NoneBot/Koishi，生成 loopback-only 配置、随机凭据、独立用户数据目录且无 operation grants；Core readiness 和认证状态接口显示双 Host 均 active、无重启/错误。另以仅包含 Windows 系统目录的 PATH 完成一次全新安装与首启，证明 Core/两个 Host 使用随包运行时；HTTP 有序停机无残留进程；默认卸载删除应用目录并保留配置和 SQLite 数据。详见[`Windows runtime 探针`](acceptance/t15-windows-runtime-probe-20261010.json)、[`Windows 首启验收`](acceptance/t15-windows-first-run-probe-20261010.json)和[`PATH 隔离验收`](acceptance/t15-windows-install-path-isolation-20261010.json)。GitHub CI #5（`422aba9`）也在 Windows runner 通过 fresh install、无系统 Node/Python 的 PATH 隔离启动、双 Host 状态、graceful shutdown 和默认卸载数据保留（Windows artifact `11660828369`）；这不是干净 VM。C14 仍未通过：干净 VM 和真实第三方插件运行尚缺。 |
| T16 | 已完成 C15 Linux 单容器 smoke；本机 WSL 启动 `linux/amd64` 镜像，Core/两个 Host 健康、单 Host profiles、HTTP 能力调用/事件、loopback 发布、具名卷重建持久性及停机清理均通过。运行报告见[`T16 WSL 容器报告`](acceptance/t16-linux-wsl-container-20261010.json)。真实平台 Gateway 仍未连接；容器由 Docker Desktop `desktop-linux` 引擎执行，WSL 原生测试与命令由 Ubuntu 24.04.5 发起。 |
| T17 | 本地检查通过：新增 `scripts/check.ps1`、`scripts/check.sh`、`.github/workflows/ci.yml` 和 `.github/workflows/representative.yml`。Windows 脚本退出码 0，包含 Windows runtime bundle 构建与 G1 smoke；Ubuntu 24.04.5 WSL 脚本退出码 0，包含 Rust/Python/Node 检查与 G1 smoke。两端分别通过 3 个 NoneBot pytest、17 个 Runner unittest、6 个 Koishi 测试、1 个 MCP interop、5 个参考客户端测试、28 个 Rust 测试及 G1 smoke（4 个事件、4 个 gateway receipt、MCP 成功、无残留 Host PID）。Bash/PowerShell/YAML 静态解析通过；Action 使用完整 commit SHA 固定。详见[本地 CI 检查报告](acceptance/t17-local-ci-checks-20261010.json)及[Windows smoke](acceptance/ci-smoke-windows.json)、[Linux smoke](acceptance/ci-smoke-linux.json)。初始推送触发 GitHub `CI #1`；Windows job 因默认 CRLF checkout 改变锁文件和 profile 的 SHA-256 而失败。新增 `.gitattributes` 固定仓库文本为 LF；Windows `core.autocrlf=true` checkout 模拟复现旧失败，修正后 Runner 17 项全通过。修正已由 `b529686` 推送；GitHub `CI #2` 的 Windows checks、Ubuntu checks、G1 smoke、Linux 镜像构建均通过。手动触发的代表样本扫描 run `38025466166` 中，Ubuntu 与 Windows jobs 各完成 20 个样本，NoneBot/Koishi L4 各 2，两个 gate 均 PASS；报告 artifact ID 分别为 `11659218369` 和 `11660216202`。`CI #3`（`f515ab9`）的 Windows/Linux checks、G1 smoke、Linux 镜像构建均通过；证据 artifact ID 为 `11660756355`（Windows）和 `11660366352`（Linux）。CI #4（`0502e52`）的 Windows/Linux checks、G1 smoke 与 Linux 镜像构建均通过。新增 `scripts/windows-install-acceptance.ps1` 验证 bundle fresh install、无系统 Node/Python 的 PATH 隔离启动、双 Host 状态、graceful shutdown 和默认卸载数据保留；本机同一验收脚本通过。GitHub `CI #5`（`422aba9`）的双 OS checks、G1 smoke、Linux 镜像构建及 Windows 安装验收均通过，Windows/Linux evidence artifact ID 分别为 `11660828369` 和 `11660787901`；报告见[PATH 隔离验收](acceptance/t15-windows-install-path-isolation-20261010.json)。schedule 与 workflow_dispatch 均已配置；T17 尚待 PR 事件验证。 |
| T18 | 未完成；干净 Windows VM、真实第三方插件、T13 本机 WSL Linux gate、T14 长测结果及最终 C01–C15 汇总仍待裁决。本机 Hyper-V 管理服务在运行，但当前会话查询 VM host 时权限不足，且未发现 Windows Sandbox 可执行文件，因此尚未启动干净 VM。 |

**G1：PASS（2026-10-10，Windows + 本机 WSL）。G2：`NOT READY`**；T13 本机 WSL Linux 20 样本 gate 重跑、T14 24 小时资源门槛、T15 干净 VM 与真实第三方插件验收、T17 PR 事件验证、T18 最终验收均未完成。T16 的 C15 Linux 单容器 smoke 已通过，但不代替真实平台 Gateway 与 G2 最终验收。

Windows Rust 命令：`cargo test --workspace --locked --offline`；参考客户端和 Koishi 分别运行 `npm test`。Windows 联邦 smoke：`uv run --project lab python lab/run.py --suite smoke --platform windows`。Linux 使用本机 WSL Ubuntu 24.04.5 `/root/ligamina` 镜像及 `/opt/node-v24.20.0/bin`；运行 `CARGO_NET_OFFLINE=true cargo test --workspace --locked --offline`、Koishi `npm test`、锁定 CPython 3.12.14 的 Runner pytest、NoneBot 原生 Bridge 探针、双 Worker shutdown 探针、生产 Core 短测，以及 `uv run --project lab python lab/run.py --suite smoke --platform linux`。T14 长测命令为 `uv run --project lab python lab/stability.py --cycles-per-host 100 --duration-hours 24 --interval-seconds 60 --warmup-minutes 30 --skip-build --report outputs/acceptance/t14-stability-linux-wsl-100x24h-20261010.json`；当前在 WSL 运行中。G1 使用自制插件和虚拟网关，不代替真实平台连接；T13 Windows gate 通过不代表 Linux gate 也通过，所有模块、宿主和 smoke 证据也不代替干净系统/G2 验收。
