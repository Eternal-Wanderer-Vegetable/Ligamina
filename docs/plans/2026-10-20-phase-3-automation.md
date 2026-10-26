# 第三阶段：可靠自动化检测与行为验证

日期：2026-10-20（Asia/Shanghai）。状态：分阶段实施中；P3-01、P3-02 Linux、P3-03 Linux、P3-04 Windows（仅 PSF 签名 CPython 3.12.10 profile）、P3-06 已交付。原 uv 管理 CPython 3.12.14 profile 仍被 Windows App Control 阻止；目录插件 Windows claims 尚未开放。代码基线：`a311ac8`，S2-12 历史裁决保持不变。

## 目标与边界

交付一套能在故障后停止扩散、恢复执行、保留证据，并对明确场景自动判断兼容性的检测流水线。沿用 Rust Core、原生 Hosts、SQLite queue、Docker/Windows Sandbox 和已冻结目录，不引入新的分布式任务平台。

优先级分成三步：恢复可信执行能力 → 对受影响集合补测 → 执行小规模、具备独立 oracle 的双路径行为验证。基础设施健康没有通过前，不派发目录插件；没有行为 fixture，不宣称行为通过。

本阶段保留 `p2-41649744e057d61031d5` 的 5,690 entries / 11,380 entry×OS 分母。暂不刷新官方目录，不自动升级插件、framework 或依赖锁，不自动修补第三方源码。真实平台登录、凭证、发送和收件人验收另立任务；重型浏览器、sdist 构建和外部在线服务不进入首批行为集合。

当前用户授权完成本计划并分阶段提交。下方各任务设计仍需以对应实施记录和 acceptance evidence 作为完成依据；新增任务状态见 [feature tracker](../../feature_list.json)。

## 已核对的工程事实

依据：[S2-12 矩阵](../../outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md)、[机器审计](../../outputs/acceptance/phase2-s212-audit-20261017.json)、[交接](../../session-handoff.md)。

| 事实 | 对下一阶段的要求 |
| --- | --- |
| 512/512 报告、11,380/11,380 单元、68,280/68,280 阶段作业已闭合 | 不重新开启历史队列；建立新 campaign 和新证据目录 |
| 单元 0 passed / 880 failed / 7,530 skipped / 2,970 unknown | 按阶段原因选补测集合，不能用单元总状态代替故障明细 |
| 2,158 个 Linux 单元带 Docker API 不可用诊断；796 个 Windows NoneBot 单元带 App Control 4551 | 增加持续健康检查、熔断；Windows runtime 必须在真实 Sandbox 内重新准入 |
| 其余 16 unknown：1 backend、11 preparation、4 timeout | 为每种原因建立有限重试与可复现分类 |
| 全部目录单元 federated/compare 均 skipped；5,621 entries 无保守分类 | 接通执行器并制作 entry-bound fixture，不能只扩充分类标签 |
| 880 failed 中 native load 631、offline npm install 153、worker nonzero exit 96 | 保留失败；区分插件问题、锁/环境问题和检测器缺陷，不用批量重试掩盖 |

源码确认的缺口：

- [lab/census.py](../../lab/census.py) 的 `_probe_for_unit()` 直接返回 Windows NoneBot 历史 blocker，未进行本次能力检测；`_handle_job()` 固定跳过 federated/compare。
- census 的 install job 实际执行安装和 native probe，native/cleanup job 投影同一 probe。恢复时不能独立重跑 native 投影并拿旧 install 结果冒充新执行。
- [lab/queue.py](../../lab/queue.py) 已有 heartbeat、租约恢复、attempt/journal、有限重试；census 当前循环未接入持续 heartbeat 和后端熔断。
- [WorkerSpec/Result](../../lab/workers/base.py)、[Docker](../../lab/workers/linux.py)、[Windows Sandbox](../../lab/workers/windows.py) 已有隔离、资源限额与清理观测；Windows 锁是进程内锁，多进程调度还需主机级互斥。
- [recipes](../../lab/recipes.py)、[compare](../../lab/compare.py) 和 [paired_worker](../../lab/baselines/paired_worker.py) 可复用，但四个固定历史样本的配对路径不能直接当作目录通用 runner。

## 交付架构与代码边界

流水线：冻结输入 → campaign manifest → backend 准入 → 本机 dispatcher → OS Worker → attempt evidence → 聚合与 gate。Linux、Windows 各有本地队列，交换已封存报告；SQLite 不放共享网络盘，不做跨主机直接 claim。

| 组件 | 计划新增/修改位置 | 职责与复用边界 |
| --- | --- | --- |
| Campaign 合同 | 新增 `contracts/lab/automation/v1/`、`lab/automation/manifest.py` | manifest、准入、attempt 引用、selection ledger、gate；不向严格 v1 run schema 偷加字段 |
| 计划与入口 | 新增 `lab/automation/__main__.py`、`selection.py` | `preflight/plan/execute/status/resume/aggregate`；plan 只读校验和建账 |
| 健康检查 | 新增 `lab/automation/health.py`，扩展两个 Worker | 版本/镜像/资源/实际进程启动与清理检查，记录能力矩阵 |
| 调度与恢复 | 新增 `dispatcher.py`、`reconcile.py`；扩展 `lab/queue.py` | 调用原队列，补 epoch fencing、心跳、熔断、reconciliation、主机锁 |
| 目录 probe | 抽取 `lab/census.py`、`pilot_nonebot.py`、`pilot_workers.py` 的可复用部分 | 新路径使用能力探针；旧 S2 路径和历史报告重放保持兼容 |
| 行为 fixture | 新增 `recipes/bindings/phase3/`、`samples/behavior/phase3/` | 精确 entry/artifact/profile/case 绑定与独立 expected oracle |
| 行为执行 | 新增 `lab/automation/behavior.py`；扩展 `lab/baselines/` | 独立 stock/native 与生产 Core/Host 路径，分开可写数据 |
| 汇总 | 新增 `lab/automation/report.py`，按需扩展 Python/Rust report | 重算分母、跨平台审计、支持候选；旧 report CLI 不接收未支持格式 |
| 配置与 CI | 新增 `config/lab/phase3-automation-v1.json`、自动化 workflow | 参数、快速自测、隔离夜间批次与证据上传；沿用现有普通 CI |

不重写通用 queue；新增需求先通过契约测试证明缺口再扩展。Core/Host 只在行为用例证明存在缺陷时修改，另列小任务和针对性回归。

## 输入、身份与证据合同

每个 campaign 保存不可变 manifest，至少包括：

- `campaign_id`、`parent_evidence_manifest_sha256`、Git commit/dirty/source bundle hash、snapshot/artifact lock/profile lock 的摘要。
- `purpose`（recovery / behavior / regression）、platforms、stage scope、显式 `expected_unit_ids` / `expected_case_ids`、selection 原因和排除账本。
- runtime/image/worker policy、fixture/config/services、资源预算、重试策略、分片算法的版本与摘要。
- 每个 unit 的 entry ID、精确版本、artifact/dependency lock/profile identity；每个 attempt 的 generation、job/worker/backend resource ID、开始/结束时间、原始状态和清理证据。

`campaign_id` 含 manifest 的规范 JSON SHA-256 前缀；实际执行另有 execution/attempt ID。重复 manifest 默认拒绝覆盖已有执行，显式 resume 只能继续同一 manifest。输入、代码、profile 或预算改变必须新建 campaign。新报告含实际 UTC 时间，不能继续复用 S2 census 的硬编码日期。

保留所有 attempt；最终投影指向被采用的 attempt，并列出第一次结果、最后结果、失败域、重试历史、flaky 和 supersedes 关系。同一执行单元 install/native/cleanup 来自同一 probe envelope；新的执行 generation 作废旧投影供选择的资格，但不删除旧证据。

新工件目录：`.work/automation/<campaign>/<platform>/queue.sqlite3` 与仓库外的 `Ligamina-automation-evidence/<campaign>/<platform>/`。第三方 Worker 的输入仍由单独外部 staging 目录提供，禁止直接映射仓库、整个证据树或 Docker socket。验收摘要写入新的 `outputs/acceptance/phase3-*.json`，大日志以受控 artifact 存储引用。

新 JSON 由写入端固定 UTF-8/LF，manifest 规定 canonical JSON 算法并拒绝 NaN；二进制发布物、日志、数据库备份按原始字节 SHA-256。封存 SQLite 使用在线 backup/checkpoint 后的快照，不能拷贝正在写入的主文件遗漏 WAL。历史证据的 LF/CRLF 等价校验继续使用既有 helper，不修改历史摘要。

`contracts/lab/v1` 的 run_state/result.status/gate/exit_code 不变。控制器的 `admission_blocked`、`circuit_open` 等存在独立 automation 合同；向现有 run v1 导出时必须显式转换并通过共享 vectors。P3 gate 是有 scope 的外层裁决，不改变旧 gate 含义或回写 P2 结果。

## Worker 准入与持续健康

能力矩阵按 `host × OS × ecosystem × runtime/profile × worker policy` 建立。第一次执行、每次恢复、host/runtime/policy 变化后，先运行自有 benign probes：

1. 检查 OS/架构、工具版本、锁定镜像本地存在、缓存完整性、空闲磁盘与可用内存；任一缺失留具体 blocker。
2. 在实际 Worker 内启动 Python/Node，检查标准库、临时目录、子进程与必要二进制依赖；Windows 不以宿主 `python --version` 代替 guest 内低权限启动。
3. 在相同隔离策略下验证只读输入、禁网、限额、输出上限、超时、进程树回收与 backend 对象不存在；验证失败不放行。
4. 保留环境 fingerprint、probe 版本、run ID、清理观测、过期时间。准入 TTL 初始 30 分钟；每批次前检查，轻量 daemon/API 健康每 30 秒检查。

昂贵边界探针在上述完整准入触发时执行；连续分片仅重复轻量检查，避免每个插件重跑整个隔离套件。probe 失败时不把未启动的每个插件灌成同一 unknown；报告一次 backend blocker 和仍待执行的精确集合。

Windows 路径优先保留现有 Sandbox 和锁定 CPython 3.12.14：收集 guest 内 CodeIntegrity/AppLocker 日志、原始 Win32 状态、被拦截文件 hash/签名、policy ID 和运行账户。在当前文档中 3077 表示强制策略拦截、3089 提供相关签名信息；用 Correlation ActivityID 关联，不能仅凭 4551 推断所有根因，日志不可用就记录不可用。见 [Microsoft App Control 事件说明](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-id-explanations)。

优先选择能在相同政策下运行原锁定 runtime 的专用 Windows 实验机，再验收原 Sandbox backend。若必须更换 runtime 发布包、解释器或 policy，记录为新的 worker/profile 身份和独立实验；不能与旧 profile 混算改善率。方案不以关闭 App Control 作为自动化修复。若两个人日诊断仍无可用组合，交付明确 blocker 与主机要求，Windows gate 保持 blocked，Linux 可以独立继续；不自动切换到未验收的 VM/backend。

Windows Sandbox 的网络、内存与共享配置依据 [Microsoft Sandbox 配置](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)，其本身不证明本项目 Job Object、CPU/PID 和输出收集策略已经有效，仍需 guest 内实测。

## 调度、熔断与恢复

第一版一个 Python controller、每 host 一个本地 SQLite，Linux 初始并发 2，Windows 全主机并发 1。Windows 增加跨进程主机锁；manifest 冻结分片数，恢复不能改分片算法或 count。跨平台 aggregate 只读取封存报告，不控制远端数据库。

| 参数/事件 | 初始设计值与动作 |
| --- | --- |
| 资源预算 | Linux 普通 Worker 2 CPU / 4 GiB，历史 pair 复用已验收 override；Windows Koishi Sandbox 初始 8 GiB、Job Object 限额另记；Windows NoneBot 经准入测量后定值 |
| admission headroom | 可用内存至少为本批并发 Worker 预算之和 + controller/OS 保留（初始 4 GiB）；scratch 按批次预计量，空闲磁盘不得低于 10 GiB |
| 超时 | install 1,200 s、startup 120 s、每行为 case 60 s、正常 cleanup 30 s；保存 wall-clock deadline 与实际阶段耗时 |
| heartbeat / lease | 每 20 s 续租，lease 120 s；整个 Worker 超时仍由 supervisor 管理，不靠租约代替杀进程 |
| 重试 | 瞬态 backend/preparation unknown 最多 3 attempts，退避 30/120 s；确定插件异常、oracle mismatch、同指纹 App Control 不自动重试；timeout 只有 control probe 健康且清理确认才允许最多一次复测 |
| 熔断 | 明确 daemon/API 失联、cleanup 未确认立即停止该 backend 新派发；其他 infrastructure 错误连续 3 次或最近 20 次至少 4 次则打开熔断器 |
| 半开 | 冷却 60 s，只派 benign sentinel；连续 3 次启动与清理通过才恢复。三个恢复窗口仍失败则退出 3，保留 interrupted campaign |
| 租约失效 | 原 attempt 记 unknown；先 reconciliation，再决定重试；旧 generation 的 heartbeat/finish/迟到输出被拒绝 |
| 输出 | stdout/stderr 各最多 4 MiB，截断带原因码与长度；协议 parse/trace hash 与 supervisor 清理证据分开 |

