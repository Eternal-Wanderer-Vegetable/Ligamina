# 历史与验收证据索引

适用：查阅特定日期下的设计、执行和验收。当前状态见 [progress](../../progress.md) 与 [T18 最新裁决](../../outputs/acceptance/t18-final-audit-20261012.json)。旧 NOT READY、skipped 和中断结果保留原文。

## 历史 Markdown

- [outputs/2026-10-09-ligamina-discussion-decisions.md](../../outputs/2026-10-09-ligamina-discussion-decisions.md)
- [outputs/2026-10-09-ligamina-engineering-plan.md](../../outputs/2026-10-09-ligamina-engineering-plan.md)
- [outputs/2026-10-09-ligamina-initial-plan.md](../../outputs/2026-10-09-ligamina-initial-plan.md)
- [outputs/2026-10-09-ligamina-mvp-checklist.md](../../outputs/2026-10-09-ligamina-mvp-checklist.md)
- [outputs/2026-10-09-ligamina-mvp-contracts.md](../../outputs/2026-10-09-ligamina-mvp-contracts.md)
- [outputs/2026-10-09-workspace-migration.md](../../outputs/2026-10-09-workspace-migration.md)
- [outputs/2026-10-10-ligamina-t13-third-party-sample-research.md](../../outputs/2026-10-10-ligamina-t13-third-party-sample-research.md)
- [outputs/acceptance/t13-wsl-network-limitation-20261010.md](../../outputs/acceptance/t13-wsl-network-limitation-20261010.md)
- [outputs/acceptance/phase2-s211-failures-20261014.md](../../outputs/acceptance/phase2-s211-failures-20261014.md)
- [outputs/acceptance/phase4-p404-signed-runtime-20261028/README.md](../../outputs/acceptance/phase4-p404-signed-runtime-20261028/README.md)
- [outputs/acceptance/windows-sandbox/README.md](../../outputs/acceptance/windows-sandbox/README.md)
- [outputs/compat/compatibility-matrix-v1.md](../../outputs/compat/compatibility-matrix-v1.md)（自动生成的当前兼容性矩阵摘要，P6-04）

## 设计记录

- [ADR 0001](../adr/0001-baseline.md)：2026-10-09/10 工具链、协议与阶段性实现；其中旧 G2 状态不是当前裁决。

## 第一阶段关键验收

- [T18 / C01–C15](../../outputs/acceptance/t18-final-audit-20261012.json)
- [Windows 代表样本](../../outputs/acceptance/t13-representative-windows-full-20261010.json)
- [WSL Linux 代表样本](../../outputs/acceptance/t13-representative-wsl-final-20261010.json)
- [WSL 24 小时稳定性](../../outputs/acceptance/t14-stability-linux-wsl-100x24h-20261011.json)
- [Windows Sandbox C14](../../outputs/acceptance/t15-windows-sandbox-c14-20261011.json)
- [Linux 容器 smoke](../../outputs/acceptance/t16-linux-wsl-container-20261010.json)

## 第二阶段目录实验

- [S2-10 全快照 census](../../outputs/acceptance/phase2-s210-census-interim-20261012.json)
- [S2-11 失败与未知归因](../../outputs/acceptance/phase2-s211-failures-20261014.json)
- [S2-11 定点 Worker 回归](../../outputs/acceptance/phase2-s211-process-exit-regression-20261014.json)
- [S2-12 最终审计与 gate 裁决](../../outputs/acceptance/phase2-s212-audit-20261017.json)
- [S2-12 官方目录兼容矩阵](../../outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md)

## 第三阶段 gate 审计

- [P3-10 五 gate 审计](../../outputs/acceptance/phase3-p310-gate-audit-20261026.json)：独立复算 P3-05/P3-08 分母，分别裁决 engine、recovery、automation、behavior 与 field gate。
- [P3-10 受影响单元矩阵](../../outputs/acceptance/phase3-p310-compatibility-matrix-20261026.md)：3,179 个 P3-05 unique units 的终态、归因、清理状态与来源报告。
- [P3-08 双平台行为账本](../../outputs/acceptance/phase3-p308-behavior-accounting-20261026.json)：120 个位置、360 个 attempt slots；缺少 executable Host fixture，0 dispatch，behavior gate 未评估。

## 第四阶段行为与运行就绪

