# 第四阶段：行为兼容性与运行就绪

日期：2026-10-26（Asia/Shanghai）。以下是计划起草时及各历史检查点的记录；后续按日期追加实施证据，不改写历史批次。源码计划基点：`9d03728853bfb18a52e1640b2084a624c70501ba`。

状态摘要（截至 2026-10-28；后续进展按日期追加）：P4-01/02 scoped engineering deliverables 已完成。P4-03 Linux 39 个 retry-budget identities 累计 3 recovered / 36 unknown；P4-03 Linux 与 P4-AUTOMATION 保持 blocked。用户先将 P4-04/feat-029 收窄为 runner-only，该七项子任务已完成；随后恢复原 Windows Enforcement 准入与行为验收范围。当前 active_feature=feat-029 / in-progress。新鲜 guest 的 3099 与 unsigned PE/NoneBot native extension 的 3077 共同确认当前 base policy identity/options/hash；CiTool 仍返回 HRESULT 0x80073BC3，因此完整 active-policy inventory 与 base-policy composition 未知。Koishi 已完成 Native Host isolation、positive、negative 三个冻结 case，各 3 次独立通过（分别 9/6/4 条 Oracle assertions，清理确认）；isolation 早期两次 setup/harness unknown 与 negative 首次 WSB 启动前 gRPC pipe unknown 均保留；无 Federated Core parity。NoneBot 另冻结签名 CPython 3.12.10 profile，锁定 `pydantic-core` 扩展被该 Enforcement policy 阻止，结果保持 unknown。自签名实验在 disposable guest 内生成并验证了独立 `.pyd` 签名副本；将发布者证书加入 guest 当前用户 Root/TrustedPublisher 后，Authenticode 与 SignTool 验证通过，但加载仍返回 Win32 4551。当前 guest 3099 确认 `VerifiedAndReputableDesktop` identity/options/hash，然而本轮没有匹配的 3077/3089，事件级关联未闭合；原 wheel 与策略未变。两个本地 Ligamina Core 均为 NotSigned，GitHub releases 无可用签名 binary。P4-WINDOWS-BEHAVIOR 仍 blocked，catalog admission=not_evaluated、claims=false。综合 review：[policy/profile/enforcement review](../../outputs/acceptance/phase4-p404-policy-preflight-20261028-v3.json) SHA-256 `910c87ad1c5eae4a999ea859146cb06723d57062b4b770e9ac25fd5f6242f728`；[self-signed experiment review](../../outputs/acceptance/phase4-p404-selfsigned-code-signing-20261028/selfsigned-signature-review.json) SHA-256 `7b3be69a418a8adb3ec289f6a028371faff51e379df3f8e82c6ee2cba0da26ae`。P4-05/P4-06 未启动，完整 Windows、跨平台、field 与 release gates 未通过。

### 2026-10-29 P4-05 Stella/Ligamina connector implementation

