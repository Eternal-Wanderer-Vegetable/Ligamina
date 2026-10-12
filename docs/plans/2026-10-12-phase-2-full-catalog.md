# 第二阶段：官方目录全量插件测试与输出规范

- 日期：2026-10-12（Asia/Shanghai）。计划建立时 S2-01–S2-12 尚未执行；执行状态由本节和 [feature_list.json](../../feature_list.json) 维护。
- 前置：第一阶段 T01–T18 / C01–C15、G1/G2 已通过；当前验收见 [T18](../../outputs/acceptance/t18-final-audit-20261012.json)。
- 用户目标：全量调取 NoneBot 和 Koishi 的官方插件，自动观察能否正常运行，并规范输出与 README 展示。
- 执行状态唯一入口：[feature_list.json](../../feature_list.json)；本计划定义依赖、文件、验证与退出条件。

## 执行状态记录

- 2026-10-12：S2-01、S2-02 已分阶段提交；S2-04 已在 S2-03 前完成。S2-03 的 sdist 依赖解析可能执行第三方构建后端，因此本次执行依赖已验收的 OS Worker；此执行顺序已写入 `feature_list.json`、`progress.md` 和 `session-handoff.md`。
- S2-04 Linux 和 Windows benign probes、运行时策略与清理记录见 [双平台验收](../../outputs/acceptance/phase2-workers-20261012.json)。WSL namespace 方案因 namespace root 可重新挂载只读 bind mount 而拒绝，证据见 [WSL boundary probe](../../outputs/acceptance/phase2-wsl-userns-boundary-20261012.json)。没有执行第三方插件。
- S2-03 已完成：为 5,690 个 entry 冻结 6,247 个平台归档引用；11,380 个 Windows/Linux profile 单元中，10,332 个锁定依赖完整并可复核，其他均记录显式终态。报告：[S2-03 验收](../../outputs/acceptance/phase2-s203-20261012.json)。未运行 sdist backend、npm lifecycle 或目录插件。
- S2-05 已完成并分阶段提交 `854b564`；四个既有支持版本在禁网 Linux Docker Worker 中完成 Native/Federated/独立 oracle 配对。
- S2-06 已完成两生态十类声明式 recipe 和 fail-closed 对照器，验收报告见 [S2-06](../../outputs/acceptance/phase2-s206-20261012.json)；recipe 执行接线留给 S2-07。
- S2-07 已完成并提交 `7523a83`：持久 catalog stage queue、六阶段依赖图、租约/attempt journal、分片与 resume 已验收；报告见 [S2-07](../../outputs/acceptance/phase2-s207-20261012.json)。CLI 在真实冻结快照上计划 3 个 Linux 单元共 18 个作业，并从相同 manifest 恢复；未派发插件 Worker。
- S2-08 实施前基线已在干净提交 `16fdf63` 上完成（[S2-08 baseline](../../outputs/acceptance/phase2-s208-baseline-20261012.json)），全仓库 `init.ps1 -Mode full` 退出码 0。
- S2-08 已完成：Python/Rust CLI 接入 schema 校验、summary 重算、JSON/text/NDJSON、矩阵与 gate 退出码；README 双阶段区域由带哈希校验的验收输入生成。报告、测试与全仓库结果见 [S2-08 acceptance](../../outputs/acceptance/phase2-s208-20261012.json)。没有派发 catalog 插件。
- S2-09 实施前 baseline 已在干净提交 `cbcbb0a` 上通过 `pwsh -NoProfile -File init.ps1 -Mode full`（[S2-09 baseline](../../outputs/acceptance/phase2-s209-baseline-20261012.json)）。试点清单覆盖每生态 30 个目录 entry、四个精确历史支持版本，保留 11,380 条 entry×OS 分配账本；详见 [manifest](../../outputs/acceptance/phase2-s209-pilot-manifest-20261012.json)。
- S2-09 pilot 执行已完成：[acceptance](../../outputs/acceptance/phase2-s209-20261012.json) 对 128 个位置全部记账：4 passed、11 failed、88 skipped、25 unknown。四个旧版本的 Linux Native/Federated/oracle/cleanup 均通过；NoneBot Linux 与 Koishi Linux/Windows 各 30 项终态闭合。分平台结果见 [NoneBot Linux](../../outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json)、[Koishi Linux](../../outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json) 与 [Koishi Windows](../../outputs/acceptance/phase2-s209-koishi-windows-pilot-20261012.json)。
- S2-09 failure review：[复核报告](../../outputs/acceptance/phase2-s209-failure-review-20261012.json) 对 11 个 native-load 失败逐一记录包身份、平台、profile/artifact hash 和观察错误。NoneBot 3 项命中冻结 `nonebot_plugin_htmlrender` 的 API 导出不匹配；Koishi 两平台各 3 个入口未解析到配置的 `lib/index.js`、各 1 个 `koishi-thirdeye` 加载时类型错误。Windows `random-chooser` 的 cleanup unknown 经重试确认 cleanup，终态为 missing-fixture skipped；两个原始 attempt 均保留。
- S2-09 done criteria 已满足：三组平台 pilot 与全 128 位置闭合；negative Worker probes、S2-07 队列恢复和本任务 checkpoint/worker-lost 恢复通过；资源预算有可达平台的 P50/P95 实测。11 个 native-load 失败和 25 个 unknown 是 pilot 发现，不会被算作通过。NoneBot Windows 23 个 catalog 行因 App Control Win32 4551 保持 unknown，另有两个历史 Windows 配对 unknown；entry-bound 行为 fixture 未执行，行为兼容性仍未评估。
- S2-09 资源：[budget config](../../config/lab/phase2-pilot-budget-v1.json) 保持 provisional。Koishi Linux scratch P95 约 34 MB；Koishi Windows Sandbox scratch P95 约 143 MB，Job Object peak commit P95 约 6.71 GB、最大约 8.57 GB（8 GiB limit 内）。NoneBot Windows 因 CPython App Control 没有 Worker RSS/scratch 数据；全快照资源估计仍待 S2-10 分片实测。S2-07 队列中断恢复及本任务 checkpoint/worker-lost 恢复测试提供恢复证据；全量 census 与真实平台验收未启动。
- S2-10 clean baseline 在 S2-09 阶段提交 `20d8f04` 上通过（[baseline](../../outputs/acceptance/phase2-s210-baseline-20261012.json)）。已实现可恢复 shard dispatcher、六阶段投影、Koishi `platform=all` artifact 映射和 per-plugin/evidence 汇总；实现后的全仓库验证结果见 [checkpoint](../../outputs/acceptance/phase2-s210-implementation-20261012.json)。11,380 entry×OS 中 10,332 个 profile lock 为 `locked`，只代表候选。隔离 smoke shard [报告](../../outputs/acceptance/phase2-s210-census-linux-shard0of256-smoke-20261012.json)另用 queue DB、不并入正式 census。Linux shards 0–135/256 与 Windows shards 0–54/256 已闭合。当前聚合 4,264/11,380 单元、191/512 shard，缺 7,116 单元和 321 shard（0 passed、444 failed、3,638 skipped、182 unknown）；Linux shard `8/256` 已在旧 install lease 到期后恢复闭合，Linux `136/256` 与 Windows `55/256` 正在运行。最近的数据 checkpoint `b2d77fb` 已提交；完整 census 尚未完成。