- [P4-04 恢复原 Enforcement scope 的计划与状态](../plans/2026-10-26-phase-4-behavior-readiness.md)：runner-only 子任务保留为历史完成项；当前恢复 guest policy 准入与目录行为验收。
- [P4-05 field candidate preflight（历史快照）与后续 live canary](../plans/2026-10-26-phase-4-behavior-readiness.md)：Koishi Echo synthetic ingress 的三次独立 outbound attempt 均收到 SnowLuma/QQNT `result=120` 拒绝，API 未接受、接收端 marker 未观察，Core operation 保持 `unknown`，`P4-FIELD=blocked`。SnowLuma v2.2.1 仍没有 `120` 映射；第三方另有空 `errMsg` 的偶发拒绝观察，但缺样本量/版本/TRACE，只能作为低置信度瞬时拒绝线索，不能解释本次三连失败。详见[result=120 原始诊断](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-result120-diagnosis-20261029.json)、[上游复查](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-result120-upstream-review-20261029.json)和[preflight accounting](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-preflight-accounting-20261029.json)。三个旧 operation 不重放，也不再由 Ligamina 直连 SnowLuma；用户指定的新现场回复须经过现有 Stella 正常 ingress 路径，详见下一条。
- [P4-05 Stella-path preflight](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-path-preflight-20261029.json)：当前 Stella 6.3.0 process 与 OneBot link 只读可达，但 deploy manager 状态不一致；WebChat 是虚拟会话且没有 QQ send API。本次未发消息。未来新的 field reply 必须由真实群 ingress 触发并由现有 Stella process 发送；三个 direct-SnowLuma canary 仍是 unknown，不能计作 Stella 端到端验收。`P4-FIELD=blocked`，等待一条新鲜入站触发与接收端观察。
- [P4-05 Stella live ingress observation](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-ingress-observation-20261029.json): preflight 后窗口内记录 40 个被动入站 trace、0 个 delivered；最新 trace 在 capture_closed 后结束，没有正常 chat generation/send 或接收端观察。消息内容和标识未读取，无法关联到指定输入；需要新鲜唯一 @Stella 文本触发。feat-030=in-progress，P4-FIELD=blocked。
- [P4-05 Stella outbound logger recheck](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-outbound-log-check-rerun-20261029.json): 当前 logger 只保存投递元数据而不保存正文；活动/轮转日志无 outbound delivery 记录。最新 indexed flow trace 仍以 passive_only/capture_closed 收口，生成与发送事件均为 0；不据此归因用户消息，P4-FIELD 保持 blocked。SHA-256 `a12e1f7be12d09beec2990c2bc272e8d61a7d5aaa32431be99d6c78929bc39c2`。
- [P4-05 Stella native group reply observation](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-native-reply-20261029.json): 用户报告“晚上好”@Stella 已收到预期回复；对应 native `ai_gateway` trace 与 OneBot outbound 均成功。该路径没有 Ligamina Client/Gateway/Host/operation 节点，另一个 distinct source trigger 的接收未确认，所以仅作 Stella-native scoped positive，P4-FIELD 仍 blocked。SHA-256 `35412ba3925ac57ae94b23bfd9003bedd7df5f8035a2979063dc7e58a47bf1bd`。
- [P4-05 Stella/Ligamina adapter readiness](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-ligamina-adapter-readiness-20261029.json): 源码盘点确认两端已有的 client/core 与 Stella outbound seam 尚无 connector；正常 Stella 回复未穿过 Ligamina operation 链。`feat-030=in-progress`、`P4-FIELD=blocked`，实现 connector 是下一项工程前置条件。SHA-256 `629074a3c5321777cc17aa4ca275661ed7465b1a8fd07e802be78d8da53b3c33`。
- [P4-05 Stella/Ligamina bridge implementation](../../outputs/acceptance/phase4-p405-live-20261029/phase4-p405-stella-ligamina-bridge-implementation-20261029.json)（SHA-256 `66096e3d024321ad02a1741b3472e73a516b190bc65e66563463e911558769b6`）：已实现默认关闭的可移植 Stella 扩展并暂存到本机 Stella checkout 的 `extensions/ligamina_bridge`（untracked，源/目标 hash 一致），含 loopback Core Client、群 ingress 映射、Host 身份/账号/群/文本校验、通过 Stella `OutboundService` 出站和 unknown 不重放。13/13 adapter tests、CPython 3.12.10 source/staged compile 与 disabled stdlib-only import 通过；operation/event/trace correlation logs 已过滤验证；尚未配置启动环境、加载进程或发送 Ligamina 消息。`feat-030=in-progress`、`P4-FIELD=blocked`，等待本机环境配置、当前进程重启及新鲜 field 场景。
- [P4-06 audit-only six-gate review](../../outputs/acceptance/phase4-p406-gate-audit-20261029.json) 与[本地验证记录](../../outputs/acceptance/phase4-p406-verification-20261029.json)：六个 gates 与 P4-01 至 P4-05 已有分母按来源报告复算；P4-FIXTURE blocked、P4-LINUX-BEHAVIOR failed、P4-AUTOMATION/P4-WINDOWS-BEHAVIOR/P4-FIELD/P4-RELEASE blocked。外部依赖未满足时只交付审计子项；没有执行发行安装/回滚，不能宣称 P4-RELEASE 或候选发行 ready。
- [P4-04 signed Node runtime 探针包](../../deploy/windows/phase4-p404-signed-runtime-probe/README.md)：断网 Sandbox 中运行已签名 Node 与 built-in-only JS；不安装或执行目录插件。
- [P4-04 Node runtime 结果摘要](../../outputs/acceptance/phase4-p404-signed-runtime-20261028/README.md)、[guest report](../../outputs/acceptance/phase4-p404-signed-runtime-20261028/signed-runtime-probe.json) 与 [host review](../../outputs/acceptance/phase4-p404-signed-runtime-review-20261028.json)：记录 exact runtime hash/signature、marker 清理、guest policy 查询限制和 Sandbox stop 结果；结论仅 `passed_runtime_only`。
- [P4-04 最新 Enforcement policy/profile review](../../outputs/acceptance/phase4-p404-policy-preflight-20261028-v3.json)：记录当前 Event 3099/3077 base identity 关联、CiTool inventory 限制、Koishi scoped Native behavior 3-run 结果、NoneBot 3.12.10 native extension 的精确策略阻止及当前 gate blocker。
- [P4-04 Koishi Windows behavior probe](../../deploy/windows/phase4-p404-koishi-behavior/README.md)：绑定冻结 Echo entry、case-selectable frozen fixture、combined Host provider lock、Stock Koishi `Bot.dispatch`、Oracle、Code Integrity 输出与清理。Native isolation 为 [run3](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-run3.json)、[run4](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-run4.json)、[run5](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-run5.json) 通过；positive 为 [run1](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-positive-run1.json)、[run2](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-positive-run2.json)、[run3](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-positive-run3.json) 通过；negative 的首次 backend unknown 保留，之后 [retry1](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-negative-retry1.json)、[run2](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-negative-run2.json)、[run3](../../outputs/acceptance/phase4-p404-koishi-behavior-20261028-negative-run3.json) 通过。三类 Native case 各 3 次成功，不构成 Federated/full catalog 声明。
- [P4-04 NoneBot signed CPython 3.12.10 profile lock](../../outputs/acceptance/phase4-p404-nonebot-python31210-profile-lock-20261028.json) 与 [NoneBot load pilot entrypoint](../../scripts/phase4-p404-nonebot-load-pilot.py)：profile 与原 3.12.14 身份分离；后续事件关联把 `_pydantic_core` DLL 拦截绑定到 Code Integrity 3077 和精确 wheel member hash。详细结果见 [NoneBot Enforcement review](../../outputs/acceptance/phase4-p404-nonebot-load-20261028-v4.json) 和 [latest retry](../../outputs/acceptance/phase4-p404-nonebot-load-20261028-v5.json)；两次均保留 unknown，安装/cleanup 通过不代表目录兼容。
- [P4-04 policy/profile preflight 历史 v1](../../outputs/acceptance/phase4-p404-policy-preflight-20261028.json)：记录当时的 external cache artifact/lock hashes、Koishi 172 个 archive closure、NoneBot 3.12.14 blocker 与未完成 policy inventory。最新裁决以 v3 为准。
- [P4-04 runner-only scope 与验收结果](../../outputs/acceptance/phase4-p404-runner-behavior-20261028/README.md)：已完成的独立 runner 行为子任务；不构成 Enforcement 策略准入或目录插件兼容结论。
- [P4-04 Windows native probe 与 App Control 管理员审阅包](../../outputs/acceptance/phase4-p404-native-probe-20261028/README.md)：记录自有 PE 的精确哈希、Event 3077/3089 关联、拦截策略、未评估的目录准入和 Sandbox-only 后续审阅边界。
- [P4-04 native probe 机器审阅报告](../../outputs/acceptance/phase4-p404-native-probe-review-20261028.json)：hash-bound 的 guest、探针、事件和策略归因。
- [P4-04 自签名 `.pyd` 实验](../../deploy/windows/phase4-p404-selfsigned-signature-probe/README.md)：guest-only 自签名与签名副本 LoadLibrary 探针；[机器审阅报告](../../outputs/acceptance/phase4-p404-selfsigned-code-signing-20261028/selfsigned-signature-review.json) 记录 guest 本地签名验证成功、Enforcement 仍报 Win32 4551、未捕获匹配 3077/3089 与隔离清理；[follow-up verification](../../outputs/acceptance/phase4-p404-selfsigned-code-signing-20261028/selfsigned-followup-verification.json) SHA-256 `7179c1a766bf2d28c3f49f043157d6cf76422920888f6b8769b10616a01d1755` 仅验证文档、报告 sidecar 与探针语法。
- [P4-04 local Core signing preflight](../../outputs/acceptance/phase4-p404-core-release-signing-preflight-20261028.json)：GitHub CLI returned no published releases; local debug/release Core executables are both `NotSigned`. This is a signing readiness snapshot, not a signed release or Windows behavior result.
- [P4-04 self-signed Core runtime probe package](../../deploy/windows/phase4-p404-selfsigned-core-probe/README.md)：records bounded v1-v8 attempts; [latest machine review](../../outputs/acceptance/phase4-p404-selfsigned-core-20261029-v8-guest-trust/selfsigned-core-review.json) shows guest-only Root/TrustedPublisher trust made SignTool verification pass but Core startup remained blocked with Win32 4551. Target file-event attribution is incomplete; guest Root-store deletion was not confirmed, while Sandbox teardown and host-store absence were confirmed. This does not satisfy Windows admission or behavior acceptance.
- The v8 local integrity checks are summarized in [follow-up verification](../../outputs/acceptance/phase4-p404-selfsigned-core-followup-verification-20261029.json), SHA-256 `4233b763b8ab23d59236e3d86f1b5eb095d5dd070c11e6022e814e2104c662fa`; these checks do not change the Enforcement gate.