- 本地源诊断确认之前阻碍不是 Stella 原生回复发送失败：用户已确认一条 Stella native @ 回复成功，但其 trace 没有 Ligamina Client/Gateway/Host/operation 链。缺的是 P4-05 在 Stella 正常 ingress 与 Ligamina Host/Gateway 之间的 connector，并须防止 native LLM 与 Gateway 双发。
- 新增可移植扩展 `clients/stella/ligamina_bridge/__init__.py` 和操作说明 `clients/stella/README.md`。`setup(pipeline)` 默认关闭，只有 `STELLA_LIGAMINA_ENABLED=1` 才注册 matcher/background Gateway。配置要求 loopback Core URL、独立 client/Gateway token、client/gateway/account/Host ID，以及 Stella `ALLOWED_GROUPS` 的明确子集；凭据只从本机环境读取。
- 只接受通过现有 `ai_gateway.is_chat_trigger` 的 normal group ingress，并要求显式 `!ligamina` 前缀和纯文本；仅该 marker 会在 native chat handler 之前被截获，普通 Stella chat 保持既有处理。EventEnvelope 的 event ID 从 OneBot account/group/message ID 稳定生成，trace ID 优先复用 Stella flow trace，事件只投递给配置的 Host。
- Gateway adapter 依 Core HTTP contract 完成 lease/claim/result；claim 校验 `principal={kind:host,id:<configured Host>}`、record/payload trace 一致、client/account 精确匹配、目标仅为双重 allowlist 群、消息仅纯文本且有长度上限。通过同进程现有 active Bot 和可信 `BotRef`/`SceneRef` 调用 `core.transport.outbound.OutboundService.send`，不调用 SnowLuma。明确 accepted 映射 succeeded，确定 failed/expired 映射 failed；partial/unknown/发送异常映射 unknown，不重放。日志不写消息正文或凭据。
- 本地专项验证：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest clients/stella/tests/test_ligamina_bridge.py -q` 通过 13/13；操作起始、OutboundService 调用前及终态日志携带已验证的 operation/event/trace ID，便于和 Stella outbound 状态记录关联；Stella runtime CPython 3.12.10 的 `py_compile` 与 stdlib-only import 通过。直接使用全局 pytest plugin autoload 时，插件环境在执行用例前因未处理的异步 fixture `after_nonebot_init` 报错；禁用自动 plugin 加载后专项测试通过，未更改环境或安装依赖。
- 同一扩展副本现已暂存到用户的 dirty Stella checkout `E:\stella\stella_project\extensions\ligamina_bridge`，源/目标 initializer SHA-256 相同；新增目录在 Stella repo 中保持 untracked。尚未配置启动环境、读写 token、重启进程或发送 marker 消息。下一步由用户在本机私密配置中填入已注册 Core client/Gateway 环境值，确认 Core 与精确 Host 就绪后重启现有 Stella，再在既有授权范围的配置群中触发一条新 `@Stella !ligamina echo <unique marker>`；分开核验 Core event accepted、指定 Host trace/principal、operation、Stella outbound receipt、接收端独立观察与清理。旧三个 direct-SnowLuma `result=120` operation 继续 unknown 且不重放。`feat-030=in-progress`、`P4-FIELD=blocked`，本地实现不构成 field acceptance。
- 机器报告：[P4-05 Stella/Ligamina bridge implementation](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-ligamina-bridge-implementation-20261029.json) SHA-256 `66096e3d024321ad02a1741b3472e73a516b190bc65e66563463e911558769b6`；本次验证、源码 hash 与 sidecar 见报告及 `.sha256`。

### P4-05 field candidate preflight（2026-10-29）

- 用户确认当前无法立即取得 P4-04 Enforcement 可接受的签名证书，并要求先跳过缺口、继续其他可推进项目。P4-04/feat-029 由 `in-progress` 转为 `blocked`；继续需要策略接受的签名身份，或另行审阅/版本化的 Sandbox-only test policy，并补齐目标文件的 Code Integrity 事件关联。此前自签名 v8 结果仍保留：签名验证成功但 Core 仍 Win32 4551；没有因此更改策略、目录准入或行为结论。
- 按任务表切到工程依赖已满足的 P4-05/feat-030。只读复核 P4-02 v2r3 campaign 与 closure 后，为每个生态各选一项满足该样本全部已冻结必选 case 的 Linux 候选：`nonebot_plugin_roll@0.2.3`，registry entry `nb-16c80a826ca035f6c49ad69a`，artifact SHA-256 `3a5ead2404f26711c9d41604ddcbb2e141df8a950fc53d32536aa7980fab1a2c`，profile lock SHA-256 `792fe11f2fd3581e888b15a23bc0c49f76b3b0d2399fc728dbfcff3d436214df`；`@koishijs/plugin-echo@2.2.5`，registry entry `ko-163c3bd4cf87c72c24210251`，artifact SHA-256 `79da39692ba8fce04ea597ff85696a0fe193cd89a95a91a0d459ea40ab592265`，profile lock SHA-256 `33e0897b794134c39215e7824a47edec3eb4acc79fcb9f4df00a03c1455f616b`，Host provider lock SHA-256 `5e3df82b0c353a8ded6c32708a8a591d9854182a38c6af9924c3e2671862bbcb`。
- 两个候选的 positive、negative、isolation Linux case 均各有 3/3 fresh paired attempts `passed`，合计每个候选 9/9 位置通过、cleanup 9/9 confirmed；本次只依据其精确 v2r3 子集进行候选选择。P4-02 全局状态保持 `p4_linux_behavior=failed`（另一个冻结 NoneBot Help 0.6.1 样本有 9 个 plugin compatibility failures）、`p4_fixture_gate=blocked`、48 个必选位置 blocked、144 个 slots not_dispatched；不据此扩大支持矩阵，也不将历史四插件 virtual-receipt regression 当作真实 field acceptance。主要证据：[closure](../../outputs/acceptance/phase4-p402-closure-20261027.json) SHA-256 `ad9607f641c2ae38228435d0b4d06d3b2515d931f7fd162f573475a2063f4a76`；[v2r3 campaign](../../outputs/acceptance/phase4-p402-behavior-runs-20261026-v2r3.json) SHA-256 `b9fee50451432d7e3eb60d696713f464885ef3e5300f789cd1ea4ce66de5e2d1`；[evidence manifest](../../outputs/acceptance/phase4-p402-evidence-manifest-20261026-v2r3.json) SHA-256 `04fa977c7ea801edfa5e1a6248e2101f3f9b072c3dd4e7351928576e8161fbb4`。
- P4-05 目前只完成 read-only candidate preflight；尚未读取或配置账号/Gateway 凭据，没有指定或联系测试对象，也没有向真实平台发送消息。P4-FIELD 仍 `not_run`。实际 field runs 开始前，用户需明确测试账号、受控群或私聊接收对象及允许发送范围，并冻结 Gateway、Host 与插件的精确版本。随后按本节计划为每生态至少一个候选执行正例、账号/会话隔离、重复/重启、Host 崩溃、Gateway timeout 与 unknown 回执场景各 3 次 fresh runs；逐条关联 ingress event → Core receipt → Host trace → operation → Gateway receipt → 接收端可观察消息，并分别记录 API accepted 与真实观察结果。
- 2026-10-29 当前路由：P4-05/feat-030 为唯一 active feature；P4-04/feat-029 blocked 于上述签名/策略和精确事件归因缺口；P4-03 Linux unknown batch 的 39 项 retry budget 已耗尽、36 项仍 unknown。P4-06/feat-031 依赖 P4-03、P4-04、P4-05 全部 done，暂不可开始。

- P4-05 independent Reference Client preparation: `npm test` in `clients/reference` built the TypeScript client and passed 5/5 tests using Node `v24.22.1`, npm `12.0.2`, and package-lock SHA-256 `2a6bfd208047b9f18e05c19ab498aabec5f275d44d7b71a3fe9e3d76f6e73f70`. The saved scoped report is [JSON](../../outputs/acceptance/phase4-p405-reference-client-20261029.json), SHA-256 `66f1a22db1401bf4495882d5d20292aec9b57f36df596f6ebf83166c05c1a8c4`. These tests cover injected HTTP fetch, in-memory VirtualPlatform behavior, and local CLI stdio MCP lifecycle; they do not exercise a live Core HTTP daemon, customer Gateway, or real platform and do not change `P4-FIELD=not_run`.

### P4-05 authorized live outbound canary（2026-10-29）

- 用户随后明确授权使用已启动的 Stella/SnowLuma 测试账号，只能向 Stella `ALLOWED_GROUPS` 中 SnowLuma 已加入的群发送受控测试消息；没有提供或要求读取任何凭据。实际范围是 5 个配置群与 11 个已加入群的交集（5 个），只选第 1 个交集群，报告以 account/group fingerprint 代替原始 ID。Gateway 冻结为 SnowLuma OneBot HTTP `1.14.19-node` / protocol `v11`。这项授权不扩展到私聊或其他账号/群。
- 选择 `@koishijs/plugin-echo@2.2.5` 精确 Linux profile，通过独立 Core HTTP + Koishi Host Worker 执行单条 synthetic ingress echo，Gateway 只允许向上述精确群目标提交本次唯一 marker。Core 对三次 distinct runs 均接受并完成 ingress event，每个 Host run 产出且只产出一个群发送操作；这不是真实平台 ingress 验收。
- 三次实际到达 SnowLuma 的发送都返回 OneBot `retcode=100`。安装版日志的三个对应动作均为 send group message rejected: result=120 err=，原因文本为空。安装版 index.mjs 的发送分支只会在 sendRawPacket 成功、SendMessageResponse 解码成功且 response.result 非零时抛出该拒绝；该文件 SHA-256 fbb5793b5f8d64d5c68285b0ac89825dd3e9691927cb5f13dee47f1dc16027ca、字节偏移 6466719。因此这是收到非零 QQ 响应，不是传输超时或参数解析拒绝；具体业务原因仍未知。三次均使用不同 marker/event/operation；只读 `get_group_msg_history` 成功读取最近 50 条，三个 marker 的 literal match 均为 0。没有 API accepted 或接收端可观察消息，三个 Core operation 均保持 `unknown`，未重试任一 operation。此证据不能说明拒绝之后的底层投递状态，也不能归因于插件兼容性。
- 机器诊断 [JSON](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-live-platform-diagnostic-20261029.json)，SHA-256 `604372e7ed7102cb553592d442dbd1618caeeb4b0a2083a90b6f0d8ee9f1b973`；引用的两份尝试报告：[c32cfb00](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-koishi-live-canary-20261029-c32cfb00.json)，SHA-256 `1a3e3c6de1dafe343da808d8d4caf934510dc2fecfa100f0276e86811f2380b2`；[8b3f6c77](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-koishi-live-canary-20261029-8b3f6c77.json)，SHA-256 `20d2a571961e38a4e7358b954821f85c2f0348b1f7b88a2b66c205a72d8a124f`；[1c443d0c](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-koishi-live-canary-20261029-1c443d0c.json)，SHA-256 `21d77f7f331975e70a6551b4f869f1c14f525eb6d36b401dd5ab4d76ad84e685`。三次 Core daemon、带本次 run label 的 Worker container 与 SQLite volume 清理均确认。
- 本地复核报告 [JSON](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-koishi-local-verification-20261029.json)，SHA-256 `1176f9e8cf41fc9b60b7985bb4ab30d074ea0adb03d50dfeda84637783ea2187`：Koishi Host `npm test` build 成功、8/8 tests passed；隔离 Docker direct-host fixture dispatch 产生一条预期 `send_message`，由虚拟 `platformCall` 收到。该复核没有真实 Core HTTP/OneBot 写操作或真实投递。测试源 SHA-256 `8ad845b6ba3a31ef6e57a118022066759899c6bce90b358fb6c2b216b888063e`；direct probe SHA-256 `eeece3190f308523dd338e2caf141735c9d30d23eff9ecffd9db3f4e3c678ff6`。live canary driver 在安全错误码中记录平台 result code 的改动 SHA-256 `11476ff92fefe0cd8ecabc3e3c1ae6cb96bda726291c9524743a03e7c8aa75bc`。
- 本机安装版 SnowLuma 日志 snapshot SHA-256 `631640c3cc3c3adcbaeb9b037686840698ebef294d043f43517a7d6105b53684`；只记录了脱敏动作摘要和 hash，没有复制群记录或凭据。P4-05 可复用的本地证据仍限于 Reference Client、Host bridge 测试与隔离 Host synthetic fixture；本次 live result 为 `blocked_platform_send_rejected`，`P4-FIELD=blocked`，feat-030 继续 `in-progress`，不把 `unknown` 改写成通过或失败兼容结论。
- 追加的只读平台检查 [JSON](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-platform-self-permission-20261029.json)，SHA-256 `31448a02f7c55154628dc88bf40434767a6d210b7aaff8d5c2ae4892bb165a03`：`get_login_info`、`get_group_list`、`get_group_member_info` 均 `ok/retcode=0`；该账号在选定群的自身角色为 member，当前未禁言。未读取其他成员内容或历史；此检查排除当前禁言但不能解释 QQ `result=120`，不改变 unknown/blocked 裁决，也没有发消息。
- 当时下一步先查明 SnowLuma/QQNT 的 `result=120` 拒绝条件，并在平台可以确认发送结果后，用全新 run/event/marker 恢复受控场景；不得重放这三个 `unknown` operation。随后仍需按计划完成每生态正例、隔离、重复/重启、超时/unknown 场景各 3 次 fresh runs，以及 API accepted 与接收端观察分开的证据
### 2026-10-29 result=120 只读诊断结论

- 三次结果确认了 QQ `SendMessageResponse` 收到并解码后的非零 `result=120`，但 `errMsg` 为空。SnowLuma v1.14.19 的[群消息发送实现](https://github.com/SnowLuma/SnowLuma/blob/v1.14.19/packages/core/src/bridge/apis/message.ts#L403-L417)只检查 `result != 0` 并把数值与 `errMsg` 作为通用错误返回；[协议类型](https://github.com/SnowLuma/SnowLuma/blob/v1.14.19/packages/proto-defs/src/action.ts#L77-L86)定义 `result` 为 `int32`，没有错误码枚举。对该仓库 literal `result=120` 的代码/issue 搜索未找到映射；这只是该版本源码与仓库搜索范围内的结论。
- 已排除这三次中的传输/响应解码失败；只读检查当时显示账号仍是群成员且未禁言；三个操作都匹配各自精确群授权。QQ 侧账号/群/内容/频控限制仍是候选解释，但现有证据不能区分它们，也不能证明 QQ wire target 与意图目标一致。
- 机器报告 [result=120 只读诊断](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-result120-diagnosis-20261029.json)，SHA-256 `8d6aa7b278bb9e782bb35e46cd7a2689ab093ad93f6ca4f7f394c176a275d98c`。结论仍为 root cause unknown：P4-FIELD=`blocked`，三个 Core operation 保持 `unknown`，不重放、不新增现场发送，直到获得可解释的 QQ 原因或可信错误码映射。

### 2026-10-29 P4-05 恢复后的只读上游复查

- 用户要求恢复 P4-05。重新核对后，保留三个 live canary 的 unknown 状态和不重放约束；本次只做公开来源与既有脱敏证据复查，没有读取凭据、访问新聊天内容或发送消息。
- 第三方项目 [dsh-astrbot-ingress design notes](https://github.com/CCYellowStar2/dsh-astrbot-ingress/blob/1cb088103c64d033ab91cc87da5d06e7e6be4904/docs/design-notes.md) 报告其环境中 `result=120` + 空 `errMsg` 是偶发 QQNT 群发送拒绝，约占其群发送的 1.7%，且后续发送通常成功。来源 commit `1cb088103c64d033ab91cc87da5d06e7e6be4904` 发布于 2026-09-13；该记录未给出样本量、SnowLuma/QQ 精确版本或原始 TRACE，只能作为低置信度的“平台瞬时拒绝”线索；它不是 QQ/SnowLuma 错误码定义，也不能解释本次连续三次拒绝。
- SnowLuma v2.2.1 的[当前发送实现](https://github.com/SnowLuma/SnowLuma/blob/v2.2.1/packages/core/src/bridge/apis/message.ts#L2928-L2935)仍对任何非零 `SendMessageResponse.result` 返回通用 `result`/`errMsg` 错误，未增加 `120` 映射。v1.14.21 引入的[回执变更](https://github.com/SnowLuma/SnowLuma/commit/2c7d9b63)只处理 `result=0` 但没有正数 `groupSequence` 的“未确认投递”，并明确避免自动重发以免重复；它与 `result=120` 分支不同。因此不升级或重配冻结的 v1.14.19-node canary 环境，也不据此扩大结论。
- 机器复查报告 [JSON](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-result120-upstream-review-20261029.json)，SHA-256 `05785007e3593e66b2693cdc1b16762fbe47b22cf5174739d5ac3bdb3f40488b`。平台瞬时拒绝假设上调为待验证线索，账号/群策略、目标映射、内容策略和频控仍未充分排除；没有出现可解释 `120` 的 QQ 原因或可信码表。三个旧 operation 不重放；任何后续现场测试都必须使用全新输入和用户指定的 Stella 进程路径，不能由 Ligamina 直接调用 SnowLuma。

### 2026-10-29 P4-05 Stella process path correction

- 用户明确要求现场出站由现有 Stella 进程执行，不直接调用 SnowLuma。当前只读 `GET /stella/status` 报告 Stella 6.3.0、进程 PID 15352、OneBot connected/healthy、允许群数 5；不记录任何账号或群标识。`deploy status --json` 却报告 desired=stopped/alive=false，Stella runtime 子状态报告的 PID 35028 当前不存在；因此不把 endpoint/link 健康误作 deploy-manager managed readiness。完整摘要：[Stella-path preflight JSON](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-path-preflight-20261029.json)，SHA-256 `b1056a5788fd7cace23ce628f8e4cc1a9031d3521e86153c6d38d6c1a0d55609`。
- 源码核验确认正常 QQ 回复需由群 ingress 进入 Stella `ai_gateway` turn pipeline，再由现有 Bot send path 投递；WebUI `/api/v1/chat` 固定进入 WebChat 虚拟群 `-1`，WebUI 没有 QQ 测试发送 API。此前三个 Ligamina synthetic-ingress → direct-SnowLuma 结果保留为三个 unknown operation，不能算作 Stella process 端到端证据；本次 preflight 未发送消息、未重启进程、未读取凭据或聊天内容。
- `feat-030` 恢复为 active/in-progress，`active_feature=feat-030`；`P4-FIELD` 仍 blocked。要通过现有 Stella 进程产生一条全新 QQ 回复，需要非 Stella 群成员先在任一已配置测试群触发唯一 @ 入站消息；之后按新鲜 trace 检查 `turn.generate`、`send.prepare`、`send.segment`，并把接收端可观察证据与 API accepted 分开记账。不得重放三个旧 operation 或直连 SnowLuma。

### 2026-10-29 P4-05 Stella live ingress observation

- 只读查询现有 Stella turn_trace.db 的时间索引，范围从 preflight 后的 2026-10-29T01:56:42Z 到核查时刻。观察到 40 条 qq_passive/passive_only trace、3 条 proactive_timer/closed trace，范围内 delivered=0。最新 trace 于 01:59:39.660Z 收口：ingress.receive 与被动过滤成功、ingress.passive.persist 因 capture_closed 跳过、插件入口完成；没有 chat.runtime / 生成或 send.segment 证据。
- 不读取消息正文、账号/群标识或凭据，也未访问接收端历史；因此不能把 trace 唯一关联到用户刚发送的某条消息，也没有接收端已见回复的证据。正常群回复规则 is_chat_trigger 要求启用群、@ Stella 与非空文本（识图可用时可带图片）；被动 ingress 单独进入不构成回复验收。
- P4-FIELD 继续 blocked，feat-030 继续 in-progress。下一次使用新鲜唯一 @ 文本触发，再核对生成与发送阶段并单独取得接收端观察。机器报告：[Stella ingress observation](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-ingress-observation-20261029.json)，SHA-256 449cc04ec56bc62e971e31ae96eb2cec2913baf588c01112ecb826118bb08c07。

### 2026-10-29 P4-05 Stella outbound logger recheck

- 针对用户关于日志保存逻辑更新的说明，复核当前 Stella 源码、活动/轮转日志与运行 trace。当前工作树 HEAD `cf63061` 包含 outbound logger commit `838819c`；该 logger 记录状态、adapter、scene、segment kinds/counts、receipt count、分类错误与耗时，不记录消息正文。活动与轮转 `.jsonl`/`.log` 中 `[OutboundDelivery]` 命中数为 0。
- 从当前进程启动 `2026-10-29T07:55:21.151Z` 到 `10:33:24Z`，索引查询得到 1472 条 qq_passive/passive_only/closed、4 条 plugin_handled、158 条 proactive timer closed、3 条 lifecycle closed，另有 1 条 qq_passive running；chat.runtime/send.prepare/send.segment/send.aggregate flow event 数均为 0。最新 trace 于 `10:33:24.020Z` 收口为 passive_only，`ingress.passive.persist` 因 `capture_closed` 跳过。未读正文/账号或群标识，不能把任一 trace 唯一关联到用户消息。
- `feat-030` 保持 in-progress，`P4-FIELD=blocked`。后续需在启用测试群产生新的唯一 @Stella 文本，之后检查正常生成与发送路径并单独确认接收端观察；不直连 SnowLuma，不重放旧 unknown operation。报告：[outbound logger recheck](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-outbound-log-check-rerun-20261029.json)，SHA-256 `a12e1f7be12d09beec2990c2bc272e8d61a7d5aaa32431be99d6c78929bc39c2`。

- 记录验证：`pwsh -NoProfile -File init.ps1 -Mode docs` passed（20 docs / 546 links / 15 history / 31 features / 0 failures）；`git diff --check` passed。

### 2026-10-29 P4-05 Stella native group reply observed

- 用户报告在真实群聊发送“晚上好”并 @Stella，收到预期回复。日志确认 trigger allowed；匹配的 `qq_passive` trace `chat.runtime` 在 10:56:33.379Z 成功，`send.segment` 于 10:56:34.524Z 成功，最终 `outcome=delivered`、trace closed/complete。`[OutboundDelivery]` 记录 `status=accepted`、OneBot v11、group、1 accepted part、1 receipt、0 failed/unknown parts。此为一次 Stella 原生路径正例，接收观察来自用户报告，未收集自动化接收端记录。
- 时间窗内另有第二条独立 @ 触发 trace，source key 与“晚上好” trace 不同，10:56:57.098Z 也有 accepted 出站；没有读取第二条正文或确认其接收，因此不把它算作第一条的重复，也不计为用户确认的正例。
- 来源 `ai_gateway.py` 的普通群处理直接调用 `_run_turn_via_engine`，然后进入 `core.transport.outbound`；两条 trace 均无 Ligamina Client/Gateway/Host/operation nodes。Outbound logger 不写消息正文。故本次确认 Stella 自身群聊回复可发送，但未通过计划要求的 Ligamina Client/Gateway 真实链路，且没有 event ID → Core receipt → Host trace → operation ID → Gateway receipt → recipient observation 关联；其余隔离/重复重启/timeout/unknown 场景的三次 fresh runs 也未完成。旧三个 direct-SnowLuma `result=120` operation 继续 unknown。
- 裁决：`feat-030=in-progress`、`P4-FIELD=blocked`。需先让 Ligamina-mediated Client/Gateway 流程使用 Stella 作为真实 OneBot sender；若当前没有该 adapter/route，这是眼下的实现前置缺口。报告：[Stella native reply observation](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-native-reply-20261029.json)，SHA-256 `35412ba3925ac57ae94b23bfd9003bedd7df5f8035a2979063dc7e58a47bf1bd`。

### 2026-10-29 P4-05 Stella/Ligamina adapter source diagnosis

- 源码盘点确认了前一节的条件缺口：Ligamina 的 Reference Client/Core 已提供 event submit 与 Gateway lease/claim/result HTTP 合同，但没有 Stella adapter；Stella 当前 Python runtime/extensions/core 也没有 Ligamina 引用。Stella 的 `extensions/__init__.py` 自动加载 `setup(pipeline)` 扩展，目前仅有 `extensions/link_monitor`。正常群 `handle_chat` 运行 `_run_turn_via_engine` 后直接调用 Stella `core.transport.outbound` 的 `OutboundService.send`，中间没有 Ligamina Client、Gateway claim/result 或 operation ledger 节点。
- 因此 native 回复成功说明 Stella 现有 OneBot sender 可用；P4-05 仍缺把真实 normal ingress 与 Ligamina Client/Gateway 关联、并让 Gateway action 通过同一 Stella 进程发送的 connector。接入必须阻止 native send 与 Gateway send 双发，并关联 event、Host trace、operation、Gateway receipt 和接收观察。GitNexus 1.6.12 索引已刷新到 Stella `cf63061`，但 process surface 有 entry-point/walk 截断；“缺 adapter”判断基于目标源码目录盘点与调用点核查，不以缺失 graph flow 作为证明。
- 本轮没有运行时修改、重启、消息发送、旧 operation replay、凭据或聊天内容读取。`feat-030=in-progress`、`P4-FIELD=blocked`，三个旧 direct-SnowLuma operation 继续 unknown。机器报告：[Stella/Ligamina adapter readiness](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-ligamina-adapter-readiness-20261029.json)，SHA-256 `629074a3c5321777cc17aa4ca275661ed7465b1a8fd07e802be78d8da53b3c33`。

- 记录验证：`pwsh -NoProfile -File init.ps1 -Mode docs` passed（20 docs / 548 links / 15 history / 31 features / 0 failures）；`git diff --check` passed。

### P4-04 self-signed Core guest-trust follow-up（2026-10-29）

- 用户确认已停止旧的 `vmwp.exe` PID 48444；本次启动前该 PID 已不存在，`wsb list --raw` 为空。于断网 disposable Sandbox 新跑 `20261029-v8-guest-trust`，仍使用只读挂载的 release Core `target/release/ligamina-cli.exe`（SHA-256 `6c6d0a390916df63ab27080fd85a9e880785854cec63c43f1ac7a0b120dbdd28`、NotSigned），guest 生成不可导出的 RSA-2048/SHA-256 自签名证书，只加入 guest `CurrentUser\Root` 与 `CurrentUser\TrustedPublisher`。
- SignTool sign 与 `/pa` verify 均 exit 0；签名副本 SHA-256 `593914a2e2b59589f53b39164419a1d1fe5595ef293a887f78643ee27a702b09`。Core `--version` 未能启动，Win32 4551 / Application Control policy blocked。基线含 20 条 Event 3099，record 1039 再次标识 `VerifiedAndReputableDesktop` / ID `27555.1000.240208` / GUID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}` / Options `0xb18c2714` / hash `2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01`；本次启动后没有 3033/3034/3076/3077/3089/3099，目标事件与 Activity ID 仍未关联。DeviceGuard kernel/user status=2/2，CiTool 返回 HRESULT `0x80073BC3`。
- cleanup 结果如实记为未确认：guest `CurrentUser\My` 与 `TrustedPublisher` 的证书删除已确认；从 guest `CurrentUser\Root` 删除时 API 返回“操作已取消”，所以逐 store cleanup=false。Sandbox 随后 teardown confirmed、活动 guest=0；主机六个 CurrentUser/LocalMachine My/Root/TrustedPublisher store 均找不到该 thumbprint，host policy 未变，源 Core 未改。没有运行 catalog plugin、Host 或行为用例。机器 review [v8](../../outputs/acceptance/phase4-p404-selfsigned-core-20261029-v8-guest-trust/selfsigned-core-review.json) SHA-256 `276b55c334540f3450554284ed702d58521c86b63429bd715d60225b45e8ed87`；report SHA-256 `479e8cf42985b72fe2fdab71aff3d2dd2f6558fb4a39578c2c25271deaf17bcb`。probe package README 记录命令、结果与逐 store cleanup 限制。
- 本轮本地复核记录 [follow-up verification](../../outputs/acceptance/phase4-p404-selfsigned-core-followup-verification-20261029.json)，SHA-256 `4233b763b8ab23d59236e3d86f1b5eb095d5dd070c11e6022e814e2104c662fa`：docs 20/519、evidence-hash 4/4、README check、PowerShell AST 2/2、v8 sidecars 5/5、host thumbprint 0/6 stores、active Sandbox=0、`git diff --check` 通过。该报告只覆盖文档/本地证据完整性，不改变 Enforcement 裁决。
- 结论：guest 本地信任解决了 Authenticode 链验证，但没有使当前 Enforcement 接受 Core。该 v8 探针完成时 feat-029 仍为 in-progress；用户随后要求先跳过签名证书缺口，当前 P4-04/feat-029 与 P4-WINDOWS-BEHAVIOR 均 blocked，catalog admission=`not_evaluated`、`catalog_plugin_claims_allowed=false`。继续需要当前策略接受的签名身份或另行审阅/版本化的 Sandbox-only test policy，并取得精确文件事件归因；不能用本地 Root/TrustedPublisher 信任替代这些条件。

