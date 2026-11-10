# 第七阶段：万级插件的自动化兼容性验证（2026-11-09）

适用：在 NoneBot、Koishi、AstrBot 三个冻结目录（5,690 + 3,265 = 8,955 个已冻结条目；矩阵另含 6 个未冻结 AstrBot 条目，共 8,961）上，自动判断一个**没有专门测试脚本**的插件能否在 Ligamina 下与原生框架一致地工作。本阶段只交付验证机制和它产生的证据，不新增框架、不修改第三方插件。

依据：用户 2026-11-09 提供的调研《面向万级插件生态的自动化兼容性验证技术调研》（第一轮），以及对本仓库源码与证据的逐条核对（第 1 节）。调研中未核对的外部说法（如 Koishi `plugin-mock` 的观测限制）只作为风险，不作为事实使用。

## 1. 调研结论核对

| 调研结论 | 核对结果 | 依据 |
| --- | --- | --- |
| NoneBot + Koishi 冻结 5,690 条，其中官方维护 55 条 | 属实（Koishi 46、NoneBot 9） | `catalogs/p2-41649744e057d61031d5/catalog.json` 的 `official_maintained` |
| AstrBot 冻结 3,265 条；加载 pilot 21/24；工具级 24/24、3 个插件 | 属实；另有 3,247 条 AstrBot 条目的 `load` 仍为 untested | AB-03/04/05，[兼容性矩阵](../../outputs/compat/compatibility-matrix-v1.md) |
| 大量条目缺少可执行行为 Fixture | 属实，且是主瓶颈：`federated_event` 只有 2 verified、1 failed、16 skipped，其余 8,942 untested | 同上 |
| 可用 MCP Command Descriptor 做 Schema 驱动的输入生成 | 部分成立。v0.2 descriptor 的 `type` 是自由字符串，没有取值约束；Koishi 有类型化参数与 options，AstrBot 有签名类型，NoneBot 只有原样文本 | `contracts/v0.2/command-descriptor.schema.json`、三个 Host 的 `commands.*` |
| NoneBot 命令发现缺 `on_alconna` 等 | 属实：只识别 `CommandRule` / `ShellCommandRule`，关键词、正则、消息/通知 matcher 被跳过 | `hosts/nonebot/ligamina_nonebot/commands.py` |
| 可以把聊天命令当有状态 API 生成多步序列 | 需修正：MCP 工具调用每次新建虚拟私聊会话且 actor 随机，**跨调用无法保持用户身份**；多用户、多轮序列必须走事件路径（固定 actor/会话的 EventEnvelope），不能走工具面 | `crates/ligamina-core/src/commands.rs`（`tool-actor-{id}`、每次新会话） |
| 每个框架保留原生测试入口、上层统一场景描述 | 统一场景格式已部分存在：P4 behavior v2 suite 与工具用例都是“刺激 + 独立 Oracle”，并已有 stock Native 与生产 Federated 双路执行；缺的是**自动生成**，不是格式 | `lab/automation/behavior_v2.py`、`lab/baselines/`、`contracts/lab/automation/` |
| 业务正确性无法对任意插件自动判定（Oracle Problem） | 同意。但本项目要回答的是**兼容性**，原生框架本身就是参考实现：同一输入下原生与联邦路径一致即构成兼容证据，不需要知道业务语义。调研把差分列为一层，本计划把它作为主 Oracle | ADR 0002；S2-05/P4 双路径比较 |
| 随机数、时间、网络对差分的影响 | 调研未涉及，但决定差分是否可用：同一插件原生运行两次就可能不同 | P6-01 记录：现场 NoneBot Roll 未设种子，只能按模式匹配 |
| AstrBot 可借鉴其测试与模拟平台 | 需新增：目前 AstrBot 没有不经 Ligamina Bridge 的原生基线，`federated_event` 3,271 条全部 untested | `hosts/astrbot`、矩阵 |
| Koishi 加载 skipped 多 | 属实：Linux 2,002 条 skipped，主要是 core-only profile 缺 Host 服务（如 database）；这是扩大可测面的最大杠杆之一 | P3-05 Koishi 服务审计、矩阵 |

## 2. 目标与边界

