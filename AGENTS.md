# Ligamina agent entrypoint

Ligamina 是独立的插件联邦运行时与兼容性实验室：Rust Core + 原生 NoneBot/Python、Koishi/TypeScript Hosts。

## Startup Workflow

1. 确认仓库根目录，检查 `git status --short` 与最近提交。
2. 读取 [progress.md](progress.md)、[feature_list.json](feature_list.json) 和 [session-handoff.md](session-handoff.md)。
3. 按下表加载当前任务需要的指南，运行 `pwsh -NoProfile -File init.ps1 -Mode docs` 或 `bash init.sh docs`。
4. 修改运行时代码前按 [开发与验证](docs/guides/development.md) 选择并记录 baseline 检查。

## 全局约束

- **One feature at a time**：从清单选择一个依赖满足的任务，更新 status；扩展 scope 时先记录原因。
- 插件保留原始发布物；修补插件后的结果作为独立实验记录。
- 支持声明绑定插件版本、发布物哈希、Host profile、OS 和行为用例；skipped、unknown 与加载成功不等于行为通过。
- Core–Host stdout 仅输出 UTF-8 NDJSON 协议；日志输出 stderr，令牌和私人配置不进入报告。
- 事件/操作/回执保持账号与会话作用域；投递 unknown 保持未知，不自动宣称发送成功。
- 第三方全量运行使用 OS 隔离 Worker；现有临时目录和环境隔离不是 OS 安全沙箱。
- 当前指南随代码更新；带日期的计划、审阅和验收保留历史语境，通过新证据更新当前状态。

约束来源、适用范围和复审条件见 [文档维护](docs/guides/documentation.md)。

## 按任务加载

| 触发条件 | 必读材料 |
| --- | --- |
| 修改 Core、Host、Client 或协议 | [架构与接口边界](docs/guides/architecture.md) |
| 构建、测试、安装、CI 或运行诊断 | [开发与验证](docs/guides/development.md) |
| 插件来源、全量扫描、差分测试或支持结论 | [兼容性实验](docs/guides/compatibility.md) |
| CLI/实验报告输出或 README 结果展示 | [输出规范](docs/guides/reporting.md) |
| 第二阶段实施与任务验收 | [第二阶段计划](docs/plans/2026-10-12-phase-2-full-catalog.md) |
| 修改文档、指令、状态或历史索引 | [文档维护](docs/guides/documentation.md) |
| 查第一阶段过程与旧裁决 | [证据索引](docs/evidence/README.md)；只加载相关记录 |

## Verification Commands

- 文档：`pwsh -NoProfile -File init.ps1 -Mode docs` / `bash init.sh docs`。
- 运行时：`pwsh -NoProfile -File init.ps1 -Mode full` / `bash init.sh full`，复用已有 build/test/smoke 入口。
- 全量插件测试属于第二阶段待实现入口；现有 representative 命令验证固定样本。

## Definition of Done

- 当前任务 done criteria 全部满足，要求的验证实际执行并保存 command and output / Evidence。
- `feature_list.json` 的 done 项包含证据，`progress.md` 区分本次检查和历史验收。
- README 与指南对应当前实现；未执行与未验证范围明确记录，仓库保持 restartable。

## End of Session

1. 更新任务状态、验证证据、失败与下一步。
2. 更新 progress 和 handoff，写明可复现命令和阻塞条件。
3. 检查 diff，保留可恢复的工作现场；仅按本次任务授权提交或发布。