### P4-04 自签名证书可行性实验（2026-10-28）

- 按用户要求验证能否在本机测试环境自行签发证书。只读输入绑定到 `pydantic_core-2.50.1-cp312-cp312-win_amd64.whl`（SHA-256 `132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498`）及其 `_pydantic_core.cp312-win_amd64.pyd` member（SHA-256 `d38e098d798d381cc44f867162bc6da2ebc5d77bf444f0dd00d42a1bc8484297`）；使用独立的 CPython 3.12.10 signed runtime profile。
- 在断网 disposable Windows Sandbox 中创建不可导出的 RSA-2048/SHA-256 Code Signing 自签名证书（thumbprint `3521DF91D8C1FCC95B5A02E299039D6A87C79831`），仅把公钥临时加入 guest 当前用户 `Root` 与 `TrustedPublisher`，并用 Microsoft SignTool 签署独立副本。`Get-AuthenticodeSignature=Valid`、`signtool verify` exit 0；guest load probe 仍返回 Win32 4551，`LoadLibrary` 未成功。签名副本 SHA-256 `bbb78eb03d6d720709829bbe5e5c18b96cd68d73967aa3fde5b74e03d410f7e2`，不属于锁定 wheel/profile/catalog artifact。
- guest DeviceGuard kernel/user status 均为 2；事件 3099 记录 `VerifiedAndReputableDesktop`、Options `0xb18c2714` 与 hash `2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01`。本轮未捕获与目标文件匹配的事件 3077/3089；虽然错误明确报告 Application Control 拒绝，活动策略激活记录也在同一 guest 中，但文件/hash/activity 事件级归因未闭合。CiTool inventory 仍为 HRESULT `0x80073BC3`。机器审阅结果为 `blocked_by_enforcement_win32_4551_event_attribution_incomplete`。
- 实验后确认 guest 临时证书信任与 scratch 已清理、Sandbox 已停止，host certificate stores 和 App Control policy 未改，源 wheel/member 未变；未安装或运行 catalog plugin，也未执行行为用例。审阅报告 [self-signed signature review](../../outputs/acceptance/phase4-p404-selfsigned-code-signing-20261028/selfsigned-signature-review.json) SHA-256 `7b3be69a418a8adb3ec289f6a028371faff51e379df3f8e82c6ee2cba0da26ae`；复现步骤与本次结果见 [probe package](../../deploy/windows/phase4-p404-selfsigned-signature-probe/README.md)；本轮文档、语法与哈希校验见 [follow-up verification](../../outputs/acceptance/phase4-p404-selfsigned-code-signing-20261028/selfsigned-followup-verification.json)，SHA-256 `7179c1a766bf2d28c3f49f043157d6cf76422920888f6b8769b10616a01d1755`，仅为本地完整性验证。
- 结论：本机测试中技术上可以自签、自行验证签名；把该证书加入 guest 本地 trust store 仍不能满足当前 Enforcement。恢复原 gate 需要当前基线认可的签名身份，或经过独立审阅、明确版本化且允许该测试的 policy；并补齐该精确二进制对应的 Code Integrity 事件归因。自签名 derivative 不能替换原 artifact 或计为兼容通过。

