# 第三阶段：可靠自动化检测与行为验证

日期：2026-10-20（Asia/Shanghai）。状态：工程方案，尚未实施。代码基线：`a311ac8`，S2-12 历史裁决保持不变。

## 目标与边界

交付一套能在故障后停止扩散、恢复执行、保留证据，并对明确场景自动判断兼容性的检测流水线。沿用 Rust Core、原生 Hosts、SQLite queue、Docker/Windows Sandbox 和已冻结目录，不引入新的分布式任务平台。

优先级分成三步：恢复可信执行能力 → 对受影响集合补测 → 执行小规模、具备独立 oracle 的双路径行为验证。基础设施健康没有通过前，不派发目录插件；没有行为 fixture，不宣称行为通过。

本阶段保留 `p2-41649744e057d61031d5` 的 5,690 entries / 11,380 entry×OS 分母。暂不刷新官方目录，不自动升级插件、framework 或依赖锁，不自动修补第三方源码。真实平台登录、凭证、发送和收件人验收另立任务；重型浏览器、sdist 构建和外部在线服务不进入首批行为集合。

用户此次授权为制定方案；以下模块、配置、命令、测试和门槛均为待实现设计。新增任务在 [feature tracker](../../feature_list.json) 中均为 `not-started`。

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

### P3-02 实施进度（2026-10-20；Linux gate blocked）

- 已新增 Linux health fingerprint、Docker daemon/锁定镜像/资源余量/benign report 校验、失败关闭的 claim admission helper、`preflight` CLI 与初始资源/熔断参数；Linux probe 脚本现可显式指定新报告路径，不会覆盖 S2 evidence。Windows preflight 仅输出 `not_tested`，未运行。
- Linux 单元检查 7 项通过（3 个 health guard + 4 个 Docker command/policy checks，Windows tests 跳过）。但真实准入无法启动：当前 Linux 执行主机没有 Docker CLI，也没有 `/var/run/docker.sock`。`preflight` 保存 [blocked report](../../outputs/acceptance/phase3-p302-linux-admission-20261020.json)，原因 `docker_cli_unavailable` / `benign_probe_report_missing`，退出 3；没有模拟 Docker passed。
- 因 P3-02 的 Linux real Worker probes 尚未通过，feature 状态是 blocked，不能声称 Linux backend 已准入。待在有锁定 Docker Worker 的 Linux 主机复跑 benign probes 后恢复；无依赖该 Worker 的 P3-06 可继续。