## 1. 范围与全量定义

主集合采用两个官方目录收录的全部插件，包含社区作者插件；官方维护插件单独统计。这是本次规划口径，不把“商店收录”“认证”和“官方维护”混为同一属性。若用户后续限定仅官方维护，改变 cohort，不丢弃已经收集的目录账本。

全量指冻结快照中全部 registry entry，不指所有历史版本、不指在一个进程同时加载全部插件。每 entry 使用明确冻结的最新稳定版本策略；保留 framework 不兼容、无发布物、失效、unsafe、缺凭证、无 fixture 等记录。下载与依赖缓存可按哈希去重，模块/入口身份与测试账目不合并。

第一阶段 profile 为主对照组：NoneBot 2.5.0 + OneBot v11 adapter 2.4.6 + CPython 3.12.14；Koishi core 4.18.11 + Node 24.20.0。额外 Python/Node/framework、Koishi loader、其他 adapter profile 必须另有锁和结果，不能替换主组后宣称主组兼容。已支持的四个旧版本继续回归。

## 2. 官方来源与已核对事实

| 用途 | 来源 | 规划时核对 |
| --- | --- | --- |
| NoneBot 官方商店规则 | [官方发布指南](https://nonebot.dev/docs/developer/plugin-publishing) | 商店收录包含开发者发布插件 |
| NoneBot 主目录 | [plugins.json](https://registry.nonebot.dev/plugins.json) | JSON array；module_name/project_link/version/is_official/supported_adapters/valid/skip_test |
| NoneBot 镜像/上游 | [registry results](https://github.com/nonebot/registry/tree/results)、[nb-cli store.py](https://github.com/nonebot/nb-cli/blob/master/nb_cli/handlers/store.py) | 官方 CLI 使用 registry 及镜像；results 中含 configs 和测试结果 |
| NoneBot 变更对照 | [nonebot2 assets/plugins.json5](https://github.com/nonebot/nonebot2/blob/master/assets/plugins.json5) | 可交叉核对目录，JSON5 需明确 parser；不是唯一执行输入 |
| Koishi 主目录 | [index.json](https://registry.koishi.chat/index.json)、[registry 仓库](https://github.com/koishijs/registry) | version/time/total/objects；object 含 package/manifest/verified 等 |
| Koishi 官方维护集合 | [官方插件页](https://koishi.chat/zh-CN/plugins/) | 包含 adapter、数据库、控制台、服务和常用功能，不限消息插件 |
| 发布物与版本 | [PyPI JSON API](https://docs.pypi.org/api/json/)、[Index API](https://docs.pypi.org/api/index-api/)、[npm registry 元数据](https://github.com/npm/registry/blob/main/docs/REGISTRY-API.md) | 版本清单优先 Index API；冻结 release 用 JSON API 核对依赖/发布时间/完整性，避免依赖已 deprecated 的项目 releases 字段 |

此次目录元数据观测：NoneBot 943 条、Koishi 4,747 条，见 [来源探针](../../outputs/acceptance/phase2-catalog-source-probe-20261012.json)。这是规划观测，未保存完整冻结执行目录、未下载插件、未完成全量测试；执行时重新抓取和计算分母。

来源切换必须记录 URL、HTTP validators、UTC 获取时间、原始字节 SHA-256、上游 commit/版本与镜像差异；接口失败不能当作空商店。NoneBot 上游 valid/skip_test 与 Koishi verified/unsafe 元数据仅作为输入信息，不继承上游“兼容通过”结论。

## 3. 现有代码可复用处与缺口

| 当前事实 | 第二阶段措施 |
| --- | --- |
| samples/third-party.lock.json 固定 20 样本，representative gate 每生态两个 L4 | 新 catalog suite 与独立 gates；保留代表样本 smoke，不把 20 的校验放宽后就称全量 |
| representative 的 L0 下载/哈希、L1 lock/profile、L2/L3/L4 与 trace 可用 | 提取共同执行组件，旧入口/报告继续回归；新 manifest 允许任意 entry 数 |
| DirectWorker 复用 Ligamina worker/Bridge | 新独立 stock framework native baseline，再对比生产 Core→Host→Gateway |
| NoneBot 使用 discover 推断 entry，Koishi 依赖 core profile；复杂 dependency 服务有限 | registry 声明 entry 优先并校验发布物；显式 preload/config/services/loader profile |
| 隔离 HOME/目录/凭证不是 OS sandbox；日志只截取尾部、整体循环主要串行 | OS Worker、完整脱敏 artifact、持久队列、分片、资源预算、断点恢复 |
| 安装禁用 npm scripts，Python 使用 --no-build | 保留受限默认；native build/browser 等走独立受控 profile，未满足时明确 skipped 原因 |
| CLI report 与 lab summary 不同格式，缺 catalog/stage 统计 | 版本化 report schema、reason codes、共享汇总、JSON/text/NDJSON、README 生成 |

源文件：[representative.py](../../lab/representative.py)、[native_runner.py](../../lab/native_runner.py)、[compare.py](../../lab/compare.py)、[report.py](../../lab/report.py)、[CLI main](../../crates/ligamina-cli/src/main.rs)。

## 4. 执行流程与验收级别

顺序：官方目录快照 → 版本/发布物锁 → 隔离安装 → 原生基线 → 联邦执行 → oracle/差分 → 生命周期与清理 → 单插件结果 → 聚合报告 → README。

全目录进入 L0/L1 账本；在入口、依赖、配置和 profile 满足时自动推进。每个插件独立运行；依赖插件/服务作为显式 recipe，不把生态全集装到同一个环境。

| 级别 | 新 suite 必须观察 | 不能替代的证据 |
| --- | --- | --- |
| L0 | registry→包→冻结版本→发布物 bytes/integrity 与 license/provenance | 仅查到 package metadata |
| L1 | 锁定依赖解析/安装，实际 framework 版本一致 | 下载成功 |
| L2 | 原生生命周期 ready、入口加载、服务可用，启动后短窗无延迟异常 | import 无异常 |
| L3 | 相同刺激进入生产联邦路径，事件/操作/回执作用域正确 | event accepted 或进程存活 |
| L4 | 原生与联邦各自通过独立 oracle，再比较行为/持久状态 | 两条路径相同但错误、没有回复、无 fixture |
| L5 | 基本 stop/restart/cleanup；选定分层 cohort 的长时间稳定性 | 所有插件都通过 24h 的未执行声明 |

消息插件测试文本、命令、私聊/群聊、mention/reply、权限与错误输入；服务/存储插件测试依赖注入、读写、重启持久化和清理；scheduler 使用测试时钟和可观察任务；adapter 使用本地协议 peer，真实登录/收发另记 real_platform；console/loader 用真实 HTTP/浏览器和资源断言，不能仅套消息 fixture。

缺少行为 recipe 的插件仍完成加载账目，L4=skipped/missing_fixture。原生失败保留 native failure，后续 stage 说明跳过原因；原生成功联邦失败优先排查 Ligamina。只有 stock baseline 的独立预期验证通过，才有资格给行为支持结论。原生离线 Mock/peer 也必须标虚拟范围。

## 5. 输出合同与分母

字段、状态映射、率、退出码和 README 规则统一按 [输出规范](../guides/reporting.md)，S2-02 将规范转成 executable schemas、golden fixtures 和汇总函数。

每个测试单元唯一键：(snapshot_id, registry_entry_id, artifact_hash, profile_hash, OS/arch, stage, case_id, attempt_id)。schema_version + comparator_version 决定解释方式。共享日志含 run/entry/stage/trace ID；配置和凭证脱敏；原始 artifact 和报告保留 SHA-256。

完整矩阵提供 JSON、CSV、Markdown；README 仅摘要和证据链接。安装/启动成功率、行为测试覆盖率和行为兼容率分别展示，官方维护与社区子集、Windows/Linux、主/额外 profile 分开统计。missing、skipped、unknown、flaky 在摘要可见。

## 6. 任务、文件、依赖与退出条件

下表的新增路径均是计划，当前不存在的入口不作为可运行命令展示。每任务的详细测试见第 7 节。

| ID | 交付和主要文件 | 依赖 | 完成条件 |
| --- | --- | --- | --- |
| S2-01 | lab/catalog/{nonebot,koishi,snapshot}.py；catalogs/<snapshot>/ raw + normalized + provenance | 第一阶段/harness | 两目录完整快照、来源切换/差异记录、entry 映射、官方子集、分母/哈希可复算 |
| S2-02 | contracts/lab/v1/{catalog,run,result,summary}.schema.json；lab/result.py；golden fixtures | S2-01 | 新旧状态映射、各分母公式、reason/exit codes、三语言消费者验 schema；不能把 completed 当 passed |
| S2-03 | lab/artifacts.py、lab/profiles.py；catalogs/<snapshot>/artifacts.lock.json + profiles | S2-01/02/04 | 全 entry 版本/发布物或明确终态、完整依赖锁、缓存哈希、源代码构建/不可用策略；旧支持版本另组回归。sdist backend 只能在 S2-04 OS Worker 内执行 |
| S2-04 | lab/workers/{base,linux,windows}.py；deploy/lab/；Worker probes | S2-02 | 安装与执行 OS 隔离；无宿主 secrets/repo写权/socket；资源/网络/子孙进程与清理证据通过 |
| S2-05 | lab/baselines/{nonebot,koishi}；native_runner.py 接口扩展 | S2-02/03/04 | stock native 与 Ligamina 路径独立；同 bytes/locks/config/stimulus；四个已有支持插件及反例双路径通过 |
| S2-06 | recipes/<ecosystem>/；lab/recipes.py、compare.py、Host profile 扩展 | S2-05 | 命令/服务/存储/定时/adapter/console 各有验证 recipe；entry/profile/config/services 绑定，缺 fixture 不假通过 |
| S2-07 | lab/catalog_run.py、lab/queue.py；lab/run.py --suite catalog；checkpoint/journal | S2-03/04/05/06 | prepare/install/native/federated/compare/cleanup 状态落盘；并发、租约、断点恢复、重试与分片无重漏 |
| S2-08 | lab/report.py、CLI main/lib；scripts/render-readme.mjs 扩展；reports/<run>/ | S2-02/07 | 校验后统一 JSON/text/NDJSON、计数/矩阵/退出码；README 与报告同源且可检查漂移 |
| S2-09 | pilot 清单/报告；预算配置；双 OS Worker 验收 | S2-06/07/08 | 分层至少 30 entry/生态 + 全部既有支持版本，Windows/Linux pilot 全记账，负例和中断恢复通过 |
| S2-10 | 两 OS 全快照 census、全 per-plugin 报告和 evidence manifest | S2-09 | 每 entry×OS 有终态；全部可达 stage 按策略尝试；未调度/漏项为 0，完整 artifact/hash 与资源清理 |
| S2-11 | failures.md、runtime 修复、scoped regression、完整 rerun 报告 | S2-10 | 每失败有定位域/原因/复现；修复保留旧结果；native-success/federation-failure 清单审阅闭合 |
| S2-12 | phase-2 audit、生成 README、兼容性矩阵和交接 | S2-11 | completion/automation/compatibility gates 分开裁决；支持声明有证据；第二阶段状态与限制准确 |

关键路径：S2-01 → S2-02 → S2-03/04 → S2-05 → S2-06 → S2-07 → S2-08 → S2-09 → S2-10 → S2-11 → S2-12。单会话聚焦一个任务；任务依赖不表示当前授权启动全量执行。

## 7. 必须验证的场景

| 任务 | 场景与预期 |
| --- | --- |
| S2-01 | 200 正常/分页缺页/空异常/429/镜像陈旧/重复包多 entry/目录更新：全部闭合且来源可追溯；不静默缩小分母 |
| S2-02 | passed/failed/skipped/unknown/missing/duplicate/zero denominator/old completed/not_run：golden 计数一致，格式错误拒绝 |
| S2-03 | 发布物 hash 错/撤包/yanked/prerelease/Requires-Python/peer conflict/sdist-only/build scripts：精确分类，不偷换 latest 或 Host baseline |
| S2-04 | 读宿主文件/写挂载/越界网络/无限日志/内存耗尽/超时/子孙残留：限制生效、终态和清理可观察；失败 Worker 不影响兄弟 |
| S2-05 | stock 调用不导入 Ligamina Bridge；群/私同 ID、reply/mention、权限、wrong-recipient：独立 oracle 捕获错误 |
| S2-06 | 配置/preload/services/loader 缺失、空响应、随机/time/http fixture：缺条件明确跳过；两边同错不能通过 |
| S2-07 | 任意 stage kill/机器重启/重复派发/过期租约/shard 重叠/cache 污染/部分报告：无漏项、未知不抹掉、可复跑单 entry |
| S2-08 | 无效 schema/未知状态/部分 run/stale README/secrets/大日志：退出码正确、stdout 可解析、摘要可复算并脱敏 |
| S2-09/10 | 两生态与两 OS；官方维护和社区、消息与非消息、已知失败与条件缺失：全 cohort 分母与终态守恒 |
| S2-11/12 | 修复前后同冻结输入；失败转通过可追溯；运行时变更执行既有 G1、锁定测试和受影响安装路径 |

## 8. 资源、网络与断点策略

初始预算是执行配置，不是已测性能承诺：普通 Worker 每机并发 2，浏览器/构建重任务 1；每任务 install 20min、startup 120s、场景 60s、cleanup 30s。Linux 初始单 Worker 2 CPU/4GiB/256 PIDs，重任务独立 8GiB profile；Windows 使用等效可验证限制。pilot 测得 P50/P95、峰值 RSS/磁盘和下载量后再冻结全量预算。

下载阶段只允许 registry/package 必需地址；行为阶段默认离线、本地 fixture 网络。外部网络、浏览器下载、数据库服务、付费 API 和真实平台凭证放进有配置记录的独立 profile，不继承开发机凭证。unsafe/许可证不清楚条目仍全量记账，按受限 profile 或明确原因终态处理。

metadata 网络失败最多 3 次指数退避并尊重 Retry-After；行为失败最多复跑 1 次用于辨别 flaky，不覆盖原结果。进度 journal 每 stage 原子写入，先持久化执行身份再启动 Worker；worker lost=unknown 后清理/重新调度，at-least-once 调度 + 幂等结果键。缓存先核验 hash，再只读使用；Native/Federated 分开 data dir 和持久状态。

完整 logs/traces 作为 artifact，README/终端保留短摘要。输出预算耗尽要标 artifact_truncated/unknown，保留复现入口，不能仅截日志后给无证据通过。磁盘预估先由 pilot 给出，低剩余容量暂停派发并保留 checkpoint，不丢已完成结果。

## 9. CI、运行方式与验收 gate

既有 PR CI 保留锁定 tests、G1 和四个支持版本回归；增加离线 catalog/schema/queue/report 检查。全量使用单独手动工作流和隔离 runner，不在每个 PR 运行全集，也不在本次创建自动调度。

拟定入口（S2-01/07/08 后才能运行）：catalog snapshot → resolve --snapshot → run --suite catalog --snapshot --platform --shard --resume → report --format → render-readme。参数落盘、命令生成到报告；每个插件可使用稳定 entry_id/profile/run 重现。执行前先完成 S2-09 pilot，估算总耗时和资源后再全量派发。

| gate | PASS 条件 | 结论 |
| --- | --- | --- |
| P2-CATALOG | 来源快照完整，raw/hash/entry分母闭合，版本策略固定 | 全量目录已冻结 |
| P2-CENSUS | entry×OS 终态记账 100%，未调度/漏项/重复无效项为 0；阻塞原因和证据完整 | 全量调查完成，允许插件失败/条件缺失 |
| P2-AUTOMATION | 无 harness/infrastructure 未解决错误；按政策全部可达 stage 已尝试；合法 skipped 分类可复核、无批次遗留 running/unknown | 自动化执行闭合，不等于全部兼容 |
| P2-BEHAVIOR | 所声明 cohort 的所有必选场景双路径/oracle/cleanup 通过；skipped/unknown/failed/flaky 为 0 | 指定 cohort/profile 行为兼容 |
| P2-FIELD | 独立真实平台登录/动作/回执/收件人证据 | 真实平台验证；缺失保持未验证 |

第二阶段调查可在不兼容插件存在时完成；“所有插件正常运行”只有全集 P2-BEHAVIOR 满足且相应范围完整时才能声明。需要凭证或行为 fixture 的条目留在缺口清单，不能为通过 gate 改写为 supported。P2-FIELD 未执行时 README 明示虚拟范围。

## 10. 本次交付与下一步

本次已安装项目级 skill、整理文档与验证入口、定义输出合同、生成第一阶段 README 证据摘要，并建立第二阶段任务清单。尚未执行 S2-01–S2-12。

下一任务是 S2-01：以此次 source probe 为起点实现目录抓取、完整快照/哈希、entry 身份与官方维护 cohort；然后实现 S2-02 的 schema，再扩展执行器。第二阶段完成报告放在新路径，不覆盖第一阶段 acceptance。