### P4-04 self-signed Core runtime-only probe（2026-10-28）

- 在断网 disposable Sandbox 中为本地 release Core 的独立副本生成不可导出 RSA-2048/SHA-256 自签名证书。原始 target/release/ligamina-cli.exe SHA-256 6c6d0a390916df63ab27080fd85a9e880785854cec63c43f1ac7a0b120dbdd28 未改变；GitHub CLI 返回 0 个 releases，debug/release Core 均 NotSigned，机器快照见 [Core release-signing preflight](../../outputs/acceptance/phase4-p404-core-release-signing-preflight-20261028.json)。
- run 20261028-v6 首次稳定复现 SignTool sign exit 0、verify exit 1（guest trust store 未更改时自签名根不受信任），Core `--version` 启动返回 Win32 4551。v6 未查询 3033/3034，也未保留本次启动前的 3099 基线快照；其完整记录保存在 probe package README 中。
- 最新 run 20261028-v7 增加 3033/3034 查询并保存 20 条基线 Event 3099。record 1039 明确记录 `VerifiedAndReputableDesktop` / ID `27555.1000.240208` / GUID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}` / Options `0xb18c2714` / hash `2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01`。SignTool signing exit 0，签名副本 SHA-256 `029cbcb31a51a81bbf6dfa48f653ff6fedcc2ca943af6ec816bcd50d6f7ee541`；verify exit 1，Core 启动仍被 App Control 拦截，Win32 4551。启动后没有新的 3033/3034/3076/3077/3089/3099 或 Activity ID，因此目标文件与拦截事件的精确关联仍未完成。机器审阅 [v7 Core review](../../outputs/acceptance/phase4-p404-selfsigned-core-20261028-v7/selfsigned-core-review.json)，SHA-256 `651b7101ad006704b1bf6d138bbf9fd1ddce7ac0498f9918c0e211233c936830`。
- DeviceGuard kernel/user status 均为 2；CiTool -lp -json 返回 HRESULT 0x80073BC3。guest 当前用户 My store 中测试证书清理确认；未改 Root/TrustedPublisher、host trust 或 App Control policy；Sandbox teardown confirmed，活动 guest 数为 0。未运行 catalog plugin、Host 或行为 case。
- 结论：本机自签名只证明可以为实验副本生成签名，不会使证书自动被当前 Enforcement 接受。该 runtime-only probe 不满足 P4-04 admission/behavior gate；继续执行需要当前基线接受的签名身份，或经管理员审阅并明确版本化、仅用于 disposable Sandbox 的 test policy，并补齐精确事件归因。

### P4-04 初始 guest admission 与 native probe（2026-10-28 历史检查点）

- 官方 Windows Sandbox CLI 管理本任务会话。v1 collector 的 CiTool capture 失败；v2 以 .NET ProcessStartInfo 保存 command path、stdout/stderr、exit code。Sandbox 禁用网络、vGPU、剪贴板、打印机、麦克风和摄像头重定向；probe 包只读挂载，run-scoped evidence folder 可写。
- Guest 为 Windows 11 Enterprise 26100 / WDAGUtilityAccount；Win32_DeviceGuard kernel/user Code Integrity 均为 2。20 条 policy-loaded 3099 事件覆盖 8 个 GUID：5 个 Options bit16 clear（event-derived Enforcement）、3 个 Audit；activation status 均为 0x0。
- CiTool -lp -json 返回 exit -2147009597 / HRESULT 0x80073BC3，stdout 为 {"OperationResult":-2147009597}、stderr 为空；完整 active policy inventory 与 IsEnforced 未核验。
- 自有 unsigned x64 PE（SHA-256 2b55b31fdb72609e7533a98ca7b5cb492305f5f5629b247282437f55d02772c5）在程序入口前被 Application Control 拒绝；无 marker、无子进程。Event 3077 与两条 3089 以 Activity ID 4d54d80c-6488-0000-b52a-554d8864dd01 关联；3077 SHA256 Flat Hash 与 PE hash 相同，policy=VerifiedAndReputableDesktop / ID 27555.1000.240208 / GUID {0283ac0f-fff1-49ae-ada1-8a933130cad6} / status 0xc0e90002。3099 Options=0xb18c2714，Audit bit16 clear。无 policy mutation，无目录插件执行。
- 复现材料见 [native probe package](../../outputs/acceptance/phase4-p404-native-probe-20261028/README.md)、[guest receipt](../../outputs/acceptance/phase4-p404-guest-admission-20261028-v2/native-probe-report.json)、[event correlation](../../outputs/acceptance/phase4-p404-guest-admission-20261028-v2/native-probe-event-correlation.json) 和 [machine review](../../outputs/acceptance/phase4-p404-native-probe-review-20261028.json)，review SHA-256 68a3eb18677801934a2e009a3cb930963581d17bb0dc90a8ad079a44972df2fd。管理员包不含策略、证书、私钥或安装脚本。
- 活动 guest ID 50591195-1109-4883-abef-c73a40345439 已由 wsb stop 关闭，wsb list 随后返回 0 个活动 session；本任务的 guest client/worker 已退出。余下 vmwp PID 48444 未能通过 Sandbox CLI 或注册 Hyper-V VM 归属到本任务，未终止。
- 下一步运行准入：管理员审阅仅限 disposable Sandbox 的窄范围 signed policy 或受信二进制方案、exact hash 与 rollback；宿主策略保持不变。重建 guest 后重测同一探针并补齐 CiTool inventory。native probe 单项通过也不等于 catalog admission；provider/config/combined lock、execution/network/resource/process-tree cleanup 仍需要独立证据。
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
| P4-04 / feat-029：Windows Sandbox Enforcement 准入与行为验收 | P4-01；复用 P3-04 | guest policy/readiness 证据、签名 runtime 与 native probe、锁定候选 pilot、逐步 Windows 行为闭环 | 原 Enforcement scope 的准入、profile/依赖闭包、pilot 与必选行为项均有可复核证据；没有证据时维持 blocked/unknown，不改写成 runner-only 完成 | 1–2 + 准入等待 |
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

## P4-04：Windows Sandbox Enforcement 准入与行为验收（2026-10-28 恢复原 scope）

2026-10-28 用户恢复原计划范围。此前暂时收窄并完成的 `WindowsSandboxWorker` / guest runner 七项 benign contract probes 保留为独立已完成子任务，不再作为 P4-04 overall done。原目标继续要求 disposable guest 中的 Enforcement 准入、签名 runtime/native probe、锁定候选 pilot 和 Windows 行为验收。正式 admission/pilot 仍沿用既有 policy，不安装信任证书、不添加信任规则、不更改 App Control policy；用户另行要求的自签名可行性探针只在断网 disposable guest 内对独立副本临时加入当前用户 Root/TrustedPublisher 信任并签名，结束后清理，不构成正式 gate 准入，也不放宽执行控制。无有效签名身份时，只能执行该类隔离诊断；行为 pilot 仍保持 blocker。

恢复后的顺序：确认 disposable guest 的 current-boot policy identity/options 与 Code Integrity 事件 → 在不改策略的条件下运行 exact signed native runtime probe → 闭合候选 artifact/Host/dependency lock 与本地归档 → 仅在 Enforcement baseline 可复核后开展目录安装/加载 pilot → 每生态 1 项、每生态 2 项，再扩展至完整二十项矩阵。Audit/Evaluation 与 Enforcement 证据分开；runtime 可启动不等于 plugin admission，安装成功不等于行为通过。

### 2026-10-28 恢复后的只读准入与 signed-runtime 检查点

- 新鲜 Windows 11 Enterprise 26100 Sandbox 的 Win32_DeviceGuard kernel/user 状态均为 2；当前启动的 Event 3099 activation 被采集。`CiTool -lp -json` 在 WDAGUtilityAccount 与 SYSTEM 下均返回 HRESULT `0x80073BC3`，所以完整 inventory 与 exact base-policy mapping 仍未知；没有执行插件或改策略。
- Koishi `@koishijs/plugin-echo@2.2.5` artifact 与 profile lock SHA 匹配；172/172 锁定依赖归档的 SHA-512 匹配，静态归档检查未发现 `.node/.dll/.exe/.so/.dylib` / `binding.gyp` payload 或 install scripts。Node 24.20.0 `node.exe` Authenticode 为 Valid，签名者 OpenJS Foundation，SHA-256 `5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5`。断网 Sandbox 中 `node --version` 和 built-in-only JS probe 均 exit 0，marker 创建/读回/删除确认；结论仅为 `passed_runtime_only`，无 post-start 3076/3077/3089/3099，catalog execution 未开始。
- NoneBot `nonebot_plugin_roll@0.2.3` artifact 与 36-dependency lock 的缓存哈希均匹配；当前锁定 CPython 3.12.14 `python.exe` Authenticode=`NotSigned`，既往 Enforcement 结果为 Win32 error 4551。Python.org 说明 3.12.14 为 source-only release，Windows binary installers 已停止提供。[官方 3.12.14 release](https://www.python.org/downloads/release/python-31214/)；3.12.10 仍提供 embeddable package，但只能作为独立 runtime/profile/lock 身份，旧 P3-04 结果只准许 runtime claims，不准许 catalog plugin claims。[官方 3.12.10 release](https://www.python.org/downloads/release/python-31210/)。
- 机器报告：[policy preflight](../../outputs/acceptance/phase4-p404-policy-preflight-20261028.json)；[Node runtime guest report](../../outputs/acceptance/phase4-p404-signed-runtime-20261028/signed-runtime-probe.json)；[host/runtime review](../../outputs/acceptance/phase4-p404-signed-runtime-review-20261028.json)。所有 `.sha256` sidecar 已核对。guest `a06a3cea-01a4-4ffa-8688-cb06c7380a31` 已 `wsb stop`，后置 `wsb list --raw` 为 `[]`。
- 该只读检查点当时 gate=`blocked_preflight`，admission=`not_evaluated`，claims=false；当时下一步是补齐 policy inventory/base mapping，再开展 Koishi pilot。更新后的 scoped pilot 结果与剩余阻塞见下方“2026-10-28 scoped Enforcement pilots 与最新阻塞”。

### 已完成的 runner-only 子任务（2026-10-28，历史 scope amendment）

此前按用户临时指示只验证 `WindowsSandboxWorker` 与 guest `runner.ps1` 的七项 benign contract probes。该子任务七项 expected result 和 cleanup 均通过，报告 `runner_behavior_status=passed_scoped`。它保留为 runner behavior evidence，不替代此处恢复的 Enforcement 准入、目录插件 pilot 或 `P4-WINDOWS-BEHAVIOR` gate。

### runner-only 子任务结果（2026-10-28）

运行入口为 `scripts/phase4-p404-runner-behavior.py`，报告为 [runner behavior JSON](../../outputs/acceptance/phase4-p404-runner-behavior-20261028.json)，SHA-256 `c0652d11bd8a763478cdd2151275c9d930f96198c3cf70664c73a87339d74f45`；源码 hash、运行时间与宿主平台绑定在报告内。七项均 expected/pass：边界 probe、进程树 cleanup、非零退出 (17)、失败后 sibling、`unknown/worker_timeout`、`unknown/worker_output_truncated`、`unknown/worker_lost`。Worker reports 和外层 Sandbox 清理全为 confirmed，最终 `runner_behavior_status=passed_scoped`。受控 worker loss 的 guest 停止后诊断 `0x80370105` 与预期 `worker_lost` 相符。所有结果只验证 runner 行为；catalog plugins executed=false，App Control policy changed=false，catalog admission=`not_evaluated`、claims=false。

复现入口（PowerShell）：`$runId = Get-Date -Format 'yyyyMMddTHHmmss'`，将 `$env:UV_PROJECT_ENVIRONMENT` 设为 `.work/phase4-p404-runner-behavior-$runId`，再运行 `uv run --locked --project lab --python 3.12.14 python scripts/phase4-p404-runner-behavior.py --output outputs/acceptance/phase4-p404-runner-behavior-$runId.json`。每次使用新环境与新输出路径；不得复用 `lab/.venv` 或覆盖既有报告。报告与 `.sha256` sidecar 保留在 `outputs/acceptance/`，摘要 README 加入 [证据索引](../evidence/README.md)。

### 2026-10-28 scoped Enforcement pilots 与最新阻塞

- 新鲜 Windows 11 Enterprise 26100 guest 的 Event 3099 记录了 base `VerifiedAndReputableDesktop`（GUID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}`、Options `0xb18c2714`、hash `2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01`）及其 supplemental policy。自有 unsigned PE Event 3077 和后续 NoneBot `_pydantic_core` Event 3077 均携带相同 base GUID/hash；这足以关联 scoped Enforcement baseline，但 CiTool inventory 仍为 HRESULT `0x80073BC3`，完整活动策略集合/base-policy composition 未知。原策略未修改。
- Koishi `@koishijs/plugin-echo@2.2.5` 保持原 artifact SHA `79da39692ba8fce04ea597ff85696a0fe193cd89a95a91a0d459ea40ab592265`、原 plugin profile lock SHA `33e0897b794134c39215e7824a47edec3eb4acc79fcb9f4df00a03c1455f616b`；172/172 依赖归档哈希通过，host provider 以独立 combined lock 绑定。签名 Node 24.20.0 OpenJS runtime 先完成 `passed_runtime_only`，后续 dependency install 与原生 plugin register/load 均通过。一个 `ko-koishijs-plugin-echo-isolation` Native Host case 通过 3 次独立 Sandbox 运行；每次 9/9 Oracle assertions、2 个 group/private scoped operations、无 Bridge adapter 导入、worker cleanup confirmed。run3/4/5 hashes 分别为 `12388dd41b3a511f08c527fef2dca6b11b2baada01dcbe533f1c4a4aacd2fb7`、`c8a86c5e572ba2ae4e7852052e84a3471fe817ab5c8a4d5cfd3e5be523a6c74f`、`c42274b61eb9fd2d554666a2485e03b49da2dc3c66053ef915dbe2c85cdbcd50`。run1/2 因 cwd/provider harness setup 未进入插件行为，分别保持 `unknown`，不计作行为失败或通过。
- 2026-10-28 Native case 扩展：runner 现以 `--case-id` 选择冻结 allowlist 中的 positive、negative 或 isolation fixture；本轮新增的 positive/negative reports 绑定 driver hash，早期 isolation run3/4/5 保持原报告不变。`ko-koishijs-plugin-echo-positive` 3/3 fresh Sandbox 运行 `passed_native_scoped`，每次 6/6 Oracle assertions、当前 base policy activation 匹配、cleanup confirmed；SHA-256：[run1](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-positive-run1.json) `46d8ee46e44106d740e6088c8327cc51c1194fa87675c39d3b69c76f3e932f92`、[run2](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-positive-run2.json) `24d32d469a00c895736757dcf37d2d83f3c8affe2390732ae9982d0b6bacaccb`、[run3](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-positive-run3.json) `2f57a8c71762a137210a8de26476b68de6eeb2f5caa99fa8fbc76f69b0d9d287`。
- `ko-koishijs-plugin-echo-negative` 3/3 successful fresh Sandbox runs also passed with 4/4 assertions, current base policy activation matched, and cleanup confirmed: [retry1](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-negative-retry1.json) SHA-256 `e1653b461e66c07983f28c016fd1e2cb88b81bc9dd2efc778bb068271a35d1e7`; [run2](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-negative-run2.json) `cdc10883168445181ca263cc0f101bffe5b05e2aa13ddf6f5a2750e97ee27f6b`; [run3](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-negative-run3.json) `af494912c4e21178463d3d494201f57ff30428bd219e1947ecd2ae03ca4e953b`. The first dispatch attempt remains `unknown/worker_backend_unavailable` (SHA-256 `7398d123cae278e1867bc3332d8c50794661b615e70b6e898809e035df36876c`): WSB returned a broken gRPC/HTTP2 pipe before guest output or policy events; `wsb list --raw` was empty. The failure was preserved, then three separate retries completed successfully. All 12 isolation/positive/negative attempt report sidecars match and `wsb list --raw` shows zero active Sandboxes. These scoped Native results do not provide Federated parity; the NoneBot native dependency and signed Core admission blockers remain.
- NoneBot `nonebot_plugin_roll@0.2.3` 使用单独生成并通过 `lab.profiles verify` 的 Windows CPython 3.12.10 profile lock（SHA-256 `b317689e8d4bebd89b7fe99d907f64e2eea7415ff645fa64fb57047b6ed1f346`）；签名 runtime 本身 Valid，36/36 锁定依赖归档安装通过。Event 3077 record 1067 以 Activity ID `4d54d80c-6488-0002-231b-554d8864dd01` 关联 3033/3089，拦截文件为 `pydantic_core/_pydantic_core.cp312-win_amd64.pyd`，status `0xc0e90002`，policy GUID/hash 与当前 base policy 相同。事件 flat hash `D38E098D798D381CC44F867162BC6DA2EBC5D77BF444F0DD00D42A1BC8484297` 与冻结 wheel `132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498` 内该成员的 SHA-256 一致。依赖安装/worker cleanup 通过，但 native load/行为仍 `unknown`，这是 App Control blocker，不记作插件兼容失败。另一 fresh retry 仍复现 DLL load 错误但没有新匹配事件，也保持 unknown。
- Windows Sandbox Worker 本地 P4-04 NoneBot pilot 现会只读采集 Code Integrity 3004/3033/3034/3036/3064/3065/3076/3077/3089/3099。重跑入口：`$env:UV_PROJECT_ENVIRONMENT=(Join-Path (Get-Location) '.work\phase4-p404-nonebot-load-venv'); uv run --locked --project lab --python 3.12.14 python scripts/phase4-p404-nonebot-load-pilot.py --output outputs/acceptance/phase4-p404-nonebot-load-<new-id>.json --cache-root E:\stella\Ligamina-artifact-cache`。入口拒绝覆盖报告/sidecar，报告绑定自身、`lab/pilot_nonebot.py` 与 alternate profile lock hashes。
- 本机 `target/debug/ligamina-cli.exe` 与 `target/release/ligamina-cli.exe` 均 `NotSigned`；GitHub releases 当前没有可用 signed binary。无当前基线接受的 signed Core 时不得启动 Federated Windows probe。正式行为 pilot 不使用自签名替代发行签名，也不添加信任例外或关闭 Enforcement；前述用户授权的 guest-only 自签名副本仅为可行性诊断，不能计入 admission 或行为通过。此 signing preflight snapshot 当时 P4-04 overall 为 in-progress；用户随后要求跳过证书缺口，当前 P4-04/feat-029 与 P4-WINDOWS-BEHAVIOR 均 blocked，catalog admission=`not_evaluated`、claims=false。