## 插件工具面

- [TOOL-04 tool-surface Linux campaign](../../outputs/acceptance/tool04-tool-surface-linux-20261107-v1.json)（SHA-256 `a1453dffec0095547eb00737efd583ee1f74fa96a5778a415d0625805d0f41fd`）：冻结 echo 与 roll 经 MCP 命令工具在 18 个全新禁网 Worker 中 18/18 通过，清理全部确认，Gateway 操作 0；准入见 [admission](../../outputs/acceptance/tool04-linux-admission-20261107.json)。authority 拒绝路径未被触发，详见[插件工具面计划](../plans/2026-11-07-plugin-tool-surface.md) 3.6。
- [AB-04 AstrBot Linux install/load pilot](../../outputs/acceptance/ab04-astrbot-linux-pilot-20261108-v4.json)（SHA-256 `9605165c1f49e5bc83a55404b218a05261d5f79af3b2f43826ec83e32dff2344`）：未修改的 AstrBot 4.29.1 在 24 个全新禁网 Worker 中逐个加载分层选取的市场插件，21 passed、1 failed、2 skipped、unknown 0，清理全部确认；准入见 [admission](../../outputs/acceptance/tool04-linux-admission-20261108.json)。加载不等于行为兼容，详见 [AstrBot Host 计划](../plans/2026-11-07-phase-5-astrbot-host.md) 3.1。
- [AB-05 AstrBot tool-surface campaign](../../outputs/acceptance/ab05-astrbot-tool-linux-20261108-v2.json)（SHA-256 `33e2e6871ac41efdd3923c28c9c6b49ada6f8c09dbe3708ea73d15f84405bafb`）：冻结的 teamup、gallery、pet 三个市场插件经 MCP 命令工具 8 个用例 ×3 次，24 个全新禁网 Worker 全部通过、清理全部确认，Gateway 操作 0；Oracle 引用冻结源码并在派发前复核。gate `astrbot_tool_linux=passed_scoped`，仅限工具级，详见 [AstrBot Host 计划](../plans/2026-11-07-phase-5-astrbot-host.md) 3.2。