目标：对每个可加载的条目，自动产出按能力维度、绑定证据的结论，并量化“测到了多少”，而不是只给 pass/fail。

回答四个问题，各有独立结论：

| 维度 | 问题 | 是否需要业务语义 | Oracle |
| --- | --- | --- | --- |
| `discovery` | 插件注册了哪些命令、消息处理器、服务、定时任务、配置项 | 否 | 运行时注册信息，静态扫描交叉核对 |
| `contract` | 插件在 Ligamina 下是否遵守框架契约（生命周期、会话作用域、异常与超时收口、工具面可调用） | 否 | 通用性质，由框架定义 |
| `differential` | 同一输入下 Federated 与 stock Native 是否等价 | 否 | 原生框架（参考实现） |
| `stateful` | 多用户、多会话、多轮序列下是否等价，状态是否按会话隔离 | 只需原生已表现出的性质 | 原生框架 + 以原生为前提的变形关系 |

非目标：

- 业务结果正确性（天气是否准、骰子是否公平）。`differential: verified` 只说明“与原生框架行为一致”，不说明插件正确。
- 外部大模型作为正式测试的生成器或 Oracle。生成与判定都是确定性的，可由 `(输入哈希, 生成器版本, 种子)` 复现。
- 修改第三方插件、Windows Enforcement 准入（仍为 P4-04）、真实平台投递（仍为 P6-01）。
- 自动生成的结论进入对外支持名单。矩阵仍是证据索引；支持声明规则见[兼容性实验](../guides/compatibility.md#当前支持范围)。

## 3. 验证结构

```text
冻结目录 ─▶ 加载（已有 census） ─▶ 能力清单（运行时 + 静态）
                                       │
             ┌─────────────────────────┼──────────────────────────┐
             ▼                         ▼                          ▼
        契约检查                生成式差分（单步）          有状态序列 / 变形关系
     （C1–C8，无需语义）     （Native vs Federated，自差分）   （固定 actor/会话，事件路径）
             └─────────────────────────┼──────────────────────────┘
                                       ▼
                 测试有效性：框架变异体 + 合成插件的检出率
                                       ▼
                 兼容性矩阵 v2（按条目 × 能力 × 平台，附覆盖度与证据哈希）
```

所有第三方代码只在已准入的 Linux Docker Worker（禁网、只读根、资源限制）中执行；生成、比较与判定都在编排侧，Oracle 不进入 Worker 输入（沿用 TOOL-04 做法）。

### 3.1 能力清单（P7-01）

新增 `contracts/lab/inventory/v1/capability-inventory.schema.json`。每个条目一份，字段按来源分开记录，不合并猜测：

| 类别 | Koishi | NoneBot | AstrBot |
| --- | --- | --- | --- |
| 命令 | `ctx.$commander` 已注册命令、参数、options、别名 | `CommandRule`、`ShellCommandRule`，新增 Alconna（`nonebot_plugin_alconna` 的规则与 `Args` 类型） | `filter.command` / `command_group`，签名类型 |
| 非命令消息处理器 | middleware、`message` 监听数 | keyword、regex、startswith/endswith/fullmatch、`on_message`、`on_notice` | regex、`event_message_type`、`llm_tool`（含参数描述） |
| 服务 | `inject` / `implements` | `require()` 的插件依赖 | star 依赖 |
| 定时与后台 | 运行时可见的计时器/cron 注册 | apscheduler 任务 | 调度注册 |
| 配置 | `Schema` 默认值与必填项 | 插件 config 模型 | `_conf_schema.json` |

- 运行时清单由现有加载探针扩展产出（AstrBot census 已上报命令，`lab/astrbot_census_side.py`）；静态清单对 Python 用 AST、对 npm 包的 JS 用字面量扫描，只作交叉核对和未加载条目的线索，置信度单列。
- 非命令处理器只进清单，不自动成为 MCP 工具；是否把 Alconna 命令暴露为工具单独评估，不扩大 ADR 0002 的范围。
- 质量测量：按生态分层抽 60 个已加载条目（每生态 20），人工从源码标注命令与处理器，计算发现率、误识别率与漏识别原因。

**P7-01 检查点（2026-11-10）：** 合同与正/负 fixture 已加；NoneBot、Koishi、AstrBot 的 `runtime.capabilities` 都返回 runtime inventory，Core 以 Host ID/epoch 独立保存并在停机/代际变化时清理。静态 scanner 从锁定发布物只读源码，并将运行时与静态来源分开。60 个样本和单人源码标注见[样本](../../outputs/acceptance/p7-p701-static-samples-20261110.json)、[标注](../../outputs/acceptance/p7-p701-manual-annotations-20261110.json)与[质量报告](../../outputs/acceptance/p7-p701-inventory-quality-20261110.json)。

| 范围 | 命令 TP/FP/FN/TN | Presence recall | Presence FPR |
| --- | ---: | ---: | ---: |
| Koishi (n=20) | 13/0/0/7 | 13/13 (100%) | 0/7 (0%) |
| NoneBot (n=20) | 16/0/0/4 | 16/16 (100%) | 0/4 (0%) |
| AstrBot (n=20) | 16/0/0/4 | 16/16 (100%) | 0/4 (0%) |
| 合计 | 45/0/0/15 | 45/45 (100%) | 0/15 (0%) |

处理器 presence（包含各 Host 的非命令事件处理器及 AstrBot `llm_tool`）：Koishi TP/FP/FN/TN=`9/0/0/11`，NoneBot=`8/0/0/12`，AstrBot=`14/0/0/6`，合计=`31/0/0/29`。本指标只判断样本中是否至少有一个相应注册，不统计注册数、不比较名称精度；动态名称在静态清单中标为 `name_confidence=unknown`。样本是按 registry ID 与缓存可用性确定的非随机子集，标签由 Codex 单次源码审阅，没有独立第二审阅者；因此该检查点达到计划的样本 presence 90% 门槛，但不能外推为万级目录的精确率。没有样本级 command/handler presence 漏项；未命名动态注册与包内源码/构建副本去重留给后续 discovery 精度测量。

### 3.2 场景合同与输入生成（P7-02）

新增 `contracts/lab/scenario/v1/`，把 behavior v2 与工具用例统一成可生成的场景：`actors`、`conversations`、`steps`（事件或工具调用）、`controls`（种子、时区、网络）、`relation`（`differential`、性质 ID 或变形关系 ID）。场景由生成器产出，不手改；同一 `(清单哈希, 生成器版本, 种子)` 必须生成相同场景（单测保证）。

输入域按参数类型构造，每条命令有上限（暂定 16 个场景）：

- 正常值：来自 usage/description/别名中的示例 token，及按类型的典型值。
- 边界：0、-1、大整数、空串、2,048 字符长串、CJK/emoji/全角、首尾空白、引号与转义字符。
- 结构：缺必填、多余参数、options 两两组合（pairwise，借鉴 NIST 组合测试，避免笛卡尔积）、别名调用。
- NoneBot 无类型命令：按原样文本，用 usage 中的 token 与通用语料组合。

生成在 Python 编排侧完成，采用 Hypothesis（锁入 `lab/uv.lock`）的策略、显式种子与失败输入缩减；Worker 只回放。fast-check 暂不引入：两条路径回放的是同一份已生成输入，不需要在 Node 侧生成。

**P7-02 检查点（2026-11-10）：** 场景与批次合同 v1、正/负 fixture 和 `lab/scenario_generator.py` 已完成；Hypothesis 锁定在 `lab/uv.lock`。同一清单哈希、描述符、Host、版本与种子稳定生成；类型域、上限 16 的 pairwise、缺必填/多余参数/别名、NoneBot 原始文本、UTC/离线控制、确定性缩减及 volatile 归一化均有单测。预算不足时合同明确返回 `pairwise.complete=false`，不冒充覆盖完成。验证报告 [p7-p702-generation-20261110.json](../../outputs/acceptance/p7-p702-generation-20261110.json)：P7-01 与 P7-02 相关 Lab 测试 14/14、py_compile、diff check 通过；没有执行插件或 Worker。此项只交付输入生成与观测归一化函数，实际 Worker 的随机/时钟/网络控制和 Native×2/Federated 回放仍属于 P7-04。

**P7-03 检查点（2026-11-10）：** stock AstrBot 4.29.1 原生基线已完成，使用自有 `p7_native_test` 平台适配器走 AstrBot 插件管理器、命令过滤器与原生 pipeline；Worker side 不导入 Ligamina Host/Bridge。运行库由 `deploy/lab/linux/astrbot/requirements.lock.txt` 以 `--require-hashes --no-deps` 准备，清单 SHA `c594e3bc43b60e956b3b911aed81d8ad613e9a5c95d4000510c0cf30c366939e`，归档 SHA `ec4e078d982a3ac90539efed022cf74a769cc5bbae2cf480309823aa049d6984`；没有在准备步骤运行目录插件。新 Linux Docker admission 的 benign probes 10/10，准入 `admitted`。冻结 AB-05 teamup/gallery/pet 共 8 个 case × 3 个全新禁网 Worker：24/24 原生尝试通过 source-bound Oracle，24/24 清理确认，24/24 与 Federated 观测按同一 AB-05 Oracle 等价；18/24 回复逐字相同，其余 6 次是 Oracle 明确允许的随机 actor ID pattern。最终报告 [p7-p703-astrbot-native-oracle-20261110.json](../../outputs/acceptance/p7-p703-astrbot-native-oracle-20261110.json)，SHA-256 `39ea221926369b217876a6ed19db243bc6430adefc9397243c9823fda65dbfe9`。早期严格字面比较器与原生观测投影各有一次被保留的诊断报告；修正后完整 Oracle-aware campaign 全通过。下一项 P7-04 / feat-049。

**P7-04 实现 smoke 检查点（2026-11-10，正式 30+30 pilot 仍未完成）：** `lab/automation/p7_differential_campaign.py` 绑定兼容矩阵 v1 中的 Linux `load=verified` 身份、冻结发布物和 profile lock，准备禁网 Worker bundle；原生框架侧独立发现命令，生成相同场景的 Native×2/Federated 三轮 fresh 执行，按 source event 归属操作，记录等价/不等价/nondeterministic/unknown、逐项归因、资源、工具暴露及 cleanup。随机控制为 UTC、固定 Python seed/`PYTHONHASHSEED` 和 Node `Math.random` preload。当前 smoke 样本及只读选择清单见 [pilot selection](../../outputs/acceptance/p7-p704-pilot-selection-20261110.json)（SHA-256 `41258d1c3540ff70586dca8a44803f7ead72d01909aaf5450bf1faafcd40384e`）；1 条 Koishi + 1 条 NoneBot 共 11 个禁网 Worker，清理 11/11 确认，三轮结论一致。Koishi 条目在原生框架未发现命令；NoneBot 发现 2 个命令、生成 22 场景，但各场景没有 Gateway 操作，66/66 观察均记为 `unknown/no_native_gateway_operation_observed`，0 个 nontrivial。该结果仅通过执行器 smoke，不代表兼容通过。报告 [p7-p704-smoke-20261110-e.json](../../outputs/acceptance/p7-p704-smoke-20261110-e.json)，SHA-256 `755835caa1e73b62090d70b4992819aabc68b86b16448a6d0cd3c5cc006d5436`。Linux Core 为避免 WSL glibc 2.39 与 Debian Worker glibc 2.36 ABI 冲突，使用 Rust 1.97.1 Debian Bookworm digest 固定镜像、`--network=none` 构建，Core SHA `be5338bbd7582526c4ed38d2eaa012e10d293bd1123731eb23f2304bc1075624`。相关 Lab tests 44/44、Koishi `npm test` 9/9、py_compile、Node syntax 与 diff check 通过。下一步执行完整每生态 30 条 pilot，再以 unknown、nontrivial、nondeterministic 数据决定是否调整 generator/确定性控制；在该决策前不得把结果外推到 P7-09 全量。

### 3.3 确定性控制与等价关系（P7-02、P7-04）

**P7-04 discovery 与大请求修正 checkpoint（2026-11-10，30+30 pilot 仍未完成）：** 首轮诊断试跑发现 Koishi Native runner 在 `app.stop()` 后读取命令表，导致退出清理清空命令；31 条目诊断报告见 [partial diagnostic](../../outputs/acceptance/p7-p704-differential-pilot-20261110-koishi-discovery-diagnostic.partial.json)，SHA-256 `9cd6ec2c213deed144285140f2d51d549b66fb4b63aeede7186a74cd1addba1d`。现改为在插件仍注册时采集；锁定样本 `koishi-plugin-bt` 在禁网 Worker 中发现 14 条命令并确认清理，证据 [discovery fix](../../outputs/acceptance/p7-p704-koishi-discovery-fix-20261110.json)，SHA-256 `d2bc0d18323ae2624ddc832b3e15da248c7a0ee4fc2dab5c201a524f725365bb`。随后两次正式尝试都准备好 60 个精确 bundle，但在 2/60 条目处停止；单条 Federated 请求实测 143,920 字节，旧实现把它放在一个 argv 值中，超过 Linux 的 131,072-byte 单参数上限并导致 Worker 未启动。Docker Worker 现支持 16 MiB 有界 stdin，请求通过 `docker run --interactive` 传递，保留旧 `--request-json` Worker CLI。143,950-byte 输入在准入禁网 Worker 中完整到达并清理；同一 177 场景请求的端到端预检 Worker exit 0、cleanup confirmed。报告 [large-stdin preflight](../../outputs/acceptance/p7-p704-large-stdin-preflight-20261110.json)，SHA-256 `f316fd8b0f341871d92534c3218ef6c2c79fc7534c5c2da7aa915676900ae2ef`。Lab 全套 289 项在补充 Windows stdin 拒绝保护前通过；当前 `test_workers.py` 10/10、worker-side stdin parser 2/2 通过。两次停止报告见 [attempt1](../../outputs/acceptance/p7-p704-differential-pilot-20261110-attempt1-incomplete.json)（SHA `4c82670d71ef12c67778ff8349630438464300431e350189fcba7b5becb6717e`）与 [attempt2](../../outputs/acceptance/p7-p704-differential-pilot-20261110-attempt2-incomplete.json)；它们不是 30+30 完成证据。大请求通过后，样本 Federated Host 仍在插件启动时退出，比较仍为 unknown；待下一轮 pilot 收集全体数据后决定是否需 P7-08 profile 扩面。不可推断为兼容通过。

**P7-04 正式 pilot 完成 checkpoint（2026-11-10）：** 第三次正式执行以新 bundle 根跑完锁定 Koishi/NoneBot 各 30 项；报告 [retry3](../../outputs/acceptance/p7-p704-differential-pilot-20261110-retry3.json)，SHA-256 `2c6f4c65402cb986aec39fb1df3f9a01589a16e2c39d9926baded357dc51be5e`。60/60 条目有终态：59 completed，1 skipped；330 次 Worker 执行全部确认清理；29 项没有 runtime 命令清单，30 项有行为场景，133 条命令均发现和执行。命令级结论为 21 nontrivial (15.8%)、5 equivalent、5 non-equivalent、0 nondeterministic、123 unknown (92.5%)。30 个行为条目的三轮 fresh 结论一致。5 个非等价命令在每条场景记录了 Native/Federated 哈希和归因；现有证据只支持 `unknown/unresolved`（两侧流程都完成，无法区分插件与 Core/Host），不宣称支持或直接归责。`nonebot_plugin_pixivbot` 因数据库迁移前置条件被跳过；复核将 `skip_domain` 更正为 `profile`，见 [retry3 adjudication](../../outputs/acceptance/p7-p704-differential-pilot-20261110-retry3-adjudication.json)，SHA-256 `71fcab8c5b74e592b828e124e49d728f5d0e9309285532d1868267346271ca8e`。retry2 在 50/60 停止，原始报告保留作历史 attempt，不覆盖。

**P7-04 → P7-09 准入裁决：** 以 `nontrivial >= 30%`、`unknown <= 50%`、行为条目三轮结论一致、Worker 清理全部确认作为全量前最低 pilot 门槛。30% 要求至少每三条已执行命令有一条触达用法提示外的行为路径；unknown 上限避免全量预算主要花在不可判定场景。当前 nontrivial 为 21/133 = 15.8%，unknown 为 123/133 = 92.5%，因此暂不准入 P7-09。先完成 P7-05 至 P7-08，改进契约覆盖、检测器有效性、有状态输入和服务/config profile，再用新证据重审；不要把未知改写为通过。

- 控制：两路使用相同的随机种子（Python 通过 Worker 预加载设置 `random` 与 `PYTHONHASHSEED`，Node 通过预加载替换 `Math.random`）、相同 `TZ`、禁网。假时钟暂不做，按 P7-04 的数据决定。
- **自差分**：Native 在两个全新 Worker 中各跑一次。两次之间不同的字段标为 volatile，从比较中去除；去除后什么都不剩的场景记 `unknown`（原因 `nondeterministic`），不计通过。
- 比较项：回复条数与顺序、目标会话与账号、消息段类型、归一化文本（掩码时间戳、UUID、耗时等 volatile token）、平台操作类型、Host 是否存活。
- 原生自身报错：若 Native 抛异常或返回框架错误，而 Federated 相同，只记为“兼容一致”，同时标 `native_error`，不计入有效覆盖；不能当作插件行为通过。
- 有效覆盖：同一命令对正常输入与“空输入/非法输入”的回复不同，才算触达了用法提示以外的路径（`nontrivial`）。矩阵按条目报告 `commands_discovered / commands_exercised / commands_nontrivial`。

### 3.4 通用契约检查（P7-05）

不需要业务语义，判定的是 Ligamina 的集成是否成立；凡原生也会表现的行为，以原生结果为前提：

| ID | 性质 |
| --- | --- |
| C1 | 加载、就绪、停止在期限内完成；停止后进程与容器清理确认 |
| C2 | Host stdout 只有 NDJSON 协议；未知事件类型不致 Host 退出 |
| C3 | 事件触发的操作只指向该事件的账号与会话；跨会话发送与原生一致才允许（如 Echo 的 `-u`） |
| C4 | 加载后的空闲窗口内无主动操作，除非清单声明了定时任务且原生同样发出 |
| C5 | 处理器抛异常后 Host 存活，后续事件照常处理 |
| C6 | 处理器超时后 Core 在期限内收口，迟到回复不进入已关闭会话 |
| C7 | 清单中的每条命令都能经 MCP 列出、schema 合法、调用在期限内返回结果或 `isError`，Gateway 操作为 0 |
| C8 | 两个插件同 Host 时，一个插件崩溃不影响另一个的事件处理（先在合成插件上验证，再抽样） |

### 3.5 有状态序列与变形关系（P7-07）

- 走事件路径，固定 2 个 actor × 2 个会话（私聊与群），序列长度上限 6。
- 依赖推断借鉴 RESTler：先单步运行每条命令，再成对运行；若命令 B 的回复因先执行 A 而改变，记 A→B 依赖边，只沿依赖边扩展序列，剪掉无关组合。Hypothesis 状态机负责生成与缩减。
- 变形关系**以原生为前提**：只有原生满足的关系才要求 Federated 满足。例如会话隔离 `State_B(T_A(S)) = State_B(S)`：原生满足而 Federated 不满足，判 failed；原生本身不满足（插件设计为全局状态），该关系对此插件不适用，记 `skipped`，原因写明。这样不需要预知插件语义，也不会把插件的设计当成缺陷。
- Host 重启后继续序列（数据目录持久化）放在最后，作为可选关系。

### 3.6 测试有效性（P7-06）

只变异**本项目代码与自有合成插件**，不改第三方发布物：

- 框架变异体：在 Host 桥接与 Core 中加入只在测试构建启用的故障点（Rust 用 cargo feature，Host 用测试构建标记 + 环境变量，生产构建不含代码路径）：错发会话、错发账号、丢回复、重复回复、丢弃非文本段、截断文本、打乱顺序、吞异常、会话间串用 actor、会话共享数据目录。
- 合成插件：三生态各一组自有小插件，覆盖计数器、组队、回显、定时、异常、超时、跨会话发送，部分带预置缺陷。
- 度量：每个变异体被哪一层（现有 Fixture、契约、差分、状态序列）检出；存活变异体逐个说明原因。这直接回答“通过率高是否意味着测到了东西”。mutmut / StrykerJS 用于 lab 自身代码，作为可选补充。

### 3.7 规模与增量

- 漏斗：Linux `load` verified 为 Koishi 2,339、NoneBot 515、AstrBot 21（另 3,247 条未测）；差分只对加载通过的条目执行，其他条目保持原结论，不重复执行。
- 每个条目每轮 3 次 Worker 运行（Native ×2 自差分、Federated ×1），场景在 Worker 内批量执行，不再按场景开 Worker；三次全新复跑只在 pilot 与抽样上做。
- 增量键：`(发布物哈希, profile lock 哈希, Host 构建哈希, Core 构建哈希, 生成器版本, 种子)`。键不变不重跑；Core 或 Host 变化时全部受影响条目重跑，生成器变化只重跑受影响生态。这是 Ekstazi 思路在条目粒度上的简化版，不做文件级依赖跟踪。
- 调度复用 P3 的 SQLite 队列、熔断、fencing 与恢复；全量预算在 P7-04 pilot 实测后写入配置，此前不做时长承诺。

### 3.8 服务与配置 profile（P7-08）

Koishi 2,002 条 Linux skipped 主要因缺 Host 服务。为高频服务提供离线 provider profile：内存数据库（`@koishijs/plugin-database-memory`）、本地存储、禁网下的 `http`（只能走错误路径，scope 写明）。NoneBot 的 `localstore`、`apscheduler` 等常见依赖同理。配置取 schema 默认值；必填且无默认的凭证类配置记 `skipped/missing_credentials`。每个 profile 单独锁定、单独记账，不覆盖 core-only 的历史结论。

## 4. 任务

依赖只写工程依赖；全部第三方执行都要求已准入的 Linux Worker。

| 任务 / feature | 依赖 | 交付 | 完成标准 |
| --- | --- | --- | --- |
| P7-01 / feat-046 | P7-PLAN | 能力清单合同；三 Host 运行时清单（含 NoneBot Alconna/关键词/正则、AstrBot `llm_tool`）；静态扫描；60 条分层人工标注样本 | 清单 schema 与正负 fixture；样本发现率、误识别率、漏识别原因成表；命令发现率暂定目标 ≥ 90%，未达到时写明原因并决定是否继续 |
| P7-02 / feat-047 | P7-PLAN | 场景合同 v1；类型化输入生成器；确定性控制与 volatile 归一化 | 同一种子生成相同场景；类型域、pairwise、缩减有单测；不执行插件 |
| P7-03 / feat-048 | P7-PLAN | AstrBot 原生基线：stock AstrBot + 自有测试平台适配器，不经 Ligamina Bridge | 已验收的 teamup/gallery/pet 在原生基线与 Federated 下按现有 AB-05 Oracle 一致；原生侧源码不导入 Bridge |
| P7-04 / feat-049 | P7-01、P7-02 | 生成式差分执行器（Native ×2 + Federated）与 Koishi/NoneBot pilot：每生态 30 个已加载条目 | 每条目报告发现/执行/nontrivial 命令数、等价/不等价/nondeterministic 数、Worker 耗时与资源；不等价项逐条归因（Core、Host、profile、插件）；三次全新复跑结论一致 |
| P7-05 / feat-050 | P7-04 | 契约检查 C1–C8 接入执行器 | 每项检查在合成插件上有正例与反例；pilot 条目全部给出 C1–C8 结论 |
| P7-06 / feat-051 | P7-04、P7-05 | 框架变异体、合成插件、检出率报告 | 每个变异体至少被一层检出，或存活原因已写明；分层检出率进入报告；PR 上运行一组小变异体回归 |
| P7-07 / feat-052 | P7-04 | 有状态序列生成、依赖推断、以原生为前提的变形关系 | 合成组队/计数器插件的序列缺陷被检出；pilot 中适用关系的条目给出结论，不适用的记录原因 |
| P7-08 / feat-053 | P7-01 | Koishi/NoneBot 常用服务与配置 profile | 每个 profile 有锁与准入；在曾因缺服务 skipped 的条目抽样上，新 profile 的 load 结论单独记账 |
| P7-09 / feat-054 | P7-03、P7-05、P7-06 | 全量 Linux campaign：先补 AstrBot 全量加载普查，再对所有加载通过条目跑清单、契约、差分；状态序列按 P7-07 结果决定范围 | 分母守恒（每个已加载条目都有终态或明确 skipped/unknown 原因）；清理全部确认；增量键可复跑；unknown 不改写为通过 |
| P7-10 / feat-055 | P7-09 | 矩阵 v2（新增 `discovery`、`contract`、`differential`、`stateful` 及覆盖度）、报告、指南与 CI | v1 矩阵与 schema 保持不变；v2 每条结论引用证据与哈希；`check` 在证据变化时失败；兼容性指南与 README 不新增支持名单 |

P7-03、P7-08 与 P7-04 可并行。P7-04 与 P7-06 是两个决策点，见第 5 节。

## 5. 决策点

调研第八节的三个假设对应前三个检查点，在扩到全量之前必须有数据：

| 检查点 | 时机 | 继续的条件 | 不满足时 |
| --- | --- | --- | --- |
| 能力能否自动发现 | P7-01 结束 | 命令发现率达标，漏识别原因可归类 | 先补发现规则，或缩小 P7-04 范围到可发现的类别 |
| 生成的输入能否触达有效路径 | P7-04 结束 | nontrivial 命令占比至少 30%、unknown 不超过 50%、三轮结论一致且清理全确认。30% 要求至少每三条执行命令触达一条用法提示外路径；unknown 上限避免全量预算主要耗在不可判定输入。 | 当前 15.8% nontrivial、92.5% unknown；先完成 P7-05–08 并复核 profile/生成器，不准入 P7-09。 |
| 测试能否识别缺陷 | P7-06 结束 | 变异体全部检出或存活原因可接受 | 补检查项后重测，不进入全量 |

## 6. 不采纳或暂缓

| 方案 | 处理 | 理由 |
| --- | --- | --- |
| 用 NoneBug / `plugin-mock` 替代生产路径 | 不采纳 | 兼容性要测的就是生产 Core/Host 路径；Native 侧已有 stock runner。二者的会话与 fixture 写法可在原生 runner 中参考 |
| Pynguin | 暂缓 | 插件依赖框架生命周期与全局上下文；生成的回归断言会把现有输出当正确答案，不适合作 Oracle |
| OSS-Fuzz / ClusterFuzzLite | 不采纳 | 面向内存安全与覆盖引导模糊测试；本阶段借鉴其失败样本保存与复现，用现有证据目录实现 |
| Testcontainers | 不采纳 | Worker 禁网，服务用离线 provider profile（P7-08） |
| Toxiproxy | 不在本阶段 | 网络故障注入属于 Gateway 可靠性（P6-02 已有本地故障注入） |
| pytest-testmon、文件级回归选择 | 暂缓 | 条目粒度的增量键已足够；文件级依赖跟踪的收益在全量数据出来后再评估 |
| 外部大模型生成或判定 | 不用于正式测试 | 结果不可复现；可在人工标注阶段辅助阅读源码，但标注结论由人确认 |

## 7. 风险

- **Oracle 边界**：差分只证明与原生一致。依赖网络的插件在禁网下只会走错误路径，结论 scope 必须写“仅错误路径”。依赖 LLM provider 的 AstrBot 插件同理。
- **原生基线自身的偏差**：stock runner 与真实部署存在配置差异；原生侧异常要单独标记，不能混入兼容结论。
- **规模成本**：每条目 3 次 Worker 运行，全量时长只能在 P7-04 后估算；预算超出时先按生态分批。
- **任务顺序**：P6-01 / feat-030 于 2026-11-10 完成；受控重复投递和 SQLite 账本去重见[第六阶段计划](2026-11-08-phase-6-runtime-reliability.md) 3.1d。P7-01 / feat-046、P7-02 / feat-047、P7-03 / feat-048、P7-04 / feat-049 均于 2026-11-10 完成，当前 active feature 为 P7-05 / feat-050。P7-01 的样本指标为源码声明 presence，不是运行时 discovery 或全目录估计；P7-03 只覆盖三个已验收 AstrBot 插件与 AB-05 用例。P7-04 30+30 pilot 的 unknown/nontrivial gate 未达全量门槛；P7-09 当前不准入，先完成 P7-05 至 P7-08 后复核。受控本地重复投递不代表真实 QQ 重复投递观察。

## 8. 验证命令（规划阶段）

```powershell
pwsh -NoProfile -File init.ps1 -Mode docs
git diff --check
```

规划阶段没有运行任何插件、Worker 或运行时测试。