### Windows Enforcement / plugin admission 验收要求（当前恢复的原计划）

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

gate completion 与工程 feature done 分开：P4-02/03/06 的审计可以明确交付失败/阻塞裁决；P4-01 首批运行验收与 P4-04 runner-only 子任务分别有 scoped 证据；恢复原范围后的 `feat-029` 仍须完成 Enforcement admission 与插件行为矩阵。`P4-WINDOWS-BEHAVIOR` 不因 runner 或 signed-runtime probe 通过而改变。P4-05 field 运行验收仍是硬性 gate，缺失时不能仅靠归因报告标 done。P4-06 若外部 gate 仍阻塞，只交付审计子项，不宣称候选发行验收全部完成。

- 另有三份早期准备报告不计入三次 outbound canary：两次记录 Core synthetic ingress accepted，但没有 operation/platform_send 状态；一份 Koishi SQLite driver setup 因 `@minatojs/driver-sqlite` 缺失未达到 Core ready。三份均保留 sidecar。第一份的历史 `daemon_stopped=false` 原样保留；本次只读重核未发现 Windows/WSL Ligamina CLI、P4-05 Worker container 或命名 volume 残留，但不把历史 cleanup 标记改写为 confirmed。详见[preflight accounting](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-preflight-accounting-20261029.json)，SHA-256 `10855d4f510c89f3cb3d0250316b226278a218bc6234230f24d918f8757902fc`。

### P4-06 audit-only gate review（2026-10-29）

- 按本计划 gate completion 的条件分支，外部 gate 阻塞时只完成可审计子项。feat-030/P4-05 记为 blocked 后，启动并完成 feat-031 的六 gate / denominator audit；该路径不绕过运行依赖，不构成全量 P4-06 或 P4-RELEASE 验收。
- [机器审计报告](../../outputs/acceptance/phase4-p406-gate-audit-20261029.json)，SHA-256 `0f73913be94e76bb86a3f83128f568abba9511c1dc091ba980d85fea10bef56b`。其复算保留 P4-01 的 6 positions / 18 paired attempts scoped pass；P4-02 的 60 positions / 180 slots（27 passed、9 failed、48 blocked、144 not_dispatched）；P4-03 的 39 个 recovery identity（3 recovered、36 unknown）；P4-05 的 3 个未 accepted / 未观察且 operation unknown 的 canary。
- 六 gate 裁决：P4-FIXTURE=`blocked`，P4-LINUX-BEHAVIOR=`failed`，P4-AUTOMATION=`blocked`，P4-WINDOWS-BEHAVIOR=`blocked`，P4-FIELD=`blocked`，P4-RELEASE=`blocked/not_run`。精确子集结论继续保持 scoped；catalog、Windows、field delivery 和 release claims 均不开放。
- feat-031 的 audit-only 子项完成，但 feature 仍因 feat-028/029/030 依赖未闭合而 blocked；固定发行 scope 的干净环境安装、启动、诊断、停止、回滚与数据恢复均未执行。完整 P4-RELEASE 仍 blocked，不能将 feat-031 记录解释为发行就绪。文档/证据验证记录 `outputs/acceptance/phase4-p406-verification-20261029.json` SHA-256 `46869a657874036a8b128643b4eba1dfb58038fab8bcfb868b5ee2250059e3df`。

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
- PR #4 已于 `2026-10-27T01:25:23Z` 合并至 `main`，merge commit 为 `3362b51276728619a4b008b834b2098075caad23`；该提交的 Linux/Windows CI 与 Phase 3 Linux self-test 均通过。P4-03 workflow 在同一 push 上的 run `40432474103` 以 workflow-file failure 结束且无 jobs。`gh workflow run` 随后返回 GitHub API 422，指出 job-level `env` 的 `runner.temp` 上下文无效；PR #5 将 run-scoped venv/cache 移至相关 step 的 `env`，PR [Linux/Windows CI](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40434531078) 和 [contracts/regressions](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40434531058) 全通过，并于 `2026-10-27T01:55:35Z` 合并为 `dd0a0206ef9db2a72232c29e4ec23092cee7e59a`。从 `main` 的 [workflow_dispatch guard run](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40435434502) 成功解析并通过 guard；由于两个 opt-in variables 仍关闭，受控 runner job 按设计跳过。该结果验证了默认分支 workflow 解析和拒绝路径，不是 runner 部署验收；one-shot runner 和真实 success/failure/cancel probes 仍待执行。
- runner workflow 修复发布后，仍须为一次性隔离 Linux runner 配置 `[self-hosted, linux, x64, ligamina-phase3]`、Docker daemon、冻结的 `Ligamina-artifact-cache`，并预置 manifest 指定的 digest-pinned Worker images。先设 repository variable `PHASE4_P403_RUNNER_ENABLED=true`，通过 protected Environment `phase4-p403-linux-controlled` 运行三种自有探针并人工取消 cancel 模式；检查 success/failure/cancel artifact 与清理后，再设 `PHASE4_P403_LINUX_RECOVERY_ENABLED=true`，派发 39 项 Linux recovery。Environment 不存放凭证。
- Windows 14 项保持未派发；现有 Evaluation/Audit 或错误码归因不满足 P4-04。继续前需管理员在 disposable Windows Sandbox guest 中确认 Enforcement 基线，并提供可审阅的 Sandbox-only signed policy/binary 证据；不得修改宿主机策略或放宽宿主基线。