以上是保守起点，不是全量性能承诺。Docker 的内存、CPU 限额必须显式设置并验证生效，不能依赖默认值，见 [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)。Windows 内存指标区分 VM 配置、Job Object peak commit 与宿主可用内存，不互当 RSS。

queue 现有 lease_owner 不足以防同名 worker 重启后的迟到提交：新增 attempt/generation token，并让 claim/heartbeat/finish 一致验证。heartbeat 线程失去租约时 supervisor 停止旧执行并只提交 reconciliation 记录，不能继续以旧租约完成。

controller 崩溃后按持久 resource registry 找到确切 container/Sandbox ID 和 generation；验证归属后仅清理本 campaign 资源，不杀无关 Sandbox。Docker API 无法查询时 `cleanup_confirmed=false`，资源进入 quarantine，禁止对相同单元启动第二份执行。无残留证据后才允许新 attempt。queue 的过期自动 requeue 路径也必须经过该门槛，不能仅在 CLI 外包装后绕过它。

平台断线时保留 queued 和 interrupted 状态，不自动把未执行工作全部写成 skipped 来满足 completion。取消时先停止 claim，再停止已拥有执行、收集 cleanup、封存 checkpoint；退出码 3。SIGKILL、断电靠下次 reconciliation 处理。

## 补测集合与失败归因

先从 512 份已审计报告的逐阶段结果生成不可变 selection ledger，不按 `unit_status == unknown` 直接过滤。failed 优先级可能掩盖同单元 cleanup unknown，因此补测分母不预写成 2,970。

选择条件：任一阶段 unknown，或 failure_domain 为 harness/infrastructure，或已确认检测器缺陷；按 entry×OS/profile 去重，记录原 report/job/attempt 的摘要和选取原因。2,970 是历史单元 unknown 数，2,158 和 796 是主要分类，实际补测集合由 ledger 计算。对 96 个 worker_nonzero_exit 先取完整诊断并用 control probe 分类，只有证实是检测器/环境问题才进入恢复类别。

对选中单元重跑完整安装/native probe 及真实 cleanup；不复用旧失败 attempt 的 native/cleanup 投影。先每个可用生态×OS 分层运行 10 个单元（不足取全部），覆盖 backend/preparation/timeout、已有 plugin failed 对照、清理路径；每层无新增未解决 infrastructure unknown 后再展开。确定插件 failed 可让执行健康 gate 通过，但不计兼容通过。

补测报告与 S2 原始结果并列。派生 current view 可选择“同身份、证据完整、清理可证”的最新 attempt，但必须输出 old→new 转移与残余 unknown；flaky 保留，跨身份结果不覆盖。未选择单元引用完整旧证据链并披露证据年龄；报告分别给出本次执行覆盖率和混合历史证据覆盖率。

只有全量账本中的每个 required install/native/cleanup 都有有效证据且无未解决 infrastructure 问题，才可能裁决全快照执行健康。通过子集只输出该 scope 的通过；不得因此把 `P2-AUTOMATION` 改成 passed。

## 行为检测：从可验证的小集合开始

首批目标为 20 个冻结目录 entry（NoneBot 10、Koishi 10），优先无真实凭证、无外部在线依赖、native 可运行的 command/message/storage/scheduler 类。历史四个精确支持版本另列回归集合，不能拿旧版本代替冻结目录版本凑数。若不足 20 个具备条件，交付缺口列表，P3-BEHAVIOR 保持未达标，不能用分类建议冒充 fixture。

选择算法、排序和候补集合在 pilot 前冻结；entry 不支持 Windows 时原 entry 仍留在该平台账本，不能看完失败再换样本。正常目标 20 entries × 2 OS = 40 entry×OS units；每 entry 至少 3 个必选 case，至少 120 个 entry×OS×case 位置。Windows 未准入时可以先出 Linux 20 units/至少 60 cases 的部分报告，双平台 gate 不通过。

每 entry 至少一个正例、一个不应触发/错误收件人负例、一个状态隔离或重复/恢复场景。scheduler 通过显式 clock seam 控制时间；没有 seam 则保留不适用原因并从候补中事前选其他类别。storage 使用同一 guest 内的本地 fixture，不能为此开放外网。

fixture 必须绑定 artifact/profile/config/service/刺激/expected 的摘要，包含账号和会话、平台类型、操作名、消息片段、收件人、次数与允许的动态字段规则。expected 来源为插件文档/受审查源码的显式规则和人工审阅，记录来源版本与理由；不从本次 Native/Federated 输出自动生成 expected。AI 可以提出候选分类/fixture，进入正式集合前需逐条审阅来源与 oracle。

每个 case 的执行顺序：

1. 校验原始发布物与完整锁；安装/构建只在 OS Worker 内，默认 wheel/npm ignore-scripts，缺必要 lifecycle/build 支持则显式 skipped。
2. 从相同不可变安装输入创建两份独立可写数据，启动 stock/native 与生产 Core+Host。两路径拥有独立进程、HOME、数据库、clock/random 状态，不能继承前一路状态。
3. 将同一刺激送入两条路径，各自验证 independent expected oracle，再比较语义 trace；仅两边相等不够。
4. 独立记录两侧退出、进程树与 Worker 清理。case 结束、超时或进程退出 0 都不能单独证明场景完成。
5. 固定 seed/time/source identity 在干净 Worker 复跑 3 次；任一次 mismatch/unknown 或未确认清理，不能成为 support candidate。

S2 阶段名称保持可追溯，但行为 campaign 的 native 必须执行场景，不再只表示加载：prepare → install → native scenario → federated scenario → compare → cleanup。每个 attempt 保存阶段语义版本，禁止将 S2 native-load 与 S3 native-behavior 直接混算。单个 pair 可封装成 OS Worker 内的受监督执行 envelope，再投影阶段结果；federated 必须实际经过生产 Core，不得调用 native 结果作替身。

## 分母与验收 gates

所有 gate 输出 scope hash、应测集合、结果计数、排除原因、证据年龄、运行身份和未解决问题。0 分母为 null/未评估，不通过。报告同时列 5,690 entries / 11,380 全量单元、补测集合、实际派发集合、行为集合、case 集合，不能混用分母。

| Gate | 通过条件 | 失败/阻塞时的交付 |
| --- | --- | --- |
| P3-ENGINE | 合同/队列/错误注入用例通过；所有声明 backend 的真实 benign isolation/control probes 通过；迟到结果不能提交 | 实现自测与每 OS 能力矩阵，缺实机证据不能标全平台 passed |
| P3-RECOVERY | 选定受影响集合账目闭合、0 unknown、0 unresolved harness/infrastructure、所有启动过的 Worker cleanup 可证；已归因插件 failed 允许 | 逐单元 old→new 表、未恢复集合与原因，受阻 OS 单独 blocked |
| P3-AUTOMATION | 冻结全量 stage eligibility 账本闭合；必需 install/native/cleanup 无 unknown、未调度或未解决 harness/infrastructure；旧有效证据/新执行来源均可追溯 | 只给实际 scope 结论，明确尚未覆盖单元；与 P2 历史裁决并列 |
| P3-BEHAVIOR | 20 个 entry 的至少 120 个必选 case 位置双路径执行、oracle 与 comparator 全通过，3 次干净复跑，无 flaky、无未确认清理 | 已执行/失败/跳过/未知与缺口；单 entry 完整通过可形成精确候选，不能宣称整个 pilot passed |
| P3-FIELD | 本计划不执行 | not run；虚拟/本地 service fixture 的通过不升级为真实投递 |

stage eligibility 必须在执行前冻结：catalog/artifact/profile 明确不具备条件的条目，保留静态 skipped 与原始依据；行为缺 fixture 不属于 install/native-load gate 的必选 scope，但一直计入全量行为缺口。worker/平台不可用不能改成静态不适用。使用外层 P3 自动化 gate 的精确规则；旧 v1 `automation` 的 unknown、未调度拒绝规则保持不变。

支持候选按 `entry + version + artifact + OS + profile + cases + delivery_scope` 发布。候选全部必选 case 与三次复跑通过，证据审阅后才加入支持清单；不得批量新增商店级支持结论。

## 实施任务与依赖

按单活跃 feature 执行；下列人日为一名开发者的初始估算，不包含等待 Windows 主机/策略处理与全量机器时间。每任务独立小提交；本次只登记 backlog。

| 任务 | 依赖 | 交付与主要文件 | 完成条件 | 人日 |
| --- | --- | --- | --- | ---: |
| P3-01 输入合同与补测账本 | S2-12 | automation schemas、manifest/selection、golden fixtures | 逐阶段可重算选取集合；缺失/重复/篡改/身份错配被拒绝；新旧身份不能合并 | 2 |
| P3-02 Worker 准入与健康检查 | P3-01 | health、backend capability adapters、资源配置 | Linux 实机 probe 通过；Windows 探针入口与阻塞证据可运行；没有准入不 claim | 2 |
| P3-03 熔断、fencing 与恢复 | P3-02 | dispatcher/reconcile、queue 扩展、host mutex | 故障后停止派发、所有旧 generation 写入拒绝、重启先清理后重试；Linux 实机注入通过 | 3 |
| P3-04 Windows runtime 准入 | P3-02 | Windows health、Sandbox runtime packaging/probes | 同锁定 runtime 的 guest 低权限实际启动、隔离/限额/清理通过；替代身份单列；无可用主机则 blocked | 2–4 |
| P3-05 受影响集合补测与归因 | P3-03 | recovery manifests、新报告、old→new ledger | 先 10/层 pilot，再扩大；Linux 必做；Windows 只有 P3-04 通过才执行；各 OS 分别裁决，剩余范围披露 | 2 + 机器时间 |
| P3-06 entry-bound 行为集合 | P3-01 | binding/fixture、来源与 oracle 审阅账本 | 20 entries、每项至少 3 case、两 OS 适用性冻结；既有四版本单列回归 | 3 |
| P3-07 通用 Native/Federated 执行 | P3-03、P3-06 | behavior、baselines、必要 Host 适配 | 实际经过 Core/Host；同错、错收件人、缺 expected、状态串扰负例全拒绝；历史四版本回归无新增失败 | 4 |
| P3-08 双平台行为 pilot | P3-04、P3-05、P3-07 | 每 case trace、3 次复跑、资源分布 | ≥120 必选位置完成执行/依赖归因与三次复跑记账，实际 Worker 清理有证据；P3-BEHAVIOR 按实测独立裁决，未通过保留失败不得改样本 | 2 + 机器时间 |
| P3-09 CI 与定期检测入口 | P3-03、P3-07 | PR 自测 workflow、受控实验机任务、artifact 上传 | 干净 checkout 可复现；失败/中断仍上传；fork PR 不触发专用 Worker 或获得实验机凭证 | 2 |
| P3-10 报告、gate 审计与交接 | P3-05、P3-08、P3-09 | report/matrix、README 新块、最终 evidence manifest | 分母独立复算、五 gates 独立裁决、新证据哈希校验通过、交接可复跑 | 2 |

总量约 24–26 人日 + 执行与外部等待。第一周先交付 P3-01–P3-03 的 Linux 可靠执行闭环，再处理 Windows 与行为场景；两个人日 Windows 诊断窗口到期可以切换去做无依赖的 fixture 工作，但 Windows 功能保持 blocked，不伪报完成。P3-05 可在 Windows 未准入时完成 Linux scoped 交付，P3-08 明确依赖 Windows，不能因此跳过双平台最终目标。

工程任务完成与兼容 gate 通过分开：P3-08 完成可复现执行、归因和报告后，即使发现确定的插件不兼容，也可交付该检测任务；P3-BEHAVIOR 仍 failed，支持名单不扩张。backend 未准入、必选位置无账或清理未知属于验收缺项，不能按插件失败处理后结束任务。

## 测试与运行手册

下列是实现任务应补齐的验收矩阵，不是本次已执行结果：

| 层级 | 必测场景 | 证据与通过要求 |
| --- | --- | --- |
| 合同与选择 | 历史 unknown 被 failed 覆盖、重复/漏项、manifest 变更恢复、profile 偷换、0 分母 | golden vectors；预计集合与实际集合完全一致 |
| 调度 | 同名 owner 重启、过期 lease、迟到提交、断线 3 次、Ctrl-C/SIGKILL、SQLite backup 恢复、Windows 两 controller | fake backend 可控时钟测试 + 真 Worker 故障注入；不能重复执行未清理单元 |
| Worker | API 失联、OOM、timeout、输出洪泛、子进程残留、read-only/禁网绕过、App Control | benign 实机 probes；清理以 supervisor/后端证据为准 |
| 分类 | process.exit(0)、npm 离线失败、插件异常、导入无输出、无效协议 | 插件失败与 backend unknown 分域，0 退出不能冒充 native 完成 |
| 行为 | 两边同错、错误会话/账号、缺 expected、空 expected 合法、动态字段越权忽略、随机/时间、数据串扰 | 独立 oracle 负例、Core trace、必选 case 全量复算 |
| 聚合 | 缺 shard/case/attempt、源 SHA 变化、跨身份合并、flaky 被最后 pass 掩盖 | Python 聚合重算；若扩展 Rust 输出，共享向量一致 |