## 第七阶段自动化验证

- [P7-04 30+30 differential pilot](../../outputs/acceptance/p7-p704-differential-pilot-20261110-retry3.json)（SHA-256 `2c6f4c65402cb986aec39fb1df3f9a01589a16e2c39d9926baded357dc51be5e`）：60 条目全部有终态；21/133 nontrivial，123/133 unknown，未达到全量 P7-09 准入门。
- [P7-05 C1–C8 contract adjudication](../../outputs/acceptance/p7-p705-contract-pilot-20261110.json)（SHA-256 `e7fd56592fda040994dcb7e77ac823ddb3aa29a405b6db8655f8730882a71769`）：60×8 结论，16 个自有正反观察夹具通过；P7-04 证据支持 12 个 C1 pass，其余 468 格显式 unknown。
- [P7-06 synthetic mutation regression](../../outputs/acceptance/p7-p706-mutation-regression-20261110.json)（SHA-256 `1efaf4d23b2014bd5fb563195e511b02659c5d5d2b4c2d2f7d44eaf47239e147`）：10/10 自有合成变异被检出；fixture 1、contract 3、differential 5、stateful 1。只衡量合成观察 oracle 敏感度，不代表生产 Core/Host 变异覆盖。

其他 JSON、锁和 traces 保留在 outputs/acceptance 下，由相应报告和历史文档引用；S2-11 复跑使用独立文件名和 SQLite 队列，不能覆盖旧 S2-10 acceptance。