### P4-03 executor / runner readiness revalidation（2026-10-27）

- P4 executor v1 曾从 `runtime-images.lock.json` 带入 Python digest `88bc855…`，但 P3-05 已完成 Linux campaign 的来源 manifest/report 中实际记录的是 `fcb712…`。生成器现严格校验 P4 parent manifest 指向的 P3 Linux manifest 与 completed report 的文件 hash、canonical hash、campaign、两生态 Worker image inventory 和 image ID；executor v2 以此来源绑定 Worker images，选择仍是原 39 个 Koishi parent identities。旧 executor 与历史结果保留，不覆盖。
- v2 executor canonical SHA `f3793ae3aef3edc2768cf6b0788e83f78e975d541123d7369a0da022491e9c93`，campaign `p4-p403-linux-5c052cc64e25`；parent validator 与 39-unit executor validator 均 `valid`。证据：[parent validation](../../outputs/acceptance/phase4-p403-validation-local-20261027-v2.json)、[executor validation](../../outputs/acceptance/phase4-p403-executor-validation-local-20261027-v2.json)。
- Ubuntu-24.04 WSL2 Docker 按 digest inspect 两个来源镜像成功；Koishi image 在 `--network none` 下输出 Node `v24.20.0`。新的本地 benign Worker preflight 共 10 项，报告整体 `passed=true`：5 passed、1 个预期非零退出 failed、4 个预期 timeout/output/resource/lost unknown，cleanup 10/10 confirmed，admission=`admitted`；这没有执行目录插件。证据：[image probe](../../outputs/acceptance/phase4-p403-worker-images-local-20261027-v2.json)、[Worker probes](../../outputs/acceptance/phase4-p403-linux-workers-local-20261027-v2.json)、[admission](../../outputs/acceptance/phase4-p403-linux-admission-local-20261027-v2.json)。
- Workflow 自托管标签收窄为 `[self-hosted, linux, x64, ligamina-phase4-p403]`，使 P4-03 一次性 runner 不匹配仍存在的 P3 `ligamina-phase3` workflow。P4-03 Python 回归 9/9、workflow static tests 5/5、相关 Bash `-n`、README check、harness 100/100 与 docs gate（20 documents / 434 links）通过；完整 command/output/hash 记录见 [local readiness verification](../../outputs/acceptance/phase4-p403-verification-local-20261027-v2.json)。GitHub Environment approval、真实 Actions artifact/cleanup 和 39 项 recovery 尚未执行，故 feat-028/P4-AUTOMATION 仍未完成。

### P4-03 locked input 与 archive-size override（2026-10-27）

- v2 本地 cache preflight 发现 39/39 Koishi artifact 文件内容 SHA-256 符合冻结 lock，但 lock 中 registry metadata size 与本地归档字节数不同；profile lock hash/size 均匹配。按执行器规则，v2 因缺少 archive-size overrides 为 `not_ready`，未启动 Worker 或插件。诊断保留于 [v2 cache preflight](../../outputs/acceptance/phase4-p403-cache-preflight-local-20261027-v2.json) 与 sidecar。
- P3-05 completed manifest 已封存通过 `_build_archive_size_overrides` 逐文件核验后的 override：每个条目绑定原 artifact lock SHA、artifact SHA、content-addressed cache path、metadata size 与实际 archive size。P4-03 v3 从已校验的 P3 manifest 中按原 39 个父 identity 提取 overrides；生成/验证器再核对 P3 manifest/report hash、completed campaign、lock hash、每条 identity、SHA、cache path 与 metadata size。恢复 scope 未改变。
- v3 executor canonical SHA `32c256a21f54b8c1bde758bb2fab54430daf4fb5fc1c3e947817c2af8ddb4cb4`，campaign `p4-p403-linux-1932fab55a8b`。本机 WSL2 cache preflight 现为 ready 39/39；报告：[v3 executor validation](../../outputs/acceptance/phase4-p403-executor-validation-local-20261027-v3.json)、[v3 cache preflight](../../outputs/acceptance/phase4-p403-cache-preflight-local-20261027-v3.json)。插件未启动。
- v3 代码定向测试 10/10、workflow static tests 5/5、相关 Bash `-n` 通过。 汇总证据见 [v3 local verification](../../outputs/acceptance/phase4-p403-verification-local-20261027-v3.json)，SHA-256 `bbb07fa81be5d018a7b3cbe74b570f3cc47a9e9555c0e2fb196a06f8b1c4e05f`，结论为 `passed_local_readiness_only`。PR #7 已以 merge commit `db87417ee41c665c588311cb073ad3cb4f4abd3d` 合并；v3 head 的 Linux、Windows 与 contracts/regressions checks 全通过。main guard run `40461641869` passed 并按关闭状态跳过 controlled job。2026-10-27 受控部署观察记录 WSL2 ephemeral runner id 21 online、runner gate=true、recovery gate=false；success probe run `40461965876` guard passed，但 protected job 仍等待 Environment owner approval。尚无 hosted probe artifact、Worker 或目录插件执行；feat-028/P4-AUTOMATION 仍未完成。

### P4-03 受控 runner 部署观察（2026-10-27，probe 前检查点）

- PR #7 已合并至 `main`（`db87417ee41c665c588311cb073ad3cb4f4abd3d`），Linux、Windows 与 contracts/regressions hosted checks 全通过。main guard run `40461641869` 在 runner opt-in 关闭时成功并按预期跳过 self-hosted job。
- WSL2 Ubuntu-24.04 one-shot runner `ligamina-p403-20261027-0632`（GitHub runner id 21，version 2.338.2）以专属 `ligamina-phase4-p403` label 在线；Docker daemon 29.1.3、两枚 digest-pinned Worker images 与 `/mnt/e/stella/Ligamina-artifact-cache` workspace symlink 已核验。Environment `phase4-p403-linux-controlled` 只允许 main、要求 owner reviewer，Environment secret count=0。
- 当时 repository variables 为 `PHASE4_P403_RUNNER_ENABLED=true`、`PHASE4_P403_LINUX_RECOVERY_ENABLED=false`。success probe run `40461965876` 已过 dispatch guard，受保护 job 等待 Environment owner approval。该部署检查点见 [runner deployment observation](../../outputs/acceptance/phase4-p403-runner-deployment-20261027-v1.json)，SHA-256 `4a3f5b005f85b081a3c53d446f7197939b9706ba40ed4d4e403d920bd8ec55e3`。
- 此处只记录 probe 前的 runner 部署状态；probe、artifact、清理及 recovery 的后续结果见下节。该检查点未派发插件 Worker 或 39 项 recovery。

### P4-03 hosted runner probes 与取消报告修复（2026-10-27）

- Success probe run `40461965876` 的 self-test `passed`，实际/预期退出码均为 0；outer cleanup confirmed，四个 run-scoped 临时路径均移除。Artifact `12714638549` 上传成功，SHA-256 `f3b3cf81c7a36184b1c3274dc70018c4752e76a3aecd3481a4b0cec04b69c13f`。
- Intentional failure probe run `40462916195` 的 workflow conclusion 为 failure（预期）；self-test 实际/预期退出码均为 23 且 `probe_expectation_met=true`。outer cleanup confirmed、artifact upload 成功；Artifact `12717121361` SHA-256 `fece5bb6620dbc83daa6ab36b59cc433575ad8492d3a45b18866c08df6c8e46a`。
- 首次 cancel probe run `40467433065` 的外层 run 为 cancelled，finalizer 与 artifact upload 步骤均成功，outer cleanup confirmed、4 个临时路径移除；但下载 artifact 显示 controlled report 仍为 `in-progress` 且缺少 runner self-test 终态文件。因此该 probe 不通过，不能只凭 run cancelled / cleanup 成功判定取消合同完成。Artifact `12717510819` SHA-256 `a54761b37dd312b8bbaa671a785ec9104cc305b6da29b15700707cac7f2dcd03`。
- 取消 run 的 artifact 显示 controlled report 仍 `in-progress` 且无 self-test 终态文件；结合此前 finalizer 只做队列/runner 临时资源核对的实现，证据与“取消终止 step 进程树前，Bash `EXIT` trap 未持久化报告”这一机制相符。已在 PR #8 加入 `lab.automation.phase4_p403_reports`：finalizer 获得 `job.status=cancelled` 后，根据取消日志 marker 与 outer-cleanup 报告收敛 interrupted controlled/self-test reports，并标示 shell trap 是否有报告。该解释属于基于 artifact 与实现的机制推断，需 hosted retest 验证。
- 修复在 PR #8 分支。本次增加针对已有非终态 self-test 报告的回归；其余本机验证命令与计数将在重跑后记录。尚需 PR hosted CI、合并至 main、重新注册 one-shot runner 并重跑 cancel probe；只有新 artifact 中终态报告、expectation、cleanup 和上传均核验后，才可启用 `PHASE4_P403_LINUX_RECOVERY_ENABLED` 并派发精确 39 个 Linux identities。Windows 14 项仍依 P4-04 admission 阻塞；所有插件仍未派发。完整证据：[hosted runner probe snapshot](../../outputs/acceptance/phase4-p403-runner-probes-20261027-v1.json)。

### P4-03 cancel retest and Linux recovery dispatch（2026-10-27）

- PR #8 已合并到 `main`，commit `a657f9103e33363bbec2dce9b3dc13f40dad1a4d`；PR 检查与 main Linux/Windows CI、Phase 3 Linux self-test 全通过。cancel retest run `40478669901` 使用一次性 WSL runner id 25；job setup、checkout、锁定 Node/uv 和 CPython 安装成功，随后探针步骤由 workflow cancel 中断。finalizer 与 artifact upload 都成功，run 结论为 `cancelled`。
- Artifact `12721079812`（`phase4-p403-linux-40478669901`，27,772 bytes，archive SHA-256 `3227d46d34e12b6e7e3ee8d8c09e9094fcb7e8a619eb23907285a2bdceb38fbc`）中的 controlled 与 self-test report 均为 `interrupted`，`probe_started=true`、`probe_expectation_met=true`、`external_cancellation_observed=true`；`shell_exit_trap_reported=false` 记录 GitHub 终止 step process tree 后没有 Bash trap 终态回执。`plugin_execution=not_started`、`worker_claimed=false`、Docker cleanup=`not_started`。outer cleanup confirmed，queue/evidence/venv/uv-cache 四个 run-scoped paths 均移除。self-test canonical SHA、controlled embedded evidence SHA、log SHA、cleanup sidecars 全部复核通过。可复查记录：[cancel retest report](../../outputs/acceptance/phase4-p403-runner-cancel-retest-20261027.json)；原始关键 reports 位于 run evidence directory。
- run `40473225877` 在 ActionManager 的 `Set up job` 阶段下载 pinned actions 超时，未进入受控 probe，无 artifact；该次按 infrastructure/setup failure 记录，不计作 probe。
- 在三项 runner probes 证据齐全后，repository variables 已设为 `PHASE4_P403_RUNNER_ENABLED=true` 与 `PHASE4_P403_LINUX_RECOVERY_ENABLED=true`。manifest `p4-p403-linux-1932fab55a8b` 只选择冻结的 39 个 Linux Koishi parent identities；run `40479738802` 的 dispatch guard 通过，protected job 等待 owner approval。该任务尚未领用 Worker，P4-AUTOMATION 仍 blocked，直至 recovery 结果、unknown 与 cleanup evidence 完整核验。Windows 14 项仍以 budget=0 等待 P4-04 admission；P3 的 53 个历史 unknown 保持不变。