当前已存在、实施时可用的起点：`bash init.sh docs`、`node --test scripts/evidence-hash.test.mjs`、`uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_queue lab.tests.test_census lab.tests.test_workers lab.tests.test_recipes lab.tests.test_compare -v`。运行时代码改动按 [开发指南](../guides/development.md) 跑针对性检查与 `bash init.sh full` / Windows 对应入口；真实 OS 验收不能以 mock 或普通 CI 代替。

计划提供的 CLI（当前不可执行；P3-01–P3-03/P3-10 实现后才可使用）：

```bash
python -m lab.automation preflight --platform linux --output .work/p3-linux-health.json
python -m lab.automation plan --config config/lab/phase3-automation-v1.json --purpose recovery --output .work/p3-manifest.json
python -m lab.automation execute --manifest .work/p3-manifest.json --platform linux
python -m lab.automation status --manifest .work/p3-manifest.json
python -m lab.automation resume --manifest .work/p3-manifest.json --platform linux
python -m lab.automation aggregate --manifest .work/p3-manifest.json --evidence-root ../Ligamina-automation-evidence
```

CLI 从 manifest 推导本地 DB/输出位置，拒绝指向历史 S2 DB 或覆盖已封存报告。Windows 在该平台已准入主机运行同一 manifest 的 windows 分区；不能通过 Linux CLI 模拟其 passed。退出码沿用 0 所选 gate 通过 / 1 未通过 / 2 输入错误 / 3 基础设施中断。

## CI、执行成本与停止条件

PR 必跑不依赖实验机的 schema/selection/queue/comparator/fixture 校验和 mock 故障注入，并保持原有 full checks。Docker benign 集成仅在已准入 runner 执行；Windows Sandbox 实机验收单列 workflow，普通 Windows 测试成功不代表 Sandbox 可用。

专用实验机第一版只消费审阅过的 commit + sealed manifest，通过手动 dispatch 启动；后续 workflow 才配置夜间核心行为回归、每周恢复/覆盖检查。此处是产品实施设计，不创建 Codex 定时自动化。workflow concurrency 防相同 campaign 重入；Windows 还需主机锁。artifact 获取可访问 registry allowlist；插件安装脚本/行为进程在禁网 OS Worker 中执行，不接收仓库令牌。对在线服务的支持另立 profile。

普通 PR 不运行全目录；目录未刷新、身份未变且无检测器修复时不重复全量扫描。按变更影响选择 recipes/Host/Core 回归集合；全量运行需显式 manifest 与预算。初始 evidence 本地保留失败/unknown 的完整受限日志 90 天，成功 trace 30 天，最终报告与所引用证据链在项目验收期内完整保留；自动清理必须拒绝删除被最终 manifest 引用的证据。

时间预算按 pilot 同阶段/同平台的 P95 × 待执行量 / 实际并发 + 启动/清理/重试开销估计；下载按缺失 artifact bytes 去重，磁盘按解包/缓存/证据分别估计，Windows NoneBot 未测前不做全集耗时保证。行为至少 120 个 case 位置 × 3 次复跑，各含两条路径；按 pair envelope 实测资源，不能把单 case 时间直接当作全量预算。

每次运行配置 wall-clock、累计下载与 evidence 大小上限；pilot 未量到这些指标前不得展开全量。达到预算、磁盘低于阈值、后端失联或清理无法确认时停止派发、封存 interrupted checkpoint，不以更多重试突破预算。恢复必须重新准入并确认隔离资源可回收。

## 本次方案交付与下一步

本次仅修改计划、导航与状态；没有派发第三方插件、刷新目录、改写 S2 原始报告或改变支持结论。文档/结构验证与实际输出记录在 [progress](../../progress.md) 和 [handoff](../../session-handoff.md)。

### P3-01 实施记录（2026-10-20）

- 已新增 `contracts/lab/automation/v1/manifest.schema.json`、`lab/automation/selection.py`、`lab/automation` 的 `plan/validate` 入口和正/负例测试。
- `plan` 校验 S2-12、S2-11、冻结 snapshot 与完整 512 shard 来源，重算全部 11,380 个 entry×OS 单元及六阶段摘要；任何阶段 unknown、归因的 infrastructure/worker 域或已确认检测器缺陷会将整单元选入全阶段新执行 ledger。静态 artifact/profile 不适用单列；跨快照、重复/缺失和源文件哈希变化拒绝。
- 封存 ledger：[phase3-p301-recovery-manifest-20261020.json](../../outputs/acceptance/phase3-p301-recovery-manifest-20261020.json)，manifest SHA-256 `f084778b6cfee271876ba6493e039fc2a288e0295a7a0fe9353f0d6f2074416e`；选中 3,179 units / 19,074 stage rows，静态 skipped 1,048，来源分母 11,380。这里是补测选择范围，不是恢复执行结果或 gate 通过。
- Linux `lab.tests.test_automation_selection` 4 项通过，覆盖 golden schema、完整数据重算、重复/漏项/篡改/身份错配/跨快照拒绝；`lab.tests.test_queue lab.tests.test_recipes lab.tests.test_compare` 基线 24 项通过。Windows 测试未执行。
- 下一项 **P3-02**：Worker 健康准入与 Linux 实机 benign probes；Windows 探针只记录可运行入口与 blocker，不进入本次测试。

### P3-02 实施记录（2026-10-20；Linux gate admitted，Windows 未测试）

- 已新增 Linux health fingerprint、Docker daemon/锁定镜像/资源余量/benign report 校验、失败关闭的 claim admission helper、`preflight` CLI 与初始资源/熔断参数；Linux probe 脚本现可显式指定新报告路径，不会覆盖 S2 evidence。Windows preflight 仅输出 `not_tested`，未运行。
- 初次 Linux 检查因主机缺少 Docker CLI/daemon 阻塞，原始 [blocked report](../../outputs/acceptance/phase3-p302-linux-admission-20261020.json) 保留。随后安装 Ubuntu `docker.io`；Docker daemon 通过继承当前代理启动，pull 到 worker image lock 指定的 digest。运行真实 Docker benign probes，没有使用 mock。
- WSL2 Linux 环境上 10/10 probes 达到各自预期终态，10/10 cleanup confirmed；覆盖只读输入/禁网/秘密边界、隔离并行 worker、非零退出、cgroup CPU/memory/PID 限额、timeout、输出截断、OOM、进程树清理和 worker-loss。timeout、输出截断、OOM、worker-loss 按预期被记录为 `unknown`，不是插件成功。
- 新 [probe report](../../outputs/acceptance/phase3-p302-linux-workers-20261020.json) SHA-256 `e9f3a5a466680227b86c54c4df2ad8ae6d83ef7244e6b0cedb317c1fb42618b4`；[admission report](../../outputs/acceptance/phase3-p302-linux-admission-retry-20261020.json) 为 `admitted`，fingerprint `3bb6e32bdea454df69571d7e621c3027fe55c0d3992e974cebe1ed09efa47e42`。Health + queue Linux 单元基线 13 项通过。Windows CLI/Worker 未运行，Windows gate 仍待 Windows/WSL 专项阶段。
- Linux Worker backend 已准入，可开始依赖它的 Linux P3-03 工作；这不代表 Windows 准入或任何插件兼容。

### P3-06 实施记录（2026-10-20；套件冻结，插件行为未执行）

- 已新增 `contracts/lab/automation/v1/behavior-suite.schema.json` 与 [entry-bound suite](../../contracts/lab/automation/v1/fixtures/behavior-suite.phase3-v1.json)：冻结目录中的 NoneBot/Koishi 各 10 个版本、entry id、artifact digest、Linux/Windows dependency-lock 与 profile id、归档成员 SHA-256、审阅的源码符号，以及每个 entry 的 positive / negative / isolation 三个必选 case。每个用例固定 session 输入结构，并为随机/时间相关输入声明固定种子和时间。
- 用例由对应锁定发布物中的源码符号独立推导。涉及状态风险的 NoneBot guess 案例将模块级 `idx/city` 作为隔离 oracle 检查项；并没有将潜在状态泄漏当作支持行为。Koishi 插件的数据库、配置、命令和调度情景限定在临时 fixture 状态中。四个历史支持版本单独从 S2-09 manifest 复制为回归 identities，未计入 20 个目录条目。
- `lab.automation validate-behavior` 校验 JSON schema、20 个唯一目录 id、artifact/profile/dependency-lock identity、各 entry 的三个 case class，以及历史四版本与 S2-09 manifest 一致性。`lab.tests.test_automation_behavior` 覆盖重复 entry、缺失 case、artifact 绑定篡改拒绝。
- Linux 检查：`uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_automation_behavior lab.tests.test_recipes lab.tests.test_compare -v` 为 18 项通过；CLI validator 通过并输出 20 entries / 60 cases / 40 entry-platform positions / 4 regressions。证据见 [suite validation](../../outputs/acceptance/phase3-p306-suite-validation-20261020.json)。本结果只证明合同与身份账本有效；`behavior_executed=false`，没有派发或执行第三方插件、没有行为兼容结论；Windows 测试按用户要求跳过。
- P3-06 工程交付完成。P3-07 仍依赖 P3-03 的 fencing/reconciliation；Windows 相关 gate 继续 not run / blocked。

### P3-03 实施记录（2026-10-20；Linux 完成，Windows 跳过）

- 队列为新 run 增加 fenced mode 和随机 attempt token / generation。fenced attempt 租约过期会被记录为 unknown 并中断 run；旧 token 无法 heartbeat 或 finish。清理未确认时 run 保持 quarantine，只有精确 attempt 清理证据 `cleanup_confirmed=true` 后才能重试和恢复派发。legacy queue 不启用 fenced mode，保留原 S2 调度语义。
- 新增 `lab/automation/dispatcher.py`、`circuit_breaker.py`、`reconcile.py`、`host_lock.py`。Dispatcher 定期 heartbeat 且提交带 attempt token；daemon/API 失联、清理未确认立即打开 circuit，其他失败使用连续/滑窗阈值；半开只允许 sentinel。Linux host lock 验证本机 controller 排他。Docker Worker 支持 campaign/job/attempt/generation 标签；reconciler 仅处理四个标签完全匹配且经 inspect 复核的容器，并确认清理结果。
- 修改前 baseline：`uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_automation_health lab.tests.test_queue -v`，13 passed（Linux）。修复中首次 focused run 发现 joined SQL 的 `run_id` 歧义；修复后复跑通过。最终 Linux queue/dispatcher suite 21 passed，文档检查 19 documents / 269 links，evidence hash Node tests 4/4，`git diff --check` 通过。
- 使用 Docker Engine 29.1.3 和锁定 `python@sha256:f77ac9e4…c84e51f` 做真实 Linux 标签清理故障注入：目标旧 attempt 容器被删除，无关容器在核验时仍运行，随后两个测试容器均被清理。详见 [P3-03 acceptance](../../outputs/acceptance/phase3-p303-linux-20261020.json)。未运行第三方插件。
- Windows/Sandbox 测试按用户要求跳过，P3-04 保持 not-started；不视作 gate 通过。后续先做 P3-05 Linux scoped recovery，再继续依赖已满足的 P3-07；Windows 工作待用户之后在 Windows/WSL 阶段推进。

### P3-05 Linux scoped recovery checkpoint（2026-10-20；因冻结输入缓存缺失而中断）

