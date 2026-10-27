# 第四阶段：行为兼容性与运行就绪

日期：2026-10-26（Asia/Shanghai）。状态更新（2026-10-27）：P4-01 / feat-026 与 P4-02 / feat-027 的 scoped engineering deliverables 已完成；P4-03 / feat-028 进行中，受控 runner 尚未通过真实部署验收；P4-04–P4-06 未启动。完整目录行为、Windows Enforcement、真实平台及发行 gate 仍未通过。源码计划基点：`9d03728853bfb18a52e1640b2084a624c70501ba`。以下基线描述计划起草时的状态；后续按日期追加实施证据，不改写历史批次。

## 目标与来源

依据用户提供的[下一步做行为验证讨论](https://chatgpt.com/share/6adf2885-7b18-83ea-8051-6ed961271c33)，将主要交付从检测账本转向：未经修改的插件在原生路径及生产 Core/Host 路径上满足独立行为 Oracle，并有精确版本、环境、用例、回执和清理证据。

讨论中的 P4-01–P4-06 是方向建议。本计划保留六项编号，补齐源码接口、依赖、阶段准入和完成标准。讨论先提出 P4-06 稳定发行，又把完整插件管理列入后续阶段；此处将 P4-06 限定为既有安装能力的候选发行、固定版本部署与回滚验收，完整安装/更新/卸载管理器及 AstrBot 留待第五阶段。

本计划区分三个结果：工程实现交付、必选行为兼容通过、真实平台可用。任何一项都不能替代另外两项；实施中发现确定的不兼容可完成归因和报告，但相应兼容 gate 仍不通过。

## 已核实基线与缺口

| 基线 | 本次读取/复核的证据 | 下一阶段含义 |
| --- | --- | --- |
| P3-05 终态账本 | [闭合报告](../../outputs/acceptance/phase3-p305-overall-closure-20261026.json)、[P3-10 审计](../../outputs/acceptance/phase3-p310-gate-audit-20261026.json) | 3,179 唯一单位：passed 1,299 / failed 688 / skipped 1,139 / unknown 53；账本闭合，健康仍 blocked |
| P3-08 行为账本 | [冻结报告](../../outputs/acceptance/phase3-p308-behavior-accounting-20261026.json)，本次 `--check` 通过 | 20 entries / 60 cases / 120 entry×OS×case positions / 360 slots，实际派发 0；行为仍未评估 |
| 五道历史 gate | 本次 P3-10 `--check` 通过 | ENGINE passed_scoped；RECOVERY/AUTOMATION blocked；BEHAVIOR not_evaluated；FIELD not_run |
| Windows Worker | [P3-04 admission](../../outputs/acceptance/phase3-p304-windows-admission-python31210-20261025.json) | 限定 CPython 3.12.10 runtime；`catalog_plugin_claims_allowed=false`，不是目录插件行为准入 |
| CI / 专用 runner | [现有受控 workflow](../../.github/workflows/phase3-linux-controlled.yml)、[P3-10 审计](../../outputs/acceptance/phase3-p310-gate-audit-20261026.json) | 普通 CI 已有历史成功记录；本次未读取 GitHub 部署侧 runner/variables/Environment，部署状态须在 P4-03 实测 |

既有 SHA 保持：P3-08 file `3daaf8aa387bb01b26ae72ca0a8bd6db9b1e0ab9f192a5ddd3d054f928e0e1f5`；P3-10 file `716efa2e9af8537506e26215c231ef68bf8b30585d61e070bffff76237692b90`。P3-05 的 39 Linux、10 Windows pilot、4 Windows preparation unknown 都是历史终态，后续新观察不能覆盖它们。

源码核对到的具体缺口：

- [behavior suite v1](../../contracts/lab/automation/v1/behavior-suite.schema.json) 接受任意 `stimuli` 对象，且 case 的 `additionalProperties=false`；不能直接向旧 case 填新 adapter/setup 字段。[冻结套件](../../contracts/lab/automation/v1/fixtures/behavior-suite.phase3-v1.json) 的刺激仍以 `input` 描述为主。
- [P3-08 accounting](../../lab/automation/behavior_campaign.py) 检查事件、`runtime_adapter_id` 和 `runtime_setup`；目前只能归因缺失，未接通实际 Host 驱动。历史 accounting comparator 的兼容层继续保留。
- [run_behavior_pair](../../lab/automation/execution.py) 已有双路径、独立工作目录、Oracle 不传入 runner、逐侧断言和 route/cleanup 检查。当前输出按 `plugin_observation` 完整相等比较，且 gateway count 与 response count 关联；服务/存储用例需要明确的语义投影及独立计数，不能强制伪造发送操作。
- [paired_worker](../../lab/baselines/paired_worker.py) 的既有入口主要从 representative fixture 构建事件及预期文本；需要适配到 entry-bound 用例，而不是继续仅执行四个历史样本。
- 原生入口已经存在：[NoneBot runner](../../lab/baselines/nonebot/runner.py)、[Koishi runner](../../lab/baselines/koishi/runner.mjs)；联邦入口为 [federated runner](../../lab/baselines/federated.py)。复用真实框架生命周期和事件分发，Core 协议保持现有责任边界。
- 旧 Oracle 多处只校验响应数量/泄漏计数，需要增加内容、接收者、状态/副作用的独立预期。`ko-f6dfe45f2f1b06768eb14b06` 的包名是 `koishi-plugin-simple-echo@1.0.4`，case 名仍含 `smmcat-guesssong`；P4 必须审核并建立显式 case 映射，不修改旧 suite，也不凭名称推断行为。

## 范围、依赖与执行次序

P4-01、P4-02 是主线。P4-03 不作为 Linux 首批闭环的全局前置；P4-04 也不阻挡 Linux scoped 交付。每次只激活一个 feature，切换任务先保存状态和原因。下表列工程依赖；运行时还必须通过对应批次的准入，不能仅检查 feature 的 done 状态。

| 任务 / feature | 工程依赖 | 主要交付 | 工程完成标准 | 估算人日 |
| --- | --- | --- | --- | --- |
| P4-PLAN / feat-025 | P3-10 | 本计划、状态路由与检查证据 | 文档、依赖、状态及既有证据复核通过；明确 implementation not started | 本次 |
| P4-01 / feat-026：Executable Host Behavior Fixtures | P4-PLAN；复用 P3-06/P3-07 | 版本化合同、20 项映射、两 Host 驱动、首批每生态 1 项 Linux 闭环 | 下面 P4-01 清单全部满足；首批全部必选 cases 双路径三次通过且清理确认 | 5–7 |
| P4-02 / feat-027：Linux 行为扩面 | P4-01 | 每生态 2 项，再覆盖原 20 entries 的 Linux 位置 | 无漏项、无替换样本；每位置执行或逐项证据归因；失败/未知对应 gate 保持未通过 | 7–10 |
| P4-03 / feat-028：unknown 与自动化部署验收 | P4-PLAN；复用 P3-03/P3-05/P3-09 | 53 项新恢复账本、故障回归、真实受控 runner 验收 | 53 项身份全覆盖；新观察可追溯；runner 成功/失败/中断和权限边界有运行证据；未解项继续 blocked | 4–6 |
| P4-04 / feat-029：Windows Enforcement 行为准入 | P4-01；复用 P3-04 | 强制策略 evidence package、新 profile/manifest、Windows 行为矩阵 | 完成下述 native-only→插件 pilot→套件门控；不可准入时 blocked，不用记账替代行为验收 | 4–6 + 外部等待 |
| P4-05 / feat-030：真实平台端到端验收 | P4-02 的工程交付 | 独立 Ligamina 客户端/Gateway 测试，Stella 作为首个集成客户 | 选定 Linux 插件已通过全部必选行为且非 flaky；真实收发、作用域、异常/重启和未知回执证据齐全 | 3–5 + 账号等待 |
| P4-06 / feat-031：候选发行与阶段审计 | P4-02/P4-03/P4-04/P4-05 | 安装/回滚验收、支持矩阵、独立 gate 审计和交接 | 工程/行为/平台/发行分别裁决；只发布证据支持的 scope；缺项不得称跨平台或生产就绪 | 2–3 |

合计约 25–37 人日，另加沙箱/runner/平台账号等待及长时间实验。是串行工作量估算，不是日历承诺。建议依次：P4-01 → P4-02 首四项 → P4-03 → P4-02 二十项 → P4-04 → P4-05 → P4-06；依赖满足时可切换不受阻任务，Windows 外部条件不足时继续 Linux 工作并保留 Windows blocked。

## P4-01：可直接开始的实施单

### 1. 先核验二十项，再冻结 v2

读取原 suite 的每个 entry、归档成员 SHA、精确 dependency lock 和三个用例，建立 20-entry review/migration matrix。逐 case 标注：原 case ID、新 case ID、驱动类型、真实入口、setup、外部依赖、Oracle 来源、预期作用域和可执行/阻塞原因。特别复查名称残留、命令权限/前缀、随机输入、数据库、定时器及工具库型条目。

新增位置建议为 `contracts/lab/automation/v2/behavior-suite.schema.json` 与 `contracts/lab/automation/v2/fixtures/behavior-suite.phase4-v2.json`，属于待实现文件。保留 v1 validator 与全部 P3 证据。v2 明确引用父 suite/case 身份；必选类仍至少 positive、negative、isolation，新增案例单列并重算分母。记录为什么改进旧 Oracle，不以旧结果反推 expected。

### 2. 可执行合同

下表字段为拟定设计，不是当前可调用 API。实施时先固定 schema、golden fixtures 和拒绝场景，再写驱动；setup 采用声明式白名单，不能由 suite 指定任意 shell/Python/JS 代码。

| 组成 | 必须绑定的内容 | 校验/观测要求 |
| --- | --- | --- |
| identity | snapshot/entry/package/version/artifact、OS、framework/runtime、profile/dependency lock、源码成员 SHA | manifest 同时绑定 adapter/setup/stimulus/oracle/normalizer 的版本和 SHA；身份不符拒绝派发 |
| runtime_adapter_id | 已注册的 event/command/service/storage/clock driver 及版本 | 两侧驱动实现和入口分别记录；注册表外 ID 拒绝 |
| runtime_setup | 插件配置、provider 及 combined lock、隔离 storage 初态、权限、账号/会话、资源限额 | 无明文秘密；provider 安装和初始化均在已准入 OS Worker；依赖闭包不完整则不执行 |
| stimulus | 一组有序、schema-valid 的事件或白名单服务/时钟操作 | event/command 必须有具体事件数组，包含账号、actor、conversation kind/id、message segments、事件 ID/时间；不接受自然语言替代载荷 |
| determinism | 明确的 clock/seed、注入边界、settle/deadline 和 scheduler drain 条件 | 记录实际生效证据；不能只把 seed 写进 JSON；不支持可控时间的插件要明示限制 |
| observation | 操作内容/目标/次数、状态快照、文件/数据库副作用、拒绝及异常 | 由真实 trace/状态提取；adapter 不能返回写死的 expected 或泄漏计数 |
| oracle | 独立来源、expected 内容/目标/状态及安全边界、断言版本/hash | 留在可信比较侧，不传给插件 runner；分别判定 Native、Federated，再比语义 |
| lifecycle | startup/ready/dispatch/settle/shutdown、Host PID、Worker ID、fenced attempt、清理证据 | 外层 Worker 验证进程树和容器/Sandbox 消失；自报 cleanup 不能替代后端实测 |

event/command：NoneBot 经原生 OneBot event model → `Bot.handle_event`；Koishi 经原生 Session/dispatch 进入命令/middleware。Federated 一侧经生产 Rust Core → Host → 平台动作队列，捕获账号和会话作用域。不能用直接调用命令 handler 的结果代替分发证据。

service/storage/clock：复用框架服务和生命周期 hook。工具库可有单独 API 单元测试，但要计入联邦支持，必须存在并验证真实 Host capability/service route；不能把纯函数结果记为 `send_message`。时钟/随机控制保持两侧相同边界，记录改造在测试驱动而非第三方发布物；修改插件的实验另建身份。

### 3. 双路径执行与比较接线

以 [execution.py](../../lab/automation/execution.py) 为比较入口，复用 [native_runner.py](../../lab/native_runner.py) 的进程边界、[paired_worker.py](../../lab/baselines/paired_worker.py) 的 profile/artifact 准备和现有 stock/Federated runners。根据调用链决定新增模块；优先将测试适配放在 lab，只有生产 Host 缺能力时才修改 Host。Core transport 沿用现有协议。

两侧使用同一冻结发布物、框架/依赖锁、配置、刺激和 Oracle，但使用独立进程、数据目录/数据库；三次 attempt 也各自重新初始化。Native 不导入 Ligamina Bridge；Federated 保留真实 Core/Host route、操作/回执、Host reap 证据。归一化仅允许事先列明的随机 ID/时间差；账号、conversation kind/id、消息/操作次数和业务状态保留语义。共用错误输出即使相等，也必须被各侧 Oracle 拒绝。

### 4. 首批样本与硬性完成条件

| 波次 | 冻结样本 | entry | 用途/尚待核验 |
| --- | --- | --- | --- |
| P4-01，NoneBot（修订后） | `nonebot_plugin_roll@0.2.3` | `nb-16c80a826ca035f6c49ad69a` | OneBot 命令与 matcher pending-state 跨会话隔离；须按锁定 artifact/profile 实际验收 |
| P4-01，Koishi | `@koishijs/plugin-echo@2.2.5` | `ko-163c3bd4cf87c72c24210251` | 原生 echo 命令、参数/权限边界、两会话收件人；负例的帮助/错误提示须按源码确定 |
| P4-02 首四项追加 | `nonebot_plugin_help@0.6.1` | `nb-0b37b4bb782b82c00e95a87f` | 固定插件集、help 命令与无效查询；需精确配置 |
| P4-02 首四项追加 | `@koishijs/plugin-help@2.4.6` | `ko-fb5aa9ada6cacd62ec595c66` | 固定命令集、权限与独立请求；需冻结 setup |

以上是计划选择，当前只核对了 suite/lock 声明，尚未重新审阅或运行归档内第三方代码。实施必须读取 exact artifact；样本不能因发现失败而悄悄替换。如首项确实不兼容，可交付诊断子项但 P4-01 完整闭环未满足；另列新增验证样本也不能抵销原必选项。

### 2026-10-26 用户授权的 P4-01 样本修订

原 NoneBot 首批样本 `nonebot_plugin_guess@0.1.9` 已按冻结 artifact `db6b9bfefdcd80d4e9da03f2b136a0d11c7e1a08cad3c378cac8eda573929957` 完成源码复核和 3 次 Native/Federated 隔离配对。三次均未通过：交错两个群会话后，先启动的会话没有恢复自己的答案状态；归因于归档源码中的模块级 `idx`、`city` 状态。报告 [v8](../../outputs/acceptance/phase4-p401-behavior-runs-20261026-v8.json) 保留失败，不能把它计为新样本的通过，也不修改第三方包或原 Oracle。

用户随后明确授权继续 P4-01 时选择另一个精确锁定的 NoneBot 样本。首批替代项选自同一 20 项 v1 suite：`nonebot_plugin_roll@0.2.3` / `nb-16c80a826ca035f6c49ad69a`，artifact SHA-256 `3a5ead2404f26711c9d41604ddcbb2e141df8a950fc53d32536aa7980fab1a2c`，源码成员 `nonebot_plugin_roll/__init__.py` SHA-256 `65835efdf2bfbaeb237df005c459610a068c55a8edd53fcae971e0976d5409d2`，Linux profile `nonebot-2.5.0-onebot-2.4.6-py3.12.14-linux-x86_64` 的依赖锁 SHA-256 `792fe11f2fd3581e888b15a23bc0c49f76b3b0d2399fc728dbfcff3d436214df`。源码使用 NoneBot `Matcher.got` 保存待补参数；新 isolation 用例将先在群 501 建立 `/rd` pending prompt，再在群 502 完成掷骰，最后只允许群 501 的原会话恢复。

此授权是对首批样本选择的明确范围修订，不改变 20 项 v1 identity/order 或 6 positions / 18 paired attempts / 36 side executions 分母。旧 suite、migration matrix 与 v8 失败报告均保留为单独哈希绑定证据；替代项必须重新完成三次 fresh Native/Federated 运行，不能继承旧样本结果。

P4-01 完成必须同时具备：

1. 20-entry 迁移矩阵完整，v2 合同拒绝缺 event/setup/adapter/锁/Oracle，旧套件仍可只读复核。
2. NoneBot/Koishi 两驱动有真实框架分发证据；service/storage/clock 各有合同和自有探针；需要目录插件证明的能力只有执行后才能声明支持。
3. 首两项所有必选正例、负例、隔离用例双路径各满足独立 Oracle，连续三次 fresh attempt 通过；按原三个 case 计算是 6 Linux positions / 18 paired attempts / 36 side executions。
4. 测试能检出：两侧相同错误输出、错误收件人、同 ID 群/私聊混淆、跨账号/会话污染、超时、身份篡改、未清理及残留 Host。负例不固定要求“零回复”，而是要求符合原生语义且无越权业务动作/副作用。
5. 保存新 suite/profile/manifest/checkpoint、双方 trace、独立 comparison、外层 cleanup、三次结果与不可变证据清单。任何失败保留；后续通过也显示 flaky/历史 attempt。

建议 5–7 人日拆为：身份/Oracle 复核和合同 1–2 日，Host 驱动与比较接线 2–3 日，真实 Linux 双路三次复跑及故障验证 2 日；实际依赖缺口可调整但须记录。

## P4-02：Linux 从四项到二十项

先完成上表四个固定样本，再按消息/命令 → 服务/存储 → 定时/复杂状态扩面到原 20 entries。四个历史支持插件仍是独立 legacy 回归，不计入本次 20 项分母。每个位置先查 fixture/profile/服务/网络前置，再经新鲜 Linux admission 派发。使用已有 queue、lease/fencing、熔断和精确标签清理，不另建不持久的并行调度器。

保留原三类用例时，分母如下。此处 **一次 paired attempt = Native 与 Federated 两侧执行**；stage jobs、attempt slots、实际 Worker 数分别统计，不混用。

| 范围 | entries | entry×OS×case positions | 三次 paired attempts | 两侧执行上限（不含安装/探针） |
| --- | --- | --- | --- | --- |
| P4-01 Linux 首两项 | 2 | 6 | 18 | 36 |
| P4-02 Linux 首四项（含首两项） | 4 | 12 | 36 | 72 |
| P4-02 Linux 原二十项 | 20 | 60 | 180 | 360 |
| P4-04 Windows 同套件 | 20 | 60 | 180 | 360 |
| 两 OS 合并 | 20 | 120 | 360 | 720 |

如 v2 增加 case，从冻结 `expected_result_ids` 重算；新旧 suite 不合并凑分母。setup 被阻塞的位置逐项留账为 skipped + 具体依赖原因，未派发槽另外记录 not_dispatched；已启动但结果不确定的 attempt 为 unknown。工程闭合需零漏项/重复、逐项证据与清理已确认；全 suite 行为 gate 只在全部必选位置三次通过、无 skipped/unknown/flaky 时通过。可提前发布已满足全部要求的精确子集，不称二十项全通过。

## P4-03：53 个 unknown 与受控自动化

从 P3-05 closure identities 提取严格 53 个父单位，按阶段、原因指纹及 OS 分组：Linux 39；Windows pilot 10；Windows preparation 4。新 manifest 包含父 report/unit/attempt 和 SHA、当前诊断、允许重试预算、停止条件。原 53 条 unknown 永久保留；新的 resolved/unresolved disposition 和 observed outcome 单独展示。

先采集每组原日志、源码、锁与后端证据，再修改检测器。依赖不可满足若有确定证据则归入新 failed/skipped，不能仅为了消除 unknown 改状态。临时网络/metadata failure 采用有上限退避和缓存身份核验；Worker 丢失/timeout 保留 unknown 直到证据足以归因。按 failure domain 区分插件与 harness/infrastructure。

P4-03 可完成 Linux 调查与 runner 子项；Windows 10 项的进一步执行若依赖 P4-04 的 enforcement/管理员证据，则等待该准入后收尾。这是运行准入条件，不把整个 P4-04 设为循环工程依赖。

专用 runner 验收必须取得真实部署证据：repo/default branch/ref/commit、runner labels、Environment protection、变量、锁定镜像和新鲜 benign preflight。先在隔离实验机上用自有探针验证成功、失败和取消后的 artifact/日志/精确清理；再派发新 manifest。现有 P3 controlled workflow 固定历史 campaign，P4 必须新建可审阅的 campaign 入口，不能直接启用旧任务重跑。

fork/PR 只能运行非特权自测；目录 Worker 维持源仓库默认分支、显式启用、受保护 Environment、只读权限、无持久 checkout 凭证。故障注入包含 daemon 不可用、lease 过期、旧 generation 回执及并发 SQLite 锁；WSL 活动队列位于原生 Linux 文件系统。2026-10-27 已用 `gh` 创建 `phase4-p403-linux-controlled` Environment，部署分支仅 `main`，要求 owner reviewer，不含 secrets；两个 dispatch variables 均设为 `false`。仓库 runner inventory 为 0；一次性隔离 runner 注册、合同 workflow/artifact/清理验收和 39 项恢复派发仍待执行。

P4-AUTOMATION 只在新 scope 无未解决 harness/infrastructure failure、unknown、必选未派发或清理不明，且真实 runner 验收完整时通过；已证实插件失败不伪装成基础设施故障。P3 的 gate 不回写。

## P4-04：Windows 强制保护环境

顺序固定为：确认 disposable guest enforcing baseline → 自有 native-only 探针 → 目录安装/加载 pilot → 每生态 1 项行为 → 每生态 2 项 → 二十项矩阵。Linux 证据独立发布，不外推到 Windows。

1. 保存 guest/OS/runtime/架构、实际 policy ID/options 和事件日志。Evaluation/Audit 与 Enforcement profile 各建身份；成功加载的 audit 结果不能充当 enforcement 验收。
2. 如原生二进制被阻止，先绑定 exact hash、实际 policy 和相关事件，再准备管理员可审阅包；限定 Sandbox-only 的签名策略/受信二进制方案及可复现探针、回滚说明。保留宿主基线及 enforcement，避免宽泛路径/发布者放行。本计划不授权部署策略。
3. CPython 3.12.10 与原 3.12.14 的 profile 独立冻结。新 admission 必须明确允许 catalog plugin claims，并覆盖安装/执行/限额/拒绝网络/进程树清理；仅 interpreter 可运行不够。
4. Koishi 逐条冻结必要 provider、config 和 combined lock；候选 provider/静态 inject 扫描不是 ready。Git SSH source、lifecycle scripts、sqlite3/skia/native 资源须形成完整来源与策略闭包，否则保留有理由的 skipped。不能把 `--ignore-scripts` 后安装成功当作 native binding 可用。
5. 每个已准入 Windows 行为位置按与 Linux 相同 Oracle 三次独立运行；报告明确 enforcing profile、Native/Federated 差异、回执范围和清理。Windows gate 未通过时，双平台行为与跨平台发行声明保持未通过。

## P4-05：真实平台验证

在 P4-02 中选出已通过全部必选 case 的 Linux 插件；无需等待 Windows，但 field scope 明确 Linux。复用现有独立 Ligamina HTTP/MCP 客户端和 Gateway 协议，让 Stella 作为首个客户接入测试部署，不在 Core 引入 Stella 依赖。QQ/NapCat 是讨论建议的候选部署，实际账号/Gateway/群及精确版本在执行前冻结。

账号/网关凭证来自私有配置，不进入 manifest、日志或报告；本轮不读取或配置。执行前需用户明确实际测试账号、受控群/私聊对象及发送范围，准备好具体测试清单后再开展真实发送。

必选场景：群/私消息进入正确插件；两个账号及群/私同 ID 路由隔离；操作目标/内容/次数正确；事件重复、进程重启、Host 崩溃和 gateway timeout 后无重复投递；scope/持久状态恢复；未确认的动作保留 unknown。保存 ingress event ID → Core receipt → Host trace → operation ID → Gateway receipt → 接收端消息 ID/可观察投递的关联。Gateway API accepted 与接收端观察分别记录，不能仅凭 accepted 推导真实送达。

每生态至少 1 个已验证插件完成正例、隔离、重复/重启、超时/未知处理各场景三次 fresh run；先确认 cleanup/残留动作再复跑。真实平台无法完全自动化的接收端确认明确人工证据和时间。field gate 与 virtual/service_fixture gate 独立，不提高其他平台结论。

## P4-06：候选发行和独立审计

复用现有 [Windows bundle](../../deploy/windows/README.md)、[Linux container](../../deploy/linux/README.md) 和 [Windows 安装验收](../../scripts/windows-install-acceptance.ps1)，冻结 release artifact/依赖/配置 schema。限定版本安装、启动、health/status/doctor、插件能力发现、日志定位、停止/卸载清理、升级失败回滚及用户数据恢复均有干净环境证据。回滚涉及 state/schema 时先备份，不能仅替换二进制宣称成功。

发布矩阵按 entry/version/artifact/profile/OS/case/delivery_scope 展示；支持项要求所有必选 cases 连续三次通过、清理确认、非 flaky。继续保存全部 failed/skipped/unknown 与 reason codes。第一阶段四个历史支持版本保留原证据范围；增量只来自新验收。

阶段审计独立裁决：

| P4 gate | 通过条件 | 未满足时 |
| --- | --- | --- |
| P4-FIXTURE | v2 合同、20 项映射、Host 驱动和首两项真实闭环全部达标 | 工具测试通过但真实闭环缺失时保持 blocked/not_evaluated |
| P4-LINUX-BEHAVIOR | 原二十项 Linux 全必选 case 三次通过、无缺项/不明清理/flaky | 已测确定不兼容记 failed；未满足前置记 blocked；精确子集另列 passed_scoped |
| P4-AUTOMATION | 上述新 recovery scope 与真实受控 runner 验收通过 | 保留 unresolved 数和部署侧缺口；历史 unknown 不清零 |
| P4-WINDOWS-BEHAVIOR | 强制模式目录准入 + 同套件全部必选行为通过 | Linux 结果独立保留；Windows 与跨平台结论不通过 |
| P4-FIELD | 选定真实平台场景全部达标，实际投递/unknown/去重证据完整 | virtual 回执不替代；外部条件不足则 blocked/not_run |
| P4-RELEASE | 指定发行 scope 的安装/回滚/诊断、行为/平台证据与审计全部满足 | 可交付明确限制的技术预览；不宣称完整生产或跨平台就绪 |

gate completion 与工程 feature done 分开：P4-02/03/06 的审计可以明确交付失败/阻塞裁决；P4-01 的首批通过及 P4-04/05 的运行验收属于硬性 done criteria，缺失时不能仅靠归因报告标 done。P4-06 若外部 gate 仍阻塞，只交付审计子项，不宣称候选发行验收全部完成。

### P4-01 实施结果（2026-10-26）

- 按用户授权，以冻结并复核的 `nonebot_plugin_roll@0.2.3` 替代失败的 `nonebot_plugin_guess@0.1.9`。原 Guess v8 报告及三次 isolation 失败保留在迁移矩阵中；不修改第三方发布物、旧样本 Oracle、20-entry 顺序或首批分母。新 Roll 与 Koishi Echo 均按精确 artifact/profile 锁定。
- v2 suite / migration matrix 覆盖 20 entries / 60 cases：6 个首批可执行 case，54 个保持 blocked。Schema 拒绝缺事件、setup、adapter、Oracle 或不匹配锁；回归覆盖同错双路径、收件目标、身份绑定、cleanup 与 Core timeout 等检测边界。
- Service 使用真实 NoneBot Host → Rust `CapabilityBroker` → Koishi Host 调用，结果 42、trace 保留；storage/clock 通过框架 Session/Event hook 观察真实事件。v10 在 36 个独立 side run 中写入 54 条事件记录，校验 event/account/actor/conversation/timestamp；36 个 Worker ID 唯一，清理全部确认。
- 正式 v10：6 positions / 18 paired attempts / 36 side executions，18 passed、0 failed、cleanup_unconfirmed=0。三个 service/storage/clock probes 均 passed；`p4_fixture_gate=passed`，`p4_first_wave_behavior=passed_scoped`。这只覆盖首批两个 Linux 样本，不表示 20 项 Linux、Windows、field、automation 或 release gate 通过。
- 最终单独运行的 `validate-behavior-v2` 是不派发插件的 suite preflight，输出 20 entries / 60 cases、6 executable / 54 blocked 和 `plugins_executed=false`，因此该命令自己的 gate 状态仍为 blocked；它不读取 v10 campaign。首两项真实闭环的 scoped gate 以 v10 campaign/manifest 为准，二者范围不同。
- 终态索引 [manifest](../../outputs/acceptance/phase4-p401-evidence-manifest-20261026-v10.json) 绑定 [v10 report](../../outputs/acceptance/phase4-p401-behavior-runs-20261026-v10.json)、suite、matrix、admission、service probe、profile/bundle、逐次 Native/Federated trace、comparison 与 cleanup；campaign report 是逐 attempt checkpoint。
- 最终锁定工具链的 `init.ps1 -Mode full` 退出码 0：docs 20/20、Lab 203 tests（1 skipped）、NoneBot/Koishi/reference tests、Rust workspace tests、Windows smoke 4 events/4 operations、release build 和 Windows bundle staging 均通过。历史 P3-08/P3-10 `--check` 仍复现原 SHA 与 gate。
- `feat-026` 的工程交付条件完成；后续下一项为 P4-02 / feat-027。P4-02 的 54 个 blocked cases、P3 历史 unknown、Windows Enforcement 准入、真实受控 runner 与 field acceptance 继续保持原状态。

复现入口：

```powershell
uv run --locked --project lab --python 3.12.14 python -m lab.automation validate-behavior-v2
uv run --locked --project lab --python 3.12.14 python -m lab.automation plan-behavior-v2 --output outputs/acceptance/phase4-p401-dry-plan-20261026.json
uv run --locked --project lab --python 3.12.14 python -m lab.automation probe-phase4-service --node-executable <Node-24.20.0-path> --output <new-service-report.json>
uv run --locked --project lab --python 3.12.14 python -m lab.automation execute-behavior-v2 --admission outputs/acceptance/phase4-p401-linux-admission-20261026-v2.json --runtime-image <sha256-pinned-image> --asset-root <locked-offline-assets> --bundle-root <new-external-bundle-root> --service-probe-report <service-report.json> --attempts 3 --output <new-campaign-report.json>
uv run --locked --project lab --python 3.12.14 python -m lab.automation manifest-behavior-v2 --campaign-report <completed-campaign-report.json> --output <new-terminal-manifest.json>
```

## 验证、证据与停止条件

当前真实可运行的检查入口如下；在修改运行时代码前按 [开发指南](../guides/development.md) 保存 baseline。`validate-behavior` 仍只校验 v1 合同；Phase 4 使用 `validate-behavior-v2`、`plan-behavior-v2`、`probe-phase4-service`、`execute-behavior-v2` 和 `manifest-behavior-v2`。

```powershell
pwsh -NoProfile -File init.ps1 -Mode docs
node --test scripts/evidence-hash.test.mjs
node .agents/skills/harness-creator/scripts/validate-harness.mjs --target .
C:\Python314\python.exe scripts/phase3-p308-behavior-accounting.py --report outputs/acceptance/phase3-p308-behavior-accounting-20261026.json --check
C:\Python314\python.exe scripts/phase3-p310-gate-audit.py --check
uv run --locked --project lab --python 3.12.14 python -m lab.automation validate-behavior
uv run --locked --project lab --python 3.12.14 python -m unittest discover -s lab/tests -v
pwsh -NoProfile -File init.ps1 -Mode full  # 必须使用 Node 24.20.0 和独立 UV_PROJECT_ENVIRONMENT
uv run --locked --project lab --python 3.12.14 python -m lab.automation validate-behavior-v2
uv run --locked --project lab --python 3.12.14 python -m unittest discover -s lab/tests -v
uv run --locked --project lab --python 3.12.14 python -m lab.automation manifest-behavior-v2 --campaign-report <completed-report.json> --output <new-manifest.json>
```

P4 待实现 CLI 必须提供 v2 validation、dry plan（不派发）、受控 execute/resume/reconcile、只读报告复核；具体命令在实现后写入指南和 help，不在本计划假装已存在。真实 Linux Worker 在 Linux runner/WSL 内执行，不能用 Windows 宿主 Python 单测替代 Linux 插件行为。C:\Python314 仅用于本次只读审计复核，不是准入 Worker runtime。

按改动选择 Host 自测、lab 合同/比较/调度故障回归、Core locked tests 和双 OS CI；协议/Host 路由修改执行 full/smoke。单测中的 fake runner 只证明检测器拒绝错误，目录支持以真实 OS Worker 双路为准。另验 clean LF checkout、Windows CRLF checkout 与缺外部 cache 的只读历史报告复核；实际执行必须重新核验 cache 内容，不能复用“历史 verified”跳过安装前身份检查。

每批证据保存结构化 argv/cwd、commit/dirty、工具链和 image digest、suite/profile/artifact/fixture/adapter/oracle/normalizer 哈希、expected IDs、双方 trace、逐次比较、failure domain、后台清理和 file/canonical SHA。文件建议 `outputs/acceptance/phase4-p401-...` 等新命名，原始日志脱敏并设大小上限；队列及 archive cache 可以在仓库外，报告保留验证引用。未启动 Worker 的 cleanup 是 not applicable，与真实 confirmed 分开。

停止派发条件：身份/缓存 hash 不符、admission 失效、后端熔断、资源不足、清理未确认、scope/接收者越界或实际投递不确定。先停止新 claim、保留精确 attempt/generation、清理并收集证据，再按 reconciliation 恢复；timeout/异常回执不自动转换为成功。Linux 初期继续使用现有最多 2 Workers 的限制，扩并发需新的资源/恢复证据。

## 本次交付与下一次启动

计划起草时的规划核验见 [P4 planning validation](../../outputs/acceptance/phase4-plan-validation-20261026.json)。现阶段 P4-01 已完成限定首批工程交付，支持名单没有扩张到完整目录。会话开始已有四个未跟踪 Windows profile-lock JSON/sidecar，仍保留且不纳入 P4 验收。

下一任务为 **P4-02 / feat-027：Linux 行为扩面与完整账本**。按 [AGENTS](../../AGENTS.md) 启动时先核对 HEAD/dirty、依赖和当前 Linux admission，再从四个固定样本开始并扩展原 20 entries；不能把本次 6 个 passed_scoped positions 或 P3 历史报告当作完整目录通过。当前 feature 状态与可复现命令见 [progress](../../progress.md)、[handoff](../../session-handoff.md) 与 [feature tracker](../../feature_list.json)。


## 实施记录（2026-10-27）

### P4-02 / feat-027 闭合

20-entry Linux 行为账本固定为 60 positions / 180 attempt slots：27 passed、9 failed、0 unknown、144 not_dispatched。36 个 stage jobs 完结，72 个 side executions 来自 72 个唯一 Worker，cleanup_unconfirmed=0。48 个 blocked positions / 144 slots 已在独立 attribution 中逐项绑定 suite identity、插件源码成员 hash 与缺失的 setup/profile/dependency prerequisites。

9 个失败槽来自同一固定位置：`nonebot_plugin_help@0.6.1` 在 NoneBot 2.5.0 / Pydantic 2.14.1 冻结环境 import-time `Config.parse_obj(driver.config)` 抛出 ValidationError。Docker network-disabled exact-profile reproduction 在事件派发前重现；属于 plugin compatibility，不是行为 oracle 或基础设施故障。Koishi Help 初版 stimulus 与 Core 默认空 prefix 不匹配，已以新 suite v2r3 修正并重新执行；v1/v2r2 证据保持未改写。报告 roll-up 漏列 plugin_compatibility 新分类，v2r3 每个 slot 已记录该分类；聚合代码及回归检查已修正，闭合时按不可变 slot evidence 重算为 9。

独立复跑四个历史支持版本：`nonebot-autohelp`、`nonebot-repeater`、`koishi-game-mini`、`koishi-multi-qa` 的 exact artifacts、profile lock、Native 与 Federated 路径及 comparison 均通过，4 个 Worker cleanup confirmed；此结论仅限 fresh Linux virtual-receipt regression。npm 12 的 root tarball policy、Koishi SRI/SHA256 fingerprint 与 installer transient-copy 问题已窄化修复并留有测试。Windows Sandbox 的 App Control Win32 4551 与真实平台投递仍未解决/执行。

闭合证据：[P4-02 closure](../../outputs/acceptance/phase4-p402-closure-20261027.json) / SHA-256 `4f2f34474ae66ce306ce7edb02c049ed22d6fe47135ea8d73d28dcf331ac38ba`；[campaign](../../outputs/acceptance/phase4-p402-behavior-runs-20261026-v2r3.json) / `b9fee50451432d7e3eb60d696713f464885ef3e5300f789cd1ea4ce66de5e2d1`；[manifest](../../outputs/acceptance/phase4-p402-evidence-manifest-20261026-v2r3.json) / `04fa977c7ea801edfa5e1a6248e2101f3f9b072c3dd4e7351928576e8161fbb4`；[legacy S2-05 regression](../../outputs/acceptance/phase4-p402-s205-regressions-20261027-v5.json) / `036b76a2a87aae0fef0618b266399df3722b17d0d89705e0ec7b32ff5e6a9476`。Gate 独立裁决为 Linux accounting=`passed`、fixed-four/full behavior=`failed`、fixture=`blocked`、automation=`not_evaluated`；不得声称 20 项行为通过或扩张 catalog/Windows/support claim。

### P4-03 / feat-028 启动

下一项按清单从 P3-05 closure 精确映射 53 个 terminal unknown（39 Linux / 10 Windows pilot / 4 Windows preparation），以父 unit/attempt/report digest 建立新 recovery manifest；P3 历史终态不改写。先读取原始 logs、source/lock 与 backend / runner evidence，再作有限 retry 决定。Windows 执行以 P4-04 Enforcement/admin admission 为运行准入；受保护 runner 的实际部署、权限边界、失败/取消 artifact 与外层 cleanup 需当前部署证据，P3 的历史计数不代表当前状态。

### P4-03 本地实施检查点（2026-10-27）

- Recovery ledger 精确绑定 P3-05 的 53 个父身份，分母仍为 Linux 39 / Windows pilot 10 / Windows preparation 4；P3 闭合和 attempt 结果未改写。Linux 39 项各有一次新 campaign attempt 预算；Windows 14 项预算为 0，直到 P4-04 Enforcement/admin 准入。manifest canonical SHA `6097a58a2a058fd06d1f65edbea83a47aa8f3d07b31e7c8b679a6313bd51bd97`，文件 SHA `523272f94689f82ae85204974d52645f0327b3d2d0474aa8ae986652ef2bd7af`；Linux executor manifest canonical SHA `ac86124f3fe1faa91d7075a3e93f562defd396a25229520df7543b402e11c72a`，选择恰好 39 个 Koishi identities。
- 只读复核 P3 Linux SQLite backend：campaign `p3-recovery-linux-842f71712e2c` 为 completed；39 个父 identity 中 cleanup stage 仍 unknown 39、install unknown 38。77 个 attempt 的 reason codes 为 worker_nonzero_exit 66、worker_cleanup_unconfirmed 4、worker_lost 3、dispatcher_executor_error 2、worker_timeout 2。每个 attempt 都有 worker cleanup 或精确 Docker-label reconciliation 的清理确认，但这不生成缺失的终态 stage receipt，因此 P3 unknown 保持不变。外部证据根有 7 个结构化 recovery report、无逐 attempt 原始 log；未据缺失日志推断根因。详见 [backend diagnostic](../../outputs/acceptance/phase4-p403-backend-diagnostic-20261027.json)。
- 新增独立受控 workflow，先运行自有 runner success / intentional failure / cancel 模式；Linux 插件恢复另需 `PHASE4_P403_LINUX_RECOVERY_ENABLED=true` 二次开关。WSL 本地 success、failure、TERM-interruption 三种 shell 演练均生成报告，outer cleanup confirmed；这是 shell/cleanup 验证，不是 GitHub runner、Environment 或 Actions artifact 的部署验收。报告见 [local runner contract](../../outputs/acceptance/phase4-p403-runner-contract-local-20261027.json)。
- 本地回归：55 个 Python automation/recovery/queue 测试、5 个 workflow static tests、三个 Bash 脚本 `bash -n` 均通过。docs gate 20 documents/424 links 及 harness 100/100 通过；命令和输出见 [verification report](../../outputs/acceptance/phase4-p403-verification-20261027.json)。受控 runner 的 GitHub 部署与真实 Artifact/upload 证据仍缺失，所以 feat-028 不得标记 done。
- 后续 hosted CI 复核（2026-10-27）：PR #4 最新代码 commit `5db0144af04cd5f00b74c71454d1a12833658d90` 的 [Linux/Windows CI](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40429421597) 与 [Linux contracts/regressions](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40429421491) 全通过。前一 Windows run `40428151648` 暴露 `host_session` 进程退出断言竞态：子进程被杀后 writer broken-pipe `Io` 可能先于 reader EOF `Closed`。修正测试接受两个有效终态并要求全部 pending 请求收到同一终态；本机 `cargo test --workspace --locked`、fmt 和 50 次定向重复均通过。完整记录见 [hosted CI report](../../outputs/acceptance/phase4-p403-github-ci-20261027.json)。
- PR #4 已于 `2026-10-27T01:25:23Z` 合并至 `main`，merge commit 为 `3362b51276728619a4b008b834b2098075caad23`；该提交的 [Linux/Windows CI](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40432475256)、[Phase 3 Linux self-test](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40432475334) 均通过。P4-03 workflow 在同一 push 上生成了 run `40432474103`，但以 workflow-file failure 结束且无 jobs。后来 `gh workflow run` 返回 GitHub API 422，精确指出 job-level `env` 的 `runner.temp` 上下文无效（第 59、60 行）；这是 P4-03 派发未能启动的原因。已将每次运行的 venv/cache 路径移动到相关 step 的 `env`，并为后续 PR 与 workflow_dispatch 验证记录新证据。修复合并并完成一次安全的非默认分支 guard 验证前，不得称 workflow 部署验收通过。
- runner workflow 修复发布后，仍须为一次性隔离 Linux runner 配置 `[self-hosted, linux, x64, ligamina-phase3]`、Docker daemon、冻结的 `Ligamina-artifact-cache`，并预置 manifest 指定的 digest-pinned Worker images。先设 repository variable `PHASE4_P403_RUNNER_ENABLED=true`，通过 protected Environment `phase4-p403-linux-controlled` 运行三种自有探针并人工取消 cancel 模式；检查 success/failure/cancel artifact 与清理后，再设 `PHASE4_P403_LINUX_RECOVERY_ENABLED=true`，派发 39 项 Linux recovery。Environment 不存放凭证。
- Windows 14 项保持未派发；现有 Evaluation/Audit 或错误码归因不满足 P4-04。继续前需管理员在 disposable Windows Sandbox guest 中确认 Enforcement 基线，并提供可审阅的 Sandbox-only signed policy/binary 证据；不得修改宿主机策略或放宽宿主基线。