### P4-03 Linux recovery result and remaining selection（2026-10-27）

- 用户批准 run `40479738802` 的 Environment deployment。dispatch guard、locked runtime setup、Linux preflight/admission、artifact upload 与 cleanup 均完成；run 结论为 `failure`，恢复报告 `status=interrupted`、campaign `p4-p403-linux-1932fab55a8b`、queue=`interrupted`。Linux preflight 的 10 项合同探针聚合 `passed=true`；admission=`admitted`。
- 原 recovery manifest 的 parent canonical/file SHA 为 `6097a58a2a058fd06d1f65edbea83a47aa8f3d07b31e7c8b679a6313bd51bd97` / `523272f94689f82ae85204974d52645f0327b3d2d0474aa8ae986652ef2bd7af`；executor v3 canonical SHA `32c256a21f54b8c1bde758bb2fab54430daf4fb5fc1c3e947817c2af8ddb4cb4`。报告 counts：recovered=3、plugin_or_stage_failed=0、unknown_or_cleanup_unconfirmed=11、not_run=25；`infrastructure_blocker=true`，Linux gate=`blocked`，plugin compatibility=`not_evaluated`。P3 的 53 条历史 unknown 均未改写。
- Attempt `ko-6fe5b7f33ce97eb5093b3005|linux|catalog|install` 记录 `dispatcher_executor_error` / `PilotWorkerError`，使 fail-closed campaign interrupted。可用报告只保存异常类型，没有 message/stack，不能确定该异常是输入、Worker 还是插件执行问题；该身份的单次 P4 retry budget 已消耗，保留 unknown 且不重跑。其他已尝试 identity 同样不重跑；部分 `worker_nonzero_exit` 的 stderr 记录有 `EAI_AGAIN gitee.com`，只证明断网 Worker 的一次外部 DNS 请求失败，不推广到其他插件。
- Docker cleanup report reconciled 44 attempts，44/44 cleanup confirmed；outer cleanup confirmed 并移除 queue、evidence、project venv、uv cache 四个 run-scoped paths。无 campaign-labelled Docker containers 残留。Actions artifact `12725309280`（`phase4-p403-linux-40479738802`，218,780 bytes，SHA-256 `f9fbaab3e896fe3707f4298aaa63d2e2b4f92c85c1f83bb8100f2e4d8c4d030b`）；报告证据 SHA、manifest SHA sidecar、Docker/outer cleanup sidecar 均已复核。可复查关键文件位于 [run evidence](../../outputs/acceptance/phase4-p403-run-40479738802/)。
- 范围扩展理由：此次 fail-closed interruption 后 25 个 Linux identity 明确 `not_run` 且无新 attempt；要完成 P4-03 的 39 项处置覆盖，可另建 campaign 仅选择这 25 个未尝试身份。新 manifest 必须 hash-bind 本次 report 与原 parent/executor，验证选集等于本次 `not_run` 集合且每项 attempts 为空，使用新 campaign ID；已尝试的 14 项及原 v3 manifest 保持冻结。若未满足这些条件，禁止再次派发。Windows 14 项仍保持 budget=0，等待 P4-04 admission；P4-AUTOMATION 在所有未派发、unknown 和 harness/infrastructure blocker 关闭前维持 blocked。
- v4 remaining executor 已由 `plan-p403-linux-not-run` 生成，campaign=`p4-p403-linux-remaining-16652ce93b5d`，只包含 25 个 Koishi identities，canonical manifest SHA-256 `8b458f0ed4607170dbb69ce592234f45ff07780f618be5c0e5c6319f0e7df9b0`，文件 SHA-256 `06b6b7f11df2bbbd361a49bd0ca191aae6c6defe2f0c6ec1e5ea6a31b120dd39`。CLI validator 结果 `valid`，selected_units=25，并对先前 recovery raw file SHA `87256bf520ec8a9267a6e71e8b37aa4cc1412b7a0b521690f6a3a36d9c1c0f2b`、prior executor 与 controlled report 作绑定。cache preflight 逐项重验 artifact 与 profile lock，25/25 ready、无缺失或 mismatch；未执行插件。P4-03 定向测试 14/14、workflow static tests 5/5、Bash `-n`、README evidence、docs gate（20 documents / 450 links / 12 indexed historical Markdown / 31 features）与 harness（100/100）均通过。完整本地记录：[v4 local verification](../../outputs/acceptance/phase4-p403-verification-local-20261027-v4-not-run.json)，SHA-256 `d962c2f021a40b664a1ace66ca188bd314214295bf6865c85dcb0a2ebc9f9fc8`，结论仅为 `passed_local_readiness_only`。新增 runner mode `linux_unknown_recovery_remaining` 复用同一双开关与受保护 Environment，runner 与 finalizer 均选择 v4 manifest。待 hosted CI 通过并合并后再派发 25 项 campaign。
- remaining-selection builder 允许沿 hash-bound 的 previous executor/report 链继续恢复：若 v4 campaign 再次中断，下一 manifest 只能选择它报告中的 `not_run` identities；递归校验会沿链拒绝任何历史已尝试身份。合成的二次中断回归已通过；该逻辑不自动重试 unknown/failed 身份。

### P4-03 remaining recovery preflight failure and dependency-scope correction（2026-10-27）

- PR #9 已合并为 main commit 444cf1cb9ebdff0d96b700773923584ab0cb9dc0；main CI run 40497600027 的 Linux、Windows jobs 与 Phase 3 self-test 40497600073 均成功。run 40499072419 在获批后通过 dispatch guard，v4 executor validation=valid，selected_units=25，campaign=p4-p403-linux-remaining-16652ce93b5d。
- Controlled job 122171933443 以 failure 结束，失败步骤为 linux_worker_probes。实际命令用 hosts/nonebot 项目执行 deploy/lab/linux/probes/acceptance.py；该 probe 只 import lab.workers 与 Python 标准库，但 hosts/nonebot 项目的默认 dev group 会拉入 nonebug -> async-asgi-testclient==1.4.11。其 source build 需要 setuptools>=40.8.0，uv 对 files.pythonhosted.org 的请求在三次重试、130.1 秒后超时。失败后同一 locked wheel 的 WSL HEAD 请求返回 HTTP 200、1.86 秒；这支持 transient network timeout 诊断，不证明 runner 网络持续稳定。
- Failure evidence 明确 plugin_execution=not_started；queue 不存在，attempts_reconciled=0，recovery report 未生成，故 25 个 v4 identities 均未尝试，仍保持可派发。Docker cleanup=not_started；outer cleanup=confirmed，queue/evidence/project venv/uv cache 四个 run-scoped paths 均移除。
- Actions artifact 12733155130（phase4-p403-linux-40499072419，45,448 bytes，SHA-256 cb3f2c7c8b1517822a0505f20659b58c66cb902a0c7aede01b9c2cce0911e465）；受控日志 SHA-256 71ea36262cfc3a3072534f514b4649b60c7ff3548f529d81696dba4e8823ab07，outer cleanup report SHA-256 8216393bf41c7e31f01017e991b138717a033fc69cded245ea9ec29e82a72f22。原始 ZIP、解包报告及校验汇总位于 [raw artifact](../../outputs/acceptance/phase4-p403-run-40499072419/artifact-12733155130.zip)，[hosted summary](../../outputs/acceptance/phase4-p403-run-40499072419/hosted-run-summary.json) SHA-256 6426139efee85a284a9e46f01d7f869ce8ee7620435cfebfd8258ae85bad3ac1。
- 故障回归显示依赖选择过宽：linux probe 只使用 lab.workers；之前 validation 步骤已经用 lab project 创建/预热 run-scoped 环境。脚本现将 probe 改为 uv run --locked --project lab --python 3.12.14，避免安装无关的 NoneBot dev/test 依赖。workflow static regression 在旧命令上失败（4/5），修改后通过（5/5）；Bash -n、README evidence、harness（100/100）、git diff --check 均通过，docs gate 最新复核为 20 documents / 458 links / 12 indexed historical Markdown / 31 features。详见 [local verification](../../outputs/acceptance/phase4-p403-probe-dependency-scope-local-20261027.json) SHA-256 23090cb6e0f4c6646dfbc88391b0730e2806566e12650d6bd388183ce9a814bd。该实现和证据在 codex/p403-probe-dependency-scope 分支，等待 hosted CI/合并。
- 下一步：通过 CI 并合并依赖范围修正后，确认新 one-shot runner 与两项 opt-in 变量，再以 linux_unknown_recovery_remaining 重派同一 v4 manifest。受保护 Environment 每个新 run 仍需按具体 run ID 获批。之后核对每个 identity、attempt、cleanup 和 artifact；只从报告明确 not_run 集合递归生成后续 manifest。P4-AUTOMATION 保持 blocked，Windows 14 项预算保持 0，P3 父状态不回写。

### P4-03 setup download interruption, profile-scoped unknowns, and v5 continuation（2026-10-27）