- 已实现 Linux-only pilot manifest、严格 identity 校验、四阶段 fenced queue、attempt/worker 标签、old→new stage ledger、缓存预检与中断 checkpoint。试点固定 20 个单元（NoneBot/Koishi 各 8 个受影响恢复项 + 2 个历史插件失败 controls），新 campaign `p3-recovery-linux-8c161abc3e6e`；[worker-identity manifest](../../outputs/acceptance/phase3-p305-linux-pilot-rebuilt-worker-manifest-20261020.json) SHA-256 `8e3d931c297cb7e22f9d293f954e908bd9c02383c9e5a6af36c4e8060a0bc668`。因本地重建的 Python profile image digest 与锁定输入 manifest 不同，单独生成了带准确 image identities 的 manifest，没有冒充原 profile identity。
- cache preflight 显示冻结 snapshot 引用的 artifact 和 dependency-lock 对 20/20 个 pilot entries 均缺失（ready 0/20）。无法从本地 artifact cache 恢复这些精确 SHA 的文件；后续重新解析出来的 Koishi lock SHA 与冻结 lock 不同，故没有把它替换成相同身份，也没有把不同 profile 结果归因到旧结果。其他 2,206 个 Linux recovery entries 的 cache 检查也未找到完整输入。完成同 identity 补测的必要条件是恢复每个选中 entry 的精确 artifact 与 profile-lock 字节。
- 仍按 fail-closed 路径启动过一个 install attempt，Worker 启动前因锁定输入缺失报 `PilotWorkerError`；没有执行任何插件代码。fenced run 中断，unknown attempt 保留；Docker 标签 reconciliation 对该 attempt 精确确认 cleanup。其余 19 个单元未派发。最终 [checkpoint](../../outputs/acceptance/phase3-p305-linux-pilot-checkpoint-20261020.json)，SHA-256 `5a35b12ffd323068ebe7152a1fb2a40ed7197f625e2c97a180e5762e082a551e`：terminal units 1/20、unknown 1、not run 19、cleanup confirmed 1；Linux health 当前 admitted，但 pilot gate blocked、plugin compatibility not evaluated。原始 admission 失败与 admitted retry 报告均保留。
- 定向验证：`uv run --locked --project lab --python 3.12.14 python -m unittest lab.tests.test_automation_recovery lab.tests.test_automation_selection lab.tests.test_queue lab.tests.test_automation_dispatcher -v`，28 项通过；`bash init.sh docs` 通过（19 documents / 270 local links）；bundled Node `--test scripts/evidence-hash.test.mjs` 4/4；`git diff --check` 通过。Windows/Sandbox 测试按用户要求跳过。
- P3-05 的 Linux 执行仍 blocked，不能标记 done。P3-07 的依赖（P3-03、P3-06）已满足，可先推进通用 Native/Federated 执行；P3-08 仍等待 P3-05、P3-04。恢复冻结缓存后必须使用新 checkpoint/recovery attempt，不能改写这次 unknown 记录或旧 P2 报告。

### P3-07 通用 Native/Federated 执行（2026-10-20；Linux 完成，目录行为未执行）

- 新增 `lab/automation/execution.py` 的 `run_behavior_pair()`：只向 runner 发送 case stimulus 与不可变 identity/binding，不发送 oracle；两侧使用不同的全新工作目录并带固定 clock/seed/session；两侧分别校验 entry/artifact/platform/profile/dependency-lock/case/fixture binding、冻结 assertions 和 cleanup。Native 必须显式证明没有导入 Bridge；Federated 必须带 Core/Host route、Host PID reaped 的 trace。缺失 expected、错误收件人、两侧同错、身份偏离、cleanup 不明、trace 缺失均 fail closed。
- `lab/baselines/paired_worker.py` 为四个 S2-05 历史样本增加 Federated Core/Host trace 强制校验；实际复跑仍通过生产 Core、NoneBot/Koishi Host 与 Gateway。`run_s205.py` 支持显式记录新构建 runtime/profile image identity；宿主 staging 安装脚本禁用、proxy 只传给可信的宿主侧依赖 resolver、不进入插件 Worker；容器以 staging 目录所属 UID 运行并清理临时 profile。
- Linux focused verification：23 项通过，包括 automation pair、native runner、comparator、baseline source/cleanup/proxy tests。最后的选择性命令逐项点名 Linux-safe tests，排除了 Windows bootstrap 静态检查；Windows runtime、Windows/WSL 测试均未运行。
- 四个历史支持插件真实 Linux Docker Worker 回归全部通过：NoneBot autohelp/repeater 与 Koishi game-mini/multi-qa；四个 pair comparison 均 passed，Native 无 Bridge import，Federated Core/Host route 均 passed、PID reap passed，Worker 与 staging cleanup 全确认。无新增历史失败，不扩张支持范围。成功报告 [P3-07 legacy regression](../../outputs/acceptance/phase3-p307-linux-legacy-20261020.json) SHA-256 `d418bd3ee8a7ad6b5f05c02adc61098a0ed50bd0076a465cbaa5498d3dbda058`，其中 runtime image ID `sha256:da8004f5b3bf4e3be46f09bdea06e1e91450a7bfb57052cf9d18448685d27d47` 与重建 Python profile image digest `fcb712608785413b1db90fd2d96031beb1e1a18588b89d6cacdde906ce42ab89` 都已明示，未冒充历史 runtime identity。前置失败/中断 attempt 1–5 均单独保留，不混入成功报告。
- 本阶段验证没有执行 20 个目录插件的 60 个 behavior cases；P3-BEHAVIOR 仍 not evaluated，P3-08 双平台执行仍等待 P3-04 与 P3-05。P3-07 完成后开始依赖已满足的 P3-09 CI/周期入口；P3-05 缓存恢复任务和 Windows admission 持续开放。

### P3-09 实施记录（2026-10-20；Linux 本地与干净 checkout 通过）

- 新增 `.github/workflows/phase3-linux-self-test.yml`：fork-safe 的 hosted `ubuntu-24.04` PR / main / weekly / manual 自测，只授予 `contents: read`，checkout 不保留凭证，固定 Node/uv 版本；失败、取消或成功都尝试上传报告和日志。`scripts/phase3-linux-self-test.sh` 记录退出状态和日志 SHA-256，只运行 Linux-safe 契约、文档与自动化单测，不执行目录插件。
- 新增独立的手动受控 runner workflow：只接收 `workflow_dispatch`，guard 要求源仓库、非 fork、默认分支和仓库变量 `PHASE3_LINUX_RUNNER_ENABLED=true`；专用 runner 标签及 `phase3-linux-controlled` GitHub Environment 用于限定机器和保护审批，campaign concurrency 禁止并发取消。权限为只读 contents，checkout 不保留凭证；`always()` 上传 campaign 报告、日志和外部 evidence root。仓库外的 runner、Environment protection 与 variable 仍需在部署仓库配置，本地没有声称已执行 GitHub hosted 或 self-hosted job。
- `scripts/phase3-linux-controlled-runner.sh` 为受控入口提供 trap 报告、逐步日志和哈希。注入首步退出码 7 验证报告仍为 `failed_or_interrupted`，带 `windows_tests_run=false`，log SHA 可复算；证据：[controlled trap report](../../outputs/acceptance/phase3-p309-linux-controlled-trap-check-20261020.json) 与 [trap log](../../outputs/acceptance/phase3-p309-linux-controlled-trap-check-20261020.log)。这验证的是本地留证逻辑，不是实际 pilot 执行。
- 向挂起中的受控入口发送 SIGTERM 也验证了退出码 143 会落为 `failed_or_interrupted` 并保留日志哈希；证据：[interruption report](../../outputs/acceptance/phase3-p309-linux-controlled-interrupt-check-20261020.json) 与 [interruption log](../../outputs/acceptance/phase3-p309-linux-controlled-interrupt-check-20261020.log)。测试替身只休眠，不运行 Worker 或插件。
- 从提交 `df0705e` 导出的干净 checkout 运行 `PATH=<locked-node>:$PATH GITHUB_RUN_ID=p309-clean-20261020 bash scripts/phase3-linux-self-test.sh`：Node 检查 8/8、Linux 自动化回归 54/54、文档检查 19 documents / 278 links、suite validator 20 entries / 60 cases / 40 platform positions / 4 legacy regressions；总状态 passed，`windows_tests_run=false`、`plugin_execution=false`。报告及对应日志：[self-test report](../../outputs/acceptance/phase3-p309-linux-self-test-clean-checkout-20261020.json)、[self-test log](../../outputs/acceptance/phase3-p309-linux-self-test-clean-checkout-20261020.log)，报告含匹配日志 SHA-256。
- 两个 workflow 均通过 PyYAML YAML 解析、`bash -n` 和 `scripts/phase3-workflows.test.mjs` 的权限/触发器/fork 隔离/失败证据静态合同。Windows runtime、WSL/Windows tests 未运行；此前误运行的只读 bootstrap 静态测试已在 P3-07 验证中明确排除。
- P3-09 工程交付完成。后续 P3-08 仍因 P3-04 Windows 准入及 P3-05 冻结缓存缺失而未满足依赖；P3-10 也不可开始。不能据此将 P3-BEHAVIOR 或其他 gate 判为通过。

### P3-04 Windows 实机准入（2026-10-25；替代 runtime profile）