- run 40508582084 在 `Set up job` 下载 pinned `actions/upload-artifact` 时，codeload.github.com 三次下载均超过每次 100 秒 HttpClient timeout；plugin_execution=`not_started`、queue 未创建、attempts_reconciled=0、artifact 未创建。one-shot runner id 28 完成后自注销，不能把此 setup failure 计作插件尝试。summary `outputs/acceptance/phase4-p403-run-40508582084/hosted-run-summary.json` 的文件 SHA-256 为 `ddb93e92e190eb632a9d846e3d9783687247ece64466e1f05a6b252c6fc3152e`。
- 获批的 run 40513327262 使用 v4 campaign `p4-p403-linux-remaining-16652ce93b5d`，受保护部署、dispatch guard、locked Node/uv/Python、Linux probe 与 admission 均通过后进入 `p4_p403_unknown_recovery`。实际恢复结果：expected_units=25、terminal_units=8、recovered=0、plugin_or_stage_failed=0、unknown_or_cleanup_unconfirmed=9、not_run=16、cleanup_confirmed=8、infrastructure_blocker=true；gate health=`blocked`、plugin_compatibility=`not_evaluated`。
- 9 个已触及身份全保持 `unknown`，未改写 P3 历史状态或判定插件兼容失败。8 个 `worker_nonzero_exit` 日志显示 core-only Koishi profile 中缺少特定宿主服务（如 `ctx.logger`、`ctx.setInterval`、`ctx.http`）或插件要求的数据目录；另有断网 profile 的外部 DNS 请求失败。第 9 个 `koishi-plugin-gfm` 以 `PilotWorkerError` 结束，但 Dispatcher 报告未保留 message/stack，根因未定。8/9 行级 cleanup confirmed；独立 Docker exact-label reconciliation 确认 26 项清理，outer cleanup confirmed，queue/evidence/project venv/uv cache 四个 run-scoped paths 均移除。Docker 无残留的判断仅限此 campaign label reconciliation。
- 原始 Actions job log 保存在 `outputs/acceptance/phase4-p403-run-40513327262/actions-run-log.txt`，SHA-256 `0c8893b7d7727cbe11e51fb2788f786d4b6c26c3bea18ae604e893fc6c89f79f`。recovery report SHA-256 `8d657d9e282fd8284626f3dccd7d9f2f774edc227199045f309e5f7e8a56909b`，controlled JSON SHA-256 `874a5e3dfb254fc9af0d480f92cc8a1ee6a663f24b1fafcc010694c4e6eafb25`，controlled log SHA-256 `8924015df65d04650bd635d2ec0e6861a743ba5ef05503b1a92493571bad4c1d`，Docker cleanup report SHA-256 `8b5fea56e75b9ba0011cb2ccdb02212127bc435a0dddb13351652870fb0dbe0b`，outer cleanup report SHA-256 `5081129ab9f3c9f174f3d370495daacb590790615c9f16eb7370ff48`，runner-state archive SHA-256 `73d5491c5089515b2bc9a8885ff2beb5ff66e259f8d4e836d4f3f5f89e309272`。可复查解包证据位于 [405133 run evidence](../../outputs/acceptance/phase4-p403-run-40513327262/)。
- v5 continuation manifest campaign=`p4-p403-linux-remaining-0d659cb25128`，仅选择这 16 个 report `not_run` 身份，并沿 previous-report 链排除已触及身份；canonical SHA-256 `b5b51ee7c019b8dd869f914003ab86341216f4b6573fe51120ff42b2c90764c0`，文件 SHA-256 `9f4081696b39c4bf9022bf89fd9298e61494c8b4a932bd79190afe777679817e`。previous report SHA-256 为 `8d657d9e282fd8284626f3dccd7d9f2f774edc227199045f309e5f7e8a56909b`；executor validator=`valid` / selected_units=16，locked-input cache=`ready` 16/16，二者均为本地准备证据，未派发插件。新增受控 mode `linux_unknown_recovery_remaining_v5` 选择这份 manifest，保留双 opt-in guard、受保护 Environment 和原 cleanup 流程。

### P4-03 v5 recovery result and P4-04 host preflight（2026-10-27）

- run [40525975905](https://github.com/Eternal-Wanderer-Vegetable/Ligamina/actions/runs/40525975905) 以 `linux_unknown_recovery_remaining_v5` 在 `main` commit `1ba5a42363113a9bacb17db7c3b05e1d07b9cb08` 完成，workflow conclusion=`success`。受保护 Environment deployment、dispatch guard、locked Node/uv、pinned CPython、Linux admission 与 artifact 上传均成功。一次性 runner `ligamina-p403-20261027-v5`（runner id 30）在完成后注销；recovery mode 下 self-owned runner contract probe step 按设计 skipped。
- Executor validation=`valid`，selected_units=16，campaign=`p4-p403-linux-remaining-0d659cb25128`，manifest canonical SHA-256 `b5b51ee7c019b8dd869f914003ab86341216f4b6573fe51120ff42b2c90764c0`。Recovery report `expected_units=16`、`terminal_units=16`、`recovered=0`、`plugin_or_stage_failed=0`、`unknown_or_cleanup_unconfirmed=16`、`not_run=0`、`cleanup_confirmed=16`、`infrastructure_blocker=false`。Gate health=`blocked`，plugin compatibility=`not_evaluated`。全 16 项的 prepare passed、install unknown、native skipped、cleanup stage unknown；install observations 为 15 个 worker non-zero exits 与 `koishi-plugin-adapter-github` 的 1 个 worker timeout。日志里的缺少 Host API / config / data directory / native dependency、Java executable 与断网外部请求，仅作为此 core-only profile 的诊断；身份维持 unknown，不回写 P3 状态，也不宣称插件兼容失败。
- 本次 Docker exact-label reconciliation 覆盖 48/48 stage attempts，均有 cleanup confirmation；外层 runner cleanup confirmed 并移除 queue、evidence、project venv、uv cache 四个 run-scoped paths。报告中的 stage `cleanup=unknown` 与独立资源清理确认保持分开。runner image/admission 报告分别为 `passed=true` / `admitted`。
- GitHub artifact `12745507602`（`phase4-p403-linux-40525975905`，184,375 bytes）下载的 ZIP 与 API digest 完全一致：`3d3750c38cfd8753bf4f2582b0450d932dd08eb17a77246b694664870a0a3c4a`；`unzip -t`、内部 5 个 sidecars 与 controlled log SHA 均通过。summary、原始 ZIP、recovery/controlled/cleanup/admission/worker 报告及 runner-state archive 保存在 [run evidence](../../outputs/acceptance/phase4-p403-run-40525975905/)。
- P4-03 Linux 39 个 retry-budget identities 已全部具有一次新 observation（前序 run 40479738802 的 14 项、run 40513327262 的 9 项与本次的 16 项）；累计 3 recovered、36 unknown。该 batch budget 已耗尽；对这些 unknown 再派发前必须新增并记录 Host profile / fixture 的 scope 与身份规则，不能重复沿用 v5 manifest。Windows 10 pilot 与 4 preparation identities 仍为 budget=0，待 P4-04 Enforcement admission。P4-AUTOMATION 继续 blocked。
- P4-04 只读 host preflight：Windows 11 Pro for Workstations build 26200；WindowsSandbox.exe 与 hypervisor 存在；Win32_DeviceGuard.CodeIntegrityPolicyEnforcementStatus=2。该字段仅是 host 观察。Get-WindowsOptionalFeature 查询要求 elevation；guest policy IDs 与 Code Integrity events 尚未采集。
- P4-04 guest admission package 已准备于 [package README](../../deploy/windows/phase4-p404-guest-admission/README.md)：.wsb 禁用 network/vGPU/clipboard/peripheral redirection，采集 package 只读挂载，只有 run-scoped result directory 可写；批处理 launcher 写启动/退出日志，collector 读取 guest OS/runtime、Win32_DeviceGuard、CiTool policies 和 Code Integrity events 3076/3077/3089/3099，不改策略、不运行 native probe 或目录插件。2026-10-28 使用此 .wsb 启动后，client 窗口响应但 result directory 仍为空；host 上已有 vmwp/vmmemWindowsSandbox processes 创建于 2026-10-26 与 2026-10-27，本次启动未观察到新 VM worker，无法证明配置对应新鲜 guest 或 LogonCommand 执行。仅关闭本次创建的 RemoteSession client，未终止既有 worker。launch report [phase4-p404-launch-attempt-20261028.json](../../outputs/acceptance/phase4-p404-launch-attempt-20261028.json)，SHA-256 5e0efa7e77f922a46665c874e6416d28923600e84121786e3ee78ee87c9731df。需要先由用户关闭/确认旧 Sandbox sessions，之后重新启动采集；guest admission 仍 not_collected。本次状态同步的 docs gate 20 documents/473 links/31 features、README render、harness 100/100、collector PowerShell parser/WSB XML、git diff --check 均通过；命令与输出见 [outputs/acceptance/phase4-p403-p404-progress-20261028.json](../../outputs/acceptance/phase4-p403-p404-progress-20261028.json) SHA-256 ca35533bca3e005ef1250f8e69ffbc325c48621ba6a3b513cf7506af0fd3c5d4。
- PR #11 merge 后主分支 CI run `40523470389` 的 Linux 与 Windows jobs 均 success；Phase 3 Linux self-test `40523470558` success。PR #11 本身的 contracts/regressions 与 Linux/Windows CI checks 均通过。
- 当前 tracker 将 feat-028 标记 blocked，并激活依赖已满足的 feat-029 / P4-04；P4-03 的 Windows slice 将在 guest admission 后回收收尾。P4-05 和 P4-06 尚未开始。

### 2026-10-30 P4-05 Stella managed-runtime staging

- 本机 WSL Docker Engine 复用了锁定的 Koishi Echo 镜像；隔离数据卷已创建并完成 synthetic actor authority seed，loopback Ligamina Core ready，`p405-koishi-echo` Host active。Stella 托管进程已受控重启，runtime 与 OneBot healthy，扩展 import 成功。
- Stella managed revision 不重新读取运行中的 `.env`。9 个 `STELLA_LIGAMINA_*` 值已进入 desired revision，但尚未进入 applied revision；扩展因此仍 disabled，Gateway lease 也未出现。随机凭据只存在于 Stella 本地 `.env` 与受限 ACL 的 Core 配置中，报告没有凭据、账号/群号或消息正文。
- 需要通过已认证的 Microsoft Edge DevTools Console 同源请求应用 pending revision，或恢复支持的 UI automation；Stella WebUI 页面没有独立的 apply-pending 控件。应用后再确认 bridge enabled、Gateway lease，再请求一个全新 marker 消息。尚未发送 Ligamina field 消息；P4-FIELD 继续 blocked、feat-030 继续 in-progress，三个旧 direct-SnowLuma operation 仍 unknown 且不重放。
- 机器证据：[P4-05 Stella local readiness](../../outputs/acceptance/phase4-p405-live-20261030/phase4-p405-stella-local-readiness-20261030.json)，SHA-256 `a0ce34aac70336482027108b631c74d3fe4c1cfb151fd3cc52a08a8a08d58969`。

### 2026-10-30 P4-05 Stella WebUI apply attempt

- 用户在本机 Edge 打开了 Stella WebUI，并授权我自行调试。Loopback root read-only probe 返回 HTTP 200（页面标题 Stella 控制台）；managed status 为 controller reachable、Core ready，但仍使用此前 applied revision。mcp__cua_repl.js 初始化与本机 @oai/sky Computer Use 初始化均在窗口枚举/页面动作前失败，错误为 failed to write kernel assets: system path not found (OS error 3)。本轮没有 UI 点击、配置变更、凭据访问或真实平台发送。
- 用户随后说明 Stella WebUI 页面没有 Console，无法执行前述页面内脚本。源码核查确认设置页无独立 apply-pending 按钮；Advanced Config 保存要求真实字段改动，随后会创建/返回 revision 并自动应用。apply endpoint 需要 JWT auth，deploy CLI 无 apply 子命令。为避免引入未经授权的额外配置变更，不用任意设置保存触发 apply。当前可用路径是已登录的 Microsoft Edge DevTools Console 的同源请求，或恢复支持的 UI automation；Codex 内置浏览器的 CUA 与本机 @oai/sky 均在页面读取前报 OS error 3。
- 后续只读 `python -m deploy status --json` 显示 managed/alive/API/controller reachable、Core ready PID 30740，仍运行旧 revision `nmzdFZbbvPABKGwGkt69kA69v85K-V1-`；该 CLI 输出不展示 desired/applied revision 或 last operation。未改变配置、读取凭据或发送消息。应用后仍须验证 desired/applied 相同、bridge enabled groups=1、Gateway lease active，才请求新的唯一 marker。P4-FIELD 保持 blocked，三个旧 direct-SnowLuma unknown operation 不重放。apply-path assessment：outputs/acceptance/phase4-p405-live-20261030/phase4-p405-stella-apply-path-assessment-20261030.json，SHA-256 `39486d63236bff412a909f8ccac73af17cf557ad773bd1cd512411819f5ffdb4`。原 UI control attempt：outputs/acceptance/phase4-p405-live-20261030/phase4-p405-stella-webui-control-attempt-20261030.json，SHA-256 `0e7f133f32b00944fafe9911f3189b9898bba0e6fc6c79cb7c9446d68e67e240`.