- 分享记录中的 Codex shell sandbox 设置不会改变项目 Worker 的 Windows Sandbox CLI 路径。复核本机 runtime 后发现 uv 管理的 CPython 3.12.14 `python.exe` 未签名，guest restricted-process create 返回 App Control Win32 4551；同机另有由 Python Software Foundation 签名的解释器。
- 冻结独立 profile `windows-cpython-3.12.10-official-embed-amd64-psf-signed`：官方 [Python 3.12.10 Windows embeddable package](https://www.python.org/downloads/release/python-31210/) 归档 SHA-256 `4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3`，`python.exe` SHA-256 `4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a`，Authenticode `Valid`，签发者 Python Software Foundation。该 profile SHA-256 `ab84e3eb8155cfb7ba3da843e8ac4fb3a3fabe8cb5539eadfa17c004626c83bb`。
- 该 runtime 在实际 Windows Sandbox 内以隔离低权限进程启动，标准库与子解释器均成功；Worker cleanup confirmed。既有 Windows Sandbox 边界套件 7/7 预期通过、清理确认。额外限额探针实测 CPU hard cap（请求 0.25 core，进程 CPU/wall 比 0.243）、active process 上限 4（Python 加 3 个子进程后第 4 个子进程创建被拒）、Job Object memory limit 256 MiB（Sandbox VM 2,048 MiB，以分离测量；峰值 Job commit 276,340,736 bytes 后 Python 收到 `MemoryError`），清理均确认。综合 admission 报告：[CPython 3.12.10 Windows admission](../../outputs/acceptance/phase3-p304-windows-admission-python31210-20261025.json)。
- P3-04 对上述精确 runtime/Worker policy profile 为 admitted；原 uv CPython 3.12.14 报告及 4551 仍保持 blocked，不回写或混算。该 admission 报告生成时没有运行目录插件；后续 P3-05 Windows v2 pilot 使用单独生成的 profile lock 绑定 10 个 NoneBot 样本，原 catalog profile lock 未修改。P3-05 的 Windows 全量受影响集合及 Linux unknown 归因仍未闭合，P3-08/P3-10 不得据此开始。

### P3-05 Windows affected pilot（2026-10-25；20 项已闭合，完整 Windows 范围未完成）

- 从 P3-01 Windows affected ledger 选取 10 个 NoneBot 条目和按历史归因域分层的 10 个 Koishi 条目，冻结独立 campaign `p3-recovery-windows-pilot-v2-9666ebb4609f`。首版 pilot manifest 未显式绑定 Koishi provider profile，保留为未执行草稿；v2 在每个条目上冻结实际 probe profile，canonical SHA-256 `0796fad72d14f0e84cfd91b77e63177873a26510dfa3adee55c9a3298feed4fa`，文件 SHA-256 `c28dcdddc035df73841e42a811abd4b4fd4c89c6fd795b783ea0214dcf9f21d5`。v2 沿用替代 profile lock v3 和 P3-04 admission，不改写原 catalog/profile locks。
- Windows Sandbox 串行执行 20/20，Worker 成功且清理确认 20/20；unit outcomes 为 failed=2、skipped=8、unknown=10、passed=0。报告：[Windows pilot execution](../../outputs/acceptance/phase3-p305-windows-pilot-v2-execution-20261025.json)，文件 SHA-256 `5e1f02f68e65faecf8048a5fc722cf613dda33b55792ae1e0e09a6a35d95d976`；[validation](../../outputs/acceptance/phase3-p305-windows-pilot-v2-validation-20261025.json)；最后 checkpoint SHA-256 `b3ea7bc0e010168769c394bf1f16f4f7f86a586c4e4c4b2a020c111d95325406`。
- NoneBot 10/10 的依赖安装通过，但 `pydantic_core._pydantic_core` 导入返回 DLL load failed；记录 `unknown / windows_native_extension_load_blocked`。协议中的 `policy_diagnostic_detected=false`，本机 CodeIntegrity/AppLocker 查询未找到对应事件，因此具体是 App Control、缺失 DLL 还是其他加载条件尚未证实。没有 Sandbox 启动/访问权限错误；未在宿主机执行插件。
- Koishi 使用 Node 24.20.0 官方哈希锁定 runtime 和 16 GiB Sandbox。结果：4 个 native load 正常但因缺 entry-bound behavior fixture 而 skipped；4 个 required Host service 不在空 provider profile 而 skipped；另 2 个 native load failed，分别为 `sqlite3` 安装脚本被禁用后没有 `node_sqlite3.node`，以及插件引用未冻结进 Host profile 的 optional peer `@koishijs/plugin-console`。这两项记录保留 failed，但属于加载/依赖配置证据，不单独形成行为兼容结论。行为 gate 保持 `not_evaluated`。
- 复跑器 `scripts/phase3-windows-pilot.py` 支持冻结身份校验、单控制器互斥锁、串行派发、原子 checkpoint/恢复和最终报告幂等复核；定向 `py_compile` 与 `lab.tests.test_profiles lab.tests.test_catalog_run lab.tests.test_pilot_nonebot lab.tests.test_pilot_workers` 34/34 通过。另增加中性 reason code `windows_native_extension_load_unattributed`：DLL load failed 而没有明确 policy marker 时仍记 unknown，不推断 App Control。Windows affected 总分母 973（796 NoneBot /177 Koishi）；pilot 已执行各 10 个，仍有 953 个（786/167）未完成。P3-05 保持 in-progress，P3-08/P3-10 暂不可开始。

### P3-05 Windows native DLL attribution（2026-10-25；diagnostic-only）

- 为归因 pilot 中十个相同的 `pydantic_core._pydantic_core` 导入错误，使用独立 Sandbox 诊断运行锁定的 CPython 3.12.10、`pydantic-core==2.50.1` wheel SHA-256 `132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498` 和 `typing-extensions==4.16.0`。未安装或加载任何 catalog plugin，未更改既有 campaign 的终态。
- Sandbox 中 `_ctypes`、`_ssl` 与 `bcryptprimitives.dll` control load 成功；官方 runtime archive 内含 `vcruntime140.dll` 与 `vcruntime140_1.dll`。对锁定的 `_pydantic_core.cp312-win_amd64.pyd`（SHA-256 `d38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297`）执行 import 和直接 LoadLibrary 都复现失败，后者捕获到 WinError 4551：App Control policy blocked this file。报告：[native DLL diagnostic](../../outputs/acceptance/phase3-p305-windows-native-dll-diagnostic-20261025.json)，SHA-256 `66646448fc2dedd3a4d311fcf0d2e8ce6163832abe01f1fcf4b81d778837d013`；可复跑脚本：[phase3-windows-native-dll-diagnostic.py](../../scripts/phase3-windows-native-dll-diagnostic.py)。
- 此诊断将十个 pilot unknown 的共同失败定位到 Sandbox 的原生扩展加载策略层，不构成插件行为结论，也不将 unknown 改写为通过。没有 Sandbox 权限错误；不在宿主机运行插件、不关闭 App Control。继续覆盖 Python 条目需要获批的 Sandbox policy/签名方案和新的冻结 profile lock；Koishi native profile 仍需纳入 sqlite3 binding 与 `@koishijs/plugin-console` provider。Windows 全量 affected 集合还有 953 项未执行，P3-05 继续 in-progress，P3-08/P3-10 仍不可开始。

### P3-05 Windows policy/cache follow-up（2026-10-25；admin policy 尚待批准）

- 独立诊断 v2 保持与 pilot v2 相同的已准入 CPython 3.12.10、wheel 和 `.pyd` SHA；Sandbox 重现 WinError 4551，仍未运行 catalog plugin，也没有改写原 10 个 unknown。报告 [diagnostic v2](../../outputs/acceptance/phase3-p305-windows-native-dll-diagnostic-v2-20261025.json) 文件 SHA-256 `fc45e224a03f36d7732a774fa6bccc37bcfba936135a2c5ad90b562af9b29bc1`，复跑脚本 [diagnostic v2](../../scripts/phase3-windows-native-dll-diagnostic-v2.py)。
- guest 事件查询只捕获 3099 policy-load 记录：`VerifiedAndReputableDesktop`（GUID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}`，hash `2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01`）与 `VerifiedAndReputableDesktopFlightSupplemental`（GUID `{1678656c-05ef-481f-bc5b-ebd8c991502d}`，hash `140CE61A30A209A23B5D43906C9BF0E8AFCD7C2CFA952F5CD04E8058EBBFAFE2`）。guest 最近 30 分钟没有 3076/3077 事件；宿主 CodeIntegrity 查询也没有匹配目标 hash/文件名的结果。因此已确认 App Control 阻止此文件，但尚不能把阻止归属到上述具体 policy。目标文件为 `pydantic_core/_pydantic_core.cp312-win_amd64.pyd`，SHA-256 `d38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297`。
- 管理员操作请求：先在同一 Sandbox guest 重现时导出 CodeIntegrity Operational 的 3077 与关联 3089 事件，并连同 3099 policy-load 信息确认实际 enforcing base policy；如果日志仍没有 block event，可对已核验的精确 `.pyd` 建立 **Hash 级** Allow rule。只有当 base policy 明确允许 supplemental 且管理方能把策略限定部署到一次性 Sandbox guest 时，才生成/签名并应用临时 supplemental policy；不可关闭 enforcement、创建宽泛路径/发布者规则或修改宿主机基线。替代方案是提供已获当前 Sandbox policy 授权的兼容签名 `.pyd`，并交付文件 SHA-256、签名者与版本/构建来源。取得管理员批准的策略或二进制后，先冻结新的 profile lock、manifest、checkpoint 和报告，再执行尚未运行的样本；原 pilot 终态不重开。Microsoft Wizard 的 event-log 规则生成支持 Hash 规则，supplemental policy 需由匹配的 base policy 允许，见 [event parsing](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/appcontrol-wizard-parsing-event-logs)、[supplemental policy creation](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/appcontrol-wizard-create-supplemental-policy) 和 [multiple policies](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/deploy-multiple-appcontrol-policies)。
- 离线缓存审计脚本 [Koishi Windows cache audit](../../scripts/phase3-windows-koishi-cache-audit.py) 对剩余 167 行及其冻结引用完成验证：167/167 profile locks、3,255/3,255 npm archives、167/167 plugin artifacts 均与冻结 SHA/SRI 一致；审计覆盖约 2.29 GB 的依赖档案和 290 MB 的 plugin artifacts，没有执行插件或 lifecycle script。此前 21 个缺失 registry npm archives 已按锁定 URL 下载到外部 artifact cache，逐个校验 SHA-512 后写入；最终报告 [cache audit](../../outputs/acceptance/phase3-p305-windows-koishi-cache-audit-20261025.json)，SHA-256 `24130cb65f49f0aca2507f999d15e2c4621fe2eb115b32ddaa37139fa8a366fb`。
- 仍有两条 `capture-website@2.3.0` 依赖锁定到同一个 Git SSH commit，当前 Worker 安全 allowlist 不接受该来源；不能自行改写成 npm registry 版本。另有 113 个 lock package 标记 `hasInstallScript`；禁用脚本时 `sqlite3` binding 仍需一个单独冻结并校验的 Windows prebuilt artifact/受控安装方案。新的 Koishi Host profile 仍需为需要它的条目显式冻结 provider，例如旧 pilot 的 `@koishijs/plugin-console` peer。完整 Windows affected 集合仍有 953 行未执行（786 NoneBot / 167 Koishi）；P3-05 保持 in-progress，P3-08/P3-10 依赖未满足。

### P3-05 Koishi service source crosswalk（2026-10-26；源码级发现，profile 未就绪）

- 对上一节 provider-candidate audit 的 13 个“无目录 `implements` metadata”服务名继续做离线源码核验。官方 [Core Context API](https://koishi.chat/en-US/api/core/context) 与 [Built-in Services](https://koishi.chat/en-US/api/) 将 `http`、`i18n` 归为 Koishi 内置服务；其余 11 个名称在 7 个 artifact/profile hash-verified 的 provider tarball 中找到服务类及服务 ID：六个外部 provider 包提供 BiliBiliLive、BiliBiliLogin、BiliBiliMovie、BiliBiliSearch、BiliBiliVideo、QhzySharp、fonts、localstorage、nazrin、skia；`koishi-plugin-hajimi-lizard@1.1.0` 本身注册 `hajimi` 服务。源码成员 SHA、服务构造器与可见 `ctx.plugin(...)` 注册点均写入报告；skia 记录为服务类与 metadata 证据，没有伪造显式注册调用。
- 报告还确认静态扫描器对 `koishi-plugin-hajimi-lizard` 的 `inject=['hajimi']` 两次命中均位于 `usage` 模板字符串中，不是可执行 inject 声明；这一个 required-service 计数是 scanner false positive。冻结目录元数据还存在字段路径差异：如 fonts 使用 `koishi.implements`，skia/Qhzy 包使用 `koishi.services.implements`；Qhzy metadata 声明的 `sharp` 与它源码实际构造的服务 ID `QhzySharp` 不同。
- 复现脚本 [phase3-windows-koishi-service-source-audit.py](../../scripts/phase3-windows-koishi-service-source-audit.py) 核验精确 catalog entry、包名/版本、artifact SHA、Windows profile lock blob SHA、package.json identity 与源码成员 SHA；报告 [service source audit](../../outputs/acceptance/phase3-p305-windows-koishi-service-source-audit-v1-20261026.json) canonical SHA `08e59310407435f3e50dd989eb716197a09faa6c31d8a97f89ec9283265bd669`，file/sidecar SHA `ed845c0112a857e6e2569ff257dbae284fb4f7524594988b6240739128e8ef57`。上一份 provider metadata-only 报告保持原样，作为较窄视图的历史证据。
- 源码候选不等于服务能加载或满足依赖。provider 的传递依赖档案没有重验，未选择 per-entry provider/config，combined target-entry locks 为 0/167。两条 Git SSH 引用、113 个 lifecycle-script occurrences 与 sqlite3 Windows native binding 仍待安全处置；skia 包还有运行时平台资源下载/native 闭包待定义。Koishi 167 项不派发，Windows/P3-05 整体不宣告完成；不重跑闭合 campaign、不运行插件、npm install、Worker 或 Sandbox。

### P3-05 Koishi per-entry requirements matrix（2026-10-26；profile 未就绪）

- 离线 join 167 个冻结 target identity 与静态源码需求、catalog provider 候选、源码级 provider crosswalk、原有 Windows profile lock。复现脚本 [entry requirements matrix](../../scripts/phase3-windows-koishi-entry-requirements-matrix.py)；报告 [matrix](../../outputs/acceptance/phase3-p305-windows-koishi-entry-requirements-matrix-v1-20261026.json)，canonical SHA `e5fa8a4a67dcb3a0ba771d36ec137c8386768919e83e5a292ba0e88feebfb042`，file/sidecar SHA `2bbe98ab0f04d7f6c5c7e5f89e8b108e73f3bee261ee34f64ad1884f7d9f10a4`。
- 矩阵覆盖 167/167 entries、152 条静态 required-service 记录；其中 `hajimi` 一条已由源码 audit 证实为模板字符串误报，剩 151 条 candidate/built-in 记录（132 candidate-only、19 documented Core built-in）。另 78 项未发现静态 required service。29 个服务名全部能关联到候选或 Core 文档依据；这不表示 provider 已选择。56 个目录候选记录的 tarball、包内 metadata 与 Windows lock blob 通过既有 audit 校验；11 个无 `implements` metadata 的源码级候选仍需重新核验 provider 传递依赖并冻结配置。
- 矩阵逐项复核了 167 个现有 target lock blobs，并将 113 个 `hasInstallScript` 标记、两个 Git SSH commit 引用及唯一 `sqlite3@5.1.7` 归到确切 target entry。combined Host lock/config 为 0/167，dispatch eligible=0，`profile_ready=false`。

### P3-05 Koishi lifecycle source audit（2026-10-26；仅归档审阅）

- 复现脚本 [lifecycle source audit](../../scripts/phase3-windows-koishi-lifecycle-source-audit.py) 对 167 个 target lock 引用的 51 个精确 npm tarball identity 逐一校验 SRI 并读取包内 `package.json`、生命周期命令和原生成员；没有解包到磁盘，也没有执行脚本。报告 [lifecycle audit](../../outputs/acceptance/phase3-p305-windows-koishi-lifecycle-source-audit-v1-20261026.json)，canonical SHA `f8ba063721607481beba16ddd72fafd7d38b8db0f3e6682cc32d5327138b2278`，file/sidecar SHA `222529e844c22dd77358795e749b286c86bac1cce4becf737c9781853dd6978c`。
- 113 个 lock marker 中，13 个由锁定的 `os`/`cpu` metadata 排除在 Windows x64 之外（6 个仅限 Darwin/Linux 的 ffmpeg 包与 7 个 `fsevents` occurrence）；100 个未被这些字段排除，其中 99 个归档确有显式 `preinstall`/`install`/`postinstall`/`prepare` 命令，另 1 个未见命令或 `binding.gyp`。这只是静态约束分类，不等于脚本会在当前 `npm ci` 路径运行。
- 51 个归档中的包名/版本均与 lock identity 一致；其中 3 个归档含 Windows x64 `.node` 成员，但只证明字节在归档内。精确 target `koishi-plugin-sleep-shiyi@1.0.2` 的 `sqlite3@5.1.7` 归档没有预编译 `.node`，命令为 `prebuild-install -r napi || node-gyp rebuild` 且带 `binding.gyp`；目前没有独立冻结的 prebuilt artifact 或安装路径。lifecycle policy 仍禁用，Skia 原生资源闭包也没有完成；不能据这份 audit 放行 scripts 或派发 Koishi。

### P3-05 Koishi provider transitive archive closure（2026-10-26；候选归档闭包已核验）

- 复现脚本 [provider closure audit](../../scripts/phase3-windows-koishi-provider-closure-audit.py) 读取 62 个 hash-verified candidate provider 的 Windows x64 lock blobs，核对每条 registry URL 与锁定 SRI，并逐字节复验外部缓存归档。报告 [provider closure](../../outputs/acceptance/phase3-p305-windows-koishi-provider-closure-audit-v1-20261026.json)，canonical SHA `b5f6ea3acb52b33e4a96c766f7e9add9153af2469f8ddd5fe9565e43096cace1`，file/sidecar SHA `cee7bdd4bdbb503f077abb8cbf7a0ce4416d5a62d83919a5ed6293fa89682333`。
- 62/62 provider locks、11,985/11,985 package records 与 1,055/1,055 unique registry archives 已核验，无缺失、SRI 不匹配或不支持的 URL；本轮仅从冻结 lock URL 补入 30 个缺失归档（439,132 bytes），每个文件在缓存前经过 SRI 校验。没有运行 npm、生命周期脚本或插件，也没有解包归档。
- 此项仅闭合候选 provider 的 registry archive bytes；没有选择 provider、冻结逐 entry 配置或合并 167 个 target Host lock。`combined_target_host_locks_frozen=0`，`provider_configurations_frozen=0`，`profile_ready=false`。Git SSH target dependency、lifecycle/native 安装策略与 sqlite3/Skia 闭包仍不能据此放行。

### P3-05 Koishi per-entry provider resolution audit（2026-10-26；候选覆盖已核验，未冻结 profile）

- 复现脚本 [provider resolution audit](../../scripts/phase3-windows-koishi-provider-resolution-audit.py) 将 167 个 target 的 152 条静态 service demand 与 provider-candidate、源码 crosswalk、Windows lock 和完整传递归档闭包逐项关联；已知 scanner false positive 1 条排除，19 条 Koishi Core built-in 需求有文档分类。
- 132 条外部 provider demand 中，131 条至少有一个 exact artifact、Windows lock 与全部 SRI-bound transitive archives 均验证的候选；唯一缺口是 `koishi-plugin-pointmintmarket-ncm@0.1.2` 需要 `market` service，但其唯一 candidate `koishi-plugin-pointmintmarket@0.1.5` 的严格 profile lock 因 peer dependency conflict 未生成。该 provider 声明 peer `koishi-plugin-pointmint@^0.0.9`；冻结目录中只有 `koishi-plugin-pointmint@0.0.8`，不能以不匹配版本冒充可用 provider。
- 报告 [provider resolution audit](../../outputs/acceptance/phase3-p305-windows-koishi-provider-resolution-audit-v1-20261026.json)，canonical SHA `456591defbf205ade96b766f7d6fcdc2e400acc7cc92e3d06963ba0486a37585`，file/sidecar SHA `4eb81b7f50f3bd0312932b4690f68cf00893d0c38ad0c1f9d2d347abde25cee5`。这只是候选覆盖审计：尚未选择 providers、冻结任一逐项配置或生成 combined Host lock，仍为 0/167；`profile_ready=false`、`dispatch_eligible=0`、compatibility `not_evaluated`。Git SSH、lifecycle/native、sqlite3、Skia 与配置 schema 仍须分别闭合。

### P3-05 Windows Sandbox 策略基线差异（2026-10-25）

- v4 是 native-only 诊断，没有执行 catalog unit，也没有改写已封存 pilot。报告绑定 pilot v2 manifest canonical SHA 0796fad72d14f0e84cfd91b77e63177873a26510dfa3adee55c9a3298feed4fa、profile lock SHA e58651dfa5e46b3ebc8590b7ba89fd7aef86e8dd42d47580c9ed7fb536d880df、runtime admission SHA 3a1a1d5755df58ed8097a58b924cb683b278f475102c11be5de57fad3fca7d70、pilot execution report SHA 5e1f02f68e65faecf8048a5fc722cf613dda33b55792ae1e0e09a6a35d95d976，以及原生扩展 SHA d38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297；本地 manifest、profile lock、admission 和 execution report 文件哈希复核一致。该扩展 import 与直接 LoadLibrary 成功。原始 EVTX 有 18 条事件，全部为 3099，没有 3076、3077 或 3089。报告：[v4 JSON](../../outputs/acceptance/phase3-p305-windows-native-dll-diagnostic-v4-20261025.json)，SHA-256 d7d50a83ec8cdeedb7d2c16f7741a04454a263dc4cfd2b83b9df020dccbd54fb；[guest EVTX](../../outputs/acceptance/phase3-p305-windows-native-dll-diagnostic-v4-20261025.evtx)，SHA-256 e5e1790e0193dc518c8c7ecf44c8eee8b78da7f1eb111fc87b9e1e0075701fa4。
- v4 3099 中，VerifiedAndReputableDesktopEvaluation base policy GUID 为 {1283ac0f-fff1-49ae-ada1-8a933130cad6}、Options 为 0xb18d2714；EvaluationFlightSupplemental GUID 为 {2678656c-05ef-481f-bc5b-ebd8c991502d}、Options 为 0xd1892304。两者 bit 16（Audit Mode）均置位，base policy 的 bit 10（Allow Supplemental Policies）置位。报告还列出其他活动 policy，包括 Audit Mode 未开启的 Microsoft Windows Endpoint Security Policy；因此只能说上述 Evaluation policy 处于 audit mode，不能称整个 guest 仅处于 audit mode。supplemental signer trust 未确认。
- 这两个 GUID 与 Microsoft 的 Inbox App Control policy 清单吻合：Evaluation base 在 Windows 11 Smart App Control evaluation mode 下活动，Evaluation Flight Supplemental 与 evaluation mode 及 Windows Insider enrollment 相关。相对地，v2 使用的 GUID {0283ac0f-fff1-49ae-ada1-8a933130cad6} / {1678656c-05ef-481f-bc5b-ebd8c991502d} 对应 Smart App Control turned on 的 base/supplemental policy。Windows Sandbox 每次启动提供全新、pristine guest；因此最可能的解释是 v4 guest 带有 SAC Evaluation 状态，而 v2 guest 使用 SAC-on enforcement policy set。该解释与当前载入的 GUID/options 及不同 load 结果相符，但仍需在同一实际 Sandbox 中用 CiTool 确认状态与 policy 部署来源，不能把相关性说成唯一因果。参考 [Inbox App Control policies](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/inbox-appcontrol-policies)、[Windows Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/) 和 [Smart App Control verification](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/test-your-app-with-smart-app-control)。
- 本机只读核查：宿主是 Windows 11 Pro for Workstations build 26200，Windows Sandbox package 0.8.107.0；宿主注册表 VerifiedAndReputablePolicyState 为 0（Off），但宿主 CiTool -lp -json 返回 HRESULT 0x80070005（Access Denied）。宿主 SAC 值不代表一次性 guest 状态。主机查询不够权限时，应在准备接受的 guest 中执行 CiTool.exe -lp 并保留 Friendly Name、Policy ID/Hash、Options 和 Is Currently Enforced，再与 guest 3099 EVTX 对账。Microsoft 对 3099 Options 中 bit 16、bit 10 的定义见[事件标记说明](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-tag-explanations)；3077 enforcement block 与关联 3089 需来自同一 guest，见[App Control troubleshooting](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/appcontrol-debugging-and-troubleshooting)。
- 证据限制：v4 的 driver_source_sha256 为 bb2b42115840ea76bd64eb3bca25b1c33fae433c967a7c9a5846a98c4e894c15，但对应源文件未随报告提供，且当前 workspace 未找到，因此尚不能把该 hash 独立绑定到可执行源码。报告字段 generated_at_utc 带有 +08:00，每条 policy 的 latest_event_utc 又与事件记录时间不符；以原始 EVTX 的事件 ID、policy GUID/hash/options 为证，时间字段保留为不一致项。
- 已新增只读采集器 [phase3-windows-policy-inventory.ps1](../../scripts/phase3-windows-policy-inventory.ps1)（PowerShell 语法检查通过；尚未在 Sandbox 执行）。它在 guest 内输出 OS build、CiTool -lp -json、3076/3077/3089/3099 事件和 CodeIntegrity EVTX。下一次需用禁网的一次性 guest 运行；脚本输入只读共享，结果目录单独授予 Sandbox 写入。当前 wsb.exe list 后出现活动 guest ID 3323b88e-1f8c-438f-8b73-c56f85597af8，来源不明；本轮未连接、执行命令或停止它。待确认 guest 所属并在其退出后，先核实没有其他 Sandbox，再开自己的诊断 guest。
- 暂不生成/部署 Hash Allow、不改 App Control policy、不运行 Python catalog unit。若确认默认 SAC Evaluation guest 是 P3-05 接受基线，应冻结独立 profile lock、manifest、checkpoint/report 后再执行；若要求 enforcement 验收，必须先在确认的强制策略下重跑 native-only 诊断并绑定 policy inventory/events。原 pilot 的 10 个 unknown 保持不变。

### P3-05 Windows NoneBot Evaluation campaign（2026-10-25；786 项已闭合；P3-05 总体仍进行中）

- 用户选择 Windows Sandbox 默认 Evaluation profile。专用 guest 的只读 preflight 绑定 Evaluation base {1283ac0f-fff1-49ae-ada1-8a933130cad6} / options 0xb18d2714 与 supplemental {2678656c-05ef-481f-bc5b-ebd8c991502d} / options 0xd1892304；两者 Audit Mode bit 置位。inventory、EVTX、解析事件与 preflight JSON 的 SHA sidecar 均已保存于 outputs/acceptance/phase3-p305-windows-default-evaluation-preflight-20261025/。CiTool -lp -json 在 guest 返回 0x80073bc3，故 policy 身份/options 取自 3099 事件；采集限制已记录。没有部署或更改 App Control policy。
- 新 manifest phase3-p305-windows-nonebot-evaluation-manifest-20261025.json canonical SHA 6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914，仅选取 786 个未与旧 pilot 终态重叠的 NoneBot 条目；profile lock SHA 31814e498cab1365fa201497faa9b3918316db701d8f503c212f32d20e9a286a，P3-04 admission SHA 3a1a1d5755df58ed8097a58b924cb683b278f475102c11be5de57fad3fca7d70。--validate-only 确认分母和绑定身份一致。
- 2026-10-25T14:41:50Z checkpoint SHA 85258ab7ae39696339168f3e7957f5998b81cf615a9d747a7652df1e30427083：20/786 settled，failed=8、skipped=12、passed=0、unknown=0、cleanup confirmed=20/20、remaining=766；status in-progress、health blocked、behavior not_evaluated。controller PID 33984 活动；该时点 Sandbox ID 为 c8c7537f-cb91-4106-a458-4262ce56f368。最终报告尚未生成；后续继续同一 controller/checkpoint，不重复派发或改写终态。
- 2026-10-25T15:01:35Z checkpoint SHA 5e48260f0f577056d19b0570ec13bb9ca69d0772e03455a130727106e31fa2ad：61/786 settled，failed=17、skipped=44、passed=0、unknown=0、cleanup confirmed=61/61、remaining=725；status in-progress、health blocked、behavior not_evaluated。controller PID 33984 仍活动；采样时无 Sandbox，最终报告尚未生成。checkpoint 未发现权限类错误。只读 validate-only 被 campaign lock 拒绝，未启动 Worker；继续监视同一 controller/checkpoint，不重复派发或改写终态。
- 2026-10-25T15:03:52Z checkpoint SHA e96b21f5e43eb8372ac52c7844e0e3536763d4bc6501542eb09435ea17fef9a0：63/786 settled，failed=18、skipped=45、passed=0、unknown=0、cleanup confirmed=63/63、remaining=723；status in-progress、health blocked、behavior not_evaluated。controller PID 33984 仍活动；采样时无 Sandbox，最终报告尚未生成。checkpoint 未发现权限类错误。只读 validate-only 被 campaign lock 拒绝，未启动 Worker；继续监视同一 controller/checkpoint，不重复派发或改写终态。
- 2026-10-25T15:05:35Z checkpoint SHA 3910f1983b09df30844f4157f7760e7bef4fcbbc4f0932132fc89e6a8b32443e：66/786 settled，failed=19、skipped=47、passed=0、unknown=0、cleanup confirmed=66/66、remaining=720；status in-progress、health blocked、behavior not_evaluated。controller PID 33984 仍活动；采样时无 Sandbox，最终报告尚未生成。checkpoint 未发现权限类错误。只读 validate-only 被 campaign lock 拒绝，未启动 Worker；继续监视同一 controller/checkpoint，不重复派发或改写终态。
- 2026-10-25T15:16:19Z checkpoint SHA 76b63fa89e24d5614e0a8c53ccaf002fe9afde8df348f40829ffd981a14ad5c0：88/786 settled，failed=26、skipped=62、passed=0、unknown=0、cleanup confirmed=88/88、remaining=698；status in-progress、health blocked、behavior not_evaluated。controller PID 33984 仍活动；Sandbox f4501ead-990b-47b4-b7db-577d731ef64a 活动，controller 的 wsb.exe 子进程 PID 8260 正收集 guest 输出。最终报告尚未生成，checkpoint 未发现权限类错误；继续监视同一 controller/checkpoint，不重复派发或改写终态。
- 2026-10-25T15:16:39Z checkpoint SHA a01cedcfd9ac95ae8e5c84b134e3763612236aca862d363fde07bfcd983048a7：89/786 settled，failed=26、skipped=63、passed=0、unknown=0、cleanup confirmed=89/89、remaining=697；status in-progress、health blocked、behavior not_evaluated。controller PID 33984 仍活动；采样时无 Sandbox/Worker，最终报告尚未生成。checkpoint 未发现权限类错误；继续监视同一 controller/checkpoint，不重复派发或改写终态。
- 2026-10-25T15:32:04Z checkpoint SHA 45e5f3b4f1b585d7ff590e45006d4d1965b8fa000ca6ee651ce96aef756622e2：127/786 settled，failed=39、skipped=86、passed=0、unknown=2、cleanup confirmed=125、cleanup_unconfirmed=0、remaining=659；status in-progress、health blocked、behavior not_evaluated。两个 unknown 是 PyPI metadata SSL unexpected EOF 导致的 worker-preparation failure；插件未执行，unknown 保留。controller PID 33984 仍活动；之前的 Sandbox c092aae7-cf57-445e-a0fa-b046b997a73d 已于 checkpoint capture 前退出。最终报告尚未生成；继续监视同一 controller/checkpoint，不重复派发或改写终态。
- 该批次仅代表默认 Evaluation profile 下的 Sandbox 加载证据，不能证明 enforcement 接受。旧 pilot 10 个 unknown 保持不变；不生成 Hash Allow、不修改策略、不在宿主机执行插件。旧 pilot 后总体尚有 953 个 Windows affected 项待覆盖，本批次完成后仍需独立处理 167 个 Koishi 项及剩余依赖；P3-05、P3-08、P3-10 均不可提前声明完成。

- 2026-10-25T15:52:42Z observation；checkpoint SHA 72c472a2bc27abfef4149330166b1d1181f85a520cc82a0f6b9f233f56a2f4cd：165/786 settled，passed=0、failed=55、skipped=108、unknown=2、cleanup confirmed=163、cleanup_unconfirmed=0、remaining=621；status in-progress、health blocked、behavior not_evaluated。checkpoint sidecar 与本体 SHA 匹配，冻结 manifest/profile/admission 哈希仍与既定身份一致。controller PID 33984 仍运行；采样时无活动 Sandbox/Worker，最终报告尚未生成。两个 SSL metadata worker-preparation unknown 保持未知；没有 Sandbox 权限错误，不重复派发或重跑终态。

- 2026-10-25T15:55:22Z observation；checkpoint SHA e68bbec6eff047cb2f9c21d3e09ff2c10c051c95b31378e07d27f0b92f8efb1d：171/786 settled，passed=0、failed=57、skipped=112、unknown=2、cleanup confirmed=169、cleanup_unconfirmed=0、remaining=615；status in-progress、health blocked、behavior not_evaluated。checkpoint 内容稳定且 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均与冻结值匹配。controller PID 33984 仍运行；采样时无活动 Sandbox/Worker，最终报告尚未生成。两个 SSL metadata worker-preparation unknown 保持未知；没有 Sandbox 权限错误，不重复派发或重跑终态。

- 2026-10-25T15:56:13Z observation；checkpoint SHA 2e0b75b96cb503308a3ec5cdfb89f2d7e66507e5526069e5582f5d3c676e417e：174/786 settled，passed=0、failed=58、skipped=114、unknown=2、cleanup confirmed=172、cleanup_unconfirmed=0、remaining=612；status in-progress、health blocked、behavior not_evaluated。checkpoint 内容稳定且 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均与冻结值匹配。controller PID 33984 仍运行；Sandbox 3cebbbdb-ba01-4a86-90eb-42b11389358c 活动，wsb.exe 子进程 PID 35496 正在采集 guest 输出，最终报告尚未生成。两个 SSL metadata worker-preparation unknown 保持未知；没有 Sandbox 权限错误，不重复派发或重跑终态。

- 2026-10-25T16:01:35Z observation；checkpoint SHA 0281193a598a6403e79e0d131f84a16d87b2628019d10d2d9c7858cb6aafe097：189/786 settled，passed=0、failed=65、skipped=122、unknown=2、cleanup confirmed=187、cleanup_unconfirmed=0、remaining=597；status in-progress、health blocked、behavior not_evaluated。checkpoint 内容稳定且 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均与冻结值匹配。controller PID 33984 仍运行；Sandbox 98ff9031-8743-48f1-93bd-404588b562fd 活动，wsb.exe 子进程 PID 23172 正在采集 guest 输出，最终报告尚未生成。两个 SSL metadata worker-preparation unknown 保持未知；没有 Sandbox 权限错误，不重复派发或重跑终态。


- 2026-10-25T16:02:28Z observation；checkpoint SHA cf3ef103080a84cc6849895eb568492183482a36233c7954bb32339d58d44167：192/786 settled，passed=0、failed=65、skipped=125、unknown=2、cleanup confirmed=190、cleanup_unconfirmed=0、remaining=594；status in-progress、health blocked、behavior not_evaluated。checkpoint 内容稳定且 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均与冻结值匹配。controller PID 33984 仍运行；采样时无活动 Sandbox/Worker，最终报告尚未生成。两个 SSL metadata worker-preparation unknown 保持未知；没有 Sandbox 权限错误，不重复派发或重跑终态。
- 2026-10-25T16:21:30Z observation；checkpoint SHA 4aed00a3c7f57fcb44538005e57081e6066725718e96b577097625451d563736：251/786 settled，passed=0、failed=82、skipped=166、unknown=3、cleanup confirmed=248、cleanup_unconfirmed=0、remaining=535；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox d9bcf919-1679-4b3e-970f-fc0fe2b03230 活动，wsb.exe exec PID 19476 正在运行；最终报告尚未生成。3 个 PyPI metadata TLS unexpected EOF worker-preparation unknown 保持未知；checkpoint 未显示 Sandbox 权限错误，不重复派发或重跑。
- 2026-10-25T16:33:22Z observation；checkpoint SHA abaa961de0fbe6a6798603b36dc2b00e3be0ff42aa68eed43cb2d40e93cf7c6e：285/786 settled，passed=0、failed=95、skipped=187、unknown=3、cleanup confirmed=282、cleanup_unconfirmed=0、remaining=501；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox e3a3da3f-d688-42ad-9ac1-c7b52d3858ac 活动，wsb.exe exec PID 17580 正在运行；最终报告尚未生成。3 个 PyPI metadata TLS unexpected EOF worker-preparation unknown 保持未知；checkpoint 未显示 Sandbox 权限错误，不重复派发或重跑。



### P3-05 Koishi Host-service probe 修复（2026-10-25；Linux diagnostics 完成）

- 复核 frozen `koishi-plugin-auto-checkin-all@1.0.3` 发布物后发现 CommonJS 模块并列导出 `apply` 与 `inject`；probe 原来只选择 `loaded.apply`，丢弃 required-service 声明。该 loader 缺陷使 Koishi Core 把插件回调放入等待队列，而插件自身又重试查询，最终表现成 180 秒 Worker timeout。
- 新增 semantics v3 分支：保留并绑定模块的 `inject` 元数据；按 probe spec 中的 Host provider 清单装载已锁定 provider，读取 required services，并在任何目标插件回调前分类缺失服务。缺失项输出结构化 `skipped / required_host_service_unavailable`，不再进入插件重试。v1/v2 probe 及原 campaign 身份保持不变；semantics 3 也进入新 campaign identity。
- 真实 Linux Docker focused diagnostic 使用 frozen `auto-checkin-all@1.0.3` artifact、combined profile 锁定的 `@minatojs/driver-sqlite@4.7.0`、生产 Host `HostLifecycle` / package-lock identities。最终 LF-byte-locked profile SHA-256 `4f76f59594b1fab0f255b116a986e186ed30e5b101a682b22b7bce388f257482`；Worker 15.784 秒完成，protocol 有效，cleanup confirmed；required services 为 `database`,`cron`，当前 Host profile 提供 database，未提供 cron，因此 native load 明确 skipped，目标插件回调未执行。报告 [auto-checkin v3 diagnostic](../../outputs/acceptance/phase3-p305-host-service-autocheckin-v3-diagnostic-attempt3-20261025.json)，combined [host package-lock](../../outputs/acceptance/phase3-p305-koishi-host-profile-auto-checkin-20261025/package-lock.json)。
- 真实 Linux Docker focused diagnostic 对 frozen `koishi-plugin-smmcat-economy@0.1.7` artifact/profile 完成离线安装；Worker 9.591 秒完成并确认 cleanup。required services `localstorage`,`puppeteer` 均未由该 Native Pilot profile 提供，probe 输出结构化 skipped，未调用插件回调。报告：[smmcat v3 diagnostic](../../outputs/acceptance/phase3-p305-host-service-smmcat-v3-diagnostic-20261025.json)。这不是插件兼容性结论；puppeteer browser provider 仍需单独冻结与评估。
- 验证：Windows 本机 CPython 3.14.7 + Node 的 `lab.tests.test_pilot_workers` 与 `lab.tests.test_automation_recovery` 共 18/18 passed；WSL Linux CPython 3.12.14 对应 suites 为 18 tests、16 passed、2 Node-only skipped，Windows 已将两个 Node 用例实跑 2/2 passed。命令与日志哈希见 [focused verification](../../outputs/acceptance/phase3-p305-v3-focused-verification-20261025.json)。`pwsh -NoProfile -File init.ps1 -Mode docs` 已通过 19 documents / 305 local links；最终日志摘要见 [docs check](../../outputs/acceptance/phase3-docs-check-p305-v3-20261025.json)。`git diff --check` 通过。
> - 这只修复 Linux probe 归因并关闭两个旧 unknown 的技术成因诊断；原 semantics-v2 campaign 保持 2 个 unknown 不变。此处记录的 Linux diagnostic 未冻结新的 20-entry campaign。Windows v2 20-entry cohort 及完整 Windows 分母另见上方「P3-05 Windows affected pilot」；全量 affected Windows 仍未执行，P3-05 保持 in-progress，P3-08/P3-10 不得开始，行为兼容仍 `not_evaluated`。
- 2026-10-25T16:46:35Z observation；checkpoint SHA 7d48269e6be2af27ea07862e08d7aaac66342bfcdf911cacdc7083d69a4af4a1：326/786 settled，passed=0、failed=106、skipped=216、unknown=4、cleanup confirmed=322、cleanup_unconfirmed=0、remaining=460；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox 7076360e-c387-4539-868d-e113fe6ecbc5 活动，wsb.exe exec PID 35328 正在运行；最终报告尚未生成。4 个 PyPI metadata worker-preparation unknown 因 TLS EOF 或超时未运行插件并保持未知；未见 Sandbox 权限错误，不重复派发或重跑。
- 2026-10-25T16:49:15Z observation；checkpoint SHA f72ddcf140999b24a8c220d2aaa0b640564e16bedbe71af2728c3fe504ae6cb7：333/786 settled，passed=0、failed=108、skipped=221、unknown=4、cleanup confirmed=329、cleanup_unconfirmed=0、remaining=453；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox 5306589b-f639-4145-86e1-f4eb5a2f3402 活动，wsb.exe exec PID 1852 正在运行；最终报告尚未生成。4 个 PyPI metadata worker-preparation unknown 因 TLS EOF 或超时未运行插件并保持未知；未见 Sandbox 权限错误，不重复派发或重跑。
- 2026-10-25T17:01:21Z observation；checkpoint SHA eda41370fa0d2be91a51350d04815e13cb464409829284f5eee451469d2d80dc：358/786 settled，passed=0、failed=119、skipped=235、unknown=4、cleanup confirmed=354、cleanup_unconfirmed=0、remaining=428；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox 98d7c521-f16c-431b-9a8a-b9ad341cf807 活动，wsb.exe exec PID 4412 正在运行；最终报告尚未生成。4 个 PyPI metadata worker-preparation unknown 因 TLS EOF 或超时未运行插件并保持未知；未见 Sandbox 权限错误，不重复派发或重跑。
- 2026-10-25T17:16:28Z observation；checkpoint SHA 7405bc04b1527b3631f9f7ddcb1ada07016d770f7213a1afe7a34f38239ed76c：404/786 settled，passed=0、failed=133、skipped=267、unknown=4、cleanup confirmed=400、cleanup_unconfirmed=0、remaining=382；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox 39bd4d1b-543c-4cc2-b446-7f42ce888d63 活动，wsb.exe exec PID 34740 正在运行；最终报告尚未生成。133 个失败为 native_load_failed，267 个 skipped 为 missing_entry_bound_fixture；4 个 PyPI metadata worker-preparation unknown 因 TLS EOF 或超时未运行插件并保持未知。checkpoint 未见权限类错误，不重复派发或重跑。
- 2026-10-25T17:31:28Z observation；checkpoint SHA 188f1ff4a87682a7daf2f627069c5042e95bf0f4aa531013dd6f2729dbfd0d04：451/786 settled，passed=0、failed=151、skipped=296、unknown=4、cleanup confirmed=447、cleanup_unconfirmed=0、remaining=335；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox 23e00a08-5148-4680-8ae9-4ce702dee0db 活动，wsb.exe exec PID 11336 正在运行；最终报告尚未生成。151 个失败为 native_load_failed，296 个 skipped 为 missing_entry_bound_fixture；4 个 PyPI metadata worker-preparation unknown 因 TLS EOF 或超时未运行插件并保持未知。不得将 unknown 改成通过；不重复派发或重跑终态样本。
- 2026-10-25T17:46:29Z observation；checkpoint SHA f9419f9d3bdc3ef5988753ccf7f11a411745c1a3bfa286a193b2b061cdca64fd：500/786 settled，passed=0、failed=171、skipped=325、unknown=4、cleanup confirmed=496、cleanup_unconfirmed=0、remaining=286；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox 38a4e139-8ed0-48d9-9eb8-d68a2318811c 正在启动，wsb.exe start PID 33380 活动；尚未出现在 wsb.exe list，最终报告尚未生成。171 个失败为 native_load_failed，325 个 skipped 为 missing_entry_bound_fixture；4 个 PyPI metadata worker-preparation unknown 因 TLS EOF 或超时未运行插件并保持未知。checkpoint 无权限类错误记录，不重复派发或重跑。
- 2026-10-25T18:01:40Z observation；checkpoint SHA e8dcfc022ca08afc1e140506ac8e62fd169ed8cca677791471eeb1640efec61f：547/786 settled，passed=0、failed=186、skipped=357、unknown=4、cleanup confirmed=543、cleanup_unconfirmed=0、remaining=239；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox 89b06abc-fe41-46e8-8544-720228b369d1 活动，wsb.exe exec PID 14276 正在执行 collect-output.ps1；最终报告尚未生成。186 个失败为 native_load_failed，357 个 skipped 为 missing_entry_bound_fixture；4 个 PyPI metadata worker-preparation unknown 因 TLS EOF 或超时未运行插件并保持未知。checkpoint 未见权限类错误记录，不重复派发或重跑。
- 2026-10-25T18:16:36Z observation；checkpoint SHA d015e40caff10bbb0c7670cb1c0a79d691cea1c94317b8422190ccabc4b5e672：595/786 settled，passed=0、failed=198、skipped=393、unknown=4、cleanup confirmed=591、cleanup_unconfirmed=0、remaining=191；status in-progress、health blocked、behavior not_evaluated。checkpoint 及 sidecar SHA 匹配；manifest canonical/file SHA、profile-lock SHA 与 admission SHA 均保持冻结值。controller PID 33984 运行；Sandbox 4aafd4e1-1289-4ef9-a138-776c6488c259 活动，wsb.exe exec PID 34756 正在运行；最终报告尚未生成。198 个失败为 native_load_failed，393 个 skipped 为 missing_entry_bound_fixture；4 个 PyPI metadata worker-preparation unknown 因 TLS EOF 或超时未运行插件并保持未知。不得将 unknown 改成通过；不重复派发或重跑终态样本。
- 2026-10-25T18:31:49Z observation；checkpoint SHA a5a6e1199b867143f658957d4bfa4725b55e6b66558a078d65548864014c60b6：640/786 settled，passed=0、failed=212、skipped=424、unknown=4、cleanup confirmed=636、cleanup_unconfirmed=0、remaining=146；status in-progress、health blocked、behavior not_evaluated。checkpoint sidecar SHA 匹配；manifest canonical SHA 6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914、manifest file SHA f576591f8854b2758c0b7270499410617dd26af5cbf8d49a3f58d75f0410ce14、profile-lock SHA 31814e498cab1365fa201497faa9b3918316db701d8f503c212f32d20e9a286a 与 admission SHA 3a1a1d5755df58ed8097a58b924cb683b278f475102c11be5de57fad3fca7d70 均与冻结值匹配。controller PID 33984 仍运行；Sandbox c3d5000b-3acf-4cc9-9261-2df12bbc9566 活动，wsb.exe exec PID 22784 活动；execution report 尚未生成。212 个 failed 为 native_load_failed，424 个 skipped 为 missing_entry_bound_fixture；4 个 unknown 为 worker_preparation_failed（3 个 PyPI metadata SSL unexpected EOF、1 个 read timeout），保持未知。checkpoint 中无 Sandbox 权限错误记录；不重复派发或重跑终态。
- 2026-10-25T18:33:30Z observation；checkpoint SHA 227226daacc31ea31b248ece51f862e2fde9f925e59ee3c04a93f42cc2cd4c01：645/786 settled，passed=0、failed=212、skipped=429、unknown=4、cleanup confirmed=641、cleanup_unconfirmed=0、remaining=141；status in-progress、health blocked、behavior not_evaluated。checkpoint sidecar SHA 匹配；manifest canonical SHA 6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914、manifest file SHA f576591f8854b2758c0b7270499410617dd26af5cbf8d49a3f58d75f0410ce14、profile-lock SHA 31814e498cab1365fa201497faa9b3918316db701d8f503c212f32d20e9a286a 与 admission SHA 3a1a1d5755df58ed8097a58b924cb683b278f475102c11be5de57fad3fca7d70 均与冻结值匹配。controller PID 33984 仍运行；Sandbox 3cd28a8a-44d7-4ded-9a38-af913d6f28a9 活动，wsb.exe exec PID 24516 活动；execution report 尚未生成。212 个 failed 为 native_load_failed，429 个 skipped 为无 entry-bound fixture；4 个 unknown 为 worker_preparation_failed（3 个 PyPI metadata SSL unexpected EOF、1 个 read timeout），保持未知。checkpoint 中无 Sandbox 权限错误记录；不重复派发或重跑终态。
- 2026-10-25T18:46:31Z observation；checkpoint SHA 4a4dc234fc32cb34cc2d083fea5128a278017e00222ca3790c061f3683768433：685/786 settled，passed=0、failed=223、skipped=458、unknown=4、cleanup confirmed=681、cleanup_unconfirmed=0、remaining=101；status in-progress、health blocked、behavior not_evaluated。checkpoint sidecar SHA 匹配；manifest canonical SHA 6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914、manifest file SHA f576591f8854b2758c0b7270499410617dd26af5cbf8d49a3f58d75f0410ce14、profile-lock SHA 31814e498cab1365fa201497faa9b3918316db701d8f503c212f32d20e9a286a 与 admission SHA 3a1a1d5755df58ed8097a58b924cb683b278f475102c11be5de57fad3fca7d70 均与冻结值匹配。controller PID 33984 仍运行；Sandbox 44d1f763-c16a-461b-93bd-b68f279997ee 活动，wsb.exe exec PID 33804 活动；execution report 尚未生成。223 个 failed 为 native_load_failed，458 个 skipped 为 missing_entry_bound_fixture；4 个 unknown 为 worker_preparation_failed（3 个 PyPI metadata SSL unexpected EOF、1 个 read timeout），保持未知。checkpoint 中无 Sandbox 权限错误记录；不重复派发或重跑终态。
- 2026-10-25T19:01:24Z observation；checkpoint SHA b74e3af1c65115164a5c4f821f14a6e0936f57e0e1006ac294b4fa4bad7d9b9a：731/786 settled，passed=0、failed=234、skipped=493、unknown=4、cleanup confirmed=727、cleanup_unconfirmed=0、remaining=55；status in-progress、health blocked、behavior not_evaluated。checkpoint sidecar SHA 匹配；manifest canonical SHA 6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914、manifest file SHA f576591f8854b2758c0b7270499410617dd26af5cbf8d49a3f58d75f0410ce14、profile-lock SHA 31814e498cab1365fa201497faa9b3918316db701d8f503c212f32d20e9a286a 与 admission SHA 3a1a1d5755df58ed8097a58b924cb683b278f475102c11be5de57fad3fca7d70 均与冻结值匹配。controller PID 33984 仍运行；Sandbox 6bdd6d4e-7595-481f-8377-674b2ecafc0d 活动，wsb.exe exec PID 4876 活动；execution report 尚未生成。234 个 failed 为 native_load_failed，493 个 skipped 为 missing_entry_bound_fixture；4 个 unknown 为 worker_preparation_failed（3 个 PyPI metadata SSL unexpected EOF、1 个 read timeout），保持未知。checkpoint 中无 Sandbox 权限错误记录；不重复派发或重跑终态。
- 2026-10-25T19:18:50Z closure observation；checkpoint SHA 9a7e700fef65f154db96aaba8df5a9362603ed4dd513e5e4ad3a37557e6e5047，execution report SHA 7bc048fb4006018213006ad7444ee9bafdf5270a5018ffc7611c6e2ba9e25afd；两者 sidecar 均匹配。NoneBot Evaluation campaign 786/786 terminal，passed=0、failed=253、skipped=529、unknown=4、remaining=0；cleanup_confirmed=782、cleanup_unconfirmed=0，attempted=782、未执行=4；report status=completed、health=blocked、behavior=not_evaluated。冻结 manifest canonical SHA 6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914、profile lock SHA 31814e498cab1365fa201497faa9b3918316db701d8f503c212f32d20e9a286a、admission SHA 3a1a1d5755df58ed8097a58b924cb683b278f475102c11be5de57fad3fca7d70 与文件哈希匹配；786 个唯一 report selection identity 与冻结 manifest 选择集合完全相等。4 个 unknown 均为 worker_preparation_failed（3 个 PyPI metadata SSL unexpected EOF、1 个 read timeout），attempted=false，保持 unknown。终态采样时 controller、wsb.exe exec 和 Sandbox 均已退出。Windows affected 仍有 167 个 Koishi 项；cache audit 证实 dependency locks 167/167、archives 3,255/3,255、plugin artifacts 167/167，但没有独立完整 Host/native profile：2 个 Git SSH source 不在 Worker allowlist，113 个 lifecycle-script occurrence 需另定受控策略；不派发 Koishi。P3-05 总体仍 in-progress，不重启 WSL。

### P3-05 Koishi provider-candidate audit（2026-10-26；profile 仍未就绪）

- 离线复核剩余 167 项的静态 `inject` 服务名与冻结目录的 `manifest.service.implements`。29 个服务名中，16 个有目录候选；15 个服务名至少有一个候选通过 provider tarball hash、包内 `koishi.service.implements` metadata 与 Windows dependency-lock blob hash 检查，共 56 个候选记录。候选的传递依赖归档未在本审计重验，profile lock blob hash 也不等于 target-entry combined lock。
- 官方 [Core Context API](https://koishi.chat/en-US/api/core/context) 将 HTTP 与 I18n 列为内置服务；其余 11 个未找到目录 `implements` 声明的名称是 BiliBiliLive、BiliBiliLogin、BiliBiliMovie、BiliBiliSearch、BiliBiliVideo、QhzySharp、fonts、hajimi、localstorage、nazrin、skia。它们仍需依据冻结 consumer source/peer 关系逐项归类；目录没有声明不等于服务绝不存在。
- 审计报告 [provider candidate audit](../../outputs/acceptance/phase3-p305-windows-koishi-provider-candidate-audit-v1-20261026.json)：canonical SHA `7f8e2efcbfa8ca7daa125327f38af21d5b105b5259a09a0dae361f0102703204`，file/sidecar SHA `1425619ed40d45742688b8cf6b942fbda805f8c77a8fd7b527e99bed8699e0be`；复现脚本 [phase3-windows-koishi-provider-candidate-audit.py](../../scripts/phase3-windows-koishi-provider-candidate-audit.py)。服务依赖需按 Koishi 的 [`required` / `optional` / `implements` 元数据](https://koishi.chat/en-US/guide/develop/publish)区分；官方文档说明 service plugins 可提供抽象或直接服务（[service plugins](https://koishi.chat/en-US/market/service)）。
- 该报告只证明冻结目录中存在符合已锁 artifact/profile 元数据的候选；没有选定通用或逐 entry provider，没有冻结 provider config，也没有为 167 个目标 entry 生成 combined lock（0/167）。`profile_ready=false`。两条 Git SSH source、113 个 lifecycle-script occurrences 与 sqlite3 Windows binding 策略仍未解决；不得派发 Koishi、不得重跑既有终态 campaign。无 npm install、catalog plugin、Worker 或 Sandbox 执行。
